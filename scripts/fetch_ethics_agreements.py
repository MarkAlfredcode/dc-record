#!/usr/bin/env python3
"""Ethics Agreement Tracker (added 2026-10-03): checks what Senate-confirmed
officials PROMISED in their OGE ethics agreements against what they DID.

The FAA-chief story (Bryan Bedford dumping Republic Airways stock months
after his ethics-agreement deadline) reproduces mechanically here: his
agreement commits him to divest Republic within 90 days of confirmation;
his 278-Ts show Republic sales in February 2026.

Sources, all keyless or on the existing api.data.gov pool:
- OGE's full disclosure index (oge_index.py) -- ethics agreements,
  compliance certifications, 278-Ts, amendments.
- congress.gov nominations for the current Congress -- Senate confirmation
  dates (deadlines run from confirmation, not from the agreement).

Checks:
1. Overdue certifications -- confirmed > deadline + 30 days ago, no
   Certification of Ethics Agreement Compliance posted (AM edition only).
2. Late certifications posted in this window.
3. 278-Ts posted in this window showing a sale/exchange of an asset the
   official promised to divest, dated after the divestiture deadline.
4. Trade timing (data/events.json) for every 278-T PDF posted in the window.
5. "What changed" diffs for amended OGE filings posted in the window, when
   both versions are public PDFs.

Agreement PDFs are parsed once and cached in data/ethics-agreements.json
(committed) -- first run reads ~650 agreements, later runs only new ones.

Output: extras/officials/ethics_agreements.json
"""
import concurrent.futures as cf
import json
import os
import re
import sys
import tempfile
from datetime import date, timedelta

import oge_index
import trades
from stock_act_ledger import Ledger, history_phrase
from jurisdiction import Jurisdiction
from dc_common import download, http_get, pdftotext, today_la
from dc_extras import (
    SeenManifest, esc, link, load_json, lookback, mdY, norm, run_mode, section, write_extra,
)

CACHE_PATH = "data/ethics-agreements.json"
SEEN_PATH = "data/ethics-tracker-seen.json"
SINCE = "2025-01-20"  # current administration
GRACE_DAYS = 30  # OGE posts certifications with a lag; don't flag inside it
CONGRESS = 119
STOP = {"inc", "corp", "corporation", "company", "co", "llc", "ltd", "plc", "holdings", "holding", "group",
        "the", "and", "class", "common", "stock", "fund", "trust", "shares", "etf", "ishares", "lp", "l.p",
        "of", "services", "international", "technologies", "technology", "partners", "capital", "na", "sa", "ag"}


def congress_keys():
    return [k.strip() for k in os.environ.get("FEC_KEYS", "").split(",") if k.strip()]


def confirmations():
    """{(last, first3): [confirm_date, ...]} for civilian nominations."""
    keys = congress_keys()
    out, off, i = {}, 0, 0
    while True:
        url = (f"https://api.congress.gov/v3/nomination/{CONGRESS}?limit=250&offset={off}&format=json"
               f"&api_key={keys[i % len(keys)]}")
        i += 1
        status, body = http_get(url, headers={"User-Agent": "dc-record-bot"}, timeout=60)
        if status >= 400:
            raise RuntimeError(f"congress.gov nominations HTTP {status}")
        d = json.loads(body)
        for n in d.get("nominations", []):
            la = n.get("latestAction") or {}
            if not (n.get("nominationType") or {}).get("isCivilian") or "Confirmed" not in la.get("text", ""):
                continue
            if not n.get("description"):
                continue
            toks = norm(n["description"].split(", of ")[0].split(",")[0]).split()
            toks = [t for t in toks if t not in ("jr", "sr", "ii", "iii", "iv")]
            if len(toks) >= 2:
                out.setdefault((toks[-1], toks[0][:3]), []).append(la["actionDate"])
        if not (d.get("pagination") or {}).get("next"):
            return out
        off += 250


def name_key(oge_name):
    last, _, first = (oge_name or "").partition(",")
    lt = [t for t in norm(last).split() if t not in ("jr", "sr", "ii", "iii", "iv")]
    ft = norm(first).split()
    return (lt[-1] if lt else "", ft[0][:3] if ft else "")


def parse_agreement(text):
    """Deadlines come from sentence context, not any "N days" phrase --
    "not later than 15 days after my appointment" is the resignation/ethics-
    briefing clause, not the divestiture window (that mistake set Bedford's
    deadline to 15 days in testing)."""
    flat = re.sub(r"\s+", " ", text)
    sentences = re.split(r"(?<=[.;])\s+", flat)
    win = r"(\d{2,3})\s+days\s+(?:of|after|following)\s+my\s+(?:confirmation|appointment)"
    cert_days = [int(m) for snt in sentences if "Certification of Ethics Agreement Compliance" in snt
                 for m in re.findall(win, snt, re.I)]
    divest_days = [int(m) for snt in sentences if re.search(r"\bdivest", snt, re.I)
                   for m in re.findall(win, snt, re.I)]
    divest, active, gap = [], False, 0
    for line in text.splitlines():
        s = line.strip()
        if re.search(r"\bdivest", s, re.I) and "Certificate of Divestiture" not in s:
            active, gap = True, 0
        b = re.match(r"^(?:x|•|▪|◦|-)\s+(.{3,120})$", s)
        if active and b:
            item = b.group(1).strip().rstrip(";,.")
            if not re.match(r"(?i)any\s", item):
                divest.append(item)
            gap = 0
        elif active and s:
            gap += 1
            if gap > 6:
                active = False
    # Divestitures written as prose rather than bullets, e.g. Bedford's
    # "...financial interests of Republic, until I have divested it".
    for pat in (r"(?:financial )?interests? of ([A-Z][\w&.'\- ]{1,50}?),? until I have divested",
                r"divest (?:my|all of my|our) (?:financial )?(?:interests?|equity|shares|stock|holdings?|position) in ([A-Z][\w&.'\- ]{1,50}?)(?=\s+(?:as soon|within|not later|no later|by|prior)|[.;,(])"):
        for m in re.finditer(pat, flat):
            ent = m.group(1).strip()
            if ent.lower() not in ("the entity", "the fund", "the company", "each entity") and ent not in divest:
                divest.append(ent)
    return {"cert_days": min(cert_days) if cert_days else 90,
            # longest stated divestiture window -> conservative "sold late" checks
            "divest_days": max(divest_days) if divest_days else 90,
            "divest": divest[:80], "v": 3}


def sig_tokens(s):
    return [t for t in norm(s).split() if len(t) >= 3 and t not in STOP]


GENERIC_FIRST = {"jpmorgan", "vanguard", "fidelity", "schwab", "spdr", "franklin", "invesco", "american",
                 "first", "united", "general", "national", "global", "blackrock", "pimco", "goldman", "morgan",
                 "wells", "bank", "energy", "select", "msci", "ftse", "betabuilders"}


def matches_divest(asset, divest_items):
    """A sale matches a promised divestiture when the ticker matches, or the
    item's first distinctive word appears in the asset name -- requiring the
    second word too when the first is a generic fund-family name (otherwise
    "JPMorgan BetaBuilders Canada ETF" matched a JPMorgan money-market fund
    in testing)."""
    at = set(sig_tokens(asset))
    a_tick = trades._ticker(asset)
    best, best_score = None, 0
    for item in divest_items:
        i_tick = trades._ticker(item)
        if a_tick and i_tick and a_tick == i_tick:
            return item
        it = sig_tokens(item)
        if not it or it[0] not in at:
            continue
        if it[0] in GENERIC_FIRST and (len(it) < 2 or it[1] not in at):
            continue
        score = len(set(it) & at)
        if score > best_score:
            best, best_score = item, score
    return best


def fetch_pdf_text(url, tmp):
    path = os.path.join(tmp, re.sub(r"\W", "_", url)[-90:] + ".pdf")
    try:
        download(url, path, timeout=60)
        return pdftotext(path)
    except Exception as e:  # noqa: BLE001
        print(f"pdf fetch failed {url}: {e}", file=sys.stderr)
        return ""


def main():
    today, start, am = today_la(), lookback(), run_mode() == "am"
    errors = []
    try:
        rows = oge_index.fetch_all()
    except Exception as e:  # noqa: BLE001
        write_extra("officials", "ethics_agreements", [], f"Ethics Agreement Tracker unavailable: OGE index fetch failed ({e}).")
        return
    try:
        confirmed = confirmations()
    except Exception as e:  # noqa: BLE001
        confirmed = {}
        errors.append(f"congress.gov confirmations unavailable ({e}) -- deadline checks skipped")

    for r in rows:
        r["_label"] = oge_index.label(r.get("type"))
        r["_base"] = oge_index.base_label(r.get("type"))
        r["_link"] = oge_index.doc_link(r.get("type"))
    by_name = {}
    for r in rows:
        by_name.setdefault(r.get("name"), []).append(r)

    # ---- parse (and cache) every ethics agreement since SINCE ----
    cache = load_json(CACHE_PATH, {}) or {}
    agreements = [r for r in rows if r["_base"].startswith("Ethics Agreement") and r.get("docDate", "") >= SINCE
                  and oge_index.is_pdf(r["_link"])]
    todo = [r for r in agreements if (cache.get(r["_link"]) or {}).get("v") != 3]
    tmp = tempfile.mkdtemp()
    if todo:
        print(f"parsing {len(todo)} new ethics agreements", file=sys.stderr)
        with cf.ThreadPoolExecutor(6) as ex:
            for r, text in zip(todo, ex.map(lambda r: fetch_pdf_text(r["_link"], tmp), todo)):
                if text.strip():
                    cache[r["_link"]] = {"name": r["name"], "docDate": r["docDate"][:10], **parse_agreement(text)}
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=1, sort_keys=True)
        f.write("\n")

    # ---- per-official status ----
    people = {}
    for r in agreements:
        people.setdefault(r["name"], []).append(r)
    overdue, late_cert, tracked = [], [], {}
    for name, eas in people.items():
        eas.sort(key=lambda r: r["docDate"])
        first_ea = eas[0]
        dates = sorted(d for d in confirmed.get(name_key(name), []) if d >= (date.fromisoformat(first_ea["docDate"][:10]) - timedelta(days=30)).isoformat())
        if not dates:
            continue
        conf = dates[0]
        parsed = [cache[e["_link"]] for e in eas if e["_link"] in cache]
        days = min([p["cert_days"] for p in parsed] or [90])
        divest_days = max([p["divest_days"] for p in parsed] or [90])
        divest = [d for p in parsed for d in p["divest"]]
        deadline = (date.fromisoformat(conf) + timedelta(days=days)).isoformat()
        divest_deadline = (date.fromisoformat(conf) + timedelta(days=divest_days)).isoformat()
        certs = sorted((r for r in by_name.get(name, []) if r["_base"].startswith("Certification of Ethics Agreement Compliance")
                        and r["docDate"] >= first_ea["docDate"]), key=lambda r: r["docDate"])
        info = {"name": name, "title": first_ea.get("title"), "agency": first_ea.get("agency"), "confirmed": conf,
                "days": days, "deadline": deadline, "divest_days": divest_days,
                "divest_deadline": divest_deadline, "divest": divest, "ea_link": eas[-1]["_link"], "certs": certs}
        tracked[name] = info
        grace_end = (date.fromisoformat(deadline) + timedelta(days=GRACE_DAYS)).isoformat()
        if not certs and today > grace_end:
            overdue.append(info)
        # Only the FIRST certification is the one the deadline governs; later
        # ones are supplemental.
        if certs and certs[0]["docDate"][:10] >= start and certs[0]["docDate"][:10] > grace_end:
            late_cert.append({**info, "cert": certs[0]})

    # ---- new 278-Ts and amendments in the window ----
    seen = SeenManifest(SEEN_PATH, keep_days=30)
    events = trades.load_events()
    post_deadline, timed, diffs, late_278t, waivers, agency_trades = [], [], [], [], [], []
    ledger = Ledger()
    jur = Jurisdiction()
    fresh = [r for r in rows if r.get("docDate", "")[:10] >= start and oge_index.is_pdf(r["_link"])
             and r["_link"] not in seen]
    for r in fresh:
        if r["_base"].startswith("278 Transaction") and not r["name"].startswith(("Trump, Donald", "Vance, J")):
            text = fetch_pdf_text(r["_link"], tmp)
            txs, _ = trades.parse_oge_278t(text)
            info = tracked.get(r["name"])
            # Trades in a sector the official's own agency oversees (the Army
            # under secretary buying SpaceX; the Federal Highway Administrator
            # buying a transportation company).
            asecs = jur.sectors_for_agency(r.get("agency"))
            for t in txs:
                hit = jur.trade_sector(t, asecs) if asecs else None
                if hit:
                    agency_trades.append({"r": r, "t": t, "sector": hit})
            # Late 278-T: filer's e-signature vs. transaction dates, 45-day outer
            # limit plus any extension OGE granted (stated on the form).
            sig = re.search(r"electronically signed on (\d{2}/\d{2}/\d{4}) by " + re.escape((r["name"] or "").split(",")[0]), text)
            ext = re.search(r"received an? (\d+) day filing extension", text)
            if sig:
                signed = trades.parse_date(sig.group(1)).isoformat()
                allowed = 45 + (int(ext.group(1)) if ext else 0)
                lates = [t for t in txs if t.get("tx_date") and (trades.days_between(t["tx_date"], signed) or 0) > allowed]
                if lates:
                    n, first = ledger.record(r["name"], r["_link"], signed, len(lates))
                    late_278t.append({"r": r, "signed": signed, "lates": lates, "allowed": allowed,
                                      "history": history_phrase(n, first)})
            for t in txs:
                for h in trades.event_hits(t, events):
                    timed.append({"r": r, "t": t, "hit": h})
                if info and info["divest"] and t["type"] in ("Sale", "Sale (partial)", "Exchange") \
                        and t.get("tx_date") and t["tx_date"] > info["divest_deadline"]:
                    item = matches_divest(t["asset"], info["divest"])
                    if item:
                        post_deadline.append({"r": r, "t": t, "info": info, "item": item})
        if "(Amended" in r["_label"]:
            prior = sorted((p for p in by_name.get(r["name"], []) if p["_base"] == r["_base"] and p["_link"] != r["_link"]
                            and oge_index.is_pdf(p["_link"]) and p["docDate"] < r["docDate"]),
                           key=lambda p: p["docDate"], reverse=True)
            if prior and r["_base"].startswith("Ethics Agreement"):
                # An amended agreement is a cover letter plus new terms; a raw
                # line diff is mostly letterhead noise. Compare what matters:
                # the divestiture lists and windows.
                old_p = parse_agreement(fetch_pdf_text(prior[0]["_link"], tmp))
                new_p = parse_agreement(fetch_pdf_text(r["_link"], tmp))
                added = [f"Divest: {d}" for d in new_p["divest"] if d not in old_p["divest"]]
                removed = [f"Divest: {d}" for d in old_p["divest"] if d not in new_p["divest"]]
                if new_p["divest_days"] != old_p["divest_days"]:
                    added.append(f"Divestiture window: {new_p['divest_days']} days (was {old_p['divest_days']})")
                diffs.append({"r": r, "old": prior[0]["_link"], "added": added, "removed": removed,
                              "n_add": len(added), "n_rem": len(removed)})
            elif prior:
                added, removed, n_add, n_rem = trades.text_diff(fetch_pdf_text(prior[0]["_link"], tmp), fetch_pdf_text(r["_link"], tmp))
                diffs.append({"r": r, "old": prior[0]["_link"], "added": added, "removed": removed, "n_add": n_add, "n_rem": n_rem})
        seen.add(r["_link"])
    for r in rows:
        if r["_base"].startswith("Conflict of Interest Waiver") and r.get("docDate", "")[:10] >= start:
            key = "waiver:" + r["name"] + r["docDate"]
            if key not in seen:
                waivers.append(r)
                seen.add(key)
    seen.save()
    ledger.save()

    write_extra("officials", "ethics_agreements",
                *build(overdue, late_cert, post_deadline, timed, diffs, errors, am, today, len(tracked), late_278t, waivers, agency_trades))


def who(info_or_row):
    title = info_or_row.get("title") or ""
    agency = info_or_row.get("agency") or ""
    last, _, first = (info_or_row.get("name") or "").partition(",")
    return f"{first.strip()} {last.strip()}".strip(), ", ".join(x for x in (title, agency) if x)


def build(overdue, late_cert, post_deadline, timed, diffs, errors, am, today, n_tracked, late_278t=(), waivers=(), agency_trades=()):
    blocks, paras = [], []
    if agency_trades:
        agency_trades = sorted(agency_trades, key=lambda x: -(x["t"].get("amount_floor") or 0))
        blocks.append({"type": "subhead", "text": "Trades in a sector their own agency oversees"})
        blocks.append({"type": "list", "items": [
            f"<strong>{esc(who(x['r'])[0])}</strong> ({esc(who(x['r'])[1])}): {esc(x['t']['type'].lower())} of "
            f"{esc(x['t']['asset'][:80])} ({esc(x['t']['amount'])}) on {mdY(x['t']['tx_date']) if x['t'].get('tx_date') else '?'} "
            f"({esc(x['sector'].replace('_', ' '))}). {link('278-T', x['r']['_link'])}"
            for x in agency_trades[:25]]})
        paras.append(f"{len(agency_trades)} executive-branch trade{'s' if len(agency_trades) != 1 else ''} in sectors the official's own agency oversees.")
    if late_278t:
        blocks.append({"type": "subhead", "text": "Late disclosures: executive-branch trades reported past the 45-day limit"})
        items = []
        for x in late_278t:
            name, role = who(x["r"])
            worst = max(x["lates"], key=lambda t: trades.days_between(t["tx_date"], x["signed"]) or 0)
            items.append(f"<strong>{esc(name)}</strong> ({esc(role)}): {len(x['lates'])} transaction(s) disclosed late in a "
                         f"{link('278-T', x['r']['_link'])} signed {mdY(x['signed'])}; the oldest, a {esc(worst['type'].lower())} of "
                         f"{esc(worst['asset'][:70])} ({esc(worst['amount'])}) on {mdY(worst['tx_date'])}, came "
                         f"{trades.days_between(worst['tx_date'], x['signed'])} days later (limit {x['allowed']}).{x['history']}")
        blocks.append({"type": "list", "items": items})
        paras.append(f"{len(late_278t)} executive-branch official{'s' if len(late_278t) != 1 else ''} filed trades past the STOCK Act's 45-day limit.")
    if waivers:
        blocks.append({"type": "subhead", "text": "New conflict-of-interest waivers"})
        blocks.append({"type": "list", "items": [
            f"<strong>{esc(who(r)[0])}</strong> ({esc(who(r)[1])}): waiver posted {mdY(r['docDate'])}. OGE doesn't post the text; request it from the agency."
            for r in waivers]})
        paras.append(f"{len(waivers)} new conflict-of-interest waiver{'s' if len(waivers) != 1 else ''} posted.")
    if post_deadline:
        blocks.append({"type": "subhead", "text": "Sold after the divestiture deadline"})
        blocks.append({"type": "list", "items": [
            f"<strong>{esc(who(x['info'])[0])}</strong> ({esc(who(x['info'])[1])}): {esc(x['t']['type'].lower())} of "
            f"{esc(x['t']['asset'][:90])} ({esc(x['t']['amount'])}) on {mdY(x['t']['tx_date'])}, "
            f"{trades.days_between(x['info']['deadline'], x['t']['tx_date'])} days after the {mdY(x['info']['deadline'])} deadline "
            f"to divest “{esc(x['item'])}” ({x['info']['days']} days after the {mdY(x['info']['confirmed'])} confirmation). "
            f"{link('278-T', x['r']['_link'])} · {link('ethics agreement', x['info']['ea_link'])}"
            for x in post_deadline[:25]]})
        if len(post_deadline) > 25:
            blocks.append({"type": "note", "text": f"{len(post_deadline) - 25} more post-deadline sales not shown."})
        paras.append(f"{len(post_deadline)} newly posted transaction{'s' if len(post_deadline) != 1 else ''} show an official selling an asset after the deadline their ethics agreement set for divesting it.")
    if late_cert:
        blocks.append({"type": "subhead", "text": "Compliance certifications posted late"})
        blocks.append({"type": "list", "items": [
            f"<strong>{esc(who(x)[0])}</strong> ({esc(who(x)[1])}): {link('certification', x['cert']['_link'])} posted "
            f"{mdY(x['cert']['docDate'])}, {trades.days_between(x['deadline'], x['cert']['docDate'])} days after the {mdY(x['deadline'])} deadline."
            for x in late_cert[:25]]})
        paras.append(f"{len(late_cert)} compliance certification{'s' if len(late_cert) != 1 else ''} posted well past deadline.")
    if timed:
        blocks.append({"type": "subhead", "text": "Executive-branch trades near market-moving events"})
        blocks.append({"type": "list", "items": [
            f"<strong>{esc(who(x['r'])[0])}</strong> ({esc(who(x['r'])[1])}): {esc(x['t']['type'].lower())} of "
            f"{esc(x['t']['asset'][:80])} ({esc(x['t']['amount'])}) on {mdY(x['t']['tx_date'])}, {esc(trades.timing_phrase(x['hit']))}. "
            f"{link('278-T', x['r']['_link'])}"
            for x in timed[:25]]})
        paras.append(f"{len(timed)} executive-branch trade{'s' if len(timed) != 1 else ''} landed near an event on the watch calendar.")
    for d in diffs:
        name, role = who(d["r"])
        blocks.append({"type": "subhead", "text": f"What changed: {name}, amended {d['r']['_base']}"})
        items = [f"<strong>Added:</strong> {esc(a)}" for a in d["added"]] + [f"<strong>Removed:</strong> {esc(x)}" for x in d["removed"]]
        if items:
            blocks.append({"type": "list", "items": items})
        blocks.append({"type": "note", "text": f"{esc(role)}. {d['n_add']} line(s) added, {d['n_rem']} removed. {link('Earlier version', d['old'])} · {link('Amended', d['r']['_link'])}."})
    if diffs:
        paras.append(f"{len(diffs)} amended OGE filing{'s' if len(diffs) != 1 else ''}, with the changes spelled out.")
    if overdue and am:
        overdue.sort(key=lambda x: x["deadline"])
        blocks.append({"type": "subhead", "text": "No compliance certification posted, past deadline"})
        blocks.append({"type": "list", "items": [
            f"<strong>{esc(who(x)[0])}</strong> ({esc(who(x)[1])}): confirmed {mdY(x['confirmed'])}, certification due "
            f"{mdY(x['deadline'])}, {trades.days_between(x['deadline'], today)} days ago. {link('Ethics agreement', x['ea_link'])}"
            for x in overdue[:30]]})
        if len(overdue) > 30:
            blocks.append({"type": "note", "text": f"{len(overdue) - 30} more overdue, not shown (oldest deadlines listed first)."})
        blocks.append({"type": "note", "text": f"Tracking {n_tracked} Senate-confirmed officials with ethics agreements since Jan. 20, 2025. Deadlines are counted from the confirmation date on congress.gov plus the agreement's stated window (usually 90 days) -- OGE can grant extensions that aren't public, so treat each flag as a lead to confirm, not a finding. OGE posts certifications with a lag, so nothing inside {GRACE_DAYS} days of a deadline is flagged. A certification filed but not yet posted by OGE will read as missing: confirm with the agency before publishing."})
        paras.append(f"{len(overdue)} confirmed official{'s have' if len(overdue) != 1 else ' has'} no posted certification of ethics-agreement compliance, more than {GRACE_DAYS} days past deadline.")
    if errors:
        blocks.append({"type": "note", "text": "Partial run: " + esc(" | ".join(errors))})
    if not paras:
        return [], "No ethics-agreement findings this window." + (" " + " ".join(errors) if errors else "")
    news = bool(post_deadline or late_cert or timed or diffs or late_278t or waivers or agency_trades)
    return ([section("Ethics Agreement Tracker",
                     "Late trades, broken divestiture promises and new waivers" if news else "Ethics agreements still awaiting compliance certification",
                     paras, blocks, no_news=not news)], " ".join(paras))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Industries to Watch -- lobbying ramp-ups in the industries in
data/lda_industries.json (prediction markets, crypto, AI, defense tech), the
Kalshi pattern ("Kalshi's Clout in Washington Grows"). Added 2026-10-03.

lda.gov's anonymous API allows ~15 requests/minute (429s after 14 in a
burst, tested) and caps page_size at 25, so a full sweep of a day's filings
isn't possible. Instead this runs the industry search terms against the
same YESTERDAY-onward window the Officials LDA task uses, confirms every hit
locally (lda.gov's search is loose token matching), then for confirmed hits:
- new registrations: is this the client's first-ever LDA filing?
- quarterly reports: did spending jump vs. the client's prior quarter?
Every call is throttled; total runtime is a few minutes at most.

Output: extras/officials/lda_trends.json
"""
import json
import re
import sys
import time
from urllib.parse import urlencode

from dc_common import days_ago_la, http_get
from dc_extras import esc, link, load_json, mdY, money, norm, section, write_extra

API = "https://lda.gov/api/v1/filings/"
THROTTLE = 4.3
PREV = {"first_quarter": ("fourth_quarter", -1), "second_quarter": ("first_quarter", 0),
        "third_quarter": ("second_quarter", 0), "fourth_quarter": ("third_quarter", 0)}
_last = [0.0]
MAX_FOLLOWUPS = 10


def lda(params):
    for attempt in range(3):
        wait = THROTTLE - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        status, body = http_get(f"{API}?{urlencode(params)}", headers={"User-Agent": "dc-record-bot"}, timeout=60)
        if status == 429:
            time.sleep(61)
            continue
        if status >= 400:
            raise RuntimeError(f"lda.gov HTTP {status}")
        return json.loads(body)
    raise RuntimeError("lda.gov rate limit")


def haystack(f):
    acts = " ".join((a.get("description") or "") + " " + (a.get("general_issue_code_display") or "")
                    for a in f.get("lobbying_activities") or [])
    c, r = f.get("client") or {}, f.get("registrant") or {}
    return " ".join([c.get("name") or "", c.get("general_description") or "", r.get("name") or "", acts])


def amount(f):
    v = f.get("income") or f.get("expenses")
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


CONTRIB_API = "https://lda.gov/api/v1/contributions/"
# Honorary expenses only count when tied to Trump-world causes ("Freedom 250
# Sponsors Failed to Disclose Donations"); every library/inaugural gift counts.
HONOREE_RE = re.compile(r"freedom ?250|america ?250|america's 250|semiquincentennial|trump|white house|ballroom|national garden|"
                        r"kennedy center|triumphal|arch|melania|be best", re.I)


def ld203(since):
    """LD-203 presidential-library (ple), inaugural (pic) and honorary (he)
    contributions posted in the window."""
    out, errors = [], []
    for ctype in ("ple", "pic", "he"):
        page = 1
        while page <= 8:
            try:
                wait = THROTTLE - (time.time() - _last[0])
                if wait > 0:
                    time.sleep(wait)
                _last[0] = time.time()
                status, body = http_get(f"{CONTRIB_API}?{urlencode({'filing_dt_posted_after': since, 'contribution_type': ctype, 'page_size': 25, 'page': page})}",
                                        headers={"User-Agent": "dc-record-bot"}, timeout=60)
                if status == 429:
                    time.sleep(61)
                    continue
                if status >= 400:
                    break
                d = json.loads(body)
            except Exception as e:  # noqa: BLE001
                errors.append(f"LD-203 {ctype}: {e}")
                break
            for f in d.get("results", []):
                filer = (f.get("registrant") or {}).get("name") or (f.get("lobbyist") or {}).get("last_name") or "?"
                for c in f.get("contribution_items") or []:
                    if c.get("contribution_type") != ctype:
                        continue
                    amt = float(c.get("amount") or 0)
                    blob = f"{c.get('payee_name') or ''} {c.get('honoree_name') or ''}"
                    if (ctype in ("ple", "pic") and amt >= 1000) or (ctype == "he" and HONOREE_RE.search(blob)):
                        out.append({"type": ctype, "filer": filer, "payee": c.get("payee_name"), "honoree": c.get("honoree_name"),
                                    "amount": amt, "date": c.get("date"), "url": f.get("filing_document_url")})
            if not d.get("next"):
                break
            page += 1
    return out, errors


def main():
    cfg = load_json("data/lda_industries.json", {}) or {}
    since = days_ago_la(1)
    jump = cfg.get("spend_jump", {"min_increase": 50000, "min_ratio": 1.5})
    hits, errors = {}, []
    for industry, spec in (cfg.get("industries") or {}).items():
        confirm = re.compile("|".join(spec.get("confirm", [])), re.I)
        for param, term in spec.get("search", []):
            try:
                d = lda({param: term, "filing_dt_posted_after": since, "page_size": 25})
            except Exception as e:  # noqa: BLE001
                errors.append(f"{industry} '{term}': {e}")
                continue
            for f in d.get("results", []):
                if confirm.search(haystack(f)):
                    hits.setdefault(f["filing_uuid"], {"f": f, "industries": set()})["industries"].add(industry)

    regs, jumps, others = [], [], []
    followups = 0
    for h in sorted(hits.values(), key=lambda h: -(amount(h["f"]) or 0)):
        f = h["f"]
        c = f.get("client") or {}
        row = {"f": f, "ind": sorted(h["industries"]), "client": c.get("name"), "registrant": (f.get("registrant") or {}).get("name"),
               "amount": amount(f), "url": f.get("filing_document_url")}
        if f.get("filing_type") == "RR":
            if followups < MAX_FOLLOWUPS:
                followups += 1
                try:
                    prior = lda({"client_name": c.get("name"), "page_size": 25})
                    earlier = [p for p in prior.get("results", []) if norm((p.get("client") or {}).get("name")) == norm(c.get("name"))
                               and (p.get("dt_posted") or "") < since]
                    row["first_ever"] = not earlier
                except Exception as e:  # noqa: BLE001
                    errors.append(f"history '{c.get('name')}': {e}")
            regs.append(row)
        elif f.get("filing_period") in PREV and row["amount"] is not None and followups < MAX_FOLLOWUPS:
            followups += 1
            prev_period, dy = PREV[f["filing_period"]]
            try:
                prev = lda({"client_id": c.get("id"), "filing_year": f["filing_year"] + dy, "filing_period": prev_period, "page_size": 25})
                prev_amt = max((amount(p) or 0) for p in prev.get("results", [])) if prev.get("results") else 0
            except Exception as e:  # noqa: BLE001
                errors.append(f"prior quarter '{c.get('name')}': {e}")
                prev_amt = None
            row["prev"] = prev_amt
            if prev_amt is not None and row["amount"] - prev_amt >= jump["min_increase"] and (prev_amt == 0 or row["amount"] / prev_amt >= jump["min_ratio"]):
                jumps.append(row)
            else:
                others.append(row)
        else:
            others.append(row)

    blocks, paras = [], []
    if regs:
        blocks.append({"type": "subhead", "text": "New registrations"})
        blocks.append({"type": "list", "items": [
            f"<strong>{esc(r['client'])}</strong> hired {esc(r['registrant'])} ({esc(', '.join(r['ind']))})"
            + (" — <strong>first-ever LDA registration for this client</strong>" if r.get("first_ever") else "")
            + f". {link('Filing', r['url'])}" for r in regs]})
        firsts = sum(1 for r in regs if r.get("first_ever"))
        paras.append(f"{len(regs)} new lobbying registration{'s' if len(regs) != 1 else ''} in watched industries" + (f", {firsts} from first-time clients" if firsts else "") + ".")
    if jumps:
        blocks.append({"type": "subhead", "text": "Spending jumps"})
        blocks.append({"type": "list", "items": [
            f"<strong>{esc(r['client'])}</strong> via {esc(r['registrant'])} ({esc(', '.join(r['ind']))}): {money(r['amount'])} this quarter, up from {money(r['prev'])}. {link('Filing', r['url'])}"
            for r in jumps]})
        paras.append(f"{len(jumps)} client{'s' if len(jumps) != 1 else ''} in watched industries sharply increased lobbying spending.")
    if others:
        blocks.append({"type": "subhead", "text": "Other new filings in watched industries"})
        blocks.append({"type": "table", "headers": ["Client", "Firm", "Industry", "Reported", "Filing"],
                       "rows": [[esc(r["client"]), esc(r["registrant"]), esc(", ".join(r["ind"])), money(r["amount"]) if r["amount"] is not None else "—",
                                 link(r["f"].get("filing_type_display") or r["f"].get("filing_type") or "filing", r["url"])] for r in others[:25]]})
    if errors:
        blocks.append({"type": "note", "text": "Partial run: " + esc(" | ".join(errors[:4]))})
    gifts, gerr = ld203(since)
    errors.extend(gerr)
    gift_section = []
    if gifts:
        names = {"ple": "presidential library", "pic": "inaugural committee", "he": "honorary expense"}
        gifts.sort(key=lambda g: -g["amount"])
        gift_section = [section("Library, Inaugural & Honorary Giving", "Lobbyists' gifts to presidential libraries, inaugurals and Trump-world causes",
                                [f"{len(gifts)} LD-203 line item{'s' if len(gifts) != 1 else ''}, {money(sum(g['amount'] for g in gifts))} in all."],
                                [{"type": "list", "items": [
                                    f"<strong>{esc(g['filer'])}</strong>: {money(g['amount'])} to {esc(g['payee'])} ({names[g['type']]}"
                                    + (f", honoring {esc(g['honoree'])}" if g.get("honoree") and g["honoree"] != g["payee"] else "")
                                    + f") on {mdY(g['date'] or '')}. {link('LD-203', g['url'])}" for g in gifts[:30]]}])]
    if not (regs or jumps or others):
        write_extra("officials", "lda_trends", gift_section, (f"{len(gifts)} LD-203 library/inaugural/honorary gifts. " if gifts else "")
                    + "No new filings in watched lobbying industries." + (" Errors: " + "; ".join(errors) if errors else ""))
        return
    if not paras:
        paras.append(f"{len(others)} new filing{'s' if len(others) != 1 else ''} in watched industries; no new clients or spending jumps.")
    blocks.append({"type": "note", "text": "Industries and keywords are set in data/lda_industries.json. Amounts are the income (firm) or expenses (in-house) line on the filing."})
    write_extra("officials", "lda_trends", [section("Industries to Watch", "Lobbying in prediction markets, crypto, AI and defense tech", paras, blocks,
                                                    no_news=not (regs or jumps))] + gift_section,
                " ".join(paras) + (f" {len(gifts)} LD-203 library/inaugural/honorary gifts." if gifts else ""))


if __name__ == "__main__":
    main()

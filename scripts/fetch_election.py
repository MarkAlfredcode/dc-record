#!/usr/bin/env python3
"""Election Watch -- temporary election-season section for the Campaigns
edition (added 2026-10-03). Active only inside data/deadlines.json's
election_mode window; outside it this writes an empty extra and exits.

Sources: every new 24-hour independent-expenditure report (F24), 48-hour
contribution notice (F6) and individual/org IE report (F5) filed in the
window. FEC's /efile/ schedule endpoints ignore their filters (confirmed
2026-10-03: file_number and min_date are both silently dropped), so each
filing's raw .fec file is read from docquery instead -- line-level, same
day, no processing lag.

Reports:
- where outside money is landing: new IE dollars by candidate/race,
  support vs. oppose, top spender;
- the biggest single new IEs;
- candidates putting their own money in (F6 lines from the candidate);
- outside groups filing their first-ever IE report.

Persisted dedupe: data/election-seen.json (file numbers).
Output: extras/campaigns/election.json
"""
import re
from collections import defaultdict

from dc_common import http_get, today_la
from dc_extras import (
    SeenManifest, esc, fec_get, fec_key_cycle, link, load_json, lookback, mdY, money, section, write_extra,
)

SEEN_PATH = "data/election-seen.json"
RAW = "https://docquery.fec.gov/dcdev/posted/{n}.fec"
FS = "\x1c"
MAX_FILINGS = 250


def fec_date(s):
    s = (s or "").strip()
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if re.fullmatch(r"\d{8}", s) else None


def amount(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def parse_raw(text):
    """Yields (kind, record) for SE (IE) and F65 (48-hour contribution) lines.
    Lines split on \\n only -- str.splitlines() also splits on \\x1c, the
    FEC field separator."""
    for line in text.split("\n"):
        f = [x.strip().strip('"').strip() for x in line.rstrip("\r").split(FS)]
        if not f or not f[0]:
            continue
        kind = f[0].upper()
        if kind.startswith("SE") and len(f) > 30:
            # v8.x SE: ... [19] dissemination date [20] amount [21] disbursement date [23] purpose
            # [26] support/oppose [27] cand id [28] last [29] first ... office/state/district near [33:36]
            tail = f[33:37]
            office = next((x for x in tail if x in ("H", "S", "P")), "")
            state = next((x for x in tail if re.fullmatch(r"[A-Z]{2}", x) and x not in ("H", "S", "P")), "")
            dist = next((x for x in tail if re.fullmatch(r"\d{1,2}", x)), "")
            yield "IE", {"payee": f[6] or " ".join(x for x in f[7:9] if x), "date": fec_date(f[19]) or fec_date(f[21]),
                         "amount": amount(f[20]), "purpose": f[23], "so": f[26], "cand_id": f[27],
                         "cand": " ".join(x for x in (f[29], f[28]) if x).title(), "office": office, "state": state, "district": dist}
        elif kind.startswith("F65") and len(f) > 17:
            yield "F6", {"entity": f[3], "name": f[4] or " ".join(x for x in (f[6], f[5]) if x),
                         "last": f[5], "date": fec_date(f[16]), "amount": amount(f[17])}


def race(r):
    if r["office"] == "H":
        return f"{r['state']}-{int(r['district']) if r['district'].isdigit() and int(r['district']) else 'AL'}"
    return f"{r['state']} Senate" if r["office"] == "S" else ("President" if r["office"] == "P" else r["state"] or "?")


def main():
    cfg = load_json("data/deadlines.json", {}) or {}
    em = cfg.get("election_mode") or {}
    today = today_la()
    if not (em.get("start", "9999") <= today <= em.get("end", "0000")):
        write_extra("campaigns", "election", [], "Election mode is off.")
        return
    keys, start = fec_key_cycle(), lookback()
    seen = SeenManifest(SEEN_PATH, keep_days=45)
    filings, errors = [], []
    for form in ("F24", "F5", "F6"):
        page = 1
        while True:
            try:
                d = fec_get("filings/", {"form_type": form, "min_receipt_date": start, "per_page": 100, "page": page, "sort": "-receipt_date"}, keys)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{form} list: {e}")
                break
            filings.extend({**f, "_form": form} for f in d.get("results", []) if str(f.get("file_number")) not in seen)
            if page >= ((d.get("pagination") or {}).get("pages") or 1):
                break
            page += 1
    filings = filings[:MAX_FILINGS]

    ies, f6, spenders, ie_keys = [], [], {}, set()
    for f in filings:
        n = f.get("file_number")
        status, body = http_get(RAW.format(n=n), headers={"User-Agent": "dc-record-bot"}, timeout=60)
        if status >= 400:
            errors.append(f"raw filing {n}: HTTP {status}")
            continue
        for kind, rec in parse_raw(body.decode("latin-1", errors="replace")):
            rec.update({"committee": f.get("committee_name") or f.get("committee_id"), "cid": f.get("committee_id"),
                        "pdf": f.get("pdf_url"), "form": f["_form"]})
            if kind == "IE" and rec["amount"] > 0:
                # Amended 24-hour reports repeat lines from the original.
                k = (rec["cid"], rec["cand_id"], rec["amount"], rec["date"], rec["payee"])
                if k in ie_keys:
                    continue
                ie_keys.add(k)
                ies.append(rec)
                spenders[f.get("committee_id")] = f.get("committee_name")
            elif kind == "F6" and rec["amount"] > 0:
                f6.append(rec)
        seen.add(str(n))

    # First-time outside spenders: committee registered (first filed) in the last 120 days.
    new_groups = []
    cids = [c for c in spenders if c]
    for i in range(0, len(cids), 10):
        try:
            d = fec_get("committees/", {"committee_id": cids[i:i + 10], "per_page": 20}, keys)
            for c in d.get("results", []):
                ffd = (c.get("first_file_date") or "")[:10]
                if ffd and ffd >= (lookback_days_ago(120)):
                    new_groups.append({"cid": c["committee_id"], "name": c.get("name"), "first": ffd,
                                       "total": sum(x["amount"] for x in ies if x["cid"] == c["committee_id"])})
        except Exception as e:  # noqa: BLE001
            errors.append(f"committee lookup: {e}")

    seen.save()
    sections, summary = build(ies, f6, new_groups, errors, em)
    write_extra("campaigns", "election", sections, summary)


def lookback_days_ago(n):
    from dc_common import days_ago_la
    return days_ago_la(n)


def top_spender(v):
    """Largest outside spender for a candidate, with which way it is spending:
    'DEFEND US PAC ($4,100,000 opposing)'."""
    name = max(v["spenders"].items(), key=lambda x: x[1])[0]
    sd = v["sides"][name]
    parts = [f"{money(sd[k])} {w}" for k, w in (("S", "supporting"), ("O", "opposing")) if sd[k]]
    return f"{esc(name)} ({' and '.join(parts)})"


def build(ies, f6, new_groups, errors, em):
    blocks, paras = [], []
    if ies:
        by = defaultdict(lambda: {"S": 0.0, "O": 0.0, "spenders": defaultdict(float), "sides": defaultdict(lambda: {"S": 0.0, "O": 0.0})})
        names = {}
        for r in ies:  # group by FEC candidate ID; filers spell names differently
            names.setdefault(r["cand_id"] or r["cand"], r["cand"] or r["cand_id"])
        for r in ies:
            k = (race(r), names[r["cand_id"] or r["cand"]])
            by[k]["S" if r["so"] == "S" else "O"] += r["amount"]
            by[k]["spenders"][r["committee"]] += r["amount"]
            by[k]["sides"][r["committee"]]["S" if r["so"] == "S" else "O"] += r["amount"]
        ranked = sorted(by.items(), key=lambda kv: -(kv[1]["S"] + kv[1]["O"]))[:15]
        total = sum(r["amount"] for r in ies)
        paras.append(f"{money(total)} in new independent expenditures reported across {len(by)} candidates, led by {esc(ranked[0][0][1])} ({esc(ranked[0][0][0])}).")
        blocks.append({"type": "subhead", "text": "Where new outside money is landing"})
        blocks.append({"type": "table", "headers": ["Race", "Candidate", "Supporting", "Opposing", "Top spender"],
                       "rows": [[esc(k[0]), f"<strong>{esc(k[1])}</strong>", money(v["S"]), money(v["O"]),
                                 top_spender(v)] for k, v in ranked]})
        big = sorted(ies, key=lambda r: -r["amount"])[:10]
        blocks.append({"type": "subhead", "text": "Largest new independent expenditures"})
        blocks.append({"type": "list", "items": [
            f"<strong>{esc(r['committee'])}</strong>: {money(r['amount'])} {'supporting' if r['so'] == 'S' else 'opposing'} "
            f"{esc(r['cand'])} ({esc(race(r))}), {esc(r['purpose'][:60])} via {esc(r['payee'][:40])}, disseminated {mdY(r['date']) if r['date'] else '?'} ({link(r['form'], r['pdf'])})"
            for r in big if r["amount"] >= 100000] or ["No single new IE of $100,000 or more."]})
    self_fund = [r for r in f6 if r["entity"] == "CAN"]
    if self_fund:
        blocks.append({"type": "subhead", "text": "Candidates funding themselves (48-hour notices)"})
        blocks.append({"type": "list", "items": [
            f"<strong>{esc(r['committee'])}</strong>: {money(r['amount'])} from the candidate, {esc(r['name'])}, on {mdY(r['date']) if r['date'] else '?'} ({link('notice', r['pdf'])})"
            for r in sorted(self_fund, key=lambda r: -r["amount"])[:15]]})
        paras.append(f"{len(self_fund)} candidate self-funding notice{'s' if len(self_fund) != 1 else ''}, {money(sum(r['amount'] for r in self_fund))} in all.")
    if new_groups:
        blocks.append({"type": "subhead", "text": "New outside groups spending"})
        blocks.append({"type": "list", "items": [
            f"<strong>{link(g['name'], 'https://www.fec.gov/data/committee/' + g['cid'] + '/')}</strong>: first filed {mdY(g['first'])}, {money(g['total'])} in new IEs"
            for g in sorted(new_groups, key=lambda g: -g["total"])]})
        paras.append(f"{len(new_groups)} outside group{'s' if len(new_groups) != 1 else ''} that registered in the last four months filed IE reports.")
    if errors:
        blocks.append({"type": "note", "text": "Partial run: " + esc(" | ".join(errors[:5]))})
    if not paras:
        return [], "No new IE or 48-hour filings this window."
    blocks.append({"type": "note", "text": f"Read line by line from the raw 24-hour/48-hour reports filed this window (election mode runs through {mdY(em.get('end', ''))}). Amended reports can double-count a line until FEC processes them."})
    return [section("Election Watch", "Outside money and self-funders in the final stretch", paras, blocks)], " ".join(paras)


if __name__ == "__main__":
    main()

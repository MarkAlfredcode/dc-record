#!/usr/bin/env python3
"""Megadonors -- every new $1M+ gift on the FEC's Schedule A (added 2026-10-04).
("Elon Musk Gives $5 Million to Back Former DOGE Partner Vivek Ramaswamy";
"Trump Says His Super PAC Has 'Close to' $1 Billion"; "Cryptocurrency
Industry Courts Democrats by Pouring Millions Into Elections".)

Pulls itemized receipts of $1,000,000+ dated within the lookback window
(by FEC LOAD date, 7 days, because super PACs file monthly/quarterly and a gift
shows up weeks after it was made -- a receipt-date window returned nothing; dedupe via data/megadonors-seen.json by sub_id),
drops FEC memo duplicates, groups gifts by donor + recipient, and reports the
biggest. Gifts from other committees/organizations are kept and labeled --
a $20M check from a 501(c)(4) to a super PAC is the dark-money-into-
disclosed-money story, not noise.

Output: extras/campaigns/megadonors.json
"""
import re
import sys
from collections import defaultdict

from dc_extras import SeenManifest, esc, fec_key_cycle, fec_paginate, link, lookback, mdY, money, section, write_extra

CYCLE = 2026
MIN_GIFT = 1_000_000
MAX_LISTED = 40
KIND = {"O": "super PAC", "U": "super PAC", "V": "hybrid PAC", "W": "hybrid PAC", "X": "party committee", "Y": "party committee",
        "Z": "party committee", "N": "PAC", "Q": "PAC", "H": "House campaign", "S": "Senate campaign", "P": "presidential campaign",
        "D": "delegate committee", "E": "electioneering committee", "I": "independent expenditor", "C": "communication cost committee"}


def pretty_person(name):
    if "," in name:
        last, _, first = name.partition(",")
        name = f"{first.strip()} {last.strip()}"
    name = re.sub(r"\b(?:MR|MRS|MS|DR)\.?(?=\s)", "", name, flags=re.I)
    name = re.sub(r"\s+", " ", name).strip()
    return name.title().replace(" Iii", " III").replace(" Ii", " II").replace(" Jr", " Jr.")


def pretty_org(name):
    n = re.sub(r"\s+", " ", name.strip())
    if len(n) <= 6:
        return n
    n = n.title()
    n = re.sub(r"\bPac\b", "PAC", n)
    n = re.sub(r"\bLlc\b", "LLC", n)
    n = re.sub(r"\bInc\b\.*", "Inc.", n)
    return re.sub(r"^Maga\b", "MAGA", n)


def main():
    keys = fec_key_cycle()
    since = lookback(7)  # by FEC load date; the seen-manifest dedupes the overlap
    seen = SeenManifest("data/megadonors-seen.json", keep_days=60)
    errors, rows = [], []
    try:
        for r in fec_paginate("schedules/schedule_a/", {
                "two_year_transaction_period": CYCLE, "min_amount": MIN_GIFT, "min_load_date": since, "per_page": 100,
                "sort": "-contribution_receipt_amount", "sort_hide_null": "true"}, keys, max_pages=4):
            if r.get("memo_code") == "X" or (r.get("contribution_receipt_amount") or 0) < MIN_GIFT:
                continue
            if r.get("line_number") in ("18", "18A", "18B", "18C"):  # contribution refunds etc. aren't gifts
                continue
            sid = r.get("sub_id") or f"{r['committee_id']}-{r['contributor_name']}-{r['contribution_receipt_date']}-{r['contribution_receipt_amount']}"
            if sid in seen:
                continue
            r["_sid"] = sid
            rows.append(r)
    except Exception as e:  # noqa: BLE001
        errors.append(str(e))
        print(f"schedule_a fetch failed: {e}", file=sys.stderr)

    groups = defaultdict(list)
    for r in rows:
        groups[(r["contributor_name"].upper(), r["committee_id"])].append(r)
    cards = []
    for (_, cid), rs in groups.items():
        rs.sort(key=lambda r: r["contribution_receipt_date"])
        r0 = rs[0]
        com = r0.get("committee") or {}
        total = sum(r["contribution_receipt_amount"] for r in rs)
        is_ind = r0.get("entity_type") == "IND"
        donor = pretty_person(r0["contributor_name"]) if is_ind else pretty_org(r0["contributor_name"])
        who = donor
        if is_ind and (r0.get("contributor_occupation") or r0.get("contributor_employer")):
            emp, occ = pretty_org(r0.get("contributor_employer") or ""), (r0.get("contributor_occupation") or "").lower()
            who += f" ({', '.join(x for x in (occ, 'of ' + emp if emp and emp.lower() not in ('self', 'self-employed', 'none', 'n/a', 'retired') else '') if x)})"
        elif not is_ind:
            who += {"PAC": " (a committee)", "COM": " (a committee)", "ORG": " (organization)"}.get(r0.get("entity_type"), "")
        dates = sorted({r["contribution_receipt_date"] for r in rs})
        when = mdY(dates[0]) if len(dates) == 1 else f"{mdY(dates[0])} to {mdY(dates[-1])}"
        kind = KIND.get(com.get("committee_type"), "committee")
        line = (f"<strong>{esc(who)}</strong> gave {money(total)} to {esc(pretty_org(com.get('name') or cid))} ({kind}) "
                f"on {when}")
        if len(rs) > 1:
            line += f" in {len(rs)} gifts"
        url = r0.get("pdf_url")
        if url:
            line += f" -- {link('FEC filing', url)}"
        cards.append((total, line, is_ind, com.get("committee_type"), com.get("name") or cid, donor))
    cards.sort(key=lambda c: -c[0])
    for r in rows:
        seen.add(r["_sid"])
    seen.save()

    if cards:
        by_rec = defaultdict(float)
        for total, _, _, _, rec, _ in cards:
            by_rec[rec] += total
        top_rec, top_amt = max(by_rec.items(), key=lambda kv: kv[1])
        blocks = []
        for label, pick in (("From individuals", lambda c: c[2]), ("From organizations and other committees", lambda c: not c[2])):
            part = [c for c in cards if pick(c)]
            if part:
                blocks.append({"type": "subhead", "text": label})
                blocks.append({"type": "list", "items": [c[1] for c in part[:MAX_LISTED]]})
                if len(part) > MAX_LISTED:
                    blocks.append({"type": "note", "text": f"{len(part) - MAX_LISTED} more $1M+ gifts in this group not shown."})
        blocks.append({"type": "subhead", "text": "Where the money went"})
        blocks.append({"type": "list", "items": [
            f"{esc(pretty_org(rec))}: {money(amt)}" for rec, amt in sorted(by_rec.items(), key=lambda kv: -kv[1])[:5]]})
        blocks.append({"type": "note", "text": f"Itemized receipts of $1,000,000 or more that FEC processed since {mdY(since)} and DC Record hasn't reported before, from FEC Schedule A "
                       "(2025-26 cycle). Super PACs file monthly or quarterly, so a gift can surface weeks after it was made. "
                       "Gifts from other committees and organizations are included and labeled."})
        n_ind = sum(1 for c in cards if c[2])
        paras = [f"{len(cards)} new gifts of $1 million or more totaling {money(sum(c[0] for c in cards))} ({n_ind} from individuals); "
                 f"{pretty_org(top_rec)} took in the most, {money(top_amt)}."]
        top = cards[0]
        headline = f"{top[5]} leads {len(cards)} new $1M+ gifts at {money(top[0])}"
        write_extra("campaigns", "megadonors", [section("Megadonors", headline, paras, blocks)],
                    f"{len(cards)} new $1M+ gifts; biggest {money(top[0])} from {top[5]}")
    else:
        write_extra("campaigns", "megadonors", [], "no new $1M+ gifts")
    if errors:
        print("ERRORS: " + "; ".join(errors), file=sys.stderr)


if __name__ == "__main__":
    main()

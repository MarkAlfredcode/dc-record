#!/usr/bin/env python3
"""Money Out -- a Schedule B (disbursements) sweep for the kinds of
spending behind several recent stories (added 2026-10-03):

- REFUNDS: committees refunding contributions in bulk (Talarico's $1M+ in
  excess-contribution refunds; Darline Graham returning megadonor money).
  Aggregated per committee over new rows; flagged above a threshold.
- FORFEITURES / DISGORGEMENT: money sent to the U.S. Treasury (excluding
  payroll taxes), "disgorgement" lines, and anything paid to or naming
  FTX / Sam Bankman-Fried's circle ("Political Committees Are Still
  Forfeiting Crypto Kingpins' Tainted Contributions").
- INVESTMENTS: committees buying stock, crypto or securities with campaign
  cash ("How Politicians Living and Dead Play the Stock Market With Campaign
  Cash").
- FORMER MEMBERS: any spending by the principal campaign committee of a
  member who left Congress since 2019 (the Devin Nunes war-chest pattern).

FEC's processed Schedule B can't be filtered or sorted by load date, so
each query looks back WINDOW_DAYS by disbursement date and a persisted
sub_id manifest (data/money-out-seen.json) keeps already-reported rows out.
Description/name searches on FEC's API are loose token matches, so every
hit is confirmed locally with a regex before it counts.

Output: extras/campaigns/money_out.json
"""
import json
import re
from collections import defaultdict

from dc_common import days_ago_la, http_get, today_la
from dc_extras import (
    SeenManifest, esc, fec_get, fec_key_cycle, fec_paginate, link, load_json, mdY, money,
    run_mode, section, write_extra,
)

SEEN_PATH = "data/money-out-seen.json"
FORMER_PATH = "data/former-members-committees.json"
WINDOW_DAYS = 60
FORMER_SINCE = "2001-01-03"
# Trump-owned venues ("Republican Political Spending at Trump Properties Tops $1 Million")
TRUMP_PROPS = re.compile(r"trump (?:national|international|hotel|tower|doral|golf|turnberry|org)|mar[- ]a[- ]lago|bedminster|\bdoral\b|trump corp", re.I)
TRUMP_SEARCHES = ["Trump National", "Mar-a-Lago", "Trump Hotel", "Trump International", "Trump Doral", "Trump Golf"]
# Theft/fraud losses ("NRCC Says an Unknown Actor Stole More Than $16,000", treasurer embezzlement)
LOSS = re.compile(r"\b(?:stolen|theft|embezzl\w*|fraudulent(?:ly)?|unauthori[sz]ed|scam\w*|misappropriat\w*|unknown (?:party|actor))\b", re.I)
LOSS_SEARCHES = ["stolen", "theft", "fraudulent", "unauthorized", "scam", "embezzlement"]
REFUND_LINES = {"F3-20A": 25000, "F3P-28A": 50000, "F3X-28A": 50000}  # line -> committee-total threshold
TAINTED = re.compile(r"\bFTX\b|bankman|\bsalame\b|nishad singh|alameda research", re.I)
TREASURY_RECIP = re.compile(r"treasury|bureau of (the )?fiscal service|u\.?s\.? marshals", re.I)
NOT_FORFEIT = re.compile(r"payroll|withholding|\btax|941|futa|fica|social security|medicare", re.I)
INVEST = re.compile(r"\b(?:stocks?|equit(?:y|ies)|securities|bitcoin|btc|crypto\w*|ethereum|brokerage|"
                    r"investment purchase|purchase of (?:shares|stock|securities)|mutual fund|etf)\b", re.I)
NOT_INVEST = re.compile(r"earmark|realized|unrealized|loss on|fee|commission|interest|dividend|refund|reimburs", re.I)
INVEST_SEARCHES = ["investment", "stock", "securities", "brokerage", "bitcoin", "crypto"]


def row_basics(r):
    c = r.get("committee") or {}
    return {
        "sub_id": str(r.get("sub_id")),
        "committee": c.get("name") or r.get("committee_id"),
        "committee_id": r.get("committee_id"),
        "recipient": r.get("recipient_name") or "",
        "desc": r.get("disbursement_description") or "",
        "amount": r.get("disbursement_amount") or 0,
        "date": (r.get("disbursement_date") or "")[:10],
        "pdf": r.get("pdf_url"),
    }


def fec_committee_url(cid):
    return f"https://www.fec.gov/data/committee/{cid}/"


def former_member_committees(keys, today):
    """{committee_id: {"name", "left"}} for principal committees of members
    who left Congress since FORMER_SINCE -- far enough back to catch
    long-dead members whose committees never closed (Frank Lautenberg,
    d. 2013). Cached in data/, rebuilt monthly."""
    cache = load_json(FORMER_PATH, {}) or {}
    if cache.get("built", "") >= days_ago_la(30) and cache.get("committees"):
        return cache["committees"]
    status, body = http_get("https://unitedstates.github.io/congress-legislators/legislators-historical.json", timeout=120)
    if status >= 400:
        return cache.get("committees", {})
    status2, body2 = http_get("https://unitedstates.github.io/congress-legislators/legislators-current.json", timeout=60)
    current = {l["id"]["bioguide"] for l in json.loads(body2)} if status2 < 400 else set()
    people = {}
    for l in json.loads(body):
        last = l["terms"][-1]
        if l["id"]["bioguide"] in current or last.get("end", "") < FORMER_SINCE:
            continue
        nm = l["name"].get("official_full") or f"{l['name']['first']} {l['name']['last']}"
        title = "Rep." if last["type"] == "rep" else "Sen."
        for f in l["id"].get("fec", []):
            people[f] = {"name": f"former {title} {nm}", "left": last.get("end", "")[:10]}
    committees = {}
    ids = sorted(people)
    for i in range(0, len(ids), 50):
        data = fec_get("candidates/search/", {"candidate_id": ids[i:i + 50], "per_page": 100}, keys)
        for c in data.get("results", []):
            for pc in c.get("principal_committees") or []:
                if pc.get("designation") == "P":
                    committees[pc["committee_id"]] = {**people[c["candidate_id"]], "committee": pc.get("name")}
    with open(FORMER_PATH, "w") as f:
        json.dump({"built": today, "committees": committees}, f, indent=1, sort_keys=True)
        f.write("\n")
    return committees


MEMBER_PATH = "data/member-committees.json"
SECURITY = re.compile(r"\bsecurity\b|bodyguard|protective|protection detail|armed guard", re.I)
NOT_SECURITY = re.compile(r"social security|security deposit|cyber|it security|data security|securities|homeland security", re.I)
DEBT_MIN = 50000


def member_committees(keys, today):
    """{committee_id: label} for every principal campaign committee of a
    sitting member (any cycle). Cached in data/, rebuilt monthly."""
    cache = load_json(MEMBER_PATH, {}) or {}
    if cache.get("built", "") >= days_ago_la(30) and cache.get("committees"):
        return cache["committees"]
    status, body = http_get("https://unitedstates.github.io/congress-legislators/legislators-current.json", timeout=60)
    if status >= 400:
        return cache.get("committees", {})
    people = {}
    for l in json.loads(body):
        t = l["terms"][-1]
        nm = l["name"].get("official_full") or f"{l['name']['first']} {l['name']['last']}"
        label = f"{'Rep.' if t['type'] == 'rep' else 'Sen.'} {nm} ({(t.get('party') or '?')[0]}-{t['state']})"
        for f in l["id"].get("fec", []):
            people[f] = label
    committees = {}
    ids = sorted(people)
    for i in range(0, len(ids), 50):
        data = fec_get("candidates/search/", {"candidate_id": ids[i:i + 50], "per_page": 100}, keys)
        for c in data.get("results", []):
            for pc in c.get("principal_committees") or []:
                if pc.get("designation") == "P":
                    committees[pc["committee_id"]] = people[c["candidate_id"]]
    with open(MEMBER_PATH, "w") as f:
        json.dump({"built": today, "committees": committees}, f, indent=1, sort_keys=True)
        f.write("\n")
    return committees


def debt_watch(keys, committees):
    """Latest Schedule D per committee; vendor debt only (loans from the
    candidate excluded). "Deadbeat Political Campaigns Still Owe Creditors
    Millions." Weekly (Monday AM) -- balances move once a quarter."""
    latest = {}
    cids = sorted(committees)
    for i in range(0, len(cids), 10):
        for raw in fec_paginate("schedules/schedule_d/", {"committee_id": cids[i:i + 10], "per_page": 100,
                                                          "sort": "-coverage_end_date"}, keys, max_pages=3):
            cid = raw.get("committee_id")
            end = (raw.get("coverage_end_date") or "")[:10]
            if cid not in latest or end > latest[cid]["end"]:
                latest[cid] = {"end": end, "rows": []}
            if end == latest[cid]["end"]:
                latest[cid]["rows"].append(raw)
    out = []
    for cid, v in latest.items():
        label = committees[cid]
        toks = [t for t in re.sub(r"\(.*?\)|[^a-z ()]", " ", (label or "").lower()).split() if t not in ("jr", "sr", "ii", "iii", "iv")]
        surname = toks[-1] if toks else "~"
        vendor = [r for r in v["rows"] if (r.get("outstanding_balance_close_of_period") or 0) > 0
                  and not re.search(r"candidate|loan", (r.get("nature_of_debt") or ""), re.I)
                  and surname not in (r.get("creditor_debtor_name") or "").lower()]
        total = sum(r["outstanding_balance_close_of_period"] for r in vendor)
        if total >= DEBT_MIN:
            top = sorted(vendor, key=lambda r: -r["outstanding_balance_close_of_period"])[:3]
            out.append({"cid": cid, "label": label, "end": v["end"], "total": total, "top": top,
                        "committee": (v["rows"][0].get("committee") or {}).get("name") or cid})
    return sorted(out, key=lambda x: -x["total"])


def main():
    keys = fec_key_cycle()
    today = today_la()
    since = days_ago_la(WINDOW_DAYS)
    cycle = int(today[:4]) + (int(today[:4]) % 2)
    seen = SeenManifest(SEEN_PATH, keep_days=WINDOW_DAYS + 30)
    base = {"two_year_transaction_period": cycle, "min_date": since, "per_page": 100}
    errors = []
    marked = []  # sub_ids to mark seen once the section is built

    # ---- refunds ----
    refunds = []
    for line, threshold in REFUND_LINES.items():
        try:
            rows = [row_basics(r) for r in fec_paginate("schedules/schedule_b/", {**base, "line_number": line, "sort": "-disbursement_date"}, keys, max_pages=40)]
        except Exception as e:  # noqa: BLE001
            errors.append(f"refunds {line}: {e}")
            continue
        new = [r for r in rows if r["sub_id"] not in seen]
        by = defaultdict(list)
        for r in new:
            by[r["committee_id"]].append(r)
        for cid, rs in by.items():
            total = sum(r["amount"] for r in rs)
            if total >= threshold:
                refunds.append({"cid": cid, "committee": rs[0]["committee"], "n": len(rs), "total": total,
                                "biggest": max(rs, key=lambda r: r["amount"]), "first": min(r["date"] for r in rs),
                                "last": max(r["date"] for r in rs)})
                marked.extend(r["sub_id"] for r in rs)
    refunds.sort(key=lambda x: -x["total"])

    # ---- forfeitures / disgorgement / tainted ----
    forfeits = {}
    for params, check in (
        ({"disbursement_description": "disgorge"}, lambda r: re.search(r"disgorg", r["desc"], re.I)),
        ({"recipient_name": "treasury"}, lambda r: TREASURY_RECIP.search(r["recipient"]) and not NOT_FORFEIT.search(r["desc"])),
        ({"recipient_name": "FTX"}, lambda r: TAINTED.search(r["recipient"] + " " + r["desc"])),
        ({"disbursement_description": "FTX"}, lambda r: TAINTED.search(r["recipient"] + " " + r["desc"])),
        ({"disbursement_description": "Bankman"}, lambda r: TAINTED.search(r["recipient"] + " " + r["desc"])),
    ):
        try:
            for raw in fec_paginate("schedules/schedule_b/", {**base, **params, "sort": "-disbursement_amount"}, keys, max_pages=5):
                r = row_basics(raw)
                if r["sub_id"] in seen or r["amount"] < 1000 or not check(r):
                    continue
                r["tainted"] = bool(TAINTED.search(r["recipient"] + " " + r["desc"]))
                forfeits[r["sub_id"]] = r
        except Exception as e:  # noqa: BLE001
            errors.append(f"forfeitures {params}: {e}")
    forfeits = sorted(forfeits.values(), key=lambda r: (not r["tainted"], -r["amount"]))
    marked.extend(r["sub_id"] for r in forfeits)

    # ---- investments ----
    invest = {}
    for term in INVEST_SEARCHES:
        try:
            for raw in fec_paginate("schedules/schedule_b/", {**base, "disbursement_description": term, "min_amount": 10000, "sort": "-disbursement_amount"}, keys, max_pages=3):
                r = row_basics(raw)
                if r["sub_id"] in seen or not INVEST.search(r["desc"] + " " + r["recipient"]) or NOT_INVEST.search(r["desc"]):
                    continue
                invest[r["sub_id"]] = r
        except Exception as e:  # noqa: BLE001
            errors.append(f"investments '{term}': {e}")
    invest = sorted(invest.values(), key=lambda r: -r["amount"])
    marked.extend(r["sub_id"] for r in invest)

    # ---- spending at Trump properties ----
    trump = defaultdict(list)
    for term in TRUMP_SEARCHES:
        try:
            for raw in fec_paginate("schedules/schedule_b/", {**base, "recipient_name": term, "sort": "-disbursement_amount"}, keys, max_pages=3):
                r = row_basics(raw)
                if r["sub_id"] not in seen and TRUMP_PROPS.search(r["recipient"]) and all(x["sub_id"] != r["sub_id"] for x in trump[r["committee_id"]]):
                    trump[r["committee_id"]].append(r)
        except Exception as e:  # noqa: BLE001
            errors.append(f"Trump properties '{term}': {e}")
    trump_rows = sorted(({"committee": rs[0]["committee"], "cid": cid, "total": sum(r["amount"] for r in rs), "rows": rs}
                         for cid, rs in trump.items()), key=lambda x: -x["total"])
    marked.extend(r["sub_id"] for x in trump_rows for r in x["rows"])

    # ---- theft / fraud losses ----
    losses = {}
    for term in LOSS_SEARCHES:
        try:
            for raw in fec_paginate("schedules/schedule_b/", {**base, "disbursement_description": term, "sort": "-disbursement_amount"}, keys, max_pages=2):
                r = row_basics(raw)
                if r["sub_id"] not in seen and r["amount"] >= 500 and LOSS.search(r["desc"]):
                    losses[r["sub_id"]] = r
        except Exception as e:  # noqa: BLE001
            errors.append(f"losses '{term}': {e}")
    losses = sorted(losses.values(), key=lambda r: -r["amount"])
    marked.extend(r["sub_id"] for r in losses)

    # ---- sitting members' campaigns paying for personal security ----
    security = []
    try:
        members = member_committees(keys, today)
        for raw in fec_paginate("schedules/schedule_b/", {**base, "disbursement_description": "security", "min_amount": 1000,
                                                          "sort": "-disbursement_amount"}, keys, max_pages=10):
            r = row_basics(raw)
            if r["committee_id"] in members and r["sub_id"] not in seen and SECURITY.search(r["desc"]) and not NOT_SECURITY.search(r["desc"]):
                security.append({**r, "member": members[r["committee_id"]]})
        marked.extend(r["sub_id"] for r in security)
    except Exception as e:  # noqa: BLE001
        members = {}
        errors.append(f"security spending: {e}")

    # ---- weekly: unpaid vendor debts (Monday AM only) ----
    debts = []
    from datetime import date as _date
    if run_mode() == "am" and _date.fromisoformat(today).weekday() == 0:
        try:
            former_all = load_json(FORMER_PATH, {}).get("committees", {})
            pool = {**{c: v for c, v in members.items()}, **{c: f["name"] for c, f in former_all.items()}}
            debts = debt_watch(keys, pool)
        except Exception as e:  # noqa: BLE001
            errors.append(f"debt watch: {e}")

    # ---- former members' committees still spending ----
    zombies = []
    try:
        former = former_member_committees(keys, today)
        cids = sorted(former)
        spend = defaultdict(list)
        for i in range(0, len(cids), 10):  # FEC caps committee_id at 10 values
            for raw in fec_paginate("schedules/schedule_b/", {**base, "committee_id": cids[i:i + 10], "sort": "-disbursement_amount"}, keys, max_pages=3):
                r = row_basics(raw)
                if r["sub_id"] not in seen:
                    spend[r["committee_id"]].append(r)
        for cid, rs in spend.items():
            total = sum(r["amount"] for r in rs)
            if total >= 2500:
                top = sorted(rs, key=lambda r: -r["amount"])[:3]
                zombies.append({"cid": cid, **former[cid], "total": total, "n": len(rs), "top": top})
                marked.extend(r["sub_id"] for r in rs)
        zombies.sort(key=lambda z: -z["total"])
    except Exception as e:  # noqa: BLE001
        errors.append(f"former members: {e}")

    for sid in marked:
        seen.add(sid)
    seen.save()
    sections, summary = build(refunds, forfeits, invest, zombies, trump_rows, losses, errors, security, debts)
    write_extra("campaigns", "money_out", sections, summary)


def build(refunds, forfeits, invest, zombies, trump_rows, losses, errors, security=(), debts=()):
    blocks, paras = [], []
    if security:
        by = defaultdict(list)
        for r in security:
            by[r["member"]].append(r)
        blocks.append({"type": "subhead", "text": "Members' campaigns paying for personal security"})
        blocks.append({"type": "list", "items": [
            f"<strong>{esc(m)}</strong>: {money(sum(r['amount'] for r in rs))} — "
            + "; ".join(f"{money(r['amount'])} to {esc(r['recipient'][:40])} ({esc(r['desc'][:40])}, {mdY(r['date'])}, {link('filing', r['pdf'])})" for r in rs[:3])
            for m, rs in sorted(by.items(), key=lambda kv: -sum(r['amount'] for r in kv[1]))[:20]]})
        paras.append(f"{len(by)} sitting member{'s' if len(by) != 1 else ''}' campaigns reported new security spending.")
    if debts:
        blocks.append({"type": "subhead", "text": "Weekly: campaigns sitting on unpaid vendor debts"})
        blocks.append({"type": "list", "items": [
            f"<strong>{esc(d['label'])}</strong> ({link(d['committee'], fec_committee_url(d['cid']))}): {money(d['total'])} owed to vendors as of {mdY(d['end'])}. Largest: "
            + "; ".join(f"{money(t['outstanding_balance_close_of_period'])} to {esc((t.get('creditor_debtor_name') or '?')[:40])}" for t in d["top"])
            for d in debts[:20]]})
        blocks.append({"type": "note", "text": f"Sitting and former members' principal campaign committees with at least {money(DEBT_MIN)} in debts on their latest Schedule D, excluding loans from the candidate. Runs Mondays."})
        paras.append(f"{len(debts)} current or former members' campaigns owe vendors {money(DEBT_MIN)}+.")
    if losses:
        blocks.append({"type": "subhead", "text": "Reported thefts, fraud and unauthorized payments"})
        blocks.append({"type": "list", "items": [
            f"<strong>{esc(r['committee'])}</strong>: {money(r['amount'])} to {esc(r['recipient'])} on {mdY(r['date'])}, "
            f"\u201c{esc(r['desc'])}\u201d ({link('filing', r['pdf'])})" for r in losses[:15]]})
        blocks.append({"type": "note", "text": "Matched on the committee's own description. Many are refunds of fraudulent contributions (stolen card numbers), not losses; the ones worth a call describe money taken from the committee."})
        paras.append(f"{len(losses)} disbursement{'s' if len(losses) != 1 else ''} describing theft, fraud or unauthorized payments.")
    if refunds:
        blocks.append({"type": "subhead", "text": "Bulk contribution refunds"})
        blocks.append({"type": "table", "headers": ["Committee", "Refunds", "Total", "Largest", "Dates"],
                       "rows": [[link(x["committee"], fec_committee_url(x["cid"])), str(x["n"]), money(x["total"]),
                                 f"{money(x['biggest']['amount'])} to {esc(x['biggest']['recipient'][:40])} ({link('filing', x['biggest']['pdf'])})",
                                 f"{mdY(x['first'])}–{mdY(x['last'])}"] for x in refunds[:20]]})
        paras.append(f"{len(refunds)} committee{'s' if len(refunds) != 1 else ''} reported large batches of contribution refunds, led by {esc(refunds[0]['committee'])} ({money(refunds[0]['total'])} across {refunds[0]['n']} refunds).")
    if forfeits:
        blocks.append({"type": "subhead", "text": "Forfeitures and disgorgements"})
        blocks.append({"type": "list", "items": [
            f"{'<strong>FTX/SBF-linked:</strong> ' if r['tainted'] else ''}<strong>{esc(r['committee'])}</strong> paid {money(r['amount'])} to "
            f"{esc(r['recipient'])} on {mdY(r['date'])}: “{esc(r['desc'])}” ({link('filing', r['pdf'])})"
            for r in forfeits[:20]]})
        paras.append(f"{len(forfeits)} payment{'s' if len(forfeits) != 1 else ''} to the Treasury or disgorgements{' (incl. FTX/SBF-linked)' if any(r['tainted'] for r in forfeits) else ''}.")
    if invest:
        blocks.append({"type": "subhead", "text": "Campaign cash into stocks, crypto and securities"})
        blocks.append({"type": "list", "items": [
            f"<strong>{esc(r['committee'])}</strong>: {money(r['amount'])} to {esc(r['recipient'])} on {mdY(r['date'])}, "
            f"“{esc(r['desc'])}” ({link('filing', r['pdf'])})" for r in invest[:20]]})
        paras.append(f"{len(invest)} disbursement{'s' if len(invest) != 1 else ''} describing investments of committee money.")
    if trump_rows:
        blocks.append({"type": "subhead", "text": "Spending at Trump properties"})
        blocks.append({"type": "list", "items": [
            f"<strong>{esc(x['committee'])}</strong>: {money(x['total'])} — "
            + "; ".join(f"{money(r['amount'])} to {esc(r['recipient'][:45])} on {mdY(r['date'])} ({esc(r['desc'][:40])}, {link('filing', r['pdf'])})" for r in x["rows"][:3])
            for x in trump_rows[:15]]})
        paras.append(f"{len(trump_rows)} committee{'s' if len(trump_rows) != 1 else ''} reported new spending at Trump-owned properties, {money(sum(x['total'] for x in trump_rows))} in all.")
    if zombies:
        blocks.append({"type": "subhead", "text": "Former members' campaigns, still spending"})
        blocks.append({"type": "list", "items": [
            f"<strong>{esc(z['name'])}</strong> (left office {mdY(z['left'])}): {link(z.get('committee') or z['cid'], fec_committee_url(z['cid']))} "
            f"spent {money(z['total'])} across {z['n']} new line items. Largest: "
            + "; ".join(f"{money(t['amount'])} to {esc(t['recipient'][:40])} ({esc(t['desc'][:50])})" for t in z["top"])
            for z in zombies[:15]]})
        paras.append(f"{len(zombies)} former member{"s'" if len(zombies) != 1 else "'s"} campaign committees reported new spending.")
    if errors:
        blocks.append({"type": "note", "text": "Partial run: " + esc(" | ".join(errors))})
    if not paras:
        return [], "No notable refunds, forfeitures, investments or former-member spending in new Schedule B data." + (" Errors: " + "; ".join(errors) if errors else "")
    blocks.append({"type": "note", "text": f"New Schedule B line items processed by FEC, disbursement dates in the last {WINDOW_DAYS} days. FEC processing trails e-filing, so these surface days to weeks after the money moved."})
    return [section("Money Out", "Refunds, forfeitures, investments, Trump properties and zombie campaigns", paras, blocks)], " ".join(paras)


if __name__ == "__main__":
    main()

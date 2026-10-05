#!/usr/bin/env python3
"""Outside Money -- independent expenditures (FEC Schedule E / 24- and 48-hour
reports) and the "redboxing" question (added 2026-10-04): when a super PAC
supports a candidate, is it paying the SAME vendor the candidate's own
campaign pays? ("Democrats Embrace 'Redboxing' Loophole in Hot Congressional
Races"; "AI Industry Super PAC Enters Midterms With a $70 Million War Chest".)

1. Pulls new independent expenditures of $50K+ in the lookback window
   (dedupe: data/outside-money-seen.json, keyed by sub_id).
2. Aggregates by target (candidate, support/oppose) and by spender; flags
   spenders whose committee registered within the last 180 days.
3. For candidates with $250K+ of new SUPPORT spending (capped), pulls the
   campaign's own largest Schedule B payees for the cycle and reports any
   payee that also appears on the super PAC's expenditures for that
   candidate. Shared vendors are a coordination-risk signal, not proof:
   FEC's firewall rules allow common vendors if staffed separately.

Output: extras/campaigns/outside_money.json
"""
import os
import re
from collections import defaultdict

from dc_common import days_ago_la
from dc_extras import (
    SeenManifest, esc, fec_get, fec_key_cycle, fec_paginate, lookback, mdY, money,
    norm, section, write_extra,
)

MIN_IE = 50_000
MIN_SUPPORT_CHECK = 250_000
MAX_CAND_CHECKS = 15
NEW_COMMITTEE_DAYS = 180
CYCLE = 2026
SUFFIX = {"inc", "llc", "llp", "lp", "ltd", "corp", "corporation", "company", "co", "the", "and", "of"}
# processors/platforms that serve everyone -- sharing them means nothing
_GENERIC_TERMS = ["actblue", "winred", "anedot", "google", "facebook", "meta platforms", "amazon", "paypal", "stripe",
                  "united states postal", "usps", "fedex", "ups", "verizon", "at&t", "gusto", "adp", "quickbooks", "intuit",
                  "microsoft", "apple", "uber", "lyft", "delta air", "american express", "visa", "mastercard", "bank",
                  "bank of america", "payroll", "irs", "treasury", "fec", "federal election", "salesforce", "zoom", "slack",
                  "dropbox", "mailchimp", "constant contact", "x corp", "twitter", "youtube", "hulu", "netflix", "tiktok",
                  "snap inc", "comcast", "charter", "spectrum", "nbc", "cbs", "fox news", "fox corp", "fox broadcasting",
                  "abc", "cnn", "iheart", "cumulus", "gannett", "cox media", "sinclair", "nexstar", "tegna", "hearst",
                  "gray tv", "scripps"]
# whole-word matches only: "Focus Groups", "Fairs & Events", "Advisa" and "Fox Strategic" are real campaign vendors
GENERIC = re.compile(r"\b(?:" + "|".join(re.escape(x) for x in _GENERIC_TERMS) + r")\b", re.I)


def vendor_key(name):
    toks = [t for t in norm(name).split() if t not in SUFFIX]
    return " ".join(toks)


def pretty(n):
    n = re.split(r"\s+d/?b/?a\s+", n or "", flags=re.I)[0].strip()
    return n if len(n) <= 6 else n.title().replace(" Pac", " PAC")


def race(r):
    st, off = r.get("candidate_office_state") or "", r.get("candidate_office") or ""
    dist = r.get("candidate_office_district") or ""
    if off == "S":
        return f"{st} Sen."
    if off == "H":
        return f"{st}-{dist}" if dist and dist != "00" else f"{st}-AL"
    return {"P": "President"}.get(off, "")


def main():
    keys = fec_key_cycle()
    since = lookback(3)
    seen = SeenManifest("data/outside-money-seen.json", keep_days=21)
    errors = []
    rows = []
    try:
        for r in fec_paginate("schedules/schedule_e/", {"min_date": since, "min_amount": MIN_IE, "per_page": 100,
                                                        "sort": "-expenditure_amount", "most_recent": "true"}, keys, max_pages=6):
            sid = r.get("sub_id") or f"{r.get('committee_id')}-{r.get('candidate_id')}-{r.get('payee_name')}-{r.get('expenditure_amount')}-{r.get('expenditure_date')}"
            if sid in seen or (r.get("expenditure_amount") or 0) < MIN_IE:
                continue
            if r.get("amendment_indicator") == "D":
                continue
            r["_sid"] = sid
            rows.append(r)
    except Exception as e:  # noqa: BLE001
        errors.append(f"Schedule E: {e}")
    if not rows:
        write_extra("campaigns", "outside_money", [], "No new $50K+ independent expenditures." + (" Errors: " + "; ".join(errors) if errors else ""))
        return

    # collapse duplicate F24 notice + later regular-report copies of the same buy
    dedup = {}
    for r in rows:
        k = (r["committee_id"], r.get("candidate_id") or r.get("candidate_name"), norm(r.get("payee_name")), round(r["expenditure_amount"]), r.get("expenditure_date"))
        dedup.setdefault(k, r)
    rows = list(dedup.values())

    targets = defaultdict(lambda: {"total": 0.0, "spenders": defaultdict(float), "r": None})
    spenders = defaultdict(lambda: {"total": 0.0, "name": "", "new": False, "first": ""})
    cutoff = days_ago_la(NEW_COMMITTEE_DAYS)
    for r in rows:
        cand = r.get("candidate_name") or "(no candidate: ballot measure/party/general)"
        t = targets[(r.get("candidate_id") or cand, r.get("support_oppose_indicator"))]
        t["total"] += r["expenditure_amount"]
        t["r"] = r
        com = r.get("committee") or {}
        t["spenders"][com.get("name") or r["committee_id"]] += r["expenditure_amount"]
        s = spenders[r["committee_id"]]
        s["total"] += r["expenditure_amount"]
        s["name"] = com.get("name") or r["committee_id"]
        s["first"] = com.get("first_file_date") or ""
        s["new"] = bool(s["first"]) and s["first"] >= cutoff

    def cname(n):
        if "," in (n or ""):
            last, first = n.split(",", 1)
            return f"{first.strip().title()} {last.strip().title()}"
        return (n or "").title()

    top_targets = sorted(targets.items(), key=lambda kv: -kv[1]["total"])[:12]
    blocks = [{"type": "subhead", "text": "Biggest new targets"},
              {"type": "table", "headers": ["Candidate", "Race", "Side", "New spending", "Top spender"],
               "rows": [[esc(cname(t["r"].get("candidate_name") or "—")), esc(race(t["r"])), "Support" if k[1] == "S" else "Oppose", money(t["total"]),
                         esc(pretty(max(t["spenders"].items(), key=lambda kv: kv[1])[0]))] for k, t in top_targets]}]
    top_sp = sorted(spenders.items(), key=lambda kv: -kv[1]["total"])[:8]
    blocks += [{"type": "subhead", "text": "Biggest new spenders"},
               {"type": "list", "items": [f"<strong>{esc(pretty(s['name']))}</strong>: {money(s['total'])}"
                                          + (f" — <strong>registered {mdY(s['first'])}</strong>, a new committee" if s["new"] else "")
                                          for _, s in top_sp]}]

    # redboxing check
    cand_support = defaultdict(lambda: {"total": 0.0, "payees": defaultdict(lambda: defaultdict(float)), "r": None})
    for r in rows:
        if r.get("support_oppose_indicator") == "S" and r.get("candidate_id") and r.get("payee_name"):
            c = cand_support[r["candidate_id"]]
            c["total"] += r["expenditure_amount"]
            c["r"] = r
            if not GENERIC.search(r["payee_name"]):
                c["payees"][vendor_key(r["payee_name"])][(r.get("committee") or {}).get("name") or r["committee_id"]] += r["expenditure_amount"]
    shared = []
    big = sorted((kv for kv in cand_support.items() if kv[1]["total"] >= MIN_SUPPORT_CHECK), key=lambda kv: -kv[1]["total"])[:MAX_CAND_CHECKS]
    for cid, c in big:
        try:
            comms = fec_get(f"candidate/{cid}/committees/", {"designation": "P", "cycle": CYCLE}, keys).get("results", [])
            if not comms:
                continue
            camp = comms[0]["committee_id"]
            pay = defaultdict(float)
            names = {}
            for p in fec_paginate("schedules/schedule_b/", {"committee_id": camp, "two_year_transaction_period": CYCLE, "per_page": 100,
                                                            "sort": "-disbursement_amount"}, keys, max_pages=2):
                nm = p.get("recipient_name") or ""
                if not nm or GENERIC.search(nm):
                    continue
                k = vendor_key(nm)
                pay[k] += p.get("disbursement_amount") or 0
                names.setdefault(k, nm)
            for k, sp in c["payees"].items():
                if k and k in pay and pay[k] > 0:
                    shared.append({"cand": cname(c["r"].get("candidate_name")), "race": race(c["r"]), "vendor": names[k], "camp_paid": pay[k],
                                   "pacs": dict(sp), "camp": camp})
        except Exception as e:  # noqa: BLE001
            errors.append(f"{cid}: {e}")
    shared.sort(key=lambda s: -sum(s["pacs"].values()))
    if shared:
        blocks.append({"type": "subhead", "text": "Same vendor, campaign and super PAC"})
        blocks.append({"type": "list", "items": [
            f"<strong>{esc(s['vendor'].title())}</strong> was paid {money(s['camp_paid'])} by {esc(s['cand'])}'s campaign ({esc(s['race'])}) this cycle and "
            + "; ".join(f"{money(v)} by {esc(pretty(n))}" for n, v in sorted(s["pacs"].items(), key=lambda kv: -kv[1])[:3])
            + " in new spending supporting the candidate." for s in shared[:10]]})
    blocks.append({"type": "note", "text": f"Independent expenditures of $50K+ reported since {mdY(since)} (24/48-hour notices plus regular reports), counting only items "
                                           "not shown in an earlier edition. A shared vendor is a coordination-risk flag, not proof: FEC rules allow common vendors behind a "
                                           "firewall. Generic platforms (ActBlue, Google, postal, banks, broadcasters) are excluded."
                                           + (" Partial run: " + esc(" | ".join(errors[:2])) if errors else "")})
    total = sum(t["total"] for t in targets.values())
    headline = f"{money(total)} in new independent expenditures"
    paras = [f"{len(rows)} new independent expenditures of $50K or more, {money(total)} in all. "
             f"Largest target: {cname(top_targets[0][1]['r'].get('candidate_name'))} ({'support' if top_targets[0][0][1] == 'S' else 'oppose'}, {money(top_targets[0][1]['total'])})."
             + (f" {len(shared)} shared-vendor overlap{'s' if len(shared) != 1 else ''} between a campaign and a supporting super PAC." if shared else "")
             + (f" {sum(1 for s in spenders.values() if s['new'])} spender(s) registered in the last {NEW_COMMITTEE_DAYS} days." if any(s["new"] for s in spenders.values()) else "")]
    write_extra("campaigns", "outside_money", [section("Outside Money", headline, paras, blocks)], paras[0])
    if not os.environ.get("DC_NO_SAVE"):
        for r in rows:
            seen.add(r["_sid"])
        seen.save()


if __name__ == "__main__":
    main()

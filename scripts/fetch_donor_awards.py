#!/usr/bin/env python3
"""Donor Awards watch (added 2026-10-04): federal contracts that go to a
political donor of the agency's own boss, especially in the days around that
boss's exit.

Prompted by the story "Kristi Noem's Agency Gave Her Donor a Lucrative
Contract Just Before She Left" (DHS signed a multi-million-dollar aircraft
purchase from a company owned by a contributor on her last day). That story
needs three facts joined: WHO is leaving (and when), WHICH awards their
agency signed in the run-up, and WHETHER the vendor's people gave money to
that official. This script does the join deterministically, at zero LLM cost.

Departures. congress.gov nominations read "<successor>, to be Secretary of
<agency>, vice <predecessor>." -- so a CONFIRMED nomination dates the
predecessor's exit (their last day is on or before the confirmation date;
an acting official may sit in between). Only top-of-agency titles count.

Two checks, both against the same donor lookup:
  A. EXIT WINDOW -- every award >= $1M (Mod 0) the departing official's agency
     signed in the 30 days up to the successor's confirmation. Pulled from
     USAspending directly, so it also works as a backtest on past exits.
  B. CURRENT WINDOW -- every new award >= $2M already in
     data/usaspending_candidates.json, checked against the donations of the
     awarding agency's current AND recently-departed head.

Donor lookup (FEC schedule_a, 2024 + 2026 cycles): contributions to (1) the
head's own committees (campaign, leadership PAC, joint fundraisers) and (2)
Trump-world committees (Trump's principal/leadership committees, MAGA Inc.,
Trump National Committee JFC, RNC), from either (a) people who list the
vendor as their EMPLOYER or (b) the vendor itself as the contributor. An
employer match proves a link between the company and the money, NOT that the
donor owns the company -- the output says so and never asserts ownership.

Output: extras/conflicts/donor_awards.json (moved from the Feds edition to the
web-only Conflicts edition 2026-10-04). Persists data/donor-awards-seen.json (award dedupe) and
data/donor-awards-cache.json (committee + donor lookups, 14-day TTL).
"""
import itertools
import json
import os
import re
import sys
import time
from datetime import date, timedelta

from dc_common import http_get, http_post_json, load_manifest, save_manifest, today_la
from conflict_signals import Signals
from dc_extras import (
    SeenManifest, esc, fec_get, fec_paginate, fec_key_cycle, load_json, mdY, money, norm, section, write_extra,
)

CACHE_PATH = "data/donor-awards-cache.json"
SEEN_PATH = "data/donor-awards-seen.json"
CANDIDATES_PATH = "data/usaspending_candidates.json"
CONGRESS = 119
EXIT_LOOKBACK_DAYS = int(os.environ.get("DC_DONOR_EXIT_DAYS") or 75)  # confirmations this recent are "live"
EXIT_WINDOW_DAYS = 30  # awards in the 30 days up to the successor's confirmation
EXIT_MIN_USD = 1_000_000
CURRENT_MIN_USD = 2_000_000
CYCLES = (2024, 2026)
MIN_EMPLOYER_USD = 2_000  # total from employer-matched people before it's worth printing
FEC_BUDGET_SEC = int(os.environ.get("DC_DONOR_BUDGET_SEC") or 420)
CACHE_TTL_DAYS = 14

# Trump-world committees (ids verified against FEC /committee/{id}/ 2026-10-04).
# Principal committees are resolved at runtime from candidate P80001571.
TRUMP_CANDIDATE = "P80001571"
TRUMP_FIXED = {
    "C00892471": "MAGA Inc.",
    "C00873893": "Trump National Committee JFC",
    "C00003418": "Republican National Committee",
}
TOP_TITLE = re.compile(
    r"to be (?:the )?(Secretary|Attorney General|Administrator|Director|Commissioner|Chairman|Chair|Postmaster General)\b", re.I)
SUB_TITLE = re.compile(r"\b(Assistant|Deputy|Under|Associate|Principal|Inspector|Judge|Ambassador|Member|Governor|"
                       r"Marshal|General Counsel|Solicitor|Chief Financial|Comptroller)\b", re.I)
SUFFIX = {"inc", "llc", "corp", "corporation", "company", "co", "ltd", "lp", "llp", "the", "and", "of", "us", "usa",
          "group", "holdings", "services", "service", "international", "enterprises", "solutions", "systems", "l", "p", "c"}
OWNER_OCC = re.compile(r"\b(owner|ceo|chief executive|founder|co-?founder|president|chairman|chair|principal|proprietor|managing (partner|director|member)|partner)\b", re.I)
OWNER_MIN_TRUMP = 25_000  # owner-titled donors to Trump-world committees below this are too common to mean anything
OWNER_MIN_HEAD = 1_000
# A recipient this generic would match every employer string that contains it.
MIN_CORE_CHARS = 6


def core(name):
    toks = [t for t in norm(name).split() if t not in SUFFIX]
    return " ".join(toks)


class Lookups:
    def __init__(self):
        self.keys = fec_key_cycle()
        self.cache = load_manifest(CACHE_PATH) or {}
        self.cache.setdefault("committees", {})
        self.cache.setdefault("donors", {})
        self.t0 = time.time()
        self.skipped = 0
        self.checked = 0
        self.today = today_la()

    def save(self):
        cutoff = (date.fromisoformat(self.today) - timedelta(days=CACHE_TTL_DAYS)).isoformat()
        self.cache["donors"] = {k: v for k, v in self.cache["donors"].items() if v.get("fetched", "") >= cutoff}
        save_manifest(CACHE_PATH, self.cache)

    def over_budget(self):
        return time.time() - self.t0 > FEC_BUDGET_SEC

    def fec(self, path, params):
        return fec_get(path, params, self.keys, timeout=25)

    # -- committees ---------------------------------------------------------
    def candidate_committees(self, candidate_id):
        """{committee_id: name} for every committee tied to a candidate
        (principal, joint fundraisers, leadership PAC), cached."""
        hit = self.cache["committees"].get(candidate_id)
        if hit and hit.get("fetched", "") >= (date.fromisoformat(self.today) - timedelta(days=60)).isoformat():
            return hit["committees"]
        out = {}
        try:
            for r in self.fec(f"candidate/{candidate_id}/committees/", {"per_page": 50}).get("results", []):
                out[r["committee_id"]] = r["name"]
        except Exception as e:  # noqa: BLE001
            print(f"committees for {candidate_id}: {e}", file=sys.stderr)
            return hit["committees"] if hit else {}
        self.cache["committees"][candidate_id] = {"fetched": self.today, "committees": out}
        return out

    def find_candidate(self, full_name):
        """FEC candidate ids for a person, matched on first+last name (not
        just surname): "Kristi Noem" -> H0SD00054. Empty for people who never
        filed with the FEC (most cabinet members)."""
        key = "name:" + norm(full_name)
        hit = self.cache["committees"].get(key)
        if hit and hit.get("fetched", "") >= (date.fromisoformat(self.today) - timedelta(days=60)).isoformat():
            return hit["ids"]
        words = norm(re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", "", full_name, flags=re.I)).split()
        if len(words) < 2:
            return []
        first, last = words[0], words[-1]
        ids = []
        try:
            for r in self.fec("candidates/search/", {"q": last, "per_page": 20}).get("results", []):
                n = norm(r.get("name", "")).split()  # "NOEM, KRISTI LYNN" -> noem kristi lynn
                if n and n[0] == last and first in n[1:3]:
                    ids.append(r["candidate_id"])
        except Exception as e:  # noqa: BLE001
            print(f"candidate search {full_name}: {e}", file=sys.stderr)
            return []
        self.cache["committees"][key] = {"fetched": self.today, "ids": ids}
        return ids

    def person_committees(self, full_name):
        out = {}
        for cid in self.find_candidate(full_name):
            out.update(self.candidate_committees(cid))
        return out

    def trump_committees(self):
        out = dict(TRUMP_FIXED)
        out.update(self.candidate_committees(TRUMP_CANDIDATE))
        return out

    # -- owner-titled donors --------------------------------------------------
    def owner_index(self, targets, min_amount):
        """{core(employer): [row]} for donors whose stated occupation says they
        run the company (owner/CEO/founder/president/...), giving >= min_amount
        to any committee in `targets`. This is the "company owned by a
        contributor" pattern: the DONOR is a person, the vendor is their
        employer string, and no name search on the vendor would find them."""
        ids = sorted(targets)
        ck = f"owners|{min_amount}|" + ",".join(ids)
        hit = self.cache["donors"].get(ck)
        if hit and hit.get("complete"):
            return hit["rows"]
        idx, complete = {}, True
        for cycle in CYCLES:
            for i in range(0, len(ids), 10):
                params = {"committee_id": ids[i:i + 10], "two_year_transaction_period": cycle,
                          "min_amount": min_amount, "per_page": 100, "sort": "-contribution_receipt_amount"}
                try:
                    for r in itertools.islice(fec_paginate("schedules/schedule_a/", params, self.keys), 1500):
                        occ, emp = (r.get("contributor_occupation") or ""), (r.get("contributor_employer") or "")
                        if r.get("entity_type") not in (None, "IND") or not OWNER_OCC.search(occ):
                            continue
                        c = core(emp)
                        if len(c.replace(" ", "")) < MIN_CORE_CHARS:
                            continue
                        idx.setdefault(c, []).append({
                            "mode": "owner", "donor": r.get("contributor_name"), "occupation": occ, "employer": emp,
                            "amount": r.get("contribution_receipt_amount"),
                            "date": (r.get("contribution_receipt_date") or "")[:10],
                            "committee_id": (r.get("committee") or {}).get("committee_id") or r.get("committee_id"),
                            "committee": (r.get("committee") or {}).get("name")})
                except Exception as e:  # noqa: BLE001
                    print(f"  owner index {cycle}: {str(e)[:90]}", file=sys.stderr)
                    complete = False
        self.cache["donors"][ck] = {"fetched": self.today, "complete": complete, "rows": idx}
        return idx

    # -- donors -------------------------------------------------------------
    def donors(self, vendor, targets, modes=("employer", "self")):
        """Contributions linked to `vendor` going to any committee in
        `targets` ({committee_id: label}). Returns (rows, complete)."""
        vcore = core(vendor)
        if len(vcore.replace(" ", "")) < MIN_CORE_CHARS:
            return [], True
        ids = sorted(targets)
        ck = vcore + "|" + ",".join(modes) + "|" + ",".join(ids)
        hit = self.cache["donors"].get(ck)
        if hit and hit.get("complete"):
            return hit["rows"], True
        rows, complete = [], True
        for mode, field in (("employer", "contributor_employer"), ("self", "contributor_name")):
            if mode not in modes:
                continue
            for cycle in CYCLES:
                for i in range(0, len(ids), 10):  # FEC caps committee_id at 10 values
                    if self.over_budget():
                        self.skipped += 1
                        return rows, False
                    params = {field: vendor_query(vendor), "committee_id": ids[i:i + 10],
                              "two_year_transaction_period": cycle, "min_amount": 500,
                              "per_page": 100, "sort": "-contribution_receipt_amount"}
                    try:
                        data = self.fec("schedules/schedule_a/", params)
                    except Exception as e:  # noqa: BLE001  (504 "Query timed out" on huge employers)
                        print(f"  donor lookup {vendor!r} {mode} {cycle}: {str(e)[:90]}", file=sys.stderr)
                        complete = False
                        continue
                    for r in data.get("results", []):
                        who = r.get("contributor_employer") if mode == "employer" else r.get("contributor_name")
                        if core(who or "") != vcore:  # exact normalized match, not "contains"
                            continue
                        rows.append({
                            "mode": mode,
                            "donor": r.get("contributor_name"),
                            "occupation": r.get("contributor_occupation"),
                            "employer": r.get("contributor_employer"),
                            "amount": r.get("contribution_receipt_amount"),
                            "date": (r.get("contribution_receipt_date") or "")[:10],
                            "committee_id": (r.get("committee") or {}).get("committee_id") or r.get("committee_id"),
                            "committee": (r.get("committee") or {}).get("name"),
                        })
        self.checked += 1
        self.cache["donors"][ck] = {"fetched": self.today, "complete": complete, "rows": rows}
        return rows, complete


def vendor_query(vendor):
    """FEC's employer/name filters are text searches; strip legal suffixes so
    "DAEDALUS AVIATION CORPORATION" also finds "Daedalus Aviation"."""
    return " ".join(t.upper() for t in re.sub(r"[^A-Za-z0-9 ]", " ", vendor).split()
                    if t.lower() not in {"inc", "llc", "corp", "corporation", "co", "ltd", "lp", "llp"})


# ---- departures ------------------------------------------------------------

def congress_get(path, params, keys):
    from urllib.parse import urlencode
    url = f"https://api.congress.gov/v3/{path}?{urlencode({**params, 'api_key': next(keys), 'format': 'json'})}"
    status, body = http_get(url, headers={"User-Agent": "Mozilla/5.0 (dc-record)"}, timeout=45)
    if status >= 400:
        raise RuntimeError(f"congress.gov {path} -> HTTP {status}")
    return json.loads(body)


def departures(keys, today):
    """Confirmed top-of-agency nominations in the last EXIT_LOOKBACK_DAYS.
    Returns [{agency, title, predecessor, successor, confirmed}]."""
    since = (date.fromisoformat(today) - timedelta(days=EXIT_LOOKBACK_DAYS + 30)).isoformat()
    out, offset = [], 0
    while True:
        try:
            d = congress_get(f"nomination/{CONGRESS}",
                             {"limit": 250, "offset": offset, "fromDateTime": since + "T00:00:00Z"}, keys)
        except Exception as e:  # noqa: BLE001
            print(f"nominations: {e}", file=sys.stderr)
            break
        batch = d.get("nominations", [])
        for n in batch:
            desc = n.get("description") or ""
            la = n.get("latestAction") or {}
            if "Confirmed" not in (la.get("text") or ""):
                continue
            m = re.match(r"(?P<succ>[^,]+),.*?\bto be (?P<role>.+?),\s*vice (?P<pred>.+)$", desc)
            if not m or not TOP_TITLE.search(desc) or SUB_TITLE.search(m.group("role")):
                continue
            conf = la.get("actionDate") or ""
            if conf < (date.fromisoformat(today) - timedelta(days=EXIT_LOOKBACK_DAYS)).isoformat():
                continue
            pred = re.split(r",|\bresigned|\bterm expired|\bdeceased|\bretired|\bwhose\b|\bto serve\b", m.group("pred"))[0]
            pred = re.sub(r"\s+", " ", pred).strip(" .")
            if len(pred.split()) < 2:
                continue
            out.append({"agency": n.get("organization") or "", "title": m.group("role").strip(),
                        "predecessor": pred, "successor": m.group("succ").strip(), "confirmed": conf,
                        "citation": n.get("citation"), "url": n.get("url")})
        if len(batch) < 250:
            break
        offset += 250
    return out




def usaspending_awards(agency, start, end, min_usd):
    awards, page = [], 1
    while page <= 6:
        payload = {
            "filters": {
                "award_type_codes": ["A", "B", "C", "D"],
                "agencies": [{"type": "awarding", "tier": "toptier", "name": agency}],
                "time_period": [{"start_date": start, "end_date": end}],
                "award_amounts": [{"lower_bound": min_usd}],
            },
            "fields": ["Award ID", "Recipient Name", "Transaction Amount", "Awarding Agency", "Awarding Sub Agency",
                       "Transaction Description", "Action Date", "Mod", "generated_internal_id"],
            "page": page, "limit": 100, "sort": "Transaction Amount", "order": "desc",
        }
        try:
            status, data = http_post_json("https://api.usaspending.gov/api/v2/search/spending_by_transaction/",
                                          payload, timeout=40)
        except Exception as e:  # noqa: BLE001
            print(f"usaspending {agency}: {e}", file=sys.stderr)
            break
        if status >= 400 or not isinstance(data, dict):
            print(f"usaspending {agency}: HTTP {status}", file=sys.stderr)
            break
        for r in data.get("results", []):
            if str(r.get("Mod") or "0") not in ("0", ""):
                continue
            gid = r.get("generated_internal_id") or r.get("Award ID")
            awards.append({
                "award_id": r.get("Award ID"), "recipient": r.get("Recipient Name") or "",
                "amount": r.get("Transaction Amount") or 0, "agency": r.get("Awarding Agency"),
                "sub_agency": r.get("Awarding Sub Agency"), "date": r.get("Action Date"),
                "description": (r.get("Transaction Description") or "")[:300],
                "url": f"https://www.usaspending.gov/award/{gid}" if gid else "",
            })
        if not (data.get("page_metadata") or {}).get("hasNext"):
            break
        page += 1
    return awards


# ---- main ------------------------------------------------------------------

def vendor_summary(rows, targets_label):
    """Aggregate donor rows -> {total, people, committees, first, last, entity}."""
    people, comms, total, dates, entity = {}, {}, 0.0, [], 0.0
    for r in rows:
        amt = float(r["amount"] or 0)
        total += amt
        if r["mode"] == "self":
            entity += amt
        else:
            people[r["donor"]] = people.get(r["donor"], 0) + amt
        lab = targets_label.get(r["committee_id"]) or r["committee"] or r["committee_id"]
        comms[lab] = comms.get(lab, 0) + amt
        if r["date"]:
            dates.append(r["date"])
    return {"total": total, "people": people, "committees": comms, "entity": entity,
            "first": min(dates) if dates else "", "last": max(dates) if dates else ""}


def describe_donations(s, official_labels):
    """One short evidence line. Never says 'owner'."""
    ppl = sorted(s["people"].items(), key=lambda kv: -kv[1])
    who = []
    if ppl:
        who.append(", ".join(f"{esc(n.title())} ({money(a)})" for n, a in ppl[:2]) + (f" +{len(ppl) - 2}" if len(ppl) > 2 else "")
                   + " list it as employer")
    if s["entity"]:
        who.append(f"the company gave {money(s['entity'])}")
    top = sorted(s["committees"].items(), key=lambda kv: -kv[1])[:2]
    return "; ".join(who) + " → " + ", ".join(f"{esc(c)} {money(a)}" for c, a in top) + "."


def pretty_desc(text):
    t = (text or "").strip().rstrip(".")[:240]
    return t[:1].upper() + t[1:].lower() if t.isupper() else t


def pretty(name):
    t = (name or "").title()
    return re.sub(r"\b(Llc|Llp|Lp|Usa|Us|Tsa|Cbp|Ice|Fbi|Fema|Dhs|Ibm|Ii|Iii|Iv)\b", lambda m: m.group(1).upper(), t)


def sub_agency_ok(role, award):
    """A sub-agency head's exit (e.g. FEMA Administrator, whose congress.gov
    organization is DHS) only covers that sub-agency's awards; a department
    head's exit covers the whole department."""
    m = re.search(r"\bof the (.+?)(?:,|$)", role)
    if not m:
        return True
    target = core(m.group(1))
    top = core(award.get("agency") or "")
    if not target or target == top or core("Department of " + m.group(1)) == top:
        return True
    words = target.split()[:2]
    return all(w in core(award.get("sub_agency") or "") for w in words)


def evaluate(lk, a, head_targets, trump, head_idx, trump_idx):
    """-> (tier, rows, labels) or None. tier 1 = owner-titled donor or any
    giving to the head's own committees; tier 2 = vendor-itself giving >= $10K
    to Trump-world; tier 0 = weak, only counted."""
    vcore = core(a["recipient"])
    if len(vcore.replace(" ", "")) < MIN_CORE_CHARS:
        return None
    rows = list(head_idx.get(vcore, [])) + list(trump_idx.get(vcore, []))
    head_rows = []
    if head_targets:
        head_rows, _ = lk.donors(a["recipient"], head_targets)
        rows += [r for r in head_rows if float(r["amount"] or 0) >= OWNER_MIN_HEAD]
    tier = 1 if rows else 0
    if not tier:
        trump_rows, _ = lk.donors(a["recipient"], trump, modes=("self",))
        st = vendor_summary(trump_rows, {})
        if st["entity"] >= 10_000:
            rows, tier = trump_rows, 2
        elif trump_rows:
            return 0, trump_rows
    return (tier, rows) if tier else None


def main():
    keys_pool = [k.strip() for k in os.environ.get("FEC_KEYS", "").split(",") if k.strip()]
    if not keys_pool:
        write_extra("conflicts", "donor_awards", [], "Donor awards: skipped, FEC_KEYS unset.")
        return
    cyc = itertools.cycle(keys_pool)
    today = today_la()
    lk = Lookups()
    seen = SeenManifest(SEEN_PATH, keep_days=45)

    trump = lk.trump_committees()
    trump_idx = lk.owner_index(trump, OWNER_MIN_TRUMP)
    deps = departures(cyc, today)
    only = (os.environ.get("DC_DONOR_ONLY") or "").lower()  # test hook: restrict to one predecessor's exit
    if only:
        deps = [d for d in deps if only in d["predecessor"].lower()]
    print(f"departures in window: {[(d['predecessor'], d['agency'], d['confirmed']) for d in deps]}", file=sys.stderr)

    findings, weak, checked = [], 0, 0
    pending = []  # (args for consider) -- gathered first so donor lookups can run in parallel
    agency_heads = {}
    done = set()

    def consider(kind, a, d, head_targets, head_idx, labels, days_before):
        nonlocal weak, checked
        if a["award_id"] in done:
            return
        done.add(a["award_id"])
        key = f"{kind}:{a['award_id']}"
        if key in seen or f"exit:{a['award_id']}" in seen or f"cur:{a['award_id']}" in seen:
            return
        checked += 1
        res = evaluate(lk, a, head_targets, trump, head_idx, trump_idx)
        if res is None:
            return
        tier, rows = res
        if tier == 0:
            weak += 1
            return
        s = vendor_summary(rows, labels)
        findings.append({"kind": kind, "award": a, "stats": s, "departure": d, "key": key, "tier": tier,
                         "days_before": days_before, "labels": labels,
                         "owner": any(r["mode"] == "owner" for r in rows),
                         "to_official": any(r["committee_id"] in head_targets for r in rows)})

    for d in deps:
        pred_committees = lk.person_committees(d["predecessor"])
        succ_committees = lk.person_committees(d["successor"])
        head_targets = {**pred_committees, **succ_committees}
        head_idx = lk.owner_index(head_targets, OWNER_MIN_HEAD) if head_targets else {}
        labels = {**trump, **{c: f"{d['predecessor']}: {n.title()}" for c, n in pred_committees.items()},
                  **{c: f"{d['successor']}: {n.title()}" for c, n in succ_committees.items()}}
        agency_heads.setdefault(d["agency"], []).append((d, head_targets, head_idx, labels))
        conf = date.fromisoformat(d["confirmed"])
        start = (conf - timedelta(days=EXIT_WINDOW_DAYS)).isoformat()
        for a in usaspending_awards(d["agency"], start, d["confirmed"], EXIT_MIN_USD):
            if not sub_agency_ok(d["title"], a):
                continue
            days_before = (conf - date.fromisoformat(a["date"])).days if a["date"] else None
            pending.append(("exit", a, d, head_targets, head_idx, labels, days_before))

    # CURRENT window: new awards already pulled by fetch_usaspending.py
    cand = load_json(CANDIDATES_PATH, {}) or {}
    for c in cand.get("new_awards", []):
        amt = float(c.get("transaction_amount") or 0)
        heads = agency_heads.get(c.get("awarding_agency") or "")
        if amt < CURRENT_MIN_USD or not heads:
            continue
        a = {"award_id": c.get("award_id"), "recipient": c.get("recipient") or "", "amount": amt,
             "agency": c.get("awarding_agency"), "sub_agency": c.get("awarding_sub_agency"),
             "date": c.get("action_date"), "description": c.get("raw_description") or "", "url": c.get("url")}
        match = next((h for h in heads if sub_agency_ok(h[0]["title"], a)), None)
        if match:
            d, ht, hi, lab = match
            pending.append(("current", a, d, ht, hi, lab, None))

    # Trump-world "vendor itself gave" lookups: one per unique vendor, 8 at a time (FEC allows 1,000/hr per key).
    import concurrent.futures as cf
    vendors = sorted({p[1]["recipient"] for p in pending if len(core(p[1]["recipient"]).replace(" ", "")) >= MIN_CORE_CHARS})
    with cf.ThreadPoolExecutor(max(1, min(8, len(keys_pool) // 3))) as ex:  # ~3 keys per worker keeps each under its hourly cap
        list(ex.map(lambda v: lk.donors(v, trump, modes=("self",)), vendors))
    for args in pending:
        consider(*args)

    lk.save()
    for f in findings:
        seen.add(f["key"])
    seen.save()

    note_bits = []
    if lk.skipped:
        note_bits.append(f"{lk.skipped} vendor lookup(s) were skipped after the FEC time budget ran out")
    if weak:
        note_bits.append(f"{weak} further award(s) went to vendors whose employees gave only small amounts to Trump-world "
                         f"committees — too common among big contractors to mean anything, so not listed")
    if not findings:
        write_extra("conflicts", "donor_awards", [],
                    f"Donor awards: nothing new. {checked} award(s) at {len(agency_heads)} agencies with a recent head change "
                    f"checked ({len(deps)} departure(s) tracked); no vendor tied to the head's own donors or to large Trump-world giving.")
        return

    findings.sort(key=lambda f: (f["tier"], not f["to_official"],
                                 f["days_before"] if f["days_before"] is not None else 999, -f["award"]["amount"]))
    blocks = []
    for f in findings[:12]:
        a, s, d = f["award"], f["stats"], f["departure"]
        dep = f"{d['predecessor']} → {d['successor']} ({mdY(d['confirmed'])})"
        timing = ""
        if f["days_before"] is not None:
            timing = (f" Signed {f['days_before']}d before the successor's confirmation."
                      if f["days_before"] else " Signed the day the successor was confirmed.")
        blocks.append({
            "type": "card",
            "title": f"<strong>{esc(pretty(a['recipient']))}</strong>: {money(a['amount'])} from {esc(a['sub_agency'] or a['agency'])}",
            "meta": [dep],
            "text": f"{esc(pretty_desc(a['description'])[:110])}.{timing} {describe_donations(s, f['labels'])}",
            "links": [{"text": "Award", "url": a["url"]}],
        })
    rest = findings[12:]
    if rest:
        blocks.append({"type": "list", "items": [
            f"<strong>{esc(pretty(f['award']['recipient']))}</strong> — {money(f['award']['amount'])}, "
            f"{esc(f['award']['sub_agency'] or f['award']['agency'])}, {money(f['stats']['total'])} FEC-linked "
            f"(<a href=\"{esc(f['award']['url'])}\">award</a>)." for f in rest[:40]]})
    blocks.append({"type": "note", "text": (
        f"Checked {checked} award(s): $1M+ in the 30 days before each of {len(deps)} agency-head change(s), plus new $2M+ awards at those agencies. "
        "Matches are FEC donors (2024/2026) to the head's own committees or Trump-world committees whose employer is the vendor, "
        "or the vendor itself; an employer match does not show ownership."
        + (f" {'; '.join(note_bits)}." if note_bits else ""))})
    top = findings[0]
    ta = top["award"]
    who_link = "the agency head" if top["to_official"] else "Trump-world committees"
    headline = (f"{pretty(ta['recipient'])} won {money(ta['amount'])} from {ta['sub_agency'] or ta['agency']}; donors tied to {who_link}"
                if top["kind"] == "exit" else
                f"New {money(ta['amount'])} award to {pretty(ta['recipient'])}; donors tied to {who_link}")
    sections = [section("Donor Awards", headline,
                        [f"{len(findings)} award(s) to donor-linked vendors around an agency-head change."], blocks)]
    signals = Signals()
    for f in findings:
        a, d = f["award"], f["departure"]
        signals.add(f"donor_award|{a['url']}", "donor_award", None, a["date"],
                    f"{money(a['amount'])} award from {a['sub_agency'] or a['agency']} to {pretty(a['recipient'])}; "
                    f"{money(f['stats']['total'])} in FEC-linked donations"
                    + (f"; agency head change {d['predecessor']} → {d['successor']}" if d else ""),
                    a["url"], company=a["recipient"])
    signals.save()
    summary = (f"Donor awards: {len(findings)} hit(s). Top: {ta['recipient']} {money(ta['amount'])} "
               f"({ta['sub_agency'] or ta['agency']}, {mdY(ta['date'] or '')}); "
               f"{sum(1 for f in findings if f['to_official'])} tied to the agency head's own committees, "
               f"{sum(1 for f in findings if f['owner'])} via an owner/executive donor. Top Story candidate.")
    write_extra("conflicts", "donor_awards", sections, summary)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""First-time federal vendor check for sole-source justifications (added
2026-10-10). For each new J&A's vendor, looks up its all-time federal
contract history in USAspending (keyless); none on record -> the edition
says "first federal contract on record".

Why: the FBI's sole-source J&A to the UFC for MMA training of special agents
claims experience "specific to the FBI", yet USAspending has no contract to
the UFC, Zuffa or TKO at all -- a first-time vendor whose justification leans
on earlier work for the agency is the pattern worth a reporter's time.

Lookup: POST /api/v2/search/spending_by_award/ (contracts, then IDVs, FY2008
on), recipient_search_text = the vendor's name (or its UEI when known). That
search is fuzzy ("TKO" matched "RETKO GROUP LLC"), so results count only when
the recipient's normalized name equals the vendor's, or starts with it for a
name of 2+ words ("lockheed martin" -> "lockheed martin corporation"). A
single-word name must match exactly ("UFC" is not "UFC GYM").

Cache: data/vendor-history-cache.json {key: {d, has_contracts, n, first,
examples}}, 30 days, so AM and PM don't repeat lookups. Fails open: an error
means "unknown", never a flag and never a crash.
"""
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from dc_common import http_post_json, load_manifest, save_manifest, today_la

CACHE_PATH = "data/vendor-history-cache.json"
CACHE_DAYS = 30
SEARCH = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
AWARD_URL = "https://www.usaspending.gov/award/{}"
GROUPS = (["A", "B", "C", "D"], ["IDV_A", "IDV_B", "IDV_B_A", "IDV_B_B", "IDV_B_C", "IDV_C", "IDV_D", "IDV_E"])
_SUFFIX = re.compile(r"\b(incorporated|inc|llc|l l c|corp|corporation|co|company|ltd|limited|lp|llp|plc|gmbh|the|dba|d b a)\b")
_SKIP = re.compile(r"^(redacted|null|none|unknown|n/?a|various|multiple|tbd)$", re.I)


def name_key(name):
    k = _SUFFIX.sub(" ", re.sub(r"[^a-z0-9& ]", " ", (name or "").lower()))
    return " ".join(k.split())


def search_name(vendor):
    """'Ultimate Fighting Championship (UFC)' -> 'Ultimate Fighting Championship'."""
    return re.sub(r"\s*\([^)]*\)", "", vendor or "").strip(" ,.;")


def matches(ours, theirs):
    if not ours or not theirs:
        return False
    if ours == theirs:
        return True
    return len(ours.split()) >= 2 and (theirs.startswith(ours + " ")
                                       or (ours.startswith(theirs + " ") and len(theirs.split()) >= 2))


def lookup(vendor, uei=None):
    """{has_contracts, n, first, examples} or raises."""
    text = uei or search_name(vendor)
    ours = name_key(text)
    found = []
    for codes in GROUPS:
        payload = {"filters": {"recipient_search_text": [text], "award_type_codes": codes,
                               "time_period": [{"start_date": "2007-10-01", "end_date": today_la()}]},
                   "fields": ["Award ID", "Recipient Name", "Award Amount", "Awarding Agency", "Start Date",
                              "generated_internal_id"],
                   "limit": 50, "sort": "Award Amount", "order": "desc"}
        status, data = http_post_json(SEARCH, payload, timeout=40)
        if status != 200:
            raise RuntimeError(f"USAspending HTTP {status}")
        for r in data.get("results") or []:
            if uei or matches(ours, name_key(r.get("Recipient Name"))):
                found.append(r)
        if found:
            break
    dates = sorted(r.get("Start Date") for r in found if r.get("Start Date"))
    return {"has_contracts": bool(found), "n": len(found), "first": dates[0] if dates else None,
            "examples": [{"recipient": r.get("Recipient Name"), "agency": r.get("Awarding Agency"),
                          "amount": r.get("Award Amount"), "start": r.get("Start Date"),
                          "url": AWARD_URL.format(r["generated_internal_id"]) if r.get("generated_internal_id") else None}
                         for r in found[:3]]}


def check(vendors, today=None, workers=4):
    """vendors: [(vendor name, uei or None)]. Returns {vendor name: result or
    None (unknown)}; updates the cache."""
    today = today or today_la()
    cutoff = (date.fromisoformat(today) - timedelta(days=CACHE_DAYS)).isoformat()
    cache = {k: v for k, v in load_manifest(CACHE_PATH).items() if v.get("d", "") >= cutoff}
    todo, out = {}, {}
    for v, uei in vendors:
        if not v or _SKIP.match(v.strip()) or not name_key(search_name(v)):
            continue
        key = f"uei:{uei}" if uei else name_key(search_name(v))
        if key in cache:
            out[v] = cache[key]
        else:
            todo[key] = (v, uei)

    def one(item):
        key, (v, uei) = item
        try:
            return key, v, lookup(v, uei)
        except Exception:  # noqa: BLE001 -- fail open
            return key, v, None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for key, v, res in ex.map(one, todo.items()):
            out[v] = res
            if res is not None:
                cache[key] = {**res, "d": today}
    save_manifest(CACHE_PATH, cache)
    return out


if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(check([(a, None) for a in sys.argv[1:]]), indent=2))

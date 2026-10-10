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
on), recipient_search_text = the vendor's UEI when known, else its name with
suffixes and punctuation stripped (the search returns NOTHING for "Action
Target, Inc."), then a brand alias, then its first 2 words, then a
distinctive first word. That search is fuzzy ("TKO" matched "RETKO GROUP
LLC"), so a result counts only when the recipient's normalized name starts
with the search words (whole words), or for one word of 4+ characters, has it
as its first word ("dell" -> "dell federal systems", not "caddell"; "ufc"
stays exact, so never "ufc gym"). Multi-vendor strings are split on ; / and.
First version (953ea70) wrongly called 35 of 105 real vendors first-time,
CACI, Dell and SpaceX among them.

Cache: data/vendor-history-cache.json {key: {d, has_contracts, n, first,
examples}}, 30 days, so AM and PM don't repeat lookups. Fails open: an error
means "unknown", never a flag and never a crash.
"""
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from dc_common import http_post_json, load_manifest, save_manifest, today_la

CACHE_PATH = "data/vendor-history-cache.json"
# extract_ja vendor_kind values for which triage never asserts "first".
ORDINARY_KINDS = ("government contractor", "commercial OEM/supplier")
CACHE_DAYS = 30
SEARCH = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
AWARD_URL = "https://www.usaspending.gov/award/{}"
GROUPS = (["A", "B", "C", "D"], ["IDV_A", "IDV_B", "IDV_B_A", "IDV_B_B", "IDV_B_C", "IDV_C", "IDV_D", "IDV_E"])
_SUFFIX = re.compile(r"\b(incorporated|inc|llc|l l c|corp|corporation|co|company|ltd|limited|lp|llp|plc|gmbh|the|dba|d b a)\b")
_SKIP = re.compile(r"^(redacted|null|none|unknown|n/?a|various|multiple|tbd)$", re.I)


def search_text(name):
    """What to send USAspending: suffixes and punctuation stripped, words as
    written (its search finds nothing for "bae system land and armament")."""
    # USAspending files "M & M WELDING": spaced ampersands, or the search finds nothing.
    k = _SUFFIX.sub(" ", re.sub(r"[^a-z0-9& ]", " ", (name or "").lower().replace("&", " & ")))
    return " ".join(re.sub(r"\b([a-z]) (\d)\b", r"\1\2", k).split())  # "L-3" -> "l3"


def name_key(name):
    """For comparing names: search_text with singular/plural alike
    ("Communication Systems" vs "COMMUNICATIONS")."""
    return " ".join(w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w
                    for w in search_text(name).split())


def search_name(vendor):
    """'Ultimate Fighting Championship (UFC)' -> 'Ultimate Fighting Championship'."""
    return re.sub(r"\s*\([^)]*\)", "", vendor or "").strip(" ,.;")


# Brand names that differ from the legal name USAspending files under
# (keyed by search_text).
ALIASES = {"spacex": "space exploration technologies", "aws": "amazon web services", "ibm": "international business machines",
           "ge": "general electric", "ge aerospace": "general electric", "hp": "hewlett packard", "hpe": "hewlett packard enterprise",
           "l3": "l3 technologies", "l3harris": "l3harris technologies", "jhu apl": "johns hopkins university applied physics",
           "iom": "international organization for migration", "thales dis usa": "gemalto", "gdit": "general dynamics information technology"}
# Too common to stand alone as a one-word retry ("American ..." would match anything).
_GENERIC = set("american national global united international general federal advanced applied integrated technical "
               "technology technologies systems services solutions group north south east west first new pacific "
               "atlantic premier strategic professional mission defense security consulting management enterprise "
               "enterprises associates partners industries holdings medical health science scientific company "
               "corporation inc llc co ltd usa us america north american worldwide".split())
_GENERIC |= {name_key(w) for w in _GENERIC}
_SPLIT = re.compile(r"\s*(?:;|,|/|\+)\s*")
_AND = re.compile(r"\s+(?:and|&)\s+", re.I)


def parts(vendor):
    """'RIX, RTX, Woodward FST, Inc' -> ['RIX', 'RTX', 'Woodward FST'];
    [] for 'Multiple vendors (...)'. "and" is split later, only if the whole
    name finds nothing (it's part of real names)."""
    base = search_name(vendor)
    if re.match(r"(multiple|various|several)\b", base, re.I):
        return []
    return [p for p in _SPLIT.split(base) if name_key(p) and not _SKIP.match(p.strip())]


def matches(ours, theirs):
    """Recipient name `theirs` is the vendor `ours` (both name_key'd)."""
    if not ours or not theirs:
        return False
    if ours == theirs:
        return True
    ow, tw = ours.split(), theirs.split()
    if len(ow) >= 2:
        return tw[:len(ow)] == ow or (len(tw) >= 2 and ow[:len(tw)] == tw)
    # One word: whole first word, 4+ characters ("caci" -> "caci inc federal",
    # "dell" -> "dell federal systems", but not "caddell"; "ufc" stays exact,
    # so it never matches "ufc gym").
    return len(ours) >= 4 and tw[0] == ours


def _search(text, codes):
    payload = {"filters": {"recipient_search_text": [text], "award_type_codes": codes,
                           "time_period": [{"start_date": "2007-10-01", "end_date": today_la()}]},
               "fields": ["Award ID", "Recipient Name", "Award Amount", "Awarding Agency", "Start Date",
                          "generated_internal_id"],
               "limit": 50, "sort": "Award Amount", "order": "desc"}
    err = None
    # USAspending drops connections under bursts ("Remote end closed
    # connection" after ~70 lookups in a row on 2026-10-10): back off.
    for wait in (0, 10, 30):
        time.sleep(wait)
        try:
            status, data = http_post_json(SEARCH, payload, timeout=40)
        except Exception as e:  # noqa: BLE001
            err = e
            continue
        if status == 200:
            return data.get("results") or []
        err = RuntimeError(f"USAspending HTTP {status}")
        if status not in (429, 500, 502, 503, 504):
            break
    raise err


def _attempts(name):
    """Search texts, most specific first: the suffix-free name (USAspending's
    search returns nothing for "Action Target, Inc." but 100 awards for
    "Action Target"), a known alias, then a shorter prefix -- but only when
    the words dropped are generic ("Dedrone Defense" -> "dedrone"). Never
    shorten past a distinctive word: "Ultimate Fighting Championship" ->
    "ultimate" matched ULTIMATE CONCRETE LLC."""
    key = search_text(name)
    # Hyphens kept as written: "Mid-America Aerotech" finds MID-AMERICA
    # AEROTECH, LLC; "mid america aerotech" finds nothing.
    hyphen = " ".join(_SUFFIX.sub(" ", re.sub(r"[^a-z0-9&\- ]", " ", name.lower().replace("&", " & "))).split())
    out = [key, hyphen if "-" in hyphen else None, ALIASES.get(key)]
    w = key.split()
    for n in (2, 1):
        if len(w) > n and all(x in _GENERIC for x in w[n:]) and (n == 2 or (len(w[0]) >= 4 and w[0] not in _GENERIC)):
            out.append(" ".join(w[:n]))
    seen, uniq = set(), []
    for t in out:
        if t and t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def lookup_one(name, uei=None):
    """{has_contracts, raw_hits, matched: [result rows]} for one vendor."""
    raw, found = 0, []
    for text in ([uei] if uei else []) + _attempts(name):
        for codes in GROUPS:
            rows = _search(text, codes)
            raw += len(rows)
            found = [r for r in rows if text == uei or matches(name_key(text), name_key(r.get("Recipient Name")))]
            if found:
                return {"has_contracts": True, "raw_hits": raw, "matched": found}
    return {"has_contracts": False, "raw_hits": raw, "matched": []}


def lookup(vendor, uei=None):
    """{has_contracts, raw_hits, first, examples} or raises. A multi-vendor
    string has history if ANY of its vendors does; 'Multiple vendors' or an
    empty name -> None (unknown)."""
    ps = parts(vendor)
    if not ps:
        return None
    results = [lookup_one(p, uei if len(ps) == 1 else None) for p in ps]
    found = [r for res in results for r in res["matched"]]
    if not found:  # "Eaton Corp and Sikorsky"
        # Skip an all-generic piece: "Lockheed Martin Rotary and Mission
        # Systems" must not search "Mission Systems" on its own.
        more = [x for p in ps if _AND.search(p) for x in _AND.split(p)
                if name_key(x) and not all(w in _GENERIC for w in name_key(x).split())]
        results += [lookup_one(x) for x in more]
        found = [r for res in results for r in res["matched"]]
    dates = sorted(r.get("Start Date") for r in found if r.get("Start Date"))
    return {"has_contracts": bool(found), "raw_hits": sum(res["raw_hits"] for res in results),
            "first": dates[0] if dates else None,
            "examples": [{"recipient": r.get("Recipient Name"), "agency": r.get("Awarding Agency"),
                          "amount": r.get("Award Amount"), "start": r.get("Start Date"),
                          "url": AWARD_URL.format(r["generated_internal_id"]) if r.get("generated_internal_id") else None}
                         for r in found[:3]]}


def check(vendors, today=None, workers=2):
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

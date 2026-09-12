#!/usr/bin/env python3
"""Task 6 (Federal Register) fetch. Single API call, no real pagination
concern at this volume/window. PRORULE significance is a genuine editorial
judgment call (title-based) -- stays with Claude; this script just removes
the fetch chore and hands over every PRORULE so nothing is silently dropped
before Claude gets to judge it.

Output: data/fedregister_candidates.json
"""
import sys
import urllib.parse

from dc_common import check_reachable, http_get_json, yesterday_la, write_candidates

OUT_PATH = "data/fedregister_candidates.json"
BASE = "https://www.federalregister.gov/api/v1/documents.json"


def fetch(window_start, types):
    params = [
        ("conditions[publication_date][gte]", window_start),
        ("per_page", "100"),
        ("order", "newest"),
    ]
    for t in types:
        params.append(("conditions[type][]", t))
    url = f"{BASE}?{urllib.parse.urlencode(params)}"
    return http_get_json(url, timeout=30)


def slim(doc):
    return {
        "type": doc.get("type"),
        "title": doc.get("title"),
        "agency": ", ".join(a.get("name", "") for a in doc.get("agencies", []) or []),
        "abstract": doc.get("abstract"),
        "html_url": doc.get("html_url"),
        "publication_date": doc.get("publication_date"),
    }


def main():
    window_start = yesterday_la()
    reachable = check_reachable("www.federalregister.gov")
    out = {"window_start": window_start, "reachable": reachable, "presdocu_and_rule": [], "prorule": [], "errors": [], "counts": {}}
    if not reachable:
        out["errors"].append("www.federalregister.gov unreachable")
        write_candidates(OUT_PATH, out)
        return

    try:
        data = fetch(window_start, ["PRESDOCU", "RULE"])
        out["presdocu_and_rule"] = [slim(d) for d in data.get("results", [])]
        out["counts"]["presdocu_and_rule_total"] = data.get("count", len(out["presdocu_and_rule"]))
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"PRESDOCU/RULE fetch failed: {e}")

    try:
        data = fetch(window_start, ["PRORULE"])
        out["prorule"] = [slim(d) for d in data.get("results", [])]
        out["counts"]["prorule_total"] = data.get("count", len(out["prorule"]))
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"PRORULE fetch failed: {e}")

    print(
        f"Federal Register: {len(out['presdocu_and_rule'])} PRESDOCU/RULE, "
        f"{len(out['prorule'])} PRORULE candidates",
        file=sys.stderr,
    )
    write_candidates(OUT_PATH, out)


if __name__ == "__main__":
    main()

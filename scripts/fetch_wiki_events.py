#!/usr/bin/env python3
"""Every-10-days events-calendar refresh, step 1 of 3 (added 2026-10-03).

Pulls the current year's Wikipedia pages (the four quarterly "Timeline of the
second Trump presidency" pages, "YYYY", "YYYY in the United States" -- see
wiki_events.year_pages), keeps only items not seen on a previous run
(data/wiki-events-seen.json), and writes them, each with a short stable id,
to data/wiki_candidates.json for the Claude curation pass
(prompt-events.txt). Step 3 is scripts/apply_event_additions.py, which takes
dates and sources from THIS file, never from the model.

In the first two weeks of January it also re-reads the previous year, so
late-December edits aren't missed.

  python3 scripts/fetch_wiki_events.py           # normal run
  python3 scripts/fetch_wiki_events.py --seed    # mark everything current as seen, write no candidates
"""
import argparse
import hashlib
import json
import sys

import wiki_events
from dc_common import days_ago_la, today_la
from dc_extras import load_json

SEEN_PATH = "data/wiki-events-seen.json"
OUT_PATH = "data/wiki_candidates.json"


def item_id(it):
    return hashlib.sha1((it["date"] + "|" + it["text"][:160]).encode()).hexdigest()[:10]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true")
    a = ap.parse_args()
    today = today_la()
    years = [int(today[:4])]
    if today[5:10] <= "01-14":
        years.insert(0, years[0] - 1)
    seen = load_json(SEEN_PATH, {}) or {}
    items, missing = [], []
    for y in years:
        got, miss = wiki_events.collect(y)
        items.extend(got)
        missing.extend(miss)
    fresh, ids = [], set()
    for it in items:
        if it["date"] > today:
            continue  # "See also" rows etc. parse as future dates
        iid = item_id(it)
        if iid in ids:
            continue
        ids.add(iid)
        if iid not in seen:
            fresh.append({"id": iid, **it})
            seen[iid] = today
    # bounded: ids older than ~14 months can't reappear in the pages we read
    cutoff = days_ago_la(430)
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    with open(SEEN_PATH, "w") as f:
        json.dump(seen, f, indent=0, sort_keys=True)
        f.write("\n")
    if a.seed:
        print(f"seeded {len(ids)} items as seen; pages missing: {missing}", file=sys.stderr)
        return
    with open(OUT_PATH, "w") as f:
        json.dump({"generated": today, "pages_missing": missing, "candidates": fresh}, f, indent=1)
        f.write("\n")
    print(f"{len(fresh)} new item(s) of {len(ids)}; pages missing: {missing}", file=sys.stderr)


if __name__ == "__main__":
    main()

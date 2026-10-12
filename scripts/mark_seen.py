#!/usr/bin/env python3
"""Records which filings an edition actually reported in its dedupe
manifest (added 2026-10-12; the main passes used to rewrite these
manifests themselves and chose their own keys). A filing counts as reported
when its link appears anywhere in the assembled edition; one the model left
out stays unseen and comes back next run. Entries older than 14 days are
dropped.

  mark_seen.py content-TODAY.json data/campaigns_candidates.json data/fec-filings-seen.json
  mark_seen.py content-TODAY.json data/officials_input.json data/pfd-ptr-seen.json

The candidates file lists filings under "filings" (key + pdf_url) or
"items" (key + url).
"""
import json
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import surfaced  # noqa: E402
from dc_common import load_manifest, save_manifest, today_la  # noqa: E402

KEEP_DAYS = 14


def main():
    content_path, cand_path, seen_path = sys.argv[1:4]
    today = today_la()
    with open(content_path) as f:
        content = json.load(f)
    with open(cand_path) as f:
        cand = json.load(f)
    rows = cand.get("filings") or cand.get("items") or []
    shown = surfaced.links_in(content)
    seen = load_manifest(seen_path)
    cutoff = (date.fromisoformat(today) - timedelta(days=KEEP_DAYS)).isoformat()
    seen = {k: v for k, v in seen.items() if (v or {}).get("reported", today) >= cutoff}
    added = missing = 0
    for r in rows:
        if surfaced.norm(r.get("pdf_url") or r.get("url")) in shown:
            seen.setdefault(r["key"], {"reported": today})
            added += 1
        else:
            missing += 1
    save_manifest(seen_path, seen)
    print(f"{seen_path}: {added} reported filing(s) recorded; {missing} given to the model but not in the "
          f"edition (left unseen, will reappear)", file=sys.stderr)


if __name__ == "__main__":
    main()

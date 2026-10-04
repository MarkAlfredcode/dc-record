#!/usr/bin/env python3
"""Search every archived DC Record edition (archive/<edition>/<date>-<am|pm>.json).

  python3 scripts/search_archive.py "Justice"                 # any mention, newest first
  python3 scripts/search_archive.py "Greenbrier|Justice" --edition officials --since 2026-09-01
  python3 scripts/search_archive.py "late" --section "Disclosure Red Flags"

Pattern is a case-insensitive regex. Prints date, edition, section and the
matching text, one hit per line.
"""
import argparse
import glob
import html
import json
import os
import re

TAG = re.compile(r"<[^>]+>")


def texts(c):
    ts = c.get("top_story") or {}
    for p in [ts.get("headline", "")] + ts.get("paragraphs", []):
        yield "Top Story", p
    for l in c.get("leads") or []:
        yield "Leads", f"{l.get('headline', '')} — {l.get('angle', '')}"
    for s in c.get("sections", []):
        e = s.get("eyebrow", "")
        for p in [s.get("headline", "")] + s.get("paragraphs", []):
            yield e, p
        for b in s.get("blocks", []):
            for it in b.get("items", []):
                yield e, it
            for row in b.get("rows", []):
                yield e, " · ".join(row)
            if b.get("text"):
                yield e, b["text"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pattern")
    ap.add_argument("--edition")
    ap.add_argument("--since")
    ap.add_argument("--section")
    a = ap.parse_args()
    rx = re.compile(a.pattern, re.I)
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "archive")
    files = sorted(glob.glob(os.path.join(root, a.edition or "*", "*.json")), reverse=True)
    n = 0
    for fp in files:
        stamp = os.path.basename(fp)[:-5]
        if a.since and stamp[:10] < a.since:
            continue
        with open(fp) as f:
            c = json.load(f)
        for sec, h in texts(c):
            if a.section and a.section.lower() not in sec.lower():
                continue
            t = html.unescape(TAG.sub("", h))
            if rx.search(t):
                print(f"{stamp}  {os.path.basename(os.path.dirname(fp)):<10} {sec[:28]:<28} {t[:220]}")
                n += 1
    print(f"-- {n} hit(s) in {len(files)} archived edition(s)")


if __name__ == "__main__":
    main()

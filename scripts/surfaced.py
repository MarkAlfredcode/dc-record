#!/usr/bin/env python3
"""What an edition already surfaced, per AM/PM mode, so an item shown in one
edition is gone from the next edition of the same mode (added 2026-10-10 for
Campaigns and The Feds).

Measured before this: Campaigns repeated 57 of 165 links AM 10/9 -> AM 10/10
and 86-114 of ~175 PM to PM; The Feds repeated 25-41 SAM.gov links PM to PM.
Two causes. (1) Campaigns' Task 1 dedupe let the model choose the manifest
key, and FEC RFAI letters (form FRQ) have no file_number, so one run keyed
them "img:<image>" and the next "sub:<sub_id>" -- 141 filings matched nothing.
(2) PM editions have no email subscribers, so the workflows treated them as
site refreshes and reverted every dedupe manifest -- each PM deduped against
nothing.

The record is the edition's own source links (an FEC PDF or SAM.gov notice
URL is a stable identity, unlike a model-chosen key):
  data/surfaced-links.json  {edition: {mode: {url: first date surfaced}}}
committed by every real run, AM or PM.

  python3 scripts/surfaced.py prior <edition>
      writes data/surfaced-prior-<edition>.json: links already surfaced in
      this MODE before today, for the model to skip while writing.
assemble.py then calls drop_repeats() as a safety net -- removing any list
item, card or table row whose links were ALL surfaced earlier in this mode --
and record().
"""
import json
import os
import re
import sys
from datetime import date, timedelta

from dc_common import load_manifest, save_manifest, today_la
from dc_extras import run_mode

PATH = "data/surfaced-links.json"
KEEP_DAYS = 10
EDITIONS = ("campaigns", "feds")
_HREF = re.compile(r"""href=["']([^"']+)["']""")


_SAM_FILE = re.compile(r"sam\.gov/api/prod/opps/v3/opportunities/resources/files/")
_SAM_OLD = re.compile(r"https://sam\.gov/workspace/contract/opp/([^/]+)/view")


def norm(url):
    url = (url or "").split("#")[0].rstrip("/").strip()
    # SAM.gov notice links changed form on 2026-10-10 (keyless fetch).
    return _SAM_OLD.sub(r"https://sam.gov/opp/\1/view", url)


def links_in(obj):
    """Every link URL in a block (hrefs in its HTML strings, plus `url`
    fields such as card links)."""
    out = set()
    if isinstance(obj, str):
        out.update(norm(u) for u in _HREF.findall(obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k == "url" and isinstance(v, str):
                out.add(norm(v))
            else:
                out |= links_in(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= links_in(v)
    out.discard("")
    return out


def prior(edition, mode=None, today=None):
    mode, today = mode or run_mode(), today or today_la()
    seen = load_manifest(PATH).get(edition, {}).get(mode, {})
    return {u for u, d in seen.items() if d < today}


def drop_repeats(content, already):
    """Removes list items, cards and table rows whose links are all in
    `already`; drops a subhead left with nothing under it. Returns the
    number of items removed."""
    if not already:
        return 0
    dropped = 0

    def repeat(x):
        ls = links_in(x)
        return bool(ls) and ls <= already

    def card_repeat(b):
        """A card is its notice (the first link): its vendor-history or
        notice links can be new without the item being. But a SAM.gov
        attachment nobody has shown yet means a document read today -- the
        late-finding cards the prompt asks for -- so the card is new even if
        its notice ran before. (2026-10-12: NASA's CLDC card, built on Q&A
        files read that morning, was dropped because the CLDC solicitation
        had run on 10/10, leaving the Solicitations headline about nothing.)"""
        if not b.get("links"):
            return repeat(b)
        if norm(b["links"][0].get("url")) not in already:
            return False
        return not any(_SAM_FILE.search(u) and u not in already for u in links_in(b))

    for s in content.get("sections", []):
        blocks = []
        lost_top_card = False
        for b in s.get("blocks", []):
            t = b.get("type")
            if t == "list":
                keep = [i for i in b.get("items", []) if not repeat(i)]
                dropped += len(b.get("items", [])) - len(keep)
                if not keep:
                    continue
                b["items"] = keep
            elif t == "card" and card_repeat(b):
                dropped += 1
                if not any(x.get("type") == "card" for x in blocks):
                    lost_top_card = True
                continue
            elif t == "table":
                rows = b.get("rows", [])
                keep = [r for r in rows if not repeat(r)]
                dropped += len(rows) - len(keep)
                if rows and not keep:
                    continue
                b["rows"] = keep
            blocks.append(b)
        # The section headline is written about its top card; if that card was a
        # repeat, lead with the next surviving card instead of a headline about nothing.
        if lost_top_card:
            nxt = next((x for x in blocks if x.get("type") == "card" and x.get("title")), None)
            if nxt:
                s["headline"] = nxt["title"]
            else:
                print(f"surfaced: top card of '{s.get('eyebrow')}' dropped as a repeat and no card left; "
                      f"headline may describe a removed item: {s.get('headline')!r}", file=sys.stderr)
        # A subhead whose items were all repeats has nothing left to head.
        s["blocks"] = [b for k, b in enumerate(blocks)
                       if not (b.get("type") == "subhead"
                               and (k + 1 == len(blocks) or blocks[k + 1].get("type") == "subhead"))]
    return dropped


def record(content, edition, mode=None, today=None):
    mode, today = mode or run_mode(), today or today_la()
    data = load_manifest(PATH)
    cutoff = (date.fromisoformat(today) - timedelta(days=KEEP_DAYS)).isoformat()
    for ed, modes in data.items():
        for m in modes:
            modes[m] = {u: d for u, d in modes[m].items() if d >= cutoff}
    seen = data.setdefault(edition, {}).setdefault(mode, {})
    links = links_in(content.get("sections", [])) | links_in(content.get("top_story", {}))
    for u in links:
        seen.setdefault(u, today)
    save_manifest(PATH, data)
    return len(links)


def main():
    if len(sys.argv) != 3 or sys.argv[1] != "prior":
        sys.exit("usage: surfaced.py prior <edition>")
    edition = sys.argv[2]
    urls = sorted(prior(edition))
    out = f"data/surfaced-prior-{edition}.json"
    with open(out, "w") as f:
        json.dump({"mode": run_mode(), "before": today_la(), "links": urls}, f)
    print(f"{out}: {len(urls)} links already surfaced in {edition} {run_mode()} editions", file=sys.stderr)


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()

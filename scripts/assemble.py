#!/usr/bin/env python3
"""Final assembly step between Claude's content pass and the renderer
(added 2026-10-03):

1. Appends every section from extras/<edition>/*.json (the deterministic
   pre-pass scripts' output) to content-TODAY.json. Claude never copies
   these -- it only read their one-line summaries -- so their text costs no
   output tokens and can't be paraphrased wrong.
2. Builds content["beat"]: every mention of a data/watchlist.json name
   anywhere in the finished edition (Top Story, sections, lists, tables),
   for the "Your Beat" box at the top.
3. Writes a dated copy of the finished content to
   archive/<edition>/<YYYY-MM-DD>-<am|pm>.json (searchable with
   scripts/search_archive.py).

Usage: assemble.py <edition> content-TODAY.json
"""
import html
import json
import os
import re
import sys

from dc_extras import load_extras, load_json, run_mode
from dc_common import today_la

TAG_RE = re.compile(r"<[^>]+>")


def plain(s):
    return html.unescape(TAG_RE.sub("", s or "")).strip()


IDENTITY_HEADERS = {"race", "candidate", "name", "filer", "member", "rep", "who", "#"}


def table_row_text(headers, row):
    """Flatten a table row for Your Beat without losing what each number is:
    '$2,031,458 · $4,536,588' means nothing, 'Supporting: $2,031,458' does."""
    if [h.lower() for h in headers] == ["race", "candidate", "supporting", "opposing", "top spender"] and len(row) == 5:
        race, cand, sup, opp, top = row
        return (f"{race} · {cand}: outside groups newly reported {sup} in independent expenditures supporting "
                f"and {opp} opposing this candidate; the largest single spender is {top}.")
    if len(headers) != len(row):
        return " · ".join(row)
    return " · ".join(c if h.lower() in IDENTITY_HEADERS else f"{h}: {c}" for h, c in zip(headers, row))


def units(content):
    """(section label, html, kind) for every piece of text in the edition."""
    ts = content.get("top_story") or {}
    for p in [ts.get("headline", "")] + list(ts.get("paragraphs", [])):
        yield "Top Story", p, "para"
    for s in content.get("sections", []):
        label = s.get("eyebrow", "")
        for p in [s.get("headline", "")] + list(s.get("paragraphs", [])):
            yield label, p, "para"
        for b in s.get("blocks", []):
            if b.get("type") == "list":
                for it in b.get("items", []):
                    yield label, it, "item"
            elif b.get("type") == "table":
                for row in b.get("rows", []):
                    yield label, table_row_text(b.get("headers") or [], row), "item"
            elif b.get("type") in ("note", "paragraph"):
                yield label, b.get("text", ""), "para"
            elif b.get("type") == "card":
                meta = " · ".join(m for m in b.get("meta") or [] if m)
                yield label, f"<strong>{b.get('title', '')}</strong> ({meta}) {b.get('text', '')}", "para"


def snippet(text_html, rx, kind):
    txt = plain(text_html)
    if kind == "item" and len(txt) <= 450:
        return text_html
    m = rx.search(txt)
    if not m:
        return html.escape(txt[:300], quote=False)
    # the sentence around the match
    start = max(txt.rfind(". ", 0, m.start()) + 2, 0) if txt.rfind(". ", 0, m.start()) >= 0 else 0
    end = txt.find(". ", m.end())
    end = len(txt) if end < 0 else end + 1
    s = txt[start:end].strip()
    if len(s) > 350:
        s = s[: 350].rsplit(" ", 1)[0] + "…"
    return html.escape(s, quote=False)


def beat(content):
    wl = (load_json("data/watchlist.json", {}) or {}).get("entries", [])
    out = []
    all_units = list(units(content))
    for e in wl:
        aliases = [a for a in e.get("aliases", []) if a]
        if not aliases:
            continue
        rx = re.compile(r"(?<![A-Za-z])(?:" + "|".join(aliases) + r")(?![A-Za-z])", re.I)
        mentions, seen, para_sections = [], set(), set()
        for label, h, kind in all_units:
            if label.lower() in ("your beat", "leads"):
                continue
            if kind == "para" and label in para_sections:
                continue  # one prose mention per section (headline + its own paragraph read as a repeat)
            if rx.search(plain(h)):
                if kind == "para":
                    para_sections.add(label)
                sn = snippet(h, rx, kind)
                if sn not in seen:
                    seen.add(sn)
                    mentions.append({"section": label, "html": sn})
            if len(mentions) >= 3:
                break
        if mentions:
            out.append({"name": e["name"], "why": e.get("why"), "mentions": mentions})
    return out


def main():
    if len(sys.argv) != 3:
        print("usage: assemble.py <edition> content-TODAY.json", file=sys.stderr)
        sys.exit(1)
    edition, path = sys.argv[1], sys.argv[2]
    with open(path) as f:
        content = json.load(f)
    have = {s.get("eyebrow", "").lower() for s in content.get("sections", [])}
    for extra in load_extras(edition):
        for s in extra.get("sections", []):
            if s.get("eyebrow", "").lower() in have:
                continue  # never double-print if Claude copied one anyway
            if s.pop("priority", False):  # e.g. Donor Awards: lead the edition, don't trail it
                content.setdefault("sections", []).insert(0, s)
            else:
                content.setdefault("sections", []).append(s)
    content["beat"] = [] if edition == "congress" else beat(content)  # no Your Beat box on the Congress edition
    with open(path, "w") as f:
        json.dump(content, f, indent=2)
        f.write("\n")
    os.makedirs(os.path.join("archive", edition), exist_ok=True)
    arch = os.path.join("archive", edition, f"{today_la()}-{run_mode()}.json")
    with open(arch, "w") as f:
        json.dump(content, f, indent=1)
        f.write("\n")
    print(f"assembled {path}: {len(content.get('sections', []))} sections, {len(content['beat'])} beat hit(s), "
          f"{len(content.get('leads') or [])} lead(s); archived {arch}", file=sys.stderr)


if __name__ == "__main__":
    main()

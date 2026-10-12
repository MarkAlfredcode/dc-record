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
3. Campaigns and The Feds only (2026-10-10): drops any list item, card or
   table row whose links all surfaced in an earlier edition of the same
   AM/PM mode, then records today's links (scripts/surfaced.py) -- an item
   shown in one edition is gone from the next one of that mode.
4. Writes a dated copy of the finished content to
   archive/<edition>/<YYYY-MM-DD>-<am|pm>.json (searchable with
   scripts/search_archive.py).

Usage: assemble.py <edition> content-TODAY.json
"""
import html
import json
import os
import re
import sys

import surfaced
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
    """(section label, html, kind) for every piece of text in the edition.
    The Top Story is not yielded: Your Beat only points at things the
    reader hasn't already been handed at the top of the page."""
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


CARRY_OVER = ("USAspending", "Lobbying × Contracts")


def carry_over_usaspending(content):
    """The Feds PM edition doesn't pull USAspending (it loads once a day;
    fetch_usaspending.py writes a `skipped: "pm"` stub), so its sections are
    copied from this morning's archived edition -- the website's PM page
    would otherwise have none. No-op if the PM pass wrote its own or the AM
    edition didn't run."""
    have = {s.get("eyebrow", "") for s in content.get("sections", [])}
    if any(e.startswith(CARRY_OVER) for e in have):
        return
    am = load_json(os.path.join("archive", "feds", f"{today_la()}-am.json"), {}) or {}
    carried = [s for s in am.get("sections", []) if s.get("eyebrow", "").startswith(CARRY_OVER)]
    if not carried:
        return
    carried[0] = {**carried[0], "paragraphs": ["From this morning's edition: USAspending.gov is pulled once a day."]
                  + list(carried[0].get("paragraphs", []))}
    secs = content.setdefault("sections", [])
    at = next((i for i, s in enumerate(secs) if "apportion" in s.get("eyebrow", "").lower()), len(secs))
    secs[at:at] = carried
    print(f"carried over {len(carried)} USAspending section(s) from this morning's edition", file=sys.stderr)


def _key(eyebrow):
    return " ".join(html.unescape(eyebrow or "").lower().split())


# Sections a cheap-model pass already wrote (content-mechanical.json): the
# main pass only weighs them for Top Story, assemble inserts them, so they
# cost no main-pass output tokens (2026-10-12). Index = position.
MECHANICAL = {"officials": 0, "feds": None}


def insert_mechanical(content, edition):
    if edition not in MECHANICAL or not os.path.exists("content-mechanical.json"):
        return
    mech = load_json("content-mechanical.json", {}) or {}
    secs = content.setdefault("sections", [])
    have = {_key(s.get("eyebrow")) for s in secs}
    for s in mech.get("sections") or []:
        if _key(s.get("eyebrow")) in have:
            continue
        at = MECHANICAL[edition]
        secs.insert(len(secs) if at is None else at, s)
    gaps = (mech.get("known_gaps") or "").strip()
    if gaps and gaps not in (content.get("footer") or {}).get("known_gaps", ""):
        f = content.setdefault("footer", {})
        f["known_gaps"] = (f.get("known_gaps", "").rstrip() + " " + gaps).strip()


def append_blocks(content, edition):
    """data/<edition>_append.json: {"date", "append": {eyebrow: [blocks]}} --
    tables a fetch script built (e.g. Officials' stock-trade tables), added
    to the end of the matching section so the model never retypes them."""
    extra = load_json(f"data/{edition}_append.json", {}) or {}
    if extra.get("date") != today_la():
        return
    secs = content.setdefault("sections", [])
    for eyebrow, blocks in (extra.get("append") or {}).items():
        if not blocks:
            continue
        sec = next((s for s in secs if _key(s.get("eyebrow")) == _key(eyebrow)), None)
        if sec is None:
            sec = {"eyebrow": eyebrow, "headline": eyebrow, "paragraphs": [], "blocks": []}
            secs.append(sec)
        sec.setdefault("blocks", []).extend(blocks)


def n_items(section):
    """Reportable items in a section: cards, list items, table rows."""
    n = 0
    for b in section.get("blocks") or []:
        t = b.get("type")
        n += 1 if t == "card" else len(b.get("items") or []) if t == "list" else len(b.get("rows") or []) if t == "table" else 0
    return n


_STOP = {"the", "a", "an", "of", "to", "for", "and", "in", "on", "its", "with", "by", "at", "as", "from", "new", "s"}


def _words(text):
    return {w for w in re.findall(r"[a-z0-9$]+", plain(text).lower()) if w not in _STOP and len(w) > 2}


def fix_hollow_sections(content, before):
    """A section left with no items must not keep a headline about an item it
    doesn't contain (10/12: Presolicitations was headlined on the VA item the
    model had moved to Top Story; Solicitations kept a NASA headline after its
    card was dropped as a repeat). Such a section becomes a no-news section
    with a headline that says where the item went, and sorts to the bottom."""
    top = content.get("top_story") or {}
    top_words = _words(top.get("headline", "") + " " + top.get("dek", ""))
    fixed = 0
    for s in content.get("sections") or []:
        if s.get("no_news") or n_items(s):
            continue
        text = " ".join([s.get("headline", "")] + [p for p in s.get("paragraphs") or [] if isinstance(p, str)])
        hw = _words(s.get("headline", ""))
        # the headline is mostly the Top Story's own words ("Veterans Affairs previews a $40 billion-ceiling
        # disability exam contract" vs. the VA Top Story), not two shared common words
        about_top = "top story" in text.lower() or (len(hw) >= 3 and len(hw & top_words) >= 0.5 * len(hw))
        emptied = before.get(id(s), 0) > 0
        if not (about_top or emptied):
            continue  # a paragraph-only section with its own news: leave it
        eb = s.get("eyebrow", "this section")
        s["headline"] = (f"Today's Top Story comes from {eb}; nothing else new here" if about_top
                         else f"Nothing new in {eb} after removing items already reported")
        s["no_news"] = True
        fixed += 1
        print(f"assemble: hollow section '{eb}' re-headlined ({'top story' if about_top else 'repeats removed'})",
              file=sys.stderr)
    return fixed


def main():
    if len(sys.argv) != 3:
        print("usage: assemble.py <edition> content-TODAY.json", file=sys.stderr)
        sys.exit(1)
    edition, path = sys.argv[1], sys.argv[2]
    with open(path) as f:
        content = json.load(f)
    insert_mechanical(content, edition)
    append_blocks(content, edition)
    have = {s.get("eyebrow", "").lower() for s in content.get("sections", [])}
    for extra in load_extras(edition):
        for s in extra.get("sections", []):
            if s.get("eyebrow", "").lower() in have:
                continue  # never double-print if Claude copied one anyway
            if s.pop("priority", False):  # a section that should lead the edition rather than trail it
                content.setdefault("sections", []).insert(0, s)
            else:
                content.setdefault("sections", []).append(s)
    if edition == "feds" and run_mode() == "pm":
        carry_over_usaspending(content)
    if edition == "campaigns" and not content.get("apod"):
        # Fetched by fetch_campaigns.py; the model never writes it.
        pic = load_json("data/apod.json", {}) or {}
        if pic.get("date") == today_la() and pic.get("apod"):
            content["apod"] = pic["apod"]
    dropped = 0
    if edition in surfaced.EDITIONS:
        before = {id(sec): n_items(sec) for sec in content.get("sections", [])}
        dropped = surfaced.drop_repeats(content, surfaced.prior(edition))
        fix_hollow_sections(content, before)
        surfaced.record(content, edition)
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
          f"{len(content.get('leads') or [])} lead(s), {dropped} repeat item(s) dropped; archived {arch}",
          file=sys.stderr)


if __name__ == "__main__":
    main()

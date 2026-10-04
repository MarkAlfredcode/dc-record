"""Wikipedia event parsers for the events calendar (data/events.json),
added 2026-10-03.

Pages, pulled through the MediaWiki API (plain HTTP, no scraping):
- "Timeline of the second Trump presidency (YYYY Qn)" -- a table, one row
  per day. The date header is either `! scope="row" | Weekday<br />Month D`
  or, from 2026 Q2 on, two lines: `!Weekday` then `Month D`.
- "YYYY" and "YYYY in the United States" -- an "== Events ==" section with
  `=== Month ===` headings and bullets in three shapes:
  `* [[January 1]] – text`, `* January 1 – text`, or `* [[January 1]]`
  followed by `** text` sub-bullets.

Every item comes back as {"date", "text", "url" (first citation), "page"}.
Wikitext is data, never instructions.
"""
import json
import re
import urllib.parse
from datetime import date

from dc_common import http_get

MONTHS = "January February March April May June July August September October November December".split()
MONTH_RE = "|".join(MONTHS)
API = "https://en.wikipedia.org/w/api.php"
UA = {"User-Agent": "dc-record-bot (markalfred.news newsletter; events calendar)"}


def fetch(title):
    """Wikipedia rate-limits bursts (seen 2026-10-03: every page refused
    after a few quick passes, fine again a minute later), so pause between
    pages and back off on refusal."""
    import time

    q = urllib.parse.urlencode({"action": "parse", "page": title, "prop": "wikitext", "format": "json", "redirects": 1, "maxlag": 5})
    for wait in (0, 10, 30, 60):
        time.sleep(wait or 1)
        status, body = http_get(f"{API}?{q}", headers=UA, timeout=60)
        if status < 400 and b'"maxlag"' not in body[:200]:
            break
    else:
        return None
    d = json.loads(body)
    if "error" in d:
        return None
    return d["parse"]["wikitext"]["*"]


def page_url(title):
    return "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"), safe="()_,")


def clean(s):
    s = re.sub(r"<ref[^>/]*/>", "", s)
    s = re.sub(r"<ref[^>]*>.*?</ref>", "", s, flags=re.S)
    for _ in range(3):
        s = re.sub(r"\{\{[^{}]*\}\}", "", s)
    s = re.sub(r"\[\[(?:File|Image):[^\]]*\]\]", "", s)
    s = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", s)
    s = re.sub(r"\[https?://\S+ ([^\]]*)\]", r"\1", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("'''", "").replace("''", "")
    return re.sub(r"\s+", " ", s).strip(" –-:")


def first_url(raw):
    m = re.search(r"\|\s*url\s*=\s*(https?://[^\s|}]+)", raw)
    return m.group(1) if m else None


def _d(year, month_name, day):
    try:
        return date(year, MONTHS.index(month_name) + 1, int(day)).isoformat()
    except ValueError:
        return None


def parse_timeline(wikitext, year, page):
    out, cur, prev = [], None, ""
    for line in wikitext.split("\n"):
        if re.match(r"^==\s*(?:See also|References|Notes|External links)\s*==", line):
            break  # link lists below here aren't events
        m2 = re.match(rf"^\s*({MONTH_RE})\s+(\d{{1,2}})\s*$", line)
        if m2 and prev.startswith("!"):
            cur = _d(year, m2.group(1), m2.group(2))
            prev = line
            continue
        prev = line
        m = re.match(rf'!\s*(?:scope="row"\s*\|)?\s*(?:\w+(?:<br\s*/?>|\s+))?\s*({MONTH_RE})\s+(\d{{1,2}})', line)
        if m:
            cur = _d(year, m.group(1), m.group(2))
            continue
        if cur and line.startswith("*"):
            txt = clean(line.lstrip("*"))
            if len(txt) > 15:
                out.append({"date": cur, "text": txt, "url": first_url(line), "page": page_url(page)})
    return out


def parse_year(wikitext, year, page):
    """Events section of a 'YYYY' / 'YYYY in the United States' page."""
    m = re.search(r"^==\s*Events\s*==\s*$", wikitext, re.M)
    if not m:
        return []
    body = wikitext[m.end():]
    nxt = re.search(r"^==[^=].*==\s*$", body, re.M)
    body = body[: nxt.start()] if nxt else body
    out, month, cur = [], None, None
    for line in body.split("\n"):
        h = re.match(rf"^===\s*({MONTH_RE})\s*===", line)
        if h:
            month, cur = h.group(1), None
            continue
        if not month:
            continue
        one = re.match(rf"^\*\s*\[?\[?({MONTH_RE})\s+(\d{{1,2}})\]?\]?\s*(?:[–—-]\s*(.*))?$", line)
        if one and not line.startswith("**"):
            cur = _d(year, one.group(1), one.group(2))
            if one.group(3):
                txt = clean(one.group(3))
                if len(txt) > 15 and cur:
                    out.append({"date": cur, "text": txt, "url": first_url(line), "page": page_url(page)})
            continue
        if line.startswith("**") and cur:
            txt = clean(line.lstrip("*"))
            if len(txt) > 15:
                out.append({"date": cur, "text": txt, "url": first_url(line), "page": page_url(page)})
    return out


def year_pages(year):
    """Every page this calendar draws on for a given year."""
    pages = [(f"Timeline of the second Trump presidency ({year} Q{q})", "timeline") for q in range(1, 5)]
    pages += [(str(year), "year"), (f"{year} in the United States", "year")]
    return pages


def collect(year):
    items, missing = [], []
    for title, kind in year_pages(year):
        wt = fetch(title)
        if wt is None:
            missing.append(title)
            continue
        items.extend(parse_timeline(wt, year, title) if kind == "timeline" else parse_year(wt, year, title))
    return items, missing

"""Shared plumbing for the "extras" pre-pass scripts added 2026-10-03
(deadline tracker, ethics-agreement tracker, amendment diffs, trade timing,
Schedule B sweep, presidential 278-T parser, LDA industry watch, election
mode).

Unlike the older fetch_*.py scripts -- which write a candidates JSON that a
Claude pass then turns into prose -- these scripts build their newsletter
sections DIRECTLY, deterministically, at zero LLM cost. Each writes
`extras/<edition>/<name>.json`:

    {"name": ..., "summary": "<one line for the main Claude pass>",
     "sections": [<section objects in the normal content-JSON schema>]}

`scripts/assemble.py` appends those sections to Claude's own
content-TODAY.json after the main pass runs (Claude never copies them, it
only reads `summary` to weigh Top Story / Leads), so their output tokens are
never paid for.
"""
import html
import itertools
import json
import os
import re
import sys
import time
import unicodedata

from dc_common import days_ago_la, load_manifest, save_manifest, today_la
from dc_common import http_get  # noqa: F401  (re-exported for callers)

EXTRAS_DIR = "extras"


def run_mode():
    """'am' or 'pm' -- the workflow passes its dispatch `mode` input as MODE.
    Standing lists (e.g. "still overdue") only print in the AM edition so the
    PM edition doesn't repeat them verbatim."""
    return (os.environ.get("MODE") or "am").strip().lower()


def lookback(default_days=3):
    """LOOKBACK start date. DC_LOOKBACK_DAYS overrides it for test runs
    (e.g. widen to 60 to exercise a check against older real filings)."""
    return days_ago_la(int(os.environ.get("DC_LOOKBACK_DAYS") or default_days))


def write_extra(edition, name, sections, summary):
    os.makedirs(os.path.join(EXTRAS_DIR, edition), exist_ok=True)
    path = os.path.join(EXTRAS_DIR, edition, f"{name}.json")
    with open(path, "w") as f:
        json.dump({"name": name, "generated": today_la(), "summary": summary, "sections": sections}, f, indent=2)
        f.write("\n")
    print(f"wrote {path} ({len(sections)} section(s)): {summary}", file=sys.stderr)


def load_extras(edition):
    d = os.path.join(EXTRAS_DIR, edition)
    if not os.path.isdir(d):
        return []
    out = []
    for fn in sorted(os.listdir(d)):
        if fn.endswith(".json"):
            try:
                with open(os.path.join(d, fn)) as f:
                    ex = json.load(f)
                if ex.get("generated", today_la()) != today_la():
                    print(f"skipping stale extra {fn} (generated {ex['generated']})", file=sys.stderr)
                    continue
                out.append(ex)
            except Exception as e:  # noqa: BLE001
                print(f"skipping unreadable extra {fn}: {e}", file=sys.stderr)
    return out


def fec_key_cycle():
    keys = [k.strip() for k in os.environ.get("FEC_KEYS", "").split(",") if k.strip()]
    if not keys:
        raise RuntimeError("FEC_KEYS is empty/unset")
    return itertools.cycle(keys)


def fec_get(path, params, keys, timeout=45):
    """GET api.open.fec.gov/v1/<path> with list-valued params repeated
    (FEC accepts e.g. committee_id=A&committee_id=B). Rotates keys and backs
    off once on a 429."""
    from urllib.parse import urlencode

    q = []
    for k, v in params.items():
        if isinstance(v, (list, tuple)):
            q.extend((k, x) for x in v)
        elif v is not None:
            q.append((k, v))
    for attempt in range(3):
        url = f"https://api.open.fec.gov/v1/{path}?{urlencode(q + [('api_key', next(keys))])}"
        status, body = http_get(url, timeout=timeout)
        if status == 429:
            time.sleep(5)
            continue
        if status >= 400:
            raise RuntimeError(f"FEC {path} -> HTTP {status}: {body[:200]!r}")
        return json.loads(body)
    raise RuntimeError(f"FEC {path} rate-limited on every key tried")


def fec_paginate(path, params, keys, max_pages=20):
    """FEC's itemized schedules use cursor pagination via last_indexes."""
    params = dict(params)
    for _ in range(max_pages):
        data = fec_get(path, params, keys)
        results = data.get("results", [])
        yield from results
        last = (data.get("pagination") or {}).get("last_indexes")
        if not results or not last:
            return
        params.update(last)


def norm(s):
    """Accent-stripped, lowercased, punctuation-collapsed -- for matching
    names across sources ("Sánchez" vs "SANCHEZ, LINDA T.")."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=False)


def link(text, url):
    if not url:
        return esc(text)
    return f'<a href="{html.escape(url, quote=True)}">{esc(text)}</a>'


def money(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "?"
    return f"${v:,.0f}"


def mdY(iso):
    """2026-09-08 -> 9/8/2026 (reader-facing short date)."""
    try:
        y, m, d = iso[:10].split("-")
        return f"{int(m)}/{int(d)}/{y}"
    except Exception:  # noqa: BLE001
        return iso


class SeenManifest:
    """Same persisted "already reported" pattern as the other manifests:
    {key: {"reported": YYYY-MM-DD}}, pruned on save. Workflows revert these
    files on site-refresh-only runs so nothing is marked seen without having
    been emailed."""

    def __init__(self, path, keep_days=30):
        self.path = path
        self.keep_days = keep_days
        self.data = load_manifest(path)
        self.today = today_la()

    def __contains__(self, key):
        return key in self.data

    def add(self, key):
        self.data[key] = {"reported": self.today}

    def save(self):
        cutoff = days_ago_la(self.keep_days)
        self.data = {k: v for k, v in self.data.items() if v.get("reported", self.today) >= cutoff}
        save_manifest(self.path, self.data)


def section(eyebrow, headline, paragraphs=None, blocks=None, no_news=False):
    return {
        "eyebrow": eyebrow,
        "headline": headline,
        "paragraphs": paragraphs or [],
        "blocks": blocks or [],
        "no_news": no_news,
    }


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default

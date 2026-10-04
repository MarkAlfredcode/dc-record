"""OGE "Officials Individual Disclosures" index -- the whole thing.

OGE moved its search UI's data source from API.xsp/v2/rest to
API.xsp/v3/rest (found 2026-10-03, when v2 started returning an XPages HTML
shell instead of JSON and that day's OGE section silently came back empty).
v3 caps `length` at 100 and ignores the DataTables `order` param (rows come
back alphabetical by name, not newest-first), so the old "page newest-first
until you run off the window" approach can't work anymore. Instead this
pulls every page (~170 x 100 rows, ~8s with a small thread pool) and lets
callers filter locally -- which also gives the ethics-agreement tracker the
full history it needs for free.

The full index is cached to /tmp for the rest of the run so the several
Officials scripts that use it don't each re-download it.
"""
import concurrent.futures as cf
import json
import os
import re
import sys
import time

from dc_common import http_get_json

V3 = "https://extapps2.oge.gov/201/Presiden.nsf/API.xsp/v3/rest"
CACHE = "/tmp/dc_record_oge_index.json"
PAGE = 100


def _page(start):
    return http_get_json(f"{V3}?draw=1&start={start}&length={PAGE}", timeout=60)


def fetch_all(max_age_sec=3600):
    if os.path.exists(CACHE) and time.time() - os.path.getmtime(CACHE) < max_age_sec:
        with open(CACHE) as f:
            return json.load(f)
    first = _page(0)
    total = int(first.get("recordsTotal") or 0)
    rows = list(first.get("data", []))
    with cf.ThreadPoolExecutor(8) as ex:
        for data in ex.map(_page, range(PAGE, total, PAGE)):
            rows.extend(data.get("data", []))
    # The index itself carries exact duplicate rows; collapse them.
    seen, uniq = set(), []
    for r in rows:
        k = json.dumps(r, sort_keys=True)
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    if total and len(rows) < total * 0.95:
        raise RuntimeError(f"OGE index short read: {len(rows)} of {total} rows")
    print(f"OGE index: {len(uniq)} unique rows ({total} reported)", file=sys.stderr)
    with open(CACHE, "w") as f:
        json.dump(uniq, f)
    return uniq


_TAG_RE = re.compile(r"<[^>]+>")
_PAREN_NOISE_RE = re.compile(r"\((?:Request this Document|Contact agency to request)\)", re.I)


def label(type_str):
    """'<a href=...>278 Transaction</a> (Amended 08/12/2025)' -> '278 Transaction (Amended 08/12/2025)'."""
    s = _PAREN_NOISE_RE.sub("", _TAG_RE.sub("", type_str or ""))
    return re.sub(r"\s+", " ", s).strip()


def base_label(type_str):
    """Label with amendment/pending annotations dropped -- for grouping a
    filing with its own earlier/later versions."""
    s = re.sub(r"\((?:Amended[^)]*|Pending Final OGE Disposition)\)", "", label(type_str))
    return re.sub(r"\s+", " ", s).strip()


def doc_link(type_str):
    m = re.search(r"href=['\"]([^'\"]+)['\"]", type_str or "")
    return m.group(1).replace(" ", "%20") if m else None


def is_pdf(url):
    return bool(url) and url.lower().split("?")[0].endswith(".pdf")


def split_name(name):
    """'Bedford, Bryan' -> ('bedford', 'bryan'); 'Trump, Donald J' -> ('trump', 'donald')."""
    last, _, first = (name or "").partition(",")
    first_tok = first.strip().split(" ")[0] if first.strip() else ""
    return last.strip().lower(), first_tok.lower()

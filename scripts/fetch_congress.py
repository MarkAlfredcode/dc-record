#!/usr/bin/env python3
"""Everything the Congress main pass used to fetch itself, done here with no
LLM (added 2026-10-12): the newest Congressional Record issue on govinfo,
its Daily Digest, the bill-introduction granules (House "Public Bills and
Resolutions", Senate introductions/submissions, committee reports), and a
congress.gov sponsor lookup for every measure introduced. The model used to
do this in 9-16 tool turns, one congress.gov call per bill.

The model gets one text bundle; bills are numbered [B1].. and linked as
href="#B1" (run_pass.py swaps in the congress.gov page URL).

Output: data/congress_input.txt (bundle), data/congress_input.json
(package_id, date_issued, refs, errors -- gate.py skips the model when the
package was already covered, see data/congress-covered.json).
"""
import html
import json
import os
import re
import sys
import time
from datetime import date
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dc_common import check_reachable, days_ago_la, http_get, today_la  # noqa: E402
from fetch_campaigns import extras_digest  # noqa: E402

OUT_TXT = "data/congress_input.txt"
OUT_JSON = "data/congress_input.json"
GOV = "https://api.govinfo.gov"
CONGRESS = 119
MAX_GRANULE_CHARS = 20_000
INTRO_TITLES = ("PUBLIC BILLS AND RESOLUTIONS", "INTRODUCTION OF BILLS AND JOINT RESOLUTIONS",
                "SUBMISSION OF CONCURRENT AND SENATE RESOLUTIONS",
                "REPORTS OF COMMITTEES ON PUBLIC BILLS AND RESOLUTIONS")
# CR text form -> (congress.gov API type, congress.gov page segment)
TYPES = {
    "H.R.": ("hr", "house-bill"), "H. Res.": ("hres", "house-resolution"),
    "H.J. Res.": ("hjres", "house-joint-resolution"), "H. Con. Res.": ("hconres", "house-concurrent-resolution"),
    "S.": ("s", "senate-bill"), "S. Res.": ("sres", "senate-resolution"),
    "S.J. Res.": ("sjres", "senate-joint-resolution"), "S. Con. Res.": ("sconres", "senate-concurrent-resolution"),
}
BILL_RE = re.compile(r"(?<![A-Za-z.])(H\.R\.|H\. Res\.|H\.J\. Res\.|H\. Con\. Res\.|S\.J\. Res\.|S\. Con\. Res\.|"
                     r"S\. Res\.|S\.) (\d+)\.\s+(?:A|An) (?:bill|resolution|joint resolution|concurrent resolution)\b")
TAG = re.compile(r"<[^>]+>")


def keys():
    ks = [k.strip() for k in os.environ.get("FEC_KEYS", "").split(",") if k.strip()]
    if not ks:
        raise RuntimeError("FEC_KEYS is empty/unset")
    i = 0
    while True:
        yield ks[i % len(ks)]
        i += 1


def get(url, key_iter, as_json=True, tries=3):
    for attempt in range(tries):
        sep = "&" if "?" in url else "?"
        status, body = http_get(f"{url}{sep}api_key={next(key_iter)}",
                                headers={"User-Agent": "Mozilla/5.0 (dc-record)"}, timeout=45)
        if status == 429 or status >= 500:
            time.sleep(3 * (attempt + 1))
            continue
        if status >= 400:
            raise RuntimeError(f"HTTP {status} for {url.split('?')[0]}")
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        return json.loads(body) if as_json else body
    raise RuntimeError(f"gave up on {url.split('?')[0]}")


def plain(htm):
    t = html.unescape(TAG.sub("", htm or ""))
    # Drop the GPO boilerplate header lines.
    lines = [l.rstrip() for l in t.split("\n")
             if l.strip() and not re.match(r"\s*(\[?Congressional Record|From the Congressional Record Online|\[\[Page)", l)]
    return "\n".join(" ".join(l.split()) for l in lines)


def main():
    today = today_la()
    errors, out = [], {"today": today, "package_id": None, "refs": {}, "errors": []}
    if not check_reachable("api.govinfo.gov"):
        out["errors"] = errors = ["api.govinfo.gov unreachable"]
        json.dump(out, open(OUT_JSON, "w"), indent=1)
        open(OUT_TXT, "w").write("GOVINFO UNREACHABLE: api.govinfo.gov failed to connect.\n")
        return
    k = keys()
    since = days_ago_la(10)
    pk = get(f"{GOV}/collections/CREC/{since}T00:00:00Z?pageSize=100&offsetMark=%2A", k)
    # lastModified also catches re-published old issues (an April issue was
    # re-modified 10/06): the newest issue is the max dateIssued.
    pkgs = sorted(pk.get("packages", []), key=lambda p: p.get("dateIssued", ""), reverse=True)
    if not pkgs:
        errors.append(f"no Congressional Record issue on govinfo since {since}")
        out["errors"] = errors
        json.dump(out, open(OUT_JSON, "w"), indent=1)
        open(OUT_TXT, "w").write(f"NO ISSUE: no Congressional Record issue on govinfo since {since}.\n")
        return
    pkg = pkgs[0]
    pid, issued = pkg["packageId"], pkg.get("dateIssued")
    out.update(package_id=pid, date_issued=issued)

    granules, url = [], f"{GOV}/packages/{pid}/granules?pageSize=1000&offsetMark=%2A"
    while url:
        g = get(url, k)
        granules += g.get("granules", [])
        url = g.get("nextPage")

    def text_of(gr):
        try:
            return plain(get(f"{GOV}/packages/{pid}/granules/{quote(gr['granuleId'])}/htm", k, as_json=False))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{gr['granuleId']}: {e}")
            return ""

    digest = [g for g in granules if g.get("granuleClass") == "DAILYDIGEST" or "-PgD" in g["granuleId"]]
    intro = [g for g in granules if g.get("title", "").strip().upper() in INTRO_TITLES]
    digest_text = [(g["title"], text_of(g)) for g in digest]
    intro_text = [(g["title"], text_of(g)) for g in intro]

    bills, seen = [], set()
    for gtitle, t in intro_text:
        how = "reported by committee" if gtitle.strip().upper().startswith("REPORTS") else "introduced"
        for m in BILL_RE.finditer(" ".join(t.split())):
            key = (m.group(1), m.group(2))
            if key not in seen:
                seen.add(key)
                bills.append((*key, how))
    refs, table = {}, []
    for i, (form, num, how) in enumerate(bills, 1):
        api_type, seg = TYPES[form]
        refs[f"B{i}"] = f"https://www.congress.gov/bill/{CONGRESS}th-congress/{seg}/{num}"
        sponsor = title = ""
        try:
            b = get(f"https://api.congress.gov/v3/bill/{CONGRESS}/{api_type}/{num}?format=json", k).get("bill", {})
            title = b.get("title") or ""
            sp = (b.get("sponsors") or [{}])[0]
            if sp:
                dist = f"-{sp['district']}" if sp.get("district") not in (None, "", 0) else ""
                sponsor = (f"{'Sen.' if form.startswith('S') else 'Rep.'} {sp.get('firstName', '')} "
                           f"{sp.get('lastName', '')} ({sp.get('party')}-{sp.get('state')}{dist})").replace("  ", " ")
        except Exception as e:  # noqa: BLE001
            errors.append(f"congress.gov {form} {num}: {e} (not yet indexed? use the Record's sponsor line)")
        table.append(f"[B{i}] {form} {num} | {how} | sponsor: {sponsor or '(see Record text)'} | {title}")
    out["refs"], out["errors"], out["bills"] = refs, errors, len(bills)

    d = date.fromisoformat(today)
    di = date.fromisoformat(issued) if issued else None
    L = [f"=== TODAY'S INPUT (built by scripts/fetch_congress.py, {today}) ===",
         f"TODAY = {today} ({d.strftime('%A, %B')} {d.day}, {d.year}).",
         f"Newest Congressional Record issue: {pid}, dated "
         f"{(di.strftime('%A, %B ') + str(di.day) + ', ' + str(di.year)) if di else issued}.",
         f"Measures in it: {sum(1 for b in bills if b[2] == 'introduced')} introduced or submitted, "
         f"{sum(1 for b in bills if b[2] != 'introduced')} reported by committee.",
         "Errors/gaps: " + ("; ".join(errors) or "none"), "",
         f"--- MEASURES ({len(bills)}): link each as href=\"#B<n>\" ---"] + table + [""]
    L.append("--- DAILY DIGEST ---" if digest_text else
             "--- NO DAILY DIGEST IN THIS ISSUE; granule titles by chamber instead ---")
    if digest_text:
        for title, t in digest_text:
            L += [f"## {title}", t[:MAX_GRANULE_CHARS], ""]
    else:
        for cls in ("SENATE", "HOUSE"):
            L.append(f"## {cls}: " + " | ".join(g["title"] for g in granules if g.get("granuleClass") == cls))
        L.append("")
    L.append("--- BILL-INTRODUCTION TEXT FROM THE RECORD (titles, referrals, cosponsors) ---")
    for title, t in intro_text:
        L += [f"## {title}", t[:MAX_GRANULE_CHARS * 2], ""]
    L.append("--- DETERMINISTIC EXTRAS (assemble.py appends these sections after you; do not copy them) ---")
    L += extras_digest("congress")
    open(OUT_TXT, "w").write("\n".join(L) + "\n")
    json.dump(out, open(OUT_JSON, "w"), indent=1)
    print(f"fetch_congress: {pid} ({issued}), {len(digest)} digest + {len(intro)} introduction granules, "
          f"{len(bills)} measures; bundle {sum(map(len, L)):,} chars; errors: {len(errors)}", file=sys.stderr)


if __name__ == "__main__":
    main()

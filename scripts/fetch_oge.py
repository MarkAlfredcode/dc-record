#!/usr/bin/env python3
"""Task 11 (OGE Executive Branch Ethics Filings) fetch/filter. Mechanizes
pagination and flagging: "Certificate of Divestiture" / "Termination" are
literal substring matches on the `type` field, no judgment needed -- this
script flags them directly. PDF-linked filings get pdftotext'd here too.
Claude's only remaining job is writing the characterization prose for
whatever's flagged.

LOOKBACK + dedupe manifest (added 2026-09-12, same fix and reasoning as the
FEC Task 1/2 manifests): confirmed by hand that OGE's own `docDate` field is
a POSTING date, not the underlying document's date -- e.g. an ethics
agreement signed September 7 didn't get a `docDate` (and therefore didn't
become visible to a 1-day rolling window) until September 10, a 3-day
posting lag. The old 1-day window with no persisted state meant a filing
indexed more than a day late could fall before every future day's window
and be permanently missed, exactly like the real FEC Talarico Form 99 miss
that prompted that fix. A wider LOOKBACK + manifest fixes this the same way.

Output: data/oge_candidates.json
Side effect: rewrites data/oge-filings-seen.json.
"""
import re
import sys

from dc_common import (
    check_reachable, days_ago_la, download, load_manifest, pdftotext,
    save_manifest, today_la, write_candidates, http_get_json,
)

OUT_PATH = "data/oge_candidates.json"
MANIFEST_PATH = "data/oge-filings-seen.json"
BASE = "https://extapps2.oge.gov/201/Presiden.nsf/API.xsp/v2/rest"

UNID_RE = re.compile(r"Presiden\.nsf/[^/]+/([0-9A-Fa-f]{32})/")


def stable_key(row):
    """A row has no explicit id field. Prefer the 32-hex-char Notes/Domino
    document UNID embedded in PDF-linked rows' href (genuinely unique);
    request-gated rows (the majority) don't carry one, so fall back to
    name + docDate (second-precision) + the type label with HTML stripped --
    stable enough in practice since a given person's given filing posts
    exactly once at exactly one timestamp."""
    type_str = row.get("type", "") or ""
    m = UNID_RE.search(type_str)
    if m:
        return "unid:" + m.group(1)
    label = re.sub(r"<[^>]+>", "", type_str).strip()
    return f"name:{row.get('name','')}|{row.get('docDate','')}|{label}"


def main():
    lookback = days_ago_la(3)
    today = today_la()
    reachable = check_reachable("extapps2.oge.gov")
    out = {
        "window_start": lookback,
        "reachable": reachable,
        "flagged": [],
        "non_flagged_by_agency": {},
        "non_flagged_files": [],
        "errors": [],
        "counts": {},
    }
    if not reachable:
        out["errors"].append("extapps2.oge.gov unreachable")
        write_candidates(OUT_PATH, out)
        return

    manifest = load_manifest(MANIFEST_PATH)

    rows_in_window = []
    start = 0
    length = 100
    while True:
        url = f"{BASE}?draw=1&start={start}&length={length}"
        try:
            data = http_get_json(url, timeout=30)
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"page start={start} failed: {e}")
            break
        batch = data.get("data", [])
        if not batch:
            break
        in_window = [r for r in batch if r.get("docDate", "") >= lookback]
        rows_in_window.extend(in_window)
        print(f"  OGE page start={start}: {len(in_window)}/{len(batch)} in window", file=sys.stderr)
        if len(in_window) < len(batch):
            break  # this batch ran off the front of the window; sorted newest-first, so stop
        start += length

    print(f"OGE: {len(rows_in_window)} rows in {lookback}..{today} window", file=sys.stderr)

    # Dedupe against prior editions -- this is the part that actually fixes
    # the late-posting problem: a row already reported (even if it first
    # became visible several days ago and keeps reappearing in the widened
    # window) doesn't get reported twice.
    new_rows = []
    for r in rows_in_window:
        key = stable_key(r)
        if key in manifest:
            continue
        new_rows.append((key, r))
    print(f"OGE: {len(new_rows)} new-to-report of {len(rows_in_window)} in window", file=sys.stderr)

    agency_counts = {}
    for key, r in new_rows:
        type_str = r.get("type", "") or ""
        low = type_str.lower()
        flag_reason = None
        if "certificate of divestiture" in low:
            flag_reason = "divestiture"
        elif "termination" in low:
            flag_reason = "termination"

        link_match = re.search(r"href=['\"]([^'\"]+)['\"]", type_str)
        link = link_match.group(1) if link_match else None
        is_pdf_link = bool(link) and link.lower().endswith(".pdf")
        pending_final = "pending final oge disposition" in low

        if flag_reason or is_pdf_link:
            pdf_text = None
            if is_pdf_link:
                try:
                    local_pdf = f"/tmp/oge_{abs(hash(link))}.pdf"
                    download(link, local_pdf, timeout=30)
                    pdf_text = pdftotext(local_pdf)[:20000]
                except Exception as e:  # noqa: BLE001
                    out["errors"].append(f"OGE PDF fetch failed for {link}: {e}")
            out["flagged"].append({
                "docDate": r.get("docDate"),
                "name": r.get("name"),
                "agency": r.get("agency"),
                "title": r.get("title"),
                "level": r.get("level"),
                "amended": r.get("amended"),
                "type": type_str,
                "flag_reason": flag_reason,
                "pending_final_disposition": pending_final,
                "link": link,
                "is_pdf_link": is_pdf_link,
                "pdf_text": pdf_text,
            })
        else:
            agency = r.get("agency", "Unknown")
            agency_counts[agency] = agency_counts.get(agency, 0) + 1
            out["non_flagged_files"].append({
                "docDate": r.get("docDate"),
                "name": r.get("name"),
                "agency": agency,
                "title": r.get("title"),
                "level": r.get("level"),
                "amended": r.get("amended"),
                "type": re.sub(r"<[^>]+>", "", type_str).strip(),
            })
        manifest[key] = {"reported": today}

    # Prune entries older than 14 days (bounds file size; far beyond any
    # posting lag actually observed -- same bound used by the FEC manifests).
    cutoff = days_ago_la(14)
    manifest = {k: v for k, v in manifest.items() if v.get("reported", today) >= cutoff}
    save_manifest(MANIFEST_PATH, manifest)

    out["non_flagged_by_agency"] = agency_counts
    out["counts"]["total_in_window"] = len(rows_in_window)
    out["counts"]["new_to_report"] = len(new_rows)
    out["counts"]["flagged"] = len(out["flagged"])
    write_candidates(OUT_PATH, out)


if __name__ == "__main__":
    main()

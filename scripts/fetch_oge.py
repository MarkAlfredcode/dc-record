#!/usr/bin/env python3
"""Task 11 (OGE Executive Branch Ethics Filings) fetch/filter. Mechanizes
pagination and flagging: "Certificate of Divestiture" / "Termination" are
literal substring matches on the `type` field, no judgment needed -- this
script flags them directly. PDF-linked filings get pdftotext'd here too.
Claude's only remaining job is writing the characterization prose for
whatever's flagged.

Output: data/oge_candidates.json
"""
import re
import sys

from dc_common import check_reachable, download, pdftotext, yesterday_la, write_candidates, http_get_json

OUT_PATH = "data/oge_candidates.json"
BASE = "https://extapps2.oge.gov/201/Presiden.nsf/API.xsp/v2/rest"


def main():
    window_start = yesterday_la()
    reachable = check_reachable("extapps2.oge.gov")
    out = {
        "window_start": window_start,
        "reachable": reachable,
        "flagged": [],
        "non_flagged_by_agency": {},
        "errors": [],
        "counts": {},
    }
    if not reachable:
        out["errors"].append("extapps2.oge.gov unreachable")
        write_candidates(OUT_PATH, out)
        return

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
        in_window = [r for r in batch if r.get("docDate", "") >= window_start]
        rows_in_window.extend(in_window)
        print(f"  OGE page start={start}: {len(in_window)}/{len(batch)} in window", file=sys.stderr)
        if len(in_window) < len(batch):
            break  # this batch ran off the front of the window; sorted newest-first, so stop
        start += length

    print(f"OGE: {len(rows_in_window)} rows in window", file=sys.stderr)
    agency_counts = {}
    for r in rows_in_window:
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
            agency_counts[r.get("agency", "Unknown")] = agency_counts.get(r.get("agency", "Unknown"), 0) + 1

    out["non_flagged_by_agency"] = agency_counts
    out["counts"]["total_in_window"] = len(rows_in_window)
    out["counts"]["flagged"] = len(out["flagged"])
    write_candidates(OUT_PATH, out)


if __name__ == "__main__":
    main()

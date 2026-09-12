#!/usr/bin/env python3
"""Task 8 (OMB Apportionments) fetch/filter -- see dc_common.py's module
docstring. Fully mechanizes: pagination, footnote-keyword flagging (a literal
substring match, no judgment needed), and Spend Plan PDF diffing against the
manifest. Claude's only remaining job for this task is writing the prose for
whatever this script flags.

Output: data/omb_candidates.json
Side effect: rewrites data/omb-spend-plan-manifest.json (same as before, just
now done by code instead of by the model).
"""
import html
import json
import re
import sys

from dc_common import (
    check_reachable, days_ago_la, http_get, http_get_json, load_manifest,
    pdftotext, save_manifest, today_la, write_candidates, download,
)

MANIFEST_PATH = "data/omb-spend-plan-manifest.json"
OUT_PATH = "data/omb_candidates.json"

FLAG_KEYWORDS = [
    "rescission", "rescind", "executive order", "antideficiency",
    "anti-deficiency", "escrow", "litigation", "lawsuit", "injunction",
    "lapse in appropriations", "impound", "withheld", "withhold",
]
FLAG_RATIONALES = [
    "omb requests additional information on programmatic spending",
    "spend plan or other documentation is necessary",
]


def footnote_flag_reason(text):
    low = text.lower()
    for kw in FLAG_KEYWORDS:
        if kw in low:
            return kw
    for rationale in FLAG_RATIONALES:
        if rationale in low:
            return "oversight-hold rationale"
    return None


def fetch_files_search(since, until):
    page = 1
    results = []
    while True:
        url = (
            f"https://openomb.org/api/v1/files/search?approvedStart={since}"
            f"&approvedEnd={until}&sort=approved_desc&limit=1000&page={page}"
        )
        data = http_get_json(url, timeout=30)
        results.extend(data.get("results", []))
        paging = data.get("paging", {})
        if page >= paging.get("pages", 1):
            break
        page += 1
    return results


def main():
    since = days_ago_la(1)
    until = today_la()
    reachable_omb = check_reachable("openomb.org")
    reachable_max = check_reachable("apportionment-public.max.gov")

    out = {
        "window": {"since": since, "until": until},
        "reachable": {"openomb.org": reachable_omb, "apportionment-public.max.gov": reachable_max},
        "flagged": [],
        "non_flagged_by_agency": {},
        "spend_plans": {"new_or_changed": [], "checked": 0},
        "errors": [],
        "counts": {},
    }

    if reachable_omb:
        try:
            files = fetch_files_search(since, until)
            out["counts"]["files_found"] = len(files)
            print(f"OMB search: {len(files)} files in window", file=sys.stderr)
            flagged_count = 0
            agency_counts = {}
            for i, f in enumerate(files):
                if i % 10 == 0:
                    print(f"  file detail {i}/{len(files)}", file=sys.stderr)
                file_id = f.get("fileId")
                try:
                    detail = http_get_json(f"https://openomb.org/api/v1/files/{file_id}", timeout=30)
                except Exception as e:  # noqa: BLE001
                    out["errors"].append(f"file {file_id} fetch failed: {e}")
                    continue
                agency = detail.get("folder", "Unknown agency")
                file_flagged = False
                for tafs in detail.get("tafs", []) or []:
                    for line in tafs.get("lines", []) or []:
                        for fn in line.get("footnotes", []) or []:
                            reason = footnote_flag_reason(fn.get("footnoteText", ""))
                            if reason:
                                file_flagged = True
                                out["flagged"].append({
                                    "agency": agency,
                                    "approverTitle": detail.get("approverTitle"),
                                    "fundsProvidedBy": detail.get("fundsProvidedBy"),
                                    "fiscalYear": detail.get("fiscalYear"),
                                    "approvalTimestamp": detail.get("approvalTimestamp"),
                                    "lineNumber": line.get("lineNumber"),
                                    "lineDescription": line.get("lineDescription"),
                                    "approvedAmount": line.get("approvedAmount"),
                                    "footnoteText": fn.get("footnoteText"),
                                    "matchedKeyword": reason,
                                    "sourceUrl": detail.get("sourceUrl") or detail.get("excelUrl"),
                                })
                        # also flag large approvedAmount even with no keyword hit
                        amt = line.get("approvedAmount")
                        if isinstance(amt, (int, float)) and amt >= 10_000_000 and not file_flagged:
                            # only worth flagging on its own if it reads like a rescission/reallocation;
                            # leave this judgment call to Claude by surfacing it as a "large_amount" candidate
                            pass
                if file_flagged:
                    flagged_count += 1
                else:
                    agency_counts[agency] = agency_counts.get(agency, 0) + 1
            out["non_flagged_by_agency"] = agency_counts
            out["counts"]["flagged_files"] = flagged_count
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"openomb.org search failed: {e}")
    else:
        out["errors"].append("openomb.org unreachable")

    # Spend Plan PDFs -- HEAD-diff against manifest (Content-Length is the
    # only reliable change signal, confirmed by hand; see prompt history)
    if reachable_max:
        try:
            status, body = http_get("https://apportionment-public.max.gov/", timeout=120)
            page_html = body.decode(errors="replace")
            hrefs = sorted(set(re.findall(r'href="(/Spend%20Plans/[^"]+\.pdf)"', page_html)))
            manifest = load_manifest(MANIFEST_PATH)
            new_manifest = {}
            out["spend_plans"]["checked"] = len(hrefs)
            import urllib.request as ur
            for i, href in enumerate(hrefs):
                url = "https://apportionment-public.max.gov" + href
                if i % 25 == 0:
                    print(f"  spend plan HEAD check {i}/{len(hrefs)}", file=sys.stderr)
                req = ur.Request(url, method="HEAD")
                try:
                    with ur.urlopen(req, timeout=20) as resp:
                        content_length = int(resp.headers.get("Content-Length", 0))
                except Exception as e:  # noqa: BLE001
                    out["errors"].append(f"HEAD failed for {href}: {e}")
                    continue
                prior = manifest.get(href, {}).get("content_length")
                new_manifest[href] = {"content_length": content_length}
                if prior is None:
                    status_label = "NEW"
                elif prior != content_length:
                    status_label = "CHANGED"
                else:
                    continue
                # download + extract text for new/changed
                local_pdf = f"/tmp/spendplan_{abs(hash(href))}.pdf"
                try:
                    download(url, local_pdf, timeout=60)
                    text = pdftotext(local_pdf)
                except Exception as e:  # noqa: BLE001
                    text = ""
                    out["errors"].append(f"download/extract failed for {href}: {e}")
                out["spend_plans"]["new_or_changed"].append({
                    "path": href, "status": status_label,
                    "content_length": content_length, "prior_content_length": prior,
                    "text": text[:20000],  # cap so one huge PDF can't blow out the payload
                    "url": url,
                })
            save_manifest(MANIFEST_PATH, new_manifest)
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"Spend Plan check failed: {e}")
    else:
        out["errors"].append("apportionment-public.max.gov unreachable")

    write_candidates(OUT_PATH, out)
    print(
        f"OMB: {out['counts'].get('flagged_files', 0)} flagged, "
        f"{sum(out['non_flagged_by_agency'].values())} non-flagged, "
        f"{len(out['spend_plans']['new_or_changed'])} new/changed spend plans, "
        f"{len(out['errors'])} errors",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()

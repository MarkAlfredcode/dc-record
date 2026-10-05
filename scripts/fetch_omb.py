#!/usr/bin/env python3
"""Task 8 (OMB Apportionments) fetch/filter -- see dc_common.py's module
docstring. Fully mechanizes: pagination, footnote-keyword flagging (a literal
substring match, no judgment needed), and Spend Plan PDF diffing against the
manifest. Claude's only remaining job for this task is writing the prose for
whatever this script flags.

Output: data/omb_candidates.json
Side effects:
- rewrites data/omb-spend-plan-manifest.json (Spend Plan PDF content-length
  tracking, unchanged from before).
- rewrites data/omb-apportionment-manifest.json (NEW, 2026-09-13 -- see
  "TWO-SOURCE DESIGN" below).

=== TWO-SOURCE DESIGN (2026-09-13) ===
Confirmed by hand the same day: openomb.org (switched to 2026-09-04 to fix a
session-budget blowout that happened when this ran inside Claude's own
reasoning loop) has a real ~2-3 day ingestion lag behind the authoritative
apportionment-public.max.gov mirror -- querying it for files "approved
yesterday" routinely returns zero even when the raw site already shows new
filings from that window. With the old 1-day rolling window and no
persisted state, anything filed in roughly the last 3 days was silently
never caught, no matter which day this ran. Confirmed live: CIGIE and RRB
filings from 2026-09-10 never appeared in an openomb.org search run
2026-09-11, and openomb's own most-recent-approval-on-record was still
2026-09-09 as of 2026-09-13.

Two independent fixes, run together rather than either/or, so a gap in one
doesn't silently lose an item:
1. Widen the openomb.org window from 1 day to 5 days, so a lagged item that
   eventually gets indexed still falls inside a later run's window instead
   of aging out of range forever.
2. Re-add a direct scan of apportionment-public.max.gov's own file tree
   (the pre-2026-09-04 approach) as a second, authoritative source -- but
   filename-diffed against a persisted manifest instead of re-fetching and
   re-parsing every file's content every run, which is what actually caused
   the original blowout. The front-page listing itself is one ~20MB HTML
   fetch either way; the fix is to only fetch+parse the individual JSON for
   paths NOT already in the manifest (normally a handful/day), not the
   whole multi-year tree. Scoped to the current and next fiscal year only
   (older years are closed out and never produce new filings).

Both sources key off the same identity -- the raw apportionment-public.
max.gov JSON path (openomb's own `sourceUrl` field IS this path) -- so
`data/omb-apportionment-manifest.json`'s `seen_paths` set is shared between
them: whichever source sees a given file first marks it seen, so the other
source (or a later, wider openomb window) never reprocesses or double-
reports it.

First-run bootstrap: if the manifest doesn't exist yet, every path found by
either source this run is recorded as seen WITHOUT being flagged/counted --
same principle as the original 2026-09-03 Spend Plan manifest bootstrap, so
the first real run doesn't dump years of backlog on the reader.
"""
import datetime as dt
import re
import sys

from dc_common import (
    check_reachable, days_ago_la, http_get, http_get_json, load_manifest,
    pdftotext, save_manifest, today_la, write_candidates, download,
)

SPEND_PLAN_MANIFEST_PATH = "data/omb-spend-plan-manifest.json"
APPORTIONMENT_MANIFEST_PATH = "data/omb-apportionment-manifest.json"
OUT_PATH = "data/omb_candidates.json"
MAX_GOV_BASE = "https://apportionment-public.max.gov"

OPENOMB_WINDOW_DAYS = 5  # was 1 -- widened to absorb openomb.org's ingestion lag

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
    low = (text or "").lower()
    for kw in FLAG_KEYWORDS:
        if kw in low:
            return kw
    for rationale in FLAG_RATIONALES:
        if rationale in low:
            return "oversight-hold rationale"
    return None


def _group_total(lines_by_group):
    """Given the ApprovedAmount-bearing lines for one (bureau, account, TAFS)
    grouping, pick the figure that actually represents the file's apportioned
    total for that grouping -- preferring the 'Total budgetary resources
    available' summary line (6190) over the pre-application-split summary
    (1920), over just taking the largest number seen (better than nothing if
    neither summary line is present)."""
    by_line_number = {}
    for ln in lines_by_group:
        by_line_number.setdefault(ln["line_number"], ln["amount"])
    for preferred in ("6190", "1920"):
        if preferred in by_line_number:
            return by_line_number[preferred]
    amounts = [ln["amount"] for ln in lines_by_group if ln["amount"]]
    return max(amounts) if amounts else 0


def summarize_non_flagged_file(agency, bureau_account_lines, fiscal_year,
                                approval_timestamp, source_url):
    """bureau_account_lines: list of (bureau, account, line_number, amount)
    tuples, one per ScheduleData/tafs.lines entry in the file. Groups by
    (bureau, account) and returns one entry per grouping -- most files have
    exactly one, but some (see FY2027 DOI/BOEM 2026-09-12) bundle several
    accounts under one bureau in a single approval."""
    groups = {}
    order = []
    for bureau, account, line_number, amount in bureau_account_lines:
        key = (bureau or "Unknown bureau", account or "Unknown account")
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append({"line_number": line_number, "amount": amount})
    entries = []
    for bureau, account in order:
        entries.append({
            "agency": agency,
            "bureau": bureau,
            "accountTitle": account,
            "approvedAmount": _group_total(groups[(bureau, account)]),
            "fiscalYear": fiscal_year,
            "approvalTimestamp": approval_timestamp,
            "sourceUrl": source_url,
        })
    return entries


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


def current_and_next_fiscal_years():
    """Federal FY starts Oct 1. Returns (current_fy, next_fy) as ints."""
    now = dt.datetime.now(dt.timezone.utc)
    current_fy = now.year + 1 if now.month >= 10 else now.year
    return current_fy, current_fy + 1


def record_flag(out, agency_counts, agency, approver_title, funds_provided_by,
                 fiscal_year, approval_timestamp, line_number, line_description,
                 approved_amount, footnote_text, matched_keyword, source_url):
    out["flagged"].append({
        "agency": agency,
        "approverTitle": approver_title,
        "fundsProvidedBy": funds_provided_by,
        "fiscalYear": fiscal_year,
        "approvalTimestamp": approval_timestamp,
        "lineNumber": line_number,
        "lineDescription": line_description,
        "approvedAmount": approved_amount,
        "footnoteText": footnote_text,
        "matchedKeyword": matched_keyword,
        "sourceUrl": source_url,
    })


def process_openomb_detail(detail, out, agency_counts, seen_paths):
    """Caller has already confirmed this file is new (path not in
    seen_paths) and NOT a bootstrap run before fetching `detail` -- both
    checks happen earlier so a not-yet-seen-but-already-known-irrelevant
    file never costs a detail fetch. Returns the path for the caller to add
    to seen_paths."""
    path = detail.get("sourceUrl") or detail.get("excelUrl")
    if not path:
        return None
    agency = detail.get("folder", "Unknown agency")
    file_flagged = False
    bureau_account_lines = []
    for tafs in detail.get("tafs", []) or []:
        bureau = tafs.get("budgetBureauTitle")
        account = tafs.get("accountTitle")
        for line in tafs.get("lines", []) or []:
            bureau_account_lines.append((
                bureau, account, line.get("lineNumber"), line.get("approvedAmount") or 0,
            ))
            for fn in line.get("footnotes", []) or []:
                reason = footnote_flag_reason(fn.get("footnoteText", ""))
                if reason:
                    file_flagged = True
                    record_flag(
                        out, agency_counts, agency, detail.get("approverTitle"),
                        detail.get("fundsProvidedBy"), detail.get("fiscalYear"),
                        detail.get("approvalTimestamp"), line.get("lineNumber"),
                        line.get("lineDescription"), line.get("approvedAmount"),
                        fn.get("footnoteText"), reason, path,
                    )
    if file_flagged:
        out["counts"]["flagged_files"] = out["counts"].get("flagged_files", 0) + 1
    else:
        agency_counts[agency] = agency_counts.get(agency, 0) + 1
        out["non_flagged_files"].extend(summarize_non_flagged_file(
            agency, bureau_account_lines, detail.get("fiscalYear"),
            detail.get("approvalTimestamp"), path,
        ))
    return path


def process_raw_json(data, path, out, agency_counts):
    """Caller only invokes this for non-bootstrap runs on genuinely new
    paths -- see main()."""
    agency = data.get("Folder", "Unknown agency")
    footnote_lookup = {
        fn.get("FootnoteNumber"): fn.get("FootnoteText")
        for fn in data.get("FootnoteData", []) or []
    }
    file_flagged = False
    bureau_account_lines = []
    for line in data.get("ScheduleData", []) or []:
        bureau_account_lines.append((
            line.get("BudgetBureauTitle"), line.get("AccountTitle"),
            line.get("LineNumber"), line.get("ApprovedAmount") or 0,
        ))
        fn_num = line.get("FootnoteNumber")
        if not fn_num:
            continue
        footnote_text = footnote_lookup.get(fn_num, "")
        reason = footnote_flag_reason(footnote_text)
        if reason:
            file_flagged = True
            record_flag(
                out, agency_counts, agency, data.get("ApproverTitle"),
                data.get("FundsProvidedBy"), data.get("FiscalYear"),
                data.get("ApprovalTimestamp"), line.get("LineNumber"),
                line.get("LineDescription"), line.get("ApprovedAmount"),
                footnote_text, reason, path,
            )
    if file_flagged:
        out["counts"]["flagged_files"] = out["counts"].get("flagged_files", 0) + 1
    else:
        agency_counts[agency] = agency_counts.get(agency, 0) + 1
        out["non_flagged_files"].extend(summarize_non_flagged_file(
            agency, bureau_account_lines, data.get("FiscalYear"),
            data.get("ApprovalTimestamp"), path,
        ))


def main():
    since = days_ago_la(OPENOMB_WINDOW_DAYS)
    until = today_la()
    reachable_omb = check_reachable("openomb.org")
    reachable_max = check_reachable("apportionment-public.max.gov")

    apportionment_manifest = load_manifest(APPORTIONMENT_MANIFEST_PATH)
    bootstrap = "seen_paths" not in apportionment_manifest
    seen_paths = set(apportionment_manifest.get("seen_paths", []))
    seen_before_run = len(seen_paths)

    out = {
        "window": {"since": since, "until": until, "window_days": OPENOMB_WINDOW_DAYS},
        "reachable": {"openomb.org": reachable_omb, "apportionment-public.max.gov": reachable_max},
        "flagged": [],
        "non_flagged_by_agency": {},
        "non_flagged_files": [],
        "spend_plans": {"new_or_changed": [], "checked": 0},
        "errors": [],
        "counts": {"source_openomb_new": 0, "source_raw_scan_new": 0},
    }
    agency_counts = {}

    # --- SOURCE A: openomb.org, widened window ---------------------------
    if reachable_omb:
        try:
            files = fetch_files_search(since, until)
            # The search result itself already carries sourceUrl -- dedupe
            # here, BEFORE the expensive per-file detail fetch, so an
            # already-seen file (common once the window is 5 days instead
            # of 1) never costs a second network call.
            new_files = [f for f in files if (f.get("sourceUrl") or f.get("excelUrl")) not in seen_paths]
            print(
                f"openomb.org search: {len(files)} files in {OPENOMB_WINDOW_DAYS}-day window, "
                f"{len(new_files)} not yet in manifest",
                file=sys.stderr,
            )
            for i, f in enumerate(new_files):
                path = f.get("sourceUrl") or f.get("excelUrl")
                if bootstrap:
                    seen_paths.add(path)  # seed-only: mark seen, no fetch, no flag
                    continue
                if i % 10 == 0:
                    print(f"  openomb detail {i}/{len(new_files)}", file=sys.stderr)
                file_id = f.get("fileId")
                try:
                    detail = http_get_json(f"https://openomb.org/api/v1/files/{file_id}", timeout=30)
                except Exception as e:  # noqa: BLE001
                    out["errors"].append(f"openomb file {file_id} fetch failed: {e}")
                    continue
                got_path = process_openomb_detail(detail, out, agency_counts, seen_paths)
                if got_path:
                    seen_paths.add(got_path)
            out["counts"]["source_openomb_new"] = len(new_files)
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"openomb.org search failed: {e}")
    else:
        out["errors"].append("openomb.org unreachable")

    # --- SOURCE B: raw apportionment-public.max.gov file tree ------------
    # Re-added 2026-09-13 as a second, authoritative source since openomb.org
    # lags the real site by several days. Manifest-diffed so only genuinely
    # new paths get individually fetched -- NOT a full re-scrape of every
    # file's content, which is what caused the original session-budget
    # blowout back when this ran inside Claude's own loop (it now runs here,
    # in plain Python, before Claude starts, so that concern doesn't carry
    # over the same way -- but the manifest diff is still the right design
    # regardless, since there's no reason to re-fetch thousands of unchanged
    # files' content every single run).
    page_html = None
    if reachable_max:
        try:
            status, body = http_get(MAX_GOV_BASE + "/", timeout=120)
            page_html = body.decode(errors="replace")
            cur_fy, next_fy = current_and_next_fiscal_years()
            fy_prefixes = tuple(
                f"/Fiscal%20Year%20{fy}/" for fy in (cur_fy - 1, cur_fy, next_fy)  # prior FY still gets revisions just after Oct 1
            )
            hrefs = sorted(set(
                h for h in re.findall(r'href="(/Fiscal%20Year%20\d+/[^"]+/JSON/[^"]+\.json)"', page_html)
                if h.startswith(fy_prefixes)
            ))
            new_paths = [h for h in hrefs if (MAX_GOV_BASE + h) not in seen_paths]
            print(
                f"raw max.gov scan: {len(hrefs)} JSON files in FY{cur_fy}/FY{next_fy}, "
                f"{len(new_paths)} not yet in manifest",
                file=sys.stderr,
            )
            if bootstrap:
                # Seed-only: the front-page listing already gives us every
                # path -- no reason to fetch each file's content just to
                # discard it. This is the case that would otherwise turn a
                # first run into thousands of individual HTTP requests.
                for href in new_paths:
                    seen_paths.add(MAX_GOV_BASE + href)
            else:
                for i, href in enumerate(new_paths):
                    if i % 25 == 0:
                        print(f"  raw-scan fetch {i}/{len(new_paths)}", file=sys.stderr)
                    url = MAX_GOV_BASE + href
                    try:
                        data = http_get_json(url, timeout=30)
                    except Exception as e:  # noqa: BLE001
                        out["errors"].append(f"raw-scan fetch failed for {href}: {e}")
                        continue
                    process_raw_json(data, url, out, agency_counts)
                    seen_paths.add(url)
            out["counts"]["source_raw_scan_new"] = len(new_paths)
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"raw max.gov apportionment scan failed: {e}")
    else:
        out["errors"].append("apportionment-public.max.gov unreachable")

    out["non_flagged_by_agency"] = agency_counts
    out["counts"].setdefault("flagged_files", 0)
    out["counts"]["manifest_size_before"] = seen_before_run
    out["counts"]["manifest_size_after"] = len(seen_paths)
    if bootstrap:
        out["counts"]["bootstrap_run"] = True
        out["flagged"] = []  # belt-and-suspenders: bootstrap never flags
        out["non_flagged_by_agency"] = {}
        out["non_flagged_files"] = []

    save_manifest(APPORTIONMENT_MANIFEST_PATH, {"seen_paths": sorted(seen_paths)})

    # --- Spend Plan PDFs (unchanged) --------------------------------------
    # HEAD-diff against manifest (Content-Length is the only reliable change
    # signal, confirmed by hand; see prompt history). Reuses page_html from
    # the raw scan above when available, instead of fetching the ~20MB front
    # page a second time.
    if reachable_max:
        try:
            if page_html is None:
                status, body = http_get(MAX_GOV_BASE + "/", timeout=120)
                page_html = body.decode(errors="replace")
            hrefs = sorted(set(re.findall(r'href="(/Spend%20Plans/[^"]+\.pdf)"', page_html)))
            manifest = load_manifest(SPEND_PLAN_MANIFEST_PATH)
            new_manifest = {}
            out["spend_plans"]["checked"] = len(hrefs)
            import urllib.request as ur
            for i, href in enumerate(hrefs):
                url = MAX_GOV_BASE + href
                if i % 25 == 0:
                    print(f"  spend plan HEAD check {i}/{len(hrefs)}", file=sys.stderr)
                req = ur.Request(url, method="HEAD")
                try:
                    with ur.urlopen(req, timeout=20) as resp:
                        content_length = int(resp.headers.get("Content-Length", 0))
                except Exception as e:  # noqa: BLE001
                    out["errors"].append(f"HEAD failed for {href}: {e}")
                    if href in manifest:
                        new_manifest[href] = manifest[href]  # else it would come back as NEW next run
                    continue
                prior = manifest.get(href, {}).get("content_length")
                new_manifest[href] = {"content_length": content_length}
                if prior is None:
                    status_label = "NEW"
                elif prior != content_length:
                    status_label = "CHANGED"
                else:
                    continue
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
                    "text": text[:20000],
                    "url": url,
                })
            save_manifest(SPEND_PLAN_MANIFEST_PATH, new_manifest)
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"Spend Plan check failed: {e}")

    write_candidates(OUT_PATH, out)
    print(
        f"OMB: {out['counts'].get('flagged_files', 0)} flagged, "
        f"{sum(out['non_flagged_by_agency'].values())} non-flagged "
        f"(openomb+{out['counts']['source_openomb_new']}, raw-scan+{out['counts']['source_raw_scan_new']}), "
        f"{len(out['spend_plans']['new_or_changed'])} new/changed spend plans, "
        f"manifest {out['counts']['manifest_size_before']}->{out['counts']['manifest_size_after']}, "
        f"{'BOOTSTRAP RUN, ' if bootstrap else ''}"
        f"{len(out['errors'])} errors",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()

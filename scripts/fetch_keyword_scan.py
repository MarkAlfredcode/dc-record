#!/usr/bin/env python3
"""Red-flag keyword sweep across every FRQ (RFAI) and F99 filing in the
window -- added 2026-09-14 alongside fetch_negative_amounts.py. Mechanizes
the download+pdftotext+grep pass that used to run as an uncapped Bash loop
inside Claude's own turn: the actual scanning needs no judgment (a literal
`grep -i` against a fixed keyword list), so it happens here at zero LLM
cost. Claude's Task 1 (in prompt-fec.txt) still does its own independent
fetch/read/paraphrase of these same filings for the newsletter's real RFAI/
Form 99 writeup -- this script is a SEPARATE, narrower pass whose only
output is "which filings (if any) hit a red-flag keyword," so a hit can be
guaranteed a real, full read regardless of Task 1's own ~20-item deep-read
cap.

Reads (doesn't write) data/fec-filings-seen.json -- Task 1 in the main
Claude pass still owns that manifest -- purely so this script doesn't
re-download and re-scan a filing already reported in a prior edition.

Output: data/keyword_scan_candidates.json
"""
import itertools
import os
import re
import sys
import tempfile

from dc_common import (
    check_reachable, days_ago_la, download, http_get_json, load_manifest,
    pdftotext, today_la, write_candidates,
)

OUT_PATH = "data/keyword_scan_candidates.json"
SEEN_MANIFEST_PATH = "data/fec-filings-seen.json"  # owned by Task 1 (main pass); read-only here

# Keep in sync by hand with fetch_negative_amounts.py's KEYWORD_PATTERN.
# NOTE: "forg" alone false-positives on "Loan Forgiveness" -- a routine FEC
# Form 99 subtype prompt-fec.txt's Task 1 already documents as boilerplate
# (confirmed live 2026-09-14: ERBECK FOR OHIO's Form 99 flagged on exactly
# this collision before the fix below). Require the forgery-specific
# suffix instead of the bare substring.
KEYWORD_PATTERN = (
    r"fraud|fraudulent|embezzl|misappropriat|theft|steal|stolen"
    r"|forg(?:e|ed|ery)|unauthorized use|restitution|indictment|subpoena"
    r"|grand jury|kickback|bribe|bribery|money launder|criminal referral"
    r"|department of justice|whistleblower|ponzi|scheme"
)
KEYWORD_RE = re.compile(KEYWORD_PATTERN, re.IGNORECASE)


def fec_keys():
    raw = os.environ.get("FEC_KEYS", "")
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        raise RuntimeError("FEC_KEYS is empty/unset")
    return itertools.cycle(keys)


def fetch_all(form_type, lookback, key_cycle):
    results = []
    page = 1
    while True:
        api_key = next(key_cycle)
        url = (
            "https://api.open.fec.gov/v1/filings/"
            f"?form_type={form_type}&sort=-receipt_date&min_receipt_date={lookback}"
            f"&per_page=100&page={page}&api_key={api_key}"
        )
        data = http_get_json(url, timeout=30)
        batch = data.get("results", [])
        results.extend(batch)
        pagination = data.get("pagination") or {}
        if page >= pagination.get("pages", 1) or not batch:
            break
        page += 1
    return results


def context_snippet(text, match_start, match_end, radius=120):
    start = max(0, match_start - radius)
    end = min(len(text), match_end + radius)
    snippet = text[start:end].replace("\n", " ")
    snippet = re.sub(r"\s+", " ", snippet).strip()
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{snippet}{suffix}"


def main():
    lookback = days_ago_la(3)
    today = today_la()
    reachable = check_reachable("api.open.fec.gov")
    out = {
        "window_start": lookback,
        "reachable": reachable,
        "scanned_count": 0,
        "flagged": [],
        "errors": [],
    }
    if not reachable:
        out["errors"].append("api.open.fec.gov unreachable")
        write_candidates(OUT_PATH, out)
        return

    seen = load_manifest(SEEN_MANIFEST_PATH)
    key_cycle = fec_keys()

    filings = []
    for form_type in ("FRQ", "F99"):
        try:
            filings.extend(
                (form_type, r) for r in fetch_all(form_type, lookback, key_cycle)
            )
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"{form_type} fetch failed: {e}")

    # Same dedupe key Task 1 uses (file_number as a string) so a filing
    # already reported in a prior edition (only reappearing because of the
    # 3-day LOOKBACK window) doesn't get re-downloaded/re-scanned here.
    to_scan = [
        (ft, r) for ft, r in filings
        if str(r.get("file_number")) not in seen
    ]
    print(f"keyword-scan: {len(to_scan)} filings to scan of {len(filings)} in window", file=sys.stderr)

    with tempfile.TemporaryDirectory() as tmpdir:
        for form_type, r in to_scan:
            pdf_url = r.get("pdf_url")
            file_number = r.get("file_number")
            if not pdf_url:
                continue
            try:
                local_pdf = os.path.join(tmpdir, f"{file_number}.pdf")
                download(pdf_url, local_pdf, timeout=45)
                text = pdftotext(local_pdf)
            except Exception as e:  # noqa: BLE001
                out["errors"].append(f"file_number={file_number} download/extract failed: {e}")
                continue
            out["scanned_count"] += 1
            if not text:
                continue
            matches = list(KEYWORD_RE.finditer(text))
            if not matches:
                continue
            matched_terms = sorted({m.group(0).lower() for m in matches})
            # One context snippet per unique matched term (not per raw
            # occurrence -- a repeated boilerplate term shouldn't produce
            # ten near-identical snippets).
            seen_terms = set()
            snippets = []
            for m in matches:
                term = m.group(0).lower()
                if term in seen_terms:
                    continue
                seen_terms.add(term)
                snippets.append(f"[{term}] {context_snippet(text, m.start(), m.end())}")
            out["flagged"].append({
                "file_number": file_number,
                "form_type": form_type,
                "committee_id": r.get("committee_id"),
                "committee_name": r.get("committee_name"),
                "document_description": r.get("document_description"),
                "office": r.get("office"),
                "receipt_date": r.get("receipt_date"),
                "pdf_url": pdf_url,
                "matched_keywords": matched_terms,
                "context_snippets": snippets,
            })

    print(f"keyword-scan: {out['scanned_count']} scanned, {len(out['flagged'])} flagged", file=sys.stderr)
    write_candidates(OUT_PATH, out)


if __name__ == "__main__":
    main()

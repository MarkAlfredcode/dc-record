#!/usr/bin/env python3
"""Task 1B (Voided & Negative-Amount Transactions) fetch/filter. Mechanizes
the entire sweep -- this is FEC Schedule A/B itemized-transaction data (a
source the FEC edition didn't touch before 2026-09-14), filtered to a literal
threshold (`amount <= -10000`), no judgment involved. Claude's only remaining
job (in the cheap-model mechanical pass) is writing the section's headline/
summary sentence and, for any hit whose description/memo text matches a
red-flag keyword, saying so plainly.

Same LOOKBACK + persisted-manifest dedupe pattern as every other FEC-edition
task (see prompt-fec.txt Step 0 and Task 1's DEDUPE note) -- FEC's own
indexing can lag a couple days, so the window has no upper bound and repeat
appearances are suppressed by the manifest instead of the window itself.

Output: data/negative_amounts_candidates.json
Side effect: rewrites data/negative-amounts-seen.json.
"""
import itertools
import os
import re
import sys

from dc_common import (
    check_reachable, days_ago_la, http_get_json, load_manifest,
    save_manifest, today_la, write_candidates,
)

OUT_PATH = "data/negative_amounts_candidates.json"
MANIFEST_PATH = "data/negative-amounts-seen.json"
THRESHOLD = -10000  # "$10,000 or more negative" -- catches five-figure AND larger voids
PAGE_CAP = 5  # safety valve; observed real volume is single digits per window, this is 10-20x headroom

# Same red-flag pattern as Task 1's keyword scan (fetch_keyword_scan.py) --
# duplicated here (not imported) so this script has no import-order
# dependency on that one; keep the two patterns in sync by hand if either
# changes. NOTE: "forg" alone false-positives on "Loan Forgiveness" -- a
# routine FEC Form 99 subtype -- hence the forgery-specific suffix instead
# of the bare substring (confirmed live 2026-09-14, see fetch_keyword_scan.py).
KEYWORD_PATTERN = (
    r"fraud|fraudulent|embezzl|misappropriat|theft|steal|stolen"
    r"|forg(?:e|ed|ery)|unauthorized use|restitution|indictment|subpoena"
    r"|grand jury|kickback|bribe|bribery|money launder|criminal referral"
    r"|department of justice|whistleblower|ponzi|scheme"
)
KEYWORD_RE = re.compile(KEYWORD_PATTERN, re.IGNORECASE)


def matched_keywords(text):
    return sorted({m.group(0).lower() for m in KEYWORD_RE.finditer(text or "")})


def two_year_period(today):
    year = int(today[:4])
    return year if year % 2 == 0 else year + 1


def fec_keys():
    raw = os.environ.get("FEC_KEYS", "")
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        raise RuntimeError("FEC_KEYS is empty/unset")
    return itertools.cycle(keys)


def paginate(base_url, key_cycle, cursor_fields):
    """Yields result dicts across pages, following FEC's cursor-based
    `pagination.last_indexes` pattern (not `page=`, which these endpoints
    don't support). Stops after PAGE_CAP pages as a safety valve."""
    params = {}
    for _ in range(PAGE_CAP):
        api_key = next(key_cycle)
        url = base_url + "&api_key=" + api_key
        for k, v in params.items():
            url += f"&{k}={v}"
        data = http_get_json(url, timeout=30)
        results = data.get("results", [])
        for r in results:
            yield r
        pagination = data.get("pagination") or {}
        last_indexes = pagination.get("last_indexes")
        if not results or not last_indexes:
            break
        params = {k: last_indexes[k] for k in cursor_fields if k in last_indexes}
        if not params:
            break


def main():
    lookback = days_ago_la(3)
    today = today_la()
    reachable = check_reachable("api.open.fec.gov")
    out = {
        "window_start": lookback,
        "threshold": THRESHOLD,
        "reachable": reachable,
        "rows": [],
        "errors": [],
        "counts": {},
    }
    if not reachable:
        out["errors"].append("api.open.fec.gov unreachable")
        write_candidates(OUT_PATH, out)
        return

    key_cycle = fec_keys()
    manifest = load_manifest(MANIFEST_PATH)
    all_rows = []

    # Schedule B: disbursements. No two_year_transaction_period requirement
    # (confirmed live 2026-09-14).
    try:
        b_url = (
            "https://api.open.fec.gov/v1/schedules/schedule_b/"
            f"?min_date={lookback}&max_amount={THRESHOLD}&sort=disbursement_date&per_page=100"
        )
        for r in paginate(b_url, key_cycle, ["last_disbursement_date", "last_index"]):
            all_rows.append({
                "schedule": "B",
                "key": f"schedule_b:{r.get('image_number')}",
                "committee_name": (r.get("committee") or {}).get("name"),
                "committee_id": r.get("committee_id"),
                "counterparty": r.get("recipient_name"),
                "amount": r.get("disbursement_amount"),
                "date": r.get("disbursement_date"),
                "description": r.get("disbursement_description"),
                "pdf_url": r.get("pdf_url"),
                "matched_keywords": matched_keywords(r.get("disbursement_description")),
            })
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"schedule_b fetch failed: {e}")

    # Schedule A: receipts. 400s without two_year_transaction_period
    # (confirmed live 2026-09-14) -- always pass it.
    try:
        period = two_year_period(today)
        a_url = (
            "https://api.open.fec.gov/v1/schedules/schedule_a/"
            f"?min_date={lookback}&max_amount={THRESHOLD}&two_year_transaction_period={period}"
            "&sort=contribution_receipt_date&per_page=100"
        )
        for r in paginate(a_url, key_cycle, ["last_contribution_receipt_date", "last_index"]):
            all_rows.append({
                "schedule": "A",
                "key": f"schedule_a:{r.get('image_number')}",
                "committee_name": (r.get("committee") or {}).get("name"),
                "committee_id": r.get("committee_id"),
                "counterparty": r.get("contributor_name"),
                "amount": r.get("contribution_receipt_amount"),
                "date": r.get("contribution_receipt_date"),
                "description": r.get("memo_text") or r.get("receipt_type_desc"),
                "pdf_url": r.get("pdf_url"),
                "matched_keywords": matched_keywords(r.get("memo_text")) + matched_keywords(r.get("receipt_type_desc")),
            })
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"schedule_a fetch failed: {e}")

    print(f"negative-amounts: {len(all_rows)} rows in {lookback}..{today} window (threshold {THRESHOLD})", file=sys.stderr)

    new_rows = [r for r in all_rows if r["key"] not in manifest]
    print(f"negative-amounts: {len(new_rows)} new-to-report of {len(all_rows)}", file=sys.stderr)

    for r in new_rows:
        r["matched_keywords"] = sorted(set(r["matched_keywords"]))
        manifest[r["key"]] = {"reported": today}

    cutoff = days_ago_la(14)
    manifest = {k: v for k, v in manifest.items() if v.get("reported", today) >= cutoff}
    save_manifest(MANIFEST_PATH, manifest)

    out["rows"] = new_rows
    out["counts"]["total_in_window"] = len(all_rows)
    out["counts"]["new_to_report"] = len(new_rows)
    write_candidates(OUT_PATH, out)


if __name__ == "__main__":
    main()

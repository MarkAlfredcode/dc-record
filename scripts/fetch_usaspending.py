#!/usr/bin/env python3
"""Task 9 (USAspending Contracts) fetch/filter. Mechanizes: pagination,
Mod=="0" filtering, and manifest-based novelty dedup. Claude still decides
which new awards get a full paragraph vs. a one-line mention and writes the
actual English description -- that's real judgment/writing, not scriptable.

Output: data/usaspending_candidates.json
Side effect: rewrites data/usaspending-contracts-manifest.json.
"""
import sys

from dc_common import (
    check_reachable, days_ago_la, http_post_json, http_get_json,
    load_manifest, save_manifest, today_la, write_candidates,
)

MANIFEST_PATH = "data/usaspending-contracts-manifest.json"
OUT_PATH = "data/usaspending_candidates.json"
BASE = "https://api.usaspending.gov"


def main():
    today = today_la()
    window_start = days_ago_la(25)
    reachable = check_reachable("api.usaspending.gov")

    out = {
        "window": {"start": window_start, "end": today},
        "reachable": reachable,
        "new_awards": [],
        "errors": [],
        "counts": {},
    }

    if not reachable:
        out["errors"].append("api.usaspending.gov unreachable")
        write_candidates(OUT_PATH, out)
        return

    manifest = load_manifest(MANIFEST_PATH)
    new_rows = []
    page = 1
    total_pages = 0
    while True:
        payload = {
            "filters": {
                "award_type_codes": ["A", "B", "C", "D"],
                "time_period": [{"start_date": window_start, "end_date": today, "date_type": "action_date"}],
                "award_amounts": [{"lower_bound": 2000000}],
            },
            "fields": [
                "Award ID", "Recipient Name", "Transaction Amount", "Awarding Agency",
                "Awarding Sub Agency", "Transaction Description", "Action Date", "Mod",
                "product_or_service_description", "naics_description",
            ],
            "page": page, "limit": 100, "sort": "Transaction Amount", "order": "desc",
        }
        try:
            status, data = http_post_json(f"{BASE}/api/v2/search/spending_by_transaction/", payload, timeout=30)
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"page {page} fetch failed: {e}")
            break
        if status >= 400:
            out["errors"].append(f"page {page} -> HTTP {status}")
            break
        results = data.get("results", [])
        if page % 10 == 0 or page == 1:
            print(f"  USAspending page {page}, {len(results)} rows", file=sys.stderr)
        for row in results:
            if row.get("Mod") != "0":
                continue
            internal_id = str(row.get("internal_id"))
            if internal_id in manifest:
                continue
            new_rows.append(row)
            manifest[internal_id] = {
                "action_date": row.get("Action Date"),
                "award_id": row.get("Award ID"),
                "recipient": row.get("Recipient Name"),
            }
        total_pages = page
        if not data.get("page_metadata", {}).get("hasNext"):
            break
        page += 1

    out["counts"]["total_pages"] = total_pages
    out["counts"]["new_count"] = len(new_rows)
    print(f"USAspending: {total_pages} pages, {len(new_rows)} new-to-report awards", file=sys.stderr)

    # Fetch full description for every new-to-report award (Claude decides
    # full-paragraph vs. one-line treatment, but needs real text either way)
    for i, row in enumerate(new_rows):
        gen_id = row.get("generated_internal_id")
        detail = {}
        if gen_id:
            try:
                detail = http_get_json(f"{BASE}/api/v2/awards/{gen_id}/", timeout=30)
            except Exception as e:  # noqa: BLE001
                out["errors"].append(f"award detail failed for {gen_id}: {e}")
        if i % 10 == 0:
            print(f"  award detail {i}/{len(new_rows)}", file=sys.stderr)
        out["new_awards"].append({
            "internal_id": row.get("internal_id"),
            "generated_internal_id": gen_id,
            "award_id": row.get("Award ID"),
            "recipient": row.get("Recipient Name"),
            "transaction_amount": row.get("Transaction Amount"),
            "awarding_agency": row.get("Awarding Agency"),
            "awarding_sub_agency": row.get("Awarding Sub Agency"),
            "action_date": row.get("Action Date"),
            "naics_description": row.get("naics_description"),
            "product_or_service_description": row.get("product_or_service_description"),
            "raw_description": detail.get("description") or row.get("Transaction Description"),
            "url": f"https://www.usaspending.gov/award/{gen_id}" if gen_id else None,
        })

    # Prune manifest entries older than 30 days
    pruned = {}
    for k, v in manifest.items():
        try:
            age_ok = v.get("action_date", "") >= days_ago_la(30)
        except Exception:  # noqa: BLE001
            age_ok = True
        if age_ok:
            pruned[k] = v
    save_manifest(MANIFEST_PATH, pruned)

    write_candidates(OUT_PATH, out)


if __name__ == "__main__":
    main()

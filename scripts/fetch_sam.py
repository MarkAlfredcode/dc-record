#!/usr/bin/env python3
"""Task 10 (Federal Contracting Actions / SAM.gov) fetch. Mechanizes
pagination and description-fetching for justifications (all of which get
full treatment per the existing rule -- small volume). Claude still judges
which solicitations are flagged (agency/subject notability is a real
editorial call) -- this script just removes the fetch chore and hands over
every solicitation so nothing gets missed.

Output: data/sam_candidates.json
"""
import os
import sys

from dc_common import check_reachable, days_ago_la, http_get_json, today_la, write_candidates

OUT_PATH = "data/sam_candidates.json"
BASE = "https://api.sam.gov/opportunities/v2/search"


def paginate(api_key, ptype, posted_from, posted_to):
    offset = 0
    limit = 1000
    all_rows = []
    total = None
    while True:
        url = (
            f"{BASE}?api_key={api_key}&ptype={ptype}&postedFrom={posted_from}"
            f"&postedTo={posted_to}&limit={limit}&offset={offset}"
        )
        try:
            data = http_get_json(url, timeout=30)
        except Exception as e:  # noqa: BLE001
            return all_rows, total, str(e)
        total = data.get("totalRecords", 0)
        rows = data.get("opportunitiesData", [])
        all_rows.extend(rows)
        offset += limit
        if offset >= total or not rows:
            break
    return all_rows, total, None


def main():
    api_key = os.environ.get("SAM_API_KEY", "")
    posted_from = days_ago_la(1)
    posted_to = today_la()
    # MM/dd/yyyy format required by this API
    def fmt(d):
        y, m, day = d.split("-")
        return f"{m}/{day}/{y}"
    posted_from_fmt, posted_to_fmt = fmt(posted_from), fmt(posted_to)

    reachable = check_reachable("api.sam.gov")
    out = {
        "window": {"from": posted_from, "to": posted_to},
        "reachable": reachable,
        "justifications": [],
        "solicitations": [],
        "errors": [],
        "counts": {},
    }
    if not reachable:
        out["errors"].append("api.sam.gov unreachable")
        write_candidates(OUT_PATH, out)
        return
    if not api_key:
        out["errors"].append("SAM_API_KEY not set")
        write_candidates(OUT_PATH, out)
        return

    # Justifications (ptype=u) -- report every one
    rows, total, err = paginate(api_key, "u", posted_from_fmt, posted_to_fmt)
    if err:
        out["errors"].append(f"justifications fetch: {err}")
    print(f"SAM justifications: {len(rows)} of {total}", file=sys.stderr)
    for i, r in enumerate(rows):
        if i % 10 == 0:
            print(f"  justification detail {i}/{len(rows)}", file=sys.stderr)
        desc_text = None
        desc_url = r.get("description")
        if desc_url:
            try:
                sep = "&" if "?" in desc_url else "?"
                dd = http_get_json(f"{desc_url}{sep}api_key={api_key}", timeout=20)
                desc_text = dd.get("description") if isinstance(dd, dict) else None
            except Exception as e:  # noqa: BLE001
                out["errors"].append(f"justification desc fetch failed for {r.get('noticeId')}: {e}")
        out["justifications"].append({
            "title": r.get("title"),
            "agency": r.get("fullParentPathName"),
            "solicitationNumber": r.get("solicitationNumber"),
            "postedDate": r.get("postedDate"),
            "naicsCode": r.get("naicsCode"),
            "classificationCode": r.get("classificationCode"),
            "pointOfContact": r.get("pointOfContact"),
            "uiLink": r.get("uiLink"),
            "description_text": desc_text,
        })
    out["counts"]["justifications_total"] = total

    # Solicitations (ptype=o,k,p) -- hand over ALL, Claude flags by judgment
    rows, total, err = paginate(api_key, "o,k,p", posted_from_fmt, posted_to_fmt)
    if err:
        out["errors"].append(f"solicitations fetch: {err}")
    print(f"SAM solicitations: {len(rows)} of {total}", file=sys.stderr)
    for r in rows:
        out["solicitations"].append({
            "title": r.get("title"),
            "agency": r.get("fullParentPathName"),
            "solicitationNumber": r.get("solicitationNumber"),
            "postedDate": r.get("postedDate"),
            "naicsCode": r.get("naicsCode"),
            "classificationCode": r.get("classificationCode"),
            "uiLink": r.get("uiLink"),
            "noticeType": r.get("type"),
        })
    out["counts"]["solicitations_total"] = total
    out["counts"]["solicitations_fetched"] = len(rows)

    write_candidates(OUT_PATH, out)


if __name__ == "__main__":
    main()

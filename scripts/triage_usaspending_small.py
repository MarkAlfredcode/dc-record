#!/usr/bin/env python3
"""Cheap-model screen of USAspending contract awards UNDER $2 million.

fetch_usaspending.py only pulls $2M+ awards, which leaves ~1,200 new
awards a day in the $100K-$2M band unread -- and small awards are where
a lot of the stories are (a $900K no-bid deal for a White House vendor, a
$1.5M detention-services contract, a tiny shell company with a first
federal award). This script pulls them, hands the search-row text
(recipient, agency, amount, description, product/service) to Haiku in
batches with no tools, and keeps only what Haiku calls priority/notable
plus a regex safety net for the standing priority subjects. The routine
remainder is reduced to per-agency counts.

2026-10-11: base awards come from spending_by_award (new_awards_only), not
a transaction pull; the recent window is 7 days, not 3 (agencies report
late -- the 3-day window saw 41 of 46 of 10/6's awards); and a Defense
backlog sweep picks up Pentagon awards as USAspending publishes them ~90
days after signing (~270 a business day in this band). Both sweeps are in
usaspending_common.py. AM only, like fetch_usaspending.py.

Floor: SMALL_FLOOR env var (default $100,000). Below that it's ~10K+
awards a day, mostly micro-purchases; raise the volume at your own cost.

Output: data/usaspending_small_triage.json
Side effect: rewrites data/usaspending-small-manifest.json (novelty dedupe;
the workflow commits it only when the email actually went out).
"""
import os
import sys
from collections import Counter

import usaspending_common as uc
from dc_common import check_reachable, load_manifest, save_manifest, today_la, write_candidates

MANIFEST_PATH = "data/usaspending-small-manifest.json"
OUT_PATH = "data/usaspending_small_triage.json"
CEILING = 1_999_999  # the API rejects fractional bounds
FLOOR = int(os.environ.get("SMALL_FLOOR", "100000"))
WINDOW_DAYS = 7        # base-award-date lookback; the manifest dedupes the overlap
# Defense publishes ~91 days after signing; this band is ~270 awards per
# business day, so the backlog window is kept tighter than the $2M+ one.
DEFENSE_DAYS = 100
MANIFEST_DAYS = 10
MAX_PAGES = 80         # per sweep; 100 rows/page
MAX_FLAGGED = 60       # priority first, then by dollar amount; the rest are counted, not listed


def main():
    today = today_la()
    if (os.environ.get("MODE") or "am").strip().lower() == "pm" and not os.environ.get("DC_USA_FORCE"):
        write_candidates(OUT_PATH, {"skipped": "pm", "reachable": True, "flagged": [], "errors": [],
                                    "counts": {"new_awards": 0, "batches": 0, "screened_ok": 0}})
        return
    sweeps = uc.sweeps(today, WINDOW_DAYS, DEFENSE_DAYS)
    out = {
        "window": {s[0]: {"start": s[1], "end": s[2]} for s in sweeps},
        "floor_usd": FLOOR, "ceiling_usd": 2_000_000, "model": uc.SCREEN_MODEL,
        "reachable": check_reachable("api.usaspending.gov"),
        "flagged": [], "routine_counts_by_agency": {}, "counts": {}, "errors": [],
    }
    if not out["reachable"]:
        out["errors"].append("api.usaspending.gov unreachable")
        write_candidates(OUT_PATH, out)
        return

    manifest = load_manifest(MANIFEST_PATH)
    meta = manifest.get("_meta") or {"action_date": "9999-12-31"}
    found = []
    for label, start, end, agencies in sweeps:
        found += [uc.award_record(r, label) for r in
                  uc.search_awards(start, end, FLOOR, CEILING, agencies, MAX_PAGES, out["errors"], f"{label} <$2M")]
    seed = None
    if not meta.get("defense_backlog_since"):  # first run: record the backlog, report only its newest days
        seed = uc.backlog_seed(uc.defense_seed_cutoff(found))
        meta["defense_backlog_since"] = today
    rows, seeded = uc.take_new(found, manifest, uc.legacy_seen_ids(manifest), seed)
    print(f"USAspending <$2M: {len(rows)} new awards (floor ${FLOOR:,}; "
          f"{sum(1 for r in rows if r['sweep'] == 'defense_backlog')} from the Defense backlog)", file=sys.stderr)

    tiers, unscreened, stats, errors = uc.screen(rows, "newly reported federal contract awards, each under $2 million")
    out["errors"] += errors

    order = {"priority": 0, "unscreened": 1, "notable": 2, "keyword": 3}
    keep = []
    for i, r in enumerate(rows):
        if i in tiers:
            keep.append({**r, "triage_tier": tiers[i]["tier"], "triage_why": tiers[i]["why"]})
        elif i in unscreened:
            keep.append({**r, "triage_tier": "unscreened", "triage_why": "screening batch failed"})
    keep.sort(key=lambda r: (order[r["triage_tier"]], -(r.get("transaction_amount") or 0)))

    out["flagged_overflow"] = max(0, len(keep) - MAX_FLAGGED)
    out["flagged"] = keep[:MAX_FLAGGED]
    out["routine_counts_by_agency"] = dict(Counter(
        uc.short_agency(r.get("awarding_agency")) for i, r in enumerate(rows)
        if i not in tiers and i not in unscreened).most_common())
    out["executive_order_citations"] = uc.eo_citations(rows)
    out["counts"] = {
        "new_awards": len(rows),
        "new_defense_backlog": sum(1 for r in rows if r["sweep"] == "defense_backlog"),
        "seeded_not_reported": seeded,
        "batches": stats["batches"], "screened_ok": stats["screened_ok"],
        "priority": sum(1 for r in keep if r["triage_tier"] == "priority"),
        "notable": sum(1 for r in keep if r["triage_tier"] == "notable"),
        "keyword": sum(1 for r in keep if r["triage_tier"] == "keyword"),
        "unscreened": sum(1 for r in keep if r["triage_tier"] == "unscreened"),
        "haiku_cost_usd": stats["haiku_cost_usd"],
    }
    print(f"USAspending <$2M triage: {out['counts']}", file=sys.stderr)

    manifest["_meta"] = meta
    save_manifest(MANIFEST_PATH, uc.prune_manifest(manifest, MANIFEST_DAYS, DEFENSE_DAYS))
    write_candidates(OUT_PATH, out)


if __name__ == "__main__":
    main()

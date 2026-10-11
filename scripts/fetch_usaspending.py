#!/usr/bin/env python3
"""Task 9 (USAspending Contracts) fetch/filter: new base contract awards of
$2M+, plus large modifications (+$25M and up) and cuts (-$5M and down).
Claude still decides what's notable and writes the English; the cheap-model
screen (triage_usaspending.py) sorts the pile first.

Rebuilt 2026-10-11 (see usaspending_common.py for the why):
- Defense backlog sweep: Pentagon awards appear ~90 days after signing, so
  the old 25-day window never held a single one. They're reported when
  USAspending publishes them, flagged `sweep: "defense_backlog"`.
- spending_by_award (base awards only, description inline) instead of ~102
  pages of transactions plus a detail fetch per award.
- Modifications were dropped entirely before (Mod != "0"); 143 of +$25M and
  31 cuts of -$5M or more in one 25-day window.
- AM only: USAspending loads once a day, and website-only PM runs revert the
  manifest anyway (so whatever PM found was re-reported the next morning).
  PM writes a `skipped: "pm"` stub and assemble.py carries the morning's
  section over. DC_USA_FORCE=1 runs it in PM anyway.

Output: data/usaspending_candidates.json
Side effect: rewrites data/usaspending-contracts-manifest.json.
"""
import os
import sys

from dc_common import check_reachable, days_ago_la, load_manifest, save_manifest, today_la, write_candidates
import usaspending_common as uc

MANIFEST_PATH = "data/usaspending-contracts-manifest.json"
OUT_PATH = "data/usaspending_candidates.json"
FLOOR = 2_000_000
MOD_INCREASE_FLOOR = 25_000_000
MOD_CUT_FLOOR = 5_000_000
MANIFEST_DAYS = 30
FIRST_RUN_MOD_DAYS = 5


def pm_skip():
    return (os.environ.get("MODE") or "am").strip().lower() == "pm" and not os.environ.get("DC_USA_FORCE")


def main():
    today = today_la()
    if pm_skip():
        write_candidates(OUT_PATH, {
            "skipped": "pm", "reachable": True, "errors": [], "new_awards": [], "modifications": [], "cuts": [],
            "note": "USAspending is pulled once a day, in the AM edition; this PM edition carries the morning's section over.",
        })
        return

    reachable = check_reachable("api.usaspending.gov")
    sweeps = uc.sweeps(today)
    out = {
        "window": {s[0]: {"start": s[1], "end": s[2]} for s in sweeps},
        "reachable": reachable,
        "thresholds": {"new_award_floor": FLOOR, "modification_increase_floor": MOD_INCREASE_FLOOR,
                       "cut_floor": -MOD_CUT_FLOOR},
        "new_awards": [], "modifications": [], "cuts": [],
        "errors": [], "counts": {},
    }
    if not reachable:
        out["errors"].append("api.usaspending.gov unreachable")
        write_candidates(OUT_PATH, out)
        return

    manifest = load_manifest(MANIFEST_PATH)
    meta = manifest.get("_meta") or {"action_date": "9999-12-31"}
    legacy = uc.legacy_seen_ids(manifest)

    awards, mods, cuts = [], [], []
    for label, start, end, agencies in sweeps:
        awards += [uc.award_record(r, label) for r in
                   uc.search_awards(start, end, FLOOR, agencies=agencies, errors=out["errors"], label=f"{label} awards")]
        mods += [uc.modification_record(r, label) for r in
                 uc.search_modifications(start, end, MOD_INCREASE_FLOOR, True, agencies, errors=out["errors"],
                                         label=f"{label} increases")]
        cuts += [uc.modification_record(r, label) for r in
                 uc.search_modifications(start, end, MOD_CUT_FLOOR, False, agencies, errors=out["errors"],
                                         label=f"{label} cuts")]

    # First run of each new sweep: record the backlog, report only its newest days.
    award_seed = mod_seed = None
    if not meta.get("defense_backlog_since"):
        seed_before = uc.defense_seed_cutoff(awards)
        award_seed = uc.backlog_seed(seed_before)
        meta["defense_backlog_since"] = today
        out["counts"]["defense_backlog_first_run_reported_from"] = seed_before
    if not meta.get("modifications_since"):
        # Modifications were never pulled before: report the last few days', not the whole window's.
        mods_from = days_ago_la(FIRST_RUN_MOD_DAYS)
        mod_seed = lambda r: (r.get("action_date") or "") < mods_from if r["sweep"] == "recent" else (  # noqa: E731
            award_seed(r) if award_seed else False)
        meta["modifications_since"] = today
    new_awards, seeded_a = uc.take_new(awards, manifest, legacy, award_seed)
    new_mods, seeded_m = uc.take_new(mods, manifest, legacy, mod_seed)
    new_cuts, seeded_c = uc.take_new(cuts, manifest, legacy, mod_seed)

    out["new_awards"] = sorted(new_awards, key=lambda r: -(r.get("transaction_amount") or 0))
    out["modifications"] = sorted(new_mods, key=lambda r: -(r.get("transaction_amount") or 0))
    out["cuts"] = sorted(new_cuts, key=lambda r: r.get("transaction_amount") or 0)
    out["counts"].update({
        "new_count": len(new_awards),
        "new_defense_backlog": sum(1 for r in new_awards if r["sweep"] == "defense_backlog"),
        "new_modifications": len(new_mods),
        "new_cuts": len(new_cuts),
        "seeded_not_reported": seeded_a + seeded_m + seeded_c,
    })
    print(f"USAspending: {len(new_awards)} new awards ({out['counts']['new_defense_backlog']} from the Defense backlog), "
          f"{len(new_mods)} modifications, {len(new_cuts)} cuts", file=sys.stderr)

    manifest["_meta"] = meta
    save_manifest(MANIFEST_PATH, uc.prune_manifest(manifest, MANIFEST_DAYS))
    write_candidates(OUT_PATH, out)


if __name__ == "__main__":
    main()

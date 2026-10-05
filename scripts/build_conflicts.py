#!/usr/bin/env python3
"""Builds content-TODAY.json for the web-only Conflicts edition (added
2026-10-04). No LLM: the scanners (fetch_donor_awards.py,
fetch_pfd_analysis.py -> trade_awards.py) each write
extras/conflicts/<name>.json; this script

1. archives this run's sections to archive/conflicts/<date>-<am|pm>.json
   (a re-run of the same slot that finds nothing new never overwrites a
   fuller archive of that slot -- the scanners dedupe against seen-manifests,
   so a second pass is usually empty),
2. rebuilds today's view from every archive dated today (AM + PM) and a
   14-day "Earlier" log from older ones, and
3. ranks people and companies that appear in more than one scanner over the
   last 60 days (data/conflict-signals.json) -- the cross-signal view no
   single run can produce.

Usage: build_conflicts.py content-TODAY.json
"""
import glob
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime

from conflict_signals import KIND_LABELS, Signals
from dc_common import LA_TZ, days_ago_la, today_la
from dc_extras import load_extras, run_mode

ORDER = ["Donor Awards", "Trades Near Contract Awards", "Disclosure Red Flags"]
EARLIER_DAYS = 14
ROLLUP_DAYS = 60
SCANNERS = {
    "donor_awards": "Donor Awards",
    "trade_awards": "Trades Near Contract Awards",
    "pfd_analysis": "Disclosure Red Flags",
}


def load_archives():
    out = []
    cutoff = days_ago_la(EARLIER_DAYS)
    for path in sorted(glob.glob("archive/conflicts/*.json")):
        name = os.path.basename(path)[:-5]
        day, _, mode = name.rpartition("-")
        if day < cutoff:
            continue
        try:
            with open(path) as f:
                out.append({"date": day, "mode": mode, **json.load(f)})
        except (OSError, ValueError):
            continue
    return out


def sort_sections(sections):
    return sorted(sections, key=lambda s: ORDER.index(s["eyebrow"]) if s["eyebrow"] in ORDER else len(ORDER))


def day_view(runs):
    """[{eyebrow, runs: [{mode, headline, paragraphs, blocks}]}] for one day's archives."""
    by_eyebrow = defaultdict(list)
    for r in sorted(runs, key=lambda r: r["mode"]):
        for s in r.get("sections", []):
            by_eyebrow[s["eyebrow"]].append({"mode": r["mode"], "headline": s["headline"],
                                             "paragraphs": s.get("paragraphs", []), "blocks": s.get("blocks", [])})
    return [{"eyebrow": e, "runs": by_eyebrow[e]} for e in sorted(by_eyebrow, key=lambda e: ORDER.index(e) if e in ORDER else len(ORDER))]


def rollup(rows, today):
    cutoff = days_ago_la(ROLLUP_DAYS)
    rows = [r for r in rows if r["date"] >= cutoff]
    people = defaultdict(list)
    companies = defaultdict(list)
    for r in rows:
        if r.get("who"):
            people[r["who"]].append(r)
        if r.get("company") and len(r["company"]) >= 4:
            companies[r["company"]].append(r)

    def pack(name, rs):
        kinds = Counter(r["kind"] for r in rs)
        rs = sorted(rs, key=lambda r: r["date"], reverse=True)
        return {"name": name, "n": len(rs), "kinds": {KIND_LABELS.get(k, k): n for k, n in kinds.most_common()},
                "n_kinds": len(kinds), "latest": rs[0]["date"], "new_today": any(r["recorded"] == today for r in rs),
                "signals": [{"date": r["date"], "detail": r["detail"], "url": r["url"], "kind": KIND_LABELS.get(r["kind"], r["kind"])} for r in rs[:8]],
                "more": max(0, len(rs) - 8)}

    ppl = [pack(n, rs) for n, rs in people.items() if len(rs) >= 2]
    ppl.sort(key=lambda p: (p["n_kinds"], p["n"], p["latest"]), reverse=True)
    cos = []
    for n, rs in companies.items():
        kinds = {r["kind"] for r in rs}
        names = {r["who"] for r in rs if r.get("who")}
        if len(kinds) >= 2 or len(names) >= 2:
            cos.append(pack(n.title(), rs) | {"people": len(names)})
    cos.sort(key=lambda p: (p["n_kinds"], p["people"], p["n"], p["latest"]), reverse=True)
    return {"people": ppl[:15], "companies": cos[:15], "window_days": ROLLUP_DAYS, "tracked": len(rows)}


def main():
    if len(sys.argv) != 2:
        print("usage: build_conflicts.py content-TODAY.json", file=sys.stderr)
        sys.exit(1)
    today, mode = today_la(), run_mode()
    extras = load_extras("conflicts")
    new_sections = sort_sections([s for ex in extras for s in ex.get("sections", [])])
    summaries = {ex["name"]: ex.get("summary", "") for ex in extras}

    os.makedirs("archive/conflicts", exist_ok=True)
    arch = f"archive/conflicts/{today}-{mode}.json"
    keep_old = False
    if not new_sections and os.path.exists(arch):
        try:
            keep_old = bool(json.load(open(arch)).get("sections"))
        except (OSError, ValueError):
            pass
    if not keep_old:
        with open(arch, "w") as f:
            json.dump({"sections": new_sections, "summaries": summaries}, f, indent=1)
            f.write("\n")

    archives = load_archives()
    today_runs = [a for a in archives if a["date"] == today]
    earlier = []
    for day in sorted({a["date"] for a in archives if a["date"] != today}, reverse=True):
        view = day_view([a for a in archives if a["date"] == day])
        if view:
            earlier.append({"date": day, "sections": view})

    sig = Signals()
    new_today = Counter(r["kind"] for r in sig.rows if r["recorded"] == today)
    content = {
        "date": datetime.now(LA_TZ).strftime("%A, %B %-d, %Y"),
        "mode": mode,
        "new_today": {KIND_LABELS.get(k, k): n for k, n in new_today.most_common()},
        "rollup": rollup(sig.rows, today),
        "today": day_view(today_runs),
        "earlier": earlier,
        "scanner_status": {SCANNERS.get(k, k): v for k, v in summaries.items()},
    }
    with open(sys.argv[1], "w") as f:
        json.dump(content, f, indent=2)
        f.write("\n")
    print(f"built {sys.argv[1]}: {len(content['today'])} section(s) today, {len(earlier)} earlier day(s), "
          f"{len(content['rollup']['people'])} repeat people, {len(content['rollup']['companies'])} repeat companies", file=sys.stderr)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Decides, before the main Claude pass, whether there is anything for the
model to write (added 2026-10-12). Prints "run" or "skip" on stdout; on a
skip it writes whatever the workflow needs instead, so no tokens are spent
re-describing nothing.

  campaigns  skip when FEC was reachable and there are no new FRQ/F99
             filings, no negative-amount rows and no extras sections: writes
             a short "nothing new" content-TODAY.json (assemble.py still adds
             the APOD) -- the model was paying a full pass to say this.
  congress   skip when the newest Congressional Record issue was already
             covered by an earlier edition (data/congress-covered.json): the
             last summary of that issue is carried over and today's extras
             are appended. Happened 3 runs in a row 10/10-10/11 (same
             Khanna/Massie lede each time).
  record-congress <content.json>  run after a model-written edition.
  officials  skip when every source was reachable and nothing new came in
             (data/officials_input.json counts): writes a short content file.

Usage: gate.py <edition>
"""
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dc_common import today_la  # noqa: E402
from dc_extras import load_extras, load_json, run_mode  # noqa: E402


COVERED_PATH = "data/congress-covered.json"


def long_date(iso):
    d = date.fromisoformat(iso)
    return f"{d.strftime('%A, %B')} {d.day}, {d.year}"


def write_stub(headline, paragraph, sources, window):
    today = today_la()
    content = {
        "date": long_date(today),
        "top_story": {"eyebrow": "Top Story", "headline": headline, "paragraphs": [paragraph]},
        "sections": [],
        "footer": {"coverage_window": window, "sources": sources,
                   "known_gaps": "No model pass this run: the pre-fetch found nothing new to write about."},
    }
    with open(f"content-{today}.json", "w") as f:
        json.dump(content, f, indent=2)
        f.write("\n")


def campaigns():
    c = load_json("data/campaigns_candidates.json", {}) or {}
    neg = load_json("data/negative_amounts_candidates.json", {}) or {}
    errors = [e for e in c.get("errors") or [] if not e.startswith("APOD")]
    if not c.get("reachable", False) or c.get("filings") or neg.get("rows") or errors or neg.get("errors"):
        return "run"
    if any(ex.get("sections") for ex in load_extras("campaigns")):
        return "run"
    write_stub("No new FEC compliance filings since the last edition",
               "No new RFAI letters, Form 99 correspondence or voided transactions of $10,000 or more surfaced "
               "since the last " + run_mode().upper() + " edition.",
               "FEC filings API; FEC Schedule A/B itemized transactions.",
               f"FEC filings received {c.get('lookback')} onward, deduplicated against earlier editions.")
    return "skip"


def congress():
    """Skip when the newest issue was already covered. The page still
    publishes: the last model-written Top Story and On the Record section
    for that issue are carried over (noted as such), and assemble.py adds
    today's fresh Missed Votes / Discharge Petitions sections."""
    inp = load_json("data/congress_input.json", {}) or {}
    pkg = inp.get("package_id")
    if not pkg or inp.get("errors"):
        return "run"
    covered = load_json(COVERED_PATH, {}) or {}
    last = covered.get("last") or {}
    if pkg not in covered.get("packages", {}) or last.get("package") != pkg:
        return "run"
    note = (f"No new Congressional Record issue has been published since this one "
            f"(first covered {covered['packages'][pkg]}); the summary below is carried over.")
    ts = dict(last["top_story"])
    ts["paragraphs"] = list(ts.get("paragraphs") or []) + [note]
    content = {"date": long_date(today_la()), "top_story": ts, "sections": last["sections"],
               "footer": {**last.get("footer", {}),
                          "known_gaps": "No new Record issue; the On the Record summary is carried over from "
                                        f"{covered['packages'][pkg]}. Missed Votes and Discharge Petitions are current."}}
    with open(f"content-{today_la()}.json", "w") as f:
        json.dump(content, f, indent=2)
        f.write("\n")
    return "skip"


def record_congress(path):
    """After a model-written Congress edition: remember which issue it
    covered and its own Top Story/sections (before extras are appended)."""
    inp = load_json("data/congress_input.json", {}) or {}
    pkg = inp.get("package_id")
    if not pkg:
        return
    with open(path) as f:
        content = json.load(f)
    covered = load_json(COVERED_PATH, {}) or {}
    pk = covered.setdefault("packages", {})
    pk.setdefault(pkg, today_la())
    covered["packages"] = dict(sorted(pk.items())[-60:])
    covered["last"] = {"package": pkg, "top_story": content.get("top_story"),
                       "sections": content.get("sections", []), "footer": content.get("footer", {})}
    with open(COVERED_PATH, "w") as f:
        json.dump(covered, f, indent=1)
        f.write("\n")


def officials():
    inp = load_json("data/officials_input.json", {}) or {}
    if not inp or inp.get("errors") or inp.get("new_items", 1):
        return "run"
    if any(ex.get("sections") for ex in load_extras("officials")):
        return "run"
    write_stub("No new ethics, lobbying or disclosure filings since the last edition",
               "No new financial disclosures, stock-trade reports, lobbying or foreign-agent registrations, or "
               "executive-branch ethics filings surfaced since the last " + run_mode().upper() + " edition.",
               "House Clerk and Senate eFD disclosures; LDA; FARA; OGE.", inp.get("window", ""))
    return "skip"


if __name__ == "__main__":
    if sys.argv[1] == "record-congress":
        record_congress(sys.argv[2])
        sys.exit(0)
    ed = sys.argv[1]
    try:
        verdict = {"campaigns": campaigns, "congress": congress, "officials": officials}[ed]()
    except Exception as e:  # noqa: BLE001  -- a broken gate must never block an edition
        print(f"gate {ed}: error {e}; running the model", file=sys.stderr)
        verdict = "run"
    print(f"gate {ed}: {verdict}", file=sys.stderr)
    print(verdict)

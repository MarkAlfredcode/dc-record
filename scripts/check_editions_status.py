#!/usr/bin/env python3
"""Checks whether all 4 DC Record editions have finished their AM (or PM)
run for today, and sends exactly one consolidated ntfy notification once
they have -- either "all ready" or a plain explanation of whatever went
wrong, never silence. Replaces the old dc-record-pm-alert.yml, which only
ever notified on the all-success case and stayed silent (no notification at
all) if something had actually failed.

Usage: python3 scripts/check_editions_status.py am|pm
Exit 0 always (this is a notifier, not a gate -- a bug here should never
fail the calling workflow's job).

Design: for each of the 3 editions, find the most recent GitHub Actions run
of its workflow whose creation time falls on TODAY (America/Los_Angeles)
and within the given window (am = 00:00-11:59:59 PT, pm = 12:00-23:59:59
PT) -- this is how the scheduler's two daily dispatches per edition
(../scheduler/wrangler.toml) land, and matches how the rest of this repo
already reasons about "AM" vs "PM" via digest/*-am.html / *-pm.html rather
than exact minute-level cron matching. Classify each edition as:
  - PENDING: no run yet this window, or the run is still in progress.
    Not an error -- just too early to say anything. Silent.
  - PROBLEM: a run completed but conclusion != "success" -- a real crash,
    e.g. the "Install dependencies" step failing, or an uncaught error
    outside the `set +e` guarded sections.
  - OK: run completed with conclusion == "success".

Deliberately does NOT additionally cross-check digest/{id}-{window}.html's
freshness as a "ran green but produced nothing" signal (an earlier version
of this script did) -- confirmed by hand 2026-09-13 that the AM/PM digest
file split (which -am.html/-pm.html a run's output lands in, tracked by
digest/.{id}-am-date in each edition's own workflow) can currently disagree
with a run's actual wall-clock window on a day with more than one dispatch,
which would make that check unreliable right now. Not a real gap: the
"ran green but found nothing to report" case (3+ of 5 sources down, etc.)
already gets its own ntfy notice from send_notice.py inside the edition's
own workflow, so this script only needs to catch the case nothing else
covers -- the run didn't complete successfully at all.

Once every edition is OK or PROBLEM (i.e. nothing is still PENDING), send
one notification for this window and record today's date in
digest/.{window}-alert-date so a later trigger the same day doesn't send it
again. While anything is still PENDING, do nothing and exit quietly --
same as the old behavior -- the calling workflow fires on every completion
of any of the 3 editions' workflows (6x/day), so most invocations are
expected to find "not all 4 done yet" and that's normal, not worth logging
as a problem.
"""
import json
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

LA_TZ = ZoneInfo("America/Los_Angeles")

EDITIONS = [
    ("campaigns", "dc-record-campaigns.yml", "Campaigns"),
    ("congress", "dc-record-congress.yml", "Congress"),
    ("feds", "dc-record-feds.yml", "The Feds"),
    ("officials", "dc-record-officials.yml", "Officials"),
]

WINDOW_HOURS = {"am": range(0, 12), "pm": range(12, 24)}


def gh_runs(workflow_file):
    """Latest runs for one workflow, newest first (gh run list's own
    default order) -- 15 is comfortably more than the <=3/day this repo's
    editions actually produce (1 scheduled + the occasional manual test),
    so today's window's run is never missed even on a heavy test day."""
    out = subprocess.run(
        ["gh", "run", "list", f"--workflow={workflow_file}", "--limit=15",
         "--json", "databaseId,conclusion,status,createdAt,url"],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return json.loads(out.stdout)


def latest_run_in_window(runs, today_str, hours):
    for r in runs:
        created = datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00")).astimezone(LA_TZ)
        if created.strftime("%Y-%m-%d") == today_str and created.hour in hours:
            return r  # newest-first order, so the first match is the latest
    return None


def classify(edition_id, workflow_file, label, window, today_str):
    try:
        runs = gh_runs(workflow_file)
    except Exception as e:  # noqa: BLE001
        return "PROBLEM", f"{label}: couldn't check run history ({e})"

    run = latest_run_in_window(runs, today_str, WINDOW_HOURS[window])
    if run is None:
        return "PENDING", None
    if run["status"] != "completed":
        return "PENDING", None
    if run["conclusion"] != "success":
        return "PROBLEM", f"{label}: run failed ({run['conclusion']}) -- {run['url']}"
    return "OK", f"{label}: OK"


def send_ntfy(title, tags, body):
    subprocess.run(
        ["curl", "-s", "--max-time", "10",
         "-H", f"Title: {title}", "-H", f"Tags: {tags}",
         "-d", body, "https://ntfy.sh/markalfreddotnews"],
        capture_output=True, timeout=15,
    )


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("am", "pm"):
        print("usage: check_editions_status.py am|pm", file=sys.stderr)
        sys.exit(0)  # never fail the caller over a bad invocation
    window = sys.argv[1]
    today_str = datetime.now(LA_TZ).strftime("%Y-%m-%d")

    marker_path = f"digest/.{window}-alert-date"
    try:
        with open(marker_path) as f:
            already_alerted = f.read().strip() == today_str
    except FileNotFoundError:
        already_alerted = False
    if already_alerted:
        print(f"Already sent today's ({today_str}) {window.upper()} alert -- staying quiet.")
        return

    statuses = []
    for edition_id, workflow_file, label in EDITIONS:
        state, detail = classify(edition_id, workflow_file, label, window, today_str)
        statuses.append((edition_id, label, state, detail))
        print(f"{label}: {state}" + (f" -- {detail}" if detail and state != 'OK' else ""))

    if any(s[2] == "PENDING" for s in statuses):
        print(f"Not all 4 {window.upper()} editions are done yet -- staying quiet.")
        return

    problems = [s for s in statuses if s[2] == "PROBLEM"]
    if problems:
        # Each edition's own workflow already sent its failure ntfy; don't
        # double-alert, and don't write the marker so a successful re-run
        # later in the window can still trigger the all-done notice.
        print(f"{len(problems)} {window.upper()} edition(s) failed -- failure alerts already sent by the editions themselves; staying quiet.")
        return

    title = f"DC Record {window.upper()}: all {len(EDITIONS)} editions ready"
    tags = "white_check_mark"
    body = ", ".join(label for _, label, _, _ in statuses) + " have all finished."

    send_ntfy(title, tags, body)
    print(f"Sent {window.upper()} alert: {title}")

    import os
    os.makedirs("digest", exist_ok=True)
    with open(marker_path, "w") as f:
        f.write(today_str + "\n")


if __name__ == "__main__":
    main()

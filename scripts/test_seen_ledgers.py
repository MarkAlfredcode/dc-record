#!/usr/bin/env python3
"""Lint: every "already reported" ledger a script writes is committed and reverted by the workflows that run it (2026-11-06).

Lifecycle of an item in a seen-ledger:  new -> marked on disk by the run -> kept only if the edition really emailed
(`email_sent.flag`); a site-refresh-only run reverts the file (`git checkout --`) so nothing counts as reported that
never reached anyone. Each workflow lists its ledgers by hand twice (the revert and the `git add`), so a new ledger added
to one list and not the other either repeats forever (never committed) or is swallowed by a run nobody was emailed
about (not reverted). This fails when a ledger a workflow's scripts write is missing from either list.

Run: python3 -I scripts/test_seen_ledgers.py
"""
import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER = re.compile(r"data/[\w\-]*(?:-seen|-shown|surfaced-links|-ledger|-pending)[\w\-]*\.json")
# Committed always, never reverted: they record things that really happened, not what an email showed.
NEVER_REVERT = {"data/oge-requests-seen.json", "data/surfaced-links.json", "data/lobby-contracts-pending.json",
                "data/wiki-events-seen.json"}
# surfaced-links is reverted only on manual test runs, not site-refresh runs (per-mode memory); pending files are queues;
# wiki-events has no email at all (dc-record-events.yml just refreshes data/events.json).


def joined(text):
    return re.sub(r"\\\n\s*", " ", text)


def main():
    scripts = {}
    for p in glob.glob(os.path.join(ROOT, "scripts", "*.py")):
        name = os.path.basename(p)[:-3]
        if name.startswith("test_"):
            continue
        scripts[name] = set(LEDGER.findall(open(p).read()))
    bad = []
    for wf in sorted(glob.glob(os.path.join(ROOT, ".github", "workflows", "dc-record-*.yml"))):
        text = joined(open(wf).read())
        if "git add" not in text:
            continue
        added, reverted, ran = set(), set(), set(re.findall(r"\b(\w+)\.py\b", text))  # also bare names in `for s in fetch_x.py ...`
        for line in text.split("\n"):
            if "git checkout --" in line:
                reverted |= set(re.findall(r"data/[\w\-]+\.json", line))
            if "git add " in line or re.search(r"for f in .*data/", line):
                added |= set(re.findall(r"data/[\w\-]+\.json", line))
        need = set()
        for s in ran:
            need |= scripts.get(s, set()) if s != "mark_seen" else set()
        for m in re.finditer(r"mark_seen\.py\s+\S+\s+\S+\s+(data/[\w\-]+\.json)", text):
            need.add(m.group(1))
        wfname = os.path.basename(wf)
        for f in sorted(need):
            if f not in added:
                bad.append(f"{wfname}: {f} is written by a script this workflow runs but never `git add`ed (it would repeat forever)")
            if f not in reverted and f not in NEVER_REVERT and not f.endswith(("-pending.json", "-ledger.json")):
                bad.append(f"{wfname}: {f} is not reverted on site-refresh-only runs (it would be marked reported with no email)")
    if bad:
        print("\n".join(bad))
        sys.exit(1)
    print("seen-ledger lists ok")


if __name__ == "__main__":
    main()

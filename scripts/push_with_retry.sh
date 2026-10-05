#!/usr/bin/env bash
# Push the current commit to main, rebasing onto whatever landed meanwhile.
#
# Every edition workflow (plus status-alert) pushes to main around the same
# cron slot. A plain `git pull --rebase` under `set -e` died on the FIRST
# conflict -- e.g. 2026-10-05 PM, when Cloudflare double-fired the cron and a
# queued duplicate run (checked out before the first one committed) wrote the
# same archive/<edition>/<date>-pm.json and digest/<edition>-pm.html files:
# add/add conflict, exit 1, retries 2-5 never ran.
#
# `-X theirs` during a rebase means "keep the commit being replayed", i.e.
# this run's own output wins any hunk-level conflict. If a conflict still
# can't be auto-resolved (e.g. modify/delete), abort the rebase so the
# checkout isn't left mid-rebase, and try again from a fresh fetch.
set -uo pipefail

for i in 1 2 3 4 5; do
  if git push; then
    exit 0
  fi
  echo "Push rejected (attempt $i/5), rebasing onto latest and retrying..."
  if ! git pull --rebase -X theirs; then
    echo "Rebase hit a conflict -X theirs couldn't resolve:"
    git status --short | grep -E '^(DD|AU|UD|UA|DU|AA|UU) ' || true
    git rebase --abort || true
  fi
  sleep $((RANDOM % 5 + 1))
done

echo "Push failed after 5 attempts." >&2
exit 1

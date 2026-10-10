# The DC Record

Automated daily newsletter — FEC compliance filings (RFAI/Form 99), House &
Senate personal financial disclosures & stock trades, the Congressional
Record, committee meetings, lobbying registrations (LDA) & LD-203
contributions, the Federal Register, OMB apportionments, and new large
USAspending contract awards — for a journalist covering campaign-finance
compliance and Congress.

Runs entirely on GitHub Actions (`.github/workflows/dc-record.yml`), on a
schedule, independent of any local machine. Three editions a day:

- **Morning** (~8:30am Pacific) — full previous-day coverage, plus the two
  once-daily-only sections below (OMB Apportionments, USAspending
  Contracts) that don't run in the other two editions.
- **Afternoon** (~4:40pm Pacific) — today's postings so far (agencies post
  filings continuously through the business day, so this isn't a repeat of
  the morning edition).
- **Evening** (~9:35pm Pacific) — same as afternoon, but **only sent on days
  Congress was actually in session** (the prompt checks this itself before
  doing anything else, via the Congressional Record and committee-meeting
  data).

Target delivery times above are ~8:30am / ~4:40pm / ~9:35pm, but the cron
triggers in `dc-record.yml` are each set 90 minutes earlier than that
(~7:00am / ~3:10pm / ~8:05pm) to absorb GitHub's scheduling jitter and the
watchdog's drop-detection grace period — see "Known limitations" below.

The actual task list, data-source gotchas, and newsletter design spec all
live in `prompt.txt` — that's the real source of truth, fed to `claude -p`
as stdin (prefixed with an `EDITION_MODE=` line so the same prompt handles
all three editions). The workflow YAML is a thin scheduler/wrapper around it.

Design notes learned from a live trial run (2026-09-03): the masthead uses
Google Fonts' 'Newsreader' with a navy+red rule treatment layered onto the
existing white/offwhite palette (still light-only, still the same base
colors — red/white/blue is an accent, not a repaint). Long lists (RFAI,
Form 99, bills) render as bulleted lists with a real per-item description,
never truncated mid-sentence and never curated down to a "selected" subset
— a table was tried for these once and reverted, since a table fits
genuinely columnar data (stock trades, K Street) better than a list whose
real content is a paragraph per row. Every edition closes with NASA's
Astronomy Picture of the Day, embedded as a base64 data URI (not a remote
image link, which most mail clients block by default) — a deliberate visual
break from the rest of the newspaper-style layout.

## OMB Apportionments & USAspending Contracts (Morning Edition only)

Tasks 8-9 in `prompt.txt` add two sections to the Morning Edition only,
positioned right above "K Street" — checked once/day rather than 3x/day
because both sources are naturally slower-moving than everything else in
this newsletter, and re-checking them on the afternoon/evening runs would be
pure repeated work for no new information.

**OMB Apportionments** (`apportionment-public.max.gov`, a static file mirror
with no real API): new apportionment JSON files are found with no stored
state at all — filenames embed their own approval timestamp, so a rolling
`>= yesterday` filter on the front page's file list is enough, same pattern
as everything else in this repo. The ~137 Spend Plan PDFs are different: no
date in their filename, and they can be silently revised in place under the
same URL — `Last-Modified`/`ETag` on this site are useless for that
(confirmed by hand: unrelated files served at the same moment carry an
identical `Last-Modified` down to the second, meaning the whole site
republishes as one bundle regardless of per-file changes).
`data/omb-spend-plan-manifest.json` tracks each PDF's `Content-Length` (the
one reliable signal) instead.

**USAspending Contracts** (`api.usaspending.gov`): new $2M+ prime contract
*base awards* only (`Mod == "0"`, excludes modifications/option-exercises).
This source has real multi-day-to-multi-week posting lag with no reliable
"date this record appeared" field (`last_modified_date` was tested and
rejected — it reflects the *award*'s last-touched date, not the specific
transaction's, so it resurfaces old already-reported activity whenever
anything else changes on the same award). Instead of a rolling date window,
this re-scans a 25-day trailing window every morning and diffs against
`data/usaspending-contracts-manifest.json`, a persisted set of previously-
seen award IDs, pruned of anything older than 30 days each run.

Both manifests are committed back to the repo by the workflow's own "Commit
updated OMB/USAspending manifests" step (needs `contents: write`, unlike the
main job's earlier `contents: read`) — a no-op on afternoon/evening runs
since only Tasks 8-9 touch those files. `data/omb-spend-plan-manifest.json`
was bootstrapped 2026-09-03 with real `Content-Length` values for all 137
spend plans as of that date; `data/usaspending-contracts-manifest.json` was
bootstrapped the same day covering the trailing 25 days (748 entries) —
both specifically so the first live morning run doesn't dump a backlog on
the reader, only genuinely new items going forward.

## Required repository secrets

Set these under **Settings → Secrets and variables → Actions**:

| Secret | What it's for |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | Auth for the `claude` CLI running headless in CI — a long-lived (1-year) token generated by `claude setup-token`, billed against the Claude subscription's included usage rather than metered API dollars. |
| `RESEND_API_KEY` | Sends the finished newsletter via Resend. |
| `FEC_KEYS` | Comma-separated pool of api.data.gov keys (works for FEC, congress.gov, govinfo — all rotate through the same pool). |

No secret is ever written into a committed file — `prompt.txt` only
references `$RESEND_API_KEY` / `$FEC_KEYS` by name; the workflow injects the
real values as env vars at run time.

**Cost:** with `CLAUDE_CODE_OAUTH_TOKEN`, this whole pipeline runs at no cost
beyond the existing Claude subscription — GitHub Actions compute is also free
at this volume (roughly 500-700 minutes/month against GitHub's 2,000
free minutes/month for private repos). The tradeoff: usage here counts
against the subscription's own usage limits, same pool as interactive
Claude Code use, so three heavy automated editions a day could eat into
headroom during a busy week. Watch for rate-limit failures in the Actions
logs; if that becomes a problem, the workflow can fall back to
`ANTHROPIC_API_KEY` metered billing instead (swap the env var in
`dc-record.yml`'s "Run The DC Record" step). The token expires in
1 year (2027-08-19) and will need regenerating via `claude setup-token`
then.

## Known limitations / things worth revisiting

- **Fixed-UTC cron drifts with US DST.** The three cron times are tuned for
  PDT (UTC-7); once PST (UTC-8) kicks in each November they'll all run an
  hour later Pacific time. Revisit the cron expressions then, or move to a
  more elaborate DST-aware trigger if that annoyance isn't worth living with.
- **GitHub's own scheduling isn't perfectly on-time — and has been getting
  worse.** GitHub documents scheduled workflows as firing late, never early,
  and being delayed further during periods of high load. A single 26-minute
  delay was the worst case observed as of 2026-08-20, but by 2026-08-31 the
  actual pattern (cross-checked against each run's real edition, from its
  artifact name) showed delays climbing from ~1-2 hours in late August to
  **6-10+ hours** on several days — far past anything the 90-minute cron pad
  (added 2026-08-30) or the watchdog's 45-minute grace period were sized for.
  Scheduled runs can also be dropped entirely (see `dc-record-watchdog.yml`,
  which polls and self-fires a catch-up via `workflow_dispatch` if a trigger
  looks missing).
  - **This created a real duplicate-send bug**, confirmed live 2026-08-27/28:
    once actual delay routinely exceeds the watchdog's 45-min grace period,
    the watchdog can no longer tell "dropped" from "just very late," so it
    dispatches a catch-up — and then the original scheduled trigger *also*
    eventually fires, hours later. Both used to run to completion, each
    presumably sending its own newsletter for the same edition/day. The old
    `concurrency:` group didn't prevent this either, because a scheduled run
    and a `workflow_dispatch` run were keyed differently (raw cron string vs.
    edition name) and so never recognized each other as duplicates.
  - **Fixed 2026-08-31**: `dc-record.yml` now (a) keys its `concurrency:`
    group on the resolved edition name so a schedule fire and a dispatch for
    the same edition land in the same group, and (b) added a "Guard against
    duplicate edition for this window" step that checks whether another run
    already published this edition since its window opened and skips the
    rest of the job if so. This doesn't fix GitHub's underlying scheduling
    delay (still worth watching / revisiting the pad on), but it does stop a
    late-but-not-dropped trigger and a watchdog catch-up from both sending.
  - Scheduled workflows are also automatically disabled if the repo goes 60
    days without any activity (a commit, or someone re-enabling it manually)
    — worth an occasional check that it's still enabled.
- **No GAO reports, no privately-sponsored travel coverage** — both were
  checked and confirmed to have no usable public API as of 2026-08 (GAO
  blocks automated requests outright; House/Senate travel-disclosure portals
  are JS apps with no discoverable data endpoint). Travel's static
  "check here" pointer section was removed 2026-08-20 at user request rather
  than kept as a low-value placeholder; neither is covered at all now.
- **Subscription usage limits** — now running on `CLAUDE_CODE_OAUTH_TOKEN`
  (subscription-backed, not metered billing), the risk shifts from "costs
  money" to "could hit a rate/usage limit" during a busy stretch, since it
  shares the same usage pool as interactive Claude Code sessions. Check
  Actions run logs occasionally for auth/rate-limit failures.

## Manual test run

Actions tab → "The DC Record" → **Run workflow**, pick an edition from the
dropdown. Useful for testing changes without waiting for the next scheduled
fire. The generated HTML is also uploaded as a downloadable build artifact
on every run (30-day retention), regardless of whether the email send
succeeded.

## Deterministic extras, Your Beat, archive (added 2026-10-03)

Pre-pass scripts that build whole sections with **no LLM**. Each writes
`extras/<edition>/<name>.json`; `scripts/assemble.py` appends them to
Claude's `content-TODAY.json` after the main pass (Claude reads only their
one-line summaries, to weigh Top Story), builds the **Your Beat** box
from `data/watchlist.json`, and saves a dated copy to
`archive/<edition>/<date>-<am|pm>.json`. (A separate Leads box was removed
the same day at the reader's request -- it repeated findings shown
elsewhere; prompts now fold any next-step fact into the item itself.
`render_common.py` still draws the box if `leads` is ever non-empty.)

| Script | Edition | Section |
|---|---|---|
| `fetch_deadlines.py` | Officials, Campaigns | Deadline Watch: members missing/late on annual PFDs; members' campaigns with no FEC report covering a period; coming-due calendar (`data/deadlines.json`) |
| `fetch_ethics_agreements.py` | Officials | Ethics Agreement Tracker: overdue/late compliance certifications, sales of promised divestitures after deadline, late executive-branch 278-Ts, new conflict-of-interest waivers, amended-filing diffs |
| `fetch_pfd_analysis.py` | Officials | Disclosure Red Flags: House/Senate trades disclosed >45 days late (with repeat-offender history), trades near `data/events.json` events, amended-report diffs |
| `fetch_presidential_trades.py` | Officials | The President's Trades: Trump/Vance 278-Ts counted and summarized |
| `fetch_lda_trends.py` | Officials | Industries to Watch (`data/lda_industries.json`) |
| `fetch_money_out.py` | Campaigns | Money Out: bulk refunds, forfeitures/disgorgements (FTX-linked flagged), investments, Trump-property spending, thefts, former members' still-spending committees |

| `fetch_wiki_events.py` → `prompt-events.txt` → `apply_event_additions.py` | (calendar) | Every ~10 days (`dc-record-events.yml`, 1st/11th/21st): new items on this year's Wikipedia timeline/year pages; Claude picks market-moving ones; the script copies dates/sources from the candidates (never from the model) into `data/events.json`, commits, and pushes an ntfy summary |

Oversight-area checks use `data/jurisdiction.json` (committee and agency → sectors) via `scripts/jurisdiction.py`.

Editable config, no code changes needed: `data/watchlist.json`,
`data/events.json` (fill in dates; undated events are skipped),
`data/deadlines.json`, `data/lda_industries.json`, `data/jurisdiction.json`.

Search past editions: `python3 scripts/search_archive.py "Justice|Greenbrier" --since 2026-09-01`.
Test a script against older data: `DC_LOOKBACK_DAYS=60 python3 scripts/fetch_pfd_analysis.py`.

OGE note: OGE moved its index API from `API.xsp/v2/rest` to `v3/rest`
(2026-10-03); v3 ignores sort order, so `scripts/oge_index.py` pulls the
whole index (~8s) and filters locally.

## Conflicts edition (removed 2026-10-10)

A fifth, web-only edition (Donor Awards, Disclosure Red Flags, Trades Near
Contract Awards, Repeat names) ran 2026-10-04 to 2026-10-10 and was removed as
not needed. Its scripts and data are in git history before the removal commit.
`data/stock-act-ledger.json` stays: Officials' ethics-agreement tracker uses it.

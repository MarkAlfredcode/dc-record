# The DC Record

Automated daily newsletter — FEC compliance filings (RFAI/Form 99), House &
Senate personal financial disclosures & stock trades, the Congressional
Record, committee meetings, lobbying registrations (LDA) & LD-203
contributions, and the Federal Register — for a journalist covering
campaign-finance compliance and Congress.

Runs entirely on GitHub Actions (`.github/workflows/dc-record.yml`), on a
schedule, independent of any local machine. Three editions a day:

- **Morning** (~5:00am Pacific) — full previous-day coverage.
- **Afternoon** (~4:15pm Pacific) — today's postings so far (agencies post
  filings continuously through the business day, so this isn't a repeat of
  the morning edition).
- **Evening** (~9:45pm Pacific) — same as afternoon, but **only sent on days
  Congress was actually in session** (the prompt checks this itself before
  doing anything else, via the Congressional Record and committee-meeting
  data).

The actual task list, data-source gotchas, and newsletter design spec all
live in `prompt.txt` — that's the real source of truth, fed to `claude -p`
as stdin (prefixed with an `EDITION_MODE=` line so the same prompt handles
all three editions). The workflow YAML is a thin scheduler/wrapper around it.

## Required repository secrets

Set these under **Settings → Secrets and variables → Actions**:

| Secret | What it's for |
|---|---|
| `ANTHROPIC_API_KEY` | Auth for the `claude` CLI running headless in CI. |
| `RESEND_API_KEY` | Sends the finished newsletter via Resend. |
| `FEC_KEYS` | Comma-separated pool of api.data.gov keys (works for FEC, congress.gov, govinfo — all rotate through the same pool). |

No secret is ever written into a committed file — `prompt.txt` only
references `$RESEND_API_KEY` / `$FEC_KEYS` by name; the workflow injects the
real values as env vars at run time.

## Known limitations / things worth revisiting

- **Fixed-UTC cron drifts with US DST.** The three cron times are tuned for
  PDT (UTC-7); once PST (UTC-8) kicks in each November they'll all run an
  hour later Pacific time. Revisit the cron expressions then, or move to a
  more elaborate DST-aware trigger if that annoyance isn't worth living with.
- **GitHub's own scheduling isn't perfectly on-time** — GitHub explicitly
  documents that scheduled workflows can be delayed during periods of high
  load, and are automatically disabled if the repo goes 60 days without any
  activity (a commit, or someone re-enabling it manually) — worth an
  occasional check that it's still enabled.
- **No GAO reports, no privately-sponsored travel API** — both were checked
  and confirmed to have no usable public API as of 2026-08 (GAO blocks
  automated requests outright; House/Senate travel-disclosure portals are
  JS apps with no discoverable data endpoint). Travel gets a static "check
  here" pointer instead of real content; GAO isn't covered at all.
- **`ANTHROPIC_API_KEY` billing** — this uses metered API billing, separate
  from any Claude subscription. `claude setup-token` may allow subscription-
  based headless auth instead, but that hasn't been verified to work in a
  CI context — worth testing if avoiding API costs matters.

## Manual test run

Actions tab → "The DC Record" → **Run workflow**, pick an edition from the
dropdown. Useful for testing changes without waiting for the next scheduled
fire. The generated HTML is also uploaded as a downloadable build artifact
on every run (30-day retention), regardless of whether the email send
succeeded.

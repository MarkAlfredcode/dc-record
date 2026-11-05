# DC Record scheduler

A standalone Cloudflare Worker that replaces GitHub Actions' own `schedule:`
trigger for the 4 dc-record edition workflows.

**Why:** GitHub's `schedule` event is documented as best-effort -- it was
observed firing every edition ~3.5h late, every single day. This Worker uses
Cloudflare Cron Triggers (independently reliable) to call each workflow's
`workflow_dispatch` API at the exact intended time instead.

## How it works

`wrangler.toml` declares ONE Cron Trigger, `0 * * * *` (hourly, UTC). On each
tick the Worker converts the scheduled time to US Eastern (`America/New_York`)
and fires whatever is due that hour. All times are Eastern and **follow EST/EDT
automatically** -- there is nothing to change at the DST switch.

| Eastern hour | Slot | Fires |
|---|---|---|
| 8:00am | `am` | Campaigns, Officials, The Feds -- AM edition (`mode: "am"`) -- plus Congress |
| 3:00pm | `pm` | Campaigns, Officials, The Feds -- PM edition (`mode: "pm"`) -- plus Congress |
| every hour, 4am-9pm | `ping` | Usage-window ping (`ping.yml` in `MarkAlfredcode/cron-utils`) -- one Haiku message; a no-op while a 5h Claude usage window is already open, so a window starts at the first ping after the previous one ends (4am, then 9am, 2pm, 7pm if nothing else opens one) |

The 8am and 3pm ticks fire both their edition slot and the ping.

`src/index.js`'s `scheduled()` handler looks up which workflows that exact
cron string maps to and fires one `workflow_dispatch` REST call per workflow,
passing `mode: "am"` or `mode: "pm"` (see `CRON_MODE`) on the 3 emailing
editions. Both slots really try to send: each workflow looks up the live
subscriber list for that edition+mode from
`markalfred.news/dc-record-recipients` and only sends if that list is
non-empty -- the 3:45pm slot used to be a hardcoded no-email site-refresh
(forcing `skip_email`/`site_refresh_only`), which is now a MANUAL-only
override (`MANUAL_SKIP_INPUT`, used just for the `dry=true` test path below),
not something a real cron dispatch sets. Congress is untouched by any of this
-- it never emails and takes no input at all (`dc-record-congress.yml`
declares none; passing any input would 422). It used to have its own
3:25am/4:10pm slots, inherited from the deleted Courts edition for no
functional reason; it now rides the main batches (pushes to `main` already
rebase-and-retry, and each workflow has its own concurrency group).

## DST

Handled in code (`easternHour()` uses `Intl` with `America/New_York`), so the
Eastern times above hold year-round. Edit the `AM_HOUR`/`PM_HOUR`/
`PING_*_HOUR` constants in `src/index.js` to change them. (The separate
`dc-record-status-alert.yml` GitHub crons are still fixed UTC and move an hour
on Eastern clocks at DST; they run hours after the editions either way.)

## Secrets

- `GITHUB_TOKEN` -- fine-grained PAT scoped to `MarkAlfredcode/dc-record`
  AND `MarkAlfredcode/cron-utils` (needed for the ping dispatch), Actions:
  read+write permission. Used to call `workflow_dispatch`.
- `TRIGGER_SECRET` -- shared secret gating the manual test endpoint (see
  below). Any random string; not otherwise sensitive.

Set both via `npx wrangler secret put <NAME>` from this directory.

## Manual testing

The Worker also answers plain HTTP GET, gated by `TRIGGER_SECRET`, so the
dispatch logic can be verified without waiting for a real cron tick:

```
curl "https://dc-record-scheduler.<subdomain>.workers.dev/?key=<TRIGGER_SECRET>&slot=pm&dry=true"
```

`dry=true` forces the manual skip input on every workflow that has one, so
a manual test never risks sending a real duplicate newsletter. Omit `dry` (or
set it to anything else) to fire a *real* dispatch with production behavior
-- only do that deliberately (a real dispatch still only emails if there's at
least one live subscriber for that edition+mode, but it's still a real send
attempt against real subscribers if there is).

## Deploying

```
cd scheduler
npx wrangler deploy
```

Requires `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` in the
environment (or `wrangler login`).

# DC Record scheduler

A standalone Cloudflare Worker that replaces GitHub Actions' own `schedule:`
trigger for the 4 dc-record edition workflows.

**Why:** GitHub's `schedule` event is documented as best-effort -- it was
observed firing every edition ~3.5h late, every single day. This Worker uses
Cloudflare Cron Triggers (independently reliable) to call each workflow's
`workflow_dispatch` API at the exact intended time instead.

## How it works

`wrangler.toml` declares 4 Cron Triggers (2 edition batches + 2
usage-window pings). The edition slots match the times that used to live in
each workflow's (now-disabled, left commented for reference) `schedule:`
block:

| Cron (UTC) | ~Pacific (PDT) | Fires |
|---|---|---|
| `0 14 * * *` | 7:00am | Campaigns, Officials, The Feds -- AM edition (`mode: "am"`) -- plus Congress and Conflicts |
| `45 22 * * *` | 3:45pm | Campaigns, Officials, The Feds -- PM edition (`mode: "pm"`) -- plus Congress and Conflicts |
| `0 10 * * *` | 3:00am | Usage-window ping (`dc-record-window-ping.yml`) -- one Haiku message so the 5h Claude usage window opens ~4h before the AM batch and resets right after it |
| `45 18 * * *` | 11:45am | Usage-window ping, ~4h before the PM batch |

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

## DST caveat

Same as the workflows themselves: these are UTC cron expressions with no
timezone support, so the Pacific-time column above drifts an hour each
Nov/March DST change. Revisit `wrangler.toml`'s `[triggers].crons` then.

## Secrets

- `GITHUB_TOKEN` -- fine-grained PAT scoped to `MarkAlfredcode/dc-record`
  only, Actions: read+write permission. Used to call `workflow_dispatch`.
- `TRIGGER_SECRET` -- shared secret gating the manual test endpoint (see
  below). Any random string; not otherwise sensitive.

Set both via `npx wrangler secret put <NAME>` from this directory.

## Manual testing

The Worker also answers plain HTTP GET, gated by `TRIGGER_SECRET`, so the
dispatch logic can be verified without waiting for a real cron tick:

```
curl "https://dc-record-scheduler.<subdomain>.workers.dev/?key=<TRIGGER_SECRET>&cron=45%2022%20*%20*%20*&dry=true"
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

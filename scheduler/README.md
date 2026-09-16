# DC Record scheduler

A standalone Cloudflare Worker that replaces GitHub Actions' own `schedule:`
trigger for the 4 dc-record edition workflows.

**Why:** GitHub's `schedule` event is documented as best-effort -- it was
observed firing every edition ~3.5h late, every single day. This Worker uses
Cloudflare Cron Triggers (independently reliable) to call each workflow's
`workflow_dispatch` API at the exact intended time instead.

## How it works

`wrangler.toml` declares 4 Cron Triggers, matching the times that used to
live in each workflow's (now-disabled, left commented for reference)
`schedule:` block:

| Cron (UTC) | ~Pacific (PDT) | Fires |
|---|---|---|
| `0 10 * * *` | 3:00am | FEC, Lobbying, Contracts -- AM edition (`mode: "am"`) |
| `25 10 * * *` | 3:25am | Courts -- real AM edition |
| `45 22 * * *` | 3:45pm | FEC, Lobbying, Contracts -- PM edition (`mode: "pm"`) |
| `10 23 * * *` | 4:10pm | Courts -- site-refresh only (Courts never emails either way) |

`src/index.js`'s `scheduled()` handler looks up which workflows that exact
cron string maps to and fires one `workflow_dispatch` REST call per workflow,
passing `mode: "am"` or `mode: "pm"` (see `CRON_MODE`) on the 3 emailing
editions. As of 2026-09-16 both slots really try to send: each workflow
looks up the live subscriber list for that edition+mode from
`markalfred.news/dc-record-recipients` and only sends if that list is
non-empty -- the 3:45pm slot used to be a hardcoded no-email site-refresh
(forcing `skip_email`/`site_refresh_only`), which is now a MANUAL-only
override (`MANUAL_SKIP_INPUT`, used just for the `dry=true` test path below),
not something a real cron dispatch sets. Courts is untouched by any of this
-- it never emails, so its PM slot still just refreshes site content and
takes no input at all (`dc-record-courts.yml` declares none; passing any
input would 422).

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

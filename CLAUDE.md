# The DC Record

Mark Alfred's automated accountability newsletter. GitHub `MarkAlfredcode/dc-record` (private). `README.md` describes the retired 2025-era single-edition setup and is out of date. This file and the prompts/workflows are current.

## Hard rules

- **Never push without explicit approval.** Show the diff or a rendered preview first. Live runs commit to `main` too, so `git pull --rebase` before pushing.
- The website (markalfred.news/dc-record) matters more than the email.
- Every item gets a direct source link (filing, notice, award on USAspending/SAM, bill, docket).
- Call out filings submitted well past their statutory deadline as **late disclosures**.
- More than ~4-5 similar items in a row → bulleted list, never run-on prose.
- An item shown in one edition must not reappear in the next edition of the same AM/PM slot.
- No WebSearch/WebFetch; use curl/requests against known URLs. Report times in Pacific.

## Editions

Four editions, each `.github/workflows/dc-record-<edition>.yml` + `prompt-<edition>.txt`:

- **campaigns**: FEC RFAIs/Form 99s, outside money (Schedule E), megadonors, money-out
- **congress**: Congressional Record, committee meetings, missed votes, discharge petitions. Takes no `mode` input (422 if passed).
- **feds**: SAM.gov notices (the section Mark cares most about; flag *unusual vendors for the government* above political ties), USAspending awards/mods/cuts, OMB apportionments, Federal Register, Section 232 Dockets (`fetch_trade_dockets.py`: new comment windows, inclusion requests, who commented or opposed, Federal Register 232 actions on Commerce's regulations.gov dockets; FEC_KEYS pool works for regulations.gov)
- **officials**: House/Senate PFDs and STOCK Act trades, OGE filings, presidential 278-Ts, stopped traders, pardon lobbying. Leads with **Contacts With the President, VP & Cabinet**: `fetch_officials.py` OCRs every FARA supplemental statement, amendment, agreement, registration and informational-materials filing from the last 14 days once (`data/fara-top-scanned.json`), searches it with `scripts/top_officials.py`, and also checks every LDA filing for the President/VP as an entity lobbied or a top official named; the model reports real communications and puts mention-only filings in `dismissed_refs` (`data/fara-top-seen.json`). The roster with tenures and prior titles is `data/top_officials.json`: **update it when the cabinet changes.**

**Document metadata** (`scripts/doc_metadata.py`, called from `sam_reader.read_file`/`read_pdf`, so every SAM attachment, ZIP member and FARA filing gets it, no LLM): PDF/Office properties, PDF link annotations, Office hyperlink rels, Word tracked changes/comments, hidden sheets and the extracted text are scanned for AI-chatbot fingerprints (`utm_source=chatgpt.com`/`openai`, `oaicite`, `citeturn`, an AI tool as Creator/Producer), other tracking params, leaked paths, and J&A author/company. Hits become `kind: "metadata"` findings (verified, quoted) merged into the per-file findings in `screen_sam_docs.py`; FARA hits (news >= 4 only) show up as `FILE METADATA/LINKS` lines in the TOP OFFICIALS block. OGE/House PTR/ethics-agreement PDFs read via `dc_common.pdftotext` are not covered. To add a signal, edit the regexes at the top of `doc_metadata.py`; `python3 scripts/doc_metadata.py <file>` prints what it finds.

`prompt-*-mechanical.txt` are cheap Haiku passes. `dc-record-events.yml` refreshes `data/events.json` from Wikipedia on the 1st/11th/21st. `dc-record.yml`, `dc-record-watchdog.yml`, `dc-record-window-ping.yml` are retired (manual dispatch only). `dc-record-status-alert.yml` checks the editions ran.

## FOIA

- `scripts/suggest_foias.py` (a step in all four edition workflows, AM and PM) drafts up to 3 narrow FOIA requests per edition from `data/foia_contacts.json` (federal FOIA offices that take email; rebuild with markalfredsite's `foia/pipeline/build_contacts.py`) and posts them to the private tracker at markalfred.news/foia.
- `scripts/draft_foia_replies.py` (Congress workflow, AM and PM) drafts replies to flagged agency emails and appeals/refiles of denials from the tracker's queue (`prompt-foia-replies.txt`).
- `foia-inbox/` is the Cloudflare Email Worker behind foia@markalfred.news (Email Routing rule on the markalfred.news zone): forward to Yahoo first, then log to the tracker's D1 database `foia-tracker`. Deploy with `npx wrangler deploy` from that folder. It also reads attachments (`src/attachments.js`, text stored in D1 `inbox_attachments`, schema in `schema_attachments.sql`), matches mail to requests by content (`src/match.js`, identical copy at markalfredsite `functions/api/foia-match.js`: foia.gov confirmation #, tracking #, quoted request title/text), and pushes an ntfy alert for newsworthy replies (`newsworthy()`, topic in `[vars] NTFY_TOPIC`). `backfill_attachments.py` (recent mail) and `import_history.py` (mail from before the Worker existed, imported as `handled`) read Yahoo over IMAP with `YAHOO_APP_PASSWORD`.
- None of these email anyone; Mark sends from the tracker. All need the `FOIA_INGEST_TOKEN` Actions secret (same value as the site's Pages secret) and skip without it.

## Schedule

A Cloudflare Worker (`scheduler/`, deployed as `dc-record-scheduler`) dispatches the 4 edition workflows via `workflow_dispatch` at `0 14` (AM, ~7:00am PT) and `45 22` (PM, ~3:45pm PT) UTC. The workflows' own `schedule:` blocks are commented out. **Revisit both crons at the Nov 1 DST change.** Cloudflare has double-fired crons before; the Worker skips a workflow already dispatched that minute, and each workflow has a `guard` job (`data/sent/<edition>.txt` for emailing editions, a 10-minute recent-run check for congress).

The hourly usage-window ping lives in the public repo `MarkAlfredcode/cron-utils` (`~/Projects/claude-window-ping`).

## Pipeline (per run)

all fetching in Python, no LLM (`fetch_campaigns.py`, `fetch_congress.py`, `fetch_officials.py`, `build_feds_input.py`, plus `fetch_*.py` extras) → `gate.py` skips the model if nothing is new → Haiku screens/triage (`screen_sam_docs.py`, `triage_*.py`, `extract_ja.py`; SAM line memory kept in the Actions cache) → `run_pass.py`, a one-turn `claude -p` pass (no tools, prompt caching off, effort medium) that writes content JSON → `mark_seen.py` → `scripts/assemble.py` (extras, Your Beat box from `data/watchlist.json`, archive, repeat removal via `data/surfaced-links.json`) → `scripts/render_<edition>.py` → `send_email.py` (Resend) → ntfy → commit manifests + `digest/<edition>.html` via `scripts/push_with_retry.sh`.

- AM editions email subscribers. PM editions are mostly website-only; Feds PM has never emailed.
- USAspending runs AM only; `assemble.py` carries AM sections into PM.
- DoD awards reach USAspending ~90 days late, hence the Defense backlog sweep in `usaspending_common.py`.
- Only commit the fixed allowlist of manifest/digest paths, never `git add -A`. Any `claude -p` run given Write can edit any file in the checkout, including prompts.
- Pipeline rebuilt 2026-10-12 (commit `85e9ab9`); the memory entry `project_dc_record.md` has the details.

## Secrets (GitHub Actions)

`CLAUDE_CODE_OAUTH_TOKEN` (bills the Claude account that generated it, shared with interactive use), `RESEND_API_KEY`, `FEC_KEYS` (api.data.gov pool), `DC_RECORD_RECIPIENTS_TOKEN` (shared with the site's `functions/dc-record-recipients.js` subscriber list), `SAM_API_KEY` (unused; SAM fetch is keyless). Values live in Mark's Claude memory, never in this repo.

## History and memory

- Why things are built the way they are: past Claude Code session transcripts in `~/.claude/projects/-Users-mark-Projects-dc-record/*.jsonl` (plus `-Users-mark/` and `-Users-mark-Projects-dc-record-scripts/`). Grep them for a file, script or error before rebuilding something. Kept for 10 years (`cleanupPeriodDays` in `~/.claude/settings.json`).
- Mark's broader Claude memory (all projects, rules, credentials): `~/.claude/projects/-Users-mark/memory/MEMORY.md`. The DC Record entry is `project_dc_record.md`.

## Known problems

- **GitHub Actions minutes:** the free 2,000/month ran out in September and is on pace to again. When blocked, runs instant-fail; fallback is a local `claude -p` run committed with `[skip ci]`. Mark plans to move the repo to a teammate's account or a free org (would need the Worker dispatch token and the site's `DC_RECORD_GH_TOKEN` updated).
- **Claude usage limit:** runs fail with "session limit" if interactive use already spent the window; re-dispatch after the reset.
- house.gov/senate.gov WAFs block Python's TLS fingerprint; those fetchers shell out to curl, 2 threads max. congress.gov 403s Python's default User-Agent. courtlistener.com, defense.gov are WAF/Akamai-blocked.
- OGE API is v3 (v2 returns HTML).

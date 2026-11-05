-- Mail to oge@markalfred.news (OGE Form 201 deliveries), logged by the foia-inbox Email Worker (added 2026-11-05).
-- Apply once: npx wrangler d1 execute foia-tracker --remote --file=schema_oge.sql
-- scripts/oge_inbox.py (dc-record, Officials workflow) reads rows with summarized_at IS NULL through the site's
-- /api/oge-queue, summarizes them, and writes summary + summarized_at back. The Officials edition shows the summaries
-- from the last 14 days in a section at its top; with none, there is no section.
CREATE TABLE IF NOT EXISTS oge_inbox (
  id            INTEGER PRIMARY KEY,
  received_at   TEXT NOT NULL,
  from_addr     TEXT,
  from_name     TEXT,
  to_addr       TEXT,
  subject       TEXT,
  message_id    TEXT,
  text          TEXT,          -- body (up to 30k chars)
  links         TEXT,          -- JSON array of http(s) links found in the body (oge.gov ones first)
  attachments   TEXT,          -- file names, '; '-joined
  ai_markers    TEXT,          -- JSON from aiMarkers() over body + attachment text, null when clean
  summary       TEXT,          -- JSON written by oge_inbox.py
  summarized_at TEXT,
  status        TEXT NOT NULL DEFAULT 'new'   -- new | summarized | noise
);
CREATE INDEX IF NOT EXISTS oge_inbox_pending ON oge_inbox(summarized_at, received_at);
CREATE TABLE IF NOT EXISTS oge_attachments (
  id       INTEGER PRIMARY KEY,
  oge_id   INTEGER NOT NULL REFERENCES oge_inbox(id),
  filename TEXT,
  mime     TEXT,
  size     INTEGER,
  status   TEXT,       -- read | no_text (scan) | skipped | error
  note     TEXT,
  text     TEXT,
  data     TEXT        -- base64 original, files up to ~1 MB (so the summarizer can OCR scans and read PDF link annotations)
);
CREATE INDEX IF NOT EXISTS oge_attachments_oge ON oge_attachments(oge_id);

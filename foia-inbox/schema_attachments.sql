-- Text read from email attachments by the foia-inbox Worker (added 2026-10-22).
-- Apply once: npx wrangler d1 execute foia-tracker --remote --file=schema_attachments.sql
CREATE TABLE IF NOT EXISTS inbox_attachments (
  id        INTEGER PRIMARY KEY,
  inbox_id  INTEGER NOT NULL REFERENCES inbox(id),
  filename  TEXT,
  mime      TEXT,
  size      INTEGER,
  status    TEXT,       -- read | no_text (scan, needs OCR) | skipped | error
  note      TEXT,
  text      TEXT
);
CREATE INDEX IF NOT EXISTS inbox_attachments_inbox ON inbox_attachments(inbox_id);

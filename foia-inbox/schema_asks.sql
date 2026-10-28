-- FOIA asks: Mark types a topic, contract or link in the tracker (Suggested > "Ask for a FOIA"); the next DC Record
-- Congress run (scripts/draft_asked_foias.py, AM and PM) drafts the request(s) and posts them as suggestions.
-- status: pending -> working (claimed by a run) -> drafted | failed; cancelled by Mark while pending.
CREATE TABLE IF NOT EXISTS asks (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at     TEXT NOT NULL,
  text           TEXT NOT NULL,
  link           TEXT,
  agency         TEXT,
  status         TEXT NOT NULL DEFAULT 'pending',
  note           TEXT,
  suggestion_ids TEXT,
  claimed_at     TEXT,
  done_at        TEXT
);
CREATE INDEX IF NOT EXISTS asks_status ON asks(status);

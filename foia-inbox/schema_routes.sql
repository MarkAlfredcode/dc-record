-- Where requests should go, learned from agency replies (learnRoute() in src/match.js, written by the Email Worker).
-- addr: the address we wrote to (or the sender, for mail not tied to a request); domain: its registrable domain.
-- kind: portal (file online at `portal`), office (send to `office` / `office_email`), hint (a transfer; shown only).
-- The tracker applies active rows to new suggestions and drafts; "Forget" sets active = 0.
CREATE TABLE IF NOT EXISTS routes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  addr TEXT NOT NULL UNIQUE,
  domain TEXT,
  agency TEXT,
  kind TEXT NOT NULL,
  portal TEXT,
  office TEXT,
  office_email TEXT,
  quote TEXT,
  inbox_id INTEGER,
  request_id INTEGER,
  learned_at TEXT,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS routes_domain ON routes(domain);

-- 2026-11-06: Claude's reading of agency mail (scripts/read_foia_replies.py + /api/foia-reads). Apply once:
--   npx wrangler d1 execute foia-tracker --remote --file schema_ai_read.sql
ALTER TABLE inbox ADD COLUMN ai_read_at TEXT;   -- when Claude last read this message; NULL = not yet read

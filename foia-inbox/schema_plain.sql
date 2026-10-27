-- 2026-10-27: plain-English reading of each agency reply (explainReply in src/match.js), and the plain
-- "what you'd get" line on FOIA suggestions. Apply once, before deploying the Worker and the site:
--   npx wrangler d1 execute foia-tracker --remote --file schema_plain.sql
ALTER TABLE inbox ADD COLUMN plain TEXT;          -- JSON {kind, headline, tone, priority, points[], quote}
ALTER TABLE suggestions ADD COLUMN gets TEXT;     -- one plain sentence: what the records would give Mark
CREATE INDEX IF NOT EXISTS inbox_message_id ON inbox(message_id);  -- the tracker joins correspondence to its reading

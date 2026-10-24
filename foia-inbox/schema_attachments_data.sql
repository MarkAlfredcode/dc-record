-- Original attachment files (<= 1 MB, pdf/docx/txt/html/csv), base64, kept so the tracker can re-attach them to an
-- emailed appeal or reply. Apply once: npx wrangler d1 execute foia-tracker --remote --file=schema_attachments_data.sql
ALTER TABLE inbox_attachments ADD COLUMN data TEXT;

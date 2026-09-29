-- Attachment size and content type, for databases created before them.
-- Mirrors db/schema.sql, which fresh installs use directly.
--
-- No backfill, and the reason is the same one 0006 gives for its own
-- absence: a synthesised value asserts something nobody observed. Size
-- could in principle be read off disk, but migrations here are SQL and
-- cannot stat a file; content_type could be guessed from file_path's
-- suffix, but a row written before the allow-list existed may carry an
-- arbitrary client-derived suffix, so guessing would manufacture a type
-- from exactly the input this change stops trusting.
--
-- So both stay NULL for existing rows, and NULL is reported as null.
-- Never 0 -- a plausible wrong size is worse than an absent one, because
-- nothing about it looks wrong.
ALTER TABLE ticket_attachments ADD COLUMN size_bytes INTEGER;
ALTER TABLE ticket_attachments ADD COLUMN content_type TEXT;

-- Archiving an import, for databases created before it existed.
-- Mirrors db/schema.sql, which fresh installs use directly.
--
-- Additive and reversible-by-nothing: existing rows get NULL, which is
-- exactly "active", so every row a database already holds keeps counting
-- toward every total and nothing changes for anyone who never archives.
ALTER TABLE transactions ADD COLUMN archived_at TIMESTAMP;
ALTER TABLE statements ADD COLUMN archived_at TIMESTAMP;
-- See schema.sql: distinguishes a hand edit from an import-time rule match.
ALTER TABLE transactions ADD COLUMN edited_at TIMESTAMP;

-- IF NOT EXISTS so that a database which somehow already has the view is
-- recorded as up to date rather than failing the whole migration.
CREATE VIEW IF NOT EXISTS active_transactions AS
    SELECT * FROM transactions WHERE archived_at IS NULL;

CREATE VIEW IF NOT EXISTS active_statements AS
    SELECT * FROM statements WHERE archived_at IS NULL;

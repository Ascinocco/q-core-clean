-- import_statement's duplicate check looks up (account_id, txn_date) once
-- per incoming row. Mirrors db/schema.sql, which fresh installs use; this
-- file is only ever applied to a database created before the index landed.
--
-- IF NOT EXISTS because one database already has it: it was applied by
-- hand on 2026-09-19, before the runner could deliver an index at all.
-- This migration records that database as up to date without touching it,
-- and creates the index anywhere it is genuinely absent.
CREATE INDEX IF NOT EXISTS idx_txn_dedup ON transactions(account_id, txn_date);

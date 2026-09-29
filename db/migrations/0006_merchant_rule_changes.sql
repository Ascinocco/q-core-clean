-- The merchant-rule audit trail, for databases created before it existed.
-- Mirrors db/schema.sql, which fresh installs use directly.
--
-- No backfill, deliberately. Rules that predate this table get no history,
-- and that is correct rather than a gap: the trail records changes made
-- from now on. A synthesised creation row would assert a time, an actor
-- and a starting value that nobody observed -- a fact in an audit log that
-- did not happen, which is worse than a rule whose history simply begins
-- when the recording did.
--
-- `rule_id` carries no REFERENCES clause on purpose. A deletion row must
-- outlive the rule it records, and a foreign key would either forbid the
-- delete or take the history with it -- in both cases the trail would be
-- unable to record the one event it most needs to.
CREATE TABLE IF NOT EXISTS merchant_rule_changes (
    id          TEXT PRIMARY KEY,
    rule_id     TEXT NOT NULL,
    changed_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    actor       TEXT NOT NULL,
    field       TEXT,
    old_value   TEXT,
    new_value   TEXT
);
CREATE INDEX IF NOT EXISTS idx_rule_changes_rule
    ON merchant_rule_changes(rule_id, changed_at);

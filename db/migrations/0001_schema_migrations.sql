-- Introduces the migration tracking table itself.
--
-- Chicken-and-egg: the runner needs this table to record anything, so it
-- creates it before applying any migration. This file therefore exists to
-- give that bootstrap a *version* — without it, a database created before
-- the migration mechanism has no pending migrations, so init_db finds it
-- incomplete (schema_migrations is in EXPECTED_TABLES), has nothing to
-- apply, and refuses to serve. IF NOT EXISTS because the runner has
-- already created it by the time this runs.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)

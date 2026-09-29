-- Canvas: artifacts gain the page and diagram kinds (ticket T-58).
--
-- Widening a CHECK needs a table rebuild, and this one has a trap.
-- `ALTER TABLE artifacts RENAME TO artifacts_pre_0015` silently rewrites
-- artifact_revisions' foreign key to REFERENCES "artifacts_pre_0015". Once
-- the old table is dropped, every revision insert fails at runtime with
-- "no such table: main.artifacts_pre_0015", because the app runs with
-- PRAGMA foreign_keys = ON (the runner does not, so nothing fails here).
-- The fix is to rebuild both tables: rename artifact_revisions aside first,
-- then artifacts, recreate both under their real names, copy, and drop both
-- old tables. Any later rebuild of artifacts must rebuild every table with
-- an FK to it the same way.
ALTER TABLE artifact_revisions RENAME TO artifact_revisions_pre_0015;
ALTER TABLE artifacts RENAME TO artifacts_pre_0015;
CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('daily', 'weekly', 'page', 'diagram')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL
);
CREATE TABLE artifact_revisions (
    artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    revision INTEGER NOT NULL,
    payload TEXT NOT NULL,
    actor TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (artifact_id, revision)
);
INSERT INTO artifacts (id, request_id, request_hash, kind, created_at, updated_at, revision)
    SELECT id, request_id, request_hash, kind, created_at, updated_at, revision FROM artifacts_pre_0015;
INSERT INTO artifact_revisions (artifact_id, revision, payload, actor, note, created_at)
    SELECT artifact_id, revision, payload, actor, note, created_at FROM artifact_revisions_pre_0015;
DROP TABLE artifact_revisions_pre_0015;
DROP TABLE artifacts_pre_0015;
CREATE INDEX idx_artifacts_created ON artifacts(created_at DESC, id DESC);

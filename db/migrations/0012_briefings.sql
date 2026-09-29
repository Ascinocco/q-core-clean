-- Private, revisioned documents and conversational review state.
CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('daily', 'weekly')),
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
CREATE TABLE review_items (
    id TEXT PRIMARY KEY,
    source_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    payload TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('open', 'deferred', 'resolved', 'dismissed')),
    revisit_date TEXT,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE review_item_history (
    item_id TEXT NOT NULL REFERENCES review_items(id),
    revision INTEGER NOT NULL,
    payload TEXT NOT NULL,
    state TEXT NOT NULL,
    revisit_date TEXT,
    actor TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (item_id, revision)
);
CREATE INDEX idx_artifacts_created ON artifacts(created_at DESC, id DESC);
CREATE INDEX idx_review_history_created ON review_item_history(created_at, item_id, revision);

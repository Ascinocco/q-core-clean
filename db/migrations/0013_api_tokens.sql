-- Named, scoped, revocable API tokens for client machines (split, part 2).
CREATE TABLE api_tokens (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    scope        TEXT NOT NULL CHECK (scope IN ('full', 'transcription')),
    token_hash   TEXT NOT NULL UNIQUE,
    prefix       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at   TEXT
);
CREATE UNIQUE INDEX idx_api_tokens_active_name ON api_tokens(name) WHERE revoked_at IS NULL;

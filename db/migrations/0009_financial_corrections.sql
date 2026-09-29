-- Append-only through the API. IDs are caller-supplied idempotency keys.
-- No target FK: history must survive a future deletion of its subject.
CREATE TABLE financial_corrections (
    id TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    target_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    request_json TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

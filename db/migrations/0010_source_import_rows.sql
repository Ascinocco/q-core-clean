CREATE TABLE source_import_rows (
    account_id TEXT NOT NULL REFERENCES entities(id),
    source_id TEXT NOT NULL,
    source_row TEXT NOT NULL,
    transaction_id TEXT NOT NULL REFERENCES transactions(id),
    input_json TEXT NOT NULL,
    decision TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (account_id, source_id, source_row),
    UNIQUE (account_id, source_id, transaction_id)
);

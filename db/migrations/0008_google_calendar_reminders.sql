-- Google Calendar projection metadata and richer reminder delivery fields.
ALTER TABLE reminders ADD COLUMN due_time TIME;
ALTER TABLE reminders ADD COLUMN location TEXT;
ALTER TABLE reminders ADD COLUMN notification_offsets_json TEXT NOT NULL DEFAULT '[1440,60]';

CREATE TABLE google_calendar_connection (
    id                 INTEGER PRIMARY KEY CHECK (id = 1),
    calendar_id        TEXT NOT NULL,
    account_email      TEXT,
    keychain_service   TEXT NOT NULL,
    connected_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_synced_at     TIMESTAMP,
    last_error         TEXT
);

CREATE TABLE google_calendar_events (
    reminder_id  TEXT PRIMARY KEY REFERENCES reminders(id) ON DELETE CASCADE,
    event_id     TEXT NOT NULL,
    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE reminders ADD COLUMN source_kind TEXT CHECK (source_kind IS NULL OR source_kind = 'birthday');
CREATE UNIQUE INDEX idx_reminders_birthday ON reminders(entity_id) WHERE source_kind = 'birthday';
CREATE TABLE google_calendar_occurrences (
    reminder_id TEXT NOT NULL REFERENCES reminders(id) ON DELETE CASCADE,
    due_date DATE NOT NULL,
    event_id TEXT NOT NULL,
    PRIMARY KEY (reminder_id, due_date)
);

-- Retained after local deletion so provider outages cannot orphan notifications.
CREATE TABLE google_calendar_deletions (
    calendar_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    PRIMARY KEY (calendar_id, event_id)
);

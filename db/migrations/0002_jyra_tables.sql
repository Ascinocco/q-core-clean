-- Jyra: boards, tickets, attachments and the transition log.
-- Mirrors the same DDL in db/schema.sql, which fresh installs use; this
-- file is only ever applied to a database created before Jyra landed.
-- A board hangs off any entity: a `project` entity for coding work, or a
-- vehicle/property for a maintenance backlog. Because `project` is an
-- entities.type rather than its own table, this is one real FK instead of
-- the polymorphic target_type/target_id pair note_links needs.
CREATE TABLE boards (
    id         TEXT PRIMARY KEY,
    entity_id  TEXT NOT NULL REFERENCES entities(id),
    title      TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_boards_entity ON boards(entity_id);

-- `status` is NOT patchable — see api/jyra.py. The only writers are the
-- transition endpoint and the atomic claim, both of which write a
-- ticket_transitions row in the same transaction.
-- claimed_by/claimed_at are deliberately denormalized from the newest
-- '-> agent_coding' transition so the atomic claim stays a single
-- conditional UPDATE and the stale sweep stays an indexed query.
CREATE TABLE tickets (
    id          TEXT PRIMARY KEY,
    board_id    TEXT NOT NULL REFERENCES boards(id),
    parent_id   TEXT REFERENCES tickets(id),
    type        TEXT NOT NULL,   -- solution | epic | story | bug | task
    title       TEXT NOT NULL,
    description TEXT,            -- markdown
    status      TEXT NOT NULL DEFAULT 'backlog',
    position    INTEGER,         -- order within (board_id, status); NULLS LAST
    claimed_by  TEXT,
    claimed_at  TIMESTAMP,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP
);
CREATE INDEX idx_tickets_board_status ON tickets(board_id, status, position);
CREATE INDEX idx_tickets_parent ON tickets(parent_id);
CREATE INDEX idx_tickets_claim ON tickets(status, claimed_at);

-- Metadata only; bytes live under data/jyra/<ticket_id>/, same split the
-- documents table uses. file_path is absolute so a coding agent can read a
-- screenshot directly with no fetch step.
CREATE TABLE ticket_attachments (
    id           TEXT PRIMARY KEY,
    ticket_id    TEXT NOT NULL REFERENCES tickets(id),
    filename     TEXT NOT NULL,
    file_path    TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    uploaded_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_attachments_ticket ON ticket_attachments(ticket_id);

-- Append-only audit log. `note` is nullable here only for the creation row
-- (from_status NULL -> 'backlog'), which has no "why"; the API requires it
-- on every other transition.
CREATE TABLE ticket_transitions (
    id          TEXT PRIMARY KEY,
    ticket_id   TEXT NOT NULL REFERENCES tickets(id),
    from_status TEXT,
    to_status   TEXT NOT NULL,
    actor       TEXT NOT NULL,
    note        TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_transitions_ticket ON ticket_transitions(ticket_id, created_at);

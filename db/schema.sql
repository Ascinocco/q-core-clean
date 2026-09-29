-- q-core schema — draft, iterating alongside design discussion.
-- See runbooks/decisions-log.md for the reasoning behind these choices.

-- ============================================================
-- Entities & relationships
-- ============================================================

-- Person, Property, Vehicle, Pet, Account (polymorphic/typed). Documents are
-- NOT an entity type — see the dedicated `documents` table below; they don't
-- need multi-hop relationships, a direct entity_id FK is enough.
-- Per-type shape of `attributes` is validated at the API layer (Pydantic
-- models), not in SQL — see runbooks/entity-attribute-schemas.md.
CREATE TABLE entities (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL,          -- 'person' | 'property' | 'vehicle' | 'pet' | 'account' | 'project'
    name        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active', -- active | inactive | sold | totaled | deceased | closed | archived
    attributes  TEXT,                    -- JSON blob for type-specific fields
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP
);

-- Edges between entities. Carries its own dates/attributes because some
-- relationships are point-in-time state (owns, insures) and some are
-- inherently temporal with their own terms (leases). No UNIQUE constraint:
-- cardinality expectations differ by relationship_type (an 'owns' edge
-- shouldn't duplicate; a 'leases' edge legitimately recurs on renewal), and
-- SQL can't express "unique unless dated" cleanly — dedup for the
-- non-temporal types is enforced at the API layer instead.
-- See runbooks/entity-attribute-schemas.md for the relationship_type vocabulary.
CREATE TABLE entity_relationships (
    id                TEXT PRIMARY KEY,
    from_entity_id    TEXT NOT NULL REFERENCES entities(id),
    to_entity_id      TEXT NOT NULL REFERENCES entities(id),
    relationship_type TEXT NOT NULL,     -- owns | resides_at | insures | finances | maintains | leases | spouse_of | parent_of
    start_date        DATE,              -- meaningful for leases/resides_at; NULL for point-in-time relationships
    end_date          DATE,
    attributes        TEXT               -- JSON blob, e.g. lease terms: {rent_amount, security_deposit}
);
CREATE INDEX idx_rel_from ON entity_relationships(from_entity_id);
CREATE INDEX idx_rel_to ON entity_relationships(to_entity_id);

-- ============================================================
-- Notes — freeform, attachable to anything
-- ============================================================

CREATE TABLE notes (
    id          TEXT PRIMARY KEY,
    body        TEXT NOT NULL,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP
);

-- Polymorphic link — target_id has no SQL foreign key since target_type
-- varies by row. Referential integrity (target actually exists) is
-- enforced at the API layer, not the DB. Standard trade-off for a
-- polymorphic association; acceptable at this scale.
CREATE TABLE note_links (
    id          TEXT PRIMARY KEY,
    note_id     TEXT NOT NULL REFERENCES notes(id),
    target_type TEXT NOT NULL,   -- 'entity' | 'transaction' | 'document' | 'reminder' | 'statement' | 'ticket'
    target_id   TEXT NOT NULL
);
CREATE INDEX idx_note_links_target ON note_links(target_type, target_id);
CREATE INDEX idx_note_links_note ON note_links(note_id);

-- ============================================================
-- Reminders
-- ============================================================

CREATE TABLE reminders (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    notes           TEXT,        -- plain description field on the reminder itself, distinct from the `notes` table below
    entity_id       TEXT REFERENCES entities(id),
    recurrence_rule TEXT,        -- NULL = one-off; else RRULE string
    start_date      DATE NOT NULL,
    end_date        DATE,
    due_time        TIME,        -- NULL = all-day Calendar event
    location        TEXT,
    notification_offsets_json TEXT NOT NULL DEFAULT '[1440,60]',
    source_kind     TEXT CHECK (source_kind IS NULL OR source_kind = 'birthday'),
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX idx_reminders_birthday ON reminders(entity_id) WHERE source_kind = 'birthday';

-- Sparse override table — NOT pre-materialized for every future
-- occurrence (that would reintroduce the performance problem lazy RRULE
-- expansion avoids). A row only exists here for an occurrence someone
-- actually completed or snoozed; GET /reminders/due expands RRULEs
-- lazily and LEFT JOINs this table, defaulting to 'pending' when no row
-- exists.
CREATE TABLE reminder_instances (
    id           TEXT PRIMARY KEY,
    reminder_id  TEXT NOT NULL REFERENCES reminders(id),
    due_date     DATE NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | done | snoozed | skipped
    completed_at TIMESTAMP,
    snoozed_to   DATE,
    UNIQUE(reminder_id, due_date)
);
CREATE INDEX idx_instances_due ON reminder_instances(due_date, status);

-- Google Calendar is a delivery projection, never the source of truth.
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

-- ============================================================
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

-- Financial: categories, statements & transactions
-- ============================================================

-- Fixed, two-level taxonomy (parent_id NULL = top-level). Category
-- describes the *kind* of spending, not the asset it's for — which
-- property/vehicle a charge belongs to is `transactions.entity_id`, not a
-- category fork (no separate "Rental Utilities" vs "Home Utilities").
-- Seed data: db/seed_categories.sql. See runbooks/category-taxonomy.md.
CREATE TABLE categories (
    id        TEXT PRIMARY KEY,     -- slug, e.g. 'housing_utilities'
    name      TEXT NOT NULL,        -- display name, e.g. 'Utilities'
    parent_id TEXT REFERENCES categories(id)  -- NULL for top-level
);
CREATE INDEX idx_categories_parent ON categories(parent_id);

CREATE TABLE statements (
    id           TEXT PRIMARY KEY,
    account_id   TEXT NOT NULL REFERENCES entities(id),
    period_start DATE NOT NULL,
    period_end   DATE NOT NULL,
    imported_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    -- Archived, never deleted: a financial record's value is that what was
    -- imported stays answerable afterwards. NULL means active.
    archived_at  TIMESTAMP,
    UNIQUE(account_id, period_start, period_end)
);

CREATE TABLE transactions (
    id            TEXT PRIMARY KEY,
    statement_id  TEXT NOT NULL REFERENCES statements(id),
    account_id    TEXT NOT NULL REFERENCES entities(id),
    txn_date      DATE NOT NULL,
    description   TEXT NOT NULL,
    amount_cents  INTEGER NOT NULL,      -- negative = debit, positive = credit
    category_id   TEXT REFERENCES categories(id),
    entity_id     TEXT REFERENCES entities(id),  -- optional link to Property/Vehicle/etc.
    archived_at   TIMESTAMP,                     -- NULL = active; see the view below
    -- Set when a human changes the category or entity link after import.
    -- Import-time auto-categorisation does NOT set it, which is the whole
    -- point: without this column "was this edited by hand" and "did a
    -- merchant rule match" are the same observation, and archiving could
    -- not honestly report what a re-import would discard.
    edited_at     TIMESTAMP
);
CREATE INDEX idx_txn_date ON transactions(txn_date);
CREATE INDEX idx_txn_entity ON transactions(entity_id, txn_date);
CREATE INDEX idx_txn_category ON transactions(category_id);
-- import_statement's duplicate check runs once per incoming row and looks
-- up (account_id, txn_date); without this every row scans transactions and
-- import cost grows with total history. None of the indexes above serve it:
-- idx_txn_date leads with the wrong column, idx_txn_entity with entity_id.
CREATE INDEX idx_txn_dedup ON transactions(account_id, txn_date);

-- Source occurrences are identities within a file, never inferred bank IDs.
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

-- THE scope every reader uses. Not a convenience: the ticket's own warning
-- is that unless every aggregate filters archived rows, archiving changes
-- nothing a user can see and the totals keep counting rows they were told
-- were removed. A filter repeated at each site is a filter that will be
-- missed at one site -- and the one that misses it is the aggregate added
-- next month, not one of today's. Reading through a view makes the
-- omission impossible rather than merely tested for.
--
-- The raw table remains reachable and is the deliberate opt-out, for the
-- two things that must see archived rows: an audit, and an explicit
-- fetch by id.
CREATE VIEW active_transactions AS
    SELECT * FROM transactions WHERE archived_at IS NULL;

CREATE VIEW active_statements AS
    SELECT * FROM statements WHERE archived_at IS NULL;

-- Recurring merchant -> category/entity auto-classification. When a
-- description matches multiple rules, the longest `pattern` wins (most
-- specific match), computed at match time — no manual priority to
-- maintain. See runbooks/merchant-rules-conventions.md.
CREATE TABLE merchant_rules (
    id           TEXT PRIMARY KEY,
    pattern      TEXT NOT NULL,          -- substring or regex on description
    category_id  TEXT REFERENCES categories(id),
    entity_id    TEXT REFERENCES entities(id)
);

-- Every create, update and delete of a merchant rule, written in the SAME
-- transaction as the change itself. A rule decides how spending is
-- classified, so "who changed this and from what" is not bookkeeping: an
-- unrecorded correction to classification logic is indistinguishable from
-- the rule always having said that.
--
-- ONE ROW PER CHANGED FIELD, not a JSON before/after pair. The question
-- this answers is per-field -- "who changed this rule's category, and
-- from what" -- which a row answers with a WHERE and a blob answers only
-- after a parse that can go wrong. It is also the shape
-- ticket_transitions already has, so the schema keeps one audit idiom
-- rather than making the next person choose between two.
--
-- A two-field PATCH therefore writes two rows. They share `changed_at`
-- and `actor`, which is enough to see they travelled together; no
-- explicit group key, because nothing needs to tell one two-field edit
-- from two one-field edits at the same instant (D52).
--
-- `field` is NULL for a creation row and for the terminal deletion row:
-- neither is a change TO a field, and inventing one would put a fact in
-- the log that did not happen.
CREATE TABLE merchant_rule_changes (
    id          TEXT PRIMARY KEY,
    rule_id     TEXT NOT NULL,
    changed_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    actor       TEXT NOT NULL,
    field       TEXT,
    old_value   TEXT,
    new_value   TEXT
);
CREATE INDEX idx_rule_changes_rule ON merchant_rule_changes(rule_id, changed_at);

-- ============================================================
-- Documents
-- ============================================================

-- Metadata only. The actual file lives on disk under data/documents/,
-- plaintext — full-disk encryption covers the at-rest
-- threat on this machine; see runbooks/decisions-log.md for the
-- per-file `age` encryption alternative that was considered and
-- rejected.
CREATE TABLE documents (
    id           TEXT PRIMARY KEY,
    entity_id    TEXT REFERENCES entities(id),
    title        TEXT NOT NULL,
    doc_type     TEXT,                   -- 'statement' | 'receipt' | 'tax' | 'insurance_policy' | 'lease' | 'title_deed' | 'warranty' | 'correspondence' | 'other'
    file_path    TEXT NOT NULL,          -- absolute path to the file under data/documents/ (plaintext)
    content_hash TEXT NOT NULL,          -- hash of plaintext, for de-dup/integrity checks
    imported_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_documents_entity ON documents(entity_id);

-- ============================================================
-- Schema migrations — which versioned deltas this database has
-- ============================================================

-- Present in schema.sql so a fresh install has it from the start; a fresh
-- bootstrap stamps every known migration as applied, since schema.sql
-- already describes the state those migrations build toward. Databases
-- created before this table existed get it via the migration runner
-- instead. See db/migrations/README.md.
CREATE TABLE schema_migrations (
    version    INTEGER PRIMARY KEY,   -- the NNNN prefix of db/migrations/NNNN_*.sql
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- Jyra — projects, boards, tickets
-- ============================================================

-- A board hangs off any entity: a `project` entity for coding work, or a
-- vehicle/property for a maintenance backlog. Because `project` is an
-- entities.type rather than its own table, this is one real FK instead of
-- the polymorphic target_type/target_id pair note_links needs.
--
-- Ticket keys (migration 0014): KA-12 is key_prefix, '-', tickets.number.
-- The prefix is unique and fixed once the board has had a ticket. The counter
-- only goes up, so a ticket number is never reused. The empty default
-- exists only because migration 0014 had to add the column NOT NULL, and
-- the API names a prefix on every insert. Written here rather than inside
-- the column list: SQLite 3.51.2's DROP COLUMN can fail with "incomplete
-- input" on a table whose column list carries comments.
CREATE TABLE boards (
    id         TEXT PRIMARY KEY,
    entity_id  TEXT NOT NULL REFERENCES entities(id),
    title      TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    key_prefix         TEXT NOT NULL DEFAULT '',
    next_ticket_number INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX idx_boards_entity ON boards(entity_id);
CREATE UNIQUE INDEX idx_boards_key_prefix ON boards(key_prefix);

-- `status` is NOT patchable — see api/jyra.py. The only writers are the
-- transition endpoint and the atomic claim, both of which write a
-- ticket_transitions row in the same transaction.
-- claimed_by/claimed_at are deliberately denormalized from the newest
-- '-> agent_coding' transition so the atomic claim stays a single
-- conditional UPDATE and the stale sweep stays an indexed query.
-- number is taken from boards.next_ticket_number in the creating
-- transaction (migration 0014).
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
    updated_at  TIMESTAMP,
    number      INTEGER
);
CREATE INDEX idx_tickets_board_status ON tickets(board_id, status, position);

-- Prefixes that issued keys and whose board let them go (in practice,
-- deleted), never issued again so a written-down key can never name a
-- different ticket (migration 0014). A never-used prefix is not recorded.
-- No foreign key: the board is gone.
CREATE TABLE retired_key_prefixes (
    prefix     TEXT PRIMARY KEY,
    board_id   TEXT NOT NULL,
    retired_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX idx_tickets_board_number ON tickets(board_id, number);
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
    -- Derived at upload and stored, never recomputed per list: a listing
    -- that stat()s every file turns one query into N, and a type
    -- recomputed at read time is a second derivation that can disagree
    -- with the first. Nullable only for rows written before these
    -- columns existed -- NULL means "not recorded", never 0 and never a
    -- guessed type.
    size_bytes   INTEGER,
    content_type TEXT,
    uploaded_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_attachments_ticket ON ticket_attachments(ticket_id);

-- Financial metadata corrections. No target FK: retain history independently.
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
-- Private, revisioned documents and conversational review state.
-- Any rebuild of artifacts must also rebuild every table with an FK to it
-- (artifact_revisions, and artifact_links from A3). See 0015_artifact_kinds.sql.
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
-- Links from an artifact to tickets and entities. Polymorphic like
-- note_links: target_id has no FK and the API keeps it honest. Entity delete
-- is refused while linked; ticket delete removes its links, never the artifact.
CREATE TABLE artifact_links (
    id          TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    target_type TEXT NOT NULL CHECK (target_type IN ('ticket', 'entity')),
    target_id   TEXT NOT NULL,
    actor       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (artifact_id, target_type, target_id)
);
CREATE INDEX idx_artifact_links_target ON artifact_links(target_type, target_id);
CREATE INDEX idx_review_history_created ON review_item_history(created_at, item_id, revision);

-- ============================================================
-- API tokens (split, part 2): named, scoped, revocable client credentials.
-- Only a SHA-256 of each token is stored; the secret is shown once at create.
-- The server's own service token (settings.api_token) is config, not a row.
-- ============================================================

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

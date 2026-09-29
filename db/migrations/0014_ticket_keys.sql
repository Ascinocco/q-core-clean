-- Human-readable ticket keys such as KA-12 (ticket T-19).
--
-- A key is the board's key_prefix, a hyphen and the ticket's number. It is
-- composed on read rather than stored, so there is one copy of each part.
-- The UUID stays the primary id: every link, trailer and history row that
-- names a ticket by UUID stays valid, and the key is an alias beside it.
--
--   boards.key_prefix          unique, ^[A-Z][A-Z0-9]{1,5}$, fixed once the
--                              board has had a ticket, and never reissued
--                              once it has issued a key and its board is
--                              gone (api/jyra.py enforces all three)
--   boards.next_ticket_number  the per-board counter create_ticket takes
--                              from. It only ever goes up, so a number is
--                              never reused, even after a delete.
--   tickets.number             assigned in the same transaction as the
--                              ticket's INSERT, unique per board.
--
-- key_prefix is NOT NULL with an empty default only because SQLite adds a
-- NOT NULL column solely with a constant default; rebuilding boards by
-- rename instead would repoint every foreign key that references it. The
-- empty string never survives: every existing row is filled below, and the
-- API names a prefix on every insert. tickets.number stays nullable for
-- the same ALTER reason and is likewise filled below and on every create.
ALTER TABLE boards ADD COLUMN key_prefix TEXT NOT NULL DEFAULT '';
ALTER TABLE boards ADD COLUMN next_ticket_number INTEGER NOT NULL DEFAULT 1;
ALTER TABLE tickets ADD COLUMN number INTEGER;

-- Prefixes that issued keys and whose board let them go (in practice,
-- deleted: a board that has had a ticket cannot rename). Never issued
-- again, by create_board, update_board or derivation, so a key written
-- down once can never come to name a different ticket. A prefix that
-- never issued a key is not recorded. No foreign key: the board is gone.
CREATE TABLE retired_key_prefixes (
    prefix     TEXT PRIMARY KEY,
    board_id   TEXT NOT NULL,
    retired_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- the owner's prefixes for the boards that existed when keys were decided
-- (2026-09-26, recorded on the ticket). A database without these boards --
-- every test database -- skips this and derives below.
UPDATE boards SET key_prefix = CASE id
    WHEN '1a1a1a1a-0000-4000-8000-000000000001' THEN 'KA'
    WHEN '1a1a1a1a-0000-4000-8000-000000000002' THEN 'KB'
    WHEN '1a1a1a1a-0000-4000-8000-000000000003' THEN 'KCX'
    WHEN '1a1a1a1a-0000-4000-8000-000000000004' THEN 'KD'
    WHEN '1a1a1a1a-0000-4000-8000-000000000005' THEN 'KE'
    WHEN '1a1a1a1a-0000-4000-8000-000000000006' THEN 'KF'
END
WHERE id IN (
    '1a1a1a1a-0000-4000-8000-000000000001',
    '1a1a1a1a-0000-4000-8000-000000000002',
    '1a1a1a1a-0000-4000-8000-000000000003',
    '1a1a1a1a-0000-4000-8000-000000000004',
    '1a1a1a1a-0000-4000-8000-000000000005',
    '1a1a1a1a-0000-4000-8000-000000000006'
);

-- Every other board derives its prefix from its entity's name, by the rule
-- api/jyra.py's derive_key_prefix implements for create_board:
--
--   words are the runs of ASCII letters and digits in the upper-cased name
--   two or more words: their initials, at most six ("Toyota Corolla" -> TC)
--   one word: its first three characters ("Canvas" -> CAN)
--   a leading digit or nothing at all gains a leading B, a single
--   character a trailing B, so the result always matches the pattern
--   a prefix already taken gains the smallest free number: CAN, CAN2, ...
--
-- test_the_migration_derives_prefixes_exactly_as_create_board_does runs
-- this SQL and that function over the same names and pins them equal. The
-- walk is one row per character, which is fine for entity names. Numbering
-- counts boards sharing a stem, so a numbered prefix could in principle
-- meet another entity whose name derives to that exact string (CAN2); the
-- unique index below then fails this whole migration, which leaves the
-- database untouched and names the index rather than serving duplicates.
WITH RECURSIVE
    walk(board_id, rest, previous_was_word, word_chars, initials) AS (
        SELECT b.id, upper(coalesce(e.name, '')), 0, '', ''
        FROM boards b LEFT JOIN entities e ON e.id = b.entity_id
        WHERE b.key_prefix = ''
        UNION ALL
        SELECT
            board_id,
            substr(rest, 2),
            substr(rest, 1, 1) GLOB '[A-Z0-9]',
            word_chars || CASE WHEN substr(rest, 1, 1) GLOB '[A-Z0-9]'
                               THEN substr(rest, 1, 1) ELSE '' END,
            initials || CASE WHEN substr(rest, 1, 1) GLOB '[A-Z0-9]'
                                  AND NOT previous_was_word
                             THEN substr(rest, 1, 1) ELSE '' END
        FROM walk
        WHERE rest <> ''
    ),
    stem(board_id, candidate) AS (
        SELECT board_id,
               CASE WHEN length(initials) >= 2 THEN substr(initials, 1, 6)
                    ELSE substr(word_chars, 1, 3) END
        FROM walk
        WHERE rest = ''
    ),
    lettered(board_id, candidate) AS (
        SELECT board_id,
               CASE WHEN candidate = '' OR substr(candidate, 1, 1) GLOB '[0-9]'
                    THEN substr('B' || candidate, 1, 6) ELSE candidate END
        FROM stem
    ),
    padded(board_id, base) AS (
        SELECT board_id,
               CASE WHEN length(candidate) < 2 THEN candidate || 'B'
                    ELSE candidate END
        FROM lettered
    ),
    ranked(board_id, base, occurrence) AS (
        SELECT p.board_id, p.base,
               ROW_NUMBER() OVER (PARTITION BY p.base ORDER BY b.created_at, b.id)
               + CASE WHEN EXISTS (
                     SELECT 1 FROM boards taken WHERE taken.key_prefix = p.base
                 ) OR EXISTS (
                     SELECT 1 FROM retired_key_prefixes r WHERE r.prefix = p.base
                 ) THEN 1 ELSE 0 END
        FROM padded p JOIN boards b ON b.id = p.board_id
    )
UPDATE boards
SET key_prefix = (
    SELECT CASE WHEN occurrence = 1 THEN base
                ELSE substr(base, 1, 6 - length(occurrence)) || occurrence END
    FROM ranked WHERE ranked.board_id = boards.id
)
WHERE key_prefix = '';

-- Existing tickets are numbered per board in creation order, id breaking
-- same-second ties, so keys read in the order the work was filed.
UPDATE tickets
SET number = ordered.n
FROM (
    SELECT id,
           ROW_NUMBER() OVER (PARTITION BY board_id ORDER BY created_at, id) AS n
    FROM tickets
) AS ordered
WHERE ordered.id = tickets.id;

UPDATE boards
SET next_ticket_number = 1 + coalesce(
    (SELECT MAX(number) FROM tickets WHERE tickets.board_id = boards.id), 0
);

CREATE UNIQUE INDEX idx_boards_key_prefix ON boards(key_prefix);
CREATE UNIQUE INDEX idx_tickets_board_number ON tickets(board_id, number);

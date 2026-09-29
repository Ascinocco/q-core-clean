"""Migration 0014: ticket keys on a database that predates them.

The database is built from today's schema and then taken back to the
pre-0014 shape (columns and indexes dropped, version un-stamped), so the
real runner applies the real file. Rows are invented.
"""

import sqlite3

from api.config import REPO_ROOT
from api.db import apply_startup_migrations, applied_migrations, init_db
from api.jyra import derive_key_prefix
from api.tests.test_migrations import _settings

KA = "1a1a1a1a-0000-4000-8000-000000000001"
KB = "1a1a1a1a-0000-4000-8000-000000000002"
KCX = "1a1a1a1a-0000-4000-8000-000000000003"
KD = "1a1a1a1a-0000-4000-8000-000000000004"
KE = "1a1a1a1a-0000-4000-8000-000000000005"
KF = "1a1a1a1a-0000-4000-8000-000000000006"
DECIDED = {KA: "KA", KB: "KB", KCX: "KCX", KD: "KD", KE: "KE", KF: "KF"}


#: What 0014 added to schema.sql, and what each becomes when stripped.
#: Stripping text rather than ALTER TABLE DROP COLUMN: on SQLite 3.51.2
#: (the server's Nix Python) DROP COLUMN fails with "incomplete input" on a
#: table whose column list carries comments. Same approach as
#: test_a_database_predating_the_attachment_columns_gains_them.
ADDED_BY_0014 = {
    "    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n"
    "    key_prefix         TEXT NOT NULL DEFAULT '',\n"
    "    next_ticket_number INTEGER NOT NULL DEFAULT 1\n": (
        "    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n"
    ),
    "    updated_at  TIMESTAMP,\n    number      INTEGER\n": "    updated_at  TIMESTAMP\n",
    "CREATE UNIQUE INDEX idx_boards_key_prefix ON boards(key_prefix);\n": "",
    "CREATE UNIQUE INDEX idx_tickets_board_number ON tickets(board_id, number);\n": "",
}
#: The table 0014 creates, cut out whole (its comment block and DDL).
RETIRED_TABLE_START = "-- Prefixes that issued keys and whose board let them go"
RETIRED_TABLE_END = "retired_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n);\n"


def _pre_0014(tmp_path):
    schema = (REPO_ROOT / "db" / "schema.sql").read_text()
    for added, before in ADDED_BY_0014.items():
        assert schema.count(added) == 1, f"0014's schema text moved: {added!r}"
        schema = schema.replace(added, before)
    start = schema.index(RETIRED_TABLE_START)
    end = schema.index(RETIRED_TABLE_END, start) + len(RETIRED_TABLE_END)
    schema = schema[:start] + schema[end:]
    old_schema = tmp_path / "pre-0014-schema.sql"
    old_schema.write_text(schema)

    settings = _settings(tmp_path, REPO_ROOT / "db" / "migrations")
    init_db(settings.model_copy(update={"schema_path": str(old_schema)}))
    connection = sqlite3.connect(settings.db_path)
    assert "key_prefix" not in {
        row[1] for row in connection.execute("PRAGMA table_info(boards)")
    }
    connection.execute("DELETE FROM schema_migrations WHERE version = 14")
    return settings, connection


def _add_board(connection, board_id, name, created_at):
    entity_id = f"entity-{board_id}"
    connection.execute(
        "INSERT INTO entities (id, type, name, status) VALUES (?, 'project', ?, 'active')",
        (entity_id, name),
    )
    connection.execute(
        "INSERT INTO boards (id, entity_id, title, created_at) VALUES (?, ?, 'B', ?)",
        (board_id, entity_id, created_at),
    )


def _add_ticket(connection, ticket_id, board_id, created_at):
    connection.execute(
        "INSERT INTO tickets (id, board_id, type, title, status, created_at) "
        "VALUES (?, ?, 'task', 'T', 'backlog', ?)",
        (ticket_id, board_id, created_at),
    )


def _migrate(settings, connection):
    connection.commit()
    connection.close()
    apply_startup_migrations(settings)
    assert 14 in applied_migrations(settings)
    migrated = sqlite3.connect(settings.db_path)
    migrated.row_factory = sqlite3.Row
    return migrated


def test_the_decided_boards_get_owners_prefixes(tmp_path):
    settings, connection = _pre_0014(tmp_path)
    for index, board_id in enumerate(DECIDED):
        _add_board(connection, board_id, f"project {index}", f"2026-09-2{index} 00:00:00")
    migrated = _migrate(settings, connection)
    prefixes = dict(migrated.execute("SELECT id, key_prefix FROM boards").fetchall())
    assert prefixes == DECIDED


def test_backfill_numbers_each_board_in_creation_order_with_id_breaking_ties(tmp_path):
    settings, connection = _pre_0014(tmp_path)
    _add_board(connection, KA, "Kite Atlas", "2026-09-25 08:00:00")
    _add_board(connection, KD, "workshop", "2026-09-25 03:00:00")
    # Inserted out of order on purpose: rowid order must not decide.
    _add_ticket(connection, "t-late", KA, "2026-09-26 10:00:00")
    _add_ticket(connection, "t-b", KA, "2026-09-25 09:00:00")
    _add_ticket(connection, "t-a", KA, "2026-09-25 09:00:00")  # same second: id wins
    _add_ticket(connection, "t-first", KA, "2026-09-25 08:30:00")
    _add_ticket(connection, "h-1", KD, "2026-09-26 00:00:00")
    migrated = _migrate(settings, connection)

    keys = dict(
        migrated.execute(
            "SELECT tickets.id, key_prefix || '-' || number FROM tickets "
            "JOIN boards ON boards.id = tickets.board_id"
        ).fetchall()
    )
    assert keys == {
        "t-first": "KA-1",
        "t-a": "KA-2",
        "t-b": "KA-3",
        "t-late": "KA-4",
        "h-1": "KD-1",
    }
    counters = dict(migrated.execute("SELECT key_prefix, next_ticket_number FROM boards"))
    assert counters == {"KA": 5, "KD": 2}


def test_a_migrated_board_continues_its_sequence_through_the_api(tmp_path, monkeypatch):
    settings, connection = _pre_0014(tmp_path)
    _add_board(connection, KA, "Kite Atlas", "2026-09-25 08:00:00")
    _add_ticket(connection, "t-1", KA, "2026-09-25 09:00:00")
    _add_ticket(connection, "t-2", KA, "2026-09-25 10:00:00")
    _migrate(settings, connection).close()

    from fastapi.testclient import TestClient

    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: settings
    try:
        client = TestClient(app)
        headers = {"Authorization": "Bearer test-token"}
        created = client.post(
            "/tickets",
            json={"board_id": KA, "type": "task", "title": "n", "actor": "t"},
            headers=headers,
        ).json()
        assert created["key"] == "KA-3"
        assert client.get("/tickets/ka-1", headers=headers).json()["id"] == "t-1"
    finally:
        app.dependency_overrides.clear()


def test_the_migration_derives_prefixes_exactly_as_create_board_does(tmp_path):
    """The SQL derivation and derive_key_prefix are two implementations of
    one rule (migrations stay SQL-only). Run both over the same boards,
    in creation order, and pin them equal -- including collisions with a
    decided prefix and with each other."""
    names = [
        "Canvas", "Canvas", "canvas", "k a", "Toyota Corolla", "", "---", "X",
        "2024 taxes", "42", "a b c d e f g h", "café au lait", "Straße",
        "  spaced   out  ", "tab\tseparated", "ÀB", "q-core",
    ]
    settings, connection = _pre_0014(tmp_path)
    _add_board(connection, KA, "Kite Atlas", "2026-01-01 00:00:00")
    for index, name in enumerate(names):
        _add_board(connection, f"b{index:02d}", name, f"2026-02-{index + 1:02d} 00:00:00")
    migrated = _migrate(settings, connection)

    from_sql = dict(migrated.execute("SELECT id, key_prefix FROM boards").fetchall())

    taken = {"KA"}
    from_python = {KA: "KA"}
    for index, name in enumerate(names):
        prefix = derive_key_prefix(name, taken)
        taken.add(prefix)
        from_python[f"b{index:02d}"] = prefix

    assert from_sql == from_python
    assert from_sql["b00"] == "CAN" and from_sql["b01"] == "CAN2"
    assert from_sql["b03"] == "KA2"


def test_the_migration_on_an_empty_jyra_changes_only_the_shape(tmp_path):
    settings, connection = _pre_0014(tmp_path)
    migrated = _migrate(settings, connection)
    columns = {row["name"] for row in migrated.execute("PRAGMA table_info(boards)")}
    assert {"key_prefix", "next_ticket_number"} <= columns
    indexes = {row[1] for row in migrated.execute("PRAGMA index_list(tickets)")}
    assert "idx_tickets_board_number" in indexes
    assert migrated.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

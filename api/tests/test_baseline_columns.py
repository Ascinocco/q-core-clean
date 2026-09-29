"""BASELINE_COLUMNS: the column analogue of BASELINE_TABLES (ticket T-29).

`test_every_table_added_since_the_baseline_has_a_migration` catches a
TABLE added to schema.sql with no migration. Nothing caught a COLUMN --
and `init_db` reaches migrations only when a table is missing, so an
added column is exactly what gets skipped. Measured before this existed:
a fabricated column in schema.sql with no migration left all 1413 tests
green, which is the `widgets` failure from PR #14 one level down.

Two halves, mirroring the table guard:

  * every column in today's schema.sql that is not in the baseline must
    be supplied by exactly one migration's ALTER TABLE ADD COLUMN;
  * every migration's ADD COLUMN must appear in schema.sql.

The baseline is the schema AS OF THE COMMIT THE RUNNER LANDED IN, not
today's file. Freezing today's would bake in any column already missing
a migration and the guard would be born vacuous -- passing cleanly while
measuring nothing. So it is derived mechanically from git rather than
remembered, and a literal copy is pinned against that derivation: the
literal is what the guards use, so they still run without git history,
and the derivation test is what proves the literal has not drifted.

BASELINE_TABLES was captured at this same commit, minus
`schema_migrations` -- the runner's own bookkeeping table, which by
definition no migration creates. Verified: the tables here and
BASELINE_TABLES differ by exactly that one name.
"""

import re
from pathlib import Path

import pytest

from api.config import REPO_ROOT

#: Known columns that predate this guard and have no migration. FROZEN --
#: see test_the_known_gaps_are_frozen. This is not a place to record new
#: gaps; a new one means writing the migration, exactly as BASELINE_TABLES
#: says of tables. Adding to this set to make the guard pass would silence
#: it, which is the failure it exists to catch.
#:
#: transactions.amount_cents is the one entry and is PERMANENT, not a
#: gap awaiting a migration. At the migration runner's introduction the column was
#: `amount NUMERIC`; today it is `amount_cents INTEGER`, and no SQL
#: migration can carry a database across: the conversion must be a no-op
#: where the column is already gone, and SQLite has no conditional DDL,
#: so a reference to `amount` fails at prepare time whether or not a row
#: would be touched.
#:
#: IF YOU ARE ABOUT TO WRITE 0008: it was written, and tried. Against a
#: database stamped to version 6 with no `amount` column it raised "no such column: amount", and
#: apply_startup_migrations is deliberately fatal, so it would have
#: REFUSED THAT DATABASE AT STARTUP. The migration is not missing
#: because nobody got to it.
#:
#: A Python migration class would express it and was rejected (ticket T-07):
#: every migration guard here parses SQL -- transaction control, table
#: parity, this baseline -- so the first Python migration would be the
#: excluded category where the hole is, added for a database nobody has.
#:
#: The gap is made LOUD instead of silent: apply_startup_migrations
#: refuses to serve a database in the stuck shape, naming the conversion.
#: See UnconvertedAmountColumnError and runbooks/decisions-log.md.
KNOWN_UNMIGRATED_COLUMNS = frozenset({("transactions", "amount_cents")})

#: Migrations permitted to use `CREATE TABLE IF NOT EXISTS`. FROZEN --
#: see test_the_conditional_create_exemptions_are_frozen.
#:
#: A versioned migration runs exactly once, guarded by schema_migrations,
#: so it does not need conditional DDL -- and conditional DDL is actively
#: harmful here: `CREATE TABLE IF NOT EXISTS x` is a NO-OP on a database
#: that already has `x`, so a column declared inside it never arrives,
#: while this guard reads the statement and counts it as supplied.
#: Silently green, which is how review-1 found it on #140 after merge.
#:
#: 0001 is structurally required: the runner creates schema_migrations
#: before applying any migration, so by the time 0001 runs the table is
#: always there and an unconditional CREATE would always fail.
#:
#: 0006 is historical. It had no stated reason and did not need one, but
#: it is already applied on real databases, and editing an applied
#: migration is worse than exempting it -- a database that has NOT
#: applied it would then get different SQL from one that has.
CONDITIONAL_CREATE_EXEMPTIONS = frozenset(
    {
        "0001_schema_migrations.sql",
        "0006_merchant_rule_changes.sql",
    }
)

#: `CREATE TABLE IF NOT EXISTS`, which this guard must not read as a
#: promise that the columns inside it will exist.
_CONDITIONAL_CREATE = re.compile(
    r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)", re.IGNORECASE
)


def _migration_files() -> list[Path]:
    paths = sorted((REPO_ROOT / "db" / "migrations").glob("*.sql"))
    assert paths, "no migrations found -- discovery is broken"
    return paths


_TABLE_BLOCK = re.compile(
    r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)\s*\((.*?)\n\);", re.DOTALL
)
_NOT_A_COLUMN = {"PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT", ")"}


def _columns_of(source: str) -> dict[str, tuple[str, ...]]:
    """Parse `table -> columns` from schema SQL.

    The same shape of parse BASELINE_TABLES uses for table names, one
    level down: table constraints are skipped by keyword, and a trailing
    comment is stripped before the first token is read.
    """
    parsed: dict[str, tuple[str, ...]] = {}
    for table, body in _TABLE_BLOCK.findall(source):
        columns = []
        for raw in body.split("\n"):
            line = raw.split("--")[0].strip()
            if not line:
                continue
            first = line.split()[0].strip(",").strip()
            if first.upper() in _NOT_A_COLUMN:
                continue
            if re.fullmatch(r"\w+", first):
                columns.append(first)
        parsed[table] = tuple(sorted(columns))
    return parsed


def _added_by_migrations() -> list[tuple[str, str]]:
    pattern = re.compile(r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)", re.IGNORECASE)
    found: list[tuple[str, str]] = []
    for path in sorted((REPO_ROOT / "db" / "migrations").glob("*.sql")):
        found.extend(pattern.findall(path.read_text()))
    return found


def _todays_columns() -> dict[str, tuple[str, ...]]:
    return _columns_of((REPO_ROOT / "db" / "schema.sql").read_text())


def supplied_columns(source: str) -> dict[str, tuple[str, ...]]:
    """Columns one migration's source actually SUPPLIES. The filter.

    A conditional CREATE promises nothing on a database that already has
    the table, so the columns inside it are not supplied. This is the
    #143 `_tables_created_by` shape -- a conditional read as
    unconditional -- reproduced in the guard written after it, and green
    until review-1 simulated it.

    ONE implementation, deliberately. review-2 found the previous version
    had this logic inline here AND retyped in the tests below, and the
    copy had already drifted: it compared `table in conditional` against
    a lowercased set where this compares `table.lower()`, so on
    `CREATE TABLE IF NOT EXISTS Thing` the copy said "supplies" and this
    said "does not". The rebuild tests ran against the copy, which is why
    they survived reverting the fix. Tests call this now; there is
    nothing left to diverge from.
    """
    conditional = {table.lower() for table in _CONDITIONAL_CREATE.findall(source)}
    return {
        table: columns
        for table, columns in _columns_of(source).items()
        if table.lower() not in conditional
    }


def _columns_created_by_migrations() -> dict[str, tuple[str, ...]]:
    """Columns each migration-CREATED table had when it was created.

    Needed because "was this column supplied by a migration?" has two
    answers: it was ALTERed in, or the CREATE TABLE that a migration runs
    already declared it. Without this half, every column on every table
    added since the baseline is unguarded -- which is most tables, and
    includes `ticket_attachments`, the table this ticket came from.
    """
    created: dict[str, tuple[str, ...]] = {}
    for path in _migration_files():
        source = path.read_text()
        if path.name in CONDITIONAL_CREATE_EXEMPTIONS:
            created.update(_columns_of(source))
            continue
        created.update(supplied_columns(source))
    return created


def _supplied_by_migrations(table: str, column: str) -> bool:
    """True if a migration would give an existing database this column."""
    if (table, column) in set(_added_by_migrations()):
        return True
    return column in _columns_created_by_migrations().get(table, ())


BASELINE_COLUMNS = {
    "categories": (
        "id",
        "name",
        "parent_id",
    ),
    "documents": (
        "content_hash",
        "doc_type",
        "entity_id",
        "file_path",
        "id",
        "imported_at",
        "title",
    ),
    "entities": (
        "attributes",
        "created_at",
        "id",
        "name",
        "status",
        "type",
        "updated_at",
    ),
    "entity_relationships": (
        "attributes",
        "end_date",
        "from_entity_id",
        "id",
        "relationship_type",
        "start_date",
        "to_entity_id",
    ),
    "merchant_rules": (
        "category_id",
        "entity_id",
        "id",
        "pattern",
    ),
    "note_links": (
        "id",
        "note_id",
        "target_id",
        "target_type",
    ),
    "notes": (
        "body",
        "created_at",
        "id",
        "updated_at",
    ),
    "reminder_instances": (
        "completed_at",
        "due_date",
        "id",
        "reminder_id",
        "snoozed_to",
        "status",
    ),
    "reminders": (
        "created_at",
        "end_date",
        "entity_id",
        "id",
        "notes",
        "recurrence_rule",
        "start_date",
        "title",
    ),
    "schema_migrations": (
        "applied_at",
        "version",
    ),
    "statements": (
        "account_id",
        "id",
        "imported_at",
        "period_end",
        "period_start",
    ),
    "transactions": (
        "account_id",
        "amount",
        "category_id",
        "description",
        "entity_id",
        "id",
        "row_hash",
        "statement_id",
        "txn_date",
    ),
}


def test_the_baseline_is_the_table_baseline_plus_the_runners_own_table():
    """The two baselines describe the same moment.

    If they drifted to different commits, a table could be in one and not
    the other and each guard would read the other's gap as intentional.
    `schema_migrations` is the runner's own bookkeeping, which by
    definition no migration creates.
    """
    from api.db import BASELINE_TABLES

    assert set(BASELINE_COLUMNS) - {"schema_migrations"} == set(BASELINE_TABLES)


def test_every_column_added_since_the_baseline_has_a_migration():
    """The guard. A column added to schema.sql alone leaves every
    pre-existing database without it, and nothing else notices.

    Covers EVERY table, not only baseline ones. The first version of this
    skipped tables added since the baseline, reasoning that their columns
    arrive with the table -- true only of the columns the table was
    created with. A column added later to a migration-created table needs
    its own migration just as much, and skipping those exempted
    `ticket_attachments`, the very table this ticket came from. The
    invented-column mutation passed against that version.
    """
    todays = _todays_columns()

    missing = []
    for table, columns in sorted(todays.items()):
        for column in columns:
            # Already there before migrations existed.
            if column in BASELINE_COLUMNS.get(table, ()):
                continue
            # ALTERed in, or declared by the migration that creates the
            # table. Both give an existing database the column.
            if _supplied_by_migrations(table, column):
                continue
            if (table, column) in KNOWN_UNMIGRATED_COLUMNS:
                continue
            missing.append(f"{table}.{column}")

    assert missing == [], (
        f"Columns in schema.sql with no migration: {missing}. "
        "Write the migration — do not add them to BASELINE_COLUMNS or to "
        "KNOWN_UNMIGRATED_COLUMNS."
    )


def test_every_column_a_migration_adds_is_in_the_schema():
    """Parity the other way, as the table guard has it.

    A column added by a migration but missing from schema.sql gives an
    upgraded database a column a fresh install does not have.
    """
    added = _added_by_migrations()
    todays = _todays_columns()

    assert added, "no ADD COLUMN found in any migration -- discovery is broken"

    missing = [
        f"{table}.{column}"
        for table, column in added
        if column not in todays.get(table, ())
    ]
    assert missing == [], f"added by migration but absent from schema.sql: {missing}"


def test_the_known_gaps_are_frozen():
    """KNOWN_UNMIGRATED_COLUMNS is history, not a waiting room.

    Without this, the guard above has an escape hatch: anyone can make a
    failure go away by naming the column here. This is the same reason
    test_baseline_tables_is_frozen_at_its_historical_size exists, and it
    is what makes the table guard still worth something.
    """
    assert KNOWN_UNMIGRATED_COLUMNS == frozenset(
        {("transactions", "amount_cents")}
    ), (
        "One is the number, permanently. The entry is not waiting for a "
        "migration -- no SQL migration can express the conversion, and a "
        "Python migration class was rejected because every migration "
        "guard here parses SQL (ticket T-07). Startup refuses the stuck "
        "shape instead, so the gap is loud rather than silent. A SECOND "
        "entry here is never the fix for anything: write the migration."
    )


def test_the_baseline_records_the_pre_rename_column():
    """The finding, pinned so it cannot be quietly resolved by editing
    the baseline instead of writing the migration."""
    assert "amount" in BASELINE_COLUMNS["transactions"]
    assert "amount_cents" not in BASELINE_COLUMNS["transactions"]
    assert "amount_cents" in _todays_columns()["transactions"]
    assert "amount" not in _todays_columns()["transactions"]


# --- conditional DDL in migrations (ticket T-30) --------------------------


def test_no_migration_uses_a_conditional_create_table():
    """A versioned migration runs exactly once. It does not need
    `IF NOT EXISTS`, and using it is actively harmful.

    `CREATE TABLE IF NOT EXISTS x` is a NO-OP where `x` already exists,
    so a column declared inside it never arrives on that database --
    while this guard reads the statement and, until ticket T-30, counted it
    as supplied. Green, and wrong, with nothing to say so.

    The two exemptions are frozen and reasoned; see
    CONDITIONAL_CREATE_EXEMPTIONS.
    """
    offenders = sorted(
        path.name
        for path in _migration_files()
        if path.name not in CONDITIONAL_CREATE_EXEMPTIONS
        and _CONDITIONAL_CREATE.search(path.read_text())
    )

    assert offenders == [], (
        f"Migrations using CREATE TABLE IF NOT EXISTS: {offenders}. A "
        "versioned migration runs once; drop the condition. Do not add "
        "the file to CONDITIONAL_CREATE_EXEMPTIONS -- that list is two "
        "historical entries, not a waiting room."
    )


def test_the_conditional_create_exemptions_are_frozen():
    """Without this the guard above has an escape hatch: name the file
    here and the failure goes away. Same reason
    test_baseline_tables_is_frozen_at_its_historical_size exists."""
    assert CONDITIONAL_CREATE_EXEMPTIONS == frozenset(
        {"0001_schema_migrations.sql", "0006_merchant_rule_changes.sql"}
    )


def test_a_conditional_create_does_not_supply_its_columns():
    """The bug itself, at the unit.

    Asserted on the helper rather than only through a simulation, so the
    property is pinned where it is implemented.
    """
    conditional = (
        "CREATE TABLE IF NOT EXISTS widgets (\n"
        "    id   TEXT PRIMARY KEY,\n"
        "    zz   TEXT\n"
        ");\n"
    )
    unconditional = conditional.replace("IF NOT EXISTS ", "")

    # The parser sees the columns either way -- the difference is not
    # whether they can be read, but whether they are PROMISED.
    assert "zz" in _columns_of(conditional)["widgets"]
    assert "zz" in _columns_of(unconditional)["widgets"]

    # Asserted on the excluding function itself. The previous version of
    # this test checked `_columns_of` and the regex and never called the
    # function that does the excluding -- so reverting the fix left it
    # green, which is what review-2 measured (1861 passed, 0 failed).
    assert supplied_columns(unconditional) == {"widgets": ("id", "zz")}
    assert supplied_columns(conditional) == {}

    # And case is part of it: the drifted copy compared a raw name to a
    # lowercased set, so a capitalised table slipped through.
    assert supplied_columns(conditional.replace("widgets", "Widgets")) == {}


# --- the rebuild pattern, decided and pinned (ticket T-30) ----------------
#
# DECISION: document the rename-aside order rather than teach the guard
# the other one. Measured: that is already the order the (rejected) 0008
# used, and the guard reads it correctly today. The ticket recorded the
# opposite -- that 0008 used the rename-into-place form and the guard
# would have blocked it -- and the simulation below is what showed
# otherwise. Teaching the parser to follow a RENAME would add a second
# way for it to be wrong, to support an order nothing here uses.


def _columns_supplied_for(source: str, table: str) -> tuple[str, ...]:
    """What the guard attributes to `table` from one migration.

    Delegates to `supplied_columns` rather than reimplementing it. The
    first version of this WAS a reimplementation and had already drifted
    from production in a way that mattered -- `table in conditional`
    against a lowercased set -- so the rebuild tests below were measuring
    the copy, not the guard, and survived reverting the fix.
    """
    return supplied_columns(source).get(table, ())


def test_a_rebuild_that_renames_the_old_table_aside_is_readable():
    """The documented order. The final CREATE TABLE carries the real
    name, so the guard attributes the new column without help."""
    source = (
        "ALTER TABLE thing RENAME TO thing_pre_change;\n"
        "CREATE TABLE thing (\n"
        "    id      TEXT PRIMARY KEY,\n"
        "    zz_new  TEXT\n"
        ");\n"
        "INSERT INTO thing (id) SELECT id FROM thing_pre_change;\n"
        "DROP TABLE thing_pre_change;\n"
    )

    assert "zz_new" in _columns_supplied_for(source, "thing")


def test_a_rebuild_that_renames_into_place_is_not_attributed():
    """The other order: correct SQL, unreadable to the guard.

    Pinned as a known limitation rather than left to be discovered. The
    guard goes RED on it, which is safe -- the README says to use the
    documented order, and the fix is never an exemption.
    """
    source = (
        "CREATE TABLE thing_new (\n"
        "    id      TEXT PRIMARY KEY,\n"
        "    zz_new  TEXT\n"
        ");\n"
        "INSERT INTO thing_new (id) SELECT id FROM thing;\n"
        "DROP TABLE thing;\n"
        "ALTER TABLE thing_new RENAME TO thing;\n"
    )

    assert _columns_supplied_for(source, "thing") == ()
    # The columns are visible, just under the interim name -- which is
    # why this fails loudly rather than passing over nothing.
    assert "zz_new" in _columns_supplied_for(source, "thing_new")


def test_the_readme_documents_the_rebuild_order():
    """The decision lives where a migration author will meet it.

    A rule recorded only in a test is a rule nobody reads before writing
    the thing it governs.
    """
    readme = (REPO_ROOT / "db" / "migrations" / "README.md").read_text()

    assert "RENAME TO thing_pre_change" in readme
    assert "CREATE TABLE IF NOT EXISTS" in readme
    assert "goes red" in readme.lower()

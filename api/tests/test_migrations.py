"""Migrations: upgrading a database that predates a schema change.

The property under test throughout is the one that makes this safe to add on
top of the bootstrap hardening: a *known, versioned* schema delta self-heals,
while genuinely unexpected incompleteness still fails loudly. Those are
different categories, and every test here pins one side or the other.
"""

import logging
import re
import shutil
import sqlite3
import threading
from pathlib import Path

import pytest

from api.config import REPO_ROOT, Settings
from api.db import (
    IncompleteDatabaseError,
    apply_startup_migrations,
    _discover_migrations,
    _statements,
    applied_migrations,
    init_db,
    pending_migrations,
)


def _settings(tmp_path: Path, migrations_dir: Path | None = None) -> Settings:
    return Settings(
        _env_file=None,
        api_token="test-token",
        intake_dir=str(tmp_path / "intake"),
        db_path=str(tmp_path / "q-core.db"),
        documents_dir=str(tmp_path / "documents"),
        # jyra_dir and logs_dir were omitted, so they defaulted to
        # REPO_ROOT/data/... and every run of this file created real
        # directories in the checkout. Found by the guard below on its
        # first arming — the same shape as intake_dir: settings that are
        # "test settings" and still point somewhere real.
        jyra_dir=str(tmp_path / "jyra"),
        logs_dir=str(tmp_path / "logs"),
        schema_path=str(REPO_ROOT / "db" / "schema.sql"),
        seed_categories_path=str(REPO_ROOT / "db" / "seed_categories.sql"),
        migrations_dir=str(
            migrations_dir if migrations_dir is not None else tmp_path / "migrations"
        ),
    )


def _write_migration(migrations_dir: Path, name: str, sql: str) -> None:
    migrations_dir.mkdir(parents=True, exist_ok=True)
    (migrations_dir / name).write_text(sql)


def _tables(db_path: str) -> set[str]:
    connection = sqlite3.connect(db_path)
    try:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        connection.close()


def _drop_table(db_path: str, table: str) -> None:
    """Simulate a database that predates the migration which adds `table`."""
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(f"DROP TABLE {table}")
        connection.commit()
    finally:
        connection.close()


def test_fresh_install_stamps_every_migration_as_applied(tmp_path):
    """A fresh database is built from schema.sql, which already contains
    everything every migration would add — so the migrations must be recorded
    as applied, not left pending to re-run CREATE TABLE over existing tables.
    """
    migrations = tmp_path / "migrations"
    _write_migration(
        migrations, "0001_widgets.sql", "CREATE TABLE widgets (id TEXT PRIMARY KEY);"
    )
    settings = _settings(tmp_path, migrations)

    init_db(settings)

    assert applied_migrations(settings) == [1]
    assert pending_migrations(settings) == []


def test_incomplete_database_is_upgraded_by_a_known_migration(tmp_path):
    """The case this whole mechanism exists for: a database created before a
    schema change is brought forward instead of being rejected.
    """
    settings = _settings(tmp_path)
    init_db(settings)
    _drop_table(settings.db_path, "documents")
    assert "documents" not in _tables(settings.db_path)

    _write_migration(
        Path(settings.migrations_dir),
        "0001_documents.sql",
        """CREATE TABLE documents (
            id TEXT PRIMARY KEY,
            entity_id TEXT REFERENCES entities(id),
            doc_type TEXT NOT NULL,
            file_path TEXT NOT NULL
        );""",
    )
    # Nothing recorded this migration, so it is pending against this database.
    assert pending_migrations(settings) == [1]

    # Migrations now apply at startup rather than inside init_db.
    apply_startup_migrations(settings)

    assert "documents" in _tables(settings.db_path)
    assert applied_migrations(settings) == [1]


def test_incomplete_database_still_raises_when_no_migration_supplies_it(tmp_path):
    """The hardening from #11/#12 must survive: incompleteness that no known
    migration explains is still a loud failure, not a silent repair.
    """
    settings = _settings(tmp_path)
    init_db(settings)
    _drop_table(settings.db_path, "documents")

    # A migration exists, but it supplies an unrelated table.
    _write_migration(
        Path(settings.migrations_dir),
        "0001_unrelated.sql",
        "CREATE TABLE unrelated (id TEXT PRIMARY KEY);",
    )

    with pytest.raises(IncompleteDatabaseError) as excinfo:
        init_db(settings)

    assert "documents" in str(excinfo.value)


def test_migrations_apply_in_version_order(tmp_path):
    """Later migrations may depend on earlier ones, so ordering is by numeric
    version — not filename string order, where 0010 sorts before 0002.
    """
    settings = _settings(tmp_path)
    init_db(settings)
    _drop_table(settings.db_path, "documents")

    migrations = Path(settings.migrations_dir)
    _write_migration(
        migrations,
        "0002_documents.sql",
        "CREATE TABLE documents (id TEXT PRIMARY KEY, note TEXT);",
    )
    # Depends on 0002 having run: string-sorted, "0010" < "0002" is false but
    # "0002" < "0010" is true, so this also pins that 10 is read as ten.
    _write_migration(
        migrations,
        "0010_documents_extra.sql",
        "ALTER TABLE documents ADD COLUMN extra TEXT;",
    )

    apply_startup_migrations(settings)

    assert applied_migrations(settings) == [2, 10]
    connection = sqlite3.connect(settings.db_path)
    try:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(documents)")
        }
    finally:
        connection.close()
    assert "extra" in columns


def test_applying_migrations_is_idempotent(tmp_path):
    """Startup runs on every restart. Re-running must not re-apply anything."""
    settings = _settings(tmp_path)
    init_db(settings)
    _drop_table(settings.db_path, "documents")
    _write_migration(
        Path(settings.migrations_dir),
        "0001_documents.sql",
        "CREATE TABLE documents (id TEXT PRIMARY KEY);",
    )

    apply_startup_migrations(settings)
    apply_startup_migrations(settings)
    apply_startup_migrations(settings)

    assert applied_migrations(settings) == [1]


def test_a_failing_migration_leaves_no_partial_version_recorded(tmp_path):
    """If a migration raises partway, it must not be recorded as applied —
    otherwise the next run skips it and the database stays half-upgraded.
    """
    settings = _settings(tmp_path)
    init_db(settings)
    _drop_table(settings.db_path, "documents")
    _write_migration(
        Path(settings.migrations_dir),
        "0001_broken.sql",
        "CREATE TABLE documents (id TEXT PRIMARY KEY);\n"
        "CREATE TABLE documents (id TEXT PRIMARY KEY);",  # duplicate: fails
    )

    with pytest.raises(sqlite3.Error):
        apply_startup_migrations(settings)

    assert applied_migrations(settings) == []
    assert "documents" not in _tables(settings.db_path)


def test_concurrent_startup_applies_a_migration_exactly_once(tmp_path):
    """Startup is once per process, but not once per machine.

    Two processes can start together — launchd restarting while a manual
    uvicorn is up, or a redeploy overlapping — and both find the same
    pending version. BEGIN IMMEDIATE serializes them; the loser re-reads
    schema_migrations inside its own transaction and skips.
    """
    settings = _settings(tmp_path)
    init_db(settings)
    _drop_table(settings.db_path, "documents")
    _write_migration(
        Path(settings.migrations_dir),
        "0001_documents.sql",
        "CREATE TABLE documents (id TEXT PRIMARY KEY);",
    )

    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        try:
            barrier.wait()
            apply_startup_migrations(settings)
        except BaseException as exc:  # noqa: BLE001 - recorded and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert applied_migrations(settings) == [1]
    assert "documents" in _tables(settings.db_path)


def test_fresh_install_of_the_repo_schema_leaves_nothing_pending(tmp_path):
    """A fresh install records every shipped migration as applied.

    Narrow on purpose. This says nothing about whether those migrations
    *cover* schema.sql — a fresh install stamps whatever migrations exist, so
    this assertion holds even when a table has been added with no migration
    for it. That gap is what
    test_every_table_added_since_the_baseline_has_a_migration exists for; an
    earlier version of this test claimed to cover it and did not.
    """
    settings = _settings(tmp_path, REPO_ROOT / "db" / "migrations")

    init_db(settings)

    assert pending_migrations(settings) == []


def test_every_table_added_since_the_baseline_has_a_migration(tmp_path):
    """Every table added since the runner landed must be creatable BOTH ways:
    from schema.sql for a fresh install, and from a migration for an existing
    database.

    A table added to only schema.sql leaves the whole suite green while every
    pre-existing database — with real data in it — starts raising
    IncompleteDatabaseError on every request. That is the exact failure this
    mechanism was built to prevent, and it has already been reached twice: the
    schema_migrations near-miss during implementation, and review-1's widgets
    simulation on PR #14, which 119 passing tests did not notice.

    Compares against a frozen BASELINE_TABLES rather than deriving the "before"
    state, because it cannot be derived — distinguishing "added without a
    migration" from "always been there" requires knowing what the schema held
    beforehand, and the old schema.sql no longer exists. A purely behavioural
    version of this check (drop every migration-created table, assert init_db
    restores the database) was measured against the widgets bug and passed,
    because a table with no migration is never dropped and so never looks
    missing.
    """
    from api.db import BASELINE_TABLES, EXPECTED_TABLES, _tables_created_by

    settings = _settings(tmp_path, REPO_ROOT / "db" / "migrations")
    versions = [version for version, _ in _discover_migrations(settings)]

    added_since_baseline = EXPECTED_TABLES - BASELINE_TABLES
    supplied_by_migrations = _tables_created_by(settings, versions)

    assert added_since_baseline == supplied_by_migrations, (
        "Tables in schema.sql with no migration: "
        f"{sorted(added_since_baseline - supplied_by_migrations)}. "
        "Write the migration — do not add them to BASELINE_TABLES."
    )


def test_baseline_tables_is_a_subset_of_expected_tables(tmp_path):
    """BASELINE_TABLES is history: every table that existed then still exists.

    If a baseline table were ever genuinely dropped, this fails and forces the
    removal to be handled deliberately, since the guard above subtracts these
    two sets and would otherwise silently stop covering something.
    """
    from api.db import BASELINE_TABLES, EXPECTED_TABLES

    assert BASELINE_TABLES <= EXPECTED_TABLES


def test_baseline_tables_is_frozen_at_its_historical_size():
    """BASELINE_TABLES records the schema as it was when the runner landed, so
    its size is fixed forever.

    The guard above compares EXPECTED_TABLES against it, which means the guard
    can be silenced by adding the new table here instead of writing a
    migration — the one thing its failure message says not to do. This makes
    that silencing fail on its own.
    """
    from api.db import BASELINE_TABLES

    assert len(BASELINE_TABLES) == 11


def test_database_predating_the_migration_mechanism_is_upgraded(tmp_path):
    """The end-to-end upgrade, against the real db/migrations/.

    Simulates a database as it stood before any of this existed — the
    BASELINE_TABLES set, with no schema_migrations and none of the tables
    later migrations add — and asserts every shipped migration brings it
    forward with its rows intact.

    Built from BASELINE_TABLES rather than by dropping one table, because
    "current schema minus the tracking table" is not a state that can occur:
    a database predating the runner also predates everything the runner has
    since added. Simulating it that way made this test fail once a second
    migration existed, by asking migration 0002 to create tables the fixture
    had left in place.

    The same drift bit again with migration 0004, which DROPs a column:
    the fixture builds from current schema.sql, which no longer has
    row_hash, so replaying 0004 asked SQLite to drop a column that was
    never there. A database old enough to predate the runner would have
    had it. The column is restored below for the same reason the tables
    are removed — the fixture has to be the old state, not the new one
    with pieces moved around.

    Migration 0005 is the third instance of the same drift, in the other
    direction: it ADDs archived_at and creates two views, all of which
    current schema.sql now has, so replaying it asked SQLite to add a
    column that was already there. Removed below. Three times now this
    fixture has needed a hand-written undo of the newest change, which is
    the cost of building "the old state" from the current schema — worth
    naming, because the next migration will need it too and the failure
    arrives as a confusing duplicate-column error rather than as a note
    that the fixture is stale.
    """
    from api.db import BASELINE_TABLES, EXPECTED_TABLES

    settings = _settings(tmp_path, REPO_ROOT / "db" / "migrations")
    init_db(settings)
    connection = sqlite3.connect(settings.db_path)
    try:
        connection.execute(
            "INSERT INTO entities (id, type, name, status) "
            "VALUES ('e1', 'vehicle', 'Toyota', 'active')"
        )
        for table in sorted(EXPECTED_TABLES - BASELINE_TABLES):
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        # Predates migration 0004, which drops it.
        connection.execute("ALTER TABLE transactions ADD COLUMN row_hash TEXT")
        # Predates migration 0005, which adds these.
        connection.execute("DROP VIEW IF EXISTS active_transactions")
        connection.execute("DROP VIEW IF EXISTS active_statements")
        connection.execute("ALTER TABLE transactions DROP COLUMN archived_at")
        connection.execute("ALTER TABLE transactions DROP COLUMN edited_at")
        connection.execute("ALTER TABLE statements DROP COLUMN archived_at")
        connection.execute("DROP INDEX idx_reminders_birthday")
        connection.execute("ALTER TABLE reminders DROP COLUMN source_kind")
        connection.execute("ALTER TABLE reminders DROP COLUMN due_time")
        connection.execute("ALTER TABLE reminders DROP COLUMN location")
        connection.execute("ALTER TABLE reminders DROP COLUMN notification_offsets_json")
        connection.commit()
    finally:
        connection.close()

    assert _tables(settings.db_path) >= BASELINE_TABLES
    assert pending_migrations(settings) == [
        version for version, _ in _discover_migrations(settings)
    ]

    apply_startup_migrations(settings)

    assert EXPECTED_TABLES <= _tables(settings.db_path)
    assert applied_migrations(settings) == [
        version for version, _ in _discover_migrations(settings)
    ]
    connection = sqlite3.connect(settings.db_path)
    try:
        assert connection.execute("SELECT name FROM entities").fetchone()[0] == "Toyota"
    finally:
        connection.close()


def test_migration_files_survive_a_semicolon_in_a_comment(tmp_path):
    """A `;` inside a `--` comment must not split the file mid-sentence.

    Regression: the first real migration's header comment contained "use;",
    which the splitter cut in two and handed the remaining prose to SQLite as
    a statement (`near "this": syntax error`).
    """
    settings = _settings(tmp_path)
    init_db(settings)
    _drop_table(settings.db_path, "documents")
    _write_migration(
        Path(settings.migrations_dir),
        "0001_documents.sql",
        "-- Mirrors schema.sql, which fresh installs use; this file is for\n"
        "-- databases that predate it.\n"
        "CREATE TABLE documents (id TEXT PRIMARY KEY);",
    )

    apply_startup_migrations(settings)

    assert "documents" in _tables(settings.db_path)
    assert applied_migrations(settings) == [1]


def test_every_repo_migration_is_reachable_by_the_runner(tmp_path, monkeypatch):
    """Replaces test_repo_migrations_are_table_additive.

    That test asserted every migration creates a table, because the old
    runner could reach no others: init_db returned on the "ready" branch
    before looking, so a column- or index-only file existed, passed the
    suite, and never executed. The assertion was a workaround for the
    limitation, not a property worth keeping.

    The property it was really protecting is: no file in db/migrations/ is
    unreachable. The runner no longer filters by what a file creates, so
    this asserts the selection directly — every discovered version is
    handed to the applier, in version order — rather than dictating a shape
    each file must take.

    Selection rather than execution, because the repo's migrations are not
    re-runnable against a database that already has their tables; requiring
    them to be would be the same mistake in a new place.
    """
    import api.db

    settings = _settings(tmp_path, REPO_ROOT / "db" / "migrations")
    init_db(settings)
    _clear_applied_versions(settings.db_path)

    discovered = [version for version, _ in _discover_migrations(settings)]
    assert discovered, "no migrations found — the directory or glob is wrong"
    assert discovered == sorted(discovered)

    handed_to_applier: list[int] = []
    monkeypatch.setattr(
        api.db,
        "_apply_migrations",
        lambda _settings, versions: handed_to_applier.extend(versions),
    )

    apply_startup_migrations(settings)

    assert handed_to_applier == discovered


def _clear_applied_versions(db_path: str) -> None:
    """Make every migration pending again without touching the schema."""
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("DELETE FROM schema_migrations")
        connection.commit()
    finally:
        connection.close()


TRANSACTION_CONTROL = {"BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE"}

#: A trigger body is `... BEGIN <statements> END`, so it contains the two
#: keywords this guard rejects. When the file defines a trigger, a flagged
#: BEGIN/END is far more likely to be that body torn apart by the splitter
#: than an author writing transaction control by hand.
CREATE_TRIGGER = re.compile(r"\bCREATE\s+TRIGGER\b", re.IGNORECASE)

#: Why a statement was flagged. The guard rejects both, but they are
#: different mistakes with different fixes, and naming the wrong one sends
#: the reader off to rewrite SQL they never wrote (ticket T-48).
SPLIT_TRIGGER_BODY = "split trigger body"
AUTHORED_TRANSACTION_CONTROL = "authored transaction control"


def _transaction_control_offenders(statements: list[str]) -> list[tuple[str, str]]:
    """Flagged statements paired with the cause, decided PER STATEMENT.

    `_statements` splits on `;`, so a trigger body's internal semicolons
    break it into pieces: the `CREATE TRIGGER ... BEGIN ...` head is
    truncated mid-statement and the trailing `END` arrives on its own. The
    file IS broken either way -- the truncated head would fail at execution
    -- but it is not broken because someone managed a transaction.

    ONLY `END` can arrive that way, and that is what makes the split
    decidable. A trigger body's opening `BEGIN` stays attached to its
    `CREATE TRIGGER` and never leads a statement of its own, so a standing
    `BEGIN`/`COMMIT`/`ROLLBACK`/`SAVEPOINT`/`RELEASE` was written by a
    person no matter what else the file contains.

    This was first written per FILE, and review-1 found what that costs:
    a migration with a real `BEGIN;` ... `COMMIT;` AND a trigger labelled
    every offender "split trigger body" and told the author "you probably
    did not write transaction control at all" -- while they were looking
    at their own `BEGIN;`. Worse than the message this replaced, because
    it contradicts the truth confidently.
    """
    defines_trigger = any(CREATE_TRIGGER.search(statement) for statement in statements)
    offenders = []
    for statement in statements:
        words = statement.split()
        if not words:
            continue
        leading = words[0].upper()
        if leading not in TRANSACTION_CONTROL:
            continue
        cause = (
            SPLIT_TRIGGER_BODY
            if leading == "END" and defines_trigger
            else AUTHORED_TRANSACTION_CONTROL
        )
        offenders.append((statement, cause))
    return offenders


def _offender_message(offenders: list[tuple[str, str, str]]) -> str:
    """Build the failure text, naming only the causes actually present."""
    causes = {cause for _name, _statement, cause in offenders}
    listing = ", ".join(
        f"{name}: {statement[:60]!r} ({cause})"
        for name, statement, cause in offenders
    )
    parts = [f"migration files must not manage transactions themselves: {listing}"]
    if AUTHORED_TRANSACTION_CONTROL in causes:
        parts.append(
            "-- _apply_migrations already wraps the whole file in one "
            "BEGIN IMMEDIATE ... COMMIT, and a file that commits mid-way "
            "breaks the atomicity init_db's re-read depends on"
        )
    if SPLIT_TRIGGER_BODY in causes:
        parts.append(
            "-- but this file defines a trigger, so the likelier cause is "
            "a semicolon INSIDE the trigger body: the runner splits on ';', "
            "which truncates the CREATE TRIGGER statement and leaves its "
            "closing END standing alone. You probably did not write "
            "transaction control at all. See db/migrations/README.md, "
            "'No semicolons inside string literals or trigger bodies'"
        )
    return " ".join(parts)


def test_repo_migrations_contain_no_transaction_control(tmp_path):
    """A migration file must not open or close a transaction of its own.

    This and test_a_migrations_ddl_and_its_record_commit_in_one_transaction
    guard the same invariant from the two sides it can break from, and
    neither can see the other's side:

    - That test watches the *runner*. It catches a change to
      _apply_migrations that commits per statement, or that splits the DDL
      from its schema_migrations row.
    - This test watches the *files*. The runner can be perfect and a
      migration containing a bare `COMMIT;` still ends the span early --
      the DDL before it commits alone, and the schema_migrations row lands
      in a separate transaction. That is precisely the half-applied state
      init_db's stale-state re-read (a todo) assumes cannot exist, so
      the correctness argument for that fix silently stops holding.

    The reason this needs a test at all is that the constraint is invisible
    from inside the file being written: a migration author sees plain SQL,
    where explicit BEGIN/COMMIT is ordinary and often good practice. Nothing
    in the file says "your statements are already inside someone else's
    transaction". The two shipped migrations are clean today by accident,
    not by anyone having checked.

    Parsed through _statements rather than grepped over raw text, so a
    comment mentioning COMMIT is not a false positive -- that function
    strips `--` comments before splitting, which is the same view of the
    file the runner itself acts on.
    """
    settings = _settings(tmp_path, REPO_ROOT / "db" / "migrations")

    offenders = []
    for _version, path in _discover_migrations(settings):
        for statement, cause in _transaction_control_offenders(
            _statements(path.read_text())
        ):
            offenders.append((path.name, statement, cause))

    assert not offenders, _offender_message(offenders)


def test_a_racers_completed_migration_is_not_reported_as_incomplete(
    tmp_path, monkeypatch
):
    """A database another thread just migrated must not be called unusable.

    The deterministic form of test_concurrent_startup_applies_a_migration_
    exactly_once's intermittent failure. That test reproduces this about 1
    run in 7 in isolation and 1 in 4 under load; this one reproduces it
    every time, because it forces the interleaving rather than hoping for
    it.

    The race: init_db reads `state` once, then acts on it ~30 lines later.
    Between those, a racer can apply the pending migration. The loser then
    finds `pending_migrations` empty -- the racer already recorded it --
    skips the apply branch, and falls through to `raise _unusable_error(
    state, ...)` on the *stale* verdict, against a database that is healthy
    by then. The giveaway in the real failures is the message: "missing
    required tables: unknown", because the error recomputes the missing set
    when raising, finds nothing missing, and raises anyway.

    Only the first `_database_state` call is faked. Everything else -- the
    pending list, the tables, the tracking table -- is the real, current
    database, so this asserts the actual bug rather than a mock of it.
    """
    import api.db

    settings = _settings(tmp_path)
    init_db(settings)
    _drop_table(settings.db_path, "documents")
    _write_migration(
        Path(settings.migrations_dir),
        "0001_documents.sql",
        "CREATE TABLE documents (id TEXT PRIMARY KEY);",
    )

    # The racer applies it, fully and successfully. (Another process
    # starting up, now that migrations live there rather than in init_db.)
    apply_startup_migrations(settings)
    assert applied_migrations(settings) == [1]
    assert "documents" in _tables(settings.db_path)

    real_database_state = api.db._database_state
    calls = {"count": 0}

    def stale_on_first_read(db_file):
        calls["count"] += 1
        if calls["count"] == 1:
            return "incomplete"
        return real_database_state(db_file)

    monkeypatch.setattr(api.db, "_database_state", stale_on_first_read)

    # Must not raise: the database is healthy, whatever this caller read
    # before the racer got there.
    init_db(settings)


@pytest.mark.parametrize("statement_count", [1, 5])
def test_a_migrations_ddl_and_its_record_commit_in_one_transaction(
    tmp_path, statement_count
):
    """The DDL and the schema_migrations row must land together.

    This is the invariant init_db's stale-state re-read rests on. Because
    the two commit atomically, a caller re-reading the database state sees
    strictly before or strictly after a migration -- never a half-applied
    in-between -- which is what makes that fix close the race by
    construction instead of narrowing it.

    Split them into two commits and the guarantee quietly degrades to a
    probability argument, with nothing failing to say so. Asserted
    structurally (the statement sequence) rather than by racing an observer
    thread, so it fails deterministically rather than occasionally.
    """
    import sqlite3 as _sqlite3

    import api.db

    settings = _settings(tmp_path)
    init_db(settings)
    _drop_table(settings.db_path, "documents")
    # Both shapes matter. A real migration is usually several statements,
    # and _statements() splits them so each is executed separately -- so
    # "one transaction" has to hold across N executes, not just one. The
    # single-statement case would pass even if the runner committed per
    # statement, which is exactly the shape this guard exists to reject.
    tables = [f"_pad_{i}" for i in range(statement_count - 1)]
    sql = ["CREATE TABLE documents (id TEXT PRIMARY KEY);"]
    sql += [f"CREATE TABLE {name} (id TEXT PRIMARY KEY);" for name in tables]
    _write_migration(
        Path(settings.migrations_dir), "0001_documents.sql", "\n".join(sql)
    )

    executed: list[str] = []
    real_connect = _sqlite3.connect

    def recording_connect(*args, **kwargs):
        # set_trace_callback rather than wrapping .execute: that attribute
        # is read-only on sqlite3.Connection, and the callback sees every
        # statement the connection runs, BEGIN and COMMIT included.
        connection = real_connect(*args, **kwargs)
        connection.set_trace_callback(
            lambda sql: executed.append(" ".join(str(sql).split())[:60])
        )
        return connection

    api.db.sqlite3.connect = recording_connect
    try:
        api.db._apply_migrations(settings, [1])
    finally:
        api.db.sqlite3.connect = real_connect

    def index_of(fragment: str) -> int:
        matches = [i for i, sql in enumerate(executed) if fragment in sql.upper()]
        assert matches, f"no statement containing {fragment!r} in {executed}"
        return matches[0]

    def last_index_of(fragment: str) -> int:
        matches = [i for i, sql in enumerate(executed) if fragment in sql.upper()]
        assert matches, f"no statement containing {fragment!r} in {executed}"
        return matches[-1]

    begin = index_of("BEGIN IMMEDIATE")
    record = index_of("INSERT INTO SCHEMA_MIGRATIONS")
    commit = last_index_of("COMMIT")

    # EVERY statement in the migration, not just the first: a runner that
    # committed per statement would still put the first one inside the span.
    ddl_indexes = [index_of("CREATE TABLE DOCUMENTS")] + [
        index_of(f"CREATE TABLE {name.upper()} ") for name in tables
    ]
    assert all(begin < ddl < record for ddl in ddl_indexes), (
        f"every DDL statement must sit between BEGIN and the "
        f"schema_migrations row; got {executed}"
    )
    assert begin < record < commit, (
        "DDL and its schema_migrations row must be inside one "
        f"BEGIN..COMMIT; got {executed}"
    )
    between = [sql for sql in executed[begin + 1 : record] if "COMMIT" in sql.upper()]
    assert not between, f"a COMMIT separates the DDL from its record: {executed}"


# --- migrations apply at startup, not per request (a todo) ------------


def _column_migration(migrations_dir: Path) -> None:
    """A migration that creates no table — the case the old runner could not
    reach at all."""
    _write_migration(
        migrations_dir,
        "0003_add_nickname.sql",
        "ALTER TABLE entities ADD COLUMN nickname TEXT;",
    )


def _columns(db_path: "str | Path", table: str) -> set[str]:
    """Column names of `table`.

    Defined ONCE. There were two identical definitions of this, 830 lines
    apart, and Python silently kept the later one -- so an edit to the
    first would have done nothing, with both call sites still passing.
    Found by test_no_test_module_binds_a_name_twice (ticket T-49).
    """
    connection = sqlite3.connect(db_path)
    try:
        return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    finally:
        connection.close()


def test_a_column_migration_is_applied_at_startup(tmp_path):
    """The whole point of this change.

    A ready database with a pending column migration used to be skipped
    silently: init_db returned on the "ready" branch before looking, and
    pending_migrations still listed it with nothing surfacing that.
    """
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    settings = _settings(tmp_path, migrations_dir)
    init_db(settings)
    _column_migration(migrations_dir)

    assert pending_migrations(settings) == [3]

    apply_startup_migrations(settings)

    assert pending_migrations(settings) == []
    assert "nickname" in _columns(settings.db_path, "entities")


def test_an_index_migration_is_applied_at_startup(tmp_path):
    """Indexes are the quieter half: nothing errors when one is missing,
    the query just uses a different plan."""
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    settings = _settings(tmp_path, migrations_dir)
    init_db(settings)
    _write_migration(
        migrations_dir,
        "0003_add_index.sql",
        "CREATE INDEX IF NOT EXISTS idx_probe ON entities(name);",
    )

    apply_startup_migrations(settings)

    connection = sqlite3.connect(settings.db_path)
    try:
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
    finally:
        connection.close()
    assert "idx_probe" in indexes


def test_startup_migrations_are_idempotent(tmp_path):
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    settings = _settings(tmp_path, migrations_dir)
    init_db(settings)
    _column_migration(migrations_dir)

    apply_startup_migrations(settings)
    apply_startup_migrations(settings)  # the ALTER must not run twice

    assert pending_migrations(settings) == []


def test_a_failing_startup_migration_raises_rather_than_serving(tmp_path):
    """Crash loud. Serving with a half-known schema turns one clear failure
    into a stream of unrelated ones, far from the cause."""
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    settings = _settings(tmp_path, migrations_dir)
    init_db(settings)
    _write_migration(migrations_dir, "0003_broken.sql", "ALTER TABLE nope ADD COLUMN x;")

    with pytest.raises(sqlite3.Error):
        apply_startup_migrations(settings)


def test_a_failing_startup_migration_names_the_file_and_the_error(tmp_path, caplog):
    """"A migration failed" is not actionable at 2am; the filename and the
    SQLite message are."""
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    settings = _settings(tmp_path, migrations_dir)
    init_db(settings)
    _write_migration(migrations_dir, "0003_broken.sql", "ALTER TABLE nope ADD COLUMN x;")

    with caplog.at_level(logging.ERROR), pytest.raises(sqlite3.Error):
        apply_startup_migrations(settings)

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "0003_broken.sql" in logged
    assert "nope" in logged, "the SQLite error itself, not just 'a migration failed'"


def test_a_failed_startup_migration_leaves_the_previous_version_intact(tmp_path):
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    settings = _settings(tmp_path, migrations_dir)
    init_db(settings)
    before = applied_migrations(settings)
    _write_migration(migrations_dir, "0003_broken.sql", "ALTER TABLE nope ADD COLUMN x;")

    with pytest.raises(sqlite3.Error):
        apply_startup_migrations(settings)

    assert applied_migrations(settings) == before


def test_startup_on_a_missing_database_does_nothing(tmp_path):
    """A fresh install has no database yet. The first request bootstraps it
    from schema.sql and stamps every version as applied, so startup must
    neither create nor replay anything here."""
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    settings = _settings(tmp_path, migrations_dir)
    _column_migration(migrations_dir)

    apply_startup_migrations(settings)

    assert not Path(settings.db_path).exists()

    init_db(settings)

    assert pending_migrations(settings) == []
    assert "nickname" not in _columns(settings.db_path, "entities"), (
        "a fresh install builds from schema.sql and stamps; it does not replay"
    )


def test_init_db_no_longer_applies_migrations(tmp_path):
    """Migrations run in exactly one place.

    init_db is per-request — its only caller is get_connection — so a
    second path able to write schema changes is what made the old
    behaviour hard to reason about, and it could only ever reach table
    migrations anyway.
    """
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    settings = _settings(tmp_path, migrations_dir)
    init_db(settings)
    _column_migration(migrations_dir)

    init_db(settings)

    assert pending_migrations(settings) == [3], "init_db must not have applied it"


# --- refusing the real database from a test (ticket T-50) -----------


def test_startup_migrations_refuse_the_real_database_under_pytest(
    tmp_path, monkeypatch
):
    """The structural backstop.

    The autouse fixture in api/tests/conftest.py stops a test in *this*
    package from reaching the real settings, but it cannot cover a lifespan
    test written later in tests/ or q_core_mcp/tests/. This guard lives in
    the code path itself, so it holds wherever the test is written.

    REPO_ROOT is redirected at a temp directory laid out like the real one,
    because the honest version of this test — pointing at the actual
    data/ — would migrate the production database if the guard were broken,
    which is precisely what it exists to prevent.

    Note the "production" path here sits outside the published allow root,
    not merely inside a fake repo. That is a consequence of the allow-list
    shape rather than a weakened test: under an allow-list you cannot
    simulate the real database with a directory under tmp_path, because
    tmp_path is exactly what a test is permitted to write to. Simulating it
    means being somewhere a test may not write, which is what "real" means
    here.
    """
    import api.db

    fake_root = tmp_path / "repo"
    (fake_root / "data").mkdir(parents=True)
    monkeypatch.setattr(api.db, "REPO_ROOT", fake_root)

    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    settings = _settings(tmp_path, migrations_dir)
    init_db(settings)
    _column_migration(migrations_dir)

    # Now point it at the "production" database, as a missed patch would.
    production = Settings(
        _env_file=None,
        api_token="test-token",
        intake_dir=str(tmp_path / "intake"),
        db_path=str(fake_root / "data" / "q-core.db"),
        documents_dir=str(tmp_path / "documents"),
        jyra_dir=str(tmp_path / "jyra"),
        logs_dir=str(tmp_path / "logs"),
        schema_path=settings.schema_path,
        seed_categories_path=settings.seed_categories_path,
        migrations_dir=str(migrations_dir),
    )
    shutil.copy(settings.db_path, production.db_path)
    before = applied_migrations(production)

    with pytest.raises(api.db.ProductionDatabaseInTestError) as exc_info:
        apply_startup_migrations(production)

    assert "PYTEST_CURRENT_TEST" in str(exc_info.value)
    assert applied_migrations(production) == before, "nothing may be written"
    assert pending_migrations(production) == [3], "the migration is still pending"


def test_the_guard_does_not_fire_for_a_temp_database(tmp_path, monkeypatch):
    """Failing closed on innocent tests is how a guard gets deleted.

    Every other test in this file points at tmp_path, so if this were
    wrong the whole suite would fail — but asserting it directly says the
    predicate is about location, not about being under pytest at all.
    """
    import api.db

    fake_root = tmp_path / "repo"
    (fake_root / "data").mkdir(parents=True)
    monkeypatch.setattr(api.db, "REPO_ROOT", fake_root)

    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    settings = _settings(tmp_path, migrations_dir)
    init_db(settings)
    _column_migration(migrations_dir)

    apply_startup_migrations(settings)

    assert pending_migrations(settings) == []


def test_the_guard_is_inert_outside_pytest(tmp_path, monkeypatch):
    """Production must still migrate its own database.

    The guard keys on PYTEST_CURRENT_TEST, so with it unset a db_path under
    REPO_ROOT/data is exactly the normal case and must proceed.
    """
    import api.db

    fake_root = tmp_path / "repo"
    (fake_root / "data").mkdir(parents=True)
    monkeypatch.setattr(api.db, "REPO_ROOT", fake_root)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    settings = _settings(tmp_path, migrations_dir)
    init_db(settings)
    production = Settings(
        _env_file=None,
        api_token="test-token",
        intake_dir=str(tmp_path / "intake"),
        db_path=str(fake_root / "data" / "q-core.db"),
        documents_dir=str(tmp_path / "documents"),
        jyra_dir=str(tmp_path / "jyra"),
        logs_dir=str(tmp_path / "logs"),
        schema_path=settings.schema_path,
        seed_categories_path=settings.seed_categories_path,
        migrations_dir=str(migrations_dir),
    )
    shutil.copy(settings.db_path, production.db_path)
    _column_migration(migrations_dir)

    apply_startup_migrations(production)

    assert pending_migrations(production) == []


def test_the_guard_resolves_paths_rather_than_comparing_strings(
    tmp_path, monkeypatch
):
    """A db_path spelled with .. must not slip past.

    The traversal has to leave the prefix and come back, or the test is
    vacuous: "<root>/data/../data/x" still *starts with* "<root>/data" as
    text, so it matches with or without resolution. Verified by mutation —
    dropping .resolve() left that version green.
    "<root>/elsewhere/../data/x" is the honest case: lexically it is under
    "elsewhere", and only resolution shows it is the real database.
    """
    import api.db

    fake_root = tmp_path / "repo"
    (fake_root / "data").mkdir(parents=True)
    monkeypatch.setattr(api.db, "REPO_ROOT", fake_root)

    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    settings = _settings(tmp_path, migrations_dir)
    init_db(settings)
    sneaky = Settings(
        _env_file=None,
        api_token="test-token",
        intake_dir=str(tmp_path / "intake"),
        db_path=str(fake_root / "elsewhere" / ".." / "data" / "q-core.db"),
        documents_dir=str(tmp_path / "documents"),
        jyra_dir=str(tmp_path / "jyra"),
        logs_dir=str(tmp_path / "logs"),
        schema_path=settings.schema_path,
        seed_categories_path=settings.seed_categories_path,
        migrations_dir=str(migrations_dir),
    )
    shutil.copy(settings.db_path, fake_root / "data" / "q-core.db")
    _column_migration(migrations_dir)

    with pytest.raises(api.db.ProductionDatabaseInTestError):
        apply_startup_migrations(sneaky)


# --- allow-list: only the pytest temp root is writable from a test -----


def _settings_in(root, migrations_dir):
    """Test settings whose every writable path is under `root`."""
    return Settings(
        _env_file=None,
        api_token="test-token",
        db_path=str(root / "q-core.db"),
        documents_dir=str(root / "documents"),
        jyra_dir=str(root / "jyra"),
        logs_dir=str(root / "logs"),
        schema_path=str(REPO_ROOT / "db" / "schema.sql"),
        seed_categories_path=str(REPO_ROOT / "db" / "seed_categories.sql"),
        migrations_dir=str(migrations_dir),
    )


def _settings_at(db_path, template):
    return Settings(
        _env_file=None,
        api_token="test-token",
        db_path=str(db_path),
        documents_dir=template.documents_dir,
        jyra_dir=template.jyra_dir,
        logs_dir=template.logs_dir,
        schema_path=template.schema_path,
        seed_categories_path=template.seed_categories_path,
        migrations_dir=template.migrations_dir,
    )


def test_an_absolute_path_into_another_checkout_is_refused(tmp_path, monkeypatch):
    """review-1's finding, and the reason the deny-list was the wrong shape.

    The old guard compared against REPO_ROOT of the *running* checkout. Run
    from a worktree with an explicit absolute Q_CORE_DB_PATH pointing at
    the real repo's data/, REPO_ROOT is the worktree, the path is not under
    it, and the guard did not fire — while the database being migrated was
    the live one. Every implementer works in a worktree and an uncommented
    Q_CORE_DB_PATH is exactly that path.

    An allow-list has nothing to enumerate: the path is not under the
    pytest temp root, so it is refused whatever checkout it belongs to.

    The path has to sit outside the allow root rather than inside a fake
    repo under tmp_path — under an allow-list, tmp_path is precisely what a
    test is allowed to write to, so a "production" path placed there is
    legitimately permitted. Being somewhere a test may not write is what
    makes it production for this guard's purposes.
    """
    import api.db

    # A different checkout entirely — not under this one's REPO_ROOT, so
    # the deny-list passed it. It also has to sit outside the allow root,
    # which is what makes this the real-world case rather than a fake:
    # the live repo is not under pytest's temp directory either.
    workspace = tmp_path / "workspace"
    allow_root = tmp_path / "pytest-root"
    allow_root.mkdir()
    monkeypatch.setenv("Q_CORE_TEST_TMP_ROOT", str(allow_root))

    other_checkout = workspace / "other-repo"
    (other_checkout / "data").mkdir(parents=True)
    monkeypatch.setattr(api.db, "REPO_ROOT", workspace / "this-repo")

    migrations_dir = allow_root / "migrations"
    migrations_dir.mkdir()
    template = _settings_in(allow_root, migrations_dir)
    init_db(template)
    _column_migration(migrations_dir)

    elsewhere = _settings_at(other_checkout / "data" / "q-core.db", template)
    shutil.copy(template.db_path, elsewhere.db_path)

    with pytest.raises(api.db.ProductionDatabaseInTestError) as exc_info:
        apply_startup_migrations(elsewhere)

    assert str(other_checkout) in str(exc_info.value)
    assert pending_migrations(elsewhere) == [3], "nothing may be applied"


def test_a_path_under_the_pytest_temp_root_is_allowed(tmp_path, monkeypatch):
    """The allow-list must not fail closed on ordinary tests — that is how a
    guard gets deleted rather than fixed."""
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    settings = _settings(tmp_path, migrations_dir)
    init_db(settings)
    _column_migration(migrations_dir)

    apply_startup_migrations(settings)

    assert pending_migrations(settings) == []


def test_the_allow_list_root_is_the_published_one_not_gettempdir(
    tmp_path, monkeypatch
):
    """Proved both ways, with gettempdir() faked so it cannot be the reason.

    The previous version of this test deleted Q_CORE_TEST_TMP_ROOT and
    relied on the fallback, so it only passed because the *default*
    basetemp happens to live under gettempdir(). A test written to show
    the allow-list does not depend on gettempdir() depended on it, and
    failed under --basetemp — where the claim it was making is the one
    that matters.

    Here gettempdir() is pointed somewhere unrelated to the published
    root, so neither location can stand in for the other:

      under fake gettempdir(), outside published  -> refused
      under published, outside fake gettempdir()  -> allowed
    """
    import api.db

    published = tmp_path / "published-root"
    published.mkdir()
    fake_tmpdir = tmp_path / "somewhere-else"
    fake_tmpdir.mkdir()
    monkeypatch.setattr(api.db.tempfile, "gettempdir", lambda: str(fake_tmpdir))
    monkeypatch.setattr(api.db, "REPO_ROOT", tmp_path / "this-repo")
    monkeypatch.setenv("Q_CORE_TEST_TMP_ROOT", str(published))

    assert api.db._test_writable_root() == published.resolve()

    # A database under the faked gettempdir() but outside the published
    # root: refused, which gettempdir()-keying would have allowed.
    migrations_dir = published / "migrations"
    migrations_dir.mkdir()
    template = _settings_in(published, migrations_dir)
    init_db(template)
    _column_migration(migrations_dir)

    decoy = _settings_at(fake_tmpdir / "q-core.db", template)
    shutil.copy(template.db_path, decoy.db_path)
    with pytest.raises(api.db.ProductionDatabaseInTestError):
        apply_startup_migrations(decoy)
    assert pending_migrations(decoy) == [3], "nothing may be applied"

    # And one under the published root but outside the faked gettempdir():
    # allowed, which gettempdir()-keying would have refused.
    apply_startup_migrations(template)
    assert pending_migrations(template) == []


def test_the_fallback_uses_gettempdir_when_nothing_is_published(
    tmp_path, monkeypatch
):
    """The fallback on its own terms, not by relying on where basetemp is.

    Separated from the test above deliberately: proving the published root
    wins and proving the fallback works are different claims, and the old
    single test proved neither cleanly.
    """
    import api.db

    fake_tmpdir = tmp_path / "fallback-root"
    fake_tmpdir.mkdir()
    monkeypatch.setattr(api.db.tempfile, "gettempdir", lambda: str(fake_tmpdir))
    monkeypatch.delenv("Q_CORE_TEST_TMP_ROOT", raising=False)

    assert api.db._test_writable_root() == fake_tmpdir.resolve()


def test_the_allow_list_is_inert_outside_pytest(tmp_path, monkeypatch):
    """Production migrates a database that is under no temp root at all."""
    import api.db

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("Q_CORE_TEST_TMP_ROOT", raising=False)
    fake_root = tmp_path / "repo"
    (fake_root / "data").mkdir(parents=True)
    monkeypatch.setattr(api.db, "REPO_ROOT", fake_root)

    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    template = _settings(tmp_path, migrations_dir)
    init_db(template)
    production = _settings_at(fake_root / "data" / "q-core.db", template)
    shutil.copy(template.db_path, production.db_path)
    _column_migration(migrations_dir)

    apply_startup_migrations(production)

    assert pending_migrations(production) == []


def test_init_db_refuses_to_create_real_directories_from_a_test(
    tmp_path, monkeypatch
):
    """documents_dir and jyra_dir are readers too.

    init_db mkdir()s both before touching the database, so a test holding
    real settings creates directories in the real checkout even if the
    database itself is elsewhere. Same guard, different path.
    """
    import api.db

    allow_root = tmp_path / "pytest-root"
    allow_root.mkdir()
    monkeypatch.setenv("Q_CORE_TEST_TMP_ROOT", str(allow_root))
    outside = tmp_path / "workspace" / "real-repo"

    migrations_dir = allow_root / "migrations"
    migrations_dir.mkdir()
    settings = Settings(
        _env_file=None,
        api_token="test-token",
        db_path=str(allow_root / "q-core.db"),
        documents_dir=str(outside / "data" / "documents"),
        jyra_dir=str(allow_root / "jyra"),
        logs_dir=str(allow_root / "logs"),
        schema_path=str(REPO_ROOT / "db" / "schema.sql"),
        seed_categories_path=str(REPO_ROOT / "db" / "seed_categories.sql"),
        migrations_dir=str(migrations_dir),
    )

    with pytest.raises(api.db.ProductionDatabaseInTestError) as exc_info:
        init_db(settings)

    assert "documents" in str(exc_info.value)
    assert not outside.exists(), "nothing may be created outside the temp root"


def test_the_fallback_warns_that_it_is_running_without_the_published_root(
    tmp_path, monkeypatch, caplog
):
    """A run that silently degraded to gettempdir() should be visible.

    The fallback is correct in the default configuration, which is exactly
    why its absence is easy to miss — deleting the publisher failed no
    test until one was written for it.
    """
    import api.db

    monkeypatch.delenv("Q_CORE_TEST_TMP_ROOT", raising=False)

    with caplog.at_level(logging.WARNING):
        api.db._test_writable_root()

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "Q_CORE_TEST_TMP_ROOT" in logged


def test_migration_0004_drops_row_hash_from_an_existing_database(tmp_path):
    """The column-drop migration, on a database that still has the column.

    This is the first migration that removes rather than adds, and the
    first that only the startup runner can deliver — before a todo a
    file like this would have sat in the directory and never executed.
    """
    settings = _settings(tmp_path, REPO_ROOT / "db" / "migrations")
    init_db(settings)

    connection = sqlite3.connect(settings.db_path)
    try:
        # A database that predates the drop: put the column back, with a
        # row in it, so the migration has something real to remove.
        connection.execute("ALTER TABLE transactions ADD COLUMN row_hash TEXT")
        connection.execute(
            "INSERT INTO entities (id, type, name, status) "
            "VALUES ('acct', 'account', 'Chase', 'active')"
        )
        connection.execute(
            "INSERT INTO statements (id, account_id, period_start, period_end) "
            "VALUES ('s1', 'acct', '2026-03-01', '2026-03-31')"
        )
        connection.execute(
            "INSERT INTO transactions "
            "(id, statement_id, account_id, txn_date, description, amount_cents, row_hash) "
            "VALUES ('t1', 's1', 'acct', '2026-03-04', 'SHELL', -5210, 'deadbeef')"
        )
        connection.execute("DELETE FROM schema_migrations WHERE version = 4")
        connection.commit()
    finally:
        connection.close()

    assert 4 in pending_migrations(settings)

    apply_startup_migrations(settings)

    connection = sqlite3.connect(settings.db_path)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(transactions)")}
        rows = connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        kept = connection.execute(
            "SELECT description, amount_cents FROM transactions WHERE id = 't1'"
        ).fetchone()
    finally:
        connection.close()

    assert "row_hash" not in columns
    assert rows == 1, "dropping a column must not drop rows"
    assert tuple(kept) == ("SHELL", -5210), "the surviving columns keep their values"
    assert pending_migrations(settings) == []


def test_a_fresh_install_has_no_row_hash_column(tmp_path):
    """schema.sql and the migration have to converge, which is the whole
    point of keeping both."""
    settings = _settings(tmp_path, REPO_ROOT / "db" / "migrations")
    init_db(settings)

    connection = sqlite3.connect(settings.db_path)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(transactions)")}
    finally:
        connection.close()

    assert "row_hash" not in columns


def test_migration_0006_adds_the_rule_audit_table_to_an_existing_database(tmp_path):
    """The audit table, on a database that predates it.

    Rows already in `merchant_rules` get no history, and that is correct
    rather than a gap: the trail records changes made from now on, and
    inventing a creation row for a rule nobody watched being created would
    put a fact in the audit log that no one observed.
    """
    settings = _settings(tmp_path, REPO_ROOT / "db" / "migrations")
    init_db(settings)

    connection = sqlite3.connect(settings.db_path)
    try:
        connection.execute(
            "INSERT INTO merchant_rules (id, pattern, category_id, entity_id) "
            "VALUES ('r1', 'MAPLE DONUTS', NULL, NULL)"
        )
        # A database predating 0006 has the rules but not the trail.
        connection.execute("DROP TABLE merchant_rule_changes")
        connection.execute("DELETE FROM schema_migrations WHERE version = 6")
        connection.commit()
    finally:
        connection.close()

    assert 6 in pending_migrations(settings)

    apply_startup_migrations(settings)

    connection = sqlite3.connect(settings.db_path)
    try:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(merchant_rule_changes)")
        }
        rules = connection.execute("SELECT COUNT(*) FROM merchant_rules").fetchone()[0]
        history = connection.execute(
            "SELECT COUNT(*) FROM merchant_rule_changes"
        ).fetchone()[0]
    finally:
        connection.close()

    assert columns == {
        "id", "rule_id", "changed_at", "actor", "field", "old_value", "new_value",
    }
    assert rules == 1, "adding a table must not disturb the rows beside it"
    assert history == 0, "no invented history for rules that predate the trail"
    assert pending_migrations(settings) == []


# --------------------------------------------------------------------------
# The guard's message has to name what it actually found (ticket T-48).
#
# A CREATE TRIGGER body contains BEGIN ... END, and `_statements` splits on
# ';', so the body's internal semicolons tear the statement apart and leave
# a bare `END`. The guard is right to reject that file -- the truncated
# CREATE TRIGGER would fail at execution anyway -- but the original message
# blamed transaction control, which the author never wrote. A message that
# names the wrong cause is worse than a vague one: it sends the reader to
# rewrite SQL that was never the problem.
# --------------------------------------------------------------------------

TRIGGER_MIGRATION = """-- add an audit log
CREATE TABLE audit_log (id TEXT PRIMARY KEY, note TEXT);

CREATE TRIGGER log_audit_insert AFTER INSERT ON audit_log BEGIN
  UPDATE audit_log SET note = 'seen' WHERE id = NEW.id;
END;
"""

AUTHORED_MIGRATION = """BEGIN;
CREATE TABLE widgets (id TEXT PRIMARY KEY);
COMMIT;
"""


def test_a_split_trigger_body_is_diagnosed_as_a_split_trigger_body():
    """The measured case from the ticket, pinned so the message can't regress."""
    statements = _statements(TRIGGER_MIGRATION)
    # The split itself, asserted rather than assumed: a bare END only exists
    # because the body was torn apart.
    assert "END" in statements, statements
    offenders = _transaction_control_offenders(statements)

    assert [statement for statement, _ in offenders] == ["END"]
    assert all(cause == SPLIT_TRIGGER_BODY for _, cause in offenders)


def test_real_transaction_control_is_still_diagnosed_as_itself():
    """The guard must not relabel every offender as a trigger problem.

    Without this, 'always blame the splitter' would pass the test above
    while being exactly as wrong as the behaviour it replaced.
    """
    offenders = _transaction_control_offenders(_statements(AUTHORED_MIGRATION))

    assert [statement for statement, _ in offenders] == ["BEGIN", "COMMIT"]
    assert all(cause == AUTHORED_TRANSACTION_CONTROL for _, cause in offenders)


def test_the_trigger_message_points_at_the_splitter_and_the_readme():
    message = _offender_message(
        [("0003_audit.sql", "END", SPLIT_TRIGGER_BODY)]
    )

    assert "semicolon INSIDE the trigger body" in message
    assert "db/migrations/README.md" in message
    # The part that makes it actionable: it says the reader probably did
    # not do the thing the guard's name accuses them of.
    assert "did not write transaction control" in message
    # And it must not assert the wrong cause as fact.
    assert "already wraps the whole file" not in message


def test_the_authored_message_keeps_the_original_explanation():
    """The genuine case still needs the atomicity reasoning -- that is the
    part explaining why a rule this surprising exists at all."""
    message = _offender_message(
        [("0004_widgets.sql", "COMMIT", AUTHORED_TRANSACTION_CONTROL)]
    )

    # The clause carrying the REASON, not just the keywords around it.
    # Asserting "BEGIN IMMEDIATE ... COMMIT" alone was not enough: deleting
    # "_apply_migrations already wraps the whole file in one" left both of
    # the original substrings intact and this test green, while the message
    # no longer said what wraps the file. A mutation found that.
    assert "_apply_migrations already wraps the whole file in one" in message
    assert "BEGIN IMMEDIATE ... COMMIT" in message
    assert "commits mid-way breaks the atomicity" in message
    assert "init_db's re-read depends on" in message
    assert "trigger body" not in message


BOTH_CAUSES_MIGRATION = """BEGIN;
CREATE TABLE audit_log (id TEXT PRIMARY KEY, note TEXT);

CREATE TRIGGER log_audit AFTER INSERT ON audit_log BEGIN
  UPDATE audit_log SET note = 'seen' WHERE id = NEW.id;
END;
COMMIT;
"""


def test_one_file_with_both_causes_is_classified_as_both():
    """Through the real classifier, on ONE file -- which is the point.

    This test used to hand-build two tuples for two DIFFERENT files and
    hand them to `_offender_message`. It proved the MESSAGE LAYER can
    print two labels; it never asked whether any file PRODUCES two, and
    the classifier computed one cause per file, so none could. Eight
    mutations passed over that.

    Here the author wrapped a trigger migration in a real transaction. The
    standing BEGIN and COMMIT are theirs; the bare END is the splitter's.
    Telling them they "probably did not write transaction control" would
    invert this guard's whole thesis while they look at their own BEGIN.
    """
    statements = _statements(BOTH_CAUSES_MIGRATION)
    offenders = dict(_transaction_control_offenders(statements))

    assert offenders == {
        "BEGIN": AUTHORED_TRANSACTION_CONTROL,
        "END": SPLIT_TRIGGER_BODY,
        "COMMIT": AUTHORED_TRANSACTION_CONTROL,
    }


def test_the_message_for_a_both_causes_file_says_both_things():
    """And neither explanation swallows the other in the text."""
    statements = _statements(BOTH_CAUSES_MIGRATION)
    message = _offender_message(
        [("0005_both.sql", st, cause)
         for st, cause in _transaction_control_offenders(statements)]
    )

    assert "_apply_migrations already wraps the whole file in one" in message
    assert "semicolon INSIDE the trigger body" in message
    # The reassurance is the dangerous half: it must not be handed to
    # someone who genuinely wrote BEGIN/COMMIT with no qualification.
    assert "(authored transaction control)" in message
    assert "(split trigger body)" in message


def test_a_standing_begin_is_never_blamed_on_the_splitter():
    """The asymmetry the fix rests on.

    A trigger body's opening BEGIN stays attached to its CREATE TRIGGER
    and never leads a statement of its own -- only the closing END
    arrives alone. So a standing BEGIN was written by a person, whatever
    else the file contains, and per-file classification is what lost that.
    """
    statements = _statements(BOTH_CAUSES_MIGRATION)
    # The premise, asserted rather than assumed.
    assert "BEGIN" in statements
    assert not any(s.upper().startswith("BEGIN ") for s in statements[1:]), (
        "a trigger body's BEGIN led a statement -- the asymmetry does not hold"
    )
    causes = dict(_transaction_control_offenders(statements))
    assert causes["BEGIN"] == AUTHORED_TRANSACTION_CONTROL


# --- columns, not just tables (ticket T-27) -------------------------------


def test_a_database_predating_the_attachment_columns_gains_them(tmp_path):
    """The COLUMN analogue of the table guard above.

    `test_every_table_added_since_the_baseline_has_a_migration` covers
    tables. Columns added to an existing table are the case
    `apply_startup_migrations` was written for -- `init_db` reaches
    migrations only when a TABLE is missing, so an added column is
    exactly what gets skipped silently.

    Built by stripping the columns out of schema.sql rather than by
    freezing a copy of the old file: a frozen copy stops describing the
    real "before" state the moment anything else in that table changes.
    """
    schema = (REPO_ROOT / "db" / "schema.sql").read_text()
    older = schema.replace("    size_bytes   INTEGER,\n", "").replace(
        "    content_type TEXT,\n", ""
    )
    assert older != schema, "the columns were not found in schema.sql to strip"

    old_schema_path = tmp_path / "old-schema.sql"
    old_schema_path.write_text(older)

    settings = _settings(tmp_path, REPO_ROOT / "db" / "migrations")
    settings = settings.model_copy(update={"schema_path": str(old_schema_path)})

    init_db(settings)
    db_path = Path(settings.db_path)
    assert "size_bytes" not in _columns(db_path, "ticket_attachments")

    # Now the database looks like one created before 0007. Un-stamp it and
    # let the real runner bring it forward.
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("DELETE FROM schema_migrations WHERE version = 7")
        connection.commit()
    finally:
        connection.close()

    apply_startup_migrations(
        settings.model_copy(update={"schema_path": str(REPO_ROOT / "db" / "schema.sql")})
    )

    columns = _columns(db_path, "ticket_attachments")
    assert "size_bytes" in columns
    assert "content_type" in columns


def test_every_added_column_in_a_migration_is_also_in_the_schema(tmp_path):
    """Parity in the other direction, for every migration, not just 0007.

    A column added by a migration but missing from schema.sql gives a
    fresh install a table an upgraded one does not have -- the same class
    of split the table guard prevents, one level down. Derived from the
    migration files rather than listed, so it covers migrations written
    after this test.
    """
    import re

    settings = _settings(tmp_path, REPO_ROOT / "db" / "migrations")
    init_db(settings)
    db_path = Path(settings.db_path)

    pattern = re.compile(
        r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)", re.IGNORECASE
    )
    found = []
    for _, path in _discover_migrations(settings):
        for table, column in pattern.findall(Path(path).read_text()):
            found.append((table, column))

    # A regex that matches nothing would make this pass over an empty set.
    assert found, "no ADD COLUMN found in any migration -- discovery is broken"

    missing = [
        (table, column)
        for table, column in found
        if column not in _columns(db_path, table)
    ]
    assert missing == [], f"added by migration but absent from schema.sql: {missing}"


# The artifact tables exactly as 0012_briefings.sql created them.
PRE_0015_ARTIFACTS_DDL = (
    """CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('daily', 'weekly')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL
)""",
    """CREATE TABLE artifact_revisions (
    artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    revision INTEGER NOT NULL,
    payload TEXT NOT NULL,
    actor TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (artifact_id, revision)
)""",
    "CREATE INDEX idx_artifacts_created ON artifacts(created_at DESC, id DESC)",
)

PRE_0015_BRIEF_ID = "00000000-0000-4000-8000-0000000000f1"


def pre_0015_brief_document(text: str) -> dict:
    return {"format": "static_html_v1", "title": "Week so far", "html": f"<p>{text}</p>",
            "period_start": "2026-09-21T00:00:00-04:00", "as_of": "2026-09-23T09:00:00-04:00",
            "timezone": "America/New_York", "sources": [],
            "coverage": [{"source": "gmail", "status": "unavailable", "detail": "Invented fixture."}]}


def restore_pre_0015_artifacts(db_path: str) -> None:
    """Put a fresh database back in the shape 0015 upgrades, with one weekly
    brief at revision 2, and mark 0015 as not yet applied."""
    import json

    connection = sqlite3.connect(db_path)
    try:
        connection.execute("DROP TABLE artifact_revisions")
        connection.execute("DROP TABLE artifacts")
        for statement in PRE_0015_ARTIFACTS_DDL:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO artifacts (id, request_id, request_hash, kind, created_at, updated_at, revision) "
            "VALUES (?, ?, 'hash', 'weekly', '2026-09-23T13:00:00+00:00', '2026-09-23T14:00:00+00:00', 2)",
            (PRE_0015_BRIEF_ID, "00000000-0000-4000-8000-0000000000f2"),
        )
        for revision, text in ((1, "First draft of the week"), (2, "Second draft of the week")):
            connection.execute(
                "INSERT INTO artifact_revisions (artifact_id, revision, payload, actor, note, created_at) "
                "VALUES (?, ?, ?, 'Test', 'Invented', '2026-09-23T13:00:00+00:00')",
                (PRE_0015_BRIEF_ID, revision, json.dumps(pre_0015_brief_document(text))),
            )
        connection.execute("DELETE FROM schema_migrations WHERE version = 15")
        connection.commit()
    finally:
        connection.close()


def test_migration_0015_widens_artifact_kinds_and_keeps_briefs(tmp_path):
    """Renaming `artifacts` aside re-points artifact_revisions' FK at the
    renamed table, so a rebuild of artifacts alone leaves every later
    revision insert failing once foreign keys are on. 0015 rebuilds both."""
    settings = _settings(tmp_path, REPO_ROOT / "db" / "migrations")
    init_db(settings)
    restore_pre_0015_artifacts(settings.db_path)
    assert 15 in pending_migrations(settings)

    apply_startup_migrations(settings)

    connection = sqlite3.connect(settings.db_path)
    try:
        counts = [connection.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("artifacts", "artifact_revisions")]
        assert counts == [1, 2]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        targets = {row[2] for row in connection.execute("PRAGMA foreign_key_list(artifact_revisions)")}
        assert targets == {"artifacts"}
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO artifact_revisions (artifact_id, revision, payload, actor, note, created_at) "
            "VALUES (?, 3, '{}', 'Test', 'After 0015', '2026-09-26T00:00:00+00:00')",
            (PRE_0015_BRIEF_ID,),
        )
        connection.commit()
        # The widened CHECK accepts the new kinds and still refuses others.
        connection.execute(
            "INSERT INTO artifacts (id, request_id, request_hash, kind, created_at, updated_at, revision) "
            "VALUES ('p', 'r-p', 'h', 'page', 'x', 'x', 1)")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO artifacts (id, request_id, request_hash, kind, created_at, updated_at, revision) "
                "VALUES ('b', 'r-b', 'h', 'bogus', 'x', 'x', 1)")
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(artifacts)")}
        assert "idx_artifacts_created" in indexes
        leftovers = connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE '%pre_0015%'").fetchall()
        assert leftovers == []
    finally:
        connection.close()
    assert pending_migrations(settings) == []


def test_migration_0016_adds_artifact_links(tmp_path):
    """A database at 0015 gains artifact_links, its index and a working FK."""
    settings = _settings(tmp_path, REPO_ROOT / "db" / "migrations")
    init_db(settings)
    connection = sqlite3.connect(settings.db_path)
    try:
        connection.execute("DROP TABLE artifact_links")
        connection.execute("DELETE FROM schema_migrations WHERE version = 16")
        connection.commit()
    finally:
        connection.close()
    assert "artifact_links" not in _tables(settings.db_path)
    assert pending_migrations(settings) == [16]

    apply_startup_migrations(settings)

    connection = sqlite3.connect(settings.db_path)
    try:
        assert "artifact_links" in _tables(settings.db_path)
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(artifact_links)")}
        assert "idx_artifact_links_target" in indexes
        targets = {row[2] for row in connection.execute("PRAGMA foreign_key_list(artifact_links)")}
        assert targets == {"artifacts"}
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO artifact_links (id, artifact_id, target_type, target_id, actor, created_at) "
                "VALUES ('l', 'no-such-artifact', 'ticket', 't', 'a', 'x')")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO artifact_links (id, artifact_id, target_type, target_id, actor, created_at) "
                "VALUES ('l', 'no-such-artifact', 'note', 't', 'a', 'x')")
    finally:
        connection.close()
    assert pending_migrations(settings) == []

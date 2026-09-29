import sqlite3
from pathlib import Path

import pytest

from api.db import init_db


def test_init_db_creates_database_and_tables(test_settings):
    init_db(test_settings)

    assert Path(test_settings.db_path).exists()
    assert Path(test_settings.documents_dir).is_dir()

    connection = sqlite3.connect(test_settings.db_path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        connection.close()

    assert "entities" in tables
    assert "reminder_instances" in tables
    assert "categories" in tables


def test_init_db_seeds_categories(test_settings):
    init_db(test_settings)

    connection = sqlite3.connect(test_settings.db_path)
    try:
        count = connection.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
    finally:
        connection.close()

    assert count == 80


def test_init_db_is_idempotent(test_settings):
    init_db(test_settings)
    init_db(test_settings)  # must not raise or wipe existing data

    connection = sqlite3.connect(test_settings.db_path)
    try:
        count = connection.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
    finally:
        connection.close()

    assert count == 80


def test_init_db_creates_nested_parent_directories(test_settings, tmp_path):
    from api.config import Settings, REPO_ROOT
    from api.db import init_db

    nested_settings = Settings(
        _env_file=None,
        api_token="test-token",
        intake_dir=str(tmp_path / "intake"),
        db_path=str(tmp_path / "a" / "b" / "q-core.db"),
        documents_dir=str(tmp_path / "a" / "b" / "documents"),
        jyra_dir=str(tmp_path / "a" / "b" / "jyra"),
        logs_dir=str(tmp_path / "a" / "b" / "logs"),
        schema_path=str(REPO_ROOT / "db" / "schema.sql"),
        seed_categories_path=str(REPO_ROOT / "db" / "seed_categories.sql"),
    )

    init_db(nested_settings)

    assert Path(nested_settings.db_path).exists()
    assert Path(nested_settings.documents_dir).is_dir()


def test_get_connection_enables_foreign_keys(test_settings):
    from api.db import get_connection

    generator = get_connection(settings=test_settings)
    connection = next(generator)
    try:
        result = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        assert result == 1
    finally:
        generator.close()


def test_get_connection_initializes_db_lazily(test_settings):
    from api.db import get_connection

    assert not Path(test_settings.db_path).exists()

    generator = get_connection(settings=test_settings)
    next(generator)
    generator.close()

    assert Path(test_settings.db_path).exists()


def test_get_connection_closes_connection_on_cleanup(test_settings):
    from api.db import get_connection

    generator = get_connection(settings=test_settings)
    connection = next(generator)
    generator.close()

    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def test_paginate_returns_first_page(test_settings):
    from api.db import get_connection, paginate

    generator = get_connection(settings=test_settings)
    connection = next(generator)
    try:
        result = paginate(
            connection,
            "SELECT id, name FROM categories WHERE parent_id IS NULL ORDER BY name",
            (),
            limit=5,
            offset=0,
        )
    finally:
        generator.close()

    assert result["total"] == 19
    assert result["limit"] == 5
    assert result["offset"] == 0
    assert len(result["items"]) == 5


def test_paginate_respects_offset(test_settings):
    from api.db import get_connection, paginate

    generator = get_connection(settings=test_settings)
    connection = next(generator)
    try:
        first_page = paginate(
            connection,
            "SELECT id, name FROM categories WHERE parent_id IS NULL ORDER BY name",
            (),
            limit=5,
            offset=0,
        )
        second_page = paginate(
            connection,
            "SELECT id, name FROM categories WHERE parent_id IS NULL ORDER BY name",
            (),
            limit=5,
            offset=5,
        )
    finally:
        generator.close()

    first_ids = {item["id"] for item in first_page["items"]}
    second_ids = {item["id"] for item in second_page["items"]}
    assert first_ids.isdisjoint(second_ids)


def test_paginate_rejects_non_positive_limit(test_settings):
    """RequestValidationError, not ValueError: a route that forgets its ge=1
    guard must degrade to a 422 rather than an unhandled 500. See a todo —
    four routes across two plans shipped without that guard."""
    from fastapi.exceptions import RequestValidationError

    from api.db import get_connection, paginate

    generator = get_connection(settings=test_settings)
    connection = next(generator)
    try:
        with pytest.raises(RequestValidationError):
            paginate(connection, "SELECT id FROM categories", (), limit=0, offset=0)
        with pytest.raises(RequestValidationError):
            paginate(connection, "SELECT id FROM categories", (), limit=-1, offset=0)
    finally:
        generator.close()


def test_paginate_rejects_a_limit_above_the_maximum(test_settings):
    """The max is enforced centrally too, so "uniform pagination, max 200" is
    one fact rather than a convention each route restates."""
    from fastapi.exceptions import RequestValidationError

    from api.db import MAX_LIMIT, get_connection, paginate

    generator = get_connection(settings=test_settings)
    connection = next(generator)
    try:
        with pytest.raises(RequestValidationError):
            paginate(
                connection,
                "SELECT id FROM categories",
                (),
                limit=MAX_LIMIT + 1,
                offset=0,
            )
        # The boundary itself is allowed.
        paginate(
            connection, "SELECT id FROM categories", (), limit=MAX_LIMIT, offset=0
        )
    finally:
        generator.close()


def test_paginate_error_points_at_the_offending_query_param(test_settings):
    """The loc must name the query parameter, or a client highlights nothing."""
    from fastapi.exceptions import RequestValidationError

    from api.db import get_connection, paginate

    generator = get_connection(settings=test_settings)
    connection = next(generator)
    try:
        with pytest.raises(RequestValidationError) as excinfo:
            paginate(connection, "SELECT id FROM categories", (), limit=0, offset=0)
        assert excinfo.value.errors()[0]["loc"] == ("query", "limit")

        with pytest.raises(RequestValidationError) as excinfo:
            paginate(connection, "SELECT id FROM categories", (), limit=10, offset=-1)
        assert excinfo.value.errors()[0]["loc"] == ("query", "offset")
    finally:
        generator.close()


def test_paginate_rejects_negative_offset(test_settings):
    from fastapi.exceptions import RequestValidationError

    from api.db import get_connection, paginate

    generator = get_connection(settings=test_settings)
    connection = next(generator)
    try:
        with pytest.raises(RequestValidationError):
            paginate(connection, "SELECT id FROM categories", (), limit=10, offset=-1)
    finally:
        generator.close()


def test_every_paginated_route_declares_the_bounds():
    """Every route taking a `limit` query param must declare ge=1 and
    le=MAX_LIMIT, so the 422 names the constraint instead of coming from
    paginate's backstop.

    Static, over the OpenAPI schema, so it covers routes that do not exist
    yet — which is the point. The same missing guard shipped on four separate
    routes across two plans (a todo) because the rule lived in a comment
    each author had to remember rather than in a test.
    """
    import os

    os.environ.setdefault("Q_CORE_API_TOKEN", "schema-only")
    from api.db import MAX_LIMIT
    from api.main import app

    offenders = []
    for path, operations in app.openapi()["paths"].items():
        for method, operation in operations.items():
            for parameter in operation.get("parameters", []):
                if parameter.get("name") != "limit":
                    continue
                schema = parameter.get("schema", {})
                if schema.get("minimum") != 1 or schema.get("maximum") != MAX_LIMIT:
                    offenders.append(
                        f"{method.upper()} {path}: minimum="
                        f"{schema.get('minimum')} maximum={schema.get('maximum')}"
                    )

    assert not offenders, (
        "routes with a `limit` param missing ge=1 / le=MAX_LIMIT:\n  "
        + "\n  ".join(offenders)
    )


def test_paginate_with_params(test_settings):
    from api.db import get_connection, paginate

    generator = get_connection(settings=test_settings)
    connection = next(generator)
    try:
        result = paginate(
            connection,
            "SELECT id, name FROM categories WHERE parent_id = ?",
            ("housing",),
            limit=50,
            offset=0,
        )
    finally:
        generator.close()

    assert result["total"] > 0
    assert all(item["id"].startswith("housing_") for item in result["items"])


def test_get_connection_enables_wal_journal_mode(test_settings):
    from api.db import get_connection

    generator = get_connection(settings=test_settings)
    connection = next(generator)
    try:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        generator.close()

    assert mode == "wal"


def test_get_connection_migrates_pre_existing_non_wal_database(test_settings):
    """A DB file created before this change must migrate on next connection.

    journal_mode is persisted in the file, not per-connection, so setting it
    only in init_db's bootstrap branch would leave an already-existing
    data/q-core.db in rollback-journal mode forever.
    """
    from api.db import get_connection, init_db

    init_db(test_settings)
    connection = sqlite3.connect(test_settings.db_path)
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        connection.close()

    generator = get_connection(settings=test_settings)
    connection = next(generator)
    try:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        generator.close()

    assert mode == "wal"


def test_get_connection_sets_deliberate_busy_timeout(test_settings):
    """The busy timeout is a chosen value, not sqlite3's 5.0s stdlib default.

    Asserting the literal milliseconds on purpose: changing BUSY_TIMEOUT_SECONDS
    should fail this test and force the rationale in api/db.py to be revisited.
    """
    from api.db import BUSY_TIMEOUT_SECONDS, get_connection

    assert BUSY_TIMEOUT_SECONDS == 10.0

    generator = get_connection(settings=test_settings)
    connection = next(generator)
    try:
        timeout_ms = connection.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        generator.close()

    assert timeout_ms == 10_000


def test_reader_is_not_blocked_by_an_in_progress_write(test_settings):
    """The behaviour that motivated this change: a poller reading while a
    write is in flight.

    Note the shape of this test. A writer holding a *small* open transaction
    does NOT block readers even in rollback-journal mode — it only holds a
    RESERVED lock, which readers are allowed past. Blocking starts when the
    writer escalates to EXCLUSIVE: at commit, or earlier if its page cache
    spills. So `cache_size=1` plus a bulk insert is what forces the escalation
    and makes this test actually discriminate; the obvious simpler version
    (BEGIN IMMEDIATE + one UPDATE) passes in both journal modes and tests
    nothing. Verified by flipping get_connection back to DELETE: this fails
    with "database is locked", the simpler version still passes.
    """
    from api.db import get_connection

    writer_generator = get_connection(settings=test_settings)
    writer = next(writer_generator)
    try:
        writer.execute("PRAGMA cache_size = 1")
        writer.execute("BEGIN IMMEDIATE")
        writer.executemany(
            "INSERT INTO categories (id, name, parent_id) VALUES (?, ?, NULL)",
            [(f"scratch_{i}", f"scratch {i}") for i in range(10_000)],
        )

        reader_generator = get_connection(settings=test_settings)
        reader = next(reader_generator)
        try:
            count = reader.execute(
                "SELECT COUNT(*) FROM categories WHERE id NOT LIKE 'scratch_%'"
            ).fetchone()[0]
        finally:
            reader_generator.close()

        # The reader sees the pre-write state and, critically, is not blocked.
        assert count == 80
    finally:
        writer.rollback()
        writer_generator.close()


def test_contended_wal_migration_does_not_raise_and_self_heals(
    test_settings, monkeypatch
):
    """A failed one-time migration must not take the request down with it.

    PRAGMA journal_mode=WAL needs a brief EXCLUSIVE lock, so on a DB still in
    rollback-journal mode it loses to any in-flight reader — precisely the
    poller-alongside-interactive-session case this change exists to serve.
    Unguarded it raises OperationalError out of a dependency, i.e. a bare 500
    with no error envelope, after waiting out the full busy timeout.

    The timeout is patched down only to keep the test fast; the contention it
    creates is real.
    """
    import api.db

    monkeypatch.setattr(api.db, "BUSY_TIMEOUT_SECONDS", 0.1)

    init_db(test_settings)
    # Bootstrap now produces WAL (D107), so the delete-mode file this
    # test needs has to be made deliberately. That is not a workaround
    # for the change — it is the case that still reaches this code
    # path: a file created before that change, hand-repaired, or
    # restored from a rollback-journal backup. Bootstrap used to supply
    # one incidentally, which is why the setup looked like a no-op.
    demoted = sqlite3.connect(test_settings.db_path)
    try:
        demoted.execute("PRAGMA journal_mode = DELETE")
    finally:
        demoted.close()
    assert (
        sqlite3.connect(test_settings.db_path)
        .execute("PRAGMA journal_mode")
        .fetchone()[0]
        == "delete"
    )

    blocking_reader = sqlite3.connect(test_settings.db_path)
    blocking_reader.execute("BEGIN")
    blocking_reader.execute("SELECT COUNT(*) FROM categories").fetchall()
    try:
        generator = api.db.get_connection(settings=test_settings)
        connection = next(generator)
        try:
            # The request survives, and the connection is still usable.
            assert connection.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        finally:
            generator.close()
    finally:
        blocking_reader.rollback()
        blocking_reader.close()

    # Self-healing: the next uncontended connection completes the migration.
    generator = api.db.get_connection(settings=test_settings)
    connection = next(generator)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        generator.close()


def test_init_db_raises_on_a_zero_byte_file_and_leaves_it_alone(test_settings):
    """The file that actually bit this repo, now a loud error.

    A 0-byte file used to satisfy `exists()` and short-circuit bootstrap
    forever, failing every request with `no such table: entities`. It is no
    longer silently replaced either: replacing it needs an os.replace that
    can swap an inode out from under a live connection, and the file being
    broken at all is evidence of a failure worth surfacing rather than
    papering over.
    """
    from api.db import CorruptDatabaseError

    db_file = Path(test_settings.db_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    db_file.touch()
    before = db_file.stat().st_mtime_ns

    with pytest.raises(CorruptDatabaseError) as exc_info:
        init_db(test_settings)

    message = str(exc_info.value)
    assert str(db_file) in message, "the message must name the offending path"
    assert "not be replaced automatically" in message
    assert db_file.stat().st_size == 0
    assert db_file.stat().st_mtime_ns == before
    assert list(db_file.parent.glob(".bootstrap-*")) == []


def test_init_db_raises_on_a_valid_but_tableless_database(test_settings):
    """Non-zero size is not enough — this is the file review-1's retracted
    "pre-migrate with PRAGMA journal_mode=WAL" advice would have produced,
    so it is reachable for this user specifically, not hypothetical."""
    from api.db import CorruptDatabaseError

    db_file = Path(test_settings.db_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_file)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.close()
    assert db_file.stat().st_size > 0

    with pytest.raises(CorruptDatabaseError):
        init_db(test_settings)


def test_init_db_raises_on_a_file_that_is_not_a_database(test_settings):
    from api.db import CorruptDatabaseError

    db_file = Path(test_settings.db_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    db_file.write_text("this is not a sqlite database")

    with pytest.raises(CorruptDatabaseError):
        init_db(test_settings)

    assert db_file.read_text() == "this is not a sqlite database"


def test_init_db_still_bootstraps_a_fresh_install(test_settings):
    """The counterweight: refusing to touch a broken file must not stop a
    fresh install, where there is nothing at the path to protect."""
    assert not Path(test_settings.db_path).exists()

    init_db(test_settings)

    connection = sqlite3.connect(test_settings.db_path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "entities" in tables
        assert connection.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 80
    finally:
        connection.close()


def test_init_db_never_clobbers_an_existing_populated_database(test_settings):
    """The counterweight to every test above: bootstrap overwrites the path,
    so a false 'needs bootstrap' verdict destroys real data."""
    init_db(test_settings)
    connection = sqlite3.connect(test_settings.db_path)
    try:
        connection.execute(
            "INSERT INTO entities (id, type, name, status, attributes, created_at)"
            " VALUES ('e1', 'pet', 'Rufus', 'active', '{}', '2026-01-01T00:00:00Z')"
        )
        connection.commit()
    finally:
        connection.close()

    init_db(test_settings)

    connection = sqlite3.connect(test_settings.db_path)
    try:
        assert connection.execute("SELECT name FROM entities").fetchone()[0] == "Rufus"
    finally:
        connection.close()


def test_init_db_does_not_clobber_a_locked_database(test_settings):
    """A locked DB must never be read as 'needs bootstrap'.

    Probing an in-use database raises OperationalError, and treating that as
    a bootstrap signal would overwrite a live, fully-populated DB — the worst
    possible outcome of this change. Ambiguity must mean 'leave it alone'.
    """
    init_db(test_settings)
    connection = sqlite3.connect(test_settings.db_path)
    connection.execute(
        "INSERT INTO entities (id, type, name, status, attributes, created_at)"
        " VALUES ('e1', 'pet', 'Rufus', 'active', '{}', '2026-01-01T00:00:00Z')"
    )
    connection.commit()
    connection.execute("BEGIN EXCLUSIVE")
    connection.execute("UPDATE entities SET name = 'Rufus' WHERE id = 'e1'")
    try:
        init_db(test_settings)  # must not raise, must not overwrite
    finally:
        connection.rollback()
        connection.close()

    connection = sqlite3.connect(test_settings.db_path)
    try:
        assert connection.execute("SELECT name FROM entities").fetchone()[0] == "Rufus"
    finally:
        connection.close()


def test_failed_bootstrap_leaves_nothing_at_the_database_path(test_settings, tmp_path):
    """Atomic rename's real payoff: a failed bootstrap can't leave a broken
    file behind to be trusted by the next run — which is how a todo's
    0-byte file came to exist in the first place."""
    from api.config import REPO_ROOT, Settings

    bad_schema = tmp_path / "bad_schema.sql"
    bad_schema.write_text("CREATE TABLE entities (id TEXT); THIS IS NOT SQL;")
    broken_settings = Settings(
        _env_file=None,
        api_token="test-token",
        intake_dir=str(tmp_path / "intake"),
        db_path=str(tmp_path / "broken" / "q-core.db"),
        documents_dir=str(tmp_path / "broken" / "documents"),
        jyra_dir=str(tmp_path / "broken" / "jyra"),
        logs_dir=str(tmp_path / "broken" / "logs"),
        schema_path=str(bad_schema),
        seed_categories_path=str(REPO_ROOT / "db" / "seed_categories.sql"),
    )

    with pytest.raises(sqlite3.DatabaseError):
        init_db(broken_settings)

    db_file = Path(broken_settings.db_path)
    assert not db_file.exists()
    # and no temp debris left behind (documents/ is created by init_db and
    # is expected; anything matching the bootstrap temp prefix is not)
    assert list(db_file.parent.glob(".bootstrap-*")) == []


def test_bootstrap_does_not_clobber_a_database_another_bootstrapper_just_wrote(
    test_settings, monkeypatch
):
    """Building into a temp file removes a todo's `table already exists`
    error, but would replace it with something worse if the rename were
    unconditional: a racer that finishes second could overwrite a database
    the winner already accepted writes into. Loud failure is better than
    silent data loss, and neither is acceptable here.

    Simulates a winner landing (and taking a write) while we are still
    building our temp copy.
    """
    import api.db

    real_bootstrap = api.db._bootstrap_into

    def bootstrap_then_lose_the_race(settings, target):
        real_bootstrap(settings, target)
        real_bootstrap(settings, settings.db_path)
        connection = sqlite3.connect(settings.db_path)
        try:
            connection.execute(
                "INSERT INTO entities (id, type, name, status, attributes, created_at)"
                " VALUES ('e1', 'pet', 'Rufus', 'active', '{}', '2026-01-01T00:00:00Z')"
            )
            connection.commit()
        finally:
            connection.close()

    monkeypatch.setattr(api.db, "_bootstrap_into", bootstrap_then_lose_the_race)

    api.db.init_db(test_settings)

    connection = sqlite3.connect(test_settings.db_path)
    try:
        row = connection.execute("SELECT name FROM entities").fetchone()
    finally:
        connection.close()

    assert row is not None, "the winner's database (and its write) was clobbered"
    assert row[0] == "Rufus"
    assert list(Path(test_settings.db_path).parent.glob(".bootstrap-*")) == []


def test_expected_tables_matches_schema_sql(test_settings):
    """EXPECTED_TABLES is hardcoded to keep it off the per-request hot path,
    so it needs this test to stop it drifting as the schema grows."""
    import re

    from api.db import EXPECTED_TABLES

    declared = set(
        re.findall(
            r"CREATE TABLE (?:IF NOT EXISTS )?([A-Za-z_][A-Za-z0-9_]*)",
            Path(test_settings.schema_path).read_text(),
        )
    )

    assert declared, "schema.sql parsed to no tables — the regex is wrong"
    assert set(EXPECTED_TABLES) == declared


def test_init_db_refuses_to_replace_a_database_missing_required_tables(test_settings):
    """Not safe to replace AND not fit to serve, so it must do neither.

    A half-built database is exactly the shape of the bug that motivated
    this work: it would otherwise be accepted and then fail every request
    with a bare `no such table`, which is the quiet failure mode all over
    again. Raising names the problem instead.
    """
    from api.db import IncompleteDatabaseError

    db_file = Path(test_settings.db_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_file)
    try:
        connection.execute("CREATE TABLE entities (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO entities VALUES ('precious-data')")
        connection.commit()
    finally:
        connection.close()
    before = db_file.stat().st_mtime_ns

    with pytest.raises(IncompleteDatabaseError) as exc_info:
        init_db(test_settings)

    assert "missing required tables" in str(exc_info.value)
    # The file is left exactly as it was — not replaced, not repaired.
    assert db_file.stat().st_mtime_ns == before
    connection = sqlite3.connect(db_file)
    try:
        assert connection.execute("SELECT id FROM entities").fetchone()[0] == (
            "precious-data"
        )
    finally:
        connection.close()
    assert list(db_file.parent.glob(".bootstrap-*")) == []


def test_init_db_accepts_a_fully_bootstrapped_database_as_ready(test_settings):
    init_db(test_settings)
    init_db(test_settings)  # still ready, still a no-op, still no raise

    connection = sqlite3.connect(test_settings.db_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 80
    finally:
        connection.close()


def _run_cold_start_round(settings, worker_count: int) -> tuple[list, list]:
    """Fire `worker_count` genuinely concurrent first-requests at a fresh DB.

    Mirrors the shape of the `-P 10` repro used on PR #11: every worker goes
    through the real get_connection path (init_db, connect, query), and a
    barrier makes them all arrive inside the cold-start window together
    rather than serializing past it.
    """
    import threading

    from api.db import get_connection

    barrier = threading.Barrier(worker_count)
    errors: list[str] = []
    counts: list[int] = []
    lock = threading.Lock()

    def worker(index: int):
        barrier.wait()
        try:
            generator = get_connection(settings=settings)
            connection = next(generator)
            try:
                # A write, not only a read. "attempt to write a readonly
                # database" — the symptom of an inode being swapped out from
                # under a live connection — is a *write* error, so a
                # read-only worker structurally cannot observe it.
                connection.execute(
                    "INSERT INTO entities"
                    " (id, type, name, status, attributes, created_at)"
                    " VALUES (?, 'pet', 'p', 'active', '{}',"
                    " '2026-01-01T00:00:00Z')",
                    (f"e{index}",),
                )
                connection.commit()
                count = connection.execute(
                    "SELECT COUNT(*) FROM categories"
                ).fetchone()[0]
                with lock:
                    counts.append(count)
            finally:
                generator.close()
        except BaseException as exc:  # noqa: BLE001 - recording, not handling
            with lock:
                errors.append(f"{type(exc).__name__}: {exc}")

    threads = [
        threading.Thread(target=worker, args=(i,)) for i in range(worker_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return errors, counts


def test_concurrent_cold_start_requests_all_succeed(test_settings):
    """Concurrent first-requests against a not-yet-bootstrapped database.

    This is the measurement a todo was opened on, re-run against the
    temp-file + atomic-rename bootstrap. Both of that race's symptoms come
    from a caller observing the real path mid-bootstrap: `table already
    exists` (two racers both ran executescript) and `no such table`
    (sqlite3.connect creates a 0-byte file before any schema is written, so
    a racer could pass the old guard and query an empty DB). Neither is
    reachable if the database is assembled privately and appears at the real
    path in one atomic step.

    Asserted rather than assumed — the reasoning above is exactly the kind
    that has been wrong twice already on this todo.
    """
    db_file = Path(test_settings.db_path)

    for _ in range(3):
        for path in (db_file, Path(f"{db_file}-wal"), Path(f"{db_file}-shm")):
            path.unlink(missing_ok=True)
        assert not db_file.exists()

        errors, counts = _run_cold_start_round(test_settings, worker_count=10)

        assert errors == []
        assert counts == [80] * 10
        assert list(db_file.parent.glob(".bootstrap-*")) == []

        # Every committed row must still be there. Reads alone cannot catch
        # the clobber path — A installs, a request writes, B installs over
        # it — because the survivor still looks like a complete database.
        # Only a missing committed row exposes it.
        connection = sqlite3.connect(db_file)
        try:
            surviving = {
                row[0] for row in connection.execute("SELECT id FROM entities")
            }
        finally:
            connection.close()
        assert surviving == {f"e{i}" for i in range(10)}


def test_bootstrap_falls_back_to_replace_without_hardlink_support(
    test_settings, monkeypatch
):
    """Not every filesystem supports os.link (exFAT and friends).

    The fallback must still bootstrap rather than failing the request; it
    just loses the create-if-absent guarantee, which is the best available
    on such a filesystem.
    """
    import api.db

    def no_hardlinks(src, dst):
        raise OSError("Operation not supported")

    monkeypatch.setattr(api.db.os, "link", no_hardlinks)

    api.db.init_db(test_settings)

    connection = sqlite3.connect(test_settings.db_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 80
    finally:
        connection.close()
    assert list(Path(test_settings.db_path).parent.glob(".bootstrap-*")) == []


def test_install_does_not_overwrite_a_winner_landing_after_the_final_check(
    test_settings, monkeypatch
):
    """The gap os.link exists to close, made deterministic.

    The pre-install re-check narrows this window but cannot close it: a racer
    can install between that check and ours. Simulated by landing a complete
    database, with a write, immediately after the re-check observes "absent".
    os.link's create-if-absent then refuses to overwrite it; a bare os.replace
    would destroy it and the write.
    """
    import api.db

    real_state = api.db._database_state
    calls = {"count": 0}

    def state_then_a_winner_lands(db_file):
        calls["count"] += 1
        observed = real_state(db_file)
        if calls["count"] == 2:  # the re-check immediately before install
            api.db._bootstrap_into(test_settings, str(db_file))
            connection = sqlite3.connect(db_file)
            try:
                connection.execute(
                    "INSERT INTO entities"
                    " (id, type, name, status, attributes, created_at)"
                    " VALUES ('e1', 'pet', 'Rufus', 'active', '{}',"
                    " '2026-01-01T00:00:00Z')"
                )
                connection.commit()
            finally:
                connection.close()
        return observed

    monkeypatch.setattr(api.db, "_database_state", state_then_a_winner_lands)

    api.db.init_db(test_settings)

    connection = sqlite3.connect(test_settings.db_path)
    try:
        row = connection.execute("SELECT name FROM entities").fetchone()
    finally:
        connection.close()

    assert row is not None, "the winner's database (and its write) was clobbered"
    assert row[0] == "Rufus"
    assert list(Path(test_settings.db_path).parent.glob(".bootstrap-*")) == []


def _process_worker(
    db_path, documents_dir, schema_path, seed_path, index, queue, barrier
):
    """Top-level so it is picklable by multiprocessing on any start method."""
    import sqlite3 as _sqlite3

    from pathlib import Path as _Path

    from api.config import Settings
    from api.db import get_connection

    settings = Settings(
        _env_file=None,
        api_token="test-token",
        # Derived from a path this worker was handed: it runs in a separate
        # process with no `tmp_path` in scope. Redirected even though this
        # worker never reads intake, so no Settings built in the suite
        # carries the REPO_ROOT/intake default that points at real
        # statements.
        intake_dir=str(_Path(db_path).parent / "intake"),
        db_path=db_path,
        documents_dir=documents_dir,
        jyra_dir=str(Path(db_path).parent / "jyra"),
        logs_dir=str(Path(db_path).parent / "logs"),
        schema_path=schema_path,
        seed_categories_path=seed_path,
    )
    barrier.wait()  # all processes enter the cold-start window together
    try:
        generator = get_connection(settings=settings)
        connection = next(generator)
        try:
            connection.execute(
                "INSERT INTO entities"
                " (id, type, name, status, attributes, created_at)"
                " VALUES (?, 'pet', 'p', 'active', '{}', '2026-01-01T00:00:00Z')",
                (f"e{index}",),
            )
            connection.commit()
            count = connection.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
            queue.put(("ok", count))
        finally:
            generator.close()
    except BaseException as exc:  # noqa: BLE001 - reporting, not handling
        queue.put((f"{type(exc).__name__}", str(exc)[:80]))
    finally:
        del _sqlite3


def test_concurrent_cold_start_across_processes(test_settings):
    """The same race across real OS processes, not threads.

    Both this test's threaded sibling and the HTTP repro it came from are
    thread-level concurrency inside one interpreter, which shares a GIL and
    a single sqlite3 module state. os.link's create-if-absent guarantee is
    an operating-system one and has to hold between independent processes —
    which is Jyra's actual shape, a polling loop in its own process racing
    an interactive session in another.
    """
    import multiprocessing

    context = multiprocessing.get_context("fork")
    worker_count = 8
    queue = context.Queue()
    # Without this, process startup jitter lets workers serialize past the
    # cold-start window and the test silently stops testing anything.
    barrier = context.Barrier(worker_count)
    processes = [
        context.Process(
            target=_process_worker,
            args=(
                test_settings.db_path,
                test_settings.documents_dir,
                test_settings.schema_path,
                test_settings.seed_categories_path,
                index,
                queue,
                barrier,
            ),
        )
        for index in range(worker_count)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=60)

    results = [queue.get(timeout=10) for _ in processes]
    failures = [result for result in results if result[0] != "ok"]

    assert failures == []
    assert [count for _, count in results] == [80] * worker_count

    connection = sqlite3.connect(test_settings.db_path)
    try:
        surviving = {row[0] for row in connection.execute("SELECT id FROM entities")}
    finally:
        connection.close()
    assert surviving == {f"e{i}" for i in range(worker_count)}
    assert list(Path(test_settings.db_path).parent.glob(".bootstrap-*")) == []


def test_no_hardlink_fallback_still_refuses_to_overwrite_a_winner(
    test_settings, monkeypatch
):
    """The os.replace in the no-hardlink fallback must stay guarded.

    With the repair path gone, that call is the only remaining os.replace in
    the module — i.e. the one place this class of bug can regrow. On a
    filesystem without hardlinks there is no atomic create-if-absent to lean
    on, so the guard is a plain existence check; it must still be there.
    """
    import api.db

    def no_hardlinks(src, dst):
        raise OSError("Operation not supported")

    monkeypatch.setattr(api.db.os, "link", no_hardlinks)

    real_state = api.db._database_state
    calls = {"count": 0}

    def state_then_a_winner_lands(db_file):
        calls["count"] += 1
        observed = real_state(db_file)
        if calls["count"] == 2:  # the re-check immediately before install
            api.db._bootstrap_into(test_settings, str(db_file))
            connection = sqlite3.connect(db_file)
            try:
                connection.execute(
                    "INSERT INTO entities"
                    " (id, type, name, status, attributes, created_at)"
                    " VALUES ('e1', 'pet', 'Rufus', 'active', '{}',"
                    " '2026-01-01T00:00:00Z')"
                )
                connection.commit()
            finally:
                connection.close()
        return observed

    monkeypatch.setattr(api.db, "_database_state", state_then_a_winner_lands)

    api.db.init_db(test_settings)

    connection = sqlite3.connect(test_settings.db_path)
    try:
        row = connection.execute("SELECT name FROM entities").fetchone()
    finally:
        connection.close()

    assert row is not None, "the winner's database (and its write) was clobbered"
    assert row[0] == "Rufus"
    assert list(Path(test_settings.db_path).parent.glob(".bootstrap-*")) == []


# --- the 2x3 matrix from ticket T-06 (D107) ---------------------------------
#
# impl-1 and review-1 each measured a different condition and were each
# right; the matrix is what settled it without either conceding. It is a
# test now rather than a table in a ticket, because the thing it records
# is a claim about behaviour and claims about behaviour rot.

LOCK_MODES = ["none", "IMMEDIATE", "EXCLUSIVE"]


def _hold(db_path, lock: str):
    """Open an external connection holding `lock`, as another process would."""
    holder = sqlite3.connect(db_path)
    if lock != "none":
        holder.execute(f"BEGIN {lock}")
        # A lock is only actually taken once the statement does something.
        if lock == "IMMEDIATE":
            holder.execute("SELECT COUNT(*) FROM categories").fetchall()
    return holder


@pytest.mark.parametrize("lock", LOCK_MODES)
def test_a_bootstrapped_database_is_never_misread_under_any_lock(
    test_settings, lock
):
    """The whole point of setting WAL at bootstrap.

    Before D107 one cell of this matrix served a database it should have
    refused: delete-mode plus an external BEGIN EXCLUSIVE made the
    read-only probe raise, and the documented
    ambiguity-resolves-to-"ready" fallback answered "ready" — correct for
    "may I overwrite this file", backwards for "may I serve it".

    Under WAL readers are never blocked, so the probe reads
    `sqlite_master` under every lock and the ambiguity never arises. This
    asserts the OUTCOME (a healthy database reads "ready" under all three
    locks) rather than the mechanism, so it still holds if the fallback
    is ever reworked.
    """
    from api.db import _database_state

    init_db(test_settings)
    holder = _hold(test_settings.db_path, lock)
    try:
        assert _database_state(Path(test_settings.db_path)) == "ready"
    finally:
        holder.rollback()
        holder.close()


@pytest.mark.parametrize("lock", ["none", "IMMEDIATE"])
def test_an_incomplete_delete_mode_database_is_refused_under_a_shared_lock(
    test_settings, lock
):
    """The other row, and the one that shows the bug is really gone.

    A healthy database reading "ready" under an exclusive lock proves
    little on its own — "ready" is also what the broken fallback
    returned. This takes a database that MUST be refused, puts it in
    delete mode, and holds each lock over it: the exclusive cell is where
    it used to be served.

    It is left in delete mode deliberately: that is the state a
    hand-built or restored file arrives in. The EXCLUSIVE cell is
    excluded here and pinned in its own test below, because it is a
    residual rather than a pass.
    """
    from api.db import _database_state

    init_db(test_settings)
    connection = sqlite3.connect(test_settings.db_path)
    try:
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("DROP TABLE transactions")
        connection.commit()
    finally:
        connection.close()

    holder = _hold(test_settings.db_path, lock)
    try:
        state = _database_state(Path(test_settings.db_path))
    finally:
        holder.rollback()
        holder.close()

    assert state != "ready", (
        f"a database missing a required table read as servable under a "
        f"{lock} lock — this is the ticket T-06 cell"
    )


def test_a_bootstrapped_database_is_wal_before_any_request_touches_it(
    test_settings,
):
    """Read straight off the file, with no connection through
    get_connection — whose per-request pragma would set WAL anyway and
    make this pass for the wrong reason."""
    init_db(test_settings)

    connection = sqlite3.connect(test_settings.db_path)
    try:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()

    assert mode == "wal"


def test_startup_puts_an_existing_delete_mode_database_into_wal(test_settings):
    """`ensure_wal_mode` covers the gap between bootstrap and the first
    request: a file that already existed and has never been served."""
    from api.db import ensure_wal_mode

    init_db(test_settings)
    demoted = sqlite3.connect(test_settings.db_path)
    try:
        demoted.execute("PRAGMA journal_mode = DELETE")
    finally:
        demoted.close()

    ensure_wal_mode(test_settings)

    connection = sqlite3.connect(test_settings.db_path)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        connection.close()


def test_startup_does_not_write_to_a_database_it_would_refuse(test_settings):
    """The restriction that makes `ensure_wal_mode` safe rather than
    merely helpful.

    apply_startup_migrations already states the rule: a database this app
    is going to reject must be left byte-for-byte as it was found.
    Setting journal_mode WRITES. Doing it to an incomplete database would
    modify the evidence a refusal exists to preserve — and would do it
    before anyone had decided to refuse.
    """
    from api.db import ensure_wal_mode

    init_db(test_settings)
    connection = sqlite3.connect(test_settings.db_path)
    try:
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("DROP TABLE transactions")
        connection.commit()
    finally:
        connection.close()
    before = Path(test_settings.db_path).read_bytes()

    ensure_wal_mode(test_settings)

    assert Path(test_settings.db_path).read_bytes() == before, (
        "a database that will be refused was modified before the refusal"
    )


def test_startup_does_not_bring_a_database_into_existence(test_settings):
    """The guard's SECOND job, which its condition states and its first
    justification does not (review-1 on #128).

    `sqlite3.connect` creates the file it is given. "missing" is not
    "ready", so the same early return that protects an incomplete
    database also stops this function conjuring a 4096-byte database
    where the owner has none — one with no schema in it, which
    `_database_state` then reads back as "corrupt", so the app would
    refuse a file it had just manufactured itself.

    THIS TEST EXISTS BECAUSE THE INVARIANT WAS ONLY CAUGHT BY ACCIDENT.
    Narrowing the condition to `in ("incomplete", "corrupt")` reads as
    more precise and reintroduces exactly this, and before this test the
    only thing that went red was
    test_mcp_mount.py::test_mounting_mcp_left_the_rest_of_the_api_alone
    — a true failure whose name tells the reader nothing about what they
    broke, and so an easy one to dismiss as unrelated. A failure should
    arrive as an instruction rather than a puzzle.
    """
    from api.db import ensure_wal_mode

    db_file = Path(test_settings.db_path)
    if db_file.exists():
        db_file.unlink()
    assert not db_file.exists(), "the fixture must start with no database"

    ensure_wal_mode(test_settings)

    assert not db_file.exists(), (
        "ensure_wal_mode created a database where there was none — "
        "sqlite3.connect creates its file, so the 'missing' state must "
        "return before reaching it"
    )


def test_the_one_residual_cell_is_still_open_and_this_is_deliberate(
    test_settings,
):
    """THE CELL D107 DOES NOT CLOSE. Pinned rather than described.

    A delete-mode, never-served, INCOMPLETE database held under an
    external BEGIN EXCLUSIVE still reads "ready". Three conditions have
    to hold at once, and each of the other tests in this group removes
    one of them:

      * bootstrap now produces WAL, so a database this code made is out
      * `ensure_wal_mode` converts an existing HEALTHY file at startup
      * `get_connection` converts anything else on its first request

    What is left is a file that is both broken and exclusively locked
    before it has ever been served. `ensure_wal_mode` deliberately will
    not touch it: setting journal_mode writes, and writing to a database
    that is about to be refused destroys the evidence the refusal exists
    to preserve (the rule apply_startup_migrations already states).

    So this asserts the WRONG answer on purpose, and the assertion is a
    tripwire rather than an endorsement. If someone later closes this
    cell, this test fails and they should read the trade before deleting
    it: the only ways to close it are to write to a to-be-refused
    database, or to refuse a locked-but-healthy one — which is the false
    refusal #54 exists to prevent. Both were rejected; that is why the
    cell is still here.
    """
    from api.db import _database_state

    init_db(test_settings)
    connection = sqlite3.connect(test_settings.db_path)
    try:
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("DROP TABLE transactions")
        connection.commit()
    finally:
        connection.close()

    holder = sqlite3.connect(test_settings.db_path)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        state = _database_state(Path(test_settings.db_path))
    finally:
        holder.rollback()
        holder.close()

    assert state == "ready", (
        "the residual cell closed. That is not automatically good — see "
        "this test's docstring for what had to be traded away."
    )

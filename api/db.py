import os
import re
import logging
import sqlite3
import tempfile
from pathlib import Path
from typing import Iterator

from fastapi import Depends
from fastapi.exceptions import RequestValidationError

from api.config import REPO_ROOT, Settings, get_settings
from api.errors import InvalidReferenceError

# Deliberate, not sqlite3's unexamined 5.0s stdlib default. WAL leaves only
# writer-vs-writer contention, and a write here is a single small transaction
# (sub-millisecond), so any wait past ~1s already means something abnormal —
# realistically a WAL checkpoint behind a large write batch (e.g. the one-off
# receipt-backlog import CLAUDE.md describes) on a cold or encrypted volume.
# 10s gives that headroom while staying under the 30s timeout typical of HTTP
# clients, so a genuinely stuck lock surfaces as an error this API controls
# rather than a client-side hang.
BUSY_TIMEOUT_SECONDS = 10.0

# The largest page any listing will return. Every route imports this rather
# than repeating the literal, and paginate() enforces it below, so "uniform
# pagination, max 200" is one fact with one definition.
# test_every_paginated_route_declares_the_bounds fails if a route drifts.
MAX_LIMIT = 200


# The tables db/schema.sql declares. Kept honest by
# test_expected_tables_matches_schema_sql, so this can't drift as the schema
# grows — cheaper than re-reading and parsing schema.sql on every request,
# which is what deriving it dynamically would cost on the hot path.
EXPECTED_TABLES = frozenset(
    {
        "artifacts",
        "artifact_revisions",
        "artifact_links",
        "review_items",
        "review_item_history",
        "entities",
        "entity_relationships",
        "notes",
        "note_links",
        "reminders",
        "reminder_instances",
        "google_calendar_connection",
        "google_calendar_events",
        "google_calendar_occurrences",
        "google_calendar_deletions",
        "categories",
        "statements",
        "transactions",
        "merchant_rules",
        "merchant_rule_changes",
        "financial_corrections",
        "source_import_rows",
        "documents",
        "schema_migrations",
        "boards",
        "tickets",
        "ticket_attachments",
        "ticket_transitions",
        "api_tokens",
        "retired_key_prefixes",
    }
)


# The tables schema.sql declared when the migration runner was introduced —
# a frozen historical fact, not a description of the current schema.
#
# NEVER add to this. Its only job is to be the "before" side of a comparison:
# every table in EXPECTED_TABLES but not here must be creatable by a migration
# as well as by schema.sql, which is what
# test_every_table_added_since_the_baseline_has_a_migration asserts. Adding a
# new table here to make that test pass would silence it and brick every
# existing database — exactly the failure the test exists to catch. The fix
# when it fails is always to write the migration.
BASELINE_TABLES = frozenset(
    {
        "entities",
        "entity_relationships",
        "notes",
        "note_links",
        "reminders",
        "reminder_instances",
        "categories",
        "statements",
        "transactions",
        "merchant_rules",
        "documents",
    }
)


class DatabaseNotUsableError(RuntimeError):
    """Something is at the database path, but it cannot be served from.

    Never repaired automatically. Whatever is there was not put there by a
    successful bootstrap — this app only ever installs a fully-assembled
    database — so it is evidence that something went wrong, and silently
    replacing it would destroy that evidence along with any data it holds.
    For a system whose job is being a trustworthy record, that is the wrong
    default: surface it and let a human decide.
    """


class IncompleteDatabaseError(DatabaseNotUsableError):
    """A real database is present but is missing tables this app requires."""


class CorruptDatabaseError(DatabaseNotUsableError):
    """The path holds something that is not a usable database at all."""


def _present_tables(db_file: Path) -> set[str]:
    """Tables in `db_file`, or an empty set if it cannot be read at all."""
    try:
        connection = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    except sqlite3.Error:  # pragma: no cover - best-effort detail only
        return set()
    try:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    except sqlite3.Error:  # pragma: no cover - best-effort detail only
        return set()
    finally:
        connection.close()


def _unusable_error(state: str, db_file: Path) -> DatabaseNotUsableError:
    if state == "incomplete":
        missing = ", ".join(sorted(EXPECTED_TABLES - _present_tables(db_file))) or "unknown"
        return IncompleteDatabaseError(
            f"The database at {db_file} is missing required tables: {missing}. "
            "Remove or repair it by hand; it will not be replaced "
            "automatically."
        )
    return CorruptDatabaseError(
        f"The file at {db_file} is not a usable database — it may be empty, "
        "truncated, or corrupt. Remove it and restart to re-bootstrap a fresh "
        "database; it will not be replaced automatically."
    )


#: The conversion a stuck database needs, quoted verbatim in the refusal
#: so it can be run without guessing at it. SQLite's ROUND is half away
#: from zero, which is what the bank statements this data is transcribed
#: from do -- half-to-even (Python's own default) would disagree with the
#: source document on exactly the values a reconciliation examines. See
#: runbooks/decisions-log.md.
AMOUNT_CENTS_CONVERSION = "CAST(ROUND(amount * 100) AS INTEGER)"


def _transactions_amount_problem(db_file: Path) -> str | None:
    """Why `transactions` cannot be served, or None if it can.

    Returns "stuck" for the shape that predates the rename (`amount`
    present, `amount_cents` absent) and "unservable" for a `transactions`
    that has NEITHER column.

    That second case used to be dismissed here as "handled by the
    EXPECTED_TABLES check". It is not, and review-1 caught the sentence:
    EXPECTED_TABLES compares table NAMES, and `transactions` is present
    in both shapes -- so nothing covered it, and the sentence naming a
    check is exactly what stops the next reader looking for one. Covered
    rather than disclaimed, because a transactions table with no amount
    column at all is unservable for precisely the reason this refusal
    exists: every amount read comes back missing.

    A table with BOTH columns is left alone deliberately. It is what a
    by-hand conversion looks like midway through, and refusing it would
    block the step that fixes it.

    An ABSENT table returns None. PRAGMA gives an empty set for a table
    that is not there, and SQLite cannot create a zero-column table, so
    empty and absent are the same fact -- table presence genuinely is
    another check's job, and this one must not speak for it.

    THE CHECK IS NAME-ONLY. "amount_cents exists" is not "amount_cents is
    usable": this looks at column NAMES and says nothing about their type,
    nullability or contents. A database whose amount_cents is TEXT, or
    full of nulls, passes here. That is deliberate and not a gap to
    close -- the question being asked is whether the RENAME happened, not
    whether the column is healthy, and a check that drifted into column
    health would refuse databases over a different problem while claiming
    this one.
    """
    try:
        connection = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    except sqlite3.Error:  # pragma: no cover - unreadable is another check's job
        return None
    try:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(transactions)")
        }
    except sqlite3.DatabaseError:  # pragma: no cover - ditto
        return None
    finally:
        connection.close()

    if not columns:
        return None
    if "amount_cents" in columns:
        return None
    return "stuck" if "amount" in columns else "unservable"


def _database_state(db_file: Path) -> str:
    """Classify what is at `db_file`: "missing", "corrupt", "incomplete", or
    "ready".

    The split between "missing" and "corrupt" is the load-bearing one, and
    it is why this does not return a single "needs bootstrap" verdict:

    - "missing" — *nothing* is at the path. The only state that may be
      written to, and the reason a fresh install still works: creating a
      database where there is none cannot destroy anything.
    - "corrupt" — a file is there but is not a usable database (empty,
      truncated, not SQLite at all, or valid SQLite with no tables). NOT
      overwritten. This app only ever installs a fully-assembled database,
      so anything else at the path is evidence of a failure, and replacing
      it would both destroy that evidence and require an os.replace that
      can swap an inode out from under a live connection.
    - "incomplete" — a real database missing tables this app requires.
      Also not overwritten, for the same reason plus the data it holds.
    - "ready" — usable, or unclassifiable and therefore left alone.

    Ambiguity resolves to "ready", never to a writable state. A *locked*
    database is "ready": probing an in-use DB raises OperationalError, and
    treating that as writable would overwrite a live, populated database.
    """
    if not db_file.exists():
        return "missing"
    if db_file.stat().st_size == 0:
        return "corrupt"
    try:
        connection = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    except sqlite3.Error:
        return "ready"
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    except sqlite3.DatabaseError as exc:
        # "file is not a database" is unambiguous garbage. Anything else
        # (locked, I/O error, corruption we cannot classify) is ambiguous.
        return "corrupt" if "not a database" in str(exc) else "ready"
    finally:
        connection.close()

    if not tables:
        return "corrupt"
    return "ready" if EXPECTED_TABLES <= tables else "incomplete"


MIGRATION_FILENAME = re.compile(r"^(\d+)_[A-Za-z0-9_.-]+\.sql$")


def _discover_migrations(settings: Settings) -> list[tuple[int, Path]]:
    """Every migration on disk, oldest first.

    Ordered by the integer version, not the filename: string order puts
    `0010_` before `0002_`, which would run a later migration first and, for
    any migration that builds on an earlier one, fail confusingly.
    """
    directory = Path(settings.migrations_dir)
    if not directory.is_dir():
        return []
    found: list[tuple[int, Path]] = []
    for path in directory.iterdir():
        match = MIGRATION_FILENAME.match(path.name)
        if match is not None:
            found.append((int(match.group(1)), path))
    found.sort(key=lambda item: item[0])
    return found


def _ensure_migrations_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, "
        "applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )


def applied_migrations(settings: Settings) -> list[int]:
    """Versions recorded as applied, oldest first. Empty if the database or
    its tracking table does not exist yet."""
    db_file = Path(settings.db_path)
    if not db_file.exists():
        return []
    connection = sqlite3.connect(db_file, timeout=BUSY_TIMEOUT_SECONDS)
    try:
        try:
            rows = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        except sqlite3.DatabaseError:
            # No tracking table: a database predating this mechanism, or not a
            # usable database at all. Either way nothing is recorded.
            return []
        return [row[0] for row in rows]
    finally:
        connection.close()


def pending_migrations(settings: Settings) -> list[int]:
    """Versions on disk that this database has not recorded as applied."""
    done = set(applied_migrations(settings))
    return [version for version, _ in _discover_migrations(settings) if version not in done]


LINE_COMMENT = re.compile(r"--[^\n]*")


def _statements(sql: str) -> list[str]:
    """Split a migration file into statements.

    Deliberately simple: migrations are plain DDL written in this repo, not
    arbitrary user SQL. `executescript` would be simpler still, but it issues
    a COMMIT before running, which would break the explicit transaction that
    makes a failed migration leave nothing behind.

    Comments are stripped before splitting rather than left in place. A
    semicolon inside a `--` comment would otherwise split the file mid-
    sentence and feed prose to SQLite as a statement — which is not
    hypothetical: it broke the first real migration written against this
    runner, whose header comment contained the word "use;".

    The remaining constraint is semicolons inside *string literals* or
    trigger bodies, which no amount of comment handling fixes and which
    db/migrations/README.md tells migration authors to avoid.
    """
    without_comments = LINE_COMMENT.sub("", sql)
    return [chunk.strip() for chunk in without_comments.split(";") if chunk.strip()]


CREATE_TABLE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'`\[]?([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


def _tables_created_by(settings: Settings, versions: list[int]) -> set[str]:
    """Which tables `versions` would create, read from the SQL without running
    it. Lets init_db decide whether migrating would actually make an
    incomplete database whole *before* writing anything to it."""
    wanted = set(versions)
    created: set[str] = set()
    for version, path in _discover_migrations(settings):
        if version in wanted:
            created.update(CREATE_TABLE.findall(path.read_text()))
    return created


def _apply_migrations(settings: Settings, versions: list[int]) -> None:
    """Apply `versions` in order, each in its own transaction.

    Concurrency-safe by the same reasoning as the bootstrap install: `BEGIN
    IMMEDIATE` takes the write lock up front, so two processes racing an
    out-of-date database serialize here rather than both applying the same
    DDL. The loser re-reads `schema_migrations` inside its own transaction,
    sees the version already recorded, and skips it — the equivalent of
    _install's FileExistsError branch, and likewise a success path.
    """
    wanted = {version for version in versions}
    connection = sqlite3.connect(settings.db_path, timeout=BUSY_TIMEOUT_SECONDS)
    connection.isolation_level = None  # explicit transaction control
    try:
        _ensure_migrations_table(connection)
        for version, path in _discover_migrations(settings):
            if version not in wanted:
                continue
            connection.execute("BEGIN IMMEDIATE")
            try:
                already = connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
                ).fetchone()
                if already is not None:
                    connection.execute("ROLLBACK")
                    continue
                for statement in _statements(path.read_text()):
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations (version) VALUES (?)", (version,)
                )
                connection.execute("COMMIT")
            except BaseException:
                # Guarded. A migration that ended the transaction itself
                # -- a .sql file containing COMMIT will do it -- leaves
                # nothing to roll back, and an unguarded ROLLBACK then
                # raises "cannot rollback - no transaction is active",
                # REPLACING the real failure. The error path would
                # destroy the diagnosis at the one moment it is needed,
                # and the logged line would name the cleanup rather than
                # the cause.
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
    finally:
        connection.close()


def _stamp_all_migrations(settings: Settings, target: str) -> None:
    """Record every known migration as applied, without running it.

    A fresh database is built from schema.sql, which already describes the
    schema every migration builds toward. Leaving them pending would re-run
    `CREATE TABLE` over tables that already exist; this is what makes a fresh
    install and a migrated install converge on the same state.
    """
    connection = sqlite3.connect(target, timeout=BUSY_TIMEOUT_SECONDS)
    try:
        _ensure_migrations_table(connection)
        connection.executemany(
            "INSERT OR IGNORE INTO schema_migrations (version) VALUES (?)",
            [(version,) for version, _ in _discover_migrations(settings)],
        )
        connection.commit()
    finally:
        connection.close()


def _bootstrap_into(settings: Settings, target: str) -> None:
    connection = sqlite3.connect(target)
    try:
        # WAL before anything else is written, so a database this code
        # created is never in rollback-journal mode even for one request
        # (D107). journal_mode persists in the file, so setting it here
        # settles it for the file's whole life.
        #
        # This closes one cell of the matrix in ticket T-06, and the
        # cell is the only one that mattered:
        #
        #   journal   external lock      _database_state   result
        #   delete    none / IMMEDIATE   incomplete        refused loudly
        #   delete    EXCLUSIVE          ready             SERVED (wrong)
        #   wal       any of the three   incomplete        refused loudly
        #
        # BEGIN EXCLUSIVE blocks readers outright, so the read-only probe
        # raises and the documented ambiguity-resolves-to-"ready" fallback
        # answers "ready". That fallback is right for the question it was
        # written for — "may I overwrite this file", where never-on-
        # ambiguity is correct — and backwards for the question init_db
        # then asks with the same answer: "may I serve this database".
        #
        # Rather than pick which of those two questions gets the wrong
        # answer, this removes the state in which they disagree: under WAL
        # readers are never blocked, the probe always reads sqlite_master,
        # and the ambiguity never arises.
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(Path(settings.schema_path).read_text())
        connection.executescript(Path(settings.seed_categories_path).read_text())
        connection.commit()
    finally:
        connection.close()
    # Inside the build, before install: the database is still private, so it
    # never appears at db_path with its migrations unstamped.
    _stamp_all_migrations(settings, target)


def _install(temp_path: str, db_file: Path) -> None:
    """Put the finished database at `db_file` without overwriting a winner.

    TWO INVARIANTS MUST HOLD HERE. They are not a record of what passed once;
    they are the reason four distinct failure modes are unreachable, and any
    rework that breaks either one reopens all four. Note also that the
    FileExistsError handler below is a SUCCESS path, not an error one: it is
    how the race's loser adopts the winner's database. Turning it into a
    raise would break every concurrent cold start.

    1. BUILD-THEN-INSTALL. The database is fully assembled — schema, seed,
       commit — in a private temp file *before* it appears at `db_file`, and
       it appears in one indivisible step. No caller can ever observe it
       mid-build. Assembling in place, or installing before seeding, brings
       back `no such table` (a caller reads a file whose schema has not
       landed) and the silent one: a caller reading *between* the schema and
       seed scripts gets a complete-looking database with a partial seed and
       no error at all — measured at 11 of 300 requests against the old code.

    2. CREATE-IF-ABSENT. os.link is atomic create-if-absent: it fails with
       FileExistsError rather than overwriting, so two racers can never both
       install regardless of timing. os.replace must never be used on the
       success path — it would overwrite a racer's database and any write it
       had already accepted, and swapping the inode out from under a live
       connection surfaces as `attempt to write a readonly database`.
       Replacing the check-then-act guard below with a *better* check would
       not help: no check can close a check-then-act window.

    The only os.replace left in this module is the no-hardlink fallback
    below, and it is guarded by an existence check rather than being
    unconditional — that call is where this class of bug would regrow.
    A pre-existing broken file is never repaired here at all: it is
    classified "corrupt" and raised on, which is what makes os.link the
    sole install path and every one of the four modes unreachable.

    """
    try:
        os.link(temp_path, db_file)
    except FileExistsError:
        # NOT an error: a racer installed between our check and this call.
        # Their database is as good as ours, so we keep theirs and discard
        # ours. This must stay a success path — turning it into a raise
        # would break every concurrent cold start, since the race's loser
        # would fail instead of using the winner's database.
        pass
    except OSError:
        # A filesystem without hardlink support (exFAT and friends), where no
        # atomic create-if-absent primitive exists. Best-effort: install only
        # if nothing is at the path. That is still a check-then-act window,
        # unlike os.link — but it is not an *unconditional* overwrite, which
        # is what would let this class of bug regrow here. Out of scope in
        # practice: data/ lives on the local volume (CLAUDE.md: local-only).
        if not db_file.exists():
            os.replace(temp_path, db_file)
            return
    Path(temp_path).unlink(missing_ok=True)


class UnconvertedAmountColumnError(DatabaseNotUsableError):
    """`transactions` still has `amount` and lacks `amount_cents`.

    Not fixable by a migration, which is why it is an error and not a
    migration (ticket T-07). The conversion must be a no-op where the column
    is already gone, and SQLite has no conditional DDL -- a reference to
    `amount` fails at prepare time whether or not a row would be touched,
    so a SQL migration would stop every database that does NOT need it
    from starting.

    Refused rather than served, because the alternative is worse than a
    clear failure: every amount the API reads would come back as nothing
    from a column that is not there, which reads as "no spending" rather
    than as a broken database.
    """


class ProductionDatabaseInTestError(RuntimeError):
    """A test asked to migrate the real database.

    Structural backstop for the autouse fixture in api/tests/conftest.py.
    That fixture stops a test in *that* package from reaching the real
    settings; this holds wherever the test is written, including a future
    lifespan test in tests/ or q_core_mcp/tests/ that nobody thought to
    cover.

    The exposure is specific: api/main.py's lifespan calls
    apply_startup_migrations, so a test that runs the lifespan without
    patching `api.main.get_settings` applies schema migrations to the
    live database — the one holding the only copy of the backlog. Before
    migrations moved to startup this was a stray log line; now it is a
    schema change.
    """


def _test_writable_root() -> Path:
    """Where a test is allowed to put a database.

    Published by the root conftest.py as the pytest temp base, because
    that is the only authoritative answer and it is reachable from pytest
    only. `tempfile.gettempdir()` is the fallback: correct in the default
    configuration, and wrong the moment anyone passes `--basetemp`, which
    can point anywhere including inside the repo. Falling back to it
    degrades to the default-configuration answer rather than to "allow
    everything".
    """
    published = os.environ.get("Q_CORE_TEST_TMP_ROOT")
    if published:
        return Path(published).resolve()

    # Visible rather than silent: the fallback is correct in the default
    # configuration, which is exactly why a run that degraded to it is
    # easy to miss — deleting the publisher failed no test until one was
    # written for it.
    logging.getLogger("api").warning(
        "Q_CORE_TEST_TMP_ROOT is not set; falling back to %s to decide "
        "which paths a test may write. Correct by default, wrong under "
        "--basetemp. The root conftest.py publishes the real value.",
        tempfile.gettempdir(),
    )
    return Path(tempfile.gettempdir()).resolve()


def require_reference(
    connection: sqlite3.Connection, table: str, value: str | None, label: str
) -> None:
    """Refuse a filter that names something which does not exist.

    Lived in api/financial.py as `_require_reference` and served three
    write paths. It is here because the READS need it too: a list
    endpoint filtered by an id that names nothing answered
    `{"items": [], "total": 0}` -- a confident "there are none" that a
    caller cannot tell from the true one. Eleven of the fourteen
    id-shaped filters on this API did that (ticket T-08).

    400, not 404: the thing addressed by the URL exists, and it is a
    VALUE INSIDE the request that refers to something absent. 404 stays
    for the path-addressed case -- `/entities/{id}/relationships` on a
    missing entity is the URL naming nothing. See D17's table.
    """
    if value is None:
        return
    # `table` is never caller-supplied -- only this module's literals and
    # the ones each route passes from its own source.
    if (
        connection.execute(
            f"SELECT 1 FROM {table} WHERE id = ?", (value,)  # noqa: S608
        ).fetchone()
        is None
    ):
        raise InvalidReferenceError(f"No {label} with id {value!r}")


def refuse_real_path_under_pytest(path, *, what: str) -> None:
    """Refuse a path a test has no business writing to.

    Used at every reader that writes somewhere configurable — the
    database, the documents directory, the Jyra attachment root — because
    the autouse fixture in api/tests/conftest.py only covers that package,
    and a test elsewhere holding real settings reaches all of them.
    """
    current_test = os.environ.get("PYTEST_CURRENT_TEST")
    if not current_test:
        return

    # Resolved, not compared as strings: a path spelled
    # "<root>/elsewhere/../data/q-core.db" is lexically outside the
    # prefix and is the same file.
    db_path = Path(path).resolve()

    # An ALLOW-list, not a deny-list. The deny-list version compared
    # against REPO_ROOT of the *running* checkout, so an absolute
    # Q_CORE_DB_PATH pointing at another checkout's data/ walked straight
    # past it — and every implementer works in a worktree, where that is
    # exactly the configuration. A deny-list has to enumerate every place
    # the real database might be; this needs only the one place a test may
    # write.
    writable_root = _test_writable_root()
    if db_path.is_relative_to(writable_root):
        # Belt and braces: allowed by location, but still refuse anything
        # under a checkout's data/. This only bites if the published root
        # somehow contains the repo — `--basetemp` pointing inside it, for
        # instance, which is a configuration that exists.
        data_dir = (REPO_ROOT / "data").resolve()
        if not db_path.is_relative_to(data_dir):
            return
        raise ProductionDatabaseInTestError(
            f"Refusing to touch {what} at {db_path} from a test "
            f"(PYTEST_CURRENT_TEST={current_test!r}): it is inside a "
            f"checkout's data/ ({data_dir}). It is under the pytest temp "
            "root, so the temp root and the repository overlap — most "
            "likely --basetemp points inside the checkout."
        )

    raise ProductionDatabaseInTestError(
        f"Refusing to touch {what} at {db_path} from a test "
        f"(PYTEST_CURRENT_TEST={current_test!r}): it is outside the "
        f"pytest temp root {_test_writable_root()}, so it is not a "
        "database this test created. Something ran the app's lifespan "
        "with settings pointing at a real checkout — the autouse fixture "
        "in api/tests/conftest.py prevents that for tests in that "
        "package; a test elsewhere has to point `db_path` at tmp_path "
        "itself."
    )


def ensure_wal_mode(settings: Settings) -> None:
    """Put an existing database into WAL before the first request is served.

    Bootstrap already sets WAL for databases this code creates (D107), and
    `get_connection` migrates any other file on its first request. This
    covers the gap between those two: a file that existed before startup
    and has never been served — hand-built, hand-repaired, or restored
    from a rollback-journal backup. That is the residual case ticket
    ticket T-06 named, and "before the first serve" is the only moment it can
    be closed from.

    ONLY WHEN THE DATABASE IS ALREADY "ready", and that restriction is not
    caution — it is the same rule apply_startup_migrations states below:
    a database this app is going to refuse must be left byte-for-byte as
    it was found. Setting journal_mode WRITES to the file. Doing it to an
    "incomplete" or "corrupt" database would modify the evidence the
    refusal exists to preserve, and would do it before anyone had decided
    to refuse.

    So this narrows the window rather than closing it: a never-served
    delete-mode file that is ALSO incomplete stays untouched and keeps the
    original ambiguity. That is the right trade — an incomplete database
    is refused on every other path anyway, and preserving a broken file
    intact matters more than the mode it is broken in.
    """
    db_file = Path(settings.db_path)
    if _database_state(db_file) != "ready":
        return

    connection = sqlite3.connect(db_file)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        # Same swallow, same reason as get_connection's: the migration
        # needs a brief EXCLUSIVE lock and can lose to an external
        # reader. Raising here would abort startup over a mode the next
        # uncontended request sets anyway.
        logging.getLogger("api").warning(
            "could not set journal_mode=WAL at startup; the first "
            "uncontended request will retry"
        )
    finally:
        connection.close()


def apply_startup_migrations(settings: Settings) -> None:
    """Bring the database's schema up to date. Called once, at startup.

    The one place migrations are applied. Running here rather than in
    `init_db` is what makes a column- or index-only migration possible at
    all: `init_db` is per-request and reaches migrations only when a
    *table* is missing, so a change that alters an existing table was
    skipped silently — `pending_migrations` still listed it and nothing
    said so.

    Failure is deliberately fatal. `_apply_migrations` runs each version in
    its own transaction and records it only on success, so the database is
    left at the previous version; the alternative — serving with a
    half-known schema — turns one clear failure into a stream of unrelated
    ones far from the cause.
    """
    refuse_real_path_under_pytest(settings.db_path, what="the database")

    db_file = Path(settings.db_path)
    state = _database_state(db_file)

    if state == "missing":
        # Nothing to upgrade. The first request bootstraps from schema.sql
        # and stamps every known version as applied, because schema.sql
        # already describes the state those migrations build toward.
        return

    if state == "corrupt":
        # Not this function's call to make. init_db refuses it per request,
        # with the evidence intact.
        return

    # Before the pending check, deliberately: a database can be stuck in
    # the pre-rename shape with every migration already applied, because
    # no migration can carry it across (ticket T-07). Testing this only when
    # something is pending would let exactly the stuck case through.
    problem = _transactions_amount_problem(db_file)
    if problem == "unservable":
        raise UnconvertedAmountColumnError(
            f"The database at {db_file} has a transactions table with "
            "neither `amount_cents` nor `amount`, so no transaction "
            "amount can be read from it at all. This is not the "
            "pre-rename shape and no conversion applies; the table has "
            "been built or altered by something other than this app. "
            "Repair it by hand against db/schema.sql."
        )
    if problem == "stuck":
        raise UnconvertedAmountColumnError(
            f"The database at {db_file} has transactions.amount and no "
            "transactions.amount_cents, so it predates the move to integer "
            "cents and cannot be served: every amount this API reads would "
            "come back missing, which looks like no spending rather than a "
            "broken database.\n\n"
            "No migration can do this, which is why there is not one. The "
            "conversion has to be a no-op on a database that has already "
            "been converted, and SQLite has no conditional DDL -- a "
            "reference to `amount` fails at prepare time whether or not a "
            "row would be touched, so a SQL migration would stop every "
            "database that does NOT need it from starting.\n\n"
            "Convert it BY HAND, with transactions.amount_cents populated "
            f"as {AMOUNT_CENTS_CONVERSION} -- SQLite's ROUND is half away "
            "from zero, which is what bank statements do; half-to-even "
            "would disagree with the source document on exactly the "
            "values a reconciliation examines. Rebuild the table rather "
            "than ALTER: the column changes type as well as name."
        )

    pending = pending_migrations(settings)
    if not pending:
        return

    if state == "incomplete":
        # Checked before applying, never after: a database this app is
        # going to reject must be left byte-for-byte as it was found.
        # Writing a tracking table into it and *then* discovering the
        # migrations did not help would modify the very evidence the
        # refusal exists to preserve.
        missing = EXPECTED_TABLES - _present_tables(db_file)
        if not missing <= _tables_created_by(settings, pending):
            return

    logger = logging.getLogger("api")
    for version, path in _discover_migrations(settings):
        if version not in pending:
            continue
        try:
            _apply_migrations(settings, [version])
        except sqlite3.Error as exc:
            logger.error(
                "migration %s failed: %s", path.name, exc, exc_info=True
            )
            raise
        logger.info("applied migration %s", path.name)


def init_db(settings: Settings) -> None:
    db_file = Path(settings.db_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    # Before mkdir, not after: these create directories in whatever
    # checkout the settings point at, database or no database.
    refuse_real_path_under_pytest(settings.documents_dir, what="the documents directory")
    refuse_real_path_under_pytest(settings.jyra_dir, what="the Jyra attachment root")
    Path(settings.documents_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.jyra_dir).mkdir(parents=True, exist_ok=True)
    # attach_file's DEFAULT source only, never every configured root.
    #
    # This runs per request through get_connection, so mkdir on a
    # user-supplied path meant a typo'd attachment_roots raised from inside
    # the connection dependency and 500'd every database request -- with an
    # error naming neither attachments nor the setting that caused it. A
    # bad value for one optional feature took down reading a transaction.
    #
    # Configured roots are checked at attach time instead, where the
    # refusal can name the setting and only that call fails. And only while
    # the default is configured: an installed app's REPO_ROOT is read-only
    # (the Nix store), and its service names its own inbox.
    default_inbox = REPO_ROOT / "data" / "attachments-inbox"
    if str(default_inbox) in settings.attachment_roots:
        default_inbox.mkdir(parents=True, exist_ok=True)

    state = _database_state(db_file)

    if state == "ready":
        # Returns immediately, without checking for pending migrations —
        # still, but for a different reason than it used to. The original
        # rationale was that the check would block on a locked database;
        # measured, WAL readers do not block (a schema_migrations read
        # during another connection's uncommitted BEGIN IMMEDIATE took
        # 0.04 ms, and 0.08 ms during a 361 ms VACUUM). What survives is
        # cost: this runs on every request, and startup-only removes the
        # question entirely. See apply_startup_migrations.
        return

    # No migration step here. Migrations apply in exactly one place —
    # apply_startup_migrations, called from api/main.py's lifespan — and
    # init_db is per-request: its only caller is get_connection. A second
    # path that can write schema changes is what made the old behaviour
    # hard to reason about, and it could only ever reach table migrations
    # anyway. What remains here is classification: by the time a request is
    # served, a database still missing tables is unexplained incompleteness
    # rather than a known delta, and that still refuses.

    if state != "missing":
        # Re-read before refusing. `state` was sampled at the top of this
        # function, and init_db runs concurrently — FastAPI serves sync
        # routes from a threadpool, and get_connection calls this on every
        # request — so a racer may have applied the pending migration in
        # between. That racer leaves `pending_migrations` empty, so the
        # branch above is skipped, and refusing on the stale `state` here
        # rejected a database that is healthy by now. The tell in the
        # failures this caused was the error naming no table at all:
        # "missing required tables: unknown".
        #
        # This closes the window by construction rather than narrowing it,
        # because _apply_migrations commits the migration's DDL and its
        # schema_migrations row in ONE transaction (BEGIN IMMEDIATE ...
        # COMMIT), for a multi-statement migration as much as a
        # single-statement one. Measured, not assumed: an external reader
        # polling both facts inside a single snapshot never once saw the
        # row recorded without the table or the reverse — ~57k samples for
        # a one-statement migration, ~35k for a 25-statement one watching
        # the last table it creates. So a re-read observes strictly before
        # or strictly after; there is no in-between state to catch.
        #
        # Splitting that into two commits — or committing per statement —
        # would silently reduce this to a probability argument. That is
        # guarded, not merely warned about: see
        # test_a_migrations_ddl_and_its_record_commit_in_one_transaction,
        # which asserts the statement sequence directly and is
        # parametrized over both migration shapes.
        state = _database_state(db_file)
        if state == "ready":
            return
        raise _unusable_error(state, db_file)

    # Assemble the database in a private temp file and install it with a
    # single atomic create-if-absent. See _install for the invariants.
    descriptor, temp_path = tempfile.mkstemp(
        dir=db_file.parent, prefix=".bootstrap-", suffix=".db"
    )
    os.close(descriptor)
    try:
        _bootstrap_into(settings, temp_path)
        # Re-check: a racer may have installed while we were building.
        final_state = _database_state(db_file)
        if final_state == "missing":
            _install(temp_path, db_file)
            return
        Path(temp_path).unlink(missing_ok=True)
        if final_state != "ready":
            raise _unusable_error(final_state, db_file)
    except BaseException:
        Path(temp_path).unlink(missing_ok=True)
        raise


def get_connection(
    settings: Settings = Depends(get_settings),
) -> Iterator[sqlite3.Connection]:
    init_db(settings)
    # check_same_thread=False: FastAPI runs yield-dependencies and sync route
    # handlers via anyio.to_thread.run_sync, and this connection's open/use/
    # close calls are not guaranteed to land on the same OS thread. Safe here
    # because the connection is single-owner, single-request-scoped (opened
    # and closed within one dependency's generator, never shared across
    # requests) — this only disables a same-thread identity check that would
    # otherwise false-positive in that pattern, not real concurrent access.
    connection = sqlite3.connect(
        settings.db_path,
        check_same_thread=False,
        timeout=BUSY_TIMEOUT_SECONDS,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    # Unconditionally, every request — not only in init_db's bootstrap branch.
    # Unlike foreign_keys, journal_mode is persisted in the database file, so a
    # file created before this change (data/q-core.db already exists from
    # Phase 1) would otherwise stay in rollback-journal mode forever. This is a
    # no-op once the file is already WAL, same cost category as foreign_keys.
    #
    # WAL is the setting that matters for a poller reading alongside an
    # interactive session: in rollback-journal mode a writer blocks readers
    # once it escalates to EXCLUSIVE — at commit, or earlier if its page cache
    # spills. (It does NOT block them for the whole transaction; a small write
    # holds only a RESERVED lock, which readers are allowed past.) WAL removes
    # reader/writer blocking entirely, leaving only writer-vs-writer
    # contention, which BUSY_TIMEOUT_SECONDS above is the net for.
    try:
        connection.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        # The migration itself needs a brief EXCLUSIVE lock and loses to an
        # in-flight reader. Harmless: the DB stays in its current mode and the
        # next uncontended request migrates it. Never fires once already WAL,
        # so this is a one-time window, not an ongoing failure path. Swallowed
        # because raising here would take down an otherwise fine request from
        # a dependency, as a bare 500 with no error envelope.
        pass
    try:
        yield connection
    finally:
        connection.close()


def paginate(
    connection: sqlite3.Connection,
    base_query: str,
    params: tuple,
    limit: int,
    offset: int,
) -> dict:
    """Paginate `base_query`.

    `base_query` must be a fixed SQL string — any filter/sort values must
    be passed via `params`, never interpolated into `base_query` itself.

    Bad limit/offset values raise RequestValidationError, so they reach the
    client as a 422 in the standard error envelope rather than as an
    unhandled 500. Every route is also expected to declare
    `Query(ge=1, le=MAX_LIMIT)` — that is what produces a good error message
    naming the constraint, and test_every_paginated_route_declares_the_bounds
    enforces it. This check is the backstop for when it is forgotten, which
    happened on four separate routes across two plans before it existed
    (a todo). A forgotten guard now degrades to the correct status code
    instead of a traceback.

    Raising an HTTP-shaped error from a DB helper is a deliberate layering
    compromise, matching validate_entity_attributes in api/models.py: the
    alternative is every caller translating a ValueError identically, which
    is the duplication that produced the bug in the first place.
    """
    if limit <= 0 or limit > MAX_LIMIT:
        raise RequestValidationError(
            [
                {
                    "type": "value_error",
                    "loc": ("query", "limit"),
                    "msg": f"limit must be between 1 and {MAX_LIMIT}, got {limit}",
                    "input": limit,
                }
            ]
        )
    if offset < 0:
        raise RequestValidationError(
            [
                {
                    "type": "value_error",
                    "loc": ("query", "offset"),
                    "msg": f"offset must be non-negative, got {offset}",
                    "input": offset,
                }
            ]
        )

    total = connection.execute(
        f"SELECT COUNT(*) FROM ({base_query})", params
    ).fetchone()[0]
    rows = connection.execute(
        f"{base_query} LIMIT ? OFFSET ?", (*params, limit, offset)
    ).fetchall()
    return {
        "items": [dict(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }

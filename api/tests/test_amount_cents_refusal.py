"""Startup refuses to serve a database stuck before the amount rename.

ticket T-07, ruled NOT fixable by migration. At the runner's landing commit
`transactions` declared `amount NUMERIC`; today schema.sql declares
`amount_cents INTEGER`, and no migration carries a database across --
because none can. The conversion needs to be a no-op where the column is
already gone, and SQLite has no conditional DDL: a reference to `amount`
fails at prepare time whether or not a row would be touched.

A Python migration class would express it, and was rejected: every
migration guard in this repo parses SQL -- the transaction-control
check, the table-additive parity, the column baseline -- so the first
Python migration would be the excluded category where the hole is,
introduced for a database nobody has.

So the gap is made LOUD instead. A database in that shape is refused at
startup, naming the conversion, rather than served while every amount
read silently returns nothing.

The two directions are both tested, and the second is the one that would
brick a working system: refusing the shape that is actually deployed.
"""

import sqlite3
from pathlib import Path

import pytest

from api.config import REPO_ROOT, Settings
from api.db import apply_startup_migrations, init_db

PRE_RENAME = "    amount        NUMERIC NOT NULL,"
POST_RENAME = (
    "    amount_cents  INTEGER NOT NULL,      -- negative = debit, positive = credit"
)


def _settings(tmp_path: Path, schema: Path) -> Settings:
    return Settings(
        _env_file=None,
        api_token="test-token",
        intake_dir=str(tmp_path / "intake"),
        db_path=str(tmp_path / "q-core.db"),
        documents_dir=str(tmp_path / "documents"),
        jyra_dir=str(tmp_path / "jyra"),
        logs_dir=str(tmp_path / "logs"),
        schema_path=str(schema),
        seed_categories_path=str(REPO_ROOT / "db" / "seed_categories.sql"),
        migrations_dir=str(REPO_ROOT / "db" / "migrations"),
    )


def _pre_rename_database(tmp_path: Path) -> Settings:
    """Built by stripping the rename out of schema.sql, the way #137 built
    its own "before" -- a frozen copy of the old file stops describing
    reality the moment anything else in that table changes."""
    schema = (REPO_ROOT / "db" / "schema.sql").read_text()
    assert POST_RENAME in schema, "schema.sql no longer matches the strip target"
    older = schema.replace(POST_RENAME, PRE_RENAME)
    assert older != schema

    path = tmp_path / "old-schema.sql"
    path.write_text(older)
    settings = _settings(tmp_path, path)
    init_db(settings)

    connection = sqlite3.connect(settings.db_path)
    try:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(transactions)")
        }
    finally:
        connection.close()
    assert "amount" in columns and "amount_cents" not in columns
    return settings


def test_a_pre_rename_database_is_refused_at_startup(tmp_path):
    settings = _pre_rename_database(tmp_path)

    with pytest.raises(Exception) as raised:
        apply_startup_migrations(settings)

    message = str(raised.value)
    # Names the column...
    assert "amount_cents" in message
    assert "transactions" in message
    # ...the conversion, exactly, so it can be run without guessing...
    assert "CAST(ROUND(amount * 100) AS INTEGER)" in message
    assert "half away from zero" in message
    # ...and that it is manual, with why. Case-insensitive: the message
    # capitalises BY HAND for emphasis, and pinning the case would make
    # this a test of typography.
    assert "by hand" in message.lower()
    assert "no migration can" in message.lower(), "the refusal must say why"


def test_the_deployed_shape_is_not_refused(tmp_path):
    """The direction that would brick a working system.

    A database with `amount_cents` and no `amount` is every database
    created from schema.sql since the rename, including the only live
    one. Refusing it would stop the API from starting.
    """
    settings = _settings(tmp_path, REPO_ROOT / "db" / "schema.sql")
    init_db(settings)

    connection = sqlite3.connect(settings.db_path)
    try:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(transactions)")
        }
    finally:
        connection.close()
    assert "amount_cents" in columns and "amount" not in columns

    apply_startup_migrations(settings)  # must not raise


def test_a_fresh_install_is_not_refused(tmp_path):
    """The bootstrap path returns before the check; asserted rather than
    assumed, since a database that does not exist yet has no columns to
    read and a naive check would fail trying."""
    settings = _settings(tmp_path / "fresh", REPO_ROOT / "db" / "schema.sql")
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)

    apply_startup_migrations(settings)  # nothing there yet; must not raise


# --- the third shape: neither column ------------------------------------


def test_a_transactions_with_neither_amount_column_is_refused(tmp_path):
    """review-1's hold on #144.

    The docstring said this case was "handled by the EXPECTED_TABLES
    check". It was not: that check compares table NAMES, and
    `transactions` is present either way. Naming a check that does not
    cover it is what stops the next reader looking for one, so it is
    covered here rather than disclaimed.
    """
    settings = _settings(tmp_path, REPO_ROOT / "db" / "schema.sql")
    init_db(settings)

    connection = sqlite3.connect(settings.db_path)
    try:
        connection.execute("ALTER TABLE transactions DROP COLUMN amount_cents")
        connection.commit()
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(transactions)")
        }
    finally:
        connection.close()
    assert "amount" not in columns and "amount_cents" not in columns

    with pytest.raises(Exception) as raised:
        apply_startup_migrations(settings)

    message = str(raised.value)
    assert "neither" in message
    # Distinct from the stuck shape: no conversion applies, so offering
    # one would send someone to run SQL against a column that is not there.
    assert "CAST(ROUND" not in message
    assert "repair it by hand" in message.lower()


def test_a_table_with_both_columns_is_left_alone(tmp_path):
    """What a by-hand conversion looks like midway through.

    Refusing it would block the very step that fixes the stuck shape,
    which would make the refusal a trap rather than a guard.
    """
    schema = (REPO_ROOT / "db" / "schema.sql").read_text()
    older = schema.replace(POST_RENAME, PRE_RENAME)
    path = tmp_path / "old-schema.sql"
    path.write_text(older)
    settings = _settings(tmp_path, path)
    init_db(settings)

    connection = sqlite3.connect(settings.db_path)
    try:
        connection.execute("ALTER TABLE transactions ADD COLUMN amount_cents INTEGER")
        connection.commit()
    finally:
        connection.close()

    apply_startup_migrations(settings)  # must not raise


def test_an_absent_transactions_table_is_not_this_checks_business(tmp_path):
    """PRAGMA gives an empty set for a table that is not there, which is
    indistinguishable from one with no columns. Table presence is the
    EXPECTED_TABLES check's job -- genuinely, this time -- so this one
    must not speak for it and claim a shape it cannot see."""
    from api.db import _transactions_amount_problem

    settings = _settings(tmp_path, REPO_ROOT / "db" / "schema.sql")
    init_db(settings)

    connection = sqlite3.connect(settings.db_path)
    try:
        connection.execute("DROP TABLE transactions")
        connection.commit()
    finally:
        connection.close()

    assert _transactions_amount_problem(Path(settings.db_path)) is None


def test_absent_and_amountless_are_told_apart(tmp_path):
    """The discrimination, in one place rather than inferred from two
    tests passing separately.

    These are the two shapes a naive probe conflates: `PRAGMA
    table_info` returns an empty set for a table that is not there, and
    an empty set is also what you would get from a table with no columns.
    Reading "no amount_cents" off an empty set refuses a database whose
    real problem is a MISSING TABLE -- which the incomplete-database
    check already handles, and handles better, because it can say which
    tables.

    Asserted side by side so the difference is the assertion rather than
    a property two unrelated tests happen to share.
    """
    from api.db import _transactions_amount_problem

    absent = _settings(tmp_path / "absent", REPO_ROOT / "db" / "schema.sql")
    Path(absent.db_path).parent.mkdir(parents=True, exist_ok=True)
    init_db(absent)
    connection = sqlite3.connect(absent.db_path)
    try:
        connection.execute("DROP TABLE transactions")
        connection.commit()
    finally:
        connection.close()

    amountless = _settings(tmp_path / "amountless", REPO_ROOT / "db" / "schema.sql")
    Path(amountless.db_path).parent.mkdir(parents=True, exist_ok=True)
    init_db(amountless)
    connection = sqlite3.connect(amountless.db_path)
    try:
        connection.execute("ALTER TABLE transactions DROP COLUMN amount_cents")
        connection.commit()
    finally:
        connection.close()

    assert _transactions_amount_problem(Path(absent.db_path)) is None
    assert _transactions_amount_problem(Path(amountless.db_path)) == "unservable"

    # And the live shape is neither, which is the direction that matters.
    live = _settings(tmp_path / "live", REPO_ROOT / "db" / "schema.sql")
    Path(live.db_path).parent.mkdir(parents=True, exist_ok=True)
    init_db(live)
    assert _transactions_amount_problem(Path(live.db_path)) is None

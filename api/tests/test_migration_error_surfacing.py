"""A failing migration must report its own error, not the cleanup's.

Found while building the rejected Python-migration branch (#143), but it
is a defect in the SQL runner and independent of that mechanism: if a
migration ends the transaction the runner opened, the runner's own
`except` does an unguarded ROLLBACK, which raises

    cannot rollback - no transaction is active

and that replaces the real failure. The diagnosis is destroyed by the
error path, at the one moment the diagnosis is what is needed.

A `.sql` migration reaches this the same way a Python one would: the file
simply contains COMMIT.
"""

import sqlite3
from pathlib import Path

import pytest

from api.config import REPO_ROOT, Settings
from api.db import apply_startup_migrations, applied_migrations, init_db


def _settings(tmp_path: Path, migrations_dir: Path) -> Settings:
    return Settings(
        _env_file=None,
        api_token="test-token",
        intake_dir=str(tmp_path / "intake"),
        db_path=str(tmp_path / "q-core.db"),
        documents_dir=str(tmp_path / "documents"),
        jyra_dir=str(tmp_path / "jyra"),
        logs_dir=str(tmp_path / "logs"),
        schema_path=str(REPO_ROOT / "db" / "schema.sql"),
        seed_categories_path=str(REPO_ROOT / "db" / "seed_categories.sql"),
        migrations_dir=str(migrations_dir),
    )


def test_a_migration_that_ends_the_transaction_still_reports_its_own_error(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    settings = _settings(tmp_path, migrations)
    init_db(settings)

    (migrations / "0001_ends_the_transaction.sql").write_text(
        "COMMIT;\nSELECT * FROM a_table_that_does_not_exist;\n"
    )

    with pytest.raises(sqlite3.Error) as raised:
        apply_startup_migrations(settings)

    message = str(raised.value)
    assert "a_table_that_does_not_exist" in message, (
        "the migration's own error was replaced by the cleanup's: " + message
    )
    assert "cannot rollback" not in message


def test_an_ordinary_failing_migration_is_unaffected(tmp_path):
    """The guard must not change the normal path.

    A migration that fails WITHOUT ending the transaction still rolls
    back, leaves nothing behind, and is not recorded.
    """
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    settings = _settings(tmp_path, migrations)
    init_db(settings)

    (migrations / "0001_plain_failure.sql").write_text(
        "ALTER TABLE entities ADD COLUMN probe TEXT;\n"
        "SELECT * FROM a_table_that_does_not_exist;\n"
    )

    with pytest.raises(sqlite3.Error):
        apply_startup_migrations(settings)

    connection = sqlite3.connect(settings.db_path)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(entities)")}
    finally:
        connection.close()
    assert "probe" not in columns, "the partial change survived the rollback"
    assert 1 not in applied_migrations(settings)

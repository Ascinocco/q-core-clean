from pathlib import Path
import json

import pytest

from api.config import REPO_ROOT, Settings


@pytest.fixture()
def test_settings(tmp_path: Path) -> Settings:
    profile = tmp_path / 'privacy.json'
    profile.write_text(json.dumps({'version': 1, 'reviewed': True,
                                   'names': ['Alex Example'], 'addresses': ['123 Example Lane']}))
    profile.chmod(0o600)
    return Settings(
        _env_file=None,
        api_token="test-token",
        db_path=str(tmp_path / "q-core.db"),
        documents_dir=str(tmp_path / "documents"),
        jyra_dir=str(tmp_path / "jyra"),
        eval_summaries_dir=str(tmp_path / "eval-summaries"),
        forecast_dir=str(tmp_path / "forecast"),
        logs_dir=str(tmp_path / "logs"),
        intake_dir=str(tmp_path / "intake"),
        inbox_dir=str(tmp_path / "inbox"),
        secrets_dir=str(tmp_path / "secrets"),
        # Never this machine's Keychain: a test that fell back to it once
        # sent the real OAuth client to Google (pre-deploy follow-ups).
        google_token_store="file",
        privacy_profile_path=str(profile),
        schema_path=str(REPO_ROOT / "db" / "schema.sql"),
        seed_categories_path=str(REPO_ROOT / "db" / "seed_categories.sql"),
        migrations_dir=str(REPO_ROOT / "db" / "migrations"),
    )


from api.config import get_settings


@pytest.fixture()
def client(test_settings: Settings):
    from fastapi.testclient import TestClient

    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    test_client = TestClient(app)
    _assert_foreign_keys_on(test_settings)
    yield test_client
    app.dependency_overrides.clear()


def _assert_foreign_keys_on(settings: Settings) -> None:
    """Nothing else in this suite notices when this pragma goes missing.

    `PRAGMA foreign_keys` is per-connection and defaults to OFF in SQLite;
    `get_connection` turns it on for every request.

    This docstring has now been wrong twice, in opposite directions, and
    the second time is the interesting one.

    First I claimed the FK-refusal tests would "pass vacuously" without the
    pragma. I measured that with the pre-delete check ALSO removed -- a
    tree that no longer exists -- and it failed there, so I wrote the
    claim off as false and called this a mere debugging aid.

    Measured on THIS tree, with `_reference_counts` in place (review-1
    found it):

        pragma removed                      -> 1 passed
        pragma removed AND ENTITY_REFERENCES emptied -> 1 failed

    `test_delete_entity_blocked_by_foreign_key` no longer depends on the
    pragma at all. This PR is why: the pre-delete count refuses first, so
    the FK became a backstop, and a backstop that stops being enforced is
    invisible by construction -- the visible behaviour is identical. The
    second line is the proof: only once the count is gone too does the
    missing FK show up.

    So the check is not a debugging aid. It is the only thing in the suite
    that would notice the pragma disappearing on this path, and it earns
    its place more than the version that called it decoration. The lesson
    is that a measurement describes the tree it was taken on: mine was
    taken on a counterfactual one and written up as though it described
    this one.
    """
    from api.db import get_connection

    connections = get_connection(settings)
    connection = next(connections)
    try:
        enabled = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        assert enabled == 1, (
            "PRAGMA foreign_keys is OFF on the app's own connection. The "
            "delete guard's pre-count refuses before the FK is ever reached, "
            "so nothing else here would have told you: the FK is a backstop, "
            "and an unenforced backstop looks exactly like an enforced one."
        )
    finally:
        connections.close()


@pytest.fixture(autouse=True)
def _never_the_real_settings(request, monkeypatch):
    """Point `api.main`'s module-level `get_settings` at the test settings.

    Autouse, not opt-in, and that is the whole point. `app.dependency_
    overrides[get_settings]` only covers `Depends`; the lifespan calls
    `get_settings()` directly, so any test that enters `TestClient` as a
    context manager gets the **real** settings — and since #67 the lifespan
    applies migrations, which means the real database. That was a stray log
    line before; now it is a schema change against the file holding the
    backlog.

    A shared opt-in fixture would be a convention, and a convention is
    exactly what fails here: the test that forgets is the one that does the
    damage, and it looks identical to one that remembered. Autouse makes
    forgetting impossible instead of discouraged.

    `api.db.apply_startup_migrations` carries a second, structural refusal
    for the same hazard, because this fixture only covers `api/tests`.

    Deliberately with no opt-out. `test_missing_config_returns_clean_500`
    still works: it exercises the `Depends(get_settings)` path, which
    resolves the function object from `api.config` rather than this name,
    so patching here does not reach it. An unused escape hatch would only
    be something to reach for later.
    """
    import api.google_calendar
    import api.main

    monkeypatch.setattr(
        api.main, "get_settings", lambda: request.getfixturevalue("test_settings")
    )
    # google_calendar calls get_settings() directly too; without this it saw
    # whatever the environment held (review of q-core #12, R1-F2).
    monkeypatch.setattr(
        api.google_calendar, "get_settings", lambda: request.getfixturevalue("test_settings")
    )

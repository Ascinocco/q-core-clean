"""Scoped, revocable client tokens (split, part 2). Invented values only."""
import json
import sqlite3

import pytest

from api import tokens

SERVICE = {"Authorization": "Bearer test-token"}


def _db(test_settings):
    connection = tokens.open_connection(test_settings)
    return connection


def _bearer(secret):
    return {"Authorization": f"Bearer {secret}"}


def test_create_shows_the_secret_once_and_stores_only_a_hash(client, test_settings):
    client.get("/entities", headers=SERVICE)  # bootstrap the database
    db = _db(test_settings)
    secret, record = tokens.create(db, "mac", "full")
    assert secret.startswith("qc_") and len(secret) > 40
    assert "token" not in record and "token_hash" not in record
    raw = db.execute("SELECT * FROM api_tokens").fetchone()
    assert raw["token_hash"] == tokens.token_hash(secret) and secret not in json.dumps(dict(raw))
    assert record["prefix"] == secret[:10]
    listed = tokens.list_tokens(db)
    assert secret not in json.dumps(listed) and listed[0]["name"] == "mac"


def test_full_token_reaches_routes_and_mcp_until_revoked(client, test_settings):
    client.get("/entities", headers=SERVICE)
    db = _db(test_settings)
    secret, _ = tokens.create(db, "mac", "full")
    assert client.get("/entities", headers=_bearer(secret)).status_code == 200
    mcp = client.post("/mcp/", headers={**_bearer(secret), "Accept": "application/json, text/event-stream"},
                      json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    # Past auth: without the lifespan the mount answers "not running" (503).
    assert mcp.status_code == 503 and "not running" in mcp.text
    tokens.revoke(db, "mac")
    assert client.get("/entities", headers=_bearer(secret)).status_code == 401
    assert client.post("/mcp/", headers=_bearer(secret), json={}).status_code == 401


def test_transcription_scope_reaches_only_transcription(client, test_settings):
    client.get("/entities", headers=SERVICE)
    db = _db(test_settings)
    secret, _ = tokens.create(db, "phone", "transcription")
    assert client.get("/entities", headers=_bearer(secret)).status_code == 401
    assert client.post("/mcp/", headers=_bearer(secret), json={}).status_code == 401
    # Past the auth gate: transcription is disabled in test settings, so the
    # next refusal is 503, not 401.
    assert client.post("/transcriptions", headers=_bearer(secret)).status_code == 503
    assert client.post("/transcriptions", headers=_bearer("qc_not-a-real-token")).status_code == 401


def test_service_token_always_works_and_unknown_tokens_do_not(client):
    assert client.get("/entities", headers=SERVICE).status_code == 200
    for bad in ("qc_guess", "test-token-but-longer", "", "Basic dGVzdA=="):
        assert client.get("/entities", headers={"Authorization": f"Bearer {bad}"}).status_code == 401
    assert client.get("/entities").status_code == 401


def test_names_are_unique_among_active_tokens_and_reusable_after_revoke(client, test_settings):
    client.get("/entities", headers=SERVICE)
    db = _db(test_settings)
    tokens.create(db, "mac")
    from api.errors import ConflictError
    with pytest.raises(ConflictError, match="already has that name"):
        tokens.create(db, "mac")
    tokens.revoke(db, "mac")
    second, _ = tokens.create(db, "mac")
    assert client.get("/entities", headers=_bearer(second)).status_code == 200
    assert len(tokens.list_tokens(db)) == 2 and len(tokens.list_tokens(db, include_revoked=False)) == 1


@pytest.mark.parametrize("name, scope", [("", "full"), ("x" * 81, "full"), ("ok", "admin")])
def test_bad_create_arguments_are_refused(client, test_settings, name, scope):
    client.get("/entities", headers=SERVICE)
    with pytest.raises(ValueError):
        tokens.create(_db(test_settings), name, scope)


def test_last_used_is_recorded_but_not_rewritten_on_every_request(client, test_settings):
    client.get("/entities", headers=SERVICE)
    db = _db(test_settings)
    secret, record = tokens.create(db, "mac")
    assert record["last_used_at"] is None
    client.get("/entities", headers=_bearer(secret))
    first = db.execute("SELECT last_used_at FROM api_tokens").fetchone()[0]
    assert first is not None
    db.execute("UPDATE api_tokens SET last_used_at = '2000-01-01 00:00:00'"); db.commit()
    client.get("/entities", headers=_bearer(secret))
    assert db.execute("SELECT last_used_at FROM api_tokens").fetchone()[0] != "2000-01-01 00:00:00"
    stamped = db.execute("SELECT last_used_at FROM api_tokens").fetchone()[0]
    client.get("/entities", headers=_bearer(secret))
    assert db.execute("SELECT last_used_at FROM api_tokens").fetchone()[0] == stamped


def test_revoking_something_unknown_is_refused(client, test_settings):
    client.get("/entities", headers=SERVICE)
    from api.errors import NotFoundError
    with pytest.raises(NotFoundError, match="No active token"):
        tokens.revoke(_db(test_settings), "nope")


def test_cli_create_list_revoke(client, test_settings, monkeypatch, capsys):
    client.get("/entities", headers=SERVICE)
    monkeypatch.setattr("api.config.get_settings", lambda: test_settings)
    assert tokens.main(["create", "cli-mac", "--scope", "transcription"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["token"].startswith("qc_") and created["scope"] == "transcription"
    assert tokens.main(["list"]) == 0
    listed = capsys.readouterr().out
    assert "cli-mac" in listed and created["token"] not in listed
    assert tokens.main(["revoke", "cli-mac"]) == 0
    assert json.loads(capsys.readouterr().out)["revoked_at"]
    assert tokens.main(["revoke", "cli-mac"]) == 1


@pytest.fixture()
def isolated_root():
    """configure_logging attaches to the root logger; put it back afterwards."""
    import logging

    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    root.handlers = []
    try:
        yield root
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers, root.level = saved_handlers, saved_level


def test_no_secret_reaches_the_logs(client, test_settings, isolated_root, caplog):
    """Done-when: no secret in logs. Covers the api.log file and every record."""
    import logging
    from pathlib import Path

    from api.logging_config import configure_logging

    test_settings.log_level = "DEBUG"
    configure_logging(test_settings)
    caplog.set_level(logging.DEBUG)
    client.get("/entities", headers=SERVICE)
    db = _db(test_settings)
    secret, _ = tokens.create(db, "mac", "full")
    assert client.get("/entities", headers=_bearer(secret)).status_code == 200
    assert client.post("/mcp/", headers={**_bearer(secret), "Accept": "application/json, text/event-stream"},
                       json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 503  # past auth
    assert client.get("/entities", headers=_bearer(secret + "x")).status_code == 401
    tokens.revoke(db, "mac")
    assert client.get("/entities", headers=_bearer(secret)).status_code == 401

    for handler in isolated_root.handlers:
        handler.flush()
    log_text = (Path(test_settings.logs_dir) / "api.log").read_text()
    assert "/entities" in log_text  # the requests were logged, so absence below means something
    everything = log_text + caplog.text + "".join(str(r.__dict__) for r in caplog.records)
    for leak in (secret, secret[3:], tokens.token_hash(secret), "test-token"):
        assert leak not in everything


def test_non_ascii_authorization_is_refused_not_a_server_error(client):
    garbage = {"Authorization": b"Bearer caf\xe9"}
    assert client.get("/entities", headers=garbage).status_code == 401
    assert client.post("/mcp/", headers=garbage, json={}).status_code == 401
    assert client.post("/transcriptions", headers=garbage).status_code == 401


def test_a_create_that_loses_the_name_race_is_a_conflict(client, test_settings, monkeypatch):
    from api.errors import ConflictError

    client.get("/entities", headers=SERVICE)
    db = _db(test_settings)
    tokens.create(db, "mac", "full")

    class SkipsTheNameCheck:
        """Simulates the other writer committing between check and insert."""
        def __init__(self, inner):
            self.inner = inner
        def execute(self, sql, *args):
            if sql.startswith("SELECT 1 FROM api_tokens"):
                return self.inner.execute("SELECT 1 WHERE 0")
            return self.inner.execute(sql, *args)
        def __getattr__(self, name):
            return getattr(self.inner, name)

    with pytest.raises(ConflictError):
        tokens.create(SkipsTheNameCheck(db), "mac", "full")
    assert [t["name"] for t in tokens.list_tokens(db)] == ["mac"]


def test_cli_rejects_a_bad_scope_without_echoing_it(capsys):
    with pytest.raises(SystemExit):
        tokens.main(["create", "mac", "--scope", "caller-supplied-value"])
    err = capsys.readouterr().err
    assert "scope must be full or transcription" in err and "caller-supplied-value" not in err

"""An unhandled exception must return this API's error envelope (a todo).

Every other error path returns `{"error": {"code", "message"}}`. A bare
unhandled exception did not: it escaped to Starlette's ServerErrorMiddleware
and came back as plain-text "Internal Server Error". Four separate
instances were found in one session -- a KeyError in the board view, a
ValueError from paginate, an OSError from open(), and the attachment
suffix crash -- all the same shape: a builtin raised by a stdlib call on
caller-influenced input, in a route with no local try/except.

Two things are pinned here, and the second is the subtle one:

1. The *body* must be the envelope. All four instances broke the envelope,
   not the status code, so asserting `status_code == 500` would pass on
   the broken behaviour.
2. The response must carry `X-Request-ID`. That only holds if the handler
   runs INSIDE the request-logging middleware. Registering
   `@app.exception_handler(Exception)` instead puts the handler on
   Starlette's ServerErrorMiddleware, which sits OUTSIDE the app's
   middleware and sends through its own `send` -- the header never gets
   attached, and the one response class a user is most likely to report
   becomes the one whose id they cannot quote. Verified by building the
   wrong version and watching this test fail.
"""

import json
import logging
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.logging_config import configure_logging, install_request_logging
from api.main import install_error_envelope


@pytest.fixture()
def isolated_root():
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    root.handlers = []
    try:
        yield root
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers, root.level = saved_handlers, saved_level


@pytest.fixture()
def failing_app(isolated_root, test_settings):
    """An app wired exactly like api.main, with routes that blow up the
    same four ways the real bugs did."""
    configure_logging(test_settings)
    app = FastAPI()
    install_error_envelope(app)
    install_request_logging(app)

    @app.get("/key-error")
    def key_error() -> dict:
        return {"board": {}["missing-status"]}

    @app.get("/value-error")
    def value_error() -> dict:
        raise ValueError("invalid literal for int() with base 10: 'x'")

    @app.get("/os-error")
    def os_error() -> dict:
        raise OSError("[Errno 63] File name too long: '/tmp/aaaa...'")

    @app.get("/leaky")
    def leaky() -> dict:
        raise RuntimeError("account 123456789 belongs to Jane Q Public")

    return app


def _client(app) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("path", ["/key-error", "/value-error", "/os-error"])
def test_an_unhandled_exception_returns_the_error_envelope(failing_app, path):
    response = _client(failing_app).get(path)

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": (
                "The server hit an unexpected error. The details are in the "
                "server log; quote the X-Request-ID header when reporting it."
            ),
        }
    }


def test_the_error_response_carries_the_request_id_header(failing_app):
    """The header is the whole point: it is how a reported failure finds
    its log line. Fails if the handler is registered outside the
    request-logging middleware -- see this module's docstring."""
    response = _client(failing_app).get("/key-error")

    assert response.headers["X-Request-ID"]


def test_the_header_matches_the_logged_request_id(failing_app, test_settings):
    response = _client(failing_app).get("/key-error")

    lines = [
        json.loads(line)
        for line in (Path(test_settings.logs_dir) / "api.log").read_text().splitlines()
    ]
    errors = [line for line in lines if line["level"] == "ERROR"]
    assert len(errors) == 1
    assert errors[0]["request_id"] == response.headers["X-Request-ID"]


def test_the_traceback_is_logged_but_never_returned(failing_app, test_settings):
    """The log gets everything; the client gets nothing it could not
    already infer. A message can contain anything a route interpolated
    into it, which is why the body is a fixed string rather than str(exc).
    """
    response = _client(failing_app).get("/leaky")

    body = response.text
    assert "123456789" not in body
    assert "Jane Q Public" not in body
    assert "Traceback" not in body
    assert "RuntimeError" not in body

    log_text = (Path(test_settings.logs_dir) / "api.log").read_text()
    assert "RuntimeError" in log_text
    assert "Traceback" in log_text


def test_only_one_error_is_logged_per_failed_request(failing_app, test_settings):
    """Handling the exception must not also let it propagate: a response
    that is sent AND re-raised gets logged a second time by the server,
    which is the duplicate-line bug the observability work already fixed
    once."""
    _client(failing_app).get("/key-error")

    lines = [
        json.loads(line)
        for line in (Path(test_settings.logs_dir) / "api.log").read_text().splitlines()
    ]
    assert len([line for line in lines if line["level"] == "ERROR"]) == 1


def test_handled_errors_are_untouched(client):
    """The existing envelope paths must not change shape."""
    response = client.get("/")

    assert response.status_code == 401
    assert response.json() == {
        "error": {"code": "unauthorized", "message": "Missing or invalid bearer token"}
    }


def test_a_successful_request_is_unaffected(client):
    assert client.get("/health", headers={"Authorization": "Bearer test-token"}).status_code == 200


# --- the real app's wiring ---------------------------------------------


def test_the_real_app_returns_the_envelope_with_a_request_id(test_settings, tmp_path):
    """Everything above builds its own app, so none of it would notice if
    `api/main.py` installed the two middlewares in the wrong order.

    `add_middleware` inserts at the front, so the LAST call ends up
    outermost. Reversing the two lines in main.py puts the envelope
    middleware outside the logging middleware, and the response stops
    passing through the wrapper that stamps the header. This test hits the
    real `api.main.app`, so it is the one that actually pins that order —
    verified by reversing the lines and watching it fail.

    An unusable database is used as the trigger because it reaches a real
    route through real dependencies, rather than needing a fake route
    bolted onto the app under test.
    """
    from api.config import get_settings
    from api.main import app

    broken = tmp_path / "not-a-database.db"
    broken.write_bytes(b"this is not a sqlite file")

    app.dependency_overrides[get_settings] = lambda: test_settings.model_copy(
        update={"db_path": str(broken)}
    )
    try:
        response = TestClient(app, raise_server_exceptions=False).get(
            "/entities",
            headers={"Authorization": f"Bearer {test_settings.api_token}"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "database_unusable"
    assert response.headers["X-Request-ID"]

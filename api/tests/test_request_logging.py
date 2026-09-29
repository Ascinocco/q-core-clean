"""Tests for the API's per-request logging middleware.

These read the log file back off disk rather than using caplog, because
reading the file is the thing this feature actually promises: a Claude
session debugging a report from the owner opens `data/logs/api.log` and parses
it. A test that only inspects in-memory records would pass even if nothing
ever reached the file.
"""

import json
import logging
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.logging_config import configure_logging, install_request_logging


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
def logged_app(isolated_root, test_settings):
    """A minimal app with the real middleware and the real file handler."""
    configure_logging(test_settings)
    app = FastAPI()
    install_request_logging(app)

    @app.get("/ok")
    def ok() -> dict:
        return {"ok": True}

    @app.get("/boom")
    def boom() -> dict:
        raise RuntimeError("the vehicle table is on fire")

    return app


def _lines(test_settings) -> list[dict]:
    log_file = Path(test_settings.logs_dir) / "api.log"
    return [json.loads(line) for line in log_file.read_text().splitlines()]


def _request_lines(test_settings) -> list[dict]:
    return [line for line in _lines(test_settings) if line["logger"] == "api.request"]


def test_a_request_is_logged_with_method_path_status_and_duration(
    logged_app, test_settings
):
    TestClient(logged_app).get("/ok")

    (line,) = _request_lines(test_settings)
    assert line["method"] == "GET"
    assert line["path"] == "/ok"
    assert line["status"] == 200
    assert line["level"] == "INFO"
    assert isinstance(line["duration_ms"], (int, float))
    assert line["duration_ms"] >= 0


def test_the_response_carries_an_x_request_id_header(logged_app):
    response = TestClient(logged_app).get("/ok")

    assert response.headers["X-Request-ID"]


def test_the_header_matches_the_logged_request_id(logged_app, test_settings):
    """The whole point of the id: The owner quotes the header from a failed call
    and it finds the exact log line."""
    response = TestClient(logged_app).get("/ok")

    (line,) = _request_lines(test_settings)
    assert line["request_id"] == response.headers["X-Request-ID"]


def test_each_request_gets_a_distinct_id(logged_app):
    client = TestClient(logged_app)

    first = client.get("/ok").headers["X-Request-ID"]
    second = client.get("/ok").headers["X-Request-ID"]

    assert first != second


def test_an_inbound_x_request_id_is_reused_rather_than_replaced(logged_app):
    """So a request id can be traced from the MCP server through the API."""
    response = TestClient(logged_app).get("/ok", headers={"X-Request-ID": "from-mcp"})

    assert response.headers["X-Request-ID"] == "from-mcp"


def test_a_log_call_inside_a_route_inherits_the_request_id(
    isolated_root, test_settings
):
    """Any line logged while handling a request correlates to it, without
    every call site having to pass the id explicitly."""
    configure_logging(test_settings)
    app = FastAPI()
    install_request_logging(app)

    @app.get("/work")
    def work() -> dict:
        logging.getLogger("api.work").info("did some work")
        return {"ok": True}

    response = TestClient(app).get("/work")

    work_lines = [
        line for line in _lines(test_settings) if line["logger"] == "api.work"
    ]
    assert len(work_lines) == 1
    assert work_lines[0]["request_id"] == response.headers["X-Request-ID"]


def test_an_unhandled_exception_is_logged_with_its_traceback(
    logged_app, test_settings
):
    TestClient(logged_app, raise_server_exceptions=False).get("/boom")

    errors = [line for line in _lines(test_settings) if line["level"] == "ERROR"]
    assert len(errors) == 1
    assert "RuntimeError: the vehicle table is on fire" in errors[0]["traceback"]
    assert errors[0]["path"] == "/boom"


def test_an_unhandled_exception_still_returns_a_body_that_leaks_nothing(logged_app):
    """The log gets the detail; the client must not.

    Asserts the client-visible behaviour is unchanged from before this
    middleware existed — a bare 500 with no exception text in the body.
    """
    response = TestClient(logged_app, raise_server_exceptions=False).get("/boom")

    assert response.status_code == 500
    assert "vehicle table is on fire" not in response.text
    assert "Traceback" not in response.text


def test_a_failed_request_is_still_logged_with_its_duration(
    logged_app, test_settings
):
    """A request that blows up is exactly the one you want timing for."""
    TestClient(logged_app, raise_server_exceptions=False).get("/boom")

    errors = [line for line in _lines(test_settings) if line["level"] == "ERROR"]
    assert errors[0]["status"] == 500
    assert isinstance(errors[0]["duration_ms"], (int, float))


def test_request_bodies_are_never_logged(isolated_root, test_settings):
    """CLAUDE.md forbids account numbers reaching any log or LLM context.

    There is no general body-redaction helper in this repo (api/models.py's
    _redact_sensitive_inputs is keyed on Pydantic error locations, not on
    arbitrary payloads), so the middleware logs no bodies at all. This test
    pins that decision: it fails the moment someone adds body logging
    without a redactor.
    """
    configure_logging(test_settings)
    app = FastAPI()
    install_request_logging(app)

    @app.post("/accounts")
    def create(payload: dict) -> dict:
        return {"ok": True}

    TestClient(app).post("/accounts", json={"routing_number": "123456789"})

    assert "123456789" not in (Path(test_settings.logs_dir) / "api.log").read_text()


def test_the_query_string_is_not_logged(isolated_root, test_settings):
    """Same reasoning as bodies: a query string can carry a search term for
    a person's name or an account. Only the path is recorded.

    Scoped to this middleware's own lines on purpose. configure_logging
    attaches to the *root* logger, so any third-party logger in the process
    also lands in this file — under TestClient that includes httpx, which
    logs the full request URL, query string and all. httpx is a test-harness
    artifact (the API server does not run an httpx client), but the same
    shape is a real production concern for uvicorn's access logger, which
    renders `/search?q=...` by construction. That is why uvicorn's access
    logger is disabled rather than routed in — see
    test_uvicorn_logging.py and runbooks/observability.md.
    """
    configure_logging(test_settings)
    app = FastAPI()
    install_request_logging(app)

    @app.get("/search")
    def search(q: str = "") -> dict:
        return {"ok": True}

    TestClient(app).get("/search?q=123456789")

    (line,) = _request_lines(test_settings)
    assert line["path"] == "/search"
    assert "123456789" not in json.dumps(line)


# --- wired into the real app -------------------------------------------


def test_the_real_app_stamps_x_request_id_on_a_health_check(client):
    assert client.get("/health").headers["X-Request-ID"]


def test_the_real_app_stamps_x_request_id_on_an_error_response(client):
    """Errors are the responses whose id the owner actually needs to quote."""
    response = client.get("/")

    assert response.status_code == 401
    assert response.headers["X-Request-ID"]


def test_the_error_envelope_body_is_unchanged_by_the_middleware(client):
    """The request id rides in a header precisely so this body shape — which
    a lot of existing tests assert — does not move."""
    body = client.get("/").json()

    assert body == {
        "error": {"code": "unauthorized", "message": "Missing or invalid bearer token"}
    }

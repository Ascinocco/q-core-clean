"""Default-deny on both listeners (split, part 2). Invented identities only.

Three exact sets are pinned by walking every registered route, so a new route
cannot quietly join any of them:
- reachable with no credential at all: nothing through Serve; only the
  state-authenticated OAuth callback on the service port;
- opened by an allowlisted Tailscale identity through Serve: BROWSER_SURFACE;
- reachable with a phone's `transcription` token: /transcriptions and /health.
"""
import os
import re
from contextlib import closing
import socket
import stat

import pytest
from fastapi.testclient import TestClient

from api import tokens
from api.config import get_settings
from api.serve_gate import BROWSER_SURFACE, in_browser_surface

SOCKET = "/run/q-core-test/serve.sock"
ALLOWED = "owner@example.invalid"
WHO = {"Tailscale-User-Login": ALLOWED}
UUID = "00000000-0000-4000-8000-000000000000"
CALLBACK = ("GET", "/integrations/google/callback")


class ArrivesOnTheServeSocket:
    """uvicorn reports a Unix-socket listener as scope["server"] = (path, None)."""

    def __init__(self, app, path=SOCKET):
        self.app, self.path = app, path

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope = {**scope, "server": (self.path, None)}
        await self.app(scope, receive, send)


@pytest.fixture
def serve_settings(test_settings):
    return test_settings.model_copy(update={"serve_socket": SOCKET, "ui_allowed_logins": [ALLOWED]})


@pytest.fixture
def app_with(serve_settings, monkeypatch):
    from api.main import app
    app.dependency_overrides[get_settings] = lambda: serve_settings
    monkeypatch.setattr("api.main.get_settings", lambda: serve_settings)
    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def via_serve(app_with):
    with TestClient(ArrivesOnTheServeSocket(app_with)) as c:
        yield c


@pytest.fixture
def local(app_with):
    with TestClient(app_with, base_url="http://127.0.0.1:8420") as c:
        yield c


def _walk(routes):
    for route in routes:
        if hasattr(route, "original_router"):
            yield from _walk(route.original_router.routes)
        else:
            yield route


def _operations(app):
    ops = []
    for route in _walk(app.routes):
        mount = type(route).__name__ == "Mount"
        # A mount has no methods of its own; MCP answers POST, so probe both.
        methods = ["GET", "POST"] if mount else sorted((getattr(route, "methods", None) or {"GET"}) - {"HEAD", "OPTIONS"})
        path = re.sub(r"\{[^}]+\}", UUID, route.path) + ("/" if mount else "")
        ops += [(method, path) for method in methods]
    return ops


def _reachable(client, app, headers):
    """Operations that did not refuse the credential (401 or 403)."""
    return {(m, p) for m, p in _operations(app) if client.request(m, p, headers=headers).status_code not in (401, 403)}


def test_the_walk_sees_pages_docs_and_mounts(app_with):
    """Guards the guard: a walk that missed these would pass over them."""
    paths = {path for _, path in _operations(app_with)}
    assert {"/ui/spending", "/ui/briefs", "/docs", "/openapi.json", "/health", "/mcp/", "/entities"} <= paths
    assert len(paths) > 80


def test_no_route_answers_without_credentials_through_serve(app_with, via_serve):
    assert _reachable(via_serve, app_with, {}) == set()


def test_on_the_service_port_only_the_oauth_callback_needs_no_credential(app_with, local):
    """The public read-only exemptions are gone; the callback is authenticated by its single-use state."""
    assert _reachable(local, app_with, {}) == {CALLBACK}
    assert local.get("/integrations/google/callback", params={"code": "c", "state": "forged"}).status_code == 400


def test_identity_through_serve_opens_exactly_the_browser_surface(app_with, via_serve):
    opened = _reachable(via_serve, app_with, WHO)
    surface = {op for op in _operations(app_with) if in_browser_surface(*op)}
    # Identity alone opens every page and read; the token page's writes are in
    # the surface but still need the pinned Serve Host/Origin (test_token_ui).
    assert opened == {op for op in surface if op[0] == "GET"}
    assert {op for op in surface if op[0] != "GET"} == {("POST", "/ui/tokens/create"), ("POST", "/ui/tokens/revoke")}
    assert len(surface) == len(BROWSER_SURFACE) + 1  # the two highcharts assets share one pattern
    assert ("POST", "/mcp/") in _operations(app_with)  # the walk probes the MCP mount's real method
    for closed in [("GET", "/docs"), ("GET", "/openapi.json"), ("GET", "/entities"), ("POST", "/mcp/"), ("GET", "/health")]:
        assert closed not in opened


def test_identity_headers_mean_nothing_on_the_service_port(app_with, local):
    assert _reachable(local, app_with, WHO) == {CALLBACK}


def test_a_transcription_token_reaches_only_transcription_and_health(app_with, local, via_serve, serve_settings):
    with closing(tokens.open_connection(serve_settings)) as db:
        phone, _ = tokens.create(db, "phone", "transcription")
    bearer = {"Authorization": f"Bearer {phone}"}
    # The callback has no scope of its own; past the gate it needs a valid OAuth state.
    expected = {("POST", "/transcriptions"), ("GET", "/health"), CALLBACK}
    assert _reachable(via_serve, app_with, bearer) == expected
    assert _reachable(local, app_with, bearer) == expected


def test_full_tokens_work_on_both_listeners_until_revoked(serve_settings, local, via_serve):
    with closing(tokens.open_connection(serve_settings)) as db:
        full, _ = tokens.create(db, "mac", "full")
        bearer = {"Authorization": f"Bearer {full}"}
        for c in (local, via_serve):
            assert c.get("/entities", headers=bearer).status_code == 200
            assert c.get("/ui/spending", headers=bearer).status_code == 200
        tokens.revoke(db, "mac")
    for c in (local, via_serve):
        assert c.get("/entities", headers=bearer).status_code == 401


def test_forged_identity_that_is_not_allowlisted_is_refused(via_serve):
    for login in ("stranger@example.invalid", "", "OWNER@example.invalid.evil"):
        assert via_serve.get("/ui/spending", headers={"Tailscale-User-Login": login}).status_code == 401
    assert via_serve.get("/ui/spending", headers={"Tailscale-User-Login": ALLOWED.upper()}).status_code == 200


def test_empty_allowlist_accepts_no_identity(serve_settings, monkeypatch):
    from api.main import app
    closed = serve_settings.model_copy(update={"ui_allowed_logins": []})
    app.dependency_overrides[get_settings] = lambda: closed
    try:
        with TestClient(ArrivesOnTheServeSocket(app)) as c:
            assert c.get("/ui/spending", headers=WHO).status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_a_request_without_server_info_is_treated_as_serve(serve_settings):
    from api.serve_gate import request_came_through_serve
    assert request_came_through_serve({"type": "http"}, serve_settings)
    assert not request_came_through_serve({"type": "http", "server": ("127.0.0.1", 8420)}, serve_settings)
    assert not request_came_through_serve({"type": "http", "server": ("/elsewhere.sock", None)}, serve_settings)
    unset = serve_settings.model_copy(update={"serve_socket": None})
    assert not request_came_through_serve({"type": "http", "server": (SOCKET, None)}, unset)


@pytest.fixture
def short_dir():
    """AF_UNIX paths are limited to ~104 bytes; pytest's tmp_path on macOS is longer."""
    import shutil
    import tempfile
    from pathlib import Path
    directory = Path(tempfile.mkdtemp(prefix="qc-", dir="/tmp"))
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


def test_listening_sockets_bind_loopback_and_a_private_unix_socket(serve_settings, short_dir):
    from api.run import listening_sockets
    probe = socket.socket(); probe.bind(("127.0.0.1", 0)); free = probe.getsockname()[1]; probe.close()
    path = short_dir / "run" / "serve.sock"
    socks = listening_sockets(serve_settings.model_copy(update={"port": free, "serve_socket": str(path)}))
    try:
        assert socks[0].getsockname() == ("127.0.0.1", free)
        assert socks[1].family == socket.AF_UNIX and path.is_socket()
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        for s in socks:
            s.close()


def test_an_open_socket_directory_stops_startup_and_leaks_nothing(serve_settings, short_dir):
    from api.run import listening_sockets
    directory = short_dir / "shared"
    directory.mkdir(mode=0o755)
    os.chmod(directory, 0o755)
    probe = socket.socket(); probe.bind(("127.0.0.1", 0)); free = probe.getsockname()[1]; probe.close()
    with pytest.raises(SystemExit, match="mode 0700"):
        listening_sockets(serve_settings.model_copy(update={"port": free, "serve_socket": str(directory / "serve.sock")}))
    # The TCP socket opened first was closed: the port can be bound again.
    again = socket.socket(); again.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    again.bind(("127.0.0.1", free)); again.close()


@pytest.mark.parametrize("value", ["relative.sock", "/run/q-core/serve", "/run/q-core/serve.sock\0x"])
def test_serve_socket_must_be_an_absolute_sock_path(test_settings, value):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        type(test_settings).model_validate({**test_settings.model_dump(), "serve_socket": value})


def test_the_configured_phone_token_reaches_only_dictation(serve_settings, monkeypatch):
    """settings.transcription_token (the mobile client's SSH-forwarded phone credential)
    is accepted by the gate for POST /transcriptions and nothing else, on
    either listener."""
    from api.main import app
    phone = serve_settings.model_copy(update={"transcription_token": "phone-configured-token"})
    app.dependency_overrides[get_settings] = lambda: phone
    monkeypatch.setattr("api.main.get_settings", lambda: phone)
    bearer = {"Authorization": "Bearer phone-configured-token"}
    try:
        with TestClient(app, base_url="http://127.0.0.1:8420") as c:
            assert _reachable(c, app, bearer) == {("POST", "/transcriptions"), CALLBACK}
        with TestClient(ArrivesOnTheServeSocket(app)) as c:
            assert _reachable(c, app, bearer) == {("POST", "/transcriptions")}
    finally:
        app.dependency_overrides.clear()


class _Capture:
    """The downstream app: records the scope ServeGate passes on."""

    def __init__(self):
        self.scope = None

    async def __call__(self, scope, receive, send):
        self.scope = scope
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})


def _gate_host(settings, server, extra_headers):
    import anyio
    from api.serve_gate import ServeGate

    capture = _Capture()
    gate = ServeGate(capture, settings_provider=lambda: settings)
    scope = {"type": "http", "method": "GET", "path": "/integrations/google/callback",
             "server": server, "headers": [(b"host", b"localhost"), *extra_headers]}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        pass

    anyio.run(gate, scope, receive, send)
    return dict(capture.scope["headers"])[b"host"]


def test_the_serve_socket_restores_host_from_x_forwarded_host(serve_settings):
    """Tailscale Serve sends Host: localhost to a unix socket and the name the
    client used in X-Forwarded-Host (ticket T-46)."""
    host = _gate_host(serve_settings, (SOCKET, None), [(b"x-forwarded-host", b"q-core.tail1234.ts.net"),
                                                        (b"tailscale-user-login", ALLOWED.encode())])
    assert host == b"q-core.tail1234.ts.net"


def test_x_forwarded_host_is_ignored_on_the_service_port(serve_settings):
    """On TCP anyone local can send any header: Host must stay as sent."""
    host = _gate_host(serve_settings, ("127.0.0.1", 8420), [(b"x-forwarded-host", b"q-core.tail1234.ts.net")])
    assert host == b"localhost"


def test_the_serve_socket_without_x_forwarded_host_keeps_host(serve_settings):
    assert _gate_host(serve_settings, (SOCKET, None), [(b"tailscale-user-login", ALLOWED.encode())]) == b"localhost"

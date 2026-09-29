"""The MCP endpoint is served by this API process, at /mcp.

These speak the protocol rather than calling tools in-process, because
every failure this mount can have is invisible to an in-process test: the
session manager not starting, the bearer check not applying to a mounted
ASGI app, the transport refusing a non-loopback Host. Each of those was
observed while building it.
"""

import json

import pytest
from fastapi.testclient import TestClient

from api.config import get_settings
from api.main import app

PROTOCOL_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "tests", "version": "1"},
    },
}


@pytest.fixture()
def mcp_client(test_settings, monkeypatch):
    """A client that actually runs the app's lifespan.

    Unlike the shared `client` fixture, this enters TestClient as a context
    manager — the mounted app's session manager starts in the lifespan, so
    without it every request fails with "Task group is not initialized".

    That also means `get_settings()` is called directly by the lifespan
    rather than through `Depends`, where `dependency_overrides` cannot
    reach it, so the module-level name is patched too. The auth wrapper and
    the lazy client resolve it the same way.

    base_url must be loopback: the transport's DNS-rebinding guard answers
    421 to any other Host. That is a feature — a second lock on the
    local-only rule — but it means the default "testserver" fails.
    """
    monkeypatch.setattr("api.main.get_settings", lambda: test_settings)
    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        with TestClient(app, base_url="http://127.0.0.1:8420") as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def _auth(test_settings):
    return {"Authorization": f"Bearer {test_settings.api_token}"}


def _sse_payload(response):
    for line in response.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return response.json()


def test_mcp_requires_the_bearer_token(mcp_client):
    """A mounted ASGI app is invisible to route-level dependencies.

    Before the wrapper existed this returned 200 with no header at all —
    the whole tool surface, unauthenticated, on a route every other
    endpoint's auth covers.
    """
    response = mcp_client.post("/mcp/", json=INITIALIZE, headers=PROTOCOL_HEADERS)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_mcp_rejects_a_wrong_token(mcp_client, test_settings):
    response = mcp_client.post(
        "/mcp/",
        json=INITIALIZE,
        headers={**PROTOCOL_HEADERS, "Authorization": "Bearer not-the-token"},
    )

    assert response.status_code == 401


def test_initialize_opens_a_session(mcp_client, test_settings):
    response = mcp_client.post(
        "/mcp/",
        json=INITIALIZE,
        headers={**PROTOCOL_HEADERS, **_auth(test_settings)},
    )

    assert response.status_code == 200
    assert response.headers.get("mcp-session-id")


def test_tools_list_returns_the_whole_surface(mcp_client, test_settings):
    """Proves the session manager is running.

    Without the mounted app's lifespan being entered by the parent, this
    fails with "Task group is not initialized" — a runtime error no
    in-process tool test can reach.
    """
    headers = {**PROTOCOL_HEADERS, **_auth(test_settings)}
    opened = mcp_client.post("/mcp/", json=INITIALIZE, headers=headers)
    session = {"mcp-session-id": opened.headers["mcp-session-id"]}
    mcp_client.post(
        "/mcp/",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={**headers, **session},
    )

    response = mcp_client.post(
        "/mcp/",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        headers={**headers, **session},
    )

    assert response.status_code == 200
    names = {tool["name"] for tool in _sse_payload(response)["result"]["tools"]}
    assert "create_entity" in names
    assert "import_statement" not in names
    assert {"preview_source_import", "commit_source_import"} <= names
    assert "create_ticket" in names
    # The same surface test_tool_surface.py pins in-process, now proven to
    # survive the transport.
    from q_core_mcp.tests.test_tool_surface import EXPECTED_TOOLS

    assert names == set(EXPECTED_TOOLS)


def test_mounting_mcp_left_the_rest_of_the_api_alone(mcp_client, test_settings):
    """Mounting at "/" instead of "/mcp" shadowed everything — /health
    answered 401 from the MCP auth wrapper and an unknown path became a
    500. This is the regression test for that. (Since split part 2 every
    route needs a token, so the probes carry one: 404 and 200 still mean the
    MCP mount did not answer for them.)"""
    assert mcp_client.get("/health", headers=_auth(test_settings)).status_code == 200
    assert mcp_client.get("/nonexistent", headers=_auth(test_settings)).status_code == 404
    assert (
        mcp_client.get("/entities", headers=_auth(test_settings)).status_code == 200
    )


def test_the_bare_mcp_path_is_served_without_a_redirect(mcp_client, test_settings):
    """`/mcp` and `/mcp/` must behave identically.

    Starlette's Mount matches "/mcp/..." but not the bare prefix, so
    without the path-rewrite middleware "/mcp" falls through to the
    router's redirect_slashes and answers 307 — and that happens *before*
    the auth wrapper, so an unauthenticated request would be redirected
    rather than refused. The configured URL is the bare one, so this is the
    path that actually gets used.
    """
    headers = {**PROTOCOL_HEADERS, **_auth(test_settings)}

    unauthenticated = mcp_client.post("/mcp", json=INITIALIZE, headers=PROTOCOL_HEADERS)
    opened = mcp_client.post("/mcp", json=INITIALIZE, headers=headers)

    assert unauthenticated.status_code == 401, "not a 307"
    assert opened.status_code == 200
    assert opened.headers.get("mcp-session-id")


def test_the_rewrite_does_not_capture_neighbouring_paths(mcp_client, test_settings):
    """Only the exact path is rewritten — "/mcpfoo" is not an MCP request."""
    assert mcp_client.get("/mcpfoo", headers=_auth(test_settings)).status_code == 404


SERVE_HOST = "q-core.tail1234.ts.net"


@pytest.fixture()
def serve_mcp_client(test_settings, monkeypatch):
    """The lifespan client again, with the Tailscale Serve name configured."""
    served = test_settings.model_copy(update={"serve_hostname": SERVE_HOST})
    monkeypatch.setattr("api.main.get_settings", lambda: served)
    app.dependency_overrides[get_settings] = lambda: served
    try:
        with TestClient(app, base_url="http://127.0.0.1:8420") as client:
            yield client, served
    finally:
        app.dependency_overrides.clear()


def test_mcp_answers_through_the_configured_serve_hostname(serve_mcp_client):
    """Serve keeps the client's Host, so the transport must accept that name.

    Before serve_hostname existed this was 421 with a valid token: the MCP
    endpoint was unreachable through Tailscale Serve.
    """
    client, settings = serve_mcp_client
    headers = {**PROTOCOL_HEADERS, **_auth(settings), "Host": SERVE_HOST,
               "Origin": f"https://{SERVE_HOST}"}
    response = client.post("/mcp/", json=INITIALIZE, headers=headers)
    assert response.status_code == 200
    assert _sse_payload(response)["result"]["serverInfo"]


@pytest.mark.parametrize("host", ["q-core.tail9999.ts.net", "evil.example", f"{SERVE_HOST}.evil.example"])
def test_other_hosts_are_still_refused_by_the_rebinding_guard(serve_mcp_client, host):
    client, settings = serve_mcp_client
    headers = {**PROTOCOL_HEADERS, **_auth(settings), "Host": host}
    assert client.post("/mcp/", json=INITIALIZE, headers=headers).status_code == 421


def test_without_a_serve_hostname_only_loopback_hosts_reach_mcp(mcp_client, test_settings):
    headers = {**PROTOCOL_HEADERS, **_auth(test_settings), "Host": SERVE_HOST}
    assert mcp_client.post("/mcp/", json=INITIALIZE, headers=headers).status_code == 421


@pytest.mark.parametrize("value", ["example.com", "Q-CORE.tail1.ts.net", "ts.net", "-a.ts.net", "a.ts.net:443"])
def test_serve_hostname_must_be_a_tailnet_dns_name(test_settings, value):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        type(test_settings).model_validate({**test_settings.model_dump(), "serve_hostname": value})


@pytest.mark.parametrize("origin", ["https://evil.example", f"http://{SERVE_HOST}", "https://q-core.tail9999.ts.net"])
def test_a_forged_origin_is_refused_even_with_the_serve_host(serve_mcp_client, origin):
    """Review R2-F1: the allowed Host alone is not enough; the Origin must match too."""
    client, settings = serve_mcp_client
    headers = {**PROTOCOL_HEADERS, **_auth(settings), "Host": SERVE_HOST, "Origin": origin}
    assert client.post("/mcp/", json=INITIALIZE, headers=headers).status_code == 403


def test_mcp_answers_through_serve_as_serve_really_sends_it(serve_mcp_client):
    """On the unix socket Serve sends Host: localhost plus X-Forwarded-Host
    (ticket T-46); ServeGate restores the name the client used."""
    from api.main import app
    from api.tests.test_serve_gate import ArrivesOnTheServeSocket

    _, settings = serve_mcp_client
    socket_settings = settings.model_copy(update={"serve_socket": "/run/q-core-test/serve.sock"})
    app.dependency_overrides[get_settings] = lambda: socket_settings
    import api.main
    original = api.main.get_settings
    api.main.get_settings = lambda: socket_settings
    try:
        with TestClient(ArrivesOnTheServeSocket(app)) as client:
            headers = {**PROTOCOL_HEADERS, **_auth(socket_settings), "Host": "localhost",
                       "X-Forwarded-Host": SERVE_HOST, "Origin": f"https://{SERVE_HOST}"}
            response = client.post("/mcp/", json=INITIALIZE, headers=headers)
            assert response.status_code == 200, response.text
            evil = {**headers, "X-Forwarded-Host": "evil.example", "Origin": "https://evil.example"}
            assert client.post("/mcp/", json=INITIALIZE, headers=evil).status_code == 421
    finally:
        api.main.get_settings = original
        app.dependency_overrides[get_settings] = lambda: settings


def test_x_forwarded_host_does_not_bypass_the_rebinding_guard_on_the_service_port(serve_mcp_client):
    """On TCP a forged X-Forwarded-Host must not stand in for Host (q-core #14
    review): the guard still sees evil.example and answers 421."""
    client, settings = serve_mcp_client
    headers = {**PROTOCOL_HEADERS, **_auth(settings), "Host": "evil.example",
               "X-Forwarded-Host": SERVE_HOST, "Origin": f"https://{SERVE_HOST}"}
    assert client.post("/mcp/", json=INITIALIZE, headers=headers).status_code == 421

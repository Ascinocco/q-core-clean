"""The /ui/tokens page and its JSON endpoints (split, part 2).

Token management mints credentials, so the endpoints answer only a person
through Tailscale Serve: the Serve port, an allowlisted identity, and for
writes the configured Serve Host and Origin. Invented values only.
"""
import base64
import hashlib
import re

import pytest
from fastapi.testclient import TestClient

from api import tokens
from api.config import get_settings
from api.tests.test_serve_gate import ArrivesOnTheServeSocket

SERVE_SOCKET = "/run/q-core-test/serve.sock"
SERVE_HOST = "q-core.tail1234.ts.net"
OWNER = {"Tailscale-User-Login": "owner@example.invalid"}
WRITE = {"X-Q-Core-Request": "tokens", "Content-Type": "application/json",
         "Host": SERVE_HOST, "Origin": f"https://{SERVE_HOST}"}


@pytest.fixture()
def serve_settings(test_settings):
    return test_settings.model_copy(update={
        "serve_socket": SERVE_SOCKET, "serve_hostname": SERVE_HOST, "ui_allowed_logins": ["owner@example.invalid"]})


@pytest.fixture()
def serve(serve_settings, monkeypatch):
    """A client arriving on the Serve port, as Tailscale Serve would proxy it."""
    from api.main import app
    app.dependency_overrides[get_settings] = lambda: serve_settings
    monkeypatch.setattr("api.main.get_settings", lambda: serve_settings)
    try:
        with TestClient(ArrivesOnTheServeSocket(app, SERVE_SOCKET)) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


def _create(c, name="mac", scope="full", headers=None):
    return c.post("/ui/tokens/create", headers={**WRITE, **OWNER, **(headers or {})}, json={"name": name, "scope": scope})


def test_page_renders_and_its_csp_hashes_every_inline_script_it_serves(serve):
    page = serve.get("/ui/tokens", headers=OWNER)
    assert page.status_code == 200
    assert "Access tokens" in page.text and 'aria-current="page"' in page.text
    csp = page.headers["content-security-policy"]
    script_src = csp.split("script-src")[1].split(";")[0]
    assert "'unsafe-inline'" not in script_src
    served = re.findall(r"<script>(.*?)</script>", page.text, re.S)
    hashes = {base64.b64encode(hashlib.sha256(s.encode()).digest()).decode() for s in served}
    assert served and {f"'sha256-{h}'" for h in hashes} == set(script_src.split())
    assert "connect-src 'self'" in csp and "frame-ancestors 'none'" in csp and "form-action 'none'" in csp


def test_create_list_revoke_as_the_owner_through_serve(serve):
    created = _create(serve)
    assert created.status_code == 200 and created.headers["cache-control"] == "no-store"
    secret = created.json()["token"]
    assert secret.startswith("qc_")
    listed = serve.get("/ui/tokens/list", headers=OWNER).json()["items"]
    assert [t["name"] for t in listed] == ["mac"] and secret not in str(listed)
    bearer = {"Authorization": f"Bearer {secret}"}
    assert serve.get("/entities", headers=bearer).status_code == 200
    revoked = serve.post("/ui/tokens/revoke", headers={**WRITE, **OWNER}, json={"id": listed[0]["id"]})
    assert revoked.status_code == 200 and revoked.json()["record"]["revoked_at"]
    assert serve.get("/entities", headers=bearer).status_code == 401


def test_duplicate_bad_scope_unknown_and_by_name_revoke(serve):
    assert _create(serve).status_code == 200
    assert _create(serve).status_code == 409
    assert _create(serve, "x", "admin").status_code == 422
    assert serve.post("/ui/tokens/revoke", headers={**WRITE, **OWNER}, json={"id": "nope"}).status_code == 404
    # The page revokes by id only; a name is not an id.
    assert serve.post("/ui/tokens/revoke", headers={**WRITE, **OWNER}, json={"id": "mac"}).status_code == 404


def test_the_service_port_never_manages_tokens(client, test_settings):
    """R1-F2: the service port never mints a token, even with the service token."""
    service = {"Authorization": f"Bearer {test_settings.api_token}"}
    for headers in ({}, OWNER):  # no credential at all: the gate refuses first
        assert client.get("/ui/tokens/list", headers=headers).status_code == 401
        assert client.post("/ui/tokens/create", headers={**WRITE, **headers}, json={"name": "x"}).status_code == 401
    assert client.get("/ui/tokens/list", headers=service).status_code == 403
    response = client.post("/ui/tokens/create", headers={**WRITE, **service}, json={"name": "x"})
    assert response.status_code == 403 and "python -m api.tokens" in response.text


@pytest.mark.parametrize("scope", ["transcription", "full"])
def test_api_tokens_of_any_scope_cannot_manage_tokens(serve, serve_settings, scope):
    """R1-F1: a phone's transcription token must not mint a full token."""
    connection = tokens.open_connection(serve_settings)
    secret, _ = tokens.create(connection, "device", scope)
    bearer = {"Authorization": f"Bearer {secret}"}
    assert serve.get("/ui/tokens/list", headers=bearer).status_code == 403
    assert serve.post("/ui/tokens/create", headers={**WRITE, **bearer}, json={"name": "x"}).status_code == 403
    assert serve.post("/ui/tokens/revoke", headers={**WRITE, **bearer}, json={"id": "x"}).status_code == 403
    assert [t["name"] for t in tokens.list_tokens(connection)] == ["device"]


def test_through_serve_without_an_allowed_identity_is_refused(serve):
    assert serve.get("/ui/tokens").status_code == 401  # the gate, before routing
    assert serve.get("/ui/tokens/list").status_code == 401
    stranger = {"Tailscale-User-Login": "stranger@example.invalid"}
    assert serve.get("/ui/tokens/list", headers=stranger).status_code == 401
    assert serve.get("/ui/tokens", headers=OWNER).status_code == 200


@pytest.mark.parametrize("override", [
    {"X-Q-Core-Request": ""},                                              # no custom header
    {"X-Q-Core-Request": "other"},                                         # wrong value
    {"Content-Type": "text/plain"},                                        # form-like body
    {"Origin": "https://evil.example"},                                    # cross-origin
    {"Origin": ""},                                                        # missing Origin (R1-F3)
    {"Origin": f"http://{SERVE_HOST}"},                                    # not HTTPS
    {"Host": "rebind.evil.test:8421", "Origin": "http://rebind.evil.test:8421"},  # DNS rebinding (R1-F3)
    {"Host": "q-core.tail9999.ts.net", "Origin": "https://q-core.tail9999.ts.net"},  # another tailnet
    {"Host": "rebind.evil.test:8421"},                                     # right Origin, wrong Host
])
def test_writes_refuse_every_shape_but_the_pinned_serve_origin(serve, override):
    import json
    headers = {**WRITE, **OWNER, **override}
    headers = {k: v for k, v in headers.items() if v != ""}
    response = serve.post("/ui/tokens/create", headers=headers, content=json.dumps({"name": "csrf"}))
    assert response.status_code == 403
    assert serve.get("/ui/tokens/list", headers=OWNER).json()["items"] == []


def test_writes_are_refused_when_no_serve_hostname_is_configured(serve_settings, monkeypatch):
    from api.main import app
    unnamed = serve_settings.model_copy(update={"serve_hostname": None})
    app.dependency_overrides[get_settings] = lambda: unnamed
    monkeypatch.setattr("api.main.get_settings", lambda: unnamed)
    try:
        with TestClient(ArrivesOnTheServeSocket(app, SERVE_SOCKET)) as c:
            response = _create(c)
            assert response.status_code == 403 and "serve_hostname" in response.text
    finally:
        app.dependency_overrides.clear()


def test_navigation_lists_the_page(client, test_settings):
    page = client.get("/ui/spending", headers={"Authorization": f"Bearer {test_settings.api_token}"})
    assert 'href="/ui/tokens"' in page.text


# --- what Tailscale Serve actually sends to a unix socket (ticket T-46) ---
# Serve rewrites Host to "localhost" for unix: targets and carries the name the
# browser used in X-Forwarded-Host (tailscale ipn/ipnlocal/serve.go). The tests
# above send Host directly, which is what hid this.

SERVE_SENDS = {"Host": "localhost", "X-Forwarded-Host": SERVE_HOST}


def test_a_create_as_serve_really_sends_it_succeeds(serve):
    response = _create(serve, "server-mcp", headers=SERVE_SENDS)
    assert response.status_code == 200, response.text
    assert response.json()["token"].startswith(tokens.TOKEN_PREFIX)


def test_serve_forwarding_another_host_is_still_refused(serve):
    """A DNS-rebinding page: the browser addressed another name, so Serve
    forwards that name (and the page's own origin)."""
    evil = {"Host": "localhost", "X-Forwarded-Host": "evil.example", "Origin": "https://evil.example"}
    assert _create(serve, "x", headers=evil).status_code in (401, 403)


def test_the_service_port_refuses_token_writes_even_with_serves_headers(serve_settings, monkeypatch):
    """Token writes need a person through Serve; the port refuses them however
    the headers look. (That X-Forwarded-Host is ignored on the port is proven
    at the gate: test_serve_gate.py.)"""
    from api.main import app
    app.dependency_overrides[get_settings] = lambda: serve_settings
    monkeypatch.setattr("api.main.get_settings", lambda: serve_settings)
    try:
        with TestClient(app, base_url="http://127.0.0.1:8420") as local:
            response = _create(local, "x", headers=SERVE_SENDS)
            assert response.status_code == 401
    finally:
        app.dependency_overrides.clear()

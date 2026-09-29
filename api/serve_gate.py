"""Default-deny at the ASGI layer, on both listeners (split, part 2).

q-core binds only to loopback and has two listeners:

- **The service port** (`settings.port`, 8420, TCP on 127.0.0.1): local tools,
  the in-process MCP server and the mobile client's SSH forward. Every request needs
  a valid token of some scope; routes then enforce the scope they need. The
  one exception, `CREDENTIAL_FREE`, is the Google OAuth callback: a browser
  redirect that carries no bearer and is authenticated instead by its
  single-use `state`. Identity headers mean nothing here.
- **The Serve socket** (`settings.serve_socket`, a Unix socket in a 0700
  directory owned by the service user): Tailscale Serve proxies the tailnet
  to it. Only tailscaled (root) and the service's own user can connect,
  and that user already holds the database, so an identity header on this
  listener was set by Serve (which deletes client-supplied ones first).
  A request needs either
    * an allowlisted Tailscale identity (`Tailscale-User-Login`), and then
      only for the exact browser routes in `BROWSER_SURFACE`, or
    * a valid token of any scope (routes enforce scope).

Everything else is refused before routing, so docs, mounts and any future
route are covered without being listed. The route-level check for the
browser routes lives in api/auth.py (`require_reader`), so identity is
checked twice and a transcription token cannot read them.
"""
from __future__ import annotations

import re

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from api.config import get_settings

IDENTITY_HEADER = b"tailscale-user-login"

#: (method, path) served with no credential on the service port.
CREDENTIAL_FREE = frozenset({("GET", "/integrations/google/callback")})

#: FastAPI's generated docs have no route of ours to carry a scope check, so
#: the gate asks for a `full` token itself: the schema is not the phone's.
DOC_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"})

#: The browser pages and the JSON they fetch: what an allowlisted Tailscale
#: identity may reach through Serve. Exact, and pinned by a test that walks
#: every route, so a new route cannot join it silently.
BROWSER_SURFACE = (
    ("GET", r"/ui/spending"),
    ("GET", r"/ui/assets/highcharts(?:-accessibility)?\.js"),
    ("GET", r"/spending/periods"),
    ("GET", r"/statements/coverage"),
    ("GET", r"/ui/evals"),
    ("GET", r"/evals/runs"),
    ("GET", r"/ui/forecast"),
    ("GET", r"/forecast/projection"),
    ("GET", r"/ui/briefs"),
    ("GET", r"/ui/briefs/[^/]+"),
    # Google's redirect after consent, when the sign-in ran through Serve
    # (api/google_calendar._redirect_uri); still bound to its single-use state.
    ("GET", r"/integrations/google/callback"),
    # The token page (api/token_ui.py), which additionally requires identity
    # for its JSON and pins writes to the configured Serve origin.
    ("GET", r"/ui/tokens"),
    ("GET", r"/ui/tokens/list"),
    ("POST", r"/ui/tokens/create"),
    ("POST", r"/ui/tokens/revoke"),
)
_SURFACE = tuple((method, re.compile(pattern)) for method, pattern in BROWSER_SURFACE)


def _refuse(message: str) -> JSONResponse:
    return JSONResponse(status_code=401, content={"error": {"code": "unauthorized", "message": message}})


def request_came_through_serve(scope, settings) -> bool:
    """Did this request arrive on the Serve socket?

    uvicorn reports a Unix socket as `server = (path, None)`. A request with
    no `server` at all is treated as Serve (the stricter listener) whenever a
    Serve socket is configured: fail closed.
    """
    path = settings.serve_socket
    if not path:
        return False
    server = scope.get("server")
    if not server:
        return True
    return server[0] == path and server[1] is None


def with_serve_host(scope):
    """The Host the client addressed, for a request that came through Serve.

    Tailscale Serve proxies to a Unix socket with `Host: localhost` and puts
    the client's host in `X-Forwarded-Host`, set with Header.Set, so any value
    the client sent is overwritten (tailscale ipn/ipnlocal/serve.go). Only
    tailscaled can reach the socket, so on it that header is Serve's word.
    Restoring it here means everything after this gate (the token page's
    pinned origin, the MCP transport's DNS-rebinding guard) sees the name the
    browser used. Never applied on the TCP port, where anyone local can set
    any header.

    This trust holds only while the socket is the target of Serve's HTTP(S)
    proxy handler (`tailscale serve --https=443 unix:...`). A raw TCP forward
    to it (`--tcp` / `--tls-terminated-tcp`) would pass client headers through
    unchanged, breaking this and the identity headers alike; never configure
    one (the NixOS host configuration runs exactly the HTTPS form).
    """
    headers = list(scope.get("headers") or ())
    forwarded = next((value for name, value in headers if name == b"x-forwarded-host"), None)
    if not forwarded:
        return scope
    rewritten = [(name, value) for name, value in headers if name != b"host"]
    rewritten.append((b"host", forwarded))
    return {**scope, "headers": rewritten}


def identity_allowed(headers: dict, settings) -> bool:
    login = headers.get(IDENTITY_HEADER, b"").decode(errors="replace").strip().lower()
    allowed = {entry.strip().lower() for entry in settings.ui_allowed_logins if entry.strip()}
    return bool(login) and login in allowed


def in_browser_surface(method: str, path: str) -> bool:
    method = "GET" if method == "HEAD" else method
    return any(m == method and pattern.fullmatch(path) for m, pattern in _SURFACE)


class ServeGate:
    """Pure ASGI middleware; refuses unauthenticated requests on both listeners."""

    def __init__(self, app, settings_provider=get_settings):
        self.app = app
        self._settings = settings_provider

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        settings = self._settings()
        method, path = scope.get("method", "GET"), scope.get("path", "")
        through_serve = request_came_through_serve(scope, settings)
        if through_serve:
            scope = with_serve_host(scope)
        headers = dict(scope.get("headers") or ())
        if through_serve:
            if identity_allowed(headers, settings) and in_browser_surface(method, path):
                await self.app(scope, receive, send)
                return
        elif (method, path) in CREDENTIAL_FREE:
            await self.app(scope, receive, send)
            return
        from api import tokens
        from api.auth import constant_time_equal, token_accepted

        authorization = headers.get(b"authorization", b"").decode("latin-1") or None
        # The phone's configured transcription token (settings, not a row) is a
        # credential for dictation only; the route checks it again.
        phone = settings.transcription_token
        if (method, path) == ("POST", "/transcriptions") and phone and authorization \
                and constant_time_equal(authorization, "Bearer " + phone):
            await self.app(scope, receive, send)
            return
        scopes = ("full",) if path in DOC_PATHS else tokens.SCOPES
        if authorization and await run_in_threadpool(token_accepted, settings, authorization, scopes):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        message = ("Requests through Tailscale Serve need an allowed Tailscale identity (browser pages) or a valid token"
                   if through_serve else "Missing or invalid bearer token")
        await _refuse(message)(scope, receive, send)

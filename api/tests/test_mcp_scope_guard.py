"""The MCP mount forwards http and lifespan scopes, and refuses the rest.

The bearer check in `_McpMount.__call__` was gated on
`scope["type"] == "http"`, so ANY other scope type skipped it and fell
through to the MCP app. A websocket upgrade would have reached the MCP
server with no token at all.

LATENT, NOT EXPLOITABLE, and that is the argument for fixing it rather
than against. No websocket routes exist and Starlette refuses the upgrade
before this mount is reached -- so the protection today is someone else's
routing table, which is not a decision anyone made. Adding the first
websocket route would silently convert an unreachable path into an
unauthenticated one, in a diff about something else entirely.

These call the mount DIRECTLY as an ASGI callable rather than through a
client, because a client cannot produce the scope in question: the whole
point is that nothing routes a websocket here yet. Driving it through
Starlette would test Starlette's refusal, not this mount's.
"""

from __future__ import annotations

import pytest

from api.main import _McpMount


class _Spy:
    """Stands in for the MCP app, and records whether it was reached."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, scope, receive, send) -> None:
        self.calls.append(scope)
        await send({"type": "websocket.accept"})


async def _noop_receive():  # pragma: no cover - never awaited on a refusal
    return {"type": "websocket.connect"}


def _drive(scope):
    """Run the mount against one scope; return (sent messages, spy)."""
    import anyio

    sent: list[dict] = []
    spy = _Spy()
    mount = _McpMount()
    mount.serve(spy)

    async def send(message):
        sent.append(message)

    anyio.run(lambda: mount(scope, _noop_receive, send))
    return sent, spy


def test_a_websocket_scope_is_closed_without_reaching_the_app():
    """The case the guard exists for.

    Closed with 1008 (policy violation) BEFORE any accept, so there is
    never a handshake with an unauthenticated caller.
    """
    sent, spy = _drive({"type": "websocket", "path": "/mcp", "headers": []})

    assert spy.calls == [], "the websocket scope reached the MCP app"
    assert sent == [{"type": "websocket.close", "code": 1008}]
    assert not any(m["type"] == "websocket.accept" for m in sent)


def test_a_websocket_scope_is_refused_before_any_token_is_consulted():
    """Not an authentication failure -- a scope that is not served at all.

    The header here is deliberately arbitrary: the guard fires before the
    bearer check, so whether the token is right never enters into it. An
    earlier version of this test fetched the real token to prove "even
    with a valid token", which needed settings the suite does not have --
    and would have tested a weaker property anyway, since it could not
    distinguish "refused before the check" from "failed the check".
    """
    header = (b"authorization", b"Bearer anything-at-all")
    sent, spy = _drive({"type": "websocket", "path": "/mcp", "headers": [header]})

    assert spy.calls == []
    assert sent == [{"type": "websocket.close", "code": 1008}]
    assert not any("http.response" in m["type"] for m in sent), (
        "a 401 here would mean the bearer check ran -- the scope should "
        "never get that far"
    )


@pytest.mark.parametrize("scope_type", ["ftp", "grpc", "", "HTTP"])
def test_an_unknown_scope_type_is_refused_silently(scope_type):
    """Allow-list, not deny-list: a new ASGI scope arrives refused.

    "HTTP" is in the list on purpose -- scope types are lowercase, and a
    case-mismatched value must not be mistaken for the real thing.
    """
    sent, spy = _drive({"type": scope_type, "path": "/mcp", "headers": []})

    assert spy.calls == [], f"{scope_type!r} reached the MCP app"
    assert sent == [], f"{scope_type!r} should be dropped, not answered"


def test_lifespan_still_passes_through():
    """Startup and shutdown carry no credentials and touch no request.

    Refusing them would break the mount rather than protect it, which is
    why the allow-list has two entries and not one.
    """
    sent, spy = _drive({"type": "lifespan"})

    assert len(spy.calls) == 1
    assert spy.calls[0]["type"] == "lifespan"


def test_http_without_a_token_is_still_401_not_dropped():
    """The existing behaviour is unchanged by the guard.

    An http scope must still reach the bearer check and get a JSON 401 --
    not be silently dropped like an unknown scope.
    """
    sent, spy = _drive({"type": "http", "path": "/mcp", "method": "POST", "headers": []})

    assert spy.calls == [], "an unauthenticated request reached the MCP app"
    assert sent, "an http scope was dropped instead of answered"
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 401

import hmac

from fastapi import Depends, Header, HTTPException, Request

from api.config import Settings, get_settings


def constant_time_equal(presented: str, expected: str) -> bool:
    """hmac.compare_digest over UTF-8 bytes.

    compare_digest on two str values raises TypeError when either holds a
    non-ASCII character, which turned a garbage Authorization header into a
    500. Bytes compare in constant time whatever they contain.
    """
    return hmac.compare_digest(presented.encode("utf-8", "surrogateescape"), expected.encode("utf-8", "surrogateescape"))


def token_accepted(settings: Settings, authorization: str | None, scopes: tuple[str, ...]) -> bool:
    """The service token, or an unrevoked client token with one of `scopes`.

    See api/tokens.py for the two kinds of credential. The client-token
    lookup opens its own short connection, so the refusal does not depend on
    (or wait for) a route's DB dependency.
    """
    from api import tokens

    if authorization and settings.api_token and constant_time_equal(authorization, f"Bearer {settings.api_token}"):
        return True
    if not authorization or not authorization.startswith("Bearer " + tokens.TOKEN_PREFIX):
        return False
    connection = tokens.open_connection(settings)
    try:
        return tokens.verify(connection, authorization, allowed_scopes=scopes)
    finally:
        connection.close()


def require_token(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """Service token or a `full`-scope client token (api/tokens.py).

    Always apply this via `dependencies=[Depends(require_token)]` at the
    route/router level, never as a named parameter dependency placed after
    `Depends(get_connection)`, so an unauthenticated request is rejected
    before the route opens its connection.
    """
    if not token_accepted(settings, authorization, ("full",)):
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "code": "unauthorized",
                    "message": "Missing or invalid bearer token",
                }
            },
        )


def require_reader(
    request: Request,
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """The browser routes (api/serve_gate.BROWSER_SURFACE): a `full` token, or
    an allowlisted Tailscale identity on a request that came through Serve.

    These used to be public on loopback. The gate already refuses anything
    without a credential; this second check keeps a narrower token (the
    phone's `transcription` scope) from reading them, and keeps identity
    meaningful only where Serve set it.
    """
    from api.serve_gate import identity_allowed, request_came_through_serve

    if token_accepted(settings, authorization, ("full",)):
        return
    if request_came_through_serve(request.scope, settings) and identity_allowed(dict(request.scope["headers"]), settings):
        return
    raise HTTPException(
        status_code=401,
        detail={"error": {"code": "unauthorized", "message": "Missing or invalid bearer token"}},
    )

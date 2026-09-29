"""App-raised error types.

Each subclasses `fastapi.HTTPException` with a `detail` that is already the
`{"error": {"code": ..., "message": ...}}` envelope this API returns. The
`http_exception_handler` registered in `api/main.py` passes such a detail
through unchanged, so route handlers just `raise NotFoundError(...)` and
never build a `JSONResponse` themselves.

Deliberately not a separate `APIError` hierarchy with its own handler: the
Phase 1 handler already covers any HTTPException carrying that envelope,
and a parallel hierarchy would mean two ways to produce the same response.
"""

import logging

from fastapi import HTTPException
from fastapi.responses import JSONResponse


class NotFoundError(HTTPException):
    def __init__(self, message: str):
        super().__init__(
            status_code=404,
            detail={"error": {"code": "not_found", "message": message}},
        )


class InvalidReferenceError(HTTPException):
    def __init__(self, message: str):
        super().__init__(
            status_code=400,
            detail={"error": {"code": "invalid_reference", "message": message}},
        )


class ConflictError(HTTPException):
    """The request is well-formed and refers to things that exist, but it
    cannot be applied against the current state — 409.

    409, not 400, and the distinction is the whole reason this class
    exists separately (D17, corrected by this change). Three codes, three
    meanings, and they only stay legible if they are read together:

      400 InvalidReferenceError — refers to something that DOES NOT EXIST
      409 ConflictError         — conflicts with something that DOES
      422 RequestValidationError — a value outside its permitted range

    It was 400 until now, which made 400 mean two different things and
    left the next reader opening the source to tell which. Two meanings
    for one status is the same defect as one name for two behaviours.

    Everything this is raised for is a state conflict rather than a bad
    reference: a category or board or entity that cannot be deleted
    because something still points at it, a statement period that
    already exists, a relationship that exists with different values.
    A caller cannot fix any of them by correcting the request — they
    have to change the state or accept the answer, which is exactly
    what 409 means and 400 does not.
    """

    def __init__(self, message: str):
        super().__init__(
            status_code=409,
            detail={"error": {"code": "conflict", "message": message}},
        )


class DataIntegrityError(HTTPException):
    """Stored data does not match what the code requires — a 500, because the
    caller did nothing wrong and cannot fix it.

    Raised instead of letting a bare KeyError or IndexError escape, which
    returns a plain-text traceback and so breaks the error envelope every
    other response honours. The choice here is deliberately *not* to skip the
    offending row: silently omitting a ticket from its board hides work in a
    system whose job is being a trustworthy record, which is the quiet failure
    mode this codebase keeps choosing against. Loud and diagnosable beats
    quietly incomplete.
    """

    def __init__(self, message: str):
        super().__init__(
            status_code=500,
            detail={"error": {"code": "data_integrity", "message": message}},
        )


#: What an unhandled exception returns. Deliberately a fixed string rather
#: than str(exc): an exception message can contain anything a route
#: interpolated into it -- a filename, a row, an account number -- and this
#: body reaches the MCP client and so a model's context.
INTERNAL_ERROR_MESSAGE = (
    "The server hit an unexpected error. The details are in the server "
    "log; quote the X-Request-ID header when reporting it."
)

#: A database that cannot be used at all is worth distinguishing from a
#: generic failure: it is the one 500 with a specific, actionable remedy,
#: and mcp/q_core_mcp/client.py surfaces this message verbatim to whoever
#: called the tool. Keep the log path and the command in it -- they are
#: what makes the message actionable, and mcp/tests/test_client.py pins
#: their presence.
DATABASE_UNUSABLE_MESSAGE = (
    "The database is not usable -- missing tables, or a file that is not a "
    "database. The real error is in data/logs/api.log; run "
    "`python -m api.run` in a terminal to watch it happen."
)


class ErrorEnvelopeMiddleware:
    """Return this API's error envelope for an otherwise-unhandled exception.

    Written as middleware rather than `@app.exception_handler(Exception)`,
    and that is the whole design. Starlette routes a handler registered for
    `Exception` (or 500) to `ServerErrorMiddleware`, which sits *outside*
    every user middleware and sends through its own `send`. A response from
    there never passes through `RequestLoggingMiddleware`'s send wrapper, so
    it carries no `X-Request-ID` -- leaving the response class a user is
    most likely to report as the only one whose id they cannot quote back.

    Installed INSIDE the logging middleware (see `install_error_envelope`),
    so the envelope it sends travels back out through that wrapper and
    picks the header up.

    The exception is swallowed rather than re-raised. Re-raising after a
    response has been sent makes the server log the same traceback a second
    time, which is the duplicate-line problem the observability work
    already fixed once.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = False

        async def track(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, track)
        except Exception as exc:
            method, path = scope.get("method", ""), scope.get("path", "")
            logging.getLogger("api.error").exception(
                "unhandled %s on %s %s",
                type(exc).__name__,
                method,
                path,
                extra={"method": method, "path": path, "status": 500},
            )
            if started:
                # The response was already on the wire when this blew up;
                # there is no way to replace it, and sending a second
                # http.response.start would be an ASGI protocol violation.
                raise
            await _envelope_response(exc)(scope, receive, track)


def _envelope_response(exc: Exception) -> JSONResponse:
    from api.db import DatabaseNotUsableError

    if isinstance(exc, DatabaseNotUsableError):
        code, message = "database_unusable", DATABASE_UNUSABLE_MESSAGE
    else:
        code, message = "internal_error", INTERNAL_ERROR_MESSAGE
    return JSONResponse(
        status_code=500, content={"error": {"code": code, "message": message}}
    )


def install_error_envelope(app) -> None:
    """Attach `ErrorEnvelopeMiddleware`.

    Order matters and is not incidental: `install_request_logging` must be
    called AFTER this, because `add_middleware` inserts at the front, so
    the last one added ends up outermost. That leaves the stack as
    ServerError -> RequestLogging -> ErrorEnvelope -> routes, which is what
    puts this middleware's response inside the logging middleware's send
    wrapper. `test_the_error_response_carries_the_request_id_header`
    fails if that order is reversed.
    """
    app.add_middleware(ErrorEnvelopeMiddleware)

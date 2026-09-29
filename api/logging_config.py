"""JSON-lines logging for the API server.

One JSON object per line, written to a rotating file under `data/logs/`.
The format exists so that debugging a report from the owner means reading
`data/logs/api.log` off disk — `grep`, `jq`, or just `tail` — rather than
asking them to reproduce the problem with a terminal open.

Deliberately duplicated rather than shared with the MCP server's
`q_core_mcp.logging_config`: `mcp/` is a thin HTTP client of this API and
imports nothing of `api/`'s internals, so a shared logging module would be
the first coupling between the two packages. The duplication is about
sixty lines and buys that independence; see runbooks/observability.md.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from opentelemetry import trace

from starlette.datastructures import MutableHeaders

#: Correlates every log line emitted while handling one request. Set by
#: `RequestLoggingMiddleware`; empty outside a request (startup, shutdown).
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5

#: Attributes every LogRecord carries. Anything on a record that isn't one
#: of these was passed by a caller as `extra=` and belongs in the output.
#: Hardcoded rather than derived from a dummy record because `logging` adds
#: attributes over Python versions, and a silently-growing exclusion set
#: would start dropping caller fields without anything failing.
_RESERVED_RECORD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

#: Extras a library attaches that are noise in a file. uvicorn puts the
#: ANSI-coloured duplicate of `message` on several of its records; it is
#: built for a terminal and in a JSON file it is unreadable escape codes
#: restating a field that is already there.
_DROPPED_RECORD_ATTRS = frozenset({"color_message"})

#: Output keys this formatter owns. A caller's `extra={"level": ...}` must
#: not be able to relabel a record's real severity — a log that can be
#: made to misreport itself is worse than no log.
_OWNED_KEYS = ("timestamp", "level", "logger", "message", "request_id", "traceback", "trace_id", "span_id")


class JsonFormatter(logging.Formatter):
    """Render a LogRecord as one line of JSON.

    `default=repr` on the dump is load-bearing: a caller passing a
    non-serializable object in `extra=` would otherwise raise TypeError
    *inside* the logging call, dropping the record entirely. Losing the
    fidelity of one field beats losing the line that says what went wrong.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED_RECORD_ATTRS
            and key not in _DROPPED_RECORD_ATTRS
            and key not in _OWNED_KEYS
        }

        payload["timestamp"] = datetime.fromtimestamp(
            record.created, tz=timezone.utc
        ).isoformat()
        payload["level"] = record.levelname
        payload["logger"] = record.name
        payload["message"] = record.getMessage()

        request_id = getattr(record, "request_id", None) or request_id_var.get()
        if request_id:
            payload["request_id"] = request_id

        # Correlation with the server's traces (api/telemetry.py). Without
        # telemetry the current span is the no-op one, whose context is
        # invalid, so nothing is added.
        context = trace.get_current_span().get_span_context()
        if context.is_valid:
            payload["trace_id"] = format(context.trace_id, "032x")
            payload["span_id"] = format(context.span_id, "016x")

        if record.exc_info:
            payload["traceback"] = self.formatException(record.exc_info)

        # ensure_ascii keeps the line byte-safe for grep; the newlines in a
        # traceback are escaped by json.dumps, which is what keeps one
        # record on one line.
        return json.dumps(payload, default=repr, ensure_ascii=True)


#: Marks the handler this module installed, so a second call can recognize
#: its own work. Identity of the handler object isn't enough — uvicorn's
#: reloader re-imports the app in a fresh module namespace.
_HANDLER_TAG = "q_core_api_json"


def _build_handler(log_path: Path) -> logging.handlers.RotatingFileHandler:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    handler.setFormatter(JsonFormatter())
    handler.set_name(_HANDLER_TAG)
    return handler


def log_path(settings) -> Path:
    return Path(settings.logs_dir) / "api.log"


def _new_request_id() -> str:
    # 12 hex chars: short enough for the owner to read off a terminal and quote
    # back, wide enough that a collision inside one log-retention window is
    # not a real concern at single-user volume.
    return uuid4().hex[:12]


class RequestLoggingMiddleware:
    """Log one line per request; stamp `X-Request-ID` on the response.

    Written as raw ASGI rather than `BaseHTTPMiddleware` because the latter
    runs the downstream app in a separate anyio task, which makes
    contextvar propagation an implementation detail to rely on rather than
    a guarantee. Raw ASGI stays in the caller's context, so the id set here
    is visible to every `logging` call made while handling the request.

    The request id is taken from an inbound `X-Request-ID` when present, so
    a call that originates at the MCP server keeps one id end to end.

    Known limitation: an unhandled exception is re-raised so that Starlette's
    ServerErrorMiddleware produces exactly the response it produces today.
    That middleware sits *outside* this one and sends through its own
    `send`, so a 500 from an unhandled exception carries no `X-Request-ID`
    header — correlate those by path and timestamp against the ERROR line,
    which does carry the id. See runbooks/observability.md.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _inbound_request_id(scope) or _new_request_id()
        method = scope.get("method", "")
        # Path only — never the query string, which can carry a person's
        # name or an account number as a search term.
        path = scope.get("path", "")

        token = request_id_var.set(request_id)
        started = time.perf_counter()
        seen = {"status": 500}

        async def send_with_request_id(message):
            if message["type"] == "http.response.start":
                seen["status"] = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        logger = logging.getLogger("api.request")
        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            logger.exception(
                "%s %s failed",
                method,
                path,
                extra={
                    "method": method,
                    "path": path,
                    "status": 500,
                    "duration_ms": _elapsed_ms(started),
                },
            )
            raise
        else:
            logger.info(
                "%s %s %s",
                method,
                path,
                seen["status"],
                extra={
                    "method": method,
                    "path": path,
                    "status": seen["status"],
                    "duration_ms": _elapsed_ms(started),
                },
            )
        finally:
            request_id_var.reset(token)


def _inbound_request_id(scope) -> str | None:
    for key, value in scope.get("headers", ()):
        if key == b"x-request-id":
            return value.decode("latin-1")[:64] or None
    return None


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def install_request_logging(app) -> None:
    """Attach `RequestLoggingMiddleware` to a FastAPI/Starlette app.

    Separate from `configure_logging` so it can run at import time: it
    touches no filesystem and reads no settings, which keeps importing
    `api.main` free of side effects.
    """
    app.add_middleware(RequestLoggingMiddleware)


def configure_logging(settings) -> logging.Logger:
    """Install the JSON file handler on the root logger. Idempotent.

    Root rather than an `api`-only logger so that anything logging from
    inside the process — uvicorn, and any library that misbehaves — lands
    in the same file with the same shape. There is deliberately no stream
    handler: stdout/stderr are left to launchd's own capture, which exists
    to catch failures that happen before this function ever runs.
    """
    root = logging.getLogger()
    root.setLevel(settings.log_level)
    # See uvicorn_log_config: httpx's INFO line carries the full URL.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(max(logging.WARNING, logging.getLogger(name).level))

    for handler in root.handlers:
        if handler.get_name() == _HANDLER_TAG:
            handler.setLevel(settings.log_level)
            return root

    root.addHandler(_build_handler(log_path(settings)))
    return root


def uvicorn_log_config(settings) -> dict:
    """A `logging.config.dictConfig` dict for `uvicorn --log-config`.

    Uvicorn installs its own stdout handlers at startup unless handed a
    config, which would leave its records on the terminal only. This routes
    them into the same JSON file as everything else, so a failure to start
    (port already bound, missing token) is still diagnosable afterwards.

    `uvicorn.access` is deliberately silenced rather than routed in.
    Uvicorn renders its access line through
    `uvicorn.protocols.utils.get_path_with_query_string`, so every line
    carries the query string — measured, not assumed. Writing those to disk
    would put search terms and anything else a caller puts in a query
    string into a file, which is the shape CLAUDE.md rules out. The
    `RequestLoggingMiddleware` above already records every request with
    more detail (duration, request id) and without the query string, so
    silencing uvicorn's version loses nothing. See
    runbooks/observability.md.
    """
    path = log_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)

    file_handler = {
        "class": "logging.handlers.RotatingFileHandler",
        "filename": str(path),
        "maxBytes": MAX_BYTES,
        "backupCount": BACKUP_COUNT,
        "encoding": "utf-8",
        "formatter": "json",
    }
    return {
        "version": 1,
        # False: uvicorn applies this config after this app's own modules
        # have imported, and True would switch off every logger they hold.
        "disable_existing_loggers": False,
        "formatters": {"json": {"()": f"{__name__}.JsonFormatter"}},
        "handlers": {_HANDLER_TAG: file_handler},
        "loggers": {
            "uvicorn": {
                "handlers": [_HANDLER_TAG],
                "level": settings.log_level,
                "propagate": False,
            },
            "uvicorn.error": {
                "handlers": [_HANDLER_TAG],
                "level": settings.log_level,
                "propagate": False,
            },
            "uvicorn.access": {
                "handlers": [],
                "level": "CRITICAL",
                "propagate": False,
            },
            # httpx logs every request at INFO as "HTTP Request: GET <full
            # URL>", query string included: the MCP tools' own calls to this
            # API carry search terms there (list_notes' q=). Same reason as
            # uvicorn.access above; warnings and errors still come through.
            "httpx": {"level": "WARNING"},
            "httpcore": {"level": "WARNING"},
        },
        "root": {"handlers": [_HANDLER_TAG], "level": settings.log_level},
    }

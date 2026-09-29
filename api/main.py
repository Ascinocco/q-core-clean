import logging
import threading
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.concurrency import run_in_threadpool
from mcp.server.transport_security import TransportSecuritySettings

from api.briefing_data import router as briefing_data_router
from api.briefing_viewer import router as briefing_viewer_router
from api.artifacts import router as artifacts_router
from api.artifact_links import router as artifact_links_router
from api.review_inbox import router as review_inbox_router
from api.auth import require_reader, require_token, token_accepted
from api.config import get_settings as _config_get_settings
from api.serve_gate import ServeGate
from api.config import get_settings
from api.config import REPO_ROOT
from api.documents import (
    AttachmentNotRedactedError,
    AttachmentTooLargeError,
    NoTextExtractedError,
    PartialExtractionError,
    RedactionIncompleteError,
    router as documents_router,
)
from api.due import router as due_router
from api.entities import router as entities_router
from api.errors import install_error_envelope
from api.financial import router as financial_router
from api.google_calendar import callback_router as google_callback_router, router as google_calendar_router
from api.jyra import router as jyra_router
from api.financial_corrections import router as financial_corrections_router
from api.source_imports import router as source_imports_router
from api.reminders import router as reminders_router
from api.notes import router as notes_router
from api.spending import router as spending_router
from api.token_ui import router as token_ui_router
from api.evaluations import router as evaluations_router
from api.forecast import router as forecast_router
from api.transcription import router as transcription_router
from api.db import apply_startup_migrations, ensure_wal_mode
from api.logging_config import configure_logging, install_request_logging
from api.personal_redaction import PersonalRedactionError
from api.spreadsheet import UnsupportedDocumentError
from q_core_mcp.client import QCoreClient
from q_core_mcp.server import build_server


class _LazyClient:
    """Defers QCoreClient construction to the first tool call.

    The MCP app has to be built at import time so it can be mounted, but
    importing `api.main` must stay side-effect free and `QCoreClient`
    reads the token and port from `Settings` in its constructor — which
    `get_settings()` cannot supply at import without breaking the same
    rule the lifespan docstring above describes.

    It stays a loopback HTTP client of this API even though it now runs
    inside it. That keeps `q_core_mcp` a client of the API's public
    surface rather than of its internals, and keeps every round-trip test
    proving what it proved before.
    """

    def __init__(self) -> None:
        self._client: QCoreClient | None = None
        self._lock = threading.Lock()

    def _ensure(self) -> QCoreClient:
        """Construct exactly once, however many callers arrive together.

        Double-checked under a lock: the steady-state path is a plain
        attribute read, and only the first callers take the lock.

        Worth saying why the lock is not redundant, because it looks it.
        `__getattr__` is synchronous with no await between the check and
        the assignment, so asyncio alone cannot interleave two first
        calls -- but that is a property of how this method is written
        today, not a guarantee of the design. Make construction
        awaitable, or reach this from a worker thread (it is an ordinary
        sync attribute access, so anything may), and two clients get
        built -- each with its own httpx connection pool, one of which
        is then silently orphaned.
        """
        if self._client is None:
            with self._lock:
                if self._client is None:
                    self._client = QCoreClient(get_settings())
        return self._client

    def __getattr__(self, name: str):
        """Delegate EVERYTHING to the real client, not a list of methods.

        This proxied only `request` (ticket T-21). `attach_file` reads
        `client.settings`, so against the deployed server it raised
        AttributeError for its whole shipped life -- the model saw
        "Error executing tool attach_file", and every refusal in that
        tool was dead code in production. `read_attachment` then reached
        for `client.fetch_bytes` and would have repeated it exactly.

        Two tools, the same bug, because the fix for the first would have
        been to add one more name to a list. Delegating wholesale removes
        the category: there is no list to fall behind.

        Underscore names are refused rather than forwarded. Nothing
        outside this class should reach a private through the proxy, and
        it also stops `_client` recursing here if __init__ has not run.
        """
        if name.startswith("_"):
            raise AttributeError(name)
        client = self._ensure()
        try:
            return getattr(client, name)
        except AttributeError as exc:
            # A genuine gap in the REAL client, not an unbuilt proxy.
            # Re-raised naming the underlying type so it cannot read as
            # "the lazy wrapper is not wired up" and send someone to fix
            # this class instead of the one actually missing the
            # attribute. Construction is not retried on the strength of
            # it either: the client is already cached by this point, so
            # an AttributeError here can never be mistaken for "not
            # constructed yet" and trigger a second build.
            raise AttributeError(
                f"{type(client).__name__} has no attribute {name!r} "
                f"(reached through {type(self).__name__})"
            ) from exc


class _McpMount:
    """Auth wrapper and indirection for the mounted MCP app.

    Two jobs, both forced by measurement rather than taste:

    A mounted ASGI app is invisible to FastAPI's route-level
    `Depends(require_token)` — `tools/list` answered 200 with no
    Authorization header at all before this existed — so the bearer check
    has to wrap the app itself.

    And the app is built per-lifespan rather than at import, because
    `StreamableHTTPSessionManager.run()` refuses a second call on the same
    instance: a module-level app can be started exactly once per process,
    so anything that stops and restarts the app — a test entering the
    lifespan twice, `--reload` — raises instead of serving. The mount is
    therefore a stable object and the app behind it is replaced on start.
    """

    def __init__(self) -> None:
        self._app = None

    def serve(self, app) -> None:
        self._app = app


    #: The only scope types this mount will forward. Everything else is
    #: refused rather than passed through.
    _ALLOWED_SCOPES = frozenset({"http", "lifespan"})

    async def __call__(self, scope, receive, send) -> None:
        # REFUSE BY DEFAULT, because the bearer check below is gated on
        # `scope["type"] == "http"`. Any other scope type skipped the
        # check entirely and fell through to the app -- so a websocket
        # upgrade would have reached the MCP server with no token.
        #
        # Latent rather than exploitable today: no websocket routes exist,
        # and Starlette refuses the upgrade before this mount is reached.
        # That is exactly why it is worth closing now. The protection is
        # someone else's routing table, which is not a decision anyone
        # made, and adding the first websocket route would silently
        # convert an unreachable path into an unauthenticated one.
        #
        # An allow-list, not a deny-list: a new ASGI scope type should
        # arrive refused, not forwarded. `lifespan` is here because
        # startup and shutdown carry no credentials and never touch a
        # request.
        if scope["type"] not in self._ALLOWED_SCOPES:
            if scope["type"] == "websocket":
                # Close before accepting: never handshake with a caller
                # that has not been authenticated.
                await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or ())
            # latin-1, as Starlette decodes headers: any byte is a valid
            # character, so a garbage header is refused below, never a 500.
            supplied = headers.get(b"authorization", b"").decode("latin-1") or None
            # A client-token check opens SQLite (10 s busy timeout) and may
            # write last_used_at; keep that off the event loop.
            if not await run_in_threadpool(token_accepted, get_settings(), supplied, ("full",)):
                response = JSONResponse(
                    status_code=401,
                    content={
                        "error": {
                            "code": "unauthorized",
                            "message": "Missing or invalid bearer token",
                        }
                    },
                )
                await response(scope, receive, send)
                return
        if self._app is None:
            response = JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "unavailable",
                        "message": "The MCP endpoint is not running.",
                    }
                },
            )
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)


_mcp_mount = _McpMount()


def _mcp_transport_security(settings) -> TransportSecuritySettings:
    """The SDK's loopback DNS-rebinding guard, plus the Tailscale Serve name.

    The SDK enables the guard by itself only for a loopback `host`, allowing
    exactly these loopback hosts and origins. Serve forwards the client's
    Host (`q-core.<tailnet>.ts.net`), which the guard answered with 421, so
    MCP never worked through Serve. Only the one configured name is added;
    any other Host is still refused.
    """
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    if settings.serve_hostname:
        hosts += [settings.serve_hostname, settings.serve_hostname + ":*"]
        origins += ["https://" + settings.serve_hostname, "https://" + settings.serve_hostname + ":*"]
    return TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                     allowed_hosts=hosts, allowed_origins=origins)


def _build_mcp_app(settings):
    """streamable_http_path="/" because the app is mounted at "/mcp".

    Starlette strips the mount prefix before the sub-app sees the path.
    Mounting at "/" instead would shadow every unmatched route — measured:
    /health answered 401 from the MCP auth wrapper and an unknown path
    became a 500.
    """
    return build_server(_LazyClient()).streamable_http_app(
        streamable_http_path="/", transport_security=_mcp_transport_security(settings)
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open the log file when the server actually starts.

    Deliberately here rather than at import time: importing `api.main` must
    stay free of side effects (it happens in every test and in `--reload`'s
    parent process), and `get_settings()` raises without a configured
    token, which would turn a plain import into a failure.
    """
    settings = get_settings()
    configure_logging(settings)
    # The one place migrations are applied. Before serving anything: a
    # request served against a half-known schema fails in its own way, far
    # from the cause. A failure here is fatal on purpose.
    # Before migrations: they write, and a WAL database is the one that
    # does not block a reader while they do.
    ensure_wal_mode(settings)
    apply_startup_migrations(settings)
    logging.getLogger("api").info("api server started")
    # Starlette does not run a mounted app's lifespan, and the streamable
    # HTTP transport's session manager starts in exactly that lifespan.
    # Without this, every /mcp request fails with "Task group is not
    # initialized" — measured, and invisible to any test that does not
    # actually speak the protocol.
    mcp_app = _build_mcp_app(settings)
    async with mcp_app.router.lifespan_context(mcp_app):
        _mcp_mount.serve(mcp_app)
        try:
            yield
        finally:
            _mcp_mount.serve(None)
    logging.getLogger("api").info("api server stopped")


app = FastAPI(title="q-core API", lifespan=lifespan)
# Installed at import: adding middleware touches no filesystem and reads no
# settings, and it must be in place before the app handles anything.
#
# Order is load-bearing. `add_middleware` inserts at the front of the list,
# so the LAST call ends up outermost: ServerError -> RequestLogging ->
# ErrorEnvelope -> routes. That puts the envelope response inside the
# logging middleware's send wrapper, which is what stamps X-Request-ID on
# it. Swapping these two lines silently drops that header from every 500 —
# api/tests/test_unhandled_exceptions.py fails if they are reversed.
# ServeGate first, so it sits innermost of these: a refused Serve request is
# still logged (and gets an X-Request-ID) by the middleware around it.
def _gate_settings():
    """Settings for ServeGate: a `Depends(get_settings)` override when one is
    installed (keyed on api.config's function, never this module's name, which
    tests replace), else this module's get_settings like the lifespan uses."""
    override = app.dependency_overrides.get(_config_get_settings)
    return override() if override is not None else get_settings()


app.add_middleware(ServeGate, settings_provider=_gate_settings)
install_error_envelope(app)
install_request_logging(app)
app.include_router(briefing_data_router)
app.include_router(briefing_viewer_router)
app.include_router(artifacts_router)
app.include_router(artifact_links_router)
app.include_router(review_inbox_router)
app.include_router(documents_router)
app.include_router(due_router)
app.include_router(entities_router)
app.include_router(notes_router)
app.include_router(spending_router)
app.include_router(token_ui_router)
app.include_router(evaluations_router)
app.include_router(forecast_router)
app.include_router(transcription_router)
app.include_router(financial_router)
app.include_router(financial_corrections_router)
app.include_router(source_imports_router)
app.include_router(jyra_router)
app.include_router(reminders_router)
app.include_router(google_calendar_router)
app.include_router(google_callback_router)


@app.get("/ui/spending", include_in_schema=False, dependencies=[Depends(require_reader)])
def spending_page():
    """Read-only HTML document, safe behind loopback only."""
    from api.ui import static_page
    return static_page("spending.html", "/ui/spending")


@app.get("/ui/assets/highcharts.js", include_in_schema=False, dependencies=[Depends(require_reader)])
def highcharts_asset() -> FileResponse:
    """Vendored charting code; never load a third-party CDN from the page."""
    return FileResponse(
        REPO_ROOT / "api" / "static" / "vendor" / "highcharts.js",
        media_type="application/javascript",
    )


@app.get("/ui/assets/highcharts-accessibility.js", include_in_schema=False, dependencies=[Depends(require_reader)])
def highcharts_accessibility_asset() -> FileResponse:
    return FileResponse(
        REPO_ROOT / "api" / "static" / "vendor" / "highcharts-accessibility.js",
        media_type="application/javascript",
    )
# Mounted last so every route above still matches first. The MCP endpoint
# lives in this process rather than as a separate stdio server: one
# launchd job, one checkout, one version, one restart — see
# runbooks/decisions-log.md.
# The Mount serves "/mcp/...", the Route serves the bare "/mcp". Without
# the Route, Starlette's Mount regex misses the bare prefix and the outer
# router answers 307 to "/mcp/" — *before* the auth wrapper, so an
# unauthenticated request would be redirected rather than refused, and the
# obvious URL would only work for a client that follows redirects on POST.
app.mount("/mcp", _mcp_mount)


class _NormalizeMcpPath:
    """Make the bare "/mcp" reach the mount instead of a redirect.

    Starlette's Mount regex matches "/mcp/..." but not the bare prefix, so
    "/mcp" would fall through to the outer router's redirect_slashes and
    answer 307 — *before* the auth wrapper, meaning an unauthenticated
    request gets redirected rather than refused, and the obvious URL only
    works for a client that follows redirects on POST. Rewriting the path
    here, ahead of routing, makes both spellings behave identically.
    """

    def __init__(self, app) -> None:
        self._app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope.get("path") == "/mcp":
            scope = {**scope, "path": "/mcp/", "raw_path": b"/mcp/"}
        await self._app(scope, receive, send)


app.add_middleware(_NormalizeMcpPath)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request, exc: StarletteHTTPException) -> JSONResponse:
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": "http_error", "message": str(exc.detail)}},
    )


def _error_detail(error: dict) -> dict:
    """Render one Pydantic error as {field, message}.

    Deliberately drops Pydantic's `input` and `url` keys. `input` echoes the
    rejected value straight back to the caller, which for this API can be a
    mistyped account number or date of birth — CLAUDE.md's rule is that such
    values never get persisted or passed onward, and an error body is an
    easy way to leak them into logs and MCP transcripts. `url` is just a
    link to pydantic.dev's docs, which no client of this API needs.
    """
    # A malformed JSON body reports loc ("body", <character offset>) — an
    # offset into the raw payload, not a field path, so the generic
    # renderer below would turn it into a field named "1".
    if error.get("type") == "json_invalid":
        return {"field": "body", "message": error.get("msg", "Invalid value")}

    location = tuple(error.get("loc", ()))
    # Strip the leading "body" segment so the common case reads as "name"
    # rather than "body.name". "query"/"path" prefixes are kept: they tell a
    # client where to look, and a query param and a body field can share a
    # name. Nested attribute errors arrive already prefixed with
    # ("body", "attributes", ...) from validate_entity_attributes.
    if location and location[0] == "body":
        location = location[1:]
    return {
        "field": ".".join(str(part) for part in location) or "body",
        "message": error.get("msg", "Invalid value"),
    }


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(
    request, exc: RequestValidationError
) -> JSONResponse:
    """Wrap FastAPI's default {"detail": [...]} 422 in this API's envelope.

    Without this, every 422 in the API — request parsing and
    validate_entity_attributes alike — returns a differently-shaped body
    than every other error path. `details` is additive: `code` and `message`
    are exactly what the other handlers produce, so a client reading only
    those works unchanged across all error types.
    """
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "message": "Request validation failed",
                "details": [_error_detail(error) for error in exc.errors()],
            }
        },
    )


@app.exception_handler(ValidationError)
async def validation_error_handler(request, exc: ValidationError) -> JSONResponse:
    if exc.title != "Settings":
        raise exc
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "configuration_error",
                "message": "Server misconfigured — check .env / Q_CORE_* environment variables.",
            }
        },
    )


@app.exception_handler(NoTextExtractedError)
async def no_text_extracted_handler(request, exc: NoTextExtractedError) -> JSONResponse:
    """422 rather than 200-with-empty-text.

    A scanned statement extracts to nothing, and returning that as a success
    makes "nothing was read" indistinguishable from "nothing needed
    redacting" — the caller would conclude the document was clean.
    """
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "no_text_extracted", "message": str(exc)}},
    )


@app.exception_handler(PartialExtractionError)
async def partial_extraction_handler(
    request, exc: PartialExtractionError
) -> JSONResponse:
    """422, carrying the counts and withholding the text.

    The same status as no_text_extracted because it is the same kind of
    answer — the document could not be read in a way the caller can rely
    on — but a distinct code, because the remedies differ: a scan needs
    OCR, a partial needs someone to decide whether the missing page
    mattered.

    The extracted pages are not included. Returning them would hand over
    the text while calling it a refusal, which is the outcome
    redaction_incomplete already declines to produce.
    """
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "partial_extraction",
                "message": (
                    f"{str(exc)}; pass allow_partial=true to accept it, but never "
                    "for a bank statement — a partial statement imports as though "
                    "it were whole"
                ),
                "extracted_chars": exc.extracted_chars,
                "zero_pages": exc.zero_pages,
            }
        },
    )


@app.exception_handler(AttachmentTooLargeError)
async def attachment_too_large_handler(request, exc) -> JSONResponse:
    """413, and the message names the limit.

    "Too large" without the number leaves a caller guessing at what would
    fit, which for a model means retrying with something else large.
    """
    return JSONResponse(
        status_code=413,
        content={"error": {"code": "attachment_too_large", "message": str(exc)}},
    )


@app.exception_handler(AttachmentNotRedactedError)
async def attachment_not_redacted_handler(request, exc) -> JSONResponse:
    """422, and it says what to do instead.

    Attachments are stored verbatim, so there is no redacted form to fall
    back to -- the only answers are store-as-is or refuse. The message
    points at register_document, which extracts and scrubs, so the refusal
    is a redirection rather than a dead end.
    """
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "attachment_not_redacted", "message": str(exc)}},
    )


@app.exception_handler(UnsupportedDocumentError)
async def unsupported_document_handler(request, exc: UnsupportedDocumentError) -> JSONResponse:
    """422 with a fixed message: the format, never the content, is named."""
    return JSONResponse(status_code=422, content={"error": {"code": "unsupported_document_format", "message": str(exc)}})


@app.exception_handler(PersonalRedactionError)
async def personal_redaction_handler(request, exc: PersonalRedactionError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"error": {"code": "personal_redaction_required", "message": str(exc)}})


@app.exception_handler(RedactionIncompleteError)
async def redaction_incomplete_handler(
    request, exc: RedactionIncompleteError
) -> JSONResponse:
    """500, and deliberately says nothing about what survived.

    Reached only when the scrubber met a format it does not recognise. The
    text is withheld rather than returned — an error body is model context
    exactly like a successful one, so quoting the leak here would defeat the
    guard that produced this.
    """
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "redaction_incomplete",
                "message": (
                    "text could not be fully redacted, so it is being withheld; "
                    "see data/logs/api.log for the location"
                ),
            }
        },
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/", dependencies=[Depends(require_token)])
def root() -> dict:
    return {"name": "q-core API"}

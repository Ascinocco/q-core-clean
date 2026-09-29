"""HTTP client for the q-core API.

The single place that knows the API is reachable over HTTP at all. Tools
await `request` and let `ApiError` propagate; the MCP SDK turns a raised
exception into a tool error, so no tool builds an error response itself.

One client belongs to one event loop. `httpx.AsyncClient` keeps a
connection pool whose sockets are bound to the loop that opened them, so
reusing an instance across separate `anyio.run(...)` calls fails on the
second request with `RuntimeError: Event loop is closed`. This is not a
problem in production — `MCPServer.run()` serves every tool call on one
loop for the process's lifetime — but it does mean a script or test that
drives tools with one `anyio.run` per call must build a client per loop,
or do all its calls inside a single `anyio.run`. The in-process test
transports hide this entirely: they never open a socket, so a client
reused across loops appears to work.

Async rather than sync for two reasons. The MCP server runs on an event
loop, so a blocking HTTP call inside a tool holds it for the duration of
the request. And `httpx.ASGITransport` — the only way to exercise tools
against the real API app in-process, without binding a port — implements
`handle_async_request` only, so a sync client cannot use it at all.
"""

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from api.config import Settings

TIMEOUT_SECONDS = 10.0

START_COMMAND = "launchctl kickstart -k gui/$(id -u)/tech.q-core.api"

# api/db.py raises CorruptDatabaseError / IncompleteDatabaseError from
# inside the get_connection dependency, which runs before the app's
# exception handlers can see it. Starlette turns that into a plain-text
# "Internal Server Error" carrying none of the detail, so the only way to
# read the real message is the API's own output.
DATABASE_PROBLEM = (
    "The q-core API failed before it could answer — most likely the "
    "database needs attention (missing tables, or a file that is not a "
    "usable database). The real error is only in the API's own output: "
    "check `data/logs/api.log` (JSON lines — the newest ERROR entry), or "
    "run `python -m api.run` in a terminal to watch it happen. Port {port}."
)


class _NoContent:
    """The API answered successfully with no body.

    A singleton rather than None so a caller can tell "the API said there
    is nothing" from "a function forgot to return". Compares unequal to {}
    and [] for the same reason: every one of those is falsy, so `if
    result:` cannot distinguish them and identity is the only check that
    survives someone writing it that way.
    """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "NO_CONTENT"


NO_CONTENT = _NoContent()


class ApiError(ToolError):
    """An API call failed. The message is written to be read by a model.

    API envelopes expose their code as a leading `code: ` token, followed
    by the original message, field details and any recovery instructions.
    The SDK may prepend its own `Error executing tool <name>: ` context.

    Subclasses the SDK's `ToolError` deliberately, and this is load-bearing
    rather than cosmetic. The SDK treats `ToolError` as an *anticipated*
    failure and passes its message through to the model; any other
    exception is treated as a crash, and the model sees only
    "Error executing tool <name>" with the real message dropped. Raising a
    plain Exception here would silently discard every mapped message below
    — the 404 text, the per-field 422 lines, the "API not reachable"
    instructions — which is the entire reason this module exists.
    """


def _looks_like_json(response: httpx.Response) -> bool:
    return "json" in response.headers.get("content-type", "").lower()


def _format_error(response: httpx.Response, port: int) -> str:
    try:
        envelope = response.json()["error"]
        message = envelope["message"]
    except (ValueError, KeyError, TypeError):
        # Not this API's envelope. Content-type only gates which fallback
        # to use, never the happy path above — a body that parses as the
        # envelope is trusted however it was labelled.
        if not _looks_like_json(response) and response.status_code >= 500:
            return DATABASE_PROBLEM.format(port=port)
        body = response.text.strip()[:200]
        return f"API returned HTTP {response.status_code}: {body}"

    code = envelope.get("code")
    lines = [f"{code}: {message}" if isinstance(code, str) and code else message]
    for detail in envelope.get("details") or []:
        lines.append(f"{detail.get('field', '?')}: {detail.get('message', '')}")
    if envelope.get("code") == "unauthorized":
        lines.append(
            "The MCP server's Q_CORE_API_TOKEN does not match the running "
            "API's. Check .env and restart both."
        )
    return "\n".join(lines)


class QCoreClient:
    def __init__(
        self,
        settings: Settings,
        # AsyncBaseTransport, not BaseTransport: httpx.ASGITransport — the
        # transport the round-trip tests are built on — subclasses only the
        # async one, so the stricter annotation excluded the very thing this
        # parameter exists for. It went unnoticed because httpx.MockTransport
        # subclasses both. It is also what httpx.AsyncClient itself declares.
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # 127.0.0.1 is hard-coded, not configurable: the loopback bind is
        # part of the security model, not a deployment detail. See
        # runbooks/decisions-log.md.
        self._port = settings.port
        # Kept, not just read: attach_file needs the configured intake,
        # inbox and data directories to know which paths it must refuse,
        # and deriving them from a global would ignore the settings a
        # caller actually constructed this client with.
        self.settings = settings
        self.base_url = f"http://127.0.0.1:{settings.port}"
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {settings.api_token}"},
            timeout=TIMEOUT_SECONDS,
            transport=transport,
        )

    def _unreachable(self, reason: str) -> ApiError:
        return ApiError(
            f"q-core API is not reachable at {self.base_url} ({reason}). "
            f"Start it with: {START_COMMAND}"
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        files: dict | None = None,
    ) -> dict:
        try:
            response = await self._client.request(
                method, path, params=params, json=json, files=files
            )
        except httpx.ConnectError as exc:
            raise self._unreachable("connection refused") from exc
        except httpx.TimeoutException as exc:
            # Deliberately not retried: a retried POST after an ambiguous
            # timeout can double-write.
            raise self._unreachable(f"timed out after {TIMEOUT_SECONDS}s") from exc

        if response.is_success:
            # A 204 has no body, so response.json() raises JSONDecodeError
            # — and that is not a ToolError, so the SDK would drop the real
            # message and show only "Error executing tool <name>". This is
            # not an edge case: POST /tickets/claim answers 204 whenever the
            # queue is drained, which for a polling loop is the majority of
            # calls. Its steady state would have surfaced as a crash.
            #
            # Keyed on an empty body rather than the status alone, because a
            # 200 with no content fails identically and the guard is free.
            if not response.content:
                return NO_CONTENT
            return response.json()
        raise ApiError(_format_error(response, self._port))

    async def fetch_bytes(self, path: str) -> tuple[bytes, str]:
        """GET a non-JSON body: the bytes and the media type.

        `request` always parses JSON, which is right for every other route
        and wrong for attachment content -- a PNG is not a document with a
        JSON representation. Error handling is deliberately identical, so
        a 404 from this route reads the same as a 404 from any other
        rather than surfacing as a decode failure.
        """
        try:
            response = await self._client.request("GET", path)
        except httpx.ConnectError as exc:
            raise self._unreachable("connection refused") from exc
        except httpx.TimeoutException as exc:
            raise self._unreachable(f"timed out after {TIMEOUT_SECONDS}s") from exc

        if response.is_success:
            media_type = response.headers.get("content-type", "").split(";")[0]
            return response.content, media_type
        raise ApiError(_format_error(response, self._port))

    async def aclose(self) -> None:
        await self._client.aclose()

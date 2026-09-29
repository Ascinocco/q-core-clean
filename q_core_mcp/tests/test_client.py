import pathlib

import anyio
import httpx
import pytest

from q_core_mcp.client import ApiError, QCoreClient


def _client(test_settings, handler) -> QCoreClient:
    return QCoreClient(test_settings, transport=httpx.MockTransport(handler))


def _request(client: QCoreClient, *args, **kwargs):
    """Drive the async client from a sync test."""
    return anyio.run(lambda: client.request(*args, **kwargs))


def test_sends_bearer_token_and_returns_json(test_settings):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"id": "abc"})

    result = _request(_client(test_settings, handler), "GET", "/entities/abc")

    assert result == {"id": "abc"}
    assert seen["auth"] == "Bearer test-token"
    assert seen["url"] == "http://127.0.0.1:8420/entities/abc"


def test_passes_query_params_and_json_body(test_settings):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        seen["body"] = request.read().decode()
        return httpx.Response(200, json={})

    _request(
        _client(test_settings, handler),
        "POST",
        "/entities",
        params={"type": "pet"},
        json={"name": "Rex"},
    )

    assert seen["params"] == {"type": "pet"}
    assert "Rex" in seen["body"]


def test_not_found_raises_with_the_api_message(test_settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"error": {"code": "not_found", "message": "No entity with id 'x'"}},
        )

    with pytest.raises(ApiError) as exc_info:
        _request(_client(test_settings, handler), "GET", "/entities/x")

    assert "No entity with id 'x'" in str(exc_info.value)


def test_validation_error_renders_one_line_per_field(test_settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "error": {
                    "code": "validation_error",
                    "message": "Request validation failed",
                    "details": [
                        {"field": "name", "message": "Field required"},
                        {
                            "field": "attributes.purchase_date",
                            "message": "Input should be a valid date",
                        },
                    ],
                }
            },
        )

    with pytest.raises(ApiError) as exc_info:
        _request(_client(test_settings, handler), "POST", "/entities", json={})

    message = str(exc_info.value)
    assert "Request validation failed" in message
    assert "name: Field required" in message
    assert "attributes.purchase_date: Input should be a valid date" in message


def test_unauthorized_explains_the_token_mismatch(test_settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "error": {
                    "code": "unauthorized",
                    "message": "Missing or invalid bearer token",
                }
            },
        )

    with pytest.raises(ApiError) as exc_info:
        _request(_client(test_settings, handler), "GET", "/entities")

    assert "Q_CORE_API_TOKEN" in str(exc_info.value)


def test_connection_refused_names_the_address_and_the_fix(test_settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ApiError) as exc_info:
        _request(_client(test_settings, handler), "GET", "/entities")

    message = str(exc_info.value)
    assert "127.0.0.1:8420" in message
    assert "launchctl" in message


def test_plain_text_500_is_reported_as_a_database_problem(test_settings):
    # api/db.py's CorruptDatabaseError / IncompleteDatabaseError raise from
    # inside the get_connection dependency, before the app's exception
    # handlers can see them, so Starlette returns a plain-text
    # "Internal Server Error" with no envelope at all. Verified against the
    # real app, not assumed. The useful detail is only in the API's own
    # stderr, so the message has to send the reader there.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500, text="Internal Server Error", headers={"content-type": "text/plain"}
        )

    with pytest.raises(ApiError) as exc_info:
        _request(_client(test_settings, handler), "GET", "/entities")

    message = str(exc_info.value)
    assert "database" in message.lower()
    # Points the reader at both ways to see the real error: the log file
    # on disk, and the command to watch it happen live. Was
    # "uvicorn api.main:app" plus `data/api.log` until the observability
    # work moved the log to data/logs/api.log and added `python -m
    # api.run` (which applies the logging config; the bare uvicorn CLI
    # does not).
    assert "data/logs/api.log" in message
    assert "api.run" in message


def test_non_json_4xx_still_reports_the_status_and_body(test_settings):
    # A non-JSON 4xx is a different animal: not the database, more likely a
    # proxy or a wrong URL, so it must not claim a database problem.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404, text="<html>Not Found</html>", headers={"content-type": "text/html"}
        )

    with pytest.raises(ApiError) as exc_info:
        _request(_client(test_settings, handler), "GET", "/nope")

    message = str(exc_info.value)
    assert "404" in message
    assert "database" not in message.lower()


def test_json_envelope_is_used_even_when_content_type_is_sloppy(test_settings):
    # Content-type gates the *fallback*, not the happy path: a body that
    # parses as the envelope is used regardless of how it was labelled.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            content=b'{"error": {"code": "conflict", "message": "still referenced"}}',
            headers={"content-type": "text/plain"},
        )

    with pytest.raises(ApiError) as exc_info:
        _request(_client(test_settings, handler), "DELETE", "/entities/x")

    assert "still referenced" in str(exc_info.value)


def test_round_trip_against_the_real_api(test_settings):
    # The stubbed tests above pin what the client sends; this pins that the
    # real API accepts it and that the client reads a real response back.
    # httpx.ASGITransport is why this client is async at all — it
    # implements handle_async_request only, so a sync client cannot drive
    # the app in-process.
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        client = QCoreClient(
            test_settings, transport=httpx.ASGITransport(app=app)
        )
        created = _request(
            client, "POST", "/entities", json={"type": "pet", "name": "Rex"}
        )
        fetched = _request(client, "GET", f"/entities/{created['id']}")
    finally:
        app.dependency_overrides.clear()

    assert created["name"] == "Rex"
    assert fetched["id"] == created["id"]


def test_real_api_404_is_mapped_from_the_real_envelope(test_settings):
    # Pins the error mapping against the API's actual envelope rather than
    # a hand-written copy of it, so the two can't drift apart silently.
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        client = QCoreClient(
            test_settings, transport=httpx.ASGITransport(app=app)
        )
        with pytest.raises(ApiError) as exc_info:
            _request(client, "GET", "/entities/does-not-exist")
    finally:
        app.dependency_overrides.clear()

    assert "does-not-exist" in str(exc_info.value)


def test_real_api_422_details_render_as_field_lines(test_settings):
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        client = QCoreClient(
            test_settings, transport=httpx.ASGITransport(app=app)
        )
        with pytest.raises(ApiError) as exc_info:
            _request(
                client,
                "POST",
                "/entities",
                json={
                    "type": "property",
                    "name": "Lake House",
                    "attributes": {"purchase_date": "not-a-date"},
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert "attributes.purchase_date:" in str(exc_info.value)


def _corrupt_db_settings(test_settings):
    db = pathlib.Path(test_settings.db_path)
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_text("this is not a sqlite database")
    return test_settings


def test_real_database_failure_maps_to_the_database_message(test_settings):
    # The end-to-end version of test_plain_text_500_...: a genuinely
    # unusable database, through the real app, mapped by the real client.
    # raise_app_exceptions=False is required for the transport to behave
    # like a real server here — see the test below for why.
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: _corrupt_db_settings(
        test_settings
    )
    try:
        client = QCoreClient(
            test_settings,
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        )
        with pytest.raises(ApiError) as exc_info:
            _request(client, "GET", "/entities")
    finally:
        app.dependency_overrides.clear()

    message = str(exc_info.value)
    assert "database" in message.lower()
    # Points the reader at both ways to see the real error: the log file
    # on disk, and the command to watch it happen live. Was
    # "uvicorn api.main:app" plus `data/api.log` until the observability
    # work moved the log to data/logs/api.log and added `python -m
    # api.run` (which applies the logging config; the bare uvicorn CLI
    # does not).
    assert "data/logs/api.log" in message
    assert "api.run" in message


def test_a_database_failure_no_longer_escapes_as_a_raw_exception(test_settings):
    """The app converts its own unhandled exceptions, so this round trip
    needs no transport flag.

    This test used to assert the opposite. httpx.ASGITransport defaults to
    raise_app_exceptions=True, and before a todo's catch-all the app let
    DatabaseNotUsableError escape, so a round-trip test that forgot the
    flag saw a raw exception instead of the 500 a real server would send --
    a genuine hazard worth documenting at the time.

    That hazard is gone for anything the app now handles: the envelope is
    produced inside the app, so both settings of the flag return the same
    response a real server does. Kept rather than deleted because the
    equivalence is the useful fact -- if a future change lets exceptions
    escape the middleware again, the two halves below stop agreeing.
    """
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: _corrupt_db_settings(
        test_settings
    )
    try:
        for raise_app_exceptions in (True, False):
            client = QCoreClient(
                test_settings,
                transport=httpx.ASGITransport(
                    app=app, raise_app_exceptions=raise_app_exceptions
                ),
            )
            with pytest.raises(ApiError) as exc_info:
                _request(client, "GET", "/entities")
            assert "database" in str(exc_info.value).lower()
    finally:
        app.dependency_overrides.clear()


# --- 204 and multipart (Jyra MCP plan, Task 1) -------------------------


def test_request_returns_no_content_on_204(test_settings):
    """POST /tickets/claim answers 204 when the queue is drained — the
    NORMAL, majority response for a polling loop, not an edge case.

    request() called response.json() on any success, so an empty body
    raised JSONDecodeError. That is not a ToolError, so per this module's
    own docstring the SDK shows the model only "Error executing tool
    claim_ticket" with the real message dropped: the agent loop's steady
    state surfacing as an opaque crash.
    """
    from q_core_mcp.client import NO_CONTENT

    client = _client(test_settings, lambda request: httpx.Response(204))

    assert _request(client, "POST", "/tickets/claim", json={}) is NO_CONTENT


def test_no_content_is_distinguishable_from_an_empty_dict():
    """A caller must be able to tell "the API said there is nothing" from
    "a dict that happens to be empty". Both are falsy, so identity is the
    check that survives someone writing `if result:`."""
    from q_core_mcp.client import NO_CONTENT

    assert NO_CONTENT is not None
    assert NO_CONTENT != {}
    assert NO_CONTENT != []


def test_an_empty_body_on_a_200_is_also_no_content(test_settings):
    """Same failure, different status. Costs nothing to cover, and means
    the guard is about the body rather than one magic number."""
    from q_core_mcp.client import NO_CONTENT

    client = _client(test_settings, lambda request: httpx.Response(200, content=b""))

    assert _request(client, "GET", "/anything") is NO_CONTENT


def test_request_sends_multipart_when_files_are_given(test_settings):
    """attach_file needs UploadFile-shaped multipart; request() took only
    params and json, so there was no way to send a file at all."""
    seen = {}

    def handler(request):
        seen["content_type"] = request.headers.get("content-type", "")
        seen["body"] = request.content
        return httpx.Response(200, json={"ok": True})

    client = _client(test_settings, handler)
    result = _request(
        client,
        "POST",
        "/tickets/t1/attachments",
        files={"upload": ("shot.png", b"bytes", "image/png")},
    )

    assert result == {"ok": True}
    assert seen["content_type"].startswith("multipart/form-data")
    assert b"shot.png" in seen["body"]
    assert b"bytes" in seen["body"]


def test_multipart_requests_still_carry_the_bearer_token(test_settings):
    """The auth header is added per-client, not per-call, but a new code
    path is exactly where that assumption gets broken."""
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={})

    client = _client(test_settings, handler)
    _request(
        client, "POST", "/x", files={"upload": ("a.txt", b"a", "text/plain")}
    )

    assert seen["auth"] == f"Bearer {test_settings.api_token}"


def test_round_trip_claim_on_an_empty_queue_returns_no_content(test_settings):
    """The 204 path against the REAL app.

    A MockTransport cannot prove this: it returns whatever its handler
    says, so it would pass against a client that never asked the API
    anything. Only the real route can show that /tickets/claim actually
    answers 204 when nothing is claimable.
    """
    from api.config import get_settings
    from api.main import app
    from q_core_mcp.client import NO_CONTENT

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        client = QCoreClient(test_settings, transport=httpx.ASGITransport(app=app))
        # No tickets exist at all, so nothing is agent_ready.
        result = _request(client, "POST", "/tickets/claim", json={"actor": "agent-1"})
    finally:
        app.dependency_overrides.clear()

    assert result is NO_CONTENT


def test_round_trip_attachment_upload_through_multipart(test_settings):
    """Multipart against the real route, which is the only thing that
    proves the encoding is one FastAPI's UploadFile accepts."""
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        client = QCoreClient(test_settings, transport=httpx.ASGITransport(app=app))
        entity = _request(
            client, "POST", "/entities", json={"type": "project", "name": "p"}
        )
        board = _request(
            client, "POST", "/boards", json={"entity_id": entity["id"], "title": "B"}
        )
        ticket = _request(
            client,
            "POST",
            "/tickets",
            json={
                "board_id": board["id"],
                "type": "task",
                "title": "t",
                "actor": "owner",
            },
        )
        attachment = _request(
            client,
            "POST",
            f"/tickets/{ticket['id']}/attachments",
            files={"upload": ("shot.png", b"screenshot-bytes", "image/png")},
        )
    finally:
        app.dependency_overrides.clear()

    assert attachment["filename"] == "shot.png"
    assert "file_path" not in attachment, "content is reached by id (ticket T-44)"
    # Where it landed is still asserted, but read the way the SERVER reads
    # it — from the row — because the response deliberately stopped
    # publishing it.
    import sqlite3

    connection = sqlite3.connect(test_settings.db_path)
    try:
        stored = pathlib.Path(
            connection.execute(
                "SELECT file_path FROM ticket_attachments WHERE id = ?",
                (attachment["id"],),
            ).fetchone()[0]
        )
    finally:
        connection.close()
    assert stored.read_bytes() == b"screenshot-bytes"
    assert stored.is_relative_to(pathlib.Path(test_settings.jyra_dir))


def test_test_settings_never_points_at_the_real_data_directory(
    test_settings, tmp_path
):
    """Every writable path the fixture hands out must be under tmp_path.

    Omitting one means the whole mcp suite writes into the user's real
    data/ — which is exactly what jyra_dir did until this task, because
    Settings supplies a repo-relative default for anything not overridden.
    Asserted by iterating the writable fields rather than naming the two we
    happened to notice, so a field added to Settings later is covered too.
    """
    from api.config import REPO_ROOT

    # intake_dir is not written to, but is redirected all the same: it
    # points at real bank statements, and a test that read one would be
    # the exact exposure the extract endpoint exists to prevent.
    writable = ("db_path", "documents_dir", "jyra_dir", "logs_dir", "intake_dir", "inbox_dir", "secrets_dir")
    for field in writable:
        value = pathlib.Path(getattr(test_settings, field))
        assert value.is_relative_to(tmp_path), f"{field} escapes tmp_path: {value}"
        assert not value.is_relative_to(REPO_ROOT / "data"), field


def test_every_writable_settings_path_is_covered_by_the_guard_above(test_settings):
    """Keeps that guard honest as Settings grows.

    A new writable path added to Settings would not be caught by a
    hand-listed tuple, so this fails if one appears — the same
    hand-maintained-list problem EXPECTED_TABLES needed a test for.
    """
    from api.config import Settings

    known = {
        # writable, must be redirected in tests
        "db_path",
        "documents_dir",
        "jyra_dir",
        "logs_dir",
        "intake_dir",
        "inbox_dir",
        "secrets_dir",  # the Google refresh token on Linux
        # a read-only SOURCE allow-list: attach_file only reads from it,
        # nothing writes there, and its default is the working tree by
        # design. The MCP fixture still redirects it to tmp_path, because
        # a test that attaches a file needs its own tmp_path allowed.
        "attachment_roots",
        # refusal boundaries for attach_file; nothing writes by these names
        "data_dir",
        "environment_file",
        # read-only inputs, safe to point at the real repo
        "transcription_ffmpeg",  # executable input, never a writable data destination
        "schema_path",
        "seed_categories_path",
        "migrations_dir",
        "privacy_profile_path",  # read-only, redirected to synthetic test data
        "eval_summaries_dir",  # read-only published summaries; redirected in fixtures
        "forecast_dir",  # private plan history, redirected in fixtures
        # not paths
        "api_token",
        "port",
        "log_level",
        "google_oauth_client_id",
        "google_oauth_client_secret",
        "timezone",
        "transcription_enabled",
        "transcription_token",
        "whisper_port",
        "serve_socket",  # a runtime socket, None in tests
        "serve_hostname",
        "ui_allowed_logins",
        "google_token_store",
    }
    assert set(Settings.model_fields) <= known, (
        "new Settings field(s) "
        f"{sorted(set(Settings.model_fields) - known)} — decide whether each is "
        "writable, and if so redirect it in the test_settings fixture"
    )


@pytest.mark.parametrize('method', ['request', 'fetch_bytes'])
@pytest.mark.parametrize('code', ['not_found', 'invalid_reference', 'conflict', 'validation_error', 'unauthorized', 'internal_error'])
def test_error_envelope_code_preserved_for_json_and_binary_requests(test_settings, method, code):
    # Code comes from the envelope, not a status-to-code lookup.
    client = _client(test_settings, lambda request: httpx.Response(
        400, json={'error': {'code': code, 'message': 'Example failure'}},
    ))
    async def run():
        try:
            if method == 'request':
                await client.request('GET', '/example')
            else:
                await client.fetch_bytes('/example')
        finally:
            await client.aclose()
    with pytest.raises(ApiError) as exc:
        anyio.run(run)
    assert str(exc.value).startswith(f'{code}: Example failure')


def test_envelope_without_code_preserves_message(test_settings):
    client = _client(test_settings, lambda request: httpx.Response(
        400, json={'error': {'message': 'Legacy failure'}},
    ))
    with pytest.raises(ApiError, match='^Legacy failure$'):
        _request(client, 'GET', '/example')

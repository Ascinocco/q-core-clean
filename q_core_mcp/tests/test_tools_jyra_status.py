"""transition_ticket and claim_ticket — the two status tools.

Together because they are the status pair: one is how a human moves a
ticket, the other is how the agent loop does. Each of the four behaviours
in the plan's Global Constraints gets a description test AND a behaviour
test, because a description-content test alone rots into a comment nobody
verified.
"""

import httpx
import pytest

from mcp.server.mcpserver.exceptions import ToolError

from q_core_mcp.client import ApiError, QCoreClient
from q_core_mcp.server import build_server


def _server(test_settings, handler):
    return build_server(
        QCoreClient(test_settings, transport=httpx.MockTransport(handler))
    )


def _real_server(test_settings, app):
    return build_server(
        QCoreClient(test_settings, transport=httpx.ASGITransport(app=app))
    )


def _seed(test_settings, app, moves=()):
    """Project -> board -> ticket, through the API directly. See the note in
    test_tools_jyra_reads.py: the tools under test here are the status
    tools, so the setup may be raw requests."""
    import anyio

    client = QCoreClient(test_settings, transport=httpx.ASGITransport(app=app))

    async def go():
        entity = await client.request(
            "POST", "/entities", json={"type": "project", "name": "p"}
        )
        board = await client.request(
            "POST", "/boards", json={"entity_id": entity["id"], "title": "B"}
        )
        ticket = await client.request(
            "POST",
            "/tickets",
            json={
                "board_id": board["id"],
                "type": "task",
                "title": "t",
                "actor": "owner",
            },
        )
        for status in moves:
            await client.request(
                "POST",
                f"/tickets/{ticket['id']}/transition",
                json={"to_status": status, "actor": "owner", "note": "n"},
            )
        return ticket

    return anyio.run(go)


# --- paths and params --------------------------------------------------


def test_transition_ticket_posts_to_the_transition_route(test_settings, call_tool):
    seen = {}

    def handler(request):
        import json as jsonlib

        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = jsonlib.loads(request.content)
        return httpx.Response(200, json={"id": "t1", "status": "review"})

    call_tool(
        _server(test_settings, handler),
        "transition_ticket",
        {
            "ticket_id": "t1",
            "to_status": "review",
            "actor": "owner",
            "note": "done with it",
        },
    )

    assert (seen["method"], seen["path"]) == ("POST", "/tickets/t1/transition")
    assert seen["body"] == {
        "to_status": "review",
        "actor": "owner",
        "note": "done with it",
    }


def test_claim_ticket_posts_to_the_claim_route(test_settings, call_tool):
    seen = {}

    def handler(request):
        import json as jsonlib

        seen["path"] = request.url.path
        seen["body"] = jsonlib.loads(request.content)
        return httpx.Response(200, json={"id": "t1", "status": "agent_coding"})

    call_tool(
        _server(test_settings, handler), "claim_ticket", {"actor": "agent-1"}
    )

    assert seen["path"] == "/tickets/claim"
    assert seen["body"] == {"actor": "agent-1"}


def test_claim_ticket_scopes_to_a_board_only_when_given(test_settings, call_tool):
    seen = {}

    def handler(request):
        import json as jsonlib

        seen["body"] = jsonlib.loads(request.content)
        return httpx.Response(200, json={"id": "t1"})

    call_tool(
        _server(test_settings, handler),
        "claim_ticket",
        {"actor": "agent-1", "board_id": "b1"},
    )
    assert seen["body"] == {"actor": "agent-1", "board_id": "b1"}


# --- the claim's empty result ------------------------------------------


def test_claim_ticket_reports_an_empty_queue_as_success(test_settings, call_tool):
    """204 is the loop's steady state, not an error.

    A tool that raised here — or returned a bare null a model reads as
    failure — would make an idle queue look like something to retry or
    report. The result has to say "nothing to do" in a way that cannot be
    mistaken for a fault.
    """
    result = call_tool(
        _server(test_settings, lambda request: httpx.Response(204)),
        "claim_ticket",
        {"actor": "agent-1"},
    )

    assert result["claimed"] is False
    assert "ticket" not in result or result["ticket"] is None
    assert result["reason"]


def test_claim_ticket_reports_a_win_distinguishably(test_settings, call_tool):
    result = call_tool(
        _server(
            test_settings,
            lambda request: httpx.Response(
                200, json={"id": "t1", "status": "agent_coding"}
            ),
        ),
        "claim_ticket",
        {"actor": "agent-1"},
    )

    assert result["claimed"] is True
    assert result["ticket"]["id"] == "t1"


# --- descriptions ------------------------------------------------------


def test_transition_description_says_the_note_is_required_and_why(
    test_settings, tools_by_name
):
    tools = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))
    description = tools["transition_ticket"].description.lower()
    assert "note" in description
    assert "required" in description or "must" in description
    # The reason, not just the rule — a model that knows why writes a
    # better note than one told to fill a field.
    assert "history" in description or "audit" in description


def test_transition_description_explains_blocked(test_settings, tools_by_name):
    tools = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))
    description = tools["transition_ticket"].description.lower()
    assert "blocked" in description
    assert "claim" in description


def test_claim_description_reads_as_the_agent_loop_entry_point(
    test_settings, tools_by_name
):
    tools = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))
    description = tools["claim_ticket"].description.lower()
    assert "agent_ready" in description
    # Empty is success, and the description must say so — it is the
    # majority response for a polling loop.
    assert "empty" in description or "nothing" in description
    assert "claimed" in description


def test_status_tools_are_not_annotated_read_only(test_settings, tools_by_name):
    """Both mutate. claim_ticket especially: its name sounds like a read,
    and it takes a ticket."""
    tools = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))
    for name in ("transition_ticket", "claim_ticket"):
        annotations = tools[name].annotations
        assert not (annotations and annotations.read_only_hint), name


# --- round trips -------------------------------------------------------


def test_round_trip_claim_on_an_empty_queue_reads_as_success(
    test_settings, call_tool
):
    """"Empty means success" proven against a real 204, not asserted in
    prose. If this ever raises, the agent loop's steady state is an error
    path and the loop will report idleness as failure."""
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        _seed(test_settings, app)  # a ticket exists, but it is in backlog
        result = call_tool(
            _real_server(test_settings, app), "claim_ticket", {"actor": "agent-1"}
        )
    finally:
        app.dependency_overrides.clear()

    assert result["claimed"] is False


def test_round_trip_claim_takes_an_agent_ready_ticket(test_settings, call_tool):
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        ticket = _seed(test_settings, app, ["agent_ready"])
        result = call_tool(
            _real_server(test_settings, app), "claim_ticket", {"actor": "agent-1"}
        )
    finally:
        app.dependency_overrides.clear()

    assert result["claimed"] is True
    assert result["ticket"]["id"] == ticket["id"]
    assert result["ticket"]["status"] == "agent_coding"
    assert result["ticket"]["claimed_by"] == "agent-1"


def test_round_trip_blocked_ticket_is_not_claimable(test_settings, call_tool):
    """The behaviour half of the blocked description test.

    This is why a failed ticket goes to blocked rather than back to
    agent_ready: a loop would pick it up again and fail identically,
    forever.
    """
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        _seed(test_settings, app, ["agent_ready", "blocked"])
        result = call_tool(
            _real_server(test_settings, app), "claim_ticket", {"actor": "agent-1"}
        )
    finally:
        app.dependency_overrides.clear()

    assert result["claimed"] is False


def test_round_trip_an_omitted_note_is_rejected_by_the_api(test_settings, call_tool):
    """The note is required at the API boundary, not only by the tool
    schema — so the guarantee survives a caller that is not this tool."""
    import anyio

    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        ticket = _seed(test_settings, app)
        client = QCoreClient(test_settings, transport=httpx.ASGITransport(app=app))

        async def go():
            return await client.request(
                "POST",
                f"/tickets/{ticket['id']}/transition",
                json={"to_status": "review", "actor": "owner"},
            )

        with pytest.raises(ApiError) as excinfo:
            anyio.run(go)
    finally:
        app.dependency_overrides.clear()

    assert "note" in str(excinfo.value).lower()


def test_an_empty_note_is_rejected(test_settings):
    """Was test_an_empty_note_is_currently_accepted, documenting a gap.

    The note used to be required in PRESENCE only: note="" returned 200 and
    wrote a history row that explained nothing. That test was written to go
    red when the API was tightened, so whoever tightened it would come back
    and strengthen this tool's description too — which is what happened
    (a todo).
    """
    import anyio

    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        ticket = _seed(test_settings, app)
        client = QCoreClient(test_settings, transport=httpx.ASGITransport(app=app))

        async def go():
            return await client.request(
                "POST",
                f"/tickets/{ticket['id']}/transition",
                json={"to_status": "review", "actor": "owner", "note": "   "},
            )

        with pytest.raises(ApiError) as excinfo:
            anyio.run(go)
    finally:
        app.dependency_overrides.clear()

    assert "note" in str(excinfo.value).lower()


def test_note_is_a_required_tool_parameter(test_settings, tools_by_name):
    """Stronger than the API's own 422: the tool schema itself makes a
    noteless transition unexpressible, so the audit trail cannot be skipped
    even by a model that ignores the description."""
    tools = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))
    schema = tools["transition_ticket"].input_schema
    assert "note" in schema.get("required", []), schema.get("required")


def test_round_trip_epic_cannot_enter_the_agent_lane(test_settings, call_tool):
    """Per-type status rules surface as an ApiError carrying the API's own
    message, not as a silent no-op."""
    import anyio

    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        client = QCoreClient(test_settings, transport=httpx.ASGITransport(app=app))

        async def make_epic():
            entity = await client.request(
                "POST", "/entities", json={"type": "project", "name": "p"}
            )
            board = await client.request(
                "POST", "/boards", json={"entity_id": entity["id"], "title": "B"}
            )
            return await client.request(
                "POST",
                "/tickets",
                json={
                    "board_id": board["id"],
                    "type": "epic",
                    "title": "E",
                    "actor": "owner",
                },
            )

        epic = anyio.run(make_epic)
        # ToolError, not ApiError: ApiError subclasses it, but the SDK
        # re-wraps whatever a tool raises at the call_tool boundary, so the
        # exception a caller sees is a plain ToolError carrying the message.
        # Tests that drive client.request directly see ApiError; tests that
        # go through a tool see ToolError.
        with pytest.raises(ToolError) as excinfo:
            call_tool(
                _real_server(test_settings, app),
                "transition_ticket",
                {
                    "ticket_id": epic["id"],
                    "to_status": "agent_ready",
                    "actor": "owner",
                    "note": "n",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert "agent_ready" in str(excinfo.value)


def test_transition_ticket_forwards_optional_history_precondition(test_settings, call_tool):
    import json
    expected = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
    seen = {}
    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={'id': 't1', 'status': 'review'})
    call_tool(_server(test_settings, handler), 'transition_ticket', {
        'ticket_id': 't1', 'to_status': 'review', 'actor': 'worker',
        'note': 'Checked current history', 'expected_transition_id': expected,
    })
    assert seen['expected_transition_id'] == expected

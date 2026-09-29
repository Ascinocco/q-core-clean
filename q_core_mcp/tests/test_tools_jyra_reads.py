"""Jyra ticket reads: get_ticket, list_tickets, get_ticket_history."""

import httpx

from q_core_mcp.client import QCoreClient
from q_core_mcp.server import build_server


def _server(test_settings, handler):
    client = QCoreClient(test_settings, transport=httpx.MockTransport(handler))
    return build_server(client)


def _recording(payload=None):
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=payload if payload is not None else {})

    return seen, handler


def _real_server(test_settings, app):
    return build_server(
        QCoreClient(test_settings, transport=httpx.ASGITransport(app=app))
    )


def _seed(test_settings, app, moves=()):
    """Create a project, board and ticket through the API directly, and run
    `moves` as transitions.

    Deliberately not through the write tools: those arrive in later tasks of
    this plan, and a read tool's test that depended on them could not run
    until they existed. The thing under test here is the read tool, so the
    setup is allowed to be plain HTTP.
    """
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


# --- vocabulary --------------------------------------------------------


def test_ticket_vocabulary_matches_the_api():
    """These strings go into tool descriptions, so a model reads them as the
    authoritative list of what it may send.

    Pinned against api.models rather than trusted: ENTITY_TYPES in
    tools/entities.py silently went stale the moment Jyra added `project` to
    the API's vocabulary, with nothing to catch it. Same
    hand-maintained-list problem EXPECTED_TABLES and ALL_STATUS_ORDER needed
    guards for inside the API.
    """
    from typing import get_args

    from api.models import TicketStatus, TicketType
    from q_core_mcp.tools.jyra import TICKET_STATUSES, TICKET_TYPES

    assert TICKET_TYPES == ", ".join(get_args(TicketType))
    assert TICKET_STATUSES == ", ".join(get_args(TicketStatus))


# --- paths and params --------------------------------------------------


def test_get_ticket_requests_the_right_path(test_settings, call_tool):
    seen, handler = _recording({"id": "t1"})
    call_tool(_server(test_settings, handler), "get_ticket", {"ticket_id": "t1"})
    assert (seen["method"], seen["path"]) == ("GET", "/tickets/t1")


def test_list_tickets_passes_only_the_filters_given(test_settings, call_tool):
    seen, handler = _recording({"items": [], "total": 0})
    call_tool(
        _server(test_settings, handler),
        "list_tickets",
        {"board_id": "b1", "status": "agent_ready"},
    )
    assert seen["path"] == "/tickets"
    assert seen["params"]["board_id"] == "b1"
    assert seen["params"]["status"] == "agent_ready"
    # Omitted rather than sent empty: type and parent_id are compared
    # literally in SQL, so an empty string filters to nothing rather than
    # being ignored — a silently wrong answer, not an error.
    assert "type" not in seen["params"]
    assert "parent_id" not in seen["params"]
    assert "claimed_before" not in seen["params"]


def test_list_tickets_passes_claimed_before_when_given(test_settings, call_tool):
    seen, handler = _recording({"items": [], "total": 0})
    call_tool(
        _server(test_settings, handler),
        "list_tickets",
        {"status": "agent_coding", "claimed_before": "2026-01-01"},
    )
    assert seen["params"]["claimed_before"] == "2026-01-01"


def test_get_ticket_history_requests_the_transitions_path(test_settings, call_tool):
    seen, handler = _recording({"items": [], "total": 0})
    call_tool(
        _server(test_settings, handler), "get_ticket_history", {"ticket_id": "t1"}
    )
    assert (seen["method"], seen["path"]) == ("GET", "/tickets/t1/transitions")


# --- annotations and descriptions --------------------------------------


def test_jyra_reads_are_annotated_read_only(test_settings, tools_by_name):
    seen, handler = _recording()
    tools = tools_by_name(_server(test_settings, handler))
    for name in (
        "get_ticket",
        "list_tickets",
        "get_ticket_history",
    ):
        annotations = tools[name].annotations
        assert annotations and annotations.read_only_hint, name
        assert not (annotations and annotations.destructive_hint), name


def test_list_tickets_description_explains_claimed_before(
    test_settings, tools_by_name
):
    """claimed_before is otherwise an inscrutable filter. It is the
    stale-claim recovery query — the way to find tickets stranded in
    agent_coding because the agent holding them died."""
    seen, handler = _recording()
    description = tools_by_name(_server(test_settings, handler))[
        "list_tickets"
    ].description.lower()
    assert "claimed_before" in description
    assert "stale" in description or "died" in description or "stuck" in description


def test_get_ticket_history_description_says_it_is_complete_and_ordered(
    test_settings, tools_by_name
):
    """What makes the history worth reading from is that it cannot have
    gaps, and that it is oldest-first."""
    seen, handler = _recording()
    description = tools_by_name(_server(test_settings, handler))[
        "get_ticket_history"
    ].description.lower()
    assert "oldest" in description
    assert "every" in description or "complete" in description


# --- round trips against the real API ----------------------------------


def test_round_trip_history_is_complete_and_oldest_first(test_settings, call_tool):
    """The behaviour half of the description test above.

    This already regressed once inside the API: ordering by (created_at, id)
    scrambled same-second events, because id is a random uuid4. Seven moves
    rather than two, so a wrong order is caught reliably instead of one time
    in six.
    """
    from api.config import get_settings
    from api.main import app

    # "complete" in the name is load-bearing, so: the single capped read
    # below is safe because this fixture is the literal list under it --
    # eight transitions including the create. Against a real ticket the
    # same call would need draining, since an audit log grows without
    # bound and one page of it looks exactly like all of it.
    moves = [
        "in_progress",
        "agent_ready",
        "agent_coding",
        "review",
        "blocked",
        "in_progress",
        "done",
    ]
    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        ticket = _seed(test_settings, app, moves)
        history = call_tool(
            _real_server(test_settings, app),
            "get_ticket_history",
            {"ticket_id": ticket["id"], "limit": 200},
        )
    finally:
        app.dependency_overrides.clear()

    assert [e["to_status"] for e in history["items"]] == ["backlog", *moves]
    for earlier, later in zip(history["items"], history["items"][1:]):
        assert later["from_status"] == earlier["to_status"]


def test_round_trip_list_tickets_finds_a_stale_claim(test_settings, call_tool):
    """claimed_before against the real route — the recovery path the
    description points at. A ticket stranded in agent_coding by a dead agent
    is invisible without it."""
    import sqlite3

    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        ticket = _seed(test_settings, app, ["agent_ready", "agent_coding"])
        connection = sqlite3.connect(test_settings.db_path)
        try:
            connection.execute(
                "UPDATE tickets SET claimed_at = '2000-01-01 00:00:00' WHERE id = ?",
                (ticket["id"],),
            )
            connection.commit()
        finally:
            connection.close()

        server = _real_server(test_settings, app)
        stale = call_tool(
            server,
            "list_tickets",
            {"status": "agent_coding", "claimed_before": "2020-01-01"},
        )
        fresh = call_tool(
            server,
            "list_tickets",
            {"status": "agent_coding", "claimed_before": "1999-01-01"},
        )
    finally:
        app.dependency_overrides.clear()

    assert [item["id"] for item in stale["items"]] == [ticket["id"]]
    assert fresh["total"] == 0


def test_round_trip_get_ticket_returns_a_real_row(test_settings, call_tool):
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        ticket = _seed(test_settings, app)
        fetched = call_tool(
            _real_server(test_settings, app), "get_ticket", {"ticket_id": ticket["id"]}
        )
    finally:
        app.dependency_overrides.clear()

    assert fetched["id"] == ticket["id"]
    assert fetched["status"] == "backlog"

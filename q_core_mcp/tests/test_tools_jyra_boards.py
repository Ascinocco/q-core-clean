"""Jyra board tools: get_board, list_boards, create_board."""

import httpx

from q_core_mcp.client import QCoreClient
from q_core_mcp.server import build_server

ALL_COLUMNS = [
    "backlog",
    "in_progress",
    "agent_ready",
    "agent_coding",
    "review",
    "blocked",
    "done",
]


def _server(test_settings, handler):
    return build_server(
        QCoreClient(test_settings, transport=httpx.MockTransport(handler))
    )


def _real_server(test_settings, app):
    return build_server(
        QCoreClient(test_settings, transport=httpx.ASGITransport(app=app))
    )


def _recording(payload=None):
    seen = {}

    def handler(request):
        import json as jsonlib

        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        if request.content:
            seen["body"] = jsonlib.loads(request.content)
        return httpx.Response(200, json=payload if payload is not None else {})

    return seen, handler


# --- paths, params, bodies ---------------------------------------------


def test_get_board_requests_the_board_path(test_settings, call_tool):
    seen, handler = _recording({"id": "b1", "columns": {}})
    call_tool(_server(test_settings, handler), "get_board", {"board_id": "b1"})
    assert (seen["method"], seen["path"]) == ("GET", "/boards/b1")


def test_list_boards_filters_by_entity_only_when_given(test_settings, call_tool):
    seen, handler = _recording({"items": [], "total": 0})
    server = _server(test_settings, handler)

    call_tool(server, "list_boards", {})
    assert "entity_id" not in seen["params"]

    call_tool(server, "list_boards", {"entity_id": "e1"})
    assert seen["params"]["entity_id"] == "e1"


def test_create_board_posts_entity_and_title(test_settings, call_tool):
    seen, handler = _recording({"id": "b1"})
    call_tool(
        _server(test_settings, handler),
        "create_board",
        {"entity_id": "e1", "title": "Delivery"},
    )
    assert (seen["method"], seen["path"]) == ("POST", "/boards")
    assert seen["body"] == {"entity_id": "e1", "title": "Delivery"}


# --- annotations and descriptions --------------------------------------


def test_board_reads_are_read_only_and_create_is_not(test_settings, tools_by_name):
    seen, handler = _recording()
    tools = tools_by_name(_server(test_settings, handler))
    for name in ("get_board", "list_boards"):
        annotations = tools[name].annotations
        assert annotations and annotations.read_only_hint, name
    create = tools["create_board"].annotations
    assert not (create and create.read_only_hint)
    assert not (create and create.destructive_hint)


def test_create_board_description_says_any_entity_can_own_one(
    test_settings, tools_by_name
):
    """A board hangs off any entity, not only a project — that is the point
    of project-being-an-entity-type, and a model that assumes otherwise
    will not offer a maintenance backlog on a vehicle."""
    seen, handler = _recording()
    description = tools_by_name(_server(test_settings, handler))[
        "create_board"
    ].description.lower()
    assert "project" in description
    assert "vehicle" in description or "property" in description


def test_get_board_description_says_every_column_is_present(
    test_settings, tools_by_name
):
    seen, handler = _recording()
    description = tools_by_name(_server(test_settings, handler))[
        "get_board"
    ].description.lower()
    assert "seven" in description or "all seven" in description
    assert "empty" in description


# --- round trips -------------------------------------------------------


def test_round_trip_board_view_has_all_seven_columns_even_when_empty(
    test_settings, call_tool
):
    """The shape a client renders from. Every column present even when
    empty is what lets a board be drawn without special-casing, and it is
    cheap to let regress — an implementation that omitted empty keys would
    look fine in any test that only checked the populated ones.

    Asserts the ORDER too: it is fixed left-to-right workflow order, so a
    client does not have to know the vocabulary to draw a sensible board.
    """
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        entity = call_tool(
            server, "create_entity", {"entity_type": "project", "name": "proj"}
        )
        board = call_tool(
            server, "create_board", {"entity_id": entity["id"], "title": "Delivery"}
        )
        view = call_tool(server, "get_board", {"board_id": board["id"]})
    finally:
        app.dependency_overrides.clear()

    assert list(view["columns"]) == ALL_COLUMNS
    assert all(
        column == {"items": [], "total": 0} for column in view["columns"].values()
    )
    assert view["entity"]["name"] == "proj"
    assert view["entity"]["type"] == "project"


def test_round_trip_a_board_can_hang_off_a_vehicle(test_settings, call_tool):
    """Not a project-only feature. The design says a maintenance backlog on
    a vehicle falls out for free because boards point at entities; this is
    the test that it actually does."""
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        car = call_tool(
            server, "create_entity", {"entity_type": "vehicle", "name": "Toyota"}
        )
        board = call_tool(
            server, "create_board", {"entity_id": car["id"], "title": "Maintenance"}
        )
        view = call_tool(server, "get_board", {"board_id": board["id"]})
    finally:
        app.dependency_overrides.clear()

    assert view["entity"]["type"] == "vehicle"
    assert view["title"] == "Maintenance"


def test_round_trip_list_boards_narrows_to_one_entity(test_settings, call_tool):
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        first = call_tool(
            server, "create_entity", {"entity_type": "project", "name": "one"}
        )
        second = call_tool(
            server, "create_entity", {"entity_type": "project", "name": "two"}
        )
        call_tool(server, "create_board", {"entity_id": first["id"], "title": "A"})
        call_tool(server, "create_board", {"entity_id": second["id"], "title": "B"})

        narrowed = call_tool(server, "list_boards", {"entity_id": first["id"]})
        everything = call_tool(server, "list_boards", {})
    finally:
        app.dependency_overrides.clear()

    assert narrowed["total"] == 1
    assert narrowed["items"][0]["title"] == "A"
    assert everything["total"] == 2


def test_round_trip_a_ticket_appears_in_its_column(test_settings, call_tool):
    """The board view is the primary read, so it is worth proving a ticket
    actually lands in the right column rather than only that the shape is
    right.

    The ticket is created and moved through plain HTTP: create_ticket
    arrives in Task 4, and a board test that called it could not run until
    then. Same rule as the read tools' seeding — the tool under test here is
    get_board.
    """
    import anyio

    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        entity = call_tool(
            server, "create_entity", {"entity_type": "project", "name": "proj"}
        )
        board = call_tool(
            server, "create_board", {"entity_id": entity["id"], "title": "D"}
        )

        client = QCoreClient(test_settings, transport=httpx.ASGITransport(app=app))

        async def seed_ticket():
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
            await client.request(
                "POST",
                f"/tickets/{ticket['id']}/transition",
                json={
                    "to_status": "blocked",
                    "actor": "owner",
                    "note": "waiting on a decision",
                },
            )
            return ticket

        ticket = anyio.run(seed_ticket)
        view = call_tool(server, "get_board", {"board_id": board["id"]})
    finally:
        app.dependency_overrides.clear()

    assert [t["id"] for t in view["columns"]["blocked"]["items"]] == [ticket["id"]]
    assert view["columns"]["backlog"] == {"items": [], "total": 0}


def test_get_board_description_sends_the_reader_to_get_ticket(
    test_settings, tools_by_name
):
    """The contract change has to be legible to a model.

    The board no longer carries descriptions (a todo). A model that is
    not told will read the absence as the ticket having none, rather than
    as one call away — which is a wrong answer, not a missing feature.
    """
    tools = tools_by_name(
        _server(test_settings, lambda r: httpx.Response(200, json={}))
    )
    description = tools["get_board"].description.lower()

    assert "get_ticket" in description
    assert "summaries" in description or "summary" in description
    assert "total" in description, "a capped column must be explainable"


def test_get_ticket_description_says_it_carries_the_body(
    test_settings, tools_by_name
):
    tools = tools_by_name(
        _server(test_settings, lambda r: httpx.Response(200, json={}))
    )
    description = tools["get_ticket"].description.lower()

    assert "description" in description
    assert "attachment_count" in description


# --- update_board / delete_board (ticket T-64) --------------------------------


def test_update_board_patches_only_the_title(test_settings, call_tool):
    seen, handler = _recording({"id": "b1", "title": "Renamed"})

    call_tool(
        _server(test_settings, handler),
        "update_board",
        {"board_id": "b1", "title": "Renamed"},
    )

    assert seen["method"] == "PATCH"
    assert seen["path"] == "/boards/b1"
    assert seen["body"] == {"title": "Renamed"}


def test_delete_board_deletes_the_board_path(test_settings, call_tool):
    seen, handler = _recording({"deleted": True})

    call_tool(_server(test_settings, handler), "delete_board", {"board_id": "b1"})

    assert seen["method"] == "DELETE"
    assert seen["path"] == "/boards/b1"


def test_both_board_writes_are_annotated_destructive(
    test_settings, tools_by_name
):
    """Renamed, because the old name asserted the opposite of the ruling.

    It was test_delete_board_is_destructive_and_update_board_is_not, and
    update_board IS destructive: renaming a board overwrites a value the
    caller may not have meant to lose, which is #135's line that an
    overwrite is not an additive write.

    ASSERTED WITH `is`, never `not getattr(..., None)`. review-1 caught
    the old form passing BECAUSE THE ANNOTATION WAS ABSENT -- `not None`
    is True, so "this tool is not destructive" and "nobody said" were the
    same result. That is the exact bool(None) conflation #135 removes,
    and I had written it one file over on the same day. A missing
    annotation must fail here, not read as a decision.
    """
    tools = tools_by_name(
        _server(test_settings, lambda r: httpx.Response(200, json={}))
    )

    for name in ("delete_board", "update_board"):
        annotations = tools[name].annotations
        assert annotations is not None, f"{name} carries no annotations at all"
        assert annotations.destructive_hint is True, (
            f"{name} must say destructive_hint=True explicitly; it is "
            f"{annotations.destructive_hint!r}, and absent is not False"
        )
    # The read stays read-only, so the pair is a contrast and not a
    # blanket that would pass however the writes were marked.
    assert tools["get_board"].annotations.read_only_hint is True


def test_round_trip_a_board_made_over_mcp_can_be_removed_over_mcp(
    test_settings, call_tool
):
    """Ticket T-64 itself: create_board was on the surface and delete_board
    was not, so a board created here could never be removed here — and
    because a referenced entity is correctly refused deletion, the board
    stranded its entity too. Found cleaning up after the live exercise,
    which had to fall back to curl.

    The assertion is the WHOLE round trip, ending in the entity delete:
    checking only that delete_board returns 200 would pass while the
    thing the ticket is about — being able to finish — still failed.
    """
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        entity = call_tool(
            server, "create_entity", {"entity_type": "project", "name": "scratch"}
        )
        board = call_tool(
            server, "create_board", {"entity_id": entity["id"], "title": "typo"}
        )
        renamed = call_tool(
            server, "update_board", {"board_id": board["id"], "title": "Fixed"}
        )
        deleted = call_tool(server, "delete_board", {"board_id": board["id"]})
        gone = call_tool(server, "delete_entity", {"entity_id": entity["id"]})
    finally:
        app.dependency_overrides.clear()

    assert renamed["title"] == "Fixed"
    assert deleted == {"deleted": True}
    assert gone, "the entity must be deletable once its board is gone"


def test_round_trip_a_board_with_tickets_refuses_to_be_deleted(
    test_settings, call_tool
):
    """The reason delete_board is safe to expose at all. An accidental
    delete cannot take work with it: emptying the board first is a
    decision about each ticket, not one about the board."""
    from mcp.server.mcpserver.exceptions import ToolError

    import pytest

    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        entity = call_tool(
            server, "create_entity", {"entity_type": "project", "name": "scratch"}
        )
        board = call_tool(
            server, "create_board", {"entity_id": entity["id"], "title": "Work"}
        )
        call_tool(
            server,
            "create_ticket",
            {
                "board_id": board["id"],
                "ticket_type": "task",
                "title": "t",
                "actor": "tester",
            },
        )
        with pytest.raises(ToolError) as excinfo:
            call_tool(server, "delete_board", {"board_id": board["id"]})
    finally:
        app.dependency_overrides.clear()

    assert "ticket" in str(excinfo.value).lower(), str(excinfo.value)


def test_round_trip_update_board_refuses_a_clear_naming_what_is_clearable(
    test_settings, call_tool
):
    """update_board takes `clear` to satisfy the surface-wide convention,
    and a board has nothing clearable — impl-1's guard says such a tool
    must be dealt with deliberately rather than default into the gap.

    Deliberate here means: forward it, and let the API's own
    _refuse_unclearable answer. One source for what is clearable, and
    the caller is told rather than quietly obeyed — a board with no
    title could not be told apart from the others.

    THE ASSERTION HAS TO NAME THE CLEAR REFUSAL SPECIFICALLY. Asserting
    only that "title" appears passed while the tool DROPPED `clear`
    altogether: the body was then empty and _require_at_least_one
    refused with "at least one of title is required", which also
    contains "title". A mutation replacing apply_clear with `pass`
    survived the entire suite on exactly that. The two refusals mean
    opposite things — "you asked for something impossible" against "you
    asked for nothing" — so this matches the phrase only the first one
    carries, and asserts the second one is NOT what came back.
    """
    from mcp.server.mcpserver.exceptions import ToolError

    import pytest

    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        entity = call_tool(
            server, "create_entity", {"entity_type": "project", "name": "scratch"}
        )
        board = call_tool(
            server, "create_board", {"entity_id": entity["id"], "title": "Work"}
        )
        with pytest.raises(ToolError) as excinfo:
            call_tool(
                server,
                "update_board",
                {"board_id": board["id"], "clear": ["title"]},
            )
    finally:
        app.dependency_overrides.clear()

    message = str(excinfo.value)
    assert "cannot clear title" in message, message
    assert "Clearable here: nothing on this model" in message, message
    assert "at least one of" not in message, (
        "this is the empty-update refusal, not the unclearable one -- "
        f"`clear` never reached the body: {message}"
    )

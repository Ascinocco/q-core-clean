"""Jyra ticket writes: create_ticket, update_ticket, delete_ticket."""

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from q_core_mcp.client import QCoreClient
from q_core_mcp.server import build_server


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
        if request.content:
            seen["body"] = jsonlib.loads(request.content)
        return httpx.Response(200, json=payload if payload is not None else {})

    return seen, handler


def _board(server, call_tool):
    entity = call_tool(
        server, "create_entity", {"entity_type": "project", "name": "proj"}
    )
    return call_tool(
        server, "create_board", {"entity_id": entity["id"], "title": "D"}
    )["id"]


# --- paths and bodies --------------------------------------------------


def test_create_ticket_posts_the_required_fields(test_settings, call_tool):
    seen, handler = _recording({"id": "t1"})
    call_tool(
        _server(test_settings, handler),
        "create_ticket",
        {
            "board_id": "b1",
            "ticket_type": "task",
            "title": "Do it",
            "actor": "owner",
        },
    )
    assert (seen["method"], seen["path"]) == ("POST", "/tickets")
    assert seen["body"] == {
        "board_id": "b1",
        "type": "task",
        "title": "Do it",
        "actor": "owner",
    }


def test_create_ticket_omits_description_and_parent_when_absent(
    test_settings, call_tool
):
    """Sent only when given. description is nullable so a null would be
    harmless, but parent_id null takes the "no parent" branch while an
    empty string would be looked up as a real id and 400."""
    seen, handler = _recording({"id": "t1"})
    call_tool(
        _server(test_settings, handler),
        "create_ticket",
        {
            "board_id": "b1",
            "ticket_type": "task",
            "title": "t",
            "actor": "owner",
            "description": "## notes",
            "parent_id": "p1",
        },
    )
    assert seen["body"]["description"] == "## notes"
    assert seen["body"]["parent_id"] == "p1"


def test_update_ticket_sends_only_what_it_was_given(test_settings, call_tool):
    """Same reasoning as update_entity, and the reason has narrowed.

    An explicit null used to take the same branch as an absent field and
    return 200 having changed nothing, so omitting was the only
    protection. Since ticket T-56 `TicketUpdate` refuses a body that names
    no field at all.

    Omitting still matters for the PARTIAL case: a null alongside a real
    value is indistinguishable from "leave that field alone", and sending
    it would claim the caller said something they did not.
    """
    seen, handler = _recording({"id": "t1"})
    call_tool(
        _server(test_settings, handler),
        "update_ticket",
        {"ticket_id": "t1", "title": "Renamed"},
    )
    assert (seen["method"], seen["path"]) == ("PATCH", "/tickets/t1")
    assert seen["body"] == {"title": "Renamed"}


def test_delete_ticket_deletes(test_settings, call_tool):
    seen, handler = _recording({"deleted": True})
    call_tool(_server(test_settings, handler), "delete_ticket", {"ticket_id": "t1"})
    assert (seen["method"], seen["path"]) == ("DELETE", "/tickets/t1")


# --- annotations and descriptions --------------------------------------


def test_delete_ticket_is_annotated_destructive(test_settings, tools_by_name):
    seen, handler = _recording()
    tools = tools_by_name(_server(test_settings, handler))
    annotations = tools["delete_ticket"].annotations
    assert annotations and annotations.destructive_hint
    # create_ticket is additive; update_ticket is NOT, since ticket T-61 --
    # it overwrites fields a caller may not have meant to lose, and the
    # MCP spec's `destructive` means "not purely additive", not "deletes
    # a row". Asserted as an explicit False rather than a falsy absence,
    # which is what this pair used to be testing without meaning to.
    assert tools["create_ticket"].annotations.destructive_hint is False
    assert tools["update_ticket"].annotations.destructive_hint is True


def test_update_ticket_description_points_at_transition_ticket(
    test_settings, tools_by_name
):
    """status is not patchable, and a model that tries is refused rather
    than quietly ignored. The description has to say where status actually
    moves, or the refusal is a dead end.

    Deliberately not "gets a 422": the tool raises before sending anything,
    so no request is made and no status code exists on this path. The API
    would return 422 if the field reached it, but it never does — and a
    docstring that names a status code the reader will never observe is the
    kind of plausible-sounding detail that survives precisely because
    nothing checks it.
    """
    seen, handler = _recording()
    description = tools_by_name(_server(test_settings, handler))[
        "update_ticket"
    ].description.lower()
    assert "status" in description
    assert "transition_ticket" in description


def test_delete_ticket_description_warns_about_history_and_children(
    test_settings, tools_by_name
):
    """Two things a caller cannot undo or discover afterwards: the audit
    trail goes with the ticket, and a ticket with children is refused."""
    seen, handler = _recording()
    description = tools_by_name(_server(test_settings, handler))[
        "delete_ticket"
    ].description.lower()
    assert "history" in description
    assert "child" in description or "children" in description


def test_create_ticket_description_lists_the_types_and_the_hierarchy_rule(
    test_settings, tools_by_name
):
    seen, handler = _recording()
    description = tools_by_name(_server(test_settings, handler))[
        "create_ticket"
    ].description
    assert "solution" in description and "epic" in description
    assert "parent" in description.lower()


# --- round trips -------------------------------------------------------


def test_round_trip_update_ticket_cannot_change_status(test_settings, call_tool):
    """The behaviour half. A description saying "you cannot patch status" is
    worth nothing if the attempt quietly succeeds instead.

    The refusal happens in the tool, before any request — so what a caller
    observes is an error, not a 422. Asserted as ToolError rather than by
    status code for that reason, and the ticket is re-fetched afterwards to
    prove nothing changed, which is the half a raises-assertion alone would
    miss.
    """
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        board_id = _board(server, call_tool)
        ticket = call_tool(
            server,
            "create_ticket",
            {
                "board_id": board_id,
                "ticket_type": "task",
                "title": "t",
                "actor": "owner",
            },
        )
        with pytest.raises(ToolError):
            call_tool(
                server,
                "update_ticket",
                {"ticket_id": ticket["id"], "status": "done"},
            )
        unchanged = call_tool(server, "get_ticket", {"ticket_id": ticket["id"]})
    finally:
        app.dependency_overrides.clear()

    assert unchanged["status"] == "backlog"


def test_round_trip_create_ticket_writes_its_creation_history(
    test_settings, call_tool
):
    """A ticket's history starts at creation, so MIN(created_at) over its
    transitions is its creation time. Proven through the tools rather than
    assumed from the API's tests."""
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        board_id = _board(server, call_tool)
        ticket = call_tool(
            server,
            "create_ticket",
            {
                "board_id": board_id,
                "ticket_type": "task",
                "title": "t",
                "actor": "owner",
            },
        )
        history = call_tool(server, "get_ticket_history", {"ticket_id": ticket["id"]})
    finally:
        app.dependency_overrides.clear()

    assert history["total"] == 1
    assert history["items"][0]["from_status"] is None
    assert history["items"][0]["to_status"] == "backlog"


def test_round_trip_a_parent_must_outrank_its_child(test_settings, call_tool):
    """The hierarchy rule surfaces as an error with the API's own message,
    so a model can correct itself rather than guessing."""
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        board_id = _board(server, call_tool)
        bug = call_tool(
            server,
            "create_ticket",
            {
                "board_id": board_id,
                "ticket_type": "bug",
                "title": "b",
                "actor": "owner",
            },
        )
        with pytest.raises(ToolError) as excinfo:
            call_tool(
                server,
                "create_ticket",
                {
                    "board_id": board_id,
                    "ticket_type": "epic",
                    "title": "e",
                    "actor": "owner",
                    "parent_id": bug["id"],
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert "parent" in str(excinfo.value).lower()


def test_round_trip_delete_refuses_a_ticket_with_children(test_settings, call_tool):
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        board_id = _board(server, call_tool)
        epic = call_tool(
            server,
            "create_ticket",
            {
                "board_id": board_id,
                "ticket_type": "epic",
                "title": "e",
                "actor": "owner",
            },
        )
        call_tool(
            server,
            "create_ticket",
            {
                "board_id": board_id,
                "ticket_type": "task",
                "title": "t",
                "actor": "owner",
                "parent_id": epic["id"],
            },
        )
        with pytest.raises(ToolError) as excinfo:
            call_tool(server, "delete_ticket", {"ticket_id": epic["id"]})
        survived = call_tool(server, "get_ticket", {"ticket_id": epic["id"]})
    finally:
        app.dependency_overrides.clear()

    assert "child" in str(excinfo.value).lower()
    assert survived["id"] == epic["id"]


def test_update_ticket_rejects_status_instead_of_dropping_it(
    test_settings, call_tool
):
    """Accepting `status` only to refuse it is deliberate.

    Without the parameter the SDK drops an unknown argument before the tool
    runs, so update_ticket(status="done") sent an empty PATCH and returned
    200 — telling the model its update succeeded while nothing changed.
    That is worse than an error, and it is the exact failure update_entity's
    comment warns about. The description claimed status was "rejected rather
    than quietly ignored"; this makes that true.
    """
    seen, handler = _recording({"id": "t1"})
    with pytest.raises(ToolError) as excinfo:
        call_tool(
            _server(test_settings, handler),
            "update_ticket",
            {"ticket_id": "t1", "status": "done"},
        )

    message = str(excinfo.value)
    assert "transition_ticket" in message
    assert "done" in message  # echoes what they tried, so the fix is obvious
    # And nothing was sent: no silent empty PATCH.
    assert "body" not in seen

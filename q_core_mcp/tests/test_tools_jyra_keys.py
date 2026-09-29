"""Ticket keys through the MCP tools (ticket T-19).

The API tests pin the behaviour; these pin that every ticket-taking tool
reaches it -- a tool that URL-encoded, validated or rewrote the id on
its own would break keys without the API noticing -- and that the tool
descriptions say keys are accepted.
"""

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from q_core_mcp.client import QCoreClient
from q_core_mcp.server import build_server

#: Every tool with a ticket_id argument. Discovered below and compared, so
#: a new ticket tool that forgets the key sentence fails here.
TICKET_TOOLS = {
    "get_ticket",
    "get_ticket_history",
    "transition_ticket",
    "update_ticket",
    "delete_ticket",
    "list_attachments",
    "attach_file",
    "list_artifacts",  # Canvas: artifacts linked to a ticket
}


@pytest.fixture()
def server(test_settings):
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        yield build_server(
            QCoreClient(test_settings, transport=httpx.ASGITransport(app=app))
        )
    finally:
        app.dependency_overrides.clear()


def _board(server, call_tool, **extra):
    entity = call_tool(server, "create_entity", {"entity_type": "project", "name": "Canvas"})
    return call_tool(server, "create_board", {"entity_id": entity["id"], "title": "D", **extra})


def test_every_ticket_tool_says_it_takes_a_key(test_settings, tools_by_name):
    tools = tools_by_name(build_server(QCoreClient(test_settings)))
    with_ticket_id = {
        name for name, tool in tools.items()
        if "ticket_id" in (tool.input_schema or {}).get("properties", {})
    }
    assert with_ticket_id == TICKET_TOOLS
    for name in with_ticket_id:
        assert "KA-12" in tools[name].description, name
    # These name a ticket through target_id, so they are not discovered
    # above; they take keys all the same, and must say so (review R3-F1).
    for name in ("link_artifact", "create_note", "list_notes", "link_note"):
        assert "KA-12" in tools[name].description, name


def test_create_board_takes_or_derives_a_prefix(server, call_tool):
    assert _board(server, call_tool)["key_prefix"] == "CAN"
    assert _board(server, call_tool, key_prefix="art9")["key_prefix"] == "ART9"


def test_update_board_changes_the_prefix_until_a_ticket_exists(server, call_tool):
    board = _board(server, call_tool)
    changed = call_tool(server, "update_board", {"board_id": board["id"], "key_prefix": "af"})
    assert changed["key_prefix"] == "AF"
    call_tool(
        server, "create_ticket",
        {"board_id": board["id"], "ticket_type": "task", "title": "t", "actor": "a"},
    )
    with pytest.raises(ToolError, match="never had a ticket"):
        call_tool(server, "update_board", {"board_id": board["id"], "key_prefix": "ZZ"})


def test_the_tools_work_a_ticket_by_key_end_to_end(server, call_tool):
    board = _board(server, call_tool, key_prefix="KA")["id"]
    epic = call_tool(
        server, "create_ticket",
        {"board_id": board, "ticket_type": "epic", "title": "E", "actor": "a"},
    )
    task = call_tool(
        server, "create_ticket",
        {"board_id": board, "ticket_type": "task", "title": "T", "actor": "a",
         "parent_id": "ka-1"},
    )
    assert (epic["key"], task["key"], task["parent_id"]) == ("KA-1", "KA-2", epic["id"])

    assert call_tool(server, "get_ticket", {"ticket_id": "ka-2"})["id"] == task["id"]
    moved = call_tool(
        server, "transition_ticket",
        {"ticket_id": "KA-2", "to_status": "agent_ready", "actor": "a", "note": "ready"},
    )
    assert moved["status"] == "agent_ready"
    history = call_tool(server, "get_ticket_history", {"ticket_id": "Ka-2", "all": True})
    assert [entry["ticket_key"] for entry in history["items"]] == ["KA-2", "KA-2"]
    listed = call_tool(server, "list_tickets", {"parent_id": "KA-1"})
    assert [item["key"] for item in listed["items"]] == ["KA-2"]
    board_view = call_tool(server, "get_board", {"board_id": board})
    assert board_view["key_prefix"] == "KA"
    assert board_view["columns"]["agent_ready"]["items"][0]["key"] == "KA-2"
    assert call_tool(
        server, "update_ticket", {"ticket_id": "ka-2", "title": "T2"}
    )["title"] == "T2"
    assert call_tool(server, "list_attachments", {"ticket_id": "KA-2"})["total"] == 0

    with pytest.raises(ToolError, match="KA-7"):
        call_tool(server, "get_ticket", {"ticket_id": "KA-7"})

    assert call_tool(server, "delete_ticket", {"ticket_id": "ka-2"}) == {"deleted": True}

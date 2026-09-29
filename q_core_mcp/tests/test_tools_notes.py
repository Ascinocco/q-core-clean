"""The note tools: request shaping, and the ticket's acceptance scenario
driven end to end through the production wiring (`_LazyClient`, real app).
"""

import json

import httpx
import pytest

from api.config import get_settings
from api.main import _LazyClient
from q_core_mcp.client import QCoreClient
from q_core_mcp.server import build_server

NOTE_TOOLS = ("create_note", "get_note", "list_notes", "update_note",
              "delete_note", "link_note", "unlink_note")


def _capture(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, dict(request.url.params),
                     json.loads(request.content) if request.content else None))
        return httpx.Response(200, json={"id": "n1", "items": [], "total": 0, "limit": 50, "offset": 0})
    return handler


def test_all_note_tools_are_registered(test_settings, tools_by_name):
    server = build_server(QCoreClient(test_settings, transport=httpx.MockTransport(_capture([]))))
    assert set(NOTE_TOOLS) <= set(tools_by_name(server))


def test_requests_carry_exactly_what_was_given(test_settings, call_tool):
    seen = []
    server = build_server(QCoreClient(test_settings, transport=httpx.MockTransport(_capture(seen))))
    call_tool(server, "create_note", {"body": "# Plan"})
    call_tool(server, "create_note", {"body": "b", "links": [{"target_type": "entity", "target_id": "e1"}]})
    call_tool(server, "update_note", {"note_id": "n1", "body": "new"})
    call_tool(server, "list_notes", {"target_type": "entity", "target_id": "e1", "q": "tire"})
    call_tool(server, "link_note", {"note_id": "n1", "target_type": "ticket", "target_id": "t1"})
    call_tool(server, "unlink_note", {"note_id": "n1", "link_id": "l1"})
    call_tool(server, "delete_note", {"note_id": "n1"})
    assert seen[0] == ("POST", "/notes", {}, {"body": "# Plan"})
    assert seen[1][3] == {"body": "b", "links": [{"target_type": "entity", "target_id": "e1"}]}
    assert seen[2] == ("PATCH", "/notes/n1", {}, {"body": "new"})
    assert seen[3][1] == "/notes" and seen[3][2]["target_type"] == "entity" and seen[3][2]["q"] == "tire"
    assert seen[4] == ("POST", "/notes/n1/links", {}, {"target_type": "ticket", "target_id": "t1"})
    assert seen[5][:2] == ("DELETE", "/notes/n1/links/l1")
    assert seen[6][:2] == ("DELETE", "/notes/n1")


@pytest.fixture()
def production_server(test_settings, monkeypatch):
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    monkeypatch.setattr("api.main.get_settings", lambda: test_settings)
    monkeypatch.setattr("api.main.QCoreClient",
                        lambda settings: QCoreClient(settings, transport=httpx.ASGITransport(app=app)))
    try:
        yield build_server(_LazyClient())
    finally:
        app.dependency_overrides.clear()


def test_acceptance_draft_revise_link_and_find_by_entity(production_server, call_tool):
    """Ticket done-when: create a standalone note, revise it several times,
    link it to an entity, and find it again by that entity."""
    server = production_server
    note = call_tool(server, "create_note", {"body": "# Home server build\n- case"})
    for n in range(3):
        current = call_tool(server, "get_note", {"note_id": note["id"]})
        call_tool(server, "update_note", {"note_id": note["id"], "body": current["body"] + f"\n- step {n}"})
    entity = call_tool(server, "create_entity", {"entity_type": "project", "name": "Home server"})
    linked = call_tool(server, "link_note",
                       {"note_id": note["id"], "target_type": "entity", "target_id": entity["id"]})
    assert linked["link_id"]

    found = call_tool(server, "list_notes", {"target_type": "entity", "target_id": entity["id"], "all": True})
    assert [item["id"] for item in found["items"]] == [note["id"]]
    assert found["items"][0]["title"] == "Home server build"
    full = call_tool(server, "get_note", {"note_id": note["id"]})
    assert full["body"].endswith("- step 0\n- step 1\n- step 2") and full["updated_at"]

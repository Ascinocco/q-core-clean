import anyio
import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from q_core_mcp.client import QCoreClient
from q_core_mcp.server import build_server

def _server(test_settings, handler):
    client = QCoreClient(test_settings, transport=httpx.MockTransport(handler))
    return build_server(client)


def _real_server(test_settings, app):
    client = QCoreClient(test_settings, transport=httpx.ASGITransport(app=app))
    return build_server(client)


def test_delete_entity_sends_delete_to_the_right_path(test_settings, call_tool):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json={"deleted": True})

    call_tool(_server(test_settings, handler), "delete_entity", {"entity_id": "abc"})

    assert seen["method"] == "DELETE"
    assert seen["path"] == "/entities/abc"


def test_delete_relationship_uses_the_flat_path_not_the_nested_one(
    test_settings,
    call_tool,
):
    # Relationships are created under /entities/{id}/relationships but
    # deleted at /relationships/{id}. Getting this wrong would 404 against
    # a path that looks plausible.
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={"deleted": True})

    call_tool(
        _server(test_settings, handler),
        "delete_relationship",
        {"relationship_id": "rel"},
    )

    assert seen["path"] == "/relationships/rel"


def test_delete_tools_are_annotated_destructive(test_settings, tools_by_name):
    tools = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))

    for name in ("delete_entity", "delete_relationship"):
        assert tools[name].annotations is not None, name
        assert tools[name].annotations.destructive_hint is True, name


def test_delete_tools_are_not_marked_read_only(test_settings, tools_by_name):
    tools = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))

    for name in ("delete_entity", "delete_relationship"):
        annotations = tools[name].annotations
        hint = annotations.read_only_hint if annotations else None
        assert hint is not True, name


def test_delete_descriptions_warn_that_it_is_permanent(test_settings, tools_by_name):
    # These are the only two tools that destroy data. The description is
    # the only thing standing between "I sold the car" and losing its
    # whole service history, so the warning is load-bearing, not decorative.
    tools = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))

    for name in ("delete_entity", "delete_relationship"):
        description = tools[name].description.lower()
        assert "cannot be undone" in description, name

    # delete_entity should point at the non-destructive alternative.
    assert "status" in tools["delete_entity"].description.lower()


def test_delete_relationship_description_explains_which_id_it_wants(
    test_settings,
    tools_by_name,
):
    # The likeliest destructive mistake here is passing an entity id,
    # which would either 404 or — worse, if ids ever collided — delete
    # something unintended.
    tools = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))

    description = tools["delete_relationship"].description.lower()
    assert "list_relationships" in description
    assert "not the id of either entity" in description


def test_conflict_message_reaches_the_caller(test_settings, call_tool):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": "conflict",
                    "message": (
                        "Cannot delete entity 'abc': still referenced by other records"
                    ),
                }
            },
        )

    with pytest.raises(ToolError) as exc_info:
        call_tool(_server(test_settings, handler), "delete_entity", {"entity_id": "abc"})

    assert "still referenced" in str(exc_info.value)


def test_round_trip_delete_entity_then_it_is_gone(test_settings, call_tool):
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)

        # Created through the client rather than a write tool: those live
        # on another branch, and this test must not depend on merge order.
        async def create():
            return await server_client.request(
                "POST", "/entities", json={"type": "pet", "name": "Rex"}
            )

        server_client = QCoreClient(
            test_settings, transport=httpx.ASGITransport(app=app)
        )
        import anyio

        created = anyio.run(create)
        result = call_tool(server, "delete_entity", {"entity_id": created["id"]})

        with pytest.raises(ToolError) as exc_info:
            call_tool(server, "get_entity", {"entity_id": created["id"]})
    finally:
        app.dependency_overrides.clear()

    assert result == {"deleted": True}
    assert created["id"] in str(exc_info.value)


def test_round_trip_delete_unknown_entity_reports_it(test_settings, call_tool):
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        with pytest.raises(ToolError) as exc_info:
            call_tool(server, "delete_entity", {"entity_id": "no-such-entity"})
    finally:
        app.dependency_overrides.clear()

    assert "no-such-entity" in str(exc_info.value)


def test_round_trip_delete_relationship_leaves_its_entities_alone(
    test_settings,
    call_tool,
):
    # Deleting a link must not cascade to the things it linked.
    from api.config import get_settings
    from api.main import app
    import anyio

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        client = QCoreClient(test_settings, transport=httpx.ASGITransport(app=app))

        async def setup():
            owner = await client.request(
                "POST", "/entities", json={"type": "person", "name": "Alex"}
            )
            car = await client.request(
                "POST", "/entities", json={"type": "vehicle", "name": "Corolla"}
            )
            link = await client.request(
                "POST",
                f"/entities/{owner['id']}/relationships",
                json={"to_entity_id": car["id"], "relationship_type": "owns"},
            )
            return owner, car, link

        owner, car, link = anyio.run(setup)
        call_tool(server, "delete_relationship", {"relationship_id": link["id"]})
        links = call_tool(server, "list_relationships", {"entity_id": owner["id"]})
        still_there = call_tool(server, "get_entity", {"entity_id": car["id"]})
    finally:
        app.dependency_overrides.clear()

    assert links["total"] == 0
    assert still_there["name"] == "Corolla"

import anyio
import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from q_core_mcp.client import QCoreClient
from q_core_mcp.server import build_server


def _server(test_settings, handler):
    client = QCoreClient(test_settings, transport=httpx.MockTransport(handler))
    return build_server(client)


def test_read_tools_are_registered(test_settings, tools_by_name):
    names = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))

    assert {"get_entity", "list_entities", "list_relationships"} <= set(names)


def test_read_tools_are_annotated_read_only(test_settings, tools_by_name):
    tools = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))

    for name in ("get_entity", "list_entities", "list_relationships"):
        assert tools[name].annotations is not None, name
        assert tools[name].annotations.read_only_hint is True, name


def test_descriptions_carry_the_enum_values(test_settings, tools_by_name):
    # The SDK captures __doc__ at decoration time, so these descriptions
    # are passed to the decorator explicitly. This test fails if someone
    # "simplifies" that back into a formatted docstring, which would
    # silently ship {placeholders} to the model.
    tools = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))

    for placeholder in ("{types}", "{statuses}", "{relationship_types}"):
        for tool in tools.values():
            assert placeholder not in tool.description, (tool.name, placeholder)
    assert "vehicle" in tools["list_entities"].description
    assert "sold" in tools["list_entities"].description
    assert "resides_at" in tools["list_relationships"].description


def test_get_entity_requests_the_right_path(test_settings, call_tool):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json={"id": "abc", "name": "Rex"})

    payload = call_tool(_server(test_settings, handler), "get_entity", {"entity_id": "abc"})

    assert seen["method"] == "GET"
    assert seen["path"] == "/entities/abc"
    assert payload["name"] == "Rex"


def test_list_entities_omits_filters_it_was_not_given(test_settings, call_tool):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"items": [], "total": 0})

    call_tool(
        _server(test_settings, handler),
        "list_entities",
        {"entity_type": "vehicle", "limit": 10},
    )

    # status absent entirely, not sent empty: it is a Literal on the API
    # side, so an empty value would be a 422 rather than "no filter".
    # limit is sent because the caller gave one; offset is not, because
    # they did not -- an unrequested page 0 is the same statement as no
    # opinion, and sending it restates the default.
    assert seen["params"] == {"type": "vehicle", "limit": "10"}


def test_list_relationships_requests_the_nested_path(test_settings, call_tool):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={"items": [], "total": 0})

    call_tool(_server(test_settings, handler), "list_relationships", {"entity_id": "abc"})

    assert seen["path"] == "/entities/abc/relationships"


def test_api_error_message_reaches_the_caller(test_settings, call_tool):
    # The load-bearing one. The SDK passes a ToolError's message through to
    # the model but replaces any other exception with a bare
    # "Error executing tool <name>". If ApiError stopped subclassing
    # ToolError, every mapped message from client.py would vanish and this
    # is the test that says so.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"error": {"code": "not_found", "message": "No entity with id 'x'"}},
        )

    with pytest.raises(ToolError) as exc_info:
        call_tool(_server(test_settings, handler), "get_entity", {"entity_id": "x"})

    assert "No entity with id 'x'" in str(exc_info.value)


def test_validation_details_reach_the_caller_as_field_lines(test_settings, call_tool):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "error": {
                    "code": "validation_error",
                    "message": "Request validation failed",
                    "details": [
                        {"field": "query.limit", "message": "Input should be >= 1"}
                    ],
                }
            },
        )

    with pytest.raises(ToolError) as exc_info:
        call_tool(_server(test_settings, handler), "list_entities", {"limit": 0})

    assert "query.limit: Input should be >= 1" in str(exc_info.value)


def test_round_trip_against_the_real_api(test_settings, call_tool):
    # The stubs above pin what the tools send; this pins that the real API
    # accepts it. A tool can agree with a mock and still be wrong.
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        client = QCoreClient(test_settings, transport=httpx.ASGITransport(app=app))
        server = build_server(client)

        async def create():
            return await client.request(
                "POST", "/entities", json={"type": "pet", "name": "Rex"}
            )

        created = anyio.run(create)
        fetched = call_tool(server, "get_entity", {"entity_id": created["id"]})
        listing = call_tool(server, "list_entities", {"entity_type": "pet"})
        links = call_tool(server, "list_relationships", {"entity_id": created["id"]})
    finally:
        app.dependency_overrides.clear()

    assert fetched["name"] == "Rex"
    assert listing["total"] == 1
    assert links["total"] == 0


def test_round_trip_unknown_entity_surfaces_the_real_404(test_settings, call_tool):
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        client = QCoreClient(test_settings, transport=httpx.ASGITransport(app=app))
        server = build_server(client)
        with pytest.raises(ToolError) as exc_info:
            call_tool(server, "get_entity", {"entity_id": "nope"})
    finally:
        app.dependency_overrides.clear()

    assert "nope" in str(exc_info.value)

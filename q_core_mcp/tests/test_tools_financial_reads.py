import httpx

from q_core_mcp.client import QCoreClient
from q_core_mcp.server import build_server


def _server(test_settings, handler):
    client = QCoreClient(test_settings, transport=httpx.MockTransport(handler))
    return build_server(client)


def _real_server(test_settings, app):
    client = QCoreClient(test_settings, transport=httpx.ASGITransport(app=app))
    return build_server(client)


def test_list_categories_requests_the_right_path(test_settings, call_tool):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"items": [], "total": 0})

    call_tool(_server(test_settings, handler), "list_categories", {})

    assert seen["path"] == "/categories"
    # Nothing on the wire: the caller named no page, so the API's default
    # governs. This used to assert limit=50, which pinned the tool layer
    # restating a default that belongs to the API (D27).
    assert seen["params"] == {}


def test_list_categories_passes_parent_id_only_when_given(test_settings, call_tool):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"items": [], "total": 0})

    call_tool(_server(test_settings, handler), "list_categories", {"parent_id": "food"})

    assert seen["params"]["parent_id"] == "food"


def test_list_statements_requests_the_right_path(test_settings, call_tool):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"items": [], "total": 0})

    call_tool(
        _server(test_settings, handler), "list_statements", {"account_id": "acct"}
    )

    assert seen["path"] == "/statements"
    assert seen["params"]["account_id"] == "acct"


def test_financial_reads_are_annotated_read_only(test_settings, tools_by_name):
    tools = tools_by_name(
        _server(test_settings, lambda r: httpx.Response(200, json={}))
    )

    for name in ("list_categories", "list_statements"):
        assert tools[name].annotations is not None, name
        assert tools[name].annotations.read_only_hint is True, name


def test_list_categories_description_says_the_taxonomy_is_fixed(
    test_settings, tools_by_name
):
    # No category-creating tool exists by design. A model that does not
    # know that will keep looking for one, or report that it cannot
    # proceed, instead of using what is already seeded.
    tools = tools_by_name(
        _server(test_settings, lambda r: httpx.Response(200, json={}))
    )
    description = tools["list_categories"].description.lower()

    assert "fixed" in description
    assert "cannot be created" in description or "cannot be edited" in description


def test_round_trip_list_categories_returns_the_seeded_taxonomy(
    test_settings, call_tool
):
    # The seeded two-level tree is installed by db/seed_categories.sql at
    # bootstrap, which is what makes list_categories useful with no
    # category-creating tool alongside it. If this ever returns nothing,
    # the "fixed taxonomy" scoping decision no longer holds.
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        listing = call_tool(_real_server(test_settings, app), "list_categories", {})
    finally:
        app.dependency_overrides.clear()

    assert listing["total"] > 0
    assert listing["items"][0]["name"]


def test_round_trip_list_statements_is_empty_on_a_fresh_database(
    test_settings, call_tool
):
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        listing = call_tool(_real_server(test_settings, app), "list_statements", {})
    finally:
        app.dependency_overrides.clear()

    assert listing["total"] == 0

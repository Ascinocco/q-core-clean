"""The `list_due_items` tool.

The request-shaping tests matter more here than for most tools, because
two of the API's query parameters cannot be named directly in Python:
`from` is a keyword. A translation layer that silently dropped a
parameter would give the model a plausible answer for the wrong window.
"""

import httpx

from q_core_mcp.client import QCoreClient
from q_core_mcp.server import build_server


def _server(test_settings, handler):
    client = QCoreClient(test_settings, transport=httpx.MockTransport(handler))
    return build_server(client)


def _ok(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        payload["path"] = request.url.path
        payload["params"] = dict(request.url.params)
        return httpx.Response(200, json={"items": [], "total": 0})

    return handler


def test_the_tool_is_registered_and_read_only(test_settings, tools_by_name):
    tools = tools_by_name(_server(test_settings, _ok({})))

    assert "list_due_items" in tools
    assert tools["list_due_items"].annotations.read_only_hint is True


def test_it_requests_due_with_only_paging_by_default(test_settings, call_tool):
    seen: dict = {}

    call_tool(_server(test_settings, _ok(seen)), "list_due_items", {})

    assert seen["path"] == "/due"
    # No `from`/`to`: the API owns the default window, and sending
    # today's date from here would pin the default at call time and make
    # the two definitions drift.
    #
    # Nothing at all now, for the same reason one step further: the API
    # owns the page size too. This asserted limit=50 and offset=0, which
    # pinned the API's default in the tool layer -- the same drift, in the
    # same file, about a different value.
    assert seen["params"] == {}


def test_from_and_to_are_translated_to_the_query_names(test_settings, call_tool):
    """`from` is a Python keyword, so the tool parameter is `from_date`.

    If the translation were dropped the call would still succeed — the
    API would apply its default window — and the model would get a
    confident answer about the wrong month.
    """
    seen: dict = {}

    call_tool(
        _server(test_settings, _ok(seen)),
        "list_due_items",
        {"from_date": "2026-10-01", "to_date": "2026-12-31"},
    )

    assert seen["params"]["from"] == "2026-10-01"
    assert seen["params"]["to"] == "2026-12-31"
    assert "from_date" not in seen["params"]
    assert "to_date" not in seen["params"]


def test_filters_it_was_not_given_are_omitted(test_settings, call_tool):
    """Sent as an explicit null, `source` would be an unknown source and
    the API would answer 422."""
    seen: dict = {}

    call_tool(_server(test_settings, _ok(seen)), "list_due_items", {})

    assert "source" not in seen["params"]
    assert "entity_id" not in seen["params"]


def test_filters_it_was_given_are_sent(test_settings, call_tool):
    seen: dict = {}

    call_tool(
        _server(test_settings, _ok(seen)),
        "list_due_items",
        {"source": "reminder", "entity_id": "e1", "limit": 10, "offset": 20},
    )

    assert seen["params"] == {
        "limit": "10",
        "offset": "20",
        "source": "reminder",
        "entity_id": "e1",
    }


def test_the_description_states_that_overdue_items_come_back(
    test_settings, tools_by_name
):
    """The one thing a model cannot infer from the response shape.

    Assuming a plain range query, a model would call with from=today,
    see nothing overdue in the window, and report "nothing is due" —
    the exact silence this endpoint exists to prevent. So the
    description has to say it, and this pins that it still does.
    """
    description = tools_by_name(_server(test_settings, _ok({})))[
        "list_due_items"
    ].description

    lowered = description.lower()
    assert "overdue" in lowered
    assert "negative" in lowered
    assert "reminder" in lowered

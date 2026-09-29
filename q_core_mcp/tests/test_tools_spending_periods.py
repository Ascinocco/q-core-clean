import httpx

from q_core_mcp.client import QCoreClient
from q_core_mcp.server import build_server


def test_spending_periods_forwards_the_entire_date_query(test_settings, call_tool):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"periods": []})

    server = build_server(QCoreClient(test_settings, transport=httpx.MockTransport(handler)))
    call_tool(server, "spending_periods", {"granularity": "week", "date_from": "2026-06-01", "date_to": "2026-06-07", "account_id": "acct"})
    assert seen == {"path": "/spending/periods", "params": {"granularity": "week", "date_from": "2026-06-01", "date_to": "2026-06-07", "account_id": "acct"}}


def test_spending_periods_is_read_only_and_explains_the_definition(test_settings, tools_by_name):
    server = build_server(QCoreClient(test_settings, transport=httpx.MockTransport(lambda _: httpx.Response(200, json={}))))
    tool = tools_by_name(server)["spending_periods"]
    assert tool.annotations.read_only_hint is True
    assert "transfers" in tool.description.lower()
    assert "merchant" in tool.description.lower()

import json

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


def test_list_transactions_passes_every_filter_it_was_given(test_settings, call_tool):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"items": [], "total": 0})

    call_tool(
        _server(test_settings, handler),
        "list_transactions",
        {
            "account_id": "acct",
            "entity_id": "car",
            "category_id": "cat",
            "date_from": "2026-01-01",
            "date_to": "2026-03-31",
        },
    )

    assert seen["path"] == "/transactions"
    assert seen["params"]["account_id"] == "acct"
    assert seen["params"]["entity_id"] == "car"
    assert seen["params"]["category_id"] == "cat"
    assert seen["params"]["date_from"] == "2026-01-01"
    assert seen["params"]["date_to"] == "2026-03-31"


def test_list_transactions_omits_filters_it_was_not_given(test_settings, call_tool):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"items": [], "total": 0})

    call_tool(_server(test_settings, handler), "list_transactions", {})

    assert seen["params"] == {}


def test_update_transaction_sends_only_supplied_fields(test_settings, call_tool):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.read())
        return httpx.Response(200, json={"id": "t1"})

    call_tool(
        _server(test_settings, handler),
        "update_transaction",
        {"transaction_id": "t1", "category_id": "groceries"},
    )

    assert seen["method"] == "PATCH"
    assert seen["path"] == "/transactions/t1"
    assert seen["body"] == {"category_id": "groceries"}


def test_transaction_tool_annotations(test_settings, tools_by_name):
    tools = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))

    assert tools["list_transactions"].annotations.read_only_hint is True
    update = tools["update_transaction"].annotations
    assert (update.read_only_hint if update else None) is not True


def test_update_transaction_description_says_it_is_correction_only(
    test_settings, tools_by_name
):
    # Acceptance criterion for behaviour 5 in the plan. Date, description
    # and amount come from the source statement and are immutable by
    # design; a model asked to "fix the amount" must learn it cannot,
    # rather than patching a different field and reporting success.
    tools = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))
    description = tools["update_transaction"].description.lower()

    assert "amount" in description
    assert "cannot" in description
    assert "apply_transaction_as_rule" in description


def test_round_trip_correction_sticks_and_leaves_the_rest_alone(
    test_settings, call_tool
):
    # Pins correction-only against the real API rather than the prose: the
    # category changes, and the imported facts do not.
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        client = QCoreClient(test_settings, transport=httpx.ASGITransport(app=app))
        account = call_tool(
            server, "create_entity", {"entity_type": "account", "name": "Chase"}
        )
        category = call_tool(server, "list_categories", {})["items"][0]

        from tests.source_import_support import seed_source_tool
        imported = seed_source_tool(call_tool, server, {
                    "account_id": account["id"],
                    "period_start": "2026-03-01",
                    "period_end": "2026-03-31",
                    "transactions": [
                        {
                            "txn_date": "2026-03-04",
                            "description": "ACME FUEL 41",
                            "amount_cents": -5210,
                        }
                    ],
                })
        txn = call_tool(server, "list_transactions", {"account_id": account["id"]})[
            "items"
        ][0]
        updated = call_tool(
            server,
            "update_transaction",
            {"transaction_id": txn["id"], "category_id": category["id"]},
        )
    finally:
        app.dependency_overrides.clear()

    assert imported["created"] == 1
    assert updated["category_id"] == category["id"]
    # The imported facts are untouched.
    assert updated["amount_cents"] == -5210
    assert updated["description"] == "ACME FUEL 41"
    assert updated["txn_date"] == "2026-03-04"


def test_round_trip_unknown_transaction_reports_it(test_settings, call_tool):
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        with pytest.raises(ToolError) as exc_info:
            call_tool(
                server,
                "update_transaction",
                {"transaction_id": "no-such-txn", "category_id": "x"},
            )
    finally:
        app.dependency_overrides.clear()

    assert "no-such-txn" in str(exc_info.value)


def test_update_transaction_refuses_the_immutable_fields(test_settings, call_tool):
    """The SDK drops unknown arguments before the tool runs.

    So `update_transaction(amount_cents=5)` used to send an empty PATCH and
    return 200 — a silent success for an operation the API refuses. The
    tool signature does not make the attempt unexpressible; it only makes
    it invisible. These parameters exist solely to be refused out loud.

    A description test would pass against the old behaviour, so this
    asserts the call raises.
    """
    server = _server(test_settings, lambda r: httpx.Response(200, json={"id": "t1"}))

    for field, value in (
        ("amount_cents", 5),
        ("txn_date", "2026-03-04"),
        ("description", "CORRECTED"),
    ):
        with pytest.raises(ToolError) as exc_info:
            call_tool(
                server, "update_transaction", {"transaction_id": "t1", field: value}
            )
        message = str(exc_info.value)
        assert field in message, field
        assert "preview_source_import" in message, field


@pytest.mark.parametrize(
    "field,value",
    [
        ("amount_cents", 5),
        ("txn_date", "2026-03-04"),
        ("description", "CORRECTED"),
    ],
)
def test_refusing_an_immutable_field_sends_no_patch(
    test_settings, call_tool, field, value
):
    """Asserts the recorded request, not just that something raised.

    The original bug's signature was a PATCH with body `{}` returning 200 —
    the argument was dropped, so nothing was sent and nothing complained.
    Recording the bodies is what distinguishes "refused before writing"
    from "wrote an empty patch and then complained", and parametrizing all
    three fields is what catches a half-fix that refuses the one field
    someone thought of.
    """
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.read() or b"{}"))
        return httpx.Response(200, json={"id": "t1"})

    server = _server(test_settings, handler)

    with pytest.raises(ToolError):
        call_tool(
            server,
            "update_transaction",
            {"transaction_id": "t1", field: value, "category_id": "groceries"},
        )

    assert bodies == [], f"refusing {field} must send no request at all"
    assert {} not in bodies, "an empty PATCH is the original bug's signature"


def test_a_normal_correction_still_works(test_settings, call_tool):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.read())
        return httpx.Response(200, json={"id": "t1"})

    call_tool(
        _server(test_settings, handler),
        "update_transaction",
        {"transaction_id": "t1", "category_id": "groceries"},
    )

    assert seen["body"] == {"category_id": "groceries"}

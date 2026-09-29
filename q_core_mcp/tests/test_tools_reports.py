
from tests.source_import_support import seed_source_tool
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


def test_spending_summary_passes_period_and_group_by(test_settings, call_tool):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"items": [], "total": 0})

    call_tool(
        _server(test_settings, handler),
        "spending_summary",
        {"period": "2026-03", "group_by": "entity"},
    )

    assert seen["path"] == "/spending_summary"
    assert seen["params"]["period"] == "2026-03"
    assert seen["params"]["group_by"] == "entity"


def test_trend_requests_the_right_path(test_settings, call_tool):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"items": [], "total": 0})

    call_tool(
        _server(test_settings, handler), "trend", {"category_id": "gro", "months": 12}
    )

    assert seen["path"] == "/trend"
    assert seen["params"]["category_id"] == "gro"
    assert seen["params"]["months"] == "12"
    assert "entity_id" not in seen["params"]


def test_cost_of_ownership_uses_the_entity_path(test_settings, call_tool):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"total": 0})

    call_tool(
        _server(test_settings, handler),
        "cost_of_ownership",
        {"entity_id": "car", "period": "2026"},
    )

    assert seen["path"] == "/entities/car/cost_of_ownership"
    assert seen["params"]["period"] == "2026"


def test_report_tools_are_read_only(test_settings, tools_by_name):
    tools = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))

    for name in ("spending_summary", "trend", "cost_of_ownership"):
        assert tools[name].annotations.read_only_hint is True, name


def test_spending_summary_description_warns_about_signed_sums(
    test_settings, tools_by_name
):
    # Acceptance criterion for behaviour 1.
    tools = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))
    description = tools["spending_summary"].description.lower()

    assert "signed" in description
    assert "transfer" in description


def test_trend_description_states_the_exactly_one_constraint(
    test_settings, tools_by_name
):
    # Acceptance criterion for behaviour 2.
    tools = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))

    assert "exactly one" in tools["trend"].description.lower()


def test_cost_of_ownership_description_warns_against_summing(
    test_settings, tools_by_name
):
    # Acceptance criterion for behaviour 4.
    tools = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))
    description = tools["cost_of_ownership"].description.lower()

    assert "double-count" in description or "double count" in description


def test_the_three_reports_describe_when_to_use_each(test_settings, tools_by_name):
    # These three are the most confusable tools in the surface — all
    # answer some form of "how much did X cost". Keeping them separate was
    # a deliberate choice over one mode-switched tool, which only pays off
    # if the descriptions actually distinguish them.
    tools = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))

    assert "spending_summary" in tools["trend"].description
    assert "per month" in tools["trend"].description.lower() or (
        "month-by-month" in tools["trend"].description.lower()
    )
    assert "one thing" in tools["cost_of_ownership"].description.lower()


def test_round_trip_trend_without_a_filter_is_rejected(test_settings, call_tool):
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        with pytest.raises(ToolError) as exc_info:
            call_tool(server, "trend", {})
    finally:
        app.dependency_overrides.clear()

    assert "exactly one" in str(exc_info.value).lower()


def test_round_trip_trend_with_both_filters_is_rejected(test_settings, call_tool):
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        entity = call_tool(
            server, "create_entity", {"entity_type": "vehicle", "name": "Corolla"}
        )
        category = call_tool(server, "list_categories", {})["items"][0]
        with pytest.raises(ToolError) as exc_info:
            call_tool(
                server,
                "trend",
                {"category_id": category["id"], "entity_id": entity["id"]},
            )
    finally:
        app.dependency_overrides.clear()

    assert "exactly one" in str(exc_info.value).lower()


def test_round_trip_transfers_net_to_zero_in_spending_summary(
    test_settings, call_tool
):
    # Demonstrates behaviour 1 rather than asserting the word "signed":
    # a card payment's two legs, both categorized as a transfer, sum to
    # zero instead of counting as 500 of spending. This is why the
    # description says to report these as net totals.
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        checking = call_tool(
            server, "create_entity", {"entity_type": "account", "name": "Checking"}
        )
        card = call_tool(
            server, "create_entity", {"entity_type": "account", "name": "Visa"}
        )
        seed_source_tool(call_tool, server, {
                "account_id": checking["id"],
                "period_start": "2026-03-01",
                "period_end": "2026-03-31",
                "transactions": [
                    {
                        "txn_date": "2026-03-15",
                        "description": "PAYMENT TO VISA",
                        "amount_cents": -50000,
                    }
                ],
            })
        seed_source_tool(call_tool, server, {
                "account_id": card["id"],
                "period_start": "2026-03-01",
                "period_end": "2026-03-31",
                "transactions": [
                    {
                        "txn_date": "2026-03-15",
                        "description": "PAYMENT RECEIVED",
                        "amount_cents": 50000,
                    }
                ],
            })
        for txn in call_tool(server, "list_transactions", {})["items"]:
            call_tool(
                server,
                "update_transaction",
                {
                    "transaction_id": txn["id"],
                    "category_id": "transfers_credit_card_payment",
                },
            )
        summary = call_tool(server, "spending_summary", {"period": "2026-03"})
        included = call_tool(
            server,
            "spending_summary",
            {"period": "2026-03", "exclude_transfers": False},
        )
    finally:
        app.dependency_overrides.clear()

    def bucket(payload):
        return [
            item
            for item in payload["items"]
            if item["key"] == "transfers_credit_card_payment"
        ]

    # exclude_transfers=False is the behaviour this test was written for
    # and it still holds: both legs land in one bucket and net to zero
    # rather than counting as 500 of spending.
    assert bucket(included), "both legs should be grouped under the transfer category"
    assert bucket(included)[0]["total"] == 0.0

    # The default now removes the bucket entirely (ticket T-40). Netting to
    # zero was only ever correct for a summary that sums across
    # categories; per-account and per-entity reads never netted, and a
    # transfer is not spending in either.
    assert not bucket(summary)

    # And this is exactly why the count exists alongside the cents: the
    # two legs sum to zero, so transfers_excluded_cents is 0 in the
    # healthy case and says nothing on its own.
    assert summary["transfers_excluded_cents"] == 0
    assert summary["transfers_excluded_count"] == 2


def test_round_trip_cost_of_ownership_for_an_unknown_entity_is_reported(
    test_settings, call_tool
):
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        with pytest.raises(ToolError) as exc_info:
            call_tool(server, "cost_of_ownership", {"entity_id": "no-such-entity"})
    finally:
        app.dependency_overrides.clear()

    assert "no-such-entity" in str(exc_info.value)


def test_spending_summary_description_explains_the_null_group(
    test_settings, tools_by_name
):
    # Found during Task 6's end-to-end run: transactions with no category
    # come back grouped under key null, not omitted and not errored. A
    # model that does not know will either report "None" as if it were a
    # category name or quietly drop the row — and dropping it makes the
    # totals not add up.
    tools = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))
    description = tools["spending_summary"].description.lower()

    assert "null" in description
    assert "uncategorized" in description


def test_round_trip_uncategorized_transactions_group_under_null(
    test_settings, call_tool
):
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        account = call_tool(
            server, "create_entity", {"entity_type": "account", "name": "Chase"}
        )
        seed_source_tool(call_tool, server, {
                "account_id": account["id"],
                "period_start": "2026-03-01",
                "period_end": "2026-03-31",
                "transactions": [
                    {
                        "txn_date": "2026-03-04",
                        "description": "UNKNOWN MERCHANT",
                        "amount_cents": -2000,
                    }
                ],
            })
        summary = call_tool(server, "spending_summary", {"period": "2026-03"})
    finally:
        app.dependency_overrides.clear()

    assert [item["key"] for item in summary["items"]] == [None]
    assert summary["items"][0]["total"] == -2000


def test_spending_summary_description_states_the_transfer_default(
    test_settings, tools_by_name
):
    """A model that does not know transfers are excluded will read the
    total as "everything", and the number is smaller than that. The
    default is not discoverable from the response, so it has to be in
    the description."""
    tools = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))
    description = tools["spending_summary"].description.lower()

    assert "exclude_transfers" in description
    assert "default" in description
    assert "transfers_excluded_count" in description


def test_spending_summary_description_says_why_the_count_matters(
    test_settings, tools_by_name
):
    """The cents field reads 0 whenever both legs are labelled, which is
    the healthy state — so a model told only about the cents will
    conclude nothing was excluded in exactly the case where something
    was."""
    tools = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))
    description = tools["spending_summary"].description

    assert "0 because two legs cancel" in description


def test_trend_description_states_the_explicit_category_exception(
    test_settings, tools_by_name
):
    """Without this a model asking about credit-card payments gets an
    empty series and reports "you made none"."""
    tools = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))
    description = tools["trend"].description.lower()

    assert "exclude_transfers" in description
    assert "by id" in description


def test_spending_summary_description_warns_against_reading_the_total_as_complete(
    test_settings, tools_by_name
):
    """Excluding transfers makes a total look authoritative. When most
    transfers are not categorized as transfers, the figure
    a model would confidently report is wrong by more than the amount it
    says it excluded."""
    tools = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))
    description = tools["spending_summary"].description.lower()

    assert "uncategorized_cents" in description
    assert "does not mean the rest is spending" in description


def test_spending_summary_description_names_both_null_fields(
    test_settings, tools_by_name
):
    """Which field name appears tells a model which null it is reading.
    A description naming only `uncategorized_cents` would leave an
    entity summary's `unattributed_cents` looking like a field the tool
    does not document — and a model that cannot place it will either
    ignore it or report it as uncategorized spending, which is the
    confusion D48 exists to remove."""
    tools = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))
    description = tools["spending_summary"].description

    assert "uncategorized_cents" in description
    assert "unattributed_cents" in description
    assert "not grouped" in description.lower()

def test_statement_coverage_requests_the_endpoint(test_settings, call_tool):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"statements": 0, "gaps": []})

    client = QCoreClient(test_settings, transport=httpx.MockTransport(handler))
    call_tool(build_server(client), "statement_coverage", {})

    assert seen["path"] == "/statements/coverage"
    assert seen["params"] == {}


def test_statement_coverage_description_forecloses_the_balance_check(
    test_settings, tools_by_name
):
    """The description has to say what the tool CANNOT do, because the
    stronger version is the one a reader invents next.

    A model (or a person) that sees "coverage is complete" will reach
    for balances to close the page-level hole, and a balance chain
    reconciles within the pages you have — it would pass on exactly the
    import it was built to catch. Saying so here is cheaper than
    rejecting the proposal a second time.
    """
    tools = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))
    description = tools["statement_coverage"].description.lower()

    assert "missing page" in description
    assert "balance" in description
    assert "stop" in description


def test_statement_coverage_description_distinguishes_gap_from_overlap(
    test_settings, tools_by_name
):
    """A gap is a stop; an overlap is expected. Conflating them makes
    the tool cry wolf on every re-export, and a report that cries wolf
    is one nobody reads."""
    tools = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))
    description = tools["statement_coverage"].description.lower()

    assert "gap" in description and "overlap" in description
    assert "re-export" in description


def test_cost_of_ownership_description_names_the_field_to_check(
    test_settings, tools_by_name
):
    """The warning not to sum is pinned elsewhere; this pins the part
    that makes it actionable.

    "Do not add these" leaves a model with no way to tell a safe pair
    from an unsafe one, so the natural question — "what do my cars cost
    me?" — still has no correct procedure. Naming the field it must read
    turns the warning into an instruction it can follow.
    """
    tools = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))
    description = tools["cost_of_ownership"].description

    assert "shared_cost_entities" in description
    assert "shared_with" in description
    assert "ended link still counts" in description.lower()

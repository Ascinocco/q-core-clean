
from tests.source_import_support import seed_source_tool
import httpx
import pytest
from pydantic import ValidationError
from mcp.server.mcpserver.exceptions import ToolError

from q_core_mcp.client import QCoreClient
from q_core_mcp.server import build_server


def _server(test_settings, handler):
    client = QCoreClient(test_settings, transport=httpx.MockTransport(handler))
    return build_server(client)


def _real_server(test_settings, app):
    client = QCoreClient(test_settings, transport=httpx.ASGITransport(app=app))
    return build_server(client)


def _import(server, call_tool, account_id, period, description, amount_cents=-5210):
    return seed_source_tool(call_tool, server, {
            "account_id": account_id,
            "period_start": f"{period}-01",
            "period_end": f"{period}-28",
            "transactions": [
                {
                    "txn_date": f"{period}-04",
                    "description": description,
                    "amount_cents": amount_cents,
                }
            ],
        })


def test_list_merchant_rules_requests_the_right_path(test_settings, call_tool):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"items": [], "total": 0})

    call_tool(_server(test_settings, handler), "list_merchant_rules", {})

    assert seen["path"] == "/merchant_rules"
    # See test_list_paging.py: this is the tool whose default call returned
    # only the first page of a larger rule set.
    assert seen["params"] == {}


def test_apply_as_rule_posts_to_the_nested_path(test_settings, call_tool):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json={"id": "r1"})

    call_tool(
        _server(test_settings, handler),
        "apply_transaction_as_rule",
        {"transaction_id": "t1", "actor": "test-actor"},
    )

    assert seen["method"] == "POST"
    assert seen["path"] == "/transactions/t1/apply_as_rule"


def test_rule_tool_annotations(test_settings, tools_by_name):
    tools = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))

    assert tools["list_merchant_rules"].annotations.read_only_hint is True
    apply_annotations = tools["apply_transaction_as_rule"].annotations
    assert (apply_annotations.read_only_hint if apply_annotations else None) is not True


def test_apply_description_says_to_correct_the_transaction_first(
    test_settings, tools_by_name
):
    # The API refuses a transaction with no category and no entity. A model
    # that calls this straight off an import gets a conflict it could have
    # avoided by reading the order of operations.
    tools = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))
    description = tools["apply_transaction_as_rule"].description

    assert "update_transaction" in description
    assert "after" in description.lower()


def test_apply_description_explains_the_already_exists_case(
    test_settings, tools_by_name
):
    # The API's own conflict message says to "edit that rule instead" — and
    # no rule-editing tool is exposed, by design. Without guidance a model
    # hits a dead end following an instruction it cannot carry out.
    tools = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))
    description = tools["apply_transaction_as_rule"].description.lower()

    assert "already" in description


def test_round_trip_rule_learned_once_matches_the_next_statement(
    test_settings, call_tool
):
    # The whole intake loop, end to end: import, correct, teach, re-import.
    # Every other test in this round covers a piece of this.
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        account = call_tool(
            server, "create_entity", {"entity_type": "account", "name": "Chase"}
        )
        category = call_tool(server, "list_categories", {})["items"][0]

        march = _import(server, call_tool, account["id"], "2026-03", "ACME FUEL 41")
        assert len(march["unmatched"]) == 1, "no rules yet, so nothing should match"

        call_tool(
            server,
            "update_transaction",
            {
                "transaction_id": march["unmatched"][0]["id"],
                "category_id": category["id"],
            },
        )
        rule = call_tool(
            server,
            "apply_transaction_as_rule",
            {"transaction_id": march["unmatched"][0]["id"], "actor": "test-actor"},
        )

        april = _import(server, call_tool, account["id"], "2026-04", "ACME FUEL 41")
        rules = call_tool(server, "list_merchant_rules", {})
    finally:
        app.dependency_overrides.clear()

    assert rule["category_id"] == category["id"]
    assert april["unmatched"] == [], "the learned rule should have matched"
    assert rules["total"] == 1


def test_round_trip_applying_an_uncorrected_transaction_is_refused(
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
        imported = _import(server, call_tool, account["id"], "2026-03", "ACME FUEL 41")
        with pytest.raises(ToolError) as exc_info:
            call_tool(
                server,
                "apply_transaction_as_rule",
                {"transaction_id": imported["unmatched"][0]["id"], "actor": "test-actor"},
            )
    finally:
        app.dependency_overrides.clear()

    assert "correct it first" in str(exc_info.value)


def test_round_trip_a_learned_rule_does_not_cover_a_different_store_number(
    test_settings, call_tool
):
    """The limitation the description has to be honest about.

    Rules match by substring: the learned pattern must appear inside the
    incoming description. apply_transaction_as_rule learns the transaction's
    FULL description, so a rule from "ACME FUEL 41" does not match
    "ACME FUEL 99" — a different store of the same chain. A model that
    reports "Acme Fuel is now categorized" is wrong the next time a
    different location appears, which is why the description says what was
    actually learned rather than claiming the merchant is handled.
    """
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        account = call_tool(
            server, "create_entity", {"entity_type": "account", "name": "Chase"}
        )
        category = call_tool(server, "list_categories", {})["items"][0]

        march = _import(server, call_tool, account["id"], "2026-03", "ACME FUEL 41")
        call_tool(
            server,
            "update_transaction",
            {
                "transaction_id": march["unmatched"][0]["id"],
                "category_id": category["id"],
            },
        )
        call_tool(
            server,
            "apply_transaction_as_rule",
            {"transaction_id": march["unmatched"][0]["id"], "actor": "test-actor"},
        )

        same_store = _import(server, call_tool, account["id"], "2026-04", "ACME FUEL 41")
        other_store = _import(
            server, call_tool, account["id"], "2026-05", "ACME FUEL 99"
        )
    finally:
        app.dependency_overrides.clear()

    assert same_store["unmatched"] == [], "the exact description should match"
    assert len(other_store["unmatched"]) == 1, (
        "a different store number is a different description, so it does not match"
    )


def test_apply_description_is_honest_about_what_was_learned(
    test_settings, tools_by_name
):
    # Guards the wording fix: the description must not let a model report
    # that a merchant in general is now handled.
    tools = tools_by_name(_server(test_settings, lambda r: httpx.Response(200, json={})))
    description = tools["apply_transaction_as_rule"].description.lower()

    assert "exact description" in description
    assert "store number" in description or "location" in description


# --- create_merchant_rule (ticket T-65) ----------------------------


def _recording(payload=None):
    seen = {}

    def handler(request):
        import json as jsonlib

        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = jsonlib.loads(request.content) if request.content else None
        return httpx.Response(200, json=payload if payload is not None else {"id": "r1"})

    return seen, handler


def test_create_merchant_rule_posts_pattern_and_category(test_settings, call_tool):
    seen, handler = _recording()
    call_tool(
        _server(test_settings, handler),
        "create_merchant_rule",
        {"pattern": "MAPLE DONUTS", "category_id": "food_coffee", "actor": "test-actor"},
    )
    assert (seen["method"], seen["path"]) == ("POST", "/merchant_rules")
    assert seen["body"] == {
        "pattern": "MAPLE DONUTS", "category_id": "food_coffee",
        "actor": "test-actor",
    }


def test_create_merchant_rule_omits_what_it_was_not_given(test_settings, call_tool):
    """The body pins only what the caller set.

    Not because null and absent differ to the API — measured, they do not:
    both fields are `str | None = None`, so an explicit null takes the same
    branch as omission everywhere, including the at-least-one check. The
    assertion is still worth having, because it is what stops the tool
    growing a habit of sending keys nobody asked for; only the reason needed
    correcting.
    """
    seen, handler = _recording()
    call_tool(
        _server(test_settings, handler),
        "create_merchant_rule",
        {"pattern": "UTILITYCO", "entity_id": "e1", "actor": "test-actor"},
    )
    assert seen["body"] == {
        "pattern": "UTILITYCO", "entity_id": "e1", "actor": "test-actor",
    }
    assert "category_id" not in seen["body"]


def test_create_merchant_rule_sends_both_when_given_both(test_settings, call_tool):
    seen, handler = _recording()
    call_tool(
        _server(test_settings, handler),
        "create_merchant_rule",
        {"pattern": "TOYOTA", "category_id": "auto_fuel", "entity_id": "e1", "actor": "test-actor"},
    )
    assert seen["body"] == {
        "pattern": "TOYOTA",
        "category_id": "auto_fuel",
        "entity_id": "e1",
        "actor": "test-actor",
    }


def test_create_merchant_rule_is_a_write_not_a_delete(test_settings, tools_by_name):
    seen, handler = _recording()
    annotations = tools_by_name(_server(test_settings, handler))[
        "create_merchant_rule"
    ].annotations
    assert not (annotations and annotations.read_only_hint)
    assert not (annotations and annotations.destructive_hint)


def test_create_merchant_rule_description_states_the_precedence_rule(
    test_settings, tools_by_name
):
    """Longest pattern wins is the whole reason a broad rule is safe to
    create: a later, more specific rule refines it without editing it. A
    caller who does not know that writes narrow patterns defensively, which
    is the behaviour that leaves Uncategorized plateauing."""
    seen, handler = _recording()
    description = tools_by_name(_server(test_settings, handler))[
        "create_merchant_rule"
    ].description.lower()
    assert "longest" in description
    assert "merchant-rules-conventions" in description


def test_create_merchant_rule_description_distinguishes_it_from_the_narrow_path(
    test_settings, tools_by_name
):
    """The two rule-making tools are confusable and do different jobs. This
    one takes a generalized pattern; apply_transaction_as_rule learns an
    exact description. A model that reaches for the wrong one gets a rule
    that never matches again."""
    seen, handler = _recording()
    description = tools_by_name(_server(test_settings, handler))[
        "create_merchant_rule"
    ].description.lower()
    assert "apply_transaction_as_rule" in description
    assert "exact" in description


# --- round trips -------------------------------------------------------


def test_round_trip_create_merchant_rule_then_list_it(test_settings, call_tool):
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        category = call_tool(server, "list_categories", {"limit": 1})["items"][0]
        created = call_tool(
            server,
            "create_merchant_rule",
            {"pattern": "MAPLE DONUTS", "category_id": category["id"], "actor": "test-actor"},
        )
        listed = call_tool(server, "list_merchant_rules", {"limit": 200})
    finally:
        app.dependency_overrides.clear()

    assert created["pattern"] == "MAPLE DONUTS"
    assert created["category_id"] == category["id"]
    assert created["id"] in [rule["id"] for rule in listed["items"]]


def test_round_trip_a_rule_with_neither_target_is_refused(test_settings, call_tool):
    """A rule that sets neither a category nor an entity classifies nothing.
    The API refuses it; the tool must surface that rather than create a rule
    that silently does nothing on every future import."""
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        with pytest.raises(ToolError) as excinfo:
            call_tool(
                server,
                "create_merchant_rule",
                {"pattern": "NAKED", "actor": "test-actor"},
            )
    finally:
        app.dependency_overrides.clear()

    message = str(excinfo.value).lower()
    assert "category_id" in message or "entity_id" in message


def test_round_trip_an_unknown_category_is_refused(test_settings, call_tool):
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        with pytest.raises(ToolError) as excinfo:
            call_tool(
                server,
                "create_merchant_rule",
                {"pattern": "X", "category_id": "not-a-category", "actor": "test-actor"},
            )
    finally:
        app.dependency_overrides.clear()

    assert "category" in str(excinfo.value).lower()


def test_round_trip_a_broad_rule_matches_a_varying_description(
    test_settings, call_tool
):
    """The point of the whole ticket, end to end.

    A broad pattern must categorize future imports whose descriptions carry
    store numbers and locations — the case apply_transaction_as_rule cannot
    reach, because it learns one exact string. If this fails, Uncategorized
    cannot trend toward zero and the tool has not solved what it was built
    for.
    """
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        account = call_tool(
            server, "create_entity", {"entity_type": "account", "name": "Visa"}
        )
        category = call_tool(server, "list_categories", {"limit": 1})["items"][0]
        call_tool(
            server,
            "create_merchant_rule",
            {"pattern": "MAPLE DONUTS", "category_id": category["id"], "actor": "test-actor"},
        )
        _import(
            server,
            call_tool,
            account["id"],
            "2026-03",
            "MAPLE DONUTS #4821 ANYTOWN XY",
        )
        transactions = call_tool(
            server, "list_transactions", {"account_id": account["id"]}
        )
    finally:
        app.dependency_overrides.clear()

    assert transactions["items"][0]["category_id"] == category["id"], (
        "a broad rule did not match a description carrying a store number — "
        "which is the exact gap this tool exists to close"
    )


# --- update_merchant_rule (a todo / ticket T-66) ------------------------


def test_update_merchant_rule_patches_the_rule_path(test_settings, call_tool):
    seen, handler = _recording()
    call_tool(
        _server(test_settings, handler),
        "update_merchant_rule",
        {"rule_id": "r1", "category_id": "food_groceries", "actor": "test-actor"},
    )
    assert (seen["method"], seen["path"]) == ("PATCH", "/merchant_rules/r1")
    assert seen["body"] == {"category_id": "food_groceries", "actor": "test-actor"}


def test_update_merchant_rule_has_no_pattern_parameter(test_settings, tools_by_name):
    """The refusal is STRUCTURAL, not prose.

    A pattern decides precedence -- longest matching pattern wins, ties to
    the lowest id -- so an editable pattern is how precedence gets fought
    by hand. Stating that in the description and accepting the argument
    anyway would leave the rule enforced only by a model's willingness to
    read. There is no parameter to pass.
    """
    schema = tools_by_name(_server(test_settings, _recording()[1]))[
        "update_merchant_rule"
    ].input_schema
    assert "pattern" not in schema["properties"], schema["properties"].keys()
    # `actor` joins them: every writer records who made the change.
    # `clear` (ticket T-17) unsets a field rather than setting one, and does
    # NOT reopen this door: naming pattern in it sends {"pattern": null},
    # which the API refuses because the column is NOT NULL. So a pattern
    # can be neither set nor cleared here, which is the property this test
    # exists for -- asserted below rather than assumed.
    assert set(schema["properties"]) == {
        "rule_id", "actor", "category_id", "entity_id", "clear",
    }


def test_clear_does_not_reopen_the_pattern_door(test_settings, call_tool):
    """`clear` takes arbitrary field names, so it could have been a way
    back to editing a pattern. It is not: the API refuses to clear a NOT
    NULL column by name, so the structural refusal above still holds."""
    from api.models import MerchantRuleUpdate

    assert "pattern" not in MerchantRuleUpdate.CLEARABLE
    with pytest.raises(ValidationError) as excinfo:
        MerchantRuleUpdate(actor="t", pattern=None, category_id="food_dining")
    assert "cannot clear" in str(excinfo.value)
    assert "pattern" in str(excinfo.value)


def test_update_merchant_rule_with_neither_target_is_refused(test_settings, call_tool):
    """An update that sets neither would report success and change nothing.

    Refused in the tool rather than left to the API, so nothing is sent --
    the same shape as create_merchant_rule's at-least-one check.
    """
    seen, handler = _recording()
    with pytest.raises(ToolError) as excinfo:
        call_tool(
            _server(test_settings, handler),
            "update_merchant_rule",
            {"rule_id": "r1", "actor": "test-actor"},
        )
    assert "at least one" in str(excinfo.value)
    assert seen == {}, "nothing should have been sent"


def test_update_merchant_rule_omits_what_it_was_not_given(test_settings, call_tool):
    seen, handler = _recording()
    call_tool(
        _server(test_settings, handler),
        "update_merchant_rule",
        {"rule_id": "r1", "entity_id": "e9", "actor": "test-actor"},
    )
    assert seen["body"] == {"entity_id": "e9", "actor": "test-actor"}
    assert "category_id" not in seen["body"]


def test_update_description_explains_why_there_is_no_pattern(
    test_settings, tools_by_name
):
    description = tools_by_name(_server(test_settings, _recording()[1]))[
        "update_merchant_rule"
    ].description
    assert "no `pattern` parameter" in description.lower().replace("there is deliberately ", "")
    assert "precedence" in description.lower()


def test_update_description_explains_why_it_is_not_delete_and_recreate(
    test_settings, tools_by_name
):
    """The tie-break reason, pinned so it is not re-litigated.

    Delete-and-recreate assigns a new id, and since #80 ties between
    equal-length patterns are broken by lowest id -- so a new id can
    silently change which OTHER rule wins a tie. That is the reason PATCH
    is the shape, and a reason that is not written down gets re-argued.
    """
    description = tools_by_name(_server(test_settings, _recording()[1]))[
        "update_merchant_rule"
    ].description.lower()
    assert "new id" in description
    assert "tie" in description


def test_update_description_says_it_does_not_recategorize_the_past(
    test_settings, tools_by_name
):
    """Category is written at import time, so correcting a rule is forward-only."""
    description = tools_by_name(_server(test_settings, _recording()[1]))[
        "update_merchant_rule"
    ].description.lower()
    assert "not recategorize" in description or "does not recategorize" in description


def test_apply_description_now_points_at_the_correction_tool(
    test_settings, tools_by_name
):
    """It used to say correcting a wrong rule was impossible. It isn't now.

    A tool description that tells a model an action cannot be done is a
    dead end it will honour -- so this has to move in the same commit as
    the tool that makes it possible.
    """
    description = tools_by_name(_server(test_settings, _recording()[1]))[
        "apply_transaction_as_rule"
    ].description
    assert "update_merchant_rule" in description
    assert "not possible" not in description


def test_round_trip_correcting_a_rule_changes_the_next_import(test_settings, call_tool):
    """The behaviour, end to end against the real API."""
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        account = call_tool(server, "create_entity", {
            "entity_type": "account", "name": "Card", "attributes": {"last4": "1234"}})
        rule = call_tool(server, "create_merchant_rule", {
            "pattern": "ZORBLAX", "category_id": "food_dining",
            "actor": "test-actor"})

        call_tool(server, "update_merchant_rule", {
            "rule_id": rule["id"], "category_id": "food_groceries",
            "actor": "test-actor"})

        _import(server, call_tool, account["id"], "2026-05", "ZORBLAX COFFEE #1")
        rows = call_tool(server, "list_transactions", {"account_id": account["id"]})
        assert [r["category_id"] for r in rows["items"]] == ["food_groceries"]
    finally:
        app.dependency_overrides.clear()


# --- reapply_merchant_rules (ticket T-51) ---------------------------------


def test_reapply_defaults_to_dry_run(test_settings, call_tool):
    """The default must be the safe one: a caller asks for the write."""
    seen, handler = _recording()
    call_tool(_server(test_settings, handler), "reapply_merchant_rules", {})
    assert (seen["method"], seen["path"]) == ("POST", "/merchant_rules/reapply")
    assert seen["body"] == {"dry_run": True}


def test_reapply_is_annotated_destructive(test_settings, tools_by_name):
    """Static hint, conditional behaviour -- so it describes the worst case.

    dry_run defaults true, but the annotation cannot vary per call.
    Labelling it by the safe default would attach the honest label to the
    call that writes.
    """
    annotations = tools_by_name(_server(test_settings, _recording()[1]))[
        "reapply_merchant_rules"
    ].annotations
    assert annotations.destructive_hint is True


def test_reapply_description_states_it_never_overwrites(test_settings, tools_by_name):
    description = tools_by_name(_server(test_settings, _recording()[1]))[
        "reapply_merchant_rules"
    ].description
    assert "NEVER OVERWRITES" in description
    assert "not retroactively" in description.lower()


def test_reapply_description_says_there_is_no_undo(test_settings, tools_by_name):
    """The id list is the only record of what moved."""
    description = tools_by_name(_server(test_settings, _recording()[1]))[
        "reapply_merchant_rules"
    ].description.lower()
    assert "no undo" in description
    assert "id" in description


def test_round_trip_reapply_classifies_rows_imported_before_the_rule(
    test_settings, call_tool
):
    from api.config import get_settings
    from api.main import app

    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = _real_server(test_settings, app)
        account = call_tool(server, "create_entity", {
            "entity_type": "account", "name": "Card", "attributes": {"last4": "1234"}})
        _import(server, call_tool, account["id"], "2026-05", "ZORBLAX COFFEE #1")
        # Rule arrives after the import.
        call_tool(server, "create_merchant_rule", {
            "actor": "test", "pattern": "ZORBLAX", "category_id": "food_dining"})

        preview = call_tool(server, "reapply_merchant_rules", {})
        assert preview["dry_run"] is True and preview["changed"] == 1
        rows = call_tool(server, "list_transactions", {"account_id": account["id"]})
        assert rows["items"][0]["category_id"] is None, "dry run wrote something"

        applied = call_tool(server, "reapply_merchant_rules", {"dry_run": False})
        assert applied["changed"] == 1
        rows = call_tool(server, "list_transactions", {"account_id": account["id"]})
        assert rows["items"][0]["category_id"] == "food_dining"
    finally:
        app.dependency_overrides.clear()

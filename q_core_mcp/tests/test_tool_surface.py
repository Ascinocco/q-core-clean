"""Whole-surface assertions: what is registered, not what each tool does.

These exist because the per-tool tests cannot catch a tool going missing.
Every other test names the tool it exercises, so a registration that is
dropped — by a bad merge resolution, a decorator left orphaned above a
conflict marker, an early return in register_tools — takes its own tests
out of the run with it and the suite stays green. `mcp/q_core_mcp/tools.py`
has already had two additive merge conflicts at exactly the point where
new tools are appended.
"""

import httpx

from q_core_mcp.client import QCoreClient
from q_core_mcp.server import build_server

#: THE STATE, not the transition: each entry records the RAW hint --
#: `True`, `False`, or `None` when the annotation does not set it -- and
#: `test_every_tool_carries_the_annotations_its_risk_implies` compares
#: with `is`. `None` and `False` are different answers here and must not
#: collapse.
#:
#: They used to. The comparison read both hints through `bool(...)`, and
#: `bool(None)` is `False`, so a MISSING annotation was certified against
#: a table entry saying `False`. That is not a harmless equivalence: the
#: MCP spec reads a missing `destructive_hint` as **true**, so the table
#: asserted "not destructive" about exactly the tools a client treats as
#: most dangerous.
#:
#: It was measured, not reasoned about. `update_relationship` arrived
#: unannotated on the composed tree and this test PASSED it while the new
#: annotation guard failed naming it -- the table agreeing with the code
#: about the opposite of what ships.
#:
#: The previous version of this comment described that conflation in the
#: PAST tense while the line four below it still had it. A comment
#: naming a transition ("this used to...") outlives the change it
#: describes and reads as settled; a comment naming the state is
#: falsifiable against the code beneath it.
EXPECTED_TOOLS = {
    "create_artifact": {"read_only": None, "destructive": False},
    "create_review_item": {"read_only": None, "destructive": False},
    "revise_artifact": {"read_only": None, "destructive": True},
    "edit_artifact": {"read_only": None, "destructive": True},
    "edit_diagram": {"read_only": None, "destructive": True},
    "link_artifact": {"read_only": None, "destructive": False},
    "unlink_artifact": {"read_only": None, "destructive": True},
    "list_artifact_links": {"read_only": True, "destructive": None},
    "revise_review_item": {"read_only": None, "destructive": True},
    "get_artifact": {"read_only": True, "destructive": None},
    "list_artifacts": {"read_only": True, "destructive": None},
    "get_review_item": {"read_only": True, "destructive": None},
    "list_review_items": {"read_only": True, "destructive": None},
    "list_review_history": {"read_only": True, "destructive": None},
    "get_briefing_window": {"read_only": True, "destructive": None},
    "list_reminder_completions": {"read_only": True, "destructive": None},
    "list_expected_payments": {"read_only": True, "destructive": None},
    "list_reminder_calendar_links": {"read_only": True, "destructive": None},
    "get_forecast_plan": {"read_only": True, "destructive": None},
    "get_cash_forecast": {"read_only": True, "destructive": None},
    "replace_forecast_plan": {"read_only": None, "destructive": True},
    # documents
    "extract_document_text": {"read_only": True, "destructive": None},
    "extract_registered_document_text": {"read_only": True, "destructive": None},
    "register_document": {"read_only": None, "destructive": False},
    "get_document": {"read_only": True, "destructive": None},
    "list_documents": {"read_only": True, "destructive": None},
    "delete_document": {"read_only": None, "destructive": True},
    # reminders
    "update_reminder": {"read_only": None, "destructive": True},
    # notes
    "create_note": {"read_only": None, "destructive": False},
    "get_note": {"read_only": True, "destructive": None},
    "list_notes": {"read_only": True, "destructive": None},
    "update_note": {"read_only": None, "destructive": True},
    "delete_note": {"read_only": None, "destructive": True},
    "link_note": {"read_only": None, "destructive": False},
    "unlink_note": {"read_only": None, "destructive": True},
    "create_reminder": {"read_only": None, "destructive": False},
    "get_reminder": {"read_only": True, "destructive": None},
    "list_reminders": {"read_only": True, "destructive": None},
    "delete_reminder": {"read_only": None, "destructive": True},
    "complete_reminder": {"read_only": None, "destructive": False},
    "snooze_reminder": {"read_only": None, "destructive": False},
    # Google Calendar is a one-way delivery projection of reminders.
    "google_calendar_status": {"read_only": True, "destructive": None},
    "connect_google_calendar": {"read_only": None, "destructive": False},
    "sync_google_calendar": {"read_only": None, "destructive": False},
    # due
    "statement_coverage": {"read_only": True, "destructive": None},
    "list_due_items": {"read_only": True, "destructive": None},
    # read
    "get_entity": {"read_only": True, "destructive": None},
    "list_entities": {"read_only": True, "destructive": None},
    "list_relationships": {"read_only": True, "destructive": None},
    # write
    "create_entity": {"read_only": None, "destructive": False},
    "update_entity": {"read_only": None, "destructive": True},
    "create_relationship": {"read_only": None, "destructive": False},
    "get_relationship": {"read_only": True, "destructive": None},
    "update_relationship": {"read_only": None, "destructive": True},
    # financial — reads
    "list_categories": {"read_only": True, "destructive": None},
    "list_statements": {"read_only": True, "destructive": None},
    "correct_statement_period": {"read_only": None, "destructive": True},
    "reassign_transaction_statement": {"read_only": None, "destructive": True},
    "preview_source_import": {"read_only": True, "destructive": None},
    "commit_source_import": {"read_only": None, "destructive": False},
    "get_financial_correction": {"read_only": True, "destructive": None},
    "list_transactions": {"read_only": True, "destructive": None},
    "update_transaction": {"read_only": None, "destructive": True},
    # archive: destructive_hint on the archiving pair, not on the
    # reversals -- putting rows back into a total takes nothing away.
    "archive_statement": {"read_only": None, "destructive": True},
    "unarchive_statement": {"read_only": None, "destructive": False},
    "archive_transaction": {"read_only": None, "destructive": True},
    "unarchive_transaction": {"read_only": None, "destructive": False},
    "list_merchant_rules": {"read_only": True, "destructive": None},
    "merchant_rule_history": {"read_only": True, "destructive": None},
    "create_merchant_rule": {"read_only": None, "destructive": False},
    # Not destructive: it edits classification in place and cannot change
    # a pattern, so it cannot alter precedence or remove a rule.
    "update_merchant_rule": {"read_only": None, "destructive": True},


    # Destructive by annotation though dry_run defaults true: a static
    # hint must describe the worst the tool can do, not its default.
    "reapply_merchant_rules": {"read_only": None, "destructive": True},
    "apply_transaction_as_rule": {"read_only": None, "destructive": False},
    "spending_periods": {"read_only": True, "destructive": None},
    "spending_summary": {"read_only": True, "destructive": None},
    "trend": {"read_only": True, "destructive": None},
    "cost_of_ownership": {"read_only": True, "destructive": None},
    # jyra — boards
    "get_board": {"read_only": True, "destructive": None},
    "list_boards": {"read_only": True, "destructive": None},
    "create_board": {"read_only": None, "destructive": False},
    # Both board WRITES are destructive, and update_board is the one that
    # needed deciding. Renaming a board overwrites a value the caller may
    # not have meant to lose, which is #135's line: an overwrite is not an
    # additive write. Four destructive update_* tools plus one exception
    # would read as an oversight, and the next person "fixes" it in
    # whichever direction they guess.
    "update_board": {"read_only": None, "destructive": True},
    # delete_board removes the board itself. The API refuses one that
    # still has tickets, but the annotation describes the worst the tool
    # can do, not the case the API happens to block -- the reasoning
    # already written down for reapply_merchant_rules.
    "delete_board": {"read_only": None, "destructive": True},
    # jyra — reads
    "get_ticket": {"read_only": True, "destructive": None},
    "list_tickets": {"read_only": True, "destructive": None},
    "get_ticket_history": {"read_only": True, "destructive": None},
    # jyra — ticket writes
    "create_ticket": {"read_only": None, "destructive": False},
    "update_ticket": {"read_only": None, "destructive": True},
    # jyra — attachments
    "attach_file": {"read_only": None, "destructive": False},
    # Reads an attachment BY ID; there is no path to open (ticket T-44).
    "read_attachment": {"read_only": True, "destructive": None},
    "list_attachments": {"read_only": True, "destructive": None},
    # jyra — status
    "transition_ticket": {"read_only": None, "destructive": False},
    "claim_ticket": {"read_only": None, "destructive": False},
    # destructive
    "delete_entity": {"read_only": None, "destructive": True},
    "delete_ticket": {"read_only": None, "destructive": True},
    "delete_attachment": {"read_only": None, "destructive": True},
    "delete_relationship": {"read_only": None, "destructive": True},
}


def _server(test_settings):
    client = QCoreClient(
        test_settings,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
    )
    return build_server(client)


def test_exactly_the_expected_tools_are_registered(test_settings, tools_by_name):
    assert sorted(tools_by_name(_server(test_settings))) == sorted(EXPECTED_TOOLS)


def test_every_tool_carries_the_annotations_its_risk_implies(
    test_settings, tools_by_name
):
    tools = tools_by_name(_server(test_settings))

    for name, expected in EXPECTED_TOOLS.items():
        annotations = tools[name].annotations
        # The RAW hint, not bool(...). `bool(None)` is False, so the old
        # form read a MISSING annotation as an explicit "not destructive"
        # and certified it against a table saying False. Measured on the
        # composed tree: `update_relationship` arrived unannotated and
        # this test passed it, while every client reads a missing
        # destructive_hint as TRUE -- the table agreeing with the code
        # about the opposite of what ships.
        read_only = annotations.read_only_hint if annotations else None
        destructive = annotations.destructive_hint if annotations else None
        assert read_only is expected["read_only"], (name, read_only)
        assert destructive is expected["destructive"], (name, destructive)


def test_every_tool_has_a_usable_description(test_settings, tools_by_name):
    # A tool with no description, or one still carrying an unformatted
    # placeholder, is worse than useless: the model picks it on the
    # strength of its name alone.
    tools = tools_by_name(_server(test_settings))

    for name, tool in tools.items():
        assert tool.description, name
        assert len(tool.description) > 40, name
        for placeholder in ("{types}", "{statuses}", "{relationship_types}"):
            assert placeholder not in tool.description, (name, placeholder)


def test_every_tool_exposes_its_parameters(test_settings, tools_by_name):
    tools = tools_by_name(_server(test_settings))

    required_params = {
        "get_entity": ["entity_id"],
        "create_entity": ["entity_type", "name"],
        "update_entity": ["entity_id"],
        "delete_entity": ["entity_id"],
        "create_relationship": ["entity_id", "to_entity_id", "relationship_type"],
        "list_relationships": ["entity_id"],
        "delete_relationship": ["relationship_id"],
    }
    for name, params in required_params.items():
        properties = tools[name].input_schema.get("properties", {})
        for param in params:
            assert param in properties, (name, param)

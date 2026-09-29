"""The reminder tools.

Mostly request-shaping: five of the six translate arguments into a body
or a path, and a dropped argument produces a successful call with the
wrong effect rather than an error. `due_date` is the one that matters
most — dropping it from complete_reminder would be a 422, but sending
the WRONG one silently completes a different occurrence.
"""

import json

import httpx

from q_core_mcp.client import QCoreClient
from q_core_mcp.server import build_server

REMINDER_TOOLS = (
    "create_reminder",
    "get_reminder",
    "list_reminders",
    "delete_reminder",
    "complete_reminder",
    "snooze_reminder",
)


def _server(test_settings, handler):
    client = QCoreClient(test_settings, transport=httpx.MockTransport(handler))
    return build_server(client)


def _capture(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        seen["body"] = (
            json.loads(request.content) if request.content else None
        )
        return httpx.Response(200, json={"id": "r1"})

    return handler


def test_all_six_tools_are_registered(test_settings, tools_by_name):
    names = tools_by_name(_server(test_settings, _capture({})))

    assert set(REMINDER_TOOLS) <= set(names)


def test_only_delete_is_marked_destructive(test_settings, tools_by_name):
    """Completing or snoozing an occurrence is reversible — the other
    call overwrites it. Deleting a reminder takes the whole series and
    every record of its occurrences, and nothing brings it back."""
    tools = tools_by_name(_server(test_settings, _capture({})))

    assert tools["delete_reminder"].annotations.destructive_hint is True
    for name in ("create_reminder", "complete_reminder", "snooze_reminder"):
        annotations = tools[name].annotations
        assert annotations is None or not annotations.destructive_hint, name


def test_create_sends_only_the_fields_it_was_given(test_settings, call_tool):
    """end_date without a recurrence_rule is a 422, so sending it as an
    explicit null would turn every plain one-off into an error."""
    seen: dict = {}

    call_tool(
        _server(test_settings, _capture(seen)),
        "create_reminder",
        {"title": "Renew passport", "start_date": "2026-10-01"},
    )

    assert seen["method"] == "POST"
    assert seen["path"] == "/reminders"
    assert seen["body"] == {"title": "Renew passport", "start_date": "2026-10-01"}


def test_create_passes_the_recurrence_fields_through(test_settings, call_tool):
    seen: dict = {}

    call_tool(
        _server(test_settings, _capture(seen)),
        "create_reminder",
        {
            "title": "Bins",
            "start_date": "2026-10-01",
            "recurrence_rule": "FREQ=WEEKLY",
            "end_date": "2026-12-31",
            "entity_id": "e1",
            "notes": "green bin",
        },
    )

    assert seen["body"] == {
        "title": "Bins",
        "start_date": "2026-10-01",
        "recurrence_rule": "FREQ=WEEKLY",
        "end_date": "2026-12-31",
        "entity_id": "e1",
        "notes": "green bin",
    }


def test_complete_sends_the_occurrence_it_was_given(test_settings, call_tool):
    """A wrong or missing due_date completes a different occurrence of a
    recurring reminder, which looks like success."""
    seen: dict = {}

    call_tool(
        _server(test_settings, _capture(seen)),
        "complete_reminder",
        {"reminder_id": "r1", "due_date": "2026-10-08"},
    )

    assert seen["method"] == "POST"
    assert seen["path"] == "/reminders/r1/complete"
    assert seen["body"] == {"due_date": "2026-10-08"}


def test_snooze_sends_both_dates(test_settings, call_tool):
    seen: dict = {}

    call_tool(
        _server(test_settings, _capture(seen)),
        "snooze_reminder",
        {"reminder_id": "r1", "due_date": "2026-10-08", "snoozed_to": "2026-10-15"},
    )

    assert seen["path"] == "/reminders/r1/snooze"
    assert seen["body"] == {"due_date": "2026-10-08", "snoozed_to": "2026-10-15"}


def test_list_omits_the_filter_it_was_not_given(test_settings, call_tool):
    seen: dict = {}

    call_tool(_server(test_settings, _capture(seen)), "list_reminders", {})

    assert seen["path"] == "/reminders"
    # Empty, not {"limit": "50", "offset": "0"}: the caller named no page,
    # so the API's default governs and the tool states no opinion.
    assert seen["params"] == {}


def test_delete_uses_the_delete_method(test_settings, call_tool):
    seen: dict = {}

    call_tool(
        _server(test_settings, _capture(seen)), "delete_reminder", {"reminder_id": "r1"}
    )

    assert seen["method"] == "DELETE"
    assert seen["path"] == "/reminders/r1"


def test_list_reminders_points_at_list_due_items(test_settings, tools_by_name):
    """The confusion this tool invites is a model calling it to answer
    "what's due" — it would get one row per repeating reminder however
    many times it fires, and nothing at all for renewals held as entity
    attributes. A confident wrong answer, so the description has to
    redirect and this pins that it still does."""
    description = tools_by_name(_server(test_settings, _capture({})))[
        "list_reminders"
    ].description

    assert "list_due_items" in description


def test_create_reminder_warns_against_duplicating_an_attribute(
    test_settings, tools_by_name
):
    """A reminder for a vehicle's registration expiry is a second copy
    of a date the entity already carries, and the two drift. D19 put
    that date on the entity deliberately."""
    description = tools_by_name(_server(test_settings, _capture({})))[
        "create_reminder"
    ].description

    assert "registration_expiry" in description
    assert "list_due_items" in description

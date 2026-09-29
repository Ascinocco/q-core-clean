"""`GET /due` — the three sources, and the rule that a stale date is loud.

The failure this endpoint exists to prevent is silence: a renewal that
nobody is asked about looks exactly like having nothing due. Most of
these tests are therefore about what must NOT be filtered out.
"""

import json
import logging
import sqlite3
from datetime import date, timedelta

import pytest

from api.tests.test_financial import _auth


def _days(n: int) -> str:
    return (date.today() + timedelta(days=n)).isoformat()


def _entity(client, test_settings, entity_type, name, attributes=None, status=None):
    body = {"type": entity_type, "name": name}
    if attributes is not None:
        body["attributes"] = attributes
    created = client.post("/entities", json=body, headers=_auth(test_settings)).json()
    assert "id" in created, created
    if status is not None:
        client.patch(
            f"/entities/{created['id']}",
            json={"status": status},
            headers=_auth(test_settings),
        )
    return created


def _due(client, test_settings, query=""):
    response = client.get(f"/due{query}", headers=_auth(test_settings))
    return response


def _sql(client, test_settings, statement, params=()):
    # One API call first, always. The database is created lazily by the
    # first request; a bare sqlite3.connect on a path that does not exist
    # yet creates an EMPTY file, and init_db then refuses it as an
    # incomplete database rather than building the schema. Every helper
    # here routes through this for that reason.
    client.get("/entities", headers=_auth(test_settings))
    connection = sqlite3.connect(test_settings.db_path)
    try:
        connection.execute(statement, params)
        connection.commit()
    finally:
        connection.close()


def _reminder(client, test_settings, reminder_id, title, start, rule=None,
              end=None, entity_id=None):
    _sql(
        client,
        test_settings,
        "INSERT INTO reminders (id, title, notes, entity_id, recurrence_rule, "
        "start_date, end_date) VALUES (?, ?, NULL, ?, ?, ?, ?)",
        (reminder_id, title, entity_id, rule, start, end),
    )


def _instance(client, test_settings, reminder_id, due_date, status,
              snoozed_to=None):
    _sql(
        client,
        test_settings,
        "INSERT INTO reminder_instances (id, reminder_id, due_date, status, "
        "snoozed_to) VALUES (?, ?, ?, ?, ?)",
        (f"i-{reminder_id}-{due_date}", reminder_id, due_date, status, snoozed_to),
    )


def _relationship(client, test_settings, from_id, to_id, rel_type, end_date):
    return client.post(
        f"/entities/{from_id}/relationships",
        json={
            "to_entity_id": to_id,
            "relationship_type": rel_type,
            "end_date": end_date,
        },
        headers=_auth(test_settings),
    )


# --- the empty case --------------------------------------------------------


def test_nothing_due_is_an_empty_page_not_an_error(client, test_settings):
    response = _due(client, test_settings)

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}


def test_due_requires_auth(client):
    assert client.get("/due").status_code == 401


# --- the attribute source --------------------------------------------------


def test_a_forward_dated_attribute_inside_the_window_is_due(client, test_settings):
    _entity(
        client,
        test_settings,
        "vehicle",
        "RAV4",
        {"registration_expiry": _days(10)},
    )

    body = _due(client, test_settings).json()

    assert body["total"] == 1
    item = body["items"][0]
    assert item["source"] == "attribute"
    assert item["field"] == "registration_expiry"
    assert item["entity_type"] == "vehicle"
    assert item["entity_name"] == "RAV4"
    assert item["due_date"] == _days(10)
    assert item["days_left"] == 10
    assert item["related"] == []


def test_a_past_forward_dated_attribute_is_overdue_not_hidden(client, test_settings):
    """D19, and the whole reason this endpoint is not a plain range query.

    Nothing rolls these point dates forward: `registration_expiry` holds
    one next occurrence and stays there once it passes. A
    `from <= d <= to` filter would make a lapsed registration silently
    invisible exactly when it matters — the same "nothing is due" failure
    the digest exists to fix.
    """
    _entity(
        client, test_settings, "vehicle", "RAV4", {"registration_expiry": _days(-45)}
    )

    body = _due(client, test_settings).json()

    assert body["total"] == 1
    assert body["items"][0]["due_date"] == _days(-45)
    assert body["items"][0]["days_left"] == -45


def test_an_attribute_beyond_the_window_is_not_due(client, test_settings):
    _entity(
        client, test_settings, "vehicle", "RAV4", {"registration_expiry": _days(400)}
    )

    assert _due(client, test_settings).json()["total"] == 0


def test_a_historical_attribute_is_never_due(client, test_settings):
    """purchase_date is in the past on every vehicle ever owned. If the
    endpoint read every date attribute rather than the forward-dated
    partition, the digest would be a list of purchases."""
    _entity(client, test_settings, "vehicle", "RAV4", {"purchase_date": _days(-400)})

    assert _due(client, test_settings).json()["total"] == 0


# Written out literally, NOT derived from FORWARD_DATED_ATTRIBUTES, and
# that is the whole point of the test below. review-1 found the gap on
# #78: the partition's own tests catch a date field on neither list and
# a name that is not a real field, but nothing catches a field on the
# WRONG side. Move `registration_expiry` into
# HISTORICAL_DATE_ATTRIBUTES and both lists stay well-formed, every test
# in api/tests/test_entities.py stays green, and the only symptom is a
# renewal that never appears — reported as "nothing is due".
#
# A parametrization derived from FORWARD_DATED_ATTRIBUTES would follow
# the field to the wrong side and keep passing. This list is a second,
# independent statement of what the digest is supposed to show, so a
# wrong-side move breaks the case for that field here.
EXPECTED_DUE_ATTRIBUTES = [
    ("property", "insurance_renewal"),
    ("property", "tax_due"),
    ("vehicle", "registration_expiry"),
    ("vehicle", "warranty_end"),
    ("pet", "vaccination_due"),
    ("account", "renewal_date"),
]


@pytest.mark.parametrize(("entity_type", "field"), EXPECTED_DUE_ATTRIBUTES)
def test_every_forward_dated_attribute_reaches_the_digest(
    client, test_settings, entity_type, field
):
    """The consumer-side check on the partition.

    Fails two ways, both of which are silent otherwise: a field
    classified as forward-dated but never read by `/due`, and a field
    moved to the historical side by mistake.
    """
    _entity(client, test_settings, entity_type, "thing", {field: _days(5)})

    body = _due(client, test_settings).json()

    assert body["total"] == 1
    assert body["items"][0]["field"] == field


def test_the_expected_list_and_the_partition_still_name_the_same_fields():
    """The other direction: a field added to FORWARD_DATED_ATTRIBUTES and
    not to the list above would be unread by every case here, and the
    parametrized test cannot report a case it does not have.

    Kept separate from the parametrized test on purpose. A wrong-side
    move fires both, and between them they say what happened: this one
    says the two statements disagree, that one says which field stopped
    surfacing.
    """
    from api.models import FORWARD_DATED_ATTRIBUTES

    declared = {
        (entity_type, field)
        for entity_type, fields in FORWARD_DATED_ATTRIBUTES.items()
        for field in fields
    }

    assert declared == set(EXPECTED_DUE_ATTRIBUTES)


@pytest.mark.parametrize("status", ["sold", "totaled", "deceased", "closed", "archived"])
def test_a_retired_entity_is_not_due(client, test_settings, status):
    """A sold car's registration expiry is nobody's obligation. Without
    this the digest grows monotonically with everything ever owned, and a
    list that long stops being read — which is the same failure as
    showing nothing."""
    _entity(
        client,
        test_settings,
        "vehicle",
        "old car",
        {"registration_expiry": _days(5)},
        status=status,
    )

    assert _due(client, test_settings).json()["total"] == 0


def test_an_inactive_entity_is_still_due(client, test_settings):
    """`inactive` is the reversible status, and an inactive account's
    renewal is exactly the thing worth being reminded about. Pinned
    separately so nobody folds it into the retired list."""
    _entity(
        client,
        test_settings,
        "account",
        "dormant card",
        {"renewal_date": _days(5)},
        status="inactive",
    )

    assert _due(client, test_settings).json()["total"] == 1


def test_a_malformed_attribute_date_is_skipped_not_a_500(client, test_settings):
    """Attributes are a JSON blob written by a model. One bad value must
    not take down the query you would use to notice it."""
    good = _entity(
        client, test_settings, "pet", "Rex", {"vaccination_due": _days(3)}
    )
    _sql(
        client,
        test_settings,
        "UPDATE entities SET attributes = ? WHERE id != ?",
        (json.dumps({"registration_expiry": "next spring"}), good["id"]),
    )
    _entity(client, test_settings, "vehicle", "RAV4")
    _sql(
        client,
        test_settings,
        "UPDATE entities SET attributes = ? WHERE name = 'RAV4'",
        (json.dumps({"registration_expiry": "soon"}),),
    )

    body = _due(client, test_settings).json()

    assert body["total"] == 1
    assert body["items"][0]["entity_name"] == "Rex"


# --- the relationship source -----------------------------------------------


def test_a_relationship_end_date_is_due(client, test_settings):
    landlord = _entity(client, test_settings, "property", "12 Oak St")
    tenant = _entity(client, test_settings, "person", "Sam")
    created = _relationship(
        client, test_settings, landlord["id"], tenant["id"], "leases", _days(20)
    )
    assert created.status_code == 200, created.json()

    body = _due(client, test_settings).json()

    assert body["total"] == 1
    item = body["items"][0]
    assert item["source"] == "relationship"
    assert item["field"] == "end_date"
    assert item["entity_id"] == landlord["id"]
    assert item["related"] == [
        {
            "entity_id": tenant["id"],
            "entity_type": "person",
            "entity_name": "Sam",
            "relationship_type": "leases",
        }
    ]


def test_a_lapsed_policy_relationship_is_overdue(client, test_settings):
    """An `insures` relationship whose end_date passed is an uninsured
    asset — the loudest thing this endpoint can report, and the one a
    lower bound would hide."""
    policy = _entity(client, test_settings, "account", "Home policy")
    house = _entity(client, test_settings, "property", "12 Oak St")
    _relationship(client, test_settings, policy["id"], house["id"], "insures",
                  _days(-10))

    body = _due(client, test_settings).json()

    assert body["total"] == 1
    assert body["items"][0]["days_left"] == -10


def test_a_relationship_to_a_retired_entity_is_not_due(client, test_settings):
    policy = _entity(client, test_settings, "account", "Auto policy")
    car = _entity(client, test_settings, "vehicle", "old car")
    _relationship(client, test_settings, policy["id"], car["id"], "insures", _days(5))
    client.patch(
        f"/entities/{car['id']}", json={"status": "sold"}, headers=_auth(test_settings)
    )

    assert _due(client, test_settings).json()["total"] == 0


def test_a_relationship_with_no_end_date_is_not_due(client, test_settings):
    owner = _entity(client, test_settings, "person", "Alex Example")
    car = _entity(client, test_settings, "vehicle", "RAV4")
    client.post(
        f"/entities/{owner['id']}/relationships",
        json={"to_entity_id": car["id"], "relationship_type": "owns"},
        headers=_auth(test_settings),
    )

    assert _due(client, test_settings).json()["total"] == 0


# --- the reminder source ---------------------------------------------------


def test_a_one_off_reminder_in_the_window_is_due(client, test_settings):
    _entity(client, test_settings, "pet", "Rex")
    _reminder(client, test_settings, "r1", "Renew passport", _days(7))

    body = _due(client, test_settings).json()

    assert body["total"] == 1
    item = body["items"][0]
    assert item["source"] == "reminder"
    assert item["title"] == "Renew passport"
    assert item["due_date"] == _days(7)


def test_a_recurring_reminder_expands_within_the_window(client, test_settings):
    _reminder(client, test_settings, "r1", "Weekly bins", _days(1), rule="FREQ=WEEKLY")

    body = _due(client, test_settings).json()

    # Days 1, 8, 15, 22, 29 within the default 30-day horizon.
    assert body["total"] == 5
    assert [item["due_date"] for item in body["items"]] == [
        _days(n) for n in (1, 8, 15, 22, 29)
    ]


def test_a_recurring_reminders_past_occurrences_are_not_expanded(
    client, test_settings
):
    """The one source that DOES honour `from`, and deliberately.

    Attributes and relationships have one date each, so showing a past
    one costs one row. A daily RRULE has unboundedly many past
    occurrences — expanding those backwards would not terminate in any
    useful sense, and a digest full of last year's bin days is a digest
    nobody reads. The asymmetry is why `from` exists.
    """
    _reminder(client, test_settings, "r1", "Daily thing", _days(-400), rule="FREQ=DAILY")

    body = _due(client, test_settings).json()

    assert body["total"] == 38  # seven days overdue through +30, inclusive
    assert body["items"][0]["due_date"] == _days(-7)


def test_a_reminder_series_stops_at_its_own_end_date(client, test_settings):
    """A lease reminder must not keep firing after the lease."""
    _reminder(
        client,
        test_settings,
        "r1",
        "Weekly bins",
        _days(1),
        rule="FREQ=WEEKLY",
        end=_days(10),
    )

    body = _due(client, test_settings).json()

    assert [item["due_date"] for item in body["items"]] == [_days(1), _days(8)]


def test_a_completed_occurrence_is_not_due(client, test_settings):
    _reminder(client, test_settings, "r1", "Weekly bins", _days(1), rule="FREQ=WEEKLY")
    _instance(client, test_settings, "r1", _days(8), "done")

    body = _due(client, test_settings).json()

    assert [item["due_date"] for item in body["items"]] == [
        _days(n) for n in (1, 15, 22, 29)
    ]


def test_a_skipped_occurrence_is_not_due(client, test_settings):
    _reminder(client, test_settings, "r1", "One off", _days(3))
    _instance(client, test_settings, "r1", _days(3), "skipped")

    assert _due(client, test_settings).json()["total"] == 0


def test_a_snoozed_occurrence_moves_to_its_snoozed_date(client, test_settings):
    _reminder(client, test_settings, "r1", "One off", _days(3))
    _instance(client, test_settings, "r1", _days(3), "snoozed", snoozed_to=_days(12))

    body = _due(client, test_settings).json()

    assert body["total"] == 1
    assert body["items"][0]["due_date"] == _days(12)


def test_a_snooze_past_the_window_drops_out(client, test_settings):
    _reminder(client, test_settings, "r1", "One off", _days(3))
    _instance(client, test_settings, "r1", _days(3), "snoozed", snoozed_to=_days(90))

    assert _due(client, test_settings).json()["total"] == 0


def test_a_malformed_rrule_is_skipped_not_a_500(client, test_settings):
    _reminder(client, test_settings, "r1", "Broken", _days(1), rule="FREQ=NONSENSE")
    _reminder(client, test_settings, "r2", "Fine", _days(2))

    body = _due(client, test_settings).json()

    assert body["total"] == 1
    assert body["items"][0]["title"] == "Fine"


def test_a_reminder_carries_its_entity_when_it_has_one(client, test_settings):
    pet = _entity(client, test_settings, "pet", "Rex")
    _reminder(client, test_settings, "r1", "Vet visit", _days(4), entity_id=pet["id"])

    item = _due(client, test_settings).json()["items"][0]

    assert item["entity_id"] == pet["id"]
    assert item["entity_name"] == "Rex"
    assert item["entity_type"] == "pet"


# --- window, filters and ordering ------------------------------------------


def test_to_before_from_is_a_422(client, test_settings):
    response = _due(client, test_settings, f"?from={_days(10)}&to={_days(5)}")

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "query.to"


def test_an_unknown_source_is_a_422(client, test_settings):
    response = _due(client, test_settings, "?source=guesswork")

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "query.source"


def test_the_source_filter_selects_one_kind(client, test_settings):
    _entity(client, test_settings, "pet", "Rex", {"vaccination_due": _days(3)})
    _reminder(client, test_settings, "r1", "Vet visit", _days(4))

    assert _due(client, test_settings).json()["total"] == 2
    attribute_only = _due(client, test_settings, "?source=attribute").json()
    assert attribute_only["total"] == 1
    assert attribute_only["items"][0]["source"] == "attribute"


def test_the_entity_filter_selects_one_entity(client, test_settings):
    rex = _entity(client, test_settings, "pet", "Rex", {"vaccination_due": _days(3)})
    _entity(client, test_settings, "pet", "Bo", {"vaccination_due": _days(4)})

    body = _due(client, test_settings, f"?entity_id={rex['id']}").json()

    assert body["total"] == 1
    assert body["items"][0]["entity_name"] == "Rex"


def test_days_left_is_relative_to_today_not_to_from(client, test_settings):
    """"Days left" means days from now. A caller asking about next month
    still needs to know the thing is 40 days away, not 0."""
    _entity(client, test_settings, "pet", "Rex", {"vaccination_due": _days(40)})

    body = _due(client, test_settings, f"?from={_days(35)}&to={_days(45)}").json()

    assert body["items"][0]["days_left"] == 40


def test_the_window_can_be_widened(client, test_settings):
    _entity(client, test_settings, "pet", "Rex", {"vaccination_due": _days(200)})

    assert _due(client, test_settings).json()["total"] == 0
    assert _due(client, test_settings, f"?to={_days(365)}").json()["total"] == 1


def test_items_are_ordered_by_due_date(client, test_settings):
    _entity(client, test_settings, "pet", "Rex", {"vaccination_due": _days(20)})
    _entity(client, test_settings, "pet", "Bo", {"vaccination_due": _days(2)})
    _reminder(client, test_settings, "r1", "Middle", _days(9))

    body = _due(client, test_settings).json()

    assert [item["due_date"] for item in body["items"]] == [
        _days(2),
        _days(9),
        _days(20),
    ]


def test_same_day_items_are_ordered_by_a_decided_key_not_by_arrival(
    client, test_settings
):
    """Sorting on the date alone is not enough, and it is not enough in a
    way that hides: Python's sort is stable, so a date-only key silently
    falls back to arrival order and every obvious test of this passes.

    The source component cannot show it either — "attribute" <
    "relationship" < "reminder" happens to be the order the sources are
    queried in, so that part of the key is invisible by construction.
    What does show it is two same-day items from ONE source whose names
    sort against the order the rows come back in: `_attribute_items`
    reads `entities` with no ORDER BY, so arrival order is SQLite's scan
    order — insertion order in practice, guaranteed by nothing. Same
    lesson as the merchant-rule tie-break (D15), and a caller paging
    through this endpoint depends on the answer.
    """
    _entity(client, test_settings, "pet", "Zed", {"vaccination_due": _days(5)})
    _entity(client, test_settings, "pet", "Abe", {"vaccination_due": _days(5)})

    body = _due(client, test_settings).json()

    assert [item["entity_name"] for item in body["items"]] == ["Abe", "Zed"]


def test_paging_covers_every_item_exactly_once(client, test_settings):
    """The ordering is computed in Python over three merged lists, so an
    unstable sort would let page 2 repeat or skip a row with nothing
    failing — the count would still look right."""
    for n in range(7):
        _entity(
            client, test_settings, "pet", f"Pet {n}", {"vaccination_due": _days(n + 1)}
        )

    first = _due(client, test_settings, "?limit=3").json()
    second = _due(client, test_settings, "?limit=3&offset=3").json()
    third = _due(client, test_settings, "?limit=3&offset=6").json()

    assert first["total"] == second["total"] == third["total"] == 7
    names = [item["entity_name"] for page in (first, second, third)
             for item in page["items"]]
    assert len(names) == 7
    assert len(set(names)) == 7


def test_all_three_sources_appear_together(client, test_settings):
    """The union is the point. An endpoint that answered one source
    correctly and dropped the others would pass most of this file."""
    pet = _entity(client, test_settings, "pet", "Rex", {"vaccination_due": _days(3)})
    policy = _entity(client, test_settings, "account", "Policy")
    _relationship(client, test_settings, policy["id"], pet["id"], "insures", _days(6))
    _reminder(client, test_settings, "r1", "Vet visit", _days(9))

    body = _due(client, test_settings).json()

    assert body["total"] == 3
    assert [item["source"] for item in body["items"]] == [
        "attribute",
        "relationship",
        "reminder",
    ]


# --- skipped rows are logged, not swallowed (D23) --------------------------


def _warnings(caplog) -> list[str]:
    """Messages from `api.due` only.

    caplog's handler sits on the ROOT logger, so `caplog.records` holds
    records from every logger in the process regardless of the `logger=`
    argument to `at_level` — which only sets a level. Asserting over
    `caplog.records` therefore passes if the warning came from anywhere
    at all. Found by mutation: moving one warning to
    `logging.getLogger("silenced")` left every one of these tests green.
    Filtering by `record.name` is what makes them about this module.
    """
    return [
        record.getMessage() for record in caplog.records if record.name == "api.due"
    ]


def test_an_unparseable_attribute_date_is_logged_at_warning(
    client, test_settings, caplog
):
    """Skipping quietly would make the digest the tool you used to not
    notice: a typo'd date becomes a renewal that never appears, and
    "nothing is due" is what you would see. The log line is the only
    place that typo surfaces."""
    rex = _entity(client, test_settings, "pet", "Rex")
    _sql(
        client,
        test_settings,
        "UPDATE entities SET attributes = ? WHERE id = ?",
        (json.dumps({"vaccination_due": "next spring"}), rex["id"]),
    )

    with caplog.at_level(logging.WARNING, logger="api.due"):
        assert _due(client, test_settings).json()["total"] == 0

    messages = _warnings(caplog)
    assert any("vaccination_due" in message for message in messages), messages
    assert any(rex["id"] in message for message in messages), messages
    assert any("next spring" in message for message in messages), messages


def test_a_missing_attribute_is_not_logged(client, test_settings, caplog):
    """Absence is the normal case — most entities have most fields
    unset. Logging it would bury the real warnings under one line per
    entity per field, which is the same as not logging at all."""
    _entity(client, test_settings, "pet", "Rex")

    with caplog.at_level(logging.WARNING, logger="api.due"):
        _due(client, test_settings)

    assert _warnings(caplog) == []


def test_an_unusable_rrule_is_logged_at_warning(client, test_settings, caplog):
    """Until POST /reminders validates the rule at write time (ticket
    ticket T-12) this log line is the only signal that a reminder will
    never fire."""
    _reminder(client, test_settings, "r1", "Broken", _days(1), rule="FREQ=NONSENSE")

    with caplog.at_level(logging.WARNING, logger="api.due"):
        assert _due(client, test_settings).json()["total"] == 0

    messages = _warnings(caplog)
    assert any("r1" in message for message in messages), messages
    assert any("FREQ=NONSENSE" in message for message in messages), messages


def test_attributes_that_are_not_an_object_are_logged(client, test_settings, caplog):
    rex = _entity(client, test_settings, "pet", "Rex")
    _sql(
        client,
        test_settings,
        "UPDATE entities SET attributes = ? WHERE id = ?",
        ("[1, 2, 3]", rex["id"]),
    )

    with caplog.at_level(logging.WARNING, logger="api.due"):
        _due(client, test_settings)

    assert any(rex["id"] in message for message in _warnings(caplog))


def test_attributes_that_are_not_valid_json_are_logged(client, test_settings, caplog):
    """The `attributes` column is TEXT holding JSON, so a write that
    bypasses the API — a migration, a manual fix, a restore — can leave
    a string that will not parse. `/due` skips the whole entity, which
    silently drops every forward-dated date it carried, so the log line
    is the only trace.

    review-1's #84 gap: this branch of `_attribute_items` had no test
    while its sibling (`attributes` parses but is not an object) did.
    """
    rex = _entity(client, test_settings, "pet", "Rex")
    _sql(
        client,
        test_settings,
        "UPDATE entities SET attributes = ? WHERE id = ?",
        ("{not json at all", rex["id"]),
    )

    with caplog.at_level(logging.WARNING, logger="api.due"):
        assert _due(client, test_settings).json()["total"] == 0

    messages = _warnings(caplog)
    assert any(rex["id"] in message for message in messages), messages
    assert any("not valid JSON" in message for message in messages), messages


def test_an_unparseable_reminder_start_date_is_logged(client, test_settings, caplog):
    """The reminder analogue of the attribute-date case, and reachable
    only now that reminders have a producer: `start_date` is DATE in the
    schema but SQLite does not enforce that, so a row written around the
    API can hold anything. A reminder whose start_date will not parse
    never fires, and without the log there is nothing to find.
    """
    _reminder(client, test_settings, "r1", "Broken start", _days(3))
    _sql(
        client,
        test_settings,
        "UPDATE reminders SET start_date = ? WHERE id = 'r1'",
        ("sometime soon",),
    )

    with caplog.at_level(logging.WARNING, logger="api.due"):
        assert _due(client, test_settings).json()["total"] == 0

    messages = _warnings(caplog)
    assert any("r1" in message for message in messages), messages
    assert any("sometime soon" in message for message in messages), messages

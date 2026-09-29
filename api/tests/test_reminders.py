"""Reminders CRUD, and the two writers of `reminder_instances`.

The end-to-end claim these exist to support is that a reminder created
through the API shows up in `GET /due` and stops showing up once it is
completed — before this router, that source could only be exercised by
writing SQL directly, so nothing tested the path a user actually takes.
"""

from datetime import date, timedelta

import pytest

from api.tests.test_financial import _auth


def _days(n: int) -> str:
    return (date.today() + timedelta(days=n)).isoformat()


def _create(client, test_settings, **body):
    body.setdefault("title", "Renew passport")
    body.setdefault("start_date", _days(7))
    return client.post("/reminders", json=body, headers=_auth(test_settings))


def _due(client, test_settings, query=""):
    return client.get(f"/due{query}", headers=_auth(test_settings)).json()


# --- create ----------------------------------------------------------------


def test_create_returns_the_reminder(client, test_settings):
    response = _create(client, test_settings, notes="at the post office")

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["title"] == "Renew passport"
    assert body["notes"] == "at the post office"
    assert body["start_date"] == _days(7)
    assert body["recurrence_rule"] is None
    assert body["id"]


def test_create_requires_auth(client):
    assert client.post("/reminders", json={}).status_code == 401


def test_an_unusable_rrule_is_rejected_at_write_time(client, test_settings):
    """The whole reason POST validates the rule.

    `GET /due` deliberately swallows a malformed RRULE so one bad row
    cannot take the digest down — which means without this check a typo
    is a reminder that silently never fires, and the tool you would use
    to notice is the digest it is missing from. Rejecting here turns it
    into a 422 while someone can still fix it.
    """
    response = _create(client, test_settings, recurrence_rule="FREQ=NONSENSE")

    assert response.status_code == 422
    assert "RRULE" in response.json()["error"]["details"][0]["message"]


def test_a_usable_rrule_is_accepted(client, test_settings):
    """The other half of the previous test. A validator that rejected
    everything would pass that one."""
    response = _create(client, test_settings, recurrence_rule="FREQ=WEEKLY;COUNT=5")

    assert response.status_code == 200
    assert response.json()["recurrence_rule"] == "FREQ=WEEKLY;COUNT=5"


def test_an_end_date_before_the_start_is_rejected(client, test_settings):
    response = _create(
        client,
        test_settings,
        recurrence_rule="FREQ=WEEKLY",
        start_date=_days(10),
        end_date=_days(3),
    )

    assert response.status_code == 422


def test_an_end_date_without_a_recurrence_rule_is_rejected(client, test_settings):
    """A one-off reminder is its start_date; an end_date on it would
    look meaningful and do nothing."""
    response = _create(client, test_settings, end_date=_days(30))

    assert response.status_code == 422


def test_an_empty_title_is_rejected(client, test_settings):
    """A titleless reminder renders as a blank line in the digest —
    present, and useless."""
    assert _create(client, test_settings, title="").status_code == 422


def test_an_unknown_entity_is_a_400(client, test_settings):
    """InvalidReferenceError is 400 repo-wide (D17 on #76)."""
    response = _create(client, test_settings, entity_id="nope")

    assert response.status_code == 400


def test_an_unknown_field_is_rejected(client, test_settings):
    assert _create(client, test_settings, colour="blue").status_code == 422


# --- read ------------------------------------------------------------------


def test_get_returns_the_reminder(client, test_settings):
    created = _create(client, test_settings).json()

    fetched = client.get(
        f"/reminders/{created['id']}", headers=_auth(test_settings)
    )

    assert fetched.status_code == 200
    assert fetched.json()["id"] == created["id"]


def test_get_an_unknown_reminder_is_a_404(client, test_settings):
    assert (
        client.get("/reminders/nope", headers=_auth(test_settings)).status_code == 404
    )


def test_list_is_paginated_and_totalled(client, test_settings):
    for n in range(5):
        _create(client, test_settings, title=f"R{n}", start_date=_days(n + 1))

    body = client.get("/reminders?limit=2", headers=_auth(test_settings)).json()

    assert body["total"] == 5
    assert len(body["items"]) == 2


def test_list_filters_by_entity(client, test_settings):
    pet = client.post(
        "/entities", json={"type": "pet", "name": "Rex"}, headers=_auth(test_settings)
    ).json()
    _create(client, test_settings, title="Vet", entity_id=pet["id"])
    _create(client, test_settings, title="Unrelated")

    body = client.get(
        f"/reminders?entity_id={pet['id']}", headers=_auth(test_settings)
    ).json()

    assert body["total"] == 1
    assert body["items"][0]["title"] == "Vet"


def test_list_has_a_decided_order_among_same_day_reminders(client, test_settings):
    """Ordering on start_date alone leaves same-day rows in scan order,
    and a caller paging through an unstable order can see a row twice or
    miss one with nothing reporting it. Created Zed-first so insertion
    order and the declared order disagree — otherwise this would pass
    under a start_date-only sort."""
    _create(client, test_settings, title="Zed", start_date=_days(3))
    _create(client, test_settings, title="Abe", start_date=_days(3))

    body = client.get("/reminders", headers=_auth(test_settings)).json()

    assert [item["title"] for item in body["items"]] == ["Abe", "Zed"]


# --- delete ----------------------------------------------------------------


def test_delete_removes_the_reminder(client, test_settings):
    created = _create(client, test_settings).json()

    response = client.delete(
        f"/reminders/{created['id']}", headers=_auth(test_settings)
    )

    assert response.status_code == 200
    assert (
        client.get(
            f"/reminders/{created['id']}", headers=_auth(test_settings)
        ).status_code
        == 404
    )


def test_delete_an_unknown_reminder_is_a_404(client, test_settings):
    assert (
        client.delete("/reminders/nope", headers=_auth(test_settings)).status_code
        == 404
    )


def test_delete_takes_its_instances_with_it(client, test_settings):
    """reminder_instances.reminder_id is a foreign key. Leaving the rows
    behind would orphan records nothing can resolve to a reminder — and
    since nothing reads an orphaned instance, the leak would be
    invisible rather than noisy."""
    created = _create(client, test_settings).json()
    client.post(
        f"/reminders/{created['id']}/complete",
        json={"due_date": _days(7)},
        headers=_auth(test_settings),
    )

    client.delete(f"/reminders/{created['id']}", headers=_auth(test_settings))

    import sqlite3

    connection = sqlite3.connect(test_settings.db_path)
    try:
        remaining = connection.execute(
            "SELECT COUNT(*) FROM reminder_instances WHERE reminder_id = ?",
            (created["id"],),
        ).fetchone()[0]
    finally:
        connection.close()
    assert remaining == 0


# --- complete and snooze ---------------------------------------------------


def test_complete_records_the_occurrence(client, test_settings):
    created = _create(client, test_settings).json()

    response = client.post(
        f"/reminders/{created['id']}/complete",
        json={"due_date": _days(7)},
        headers=_auth(test_settings),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "done"
    assert body["due_date"] == _days(7)
    assert body["completed_at"] is not None


def test_completing_twice_is_not_an_error(client, test_settings):
    """(reminder_id, due_date) is UNIQUE, so a plain INSERT would 500 on
    the second call. Completing something twice is a normal thing to do,
    not a conflict worth reporting."""
    created = _create(client, test_settings).json()
    payload = {"due_date": _days(7)}

    first = client.post(
        f"/reminders/{created['id']}/complete",
        json=payload,
        headers=_auth(test_settings),
    )
    second = client.post(
        f"/reminders/{created['id']}/complete",
        json=payload,
        headers=_auth(test_settings),
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]


def test_snooze_records_the_new_date(client, test_settings):
    created = _create(client, test_settings).json()

    response = client.post(
        f"/reminders/{created['id']}/snooze",
        json={"due_date": _days(7), "snoozed_to": _days(14)},
        headers=_auth(test_settings),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "snoozed"
    assert response.json()["snoozed_to"] == _days(14)


def test_snoozing_backwards_is_rejected(client, test_settings):
    """Snoozing to the same day or earlier leaves the item due and looks
    like it worked."""
    created = _create(client, test_settings).json()

    response = client.post(
        f"/reminders/{created['id']}/snooze",
        json={"due_date": _days(7), "snoozed_to": _days(7)},
        headers=_auth(test_settings),
    )

    assert response.status_code == 422


def test_snoozing_after_completing_overwrites_the_status(client, test_settings):
    """The rationale lives in the original commit message, not here.

    It introduced the UPSERT on (reminder_id, due_date) and said why:
    "Completing something twice, or snoozing what was already completed,
    is normal -- a plain INSERT would 500 on the second call."

    Cited because #136 nearly reversed it. The live MCP exercise reported
    "snoozing a completed occurrence resurrects it" as a defect; this
    test caught the refusal I built for it, and reading that commit is what
    settled that the existing behaviour is deliberate. It is also the
    only way to undo a completion recorded by mistake, so refusing would
    remove the escape hatch rather than close a hole.
    """
    created = _create(client, test_settings).json()
    client.post(
        f"/reminders/{created['id']}/complete",
        json={"due_date": _days(7)},
        headers=_auth(test_settings),
    )

    response = client.post(
        f"/reminders/{created['id']}/snooze",
        json={"due_date": _days(7), "snoozed_to": _days(14)},
        headers=_auth(test_settings),
    )

    assert response.json()["status"] == "snoozed"
    assert response.json()["completed_at"] is None


@pytest.mark.parametrize("action", ["complete", "snooze"])
def test_acting_on_an_unknown_reminder_is_a_404(client, test_settings, action):
    payload = {"due_date": _days(7)}
    if action == "snooze":
        payload["snoozed_to"] = _days(14)

    response = client.post(
        f"/reminders/nope/{action}", json=payload, headers=_auth(test_settings)
    )

    assert response.status_code == 404


# --- the loop that matters: created here, read by /due ---------------------


def test_a_reminder_created_through_the_api_appears_in_due(client, test_settings):
    """Before this router, the `reminder` source of /due could only be
    exercised by writing SQL directly, so nothing tested the path a user
    actually takes."""
    _create(client, test_settings, title="Renew passport", start_date=_days(7))

    body = _due(client, test_settings)

    assert body["total"] == 1
    assert body["items"][0]["source"] == "reminder"
    assert body["items"][0]["title"] == "Renew passport"


def test_completing_an_occurrence_removes_it_from_due(client, test_settings):
    created = _create(client, test_settings, start_date=_days(7)).json()
    assert _due(client, test_settings)["total"] == 1

    client.post(
        f"/reminders/{created['id']}/complete",
        json={"due_date": _days(7)},
        headers=_auth(test_settings),
    )

    assert _due(client, test_settings)["total"] == 0


def test_snoozing_moves_the_item_in_due(client, test_settings):
    created = _create(client, test_settings, start_date=_days(7)).json()

    client.post(
        f"/reminders/{created['id']}/snooze",
        json={"due_date": _days(7), "snoozed_to": _days(14)},
        headers=_auth(test_settings),
    )

    body = _due(client, test_settings)
    assert body["total"] == 1
    assert body["items"][0]["due_date"] == _days(14)


def test_completing_one_occurrence_leaves_the_rest_of_the_series(
    client, test_settings
):
    """The instance is keyed on (reminder_id, due_date), so completing
    one week must not complete the series. A bug here would look like
    the reminder working — it would just quietly stop."""
    created = _create(
        client, test_settings, start_date=_days(1), recurrence_rule="FREQ=WEEKLY"
    ).json()
    assert _due(client, test_settings)["total"] == 5

    client.post(
        f"/reminders/{created['id']}/complete",
        json={"due_date": _days(8)},
        headers=_auth(test_settings),
    )

    body = _due(client, test_settings)
    assert body["total"] == 4
    assert _days(8) not in [item["due_date"] for item in body["items"]]


def test_deleting_a_reminder_removes_it_from_due(client, test_settings):
    created = _create(client, test_settings, start_date=_days(7)).json()

    client.delete(f"/reminders/{created['id']}", headers=_auth(test_settings))

    assert _due(client, test_settings)["total"] == 0


def test_a_rule_accepted_on_create_is_one_the_digest_can_expand(client, test_settings):
    """The property the dtstart in _validated_rrule exists for.

    Validation and expansion must parse identically, or "accepted" stops
    meaning "will fire". A UTC UNTIL against a naive dtstart is the case
    that separates them: it parses bare and raises once a dtstart is
    supplied, so validating bare would accept a rule /due then silently
    skips. Checks the pairing rather than the implementation detail, so
    it still holds if either side changes how it parses — as long as
    both change together.
    """
    rule = "FREQ=DAILY;UNTIL=20261231T000000Z"

    response = _create(
        client, test_settings, recurrence_rule=rule, start_date=_days(1)
    )

    assert response.status_code == 422, (
        "a rule /due cannot expand must not be accepted on create"
    )

    from datetime import datetime

    import pytest as _pytest
    from dateutil.rrule import rrulestr

    # The other half: the reason it is rejected is the same parse /due
    # performs, not an unrelated rule of our own.
    with _pytest.raises(ValueError):
        rrulestr(rule, dtstart=datetime(2026, 1, 1))


# --- the date /due reports is the date the writers accept (ticket T-54) -------


def _complete(client, test_settings, reminder_id, due_date):
    return client.post(
        f"/reminders/{reminder_id}/complete",
        json={"due_date": due_date},
        headers=_auth(test_settings),
    )


def _snooze(client, test_settings, reminder_id, due_date, snoozed_to):
    return client.post(
        f"/reminders/{reminder_id}/snooze",
        json={"due_date": due_date, "snoozed_to": snoozed_to},
        headers=_auth(test_settings),
    )


def _reported_date(client, test_settings, title="Renew passport"):
    """The date GET /due actually reports, read back rather than assumed.

    The whole defect was that the reported date and the accepted date
    were different, so a test that hardcodes what it thinks /due says
    cannot see it: it would pass against the broken code by choosing the
    occurrence's own date, which always worked. The date has to come out
    of the response.
    """
    items = [
        item
        for item in _due(client, test_settings, f"?from={_days(0)}&to={_days(400)}")[
            "items"
        ]
        if item["title"] == title
    ]
    assert len(items) == 1, f"expected exactly one due item, got {items}"
    return items[0]["due_date"]


def test_completing_the_date_due_reported_clears_a_snoozed_occurrence(
    client, test_settings
):
    """The headline of ticket T-54, and the reason resolve_occurrence exists.

    An occurrence is keyed by its SCHEDULED date, but /due reports a
    snoozed one at its snoozed_to -- so the one date a caller was handed
    was the one date that identified nothing. Completing it wrote an
    unrelated second instance and the reminder stayed due, answered 200.
    complete_reminder's own description tells callers to "pass the
    due_date exactly as list_due_items reported it".
    """
    created = _create(client, test_settings).json()
    _snooze(client, test_settings, created["id"], _days(7), _days(14))

    reported = _reported_date(client, test_settings)
    assert reported == _days(14), "precondition: /due reports the snoozed date"

    response = _complete(client, test_settings, created["id"], reported)

    assert response.status_code == 200, response.json()
    remaining = [
        item
        for item in _due(client, test_settings, f"?from={_days(0)}&to={_days(400)}")[
            "items"
        ]
        if item["title"] == "Renew passport"
    ]
    assert remaining == [], (
        "completing the date /due reported left the occurrence due — the "
        "reported date and the accepted date are different again"
    )


def test_the_occurrences_own_date_still_completes_it_after_a_snooze(
    client, test_settings
):
    """The other half. Resolution must ADD a way to name the occurrence,
    not swap which one works — a caller holding the scheduled date from
    before the snooze is still right."""
    created = _create(client, test_settings).json()
    _snooze(client, test_settings, created["id"], _days(7), _days(14))

    response = _complete(client, test_settings, created["id"], _days(7))

    assert response.status_code == 200, response.json()
    assert response.json()["due_date"] == _days(7)
    assert _reported_titles(client, test_settings) == []


def _reported_titles(client, test_settings):
    return [
        item["title"]
        for item in _due(client, test_settings, f"?from={_days(0)}&to={_days(400)}")[
            "items"
        ]
    ]


@pytest.mark.parametrize("action", ["complete", "snooze"])
def test_a_date_that_is_not_an_occurrence_is_refused_naming_the_real_ones(
    client, test_settings, action
):
    """Ticket T-74: a non-occurrence date was a cheerful 200 storing a
    completion against a date the reminder is never due on, leaving the
    real occurrence outstanding. The caller got no signal to correct --
    the silent-no-op shape update_entity refuses on principle.

    Parametrized over both writers because they take the same `due_date`
    and had the same hole; fixing only the one that was reported would
    leave the other.
    """
    created = _create(client, test_settings).json()  # start_date = +7, no rule

    payload = {"due_date": _days(900)}
    if action == "snooze":
        payload["snoozed_to"] = _days(950)
    response = client.post(
        f"/reminders/{created['id']}/{action}",
        json=payload,
        headers=_auth(test_settings),
    )

    assert response.status_code == 422, response.json()
    message = response.json()["error"]["details"][0]["message"]
    assert _days(900) in message, "the refusal must quote what was sent"
    assert _days(7) in message, (
        "the refusal must name the occurrence that does exist, the way the "
        "entity and relationship enums name their permitted values"
    )
    # and nothing was written
    assert _reported_titles(client, test_settings) == ["Renew passport"]


def test_a_recurring_reminder_accepts_any_date_on_its_series(
    client, test_settings
):
    """The membership test must be the RRULE, not the start date.

    A weekly reminder is due on many dates and a caller completing the
    third one is right; a check that only accepted start_date would
    refuse it, and the parametrized non-occurrence test above would
    still pass.
    """
    created = _create(
        client, test_settings, start_date=_days(7), recurrence_rule="FREQ=WEEKLY"
    ).json()

    response = _complete(client, test_settings, created["id"], _days(21))

    assert response.status_code == 200, response.json()
    assert response.json()["due_date"] == _days(21)


def test_a_recurring_reminder_refuses_a_date_between_its_occurrences(
    client, test_settings
):
    """The companion: accepting the series must not mean accepting
    anything after the start. +8 is one day past a weekly occurrence."""
    created = _create(
        client, test_settings, start_date=_days(7), recurrence_rule="FREQ=WEEKLY"
    ).json()

    response = _complete(client, test_settings, created["id"], _days(8))

    assert response.status_code == 422, response.json()
    message = response.json()["error"]["details"][0]["message"]
    assert _days(7) in message and _days(14) in message, (
        "a recurring reminder's refusal should name the occurrences "
        f"nearest the date sent, got: {message}"
    )


def test_two_occurrences_snoozed_onto_one_day_are_refused_not_guessed(
    client, test_settings
):
    """The ambiguous case resolution must not paper over.

    Two occurrences of one weekly reminder snoozed onto the same day:
    /due lists both there, so that date genuinely identifies two things.
    Picking either would complete an occurrence the caller did not name.
    """
    created = _create(
        client, test_settings, start_date=_days(7), recurrence_rule="FREQ=WEEKLY"
    ).json()
    _snooze(client, test_settings, created["id"], _days(7), _days(30))
    _snooze(client, test_settings, created["id"], _days(14), _days(30))

    response = _complete(client, test_settings, created["id"], _days(30))

    assert response.status_code == 422, response.json()
    message = response.json()["error"]["details"][0]["message"]
    assert _days(7) in message and _days(14) in message, (
        "the refusal must hand back the two scheduled dates, which do "
        f"distinguish the occurrences: {message}"
    )
    # THE WORDING, not just the dates. review-2 found this test passing
    # under the drop-the-snoozed-lookup mutation: that mutation makes the
    # call fall through to the generic not-an-occurrence refusal, which
    # ALSO names +7 and +14 because they are the nearby occurrences it
    # lists. Two refusals naming the same dates for opposite reasons, and
    # the assertion could not tell them apart -- the same shape as the
    # "title" collision in #141.
    assert "ambiguous" in message, (
        "this must be the AMBIGUITY refusal, not the generic "
        f"not-an-occurrence one that happens to list the same dates: {message}"
    )
    assert "not an occurrence" not in message, message



def test_a_snooze_landing_on_another_occurrence_is_ambiguous_not_silent(
    client, test_settings
):
    """F1 from review-2, and it is this endpoint's own bug one
    configuration over.

    A weekly reminder snoozed +7 -> +14 puts the moved occurrence on top
    of the series occurrence already at +14. /due lists BOTH rows there,
    correctly -- they are two different occurrences that happen to fall
    on one day. resolve_occurrence used to return at the first branch the
    moment the date was on the series, so complete(+14) cleared the
    series one and left the moved one due, answering 200. Measured
    before the fix: ['+14', '+14', '+21', '+28'] before, and after
    completing +14 the list still held a +14.

    The date now yields two candidates and is refused naming both
    scheduled dates, which is the only answer that does not guess.
    """
    created = _create(
        client, test_settings, start_date=_days(7), recurrence_rule="FREQ=WEEKLY"
    ).json()
    _snooze(client, test_settings, created["id"], _days(7), _days(14))

    listed = [
        item["due_date"]
        for item in _due(client, test_settings, f"?from={_days(0)}&to={_days(40)}")[
            "items"
        ]
        if item["title"] == "Renew passport"
    ]
    assert listed.count(_days(14)) == 2, (
        f"precondition: two occurrences should be listed at +14, got {listed}"
    )

    response = _complete(client, test_settings, created["id"], _days(14))

    assert response.status_code == 422, (
        f"completing an ambiguous date must refuse, not pick one: "
        f"{response.json()}"
    )
    message = response.json()["error"]["details"][0]["message"]
    assert "ambiguous" in message, message
    assert _days(7) in message and _days(14) in message, message


def test_a_bounded_series_refused_far_away_does_not_blame_its_own_rule(
    client, test_settings
):
    """F2 from review-2. The refusal named the wrong cause.

    A ±1y search coming back empty was reported as "this reminder has no
    occurrences at all -- its start_date is unparseable or its
    recurrence_rule is unusable", which is false for a perfectly good
    bounded series asked about a far date, and sends the reader to fix a
    rule that is not broken.
    """
    created = _create(
        client,
        test_settings,
        start_date=_days(7),
        recurrence_rule="FREQ=WEEKLY",
        end_date=_days(30),
    ).json()

    response = _complete(client, test_settings, created["id"], _days(900))

    assert response.status_code == 422, response.json()
    message = response.json()["error"]["details"][0]["message"]
    assert "unusable" not in message and "unparseable" not in message, (
        f"the series is fine; the requested date is far away: {message}"
    )
    assert _days(7) in message and _days(30) in message, (
        f"the refusal should name the series bounds: {message}"
    )


# --- review-2's re-review holds on #136 -----------------------------------


def _due_dates(client, test_settings, title="Renew passport"):
    """What /due actually reports, read back rather than assumed."""
    return [
        item["due_date"]
        for item in _due(client, test_settings, f"?from={_days(0)}&to={_days(40)}")[
            "items"
        ]
        if item["title"] == title
    ]


def test_a_date_with_one_outstanding_occupant_resolves_to_it(
    client, test_settings
):
    """R-F1: the remainder of the class, and it had become a FALSE REFUSAL.

    The candidate set counted a scheduled occurrence on membership
    alone, while /due counts one only if its instance state leaves it
    outstanding. Weekly from +7, complete(+14), snooze(+7 -> +14): the
    resident occupant of +14 is done, so /due shows ONE row there -- and
    completing it answered 422 "identifies 2 occurrences". At an earlier commit
    the same sequence was a silent 200 against the wrong row, so this is
    the same disagreement surfacing the other way round.

    DRIVEN BY THE /due RESPONSE, per the ruling: the count that decides
    whether this is ambiguous is the count /due reports, so the test
    reads it rather than asserting a number it chose.
    """
    from collections import Counter

    created = _create(
        client, test_settings, start_date=_days(7), recurrence_rule="FREQ=WEEKLY"
    ).json()
    _complete(client, test_settings, created["id"], _days(14))

    before = Counter(_due_dates(client, test_settings))
    _snooze(client, test_settings, created["id"], _days(7), _days(14))
    after = Counter(_due_dates(client, test_settings))

    # THE TARGET COMES OUT OF /due, not out of this test. A hardcoded
    # date passes against a resolver that agrees with itself but not
    # with /due, which is the whole failure mode (review-2). The date
    # the snooze moved the row ONTO is the one /due gained.
    gained = list((after - before).elements())
    assert len(gained) == 1, f"expected one moved row, got {gained}"
    target = gained[0]
    assert after[target] == 1, (
        f"precondition: /due should show exactly one row at {target}, "
        f"got {after[target]}"
    )

    response = _complete(client, test_settings, created["id"], target)

    assert response.status_code == 200, (
        f"one outstanding occupant at {target} is not ambiguous; refusing "
        f"it is a false refusal: {response.json()}"
    )
    assert Counter(_due_dates(client, test_settings))[target] == 0, (
        f"the row /due showed at {target} should be gone"
    )


def test_completing_the_same_date_twice_still_works(client, test_settings):
    """The fallback's first reason. that commit calls this normal and the
    UPSERT exists for it, so gating membership on "is it outstanding"
    must not make the second call ambiguous or unresolvable."""
    created = _create(client, test_settings).json()

    first = _complete(client, test_settings, created["id"], _days(7))
    second = _complete(client, test_settings, created["id"], _days(7))

    assert first.status_code == 200 and second.status_code == 200, (
        f"{first.json()} / {second.json()}"
    )


def test_snoozing_a_completed_occurrence_still_resolves(client, test_settings):
    """The fallback's second reason, and the escape hatch from that commit.

    Undoing a completion recorded by mistake is the only thing snoozing
    a done occurrence is for. A membership gate that dropped `done`
    outright would make its date unresolvable and close that door.
    """
    created = _create(client, test_settings).json()
    _complete(client, test_settings, created["id"], _days(7))

    response = _snooze(client, test_settings, created["id"], _days(7), _days(14))

    assert response.status_code == 200, response.json()
    assert _days(14) in _due_dates(client, test_settings)


def test_the_ambiguity_refusal_names_only_dates_that_work(
    client, test_settings
):
    """R-F2: the remedy has to be a remedy.

    The refusal listed every candidate "instead", including the
    requested date itself -- which refuses identically, sending the
    caller back to the same 422. #138's shape one endpoint over.

    Measured: during the ambiguity, completing the MOVED occurrence's
    own scheduled date succeeds, and the shared date then succeeds too
    because only one occupant is left. The message now says exactly
    that, and this test walks it.
    """
    created = _create(
        client, test_settings, start_date=_days(7), recurrence_rule="FREQ=WEEKLY"
    ).json()
    _snooze(client, test_settings, created["id"], _days(7), _days(14))

    refused = _complete(client, test_settings, created["id"], _days(14))
    assert refused.status_code == 422, refused.json()
    message = refused.json()["error"]["details"][0]["message"]
    assert _days(7) in message, message
    # THE CLAUSE MUST END THERE. Asserting the clause with `in` and no
    # terminator matched a PREFIX of a longer list: with the requested
    # date listed too the text reads "...scheduled date: <+7>, <+14>.",
    # which still contains "...scheduled date: <+7>". The mutation that
    # re-added the requested date survived on exactly that, which is the
    # fourth time today a check has read through the distinction it
    # exists to make. The full stop is what pins "only these".
    assert (
        f"Name a moved occurrence by its own scheduled date: {_days(7)}."
        in message
    ), f"the refusal must name ONLY what works: {message}"
    # And the requested date must not be offered as an alternative, even
    # though it appears later in its own sentence.
    offered = message.split("scheduled date: ", 1)[1].split(".", 1)[0]
    assert _days(14) not in offered, (
        f"the requested date was offered as a remedy for itself: {offered!r}"
    )

    # The remedy it gives actually works...
    assert (
        _complete(client, test_settings, created["id"], _days(7)).status_code == 200
    )
    # ...and the shared date works afterwards, as the message promises.
    assert (
        _complete(client, test_settings, created["id"], _days(14)).status_code == 200
    )


def test_an_occurrence_snoozed_onto_its_own_date_is_one_occurrence(
    client, test_settings
):
    """R-F3: pinned through raw SQL, because no API path reaches it.

    `ReminderSnooze` refuses snoozed_to on or before due_date with a
    422, so the self-snooze dedup in resolve_occurrence guards a row
    only a direct write can create -- a restore, or a repair script.
    The table has no constraint forbidding it, so the guard stays; this
    inserts the row the API will not, which is the only way to
    distinguish the guard working from the state being unreachable.
    """
    import sqlite3

    created = _create(client, test_settings).json()
    # The API refuses to create this state...
    refused = _snooze(client, test_settings, created["id"], _days(7), _days(7))
    assert refused.status_code == 422, (
        f"precondition: the API should refuse a self-snooze: {refused.json()}"
    )

    # ...so write it directly, the way a restore would.
    connection = sqlite3.connect(test_settings.db_path)
    try:
        connection.execute(
            "INSERT INTO reminder_instances "
            "(id, reminder_id, due_date, status, snoozed_to) "
            "VALUES (?, ?, ?, 'snoozed', ?)",
            ("self-snooze", created["id"], _days(7), _days(7)),
        )
        connection.commit()
    finally:
        connection.close()

    response = _complete(client, test_settings, created["id"], _days(7))

    assert response.status_code == 200, (
        "an occurrence snoozed onto its own date is one occurrence, not two; "
        f"without the dedup it refuses itself as ambiguous: {response.json()}"
    )


def test_the_resident_occurrence_snoozed_away_behaves_the_same(
    client, test_settings
):
    """review-2's sibling of R-F1: the resident leaves by SNOOZE, not by
    completion.

    Same disagreement, reached the other way. The occurrence already at
    +14 is snoozed on to +21, so it is no longer outstanding there; the
    occurrence from +7 is then moved onto +14. /due shows one row at
    +14 and completing it must clear exactly that row.

    Built in this order because the API refuses a snooze whose target is
    on or before the due date, so the resident has to be moved first.
    """
    from collections import Counter

    created = _create(
        client, test_settings, start_date=_days(7), recurrence_rule="FREQ=WEEKLY"
    ).json()
    _snooze(client, test_settings, created["id"], _days(14), _days(21))

    before = Counter(_due_dates(client, test_settings))
    _snooze(client, test_settings, created["id"], _days(7), _days(14))
    after = Counter(_due_dates(client, test_settings))

    gained = list((after - before).elements())
    assert len(gained) == 1, f"expected one moved row, got {gained}"
    target = gained[0]
    assert after[target] == 1, f"/due should show one row at {target}: {after}"

    response = _complete(client, test_settings, created["id"], target)

    assert response.status_code == 200, response.json()
    assert Counter(_due_dates(client, test_settings))[target] == 0

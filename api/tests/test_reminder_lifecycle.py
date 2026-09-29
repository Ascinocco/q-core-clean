"""Regression scenarios through public routes with an isolated fake provider."""

import json
import sqlite3
from datetime import date, timedelta
from urllib.error import HTTPError

import pytest

from api.tests.test_financial import _auth


def day(n):
    return (date.today() + timedelta(days=n)).isoformat()


def create(client, settings, **fields):
    response = client.post(
        "/reminders",
        headers=_auth(settings),
        json={"title": "Synthetic appointment", "start_date": day(1), **fields},
    )
    assert response.status_code == 200, response.text
    return response.json()


def act(client, settings, reminder, action, **fields):
    return client.post(
        f"/reminders/{reminder['id']}/{action}", headers=_auth(settings), json=fields
    )


def patch(client, settings, reminder, **fields):
    return client.patch(
        f"/reminders/{reminder['id']}", headers=_auth(settings), json=fields
    )


def due(client, settings, **params):
    return client.get(
        "/due", headers=_auth(settings), params={"source": "reminder", **params}
    ).json()["items"]


@pytest.fixture()
def calendar(client, test_settings, monkeypatch):
    from api import google_calendar as google

    with sqlite3.connect(test_settings.db_path) as connection:
        connection.execute(
            "INSERT INTO google_calendar_connection (id, calendar_id, keychain_service) VALUES (1, 'synthetic', 'test')"
        )
    monkeypatch.setattr(google, "_access_token", lambda: "fake-token")
    state = {"events": {}, "fail": False, "lose_insert_response": False, "calls": []}

    def request(url, method="GET", body=None, token=None):
        state["calls"].append((method, url, body))
        if state["fail"]:
            raise OSError("synthetic provider outage")
        key = url.rsplit("/", 1)[-1]
        events = state["events"]
        if method == "POST":
            key = body["id"]
            if key in events:
                raise HTTPError(url, 409, "exists", {}, None)
            events[key] = json.loads(json.dumps(body))
            if state["lose_insert_response"]:
                state["lose_insert_response"] = False
                raise TimeoutError("insert succeeded, response lost")
        elif method == "PUT":
            if key not in events:
                raise HTTPError(url, 404, "missing", {}, None)
            events[key] = {"id": key, **body}
        elif method == "DELETE":
            if key not in events:
                raise HTTPError(url, 404, "missing", {}, None)
            del events[key]
            return {}
        return events[key]

    monkeypatch.setattr(google, "_request", request)
    return state


def test_seven_day_aging_and_explicit_window(client, test_settings):
    for n in (-8, -7, -1, 0, 1):
        create(client, test_settings, title=f"Item {n}", start_date=day(n))
    assert [item["due_date"] for item in due(client, test_settings)] == [
        day(n) for n in (-7, -1, 0, 1)
    ]
    assert [
        item["due_date"]
        for item in due(client, test_settings, **{"from": day(-8), "to": day(-8)})
    ] == [day(-8)]
    assert (
        len(client.get("/reminders", headers=_auth(test_settings)).json()["items"]) == 5
    )


@pytest.mark.parametrize("rule", [None, "FREQ=YEARLY"])
def test_snooze_crosses_window_and_ages_from_effective_date(
    client, test_settings, rule
):
    reminder = create(client, test_settings, start_date=day(-40), recurrence_rule=rule)
    assert (
        act(
            client,
            test_settings,
            reminder,
            "snooze",
            due_date=day(-40),
            snoozed_to=day(-7),
        ).status_code
        == 200
    )
    items = due(client, test_settings)
    assert len(items) == 1
    assert items[0]["reminder_id"] == reminder["id"]
    assert items[0]["occurrence_date"] == day(-40)
    assert items[0]["due_date"] == day(-7)
    assert len(due(client, test_settings, **{"from": day(-7), "to": day(-7)})) == 1
    assert (
        act(client, test_settings, reminder, "complete", due_date=day(-7)).status_code
        == 200
    )
    assert due(client, test_settings) == []


def test_edit_keeps_identity_and_requires_explicit_history_reset(
    client, test_settings, calendar
):
    reminder = create(
        client,
        test_settings,
        notes="note",
        due_time="09:00",
        recurrence_rule="FREQ=DAILY",
    )
    event_id = next(iter(calendar["events"]))
    assert (
        act(client, test_settings, reminder, "complete", due_date=day(1)).status_code
        == 200
    )
    edited = patch(
        client,
        test_settings,
        reminder,
        title="Changed",
        notes=None,
        notification_offsets_minutes=[60],
    )
    assert edited.status_code == 200
    assert edited.json()["id"] == reminder["id"] and edited.json()["notes"] is None
    assert list(calendar["events"]) == [event_id]
    assert calendar["events"][event_id]["summary"] == "Changed"
    assert all(item["due_date"] != day(1) for item in due(client, test_settings))
    assert patch(client, test_settings, reminder, start_date=day(2)).status_code == 409
    assert (
        patch(
            client, test_settings, reminder, start_date=day(2), reset_occurrences=True
        ).status_code
        == 200
    )
    assert calendar["events"][event_id]["recurrence"] == ["RRULE:FREQ=DAILY"]
    for fields in (
        {},
        {"title": None},
        {"notification_offsets_minutes": None},
        {"recurrence_rule": "invalid"},
        {"end_date": day(-2)},
    ):
        assert patch(client, test_settings, reminder, **fields).status_code == 422


def test_oneoff_completion_snooze_and_outage_recovery(client, test_settings, calendar):
    reminder = create(client, test_settings, due_time="09:00")
    event_id = next(iter(calendar["events"]))
    assert (
        act(
            client,
            test_settings,
            reminder,
            "snooze",
            due_date=day(1),
            snoozed_to=day(4),
        ).status_code
        == 200
    )
    assert calendar["events"][event_id]["start"]["dateTime"] == day(4) + "T09:00:00"
    calendar["fail"] = True
    assert (
        act(client, test_settings, reminder, "complete", due_date=day(4)).status_code
        == 200
    )
    assert due(client, test_settings) == []
    assert client.get(
        "/integrations/google/status", headers=_auth(test_settings)
    ).json()["connection"]["last_error"]
    assert (
        client.post(
            "/integrations/google/sync", headers=_auth(test_settings)
        ).status_code
        == 503
    )
    calendar["fail"] = False
    for _ in range(2):
        assert (
            client.post(
                "/integrations/google/sync", headers=_auth(test_settings)
            ).status_code
            == 200
        )
        assert calendar["events"] == {}
    assert (
        act(
            client,
            test_settings,
            reminder,
            "snooze",
            due_date=day(1),
            snoozed_to=day(5),
        ).status_code
        == 200
    )
    assert len(calendar["events"]) == 1


@pytest.mark.parametrize("time", [None, "09:00"])
def test_recurring_exceptions_survive_full_sync(client, test_settings, calendar, time):
    reminder = create(
        client, test_settings, due_time=time, recurrence_rule="FREQ=DAILY"
    )
    master = next(iter(calendar["events"]))
    assert (
        act(
            client,
            test_settings,
            reminder,
            "snooze",
            due_date=day(1),
            snoozed_to=day(40),
        ).status_code
        == 200
    )
    assert len(calendar["events"]) == 2
    assert any("EXDATE" in part for part in calendar["events"][master]["recurrence"])
    assert (
        act(client, test_settings, reminder, "complete", due_date=day(2)).status_code
        == 200
    )
    assert len(calendar["events"][master]["recurrence"]) == 3
    before = json.loads(json.dumps(calendar["events"]))
    assert (
        client.post(
            "/integrations/google/sync", headers=_auth(test_settings)
        ).status_code
        == 200
    )
    assert calendar["events"] == before
    # Delete the entire series during an outage; retry must remove both events.
    calendar["fail"] = True
    assert (
        client.delete(
            f"/reminders/{reminder['id']}", headers=_auth(test_settings)
        ).status_code
        == 200
    )
    calendar["fail"] = False
    assert (
        client.post(
            "/integrations/google/sync", headers=_auth(test_settings)
        ).status_code
        == 200
    )
    assert calendar["events"] == {}


def test_insert_timeout_does_not_duplicate_on_retry(client, test_settings, calendar):
    calendar["lose_insert_response"] = True
    create(client, test_settings)
    assert len(calendar["events"]) == 1
    assert (
        client.post(
            "/integrations/google/sync", headers=_auth(test_settings)
        ).status_code
        == 200
    )
    assert len(calendar["events"]) == 1


@pytest.mark.parametrize("entity_type", ["person", "pet"])
def test_birthday_source_identity_overrides_removal_and_calendar(
    client, test_settings, calendar, entity_type
):
    entity = client.post(
        "/entities",
        headers=_auth(test_settings),
        json={
            "type": entity_type,
            "name": "Synthetic Friend",
            "attributes": {"date_of_birth": "2000-04-27"},
        },
    ).json()

    def reminders():
        return client.get(
            "/reminders",
            headers=_auth(test_settings),
            params={"entity_id": entity["id"]},
        ).json()["items"]

    birthday = reminders()[0]
    assert birthday["source_kind"] == "birthday"
    assert birthday["notification_offsets_minutes"] == [10080, 1440, 60]
    assert birthday["due_time"] == "09:00:00"
    event = next(iter(calendar["events"].values()))
    assert event["start"]["timeZone"] == "America/New_York"
    assert event["start"]["dateTime"].endswith("T09:00:00")
    assert (
        patch(
            client,
            test_settings,
            birthday,
            notification_offsets_minutes=[60],
            due_time="10:00",
        ).status_code
        == 200
    )
    for _ in range(2):
        assert (
            client.patch(
                f"/entities/{entity['id']}",
                headers=_auth(test_settings),
                json={"name": "Renamed Friend"},
            ).status_code
            == 200
        )
    assert len(reminders()) == 1 and reminders()[0]["id"] == birthday["id"]
    assert reminders()[0]["notification_offsets_minutes"] == [60]
    assert reminders()[0]["due_time"] == "10:00:00"
    assert patch(client, test_settings, birthday, start_date=day(9)).status_code == 409
    assert (
        client.delete(
            f"/reminders/{birthday['id']}", headers=_auth(test_settings)
        ).status_code
        == 409
    )
    assert (
        client.patch(
            f"/entities/{entity['id']}",
            headers=_auth(test_settings),
            json={"attributes": {"date_of_birth": "2000-05-18"}},
        ).status_code
        == 200
    )
    assert reminders()[0]["id"] == birthday["id"] and reminders()[0][
        "start_date"
    ].endswith("-05-18")
    assert (
        client.patch(
            f"/entities/{entity['id']}",
            headers=_auth(test_settings),
            json={
                "attributes": {
                    "date_of_birth": "2000-05-18",
                    "birthday_reminder_enabled": False,
                }
            },
        ).status_code
        == 200
    )
    assert reminders() == [] and calendar["events"] == {}


def test_leap_day_and_deactivation(client, test_settings):
    entity = client.post(
        "/entities",
        headers=_auth(test_settings),
        json={
            "type": "person",
            "name": "Leap Friend",
            "attributes": {"date_of_birth": "2000-02-29"},
        },
    ).json()
    items = due(client, test_settings, **{"from": "2027-01-01", "to": "2028-12-31"})
    assert [item["due_date"] for item in items] == ["2027-02-28", "2028-02-29"]
    assert (
        client.patch(
            f"/entities/{entity['id']}",
            headers=_auth(test_settings),
            json={"status": "inactive"},
        ).status_code
        == 200
    )
    assert client.get("/reminders", headers=_auth(test_settings)).json()["items"] == []


def test_nullable_edits_and_series_end_calendar(client, test_settings, calendar):
    reminder = create(
        client, test_settings, due_time="09:00", recurrence_rule="FREQ=DAILY;COUNT=10"
    )
    event_id = next(iter(calendar["events"]))
    assert (
        patch(
            client, test_settings, reminder, end_date=day(3), due_time=None
        ).status_code
        == 200
    )
    event = calendar["events"][event_id]
    assert event["recurrence"] == ["RRULE:FREQ=DAILY;COUNT=3"]
    assert event["start"] == {"date": day(1)}
    assert (
        patch(
            client, test_settings, reminder, recurrence_rule=None, end_date=None
        ).status_code
        == 200
    )
    assert "recurrence" not in calendar["events"][event_id]


def test_manual_birthday_conflict_rolls_back_entity_update(client, test_settings):
    entity = client.post(
        "/entities",
        headers=_auth(test_settings),
        json={"type": "person", "name": "Manual Friend"},
    ).json()
    manual = create(
        client,
        test_settings,
        entity_id=entity["id"],
        start_date="2027-05-18",
        recurrence_rule="FREQ=YEARLY",
    )
    response = client.patch(
        f"/entities/{entity['id']}",
        headers=_auth(test_settings),
        json={"attributes": {"date_of_birth": "2000-05-18"}},
    )
    assert response.status_code == 409
    assert (
        client.get(f"/entities/{entity['id']}", headers=_auth(test_settings)).json()[
            "attributes"
        ]
        == {}
    )
    assert (
        client.get("/reminders", headers=_auth(test_settings)).json()["items"][0]["id"]
        == manual["id"]
    )


def test_parallel_edits_preserve_independent_fields(client, test_settings):
    from concurrent.futures import ThreadPoolExecutor

    reminder = create(client, test_settings)
    with ThreadPoolExecutor(2) as executor:
        calls = [
            executor.submit(patch, client, test_settings, reminder, **fields)
            for fields in ({"notes": "new note"}, {"location": "new place"})
        ]
        assert all(call.result().status_code == 200 for call in calls)
    saved = client.get(
        f"/reminders/{reminder['id']}", headers=_auth(test_settings)
    ).json()
    assert saved["notes"] == "new note" and saved["location"] == "new place"


def test_google_empty_delete_response(monkeypatch):
    from contextlib import contextmanager
    from api import google_calendar as google

    @contextmanager
    def response(*args, **kwargs):
        from io import BytesIO

        yield BytesIO(b"")

    monkeypatch.setattr(google, "urlopen", response)
    assert (
        google._request("https://www.googleapis.com/calendar/v3/synthetic", "DELETE")
        == {}
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"due_time": "09:00:00.123"},
        {"due_time": "09:00:00+02:00"},
        {"notification_offsets_minutes": [40321]},
        {"notification_offsets_minutes": [1, 2, 3, 4, 5, 6]},
    ],
)
def test_notification_inputs_are_deliverable(client, test_settings, fields):
    reminder = create(client, test_settings)
    assert patch(client, test_settings, reminder, **fields).status_code == 422


@pytest.mark.parametrize(
    "manual_start,rule,birthday,conflict",
    [
        ("2026-01-01", "FREQ=YEARLY;BYMONTH=5;BYMONTHDAY=18", "2000-05-18", True),
        ("2030-01-01", "FREQ=YEARLY;BYMONTH=5;BYMONTHDAY=18", "2000-05-18", True),
        ("2026-01-01", "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=28", "2000-02-29", True),
        ("2026-05-18", "FREQ=YEARLY;BYMONTH=6;BYMONTHDAY=10", "2000-05-18", False),
    ],
)
def test_manual_birthday_uses_occurrences_not_anchor(
    client, test_settings, monkeypatch, manual_start, rule, birthday, conflict
):
    from datetime import datetime

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 22, tzinfo=tz)

    monkeypatch.setattr("api.birthdays.datetime", FixedDatetime)
    entity = client.post(
        "/entities",
        headers=_auth(test_settings),
        json={"type": "person", "name": "Synthetic annual reminder owner"},
    ).json()
    manual = create(
        client,
        test_settings,
        entity_id=entity["id"],
        start_date=manual_start,
        recurrence_rule=rule,
    )
    response = client.patch(
        f"/entities/{entity['id']}",
        headers=_auth(test_settings),
        json={"attributes": {"date_of_birth": birthday}},
    )
    assert response.status_code == (409 if conflict else 200)
    reminders = client.get(
        "/reminders", headers=_auth(test_settings), params={"entity_id": entity["id"]}
    ).json()["items"]
    assert len(reminders) == (1 if conflict else 2)
    assert next(r for r in reminders if r["id"] == manual["id"]) == manual
    if conflict:
        assert (
            client.get(
                f"/entities/{entity['id']}", headers=_auth(test_settings)
            ).json()["attributes"]
            == {}
        )

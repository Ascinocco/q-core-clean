"""Maintain source-owned birthday reminders inside the entity transaction."""

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo
from uuid import uuid4

from api.errors import ConflictError
from api.due import _occurrences
from api.google_calendar import queue_reminder_deletion


def maintain_birthday(connection, entity_id: str) -> str | None:
    entity = connection.execute(
        "SELECT * FROM entities WHERE id = ?", (entity_id,)
    ).fetchone()
    if entity["type"] not in ("person", "pet"):
        return None
    attributes = json.loads(entity["attributes"] or "{}")
    existing = connection.execute(
        "SELECT * FROM reminders WHERE entity_id = ? AND source_kind = 'birthday'",
        (entity_id,),
    ).fetchone()
    birthday = attributes.get("date_of_birth")
    if (
        not birthday
        or attributes.get("birthday_reminder_enabled") is False
        or entity["status"] != "active"
    ):
        if existing:
            queue_reminder_deletion(connection, existing["id"])
            connection.execute(
                "DELETE FROM reminder_instances WHERE reminder_id = ?",
                (existing["id"],),
            )
            connection.execute("DELETE FROM reminders WHERE id = ?", (existing["id"],))
            return existing["id"]
        return None
    born = date.fromisoformat(birthday)
    leap_day = (born.month, born.day) == (2, 29)
    rule = (
        "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=-1"
        if leap_day
        else f"FREQ=YEARLY;BYMONTH={born.month};BYMONTHDAY={born.day}"
    )
    today = datetime.now(ZoneInfo("America/New_York")).date()

    def in_year(year):
        from calendar import monthrange

        return date(year, born.month, min(born.day, monthrange(year, born.month)[1]))

    start = in_year(today.year)
    if start < today:
        start = in_year(today.year + 1)
    title = f"{entity['name']} birthday"
    if existing:
        # Repeat entity saves keep the original recurrence anchor and user overrides.
        changed = existing["recurrence_rule"] != rule
        connection.execute(
            "UPDATE reminders SET title = ?, start_date = ?, recurrence_rule = ? WHERE id = ?",
            (
                title,
                start.isoformat() if changed else existing["start_date"],
                rule,
                existing["id"],
            ),
        )
        if changed:
            connection.execute(
                "DELETE FROM reminder_instances WHERE reminder_id = ?",
                (existing["id"],),
            )
        return existing["id"]
    manual = connection.execute(
        "SELECT * FROM reminders WHERE entity_id = ? AND source_kind IS NULL AND recurrence_rule LIKE '%FREQ=YEARLY%'",
        (entity_id,),
    ).fetchall()
    for item in manual:
        # DTSTART is an expansion anchor, not necessarily an occurrence:
        # FREQ=YEARLY;BYMONTH=5;BYMONTHDAY=18 can start on January 1.
        # Check the next birthday and the first birthday at/after a later
        # manual start. This is a bounded guard, not an arbitrary RRULE
        # intersection solver; manual reminders are never adopted or deleted.
        manual_start = date.fromisoformat(item["start_date"])
        at_manual_start = in_year(max(start.year, manual_start.year))
        if at_manual_start < manual_start and at_manual_start.year < date.max.year:
            at_manual_start = in_year(at_manual_start.year + 1)
        if any(
            _occurrences(item, candidate, candidate)
            for candidate in {start, at_manual_start}
        ):
            raise ConflictError(
                "An existing manual annual reminder occurs on this birthday. "
                "Review it before enabling the automatic birthday reminder, or "
                "set birthday_reminder_enabled=false."
            )
    reminder_id = str(uuid4())
    connection.execute(
        "INSERT INTO reminders (id, title, entity_id, source_kind, start_date, recurrence_rule, due_time, notification_offsets_json) VALUES (?, ?, ?, 'birthday', ?, ?, '09:00:00', '[10080,1440,60]')",
        (reminder_id, title, entity_id, start.isoformat(), rule),
    )
    return reminder_id

"""CRUD for `reminders`, and the two writers of `reminder_instances`.

`GET /due` (api/due.py) already reads both tables; until this landed the
`reminder` source had no producer outside direct SQL. The split is
deliberate: `/due` is the only reader and answers "what's coming up"
across all three sources, so nothing here duplicates it — there is no
`GET /reminders/due`.

`complete` and `snooze` are here rather than on `/due` because they
write, and `/due` is a read. They are the only things that create a
`reminder_instances` row: without them a recurring reminder nags
forever and a one-off can never be marked done.
"""

import json
import sqlite3
from datetime import date, datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, Query

from api.auth import require_token
from api.db import require_reference, MAX_LIMIT, get_connection, paginate
from api.due import resolve_occurrence
from api.errors import InvalidReferenceError, NotFoundError, ConflictError
from api.google_calendar import (
    serialized_reminder_write,
    sync_reminder_safely,
    queue_reminder_deletion,
)
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from api.models import (
    ReminderCreate,
    ReminderUpdate,
    ReminderInstanceCreate,
    ReminderInstanceResponse,
    ReminderResponse,
    ReminderSnooze,
)

router = APIRouter(dependencies=[Depends(require_token)])


def _get_reminder_row(connection: sqlite3.Connection, reminder_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"No reminder with id {reminder_id!r}")
    return row


def _reminder_response(row: sqlite3.Row) -> dict:
    result = dict(row)
    result["notification_offsets_minutes"] = json.loads(
        result.pop("notification_offsets_json")
    )
    return result


def _require_entity(connection: sqlite3.Connection, entity_id: str | None) -> None:
    if entity_id is None:
        return
    if (
        connection.execute(
            "SELECT 1 FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        is None
    ):
        # 400, not 422: InvalidReferenceError is what the repo raises for a
        # reference to something that does not exist (see D17 on #76).
        raise InvalidReferenceError(f"No entity with id {entity_id!r}")


@router.post("/reminders", response_model=ReminderResponse)
@serialized_reminder_write
def create_reminder(
    body: ReminderCreate,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    _require_entity(connection, body.entity_id)

    reminder_id = str(uuid4())
    connection.execute(
        "INSERT INTO reminders (id, title, notes, entity_id, recurrence_rule, "
        "start_date, end_date, due_time, location, notification_offsets_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            reminder_id,
            body.title,
            body.notes,
            body.entity_id,
            body.recurrence_rule,
            body.start_date.isoformat(),
            body.end_date.isoformat() if body.end_date is not None else None,
            body.due_time.isoformat() if body.due_time is not None else None,
            body.location,
            json.dumps(body.notification_offsets_minutes),
        ),
    )
    connection.commit()
    sync_reminder_safely(connection, reminder_id)
    return _reminder_response(_get_reminder_row(connection, reminder_id))


@router.patch("/reminders/{reminder_id}", response_model=ReminderResponse)
@serialized_reminder_write
def update_reminder(
    reminder_id: str,
    body: ReminderUpdate,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    connection.execute("BEGIN IMMEDIATE")
    old = _reminder_response(_get_reminder_row(connection, reminder_id))
    patch = body.model_dump(
        mode="json", exclude_unset=True, exclude={"reset_occurrences"}
    )
    protected = {"title", "entity_id", "start_date", "end_date", "recurrence_rule"}
    if old["source_kind"] == "birthday" and any(
        old[key] != value for key, value in patch.items() if key in protected
    ):
        raise ConflictError(
            "Edit the entity to change its managed birthday title or schedule. Reminder time, notes, location and notification overrides are editable."
        )
    merged = {key: old[key] for key in ReminderCreate.model_fields}
    merged.update(patch)
    try:
        validated = ReminderCreate.model_validate(merged).model_dump(mode="json")
    except ValidationError as exc:
        raise RequestValidationError(
            [{**error, "loc": ("body", *error["loc"])} for error in exc.errors()]
        ) from exc
    _require_entity(connection, validated["entity_id"])
    schedule_changed = any(
        validated[key] != old[key]
        for key in ("start_date", "recurrence_rule", "end_date")
    )
    if schedule_changed:
        has_history = connection.execute(
            "SELECT 1 FROM reminder_instances WHERE reminder_id = ? LIMIT 1",
            (reminder_id,),
        ).fetchone()
        if has_history and not body.reset_occurrences:
            raise ConflictError(
                "Schedule changes with occurrence history require reset_occurrences=true; this clears completion and snooze history for the series."
            )
        connection.execute(
            "DELETE FROM reminder_instances WHERE reminder_id = ?", (reminder_id,)
        )
    validated["notification_offsets_json"] = json.dumps(
        validated.pop("notification_offsets_minutes")
    )
    connection.execute(
        "UPDATE reminders SET "
        + ", ".join(f"{key} = ?" for key in validated)
        + " WHERE id = ?",
        (*validated.values(), reminder_id),
    )
    connection.commit()
    sync_reminder_safely(connection, reminder_id)
    return _reminder_response(_get_reminder_row(connection, reminder_id))


@router.get("/reminders/{reminder_id}", response_model=ReminderResponse)
def get_reminder(
    reminder_id: str,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    return _reminder_response(_get_reminder_row(connection, reminder_id))


#: The order this endpoint returns, stated once beside the query
#: that produces it (ORDER BY start_date, title COLLATE NOCASE, id). The tool description
#: interpolates this rather than retyping it, and a behavioural
#: test asserts the rows actually come back this way -- a flipped
#: ORDER BY is invisible to every caller who believed the sentence.
LIST_REMINDERS_ORDER = "by start_date"


@router.get("/reminders")
def list_reminders(
    entity_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    # A filter naming something that does not exist is refused, not
    # answered with an empty page: the two are indistinguishable to
    # the caller, and only one of them is true (ticket T-08).
    require_reference(connection, "entities", entity_id, "entity")

    # Ordered on a total key, not on start_date alone: two reminders can
    # share a start date, and a caller paging through an unstable order
    # can see a row twice or not at all with nothing reporting it. Same
    # reason the merchant matcher names its tie-break (D15).
    query = "SELECT * FROM reminders WHERE 1=1"
    params: list = []
    if entity_id is not None:
        query += " AND entity_id = ?"
        params.append(entity_id)
    query += " ORDER BY start_date, title COLLATE NOCASE, id"
    page = paginate(connection, query, tuple(params), limit=limit, offset=offset)
    page["items"] = [_reminder_response(item) for item in page["items"]]
    return page


@router.delete("/reminders/{reminder_id}")
@serialized_reminder_write
def delete_reminder(
    reminder_id: str,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    _get_reminder_row(connection, reminder_id)
    row = _get_reminder_row(connection, reminder_id)
    if row["source_kind"] == "birthday":
        raise ConflictError(
            "Disable birthday_reminder_enabled on the entity to remove its managed reminder."
        )
    queue_reminder_deletion(connection, reminder_id)
    # Instances first, in the same transaction. reminder_instances.
    # reminder_id is a foreign key, so leaving them would either fail the
    # delete or orphan rows that no longer resolve to a reminder — and
    # nothing reads an orphaned instance, so it would be invisible.
    connection.execute(
        "DELETE FROM reminder_instances WHERE reminder_id = ?", (reminder_id,)
    )
    connection.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
    connection.commit()
    sync_reminder_safely(connection, reminder_id)
    return {"deleted": reminder_id}


def _upsert_instance(
    connection: sqlite3.Connection,
    reminder_id: str,
    due: date,
    status: str,
    completed_at: str | None,
    snoozed_to: str | None,
) -> dict:
    # UPSERT rather than INSERT: (reminder_id, due_date) is UNIQUE, and
    # completing an occurrence twice, or snoozing one that was already
    # snoozed, is a normal thing to do — not an error worth a 409.
    connection.execute(
        "INSERT INTO reminder_instances "
        "(id, reminder_id, due_date, status, completed_at, snoozed_to) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(reminder_id, due_date) DO UPDATE SET "
        "status = excluded.status, completed_at = excluded.completed_at, "
        "snoozed_to = excluded.snoozed_to",
        (str(uuid4()), reminder_id, due.isoformat(), status, completed_at, snoozed_to),
    )
    connection.commit()
    row = connection.execute(
        "SELECT * FROM reminder_instances WHERE reminder_id = ? AND due_date = ?",
        (reminder_id, due.isoformat()),
    ).fetchone()
    result = dict(row)
    sync_reminder_safely(connection, reminder_id)
    return result


@router.post(
    "/reminders/{reminder_id}/complete", response_model=ReminderInstanceResponse
)
@serialized_reminder_write
def complete_reminder(
    reminder_id: str,
    body: ReminderInstanceCreate,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    row = _get_reminder_row(connection, reminder_id)
    # Resolved, not taken literally: the date GET /due reports for a
    # snoozed occurrence is its snoozed_to, and that is not the key this
    # row is stored under. See resolve_occurrence.
    due = resolve_occurrence(connection, row, body.due_date)
    return _upsert_instance(
        connection,
        reminder_id,
        due,
        "done",
        datetime.now().isoformat(timespec="seconds"),
        None,
    )


@router.post("/reminders/{reminder_id}/snooze", response_model=ReminderInstanceResponse)
@serialized_reminder_write
def snooze_reminder(
    reminder_id: str,
    body: ReminderSnooze,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    row = _get_reminder_row(connection, reminder_id)
    # Snoozing a COMPLETED occurrence deliberately still un-completes it,
    # and test_snoozing_after_completing_overwrites_the_status pins that.
    # It was reported as "resurrect on snooze" out of the live exercise
    # and it is not a defect: the caller asked for that occurrence back,
    # and this is the only way to undo a completion recorded by mistake.
    # What made it look like one was the identity bug below -- the
    # reported date not clearing the occurrence, so a caller retried with
    # combinations until something moved.
    due = resolve_occurrence(connection, row, body.due_date)
    return _upsert_instance(
        connection,
        reminder_id,
        due,
        "snoozed",
        None,
        body.snoozed_to.isoformat(),
    )

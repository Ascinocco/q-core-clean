"""Key-family date fields accept a calendar date and nothing else.

Pydantic v2 accepts a datetime STRING for a `date` field when the time is
exactly `00:00:00` -- and only then -- keeping the calendar date as
written and DISCARDING the offset (measured on 2.13.5). For a field that
is part of the account-scoped de-duplication key, that is a key bug:

    '2026-05-04T00:00:00+14:00'  ->  stored 2026-05-04
                                     UTC date is 2026-05-03

So two callers naming the same instant in different offsets write
different keys, and one caller naming different instants writes the same
key -- both with a 200.

WHY THE BOUNDARY WAS MISSED TWICE. The first probe set used six inputs,
five with non-midnight times and one date-only, and reported "422 in
every case" -- true of everything it tried, and blind to the only value
the implementation treats differently. A separate probe used midnight and
saw the opposite. Both were right about different inputs. That is why the
midnight row below is the one that matters, and why the table includes
00:00:01 and 23:30 either side of it: a boundary is only pinned from both
directions.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import BaseModel, ValidationError

from api.models import (
    ImportStatementRequest,
    ReminderInstanceCreate,
    ReminderSnooze,
    StatementCreate,
    TransactionInput,
)

#: (raw value, accepted?) -- the amended table from the ticket.
CASES = [
    ("2026-05-04", True),
    ("2026-05-04T00:00:00+01:00", False),
    ("2026-05-04T00:00:00-05:00", False),
    ("2026-05-04T00:00:00+14:00", False),
    ("2026-05-04T00:00:00Z", False),
    ("2026-05-04T00:00:00", False),
    ("2026-05-04T00:00:01+01:00", False),
    ("2026-05-04T23:30:00+00:00", False),
    ("05/04/2026", False),
]


def _transaction(value):
    return TransactionInput(txn_date=value, description="X", amount_cents=-1)


def _statement(value):
    return StatementCreate(account_id="a", period_start=value, period_end="2026-06-04")


def _instance(value):
    return ReminderInstanceCreate(due_date=value)


def _snooze(value):
    # snoozed_to is kept well clear of due_date so the only thing under
    # test is due_date -- model_post_init would otherwise reject the pair
    # for its own reason and hide the result.
    return ReminderSnooze(due_date=value, snoozed_to="2026-12-31")


def _import(value):
    return ImportStatementRequest(
        account_id="a", period_start=value, period_end="2026-06-04", transactions=[]
    )


@pytest.mark.parametrize("build",
                         [_transaction, _statement, _import, _instance, _snooze],
                         ids=["txn_date", "period_start/StatementCreate",
                              "period_start/ImportStatementRequest",
                              "due_date/ReminderInstanceCreate",
                              "due_date/ReminderSnooze"])
@pytest.mark.parametrize("raw,accepted", CASES, ids=[c[0] for c in CASES])
def test_key_date_accepts_only_a_calendar_date(build, raw, accepted):
    if accepted:
        assert build(raw)
    else:
        with pytest.raises(ValidationError) as excinfo:
            build(raw)
        assert "calendar date" in str(excinfo.value)


def test_the_utc_midnight_form_is_refused_too():
    """It reads as unambiguous and is not, which is the trap.

    `...T00:00:00Z` names an instant a human can resolve without context.
    The system cannot: the offset is discarded before the value reaches
    the key, so accepting it would mean accepting a value whose timezone
    is then ignored. "Unambiguous to a reader" and "unambiguous to the
    key" are different properties, and only the second one is being
    protected here.
    """
    with pytest.raises(ValidationError):
        _transaction("2026-05-04T00:00:00Z")


def test_a_date_object_passes_and_a_datetime_object_does_not():
    """A `datetime` is a `date` subclass, so it must be refused explicitly.

    A caller holding a `date` has already made the choice this rejects;
    one holding a `datetime` has not, and the truncation would be silent.
    """
    assert _transaction(date(2026, 5, 4)).txn_date == date(2026, 5, 4)
    for moment in (
        datetime(2026, 5, 4, tzinfo=timezone.utc),
        datetime(2026, 5, 4, tzinfo=timezone(timedelta(hours=14))),
        datetime(2026, 5, 4),
    ):
        with pytest.raises(ValidationError):
            _transaction(moment)


def test_the_offset_would_have_changed_the_stored_key():
    """Pins WHY this matters, not just that it is refused.

    Without the guard, `+14:00` midnight stored 2026-05-04 while the
    instant's UTC date was 2026-05-03. This asserts the arithmetic that
    makes the two disagree, so a future reader can see the stakes without
    re-deriving them.
    """
    moment = datetime.fromisoformat("2026-05-04T00:00:00+14:00")
    assert moment.date() == date(2026, 5, 4)
    assert moment.astimezone(timezone.utc).date() == date(2026, 5, 3)


# --------------------------------------------------------------------------
# The classification, asserted.
#
# In #116 this inventory was PROSE in the PR body, and it claimed the plain
# bucket was safe because "none reaches a key". That was false:
# reminder_instances has UNIQUE(reminder_id, due_date) and the completion
# path upserts ON CONFLICT(reminder_id, due_date), so due_date decides
# whether completing an occurrence inserts a new row or silently overwrites
# one. Prose cannot fail, so nothing caught it. These tests can.
# --------------------------------------------------------------------------

import inspect
from pathlib import Path

from pydantic import BeforeValidator

import api.models as models_module
from api.models import _date_only

SCHEMA = Path(__file__).resolve().parents[2] / "db" / "schema.sql"

#: Part of a key: the value decides identity, so a silent one-day shift
#: changes WHICH row is written, not just what it says.
KEY_DATE_FIELDS = {
    "TransactionInput.txn_date",
    "StatementCreate.period_start",
    "StatementCreate.period_end",
    "ImportStatementRequest.period_start",
    "ImportStatementRequest.period_end",
    "ReminderInstanceCreate.due_date",
    "ReminderSnooze.due_date",
}

#: Built from stored ISO strings on the way out; never carry caller input.
RESPONSE_DATE_FIELDS = {
    "TransactionResponse.txn_date",
    "StatementResponse.period_start",
    "StatementResponse.period_end",
    "RelationshipResponse.start_date",
    "RelationshipResponse.end_date",
    "ReminderResponse.start_date",
    "ReminderResponse.end_date",
    "ReminderInstanceResponse.due_date",
    "ReminderInstanceResponse.snoozed_to",
}

#: Reaches no key. A truncated value here is a wrong DISPLAYED date --
#: visible to the person who entered it and correctable with a PATCH.
#: ReminderSnooze.snoozed_to is deliberately here: the upsert writes it
#: through `excluded.snoozed_to` as a value column, and it is never the
#: conflict target. Widening KeyDate to this whole bucket is a defensible
#: follow-up, but it is a different argument -- consistency, not key
#: integrity -- and belongs on its own terms.
PLAIN_DATE_FIELDS = {
    "AccountAttributes.opened_date",
    "AccountAttributes.renewal_date",
    "Mortgage.origination_date",
    "PersonAttributes.date_of_birth",
    "PetAttributes.date_of_birth",
    "PetAttributes.vaccination_due",
    "PropertyAttributes.purchase_date",
    "PropertyAttributes.insurance_renewal",
    "PropertyAttributes.tax_due",
    "RelationshipCreate.start_date",
    "RelationshipCreate.end_date",
    # Same two dates, on the amend path. Plain rather than key: D93
    # keeps the dedup key (pair + type) off RelationshipUpdate
    # entirely, so neither of these can be part of one.
    "RelationshipUpdate.start_date",
    "RelationshipUpdate.end_date",
    "ReminderCreate.start_date",
    "ReminderCreate.end_date",
    "ReminderUpdate.start_date",
    "ReminderUpdate.end_date",
    "ReminderSnooze.snoozed_to",
    "VehicleAttributes.purchase_date",
    "VehicleAttributes.registration_expiry",
    "VehicleAttributes.warranty_end",
}


def _date_fields():
    """Every date-annotated field defined in api.models -> (label, guarded)."""
    found = {}
    for name, obj in vars(models_module).items():
        if not (inspect.isclass(obj) and issubclass(obj, BaseModel)):
            continue
        if obj is BaseModel or obj.__module__ != models_module.__name__:
            continue
        for field_name, field in obj.model_fields.items():
            if field.annotation not in (date, date | None):
                continue
            guarded = any(
                isinstance(m, BeforeValidator) and m.func is _date_only
                for m in field.metadata
            )
            found[f"{name}.{field_name}"] = guarded
    return found


def test_every_date_field_is_classified():
    """A new date field cannot default to plain `date` unnoticed.

    Discovery walks the module rather than a literal list of models, so a
    field on a NEW model is caught too -- a literal model list would have
    to be updated in the same edit that misses the field.
    """
    found = set(_date_fields())
    classified = KEY_DATE_FIELDS | RESPONSE_DATE_FIELDS | PLAIN_DATE_FIELDS
    assert not (found - classified), (
        f"date fields classified as neither key, response, nor plain: "
        f"{sorted(found - classified)} -- add each to one of the three sets "
        f"in this file. If it is part of a key, annotate it KeyDate."
    )


def test_the_classification_lists_name_real_fields():
    """The reverse: a renamed or deleted field left behind on a list.

    Without this, a stale entry keeps asserting a guard on something that
    no longer exists, and the suite stays green while the protection is
    gone.
    """
    found = _date_fields()
    classified = KEY_DATE_FIELDS | RESPONSE_DATE_FIELDS | PLAIN_DATE_FIELDS
    assert not (classified - set(found)), (
        f"classified but no longer a date field in api.models: "
        f"{sorted(classified - set(found))} -- renamed, retyped, or removed."
    )


def test_each_bucket_matches_how_the_field_is_actually_annotated():
    """The label and the code agree -- a bucket is a claim, not a comment."""
    found = _date_fields()
    wrong_way = sorted(f for f in KEY_DATE_FIELDS if not found[f])
    assert not wrong_way, f"classified key-family but NOT KeyDate: {wrong_way}"
    unexpected = sorted(
        f for f in RESPONSE_DATE_FIELDS | PLAIN_DATE_FIELDS if found[f]
    )
    assert not unexpected, (
        f"carries the KeyDate guard but is classified response/plain: "
        f"{unexpected} -- move it to KEY_DATE_FIELDS or drop the guard."
    )


def test_every_date_column_in_a_unique_constraint_is_guarded():
    """Derived from the schema, so it does not depend on my classification.

    This is the check that would have caught `due_date` in #116 on its
    own. The literal sets above still record a judgement per field, but a
    judgement can be wrong -- and was. A UNIQUE constraint is not a
    judgement, so this reads the constraints out of db/schema.sql and
    requires the matching REQUEST-model field to be guarded.

    Matching is by column name, which holds because the models and the
    schema use the same names. Response models are excluded: they are
    built from stored strings and never carry caller input.
    """
    sql = SCHEMA.read_text()
    date_columns = set(re.findall(r"^\s*(\w+)\s+DATE\b", sql, re.MULTILINE))
    unique_columns = set()
    for clause in re.findall(r"UNIQUE\s*\(([^)]*)\)", sql):
        unique_columns |= {c.strip() for c in clause.split(",")}

    key_columns = date_columns & unique_columns
    assert key_columns, "parsed no dated UNIQUE columns -- the regex broke"

    found = _date_fields()
    unguarded = sorted(
        label
        for label, guarded in found.items()
        if not guarded
        and label.split(".")[1] in key_columns
        and not label.split(".")[0].endswith("Response")
    )
    assert not unguarded, (
        f"these fields name a column inside a UNIQUE constraint "
        f"({sorted(key_columns)}) but accept a datetime that truncates "
        f"silently: {unguarded} -- annotate each KeyDate."
    )

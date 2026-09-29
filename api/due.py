"""`GET /due` — one query answering "what's coming up".

Three different things in this database carry a future date, and before
this they were three different questions: a renewal on an entity's
attributes, an `end_date` on a relationship, and a `reminders` row with
an RRULE. D17 unions them behind one endpoint rather than building
`/reminders/due` as a second thing a model has to remember to also ask —
a digest that answers only one of the three is worse than none, because
the two it missed look like "nothing is due".
"""

import json
import logging
import sqlite3
from datetime import date, timedelta

from dateutil.rrule import rrulestr
from fastapi import APIRouter, Depends, Query
from fastapi.exceptions import RequestValidationError

from api.auth import require_token
from api.db import MAX_LIMIT, get_connection, require_reference
from api.models import FORWARD_DATED_ATTRIBUTES

logger = logging.getLogger("api.due")

router = APIRouter(dependencies=[Depends(require_token)])

# The digest's default horizon. Long enough that a monthly cadence of
# looking sees everything once, short enough that the list stays readable.
DEFAULT_WINDOW_DAYS = 30

# Entity states where a due date is no longer anyone's problem. A sold
# car's registration expiry is not an obligation, and leaving those in
# would make the digest grow monotonically with everything ever owned —
# the list stops being read at that point, which is the real failure.
# `inactive` is deliberately NOT here: it is the reversible one, and an
# inactive account's renewal is exactly the thing worth being reminded of.
RETIRED_STATUSES = ("sold", "totaled", "deceased", "closed", "archived")

SOURCES = ("attribute", "relationship", "reminder")


def _parse_date(raw: object) -> date | None:
    """Attributes are a JSON blob, so a date field can hold anything.

    Returns None rather than raising: a malformed attribute is a data
    problem on one entity, and making it a 500 would take the whole
    digest down with it — the digest is what you would use to notice.
    """
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _attribute_items(connection: sqlite3.Connection, to_date: date) -> list[dict]:
    """Forward-dated attributes, with NO lower bound.

    D19: a past one surfaces as overdue rather than being filtered out.
    Nothing rolls these point dates forward — `registration_expiry` holds
    one next occurrence and stays there once it passes — so a
    `from <= d <= to` filter would hide a lapsed registration exactly
    when it matters. A stale attribute is loud, not absent.
    """
    placeholders = ",".join("?" * len(RETIRED_STATUSES))
    rows = connection.execute(
        f"SELECT id, type, name, attributes FROM entities "
        f"WHERE status NOT IN ({placeholders})",
        RETIRED_STATUSES,
    ).fetchall()

    items = []
    for row in rows:
        fields = FORWARD_DATED_ATTRIBUTES.get(row["type"], ())
        if not fields:
            continue
        try:
            attributes = json.loads(row["attributes"] or "{}")
        except json.JSONDecodeError:
            logger.warning(
                "skipping entity %s: attributes is not valid JSON",
                row["id"],
                extra={"entity_id": row["id"], "source": "attribute"},
            )
            continue
        if not isinstance(attributes, dict):
            logger.warning(
                "skipping entity %s: attributes is %s, not an object",
                row["id"],
                type(attributes).__name__,
                extra={"entity_id": row["id"], "source": "attribute"},
            )
            continue
        for field in fields:
            raw = attributes.get(field)
            due = _parse_date(raw)
            if due is None:
                if raw is not None:
                    # Logged, not merely skipped (D23). Swallowing this
                    # silently turns a typo into a renewal that never
                    # appears, which is the failure the endpoint exists
                    # to prevent — the digest would be the tool you used
                    # to not notice. The value is logged because a date
                    # is not sensitive and the typo is the whole point.
                    logger.warning(
                        "skipping unparseable date attribute %s on entity %s: %r",
                        field,
                        row["id"],
                        raw,
                        extra={
                            "entity_id": row["id"],
                            "field": field,
                            "source": "attribute",
                        },
                    )
                continue
            if due > to_date:
                continue
            items.append(
                {
                    "entity_id": row["id"],
                    "entity_type": row["type"],
                    "entity_name": row["name"],
                    "source": "attribute",
                    "field": field,
                    "title": field.replace("_", " "),
                    "due_date": due,
                    "related": [],
                }
            )
    return items


def _relationship_items(connection: sqlite3.Connection, to_date: date) -> list[dict]:
    """Relationship `end_date`s — a lease ending, a policy lapsing.

    Same no-lower-bound rule as attributes, and for the same reason: an
    `insures` relationship whose `end_date` passed is an uninsured asset,
    which is the loudest thing this endpoint can say. Reported against
    the `from` entity, with the counterpart in `related`, because
    "the lease on 12 Oak St ends" is the useful sentence and the tenant
    is the detail.
    """
    placeholders = ",".join("?" * len(RETIRED_STATUSES))
    rows = connection.execute(
        f"SELECT r.id AS relationship_id, r.relationship_type, r.end_date, "
        f"f.id AS from_id, f.type AS from_type, f.name AS from_name, "
        f"t.id AS to_id, t.type AS to_type, t.name AS to_name "
        f"FROM entity_relationships r "
        f"JOIN entities f ON f.id = r.from_entity_id "
        f"JOIN entities t ON t.id = r.to_entity_id "
        f"WHERE r.end_date IS NOT NULL AND r.end_date <= ? "
        f"AND f.status NOT IN ({placeholders}) "
        f"AND t.status NOT IN ({placeholders})",
        (to_date.isoformat(), *RETIRED_STATUSES, *RETIRED_STATUSES),
    ).fetchall()

    items = []
    for row in rows:
        due = _parse_date(row["end_date"])
        if due is None:
            continue
        items.append(
            {
                "entity_id": row["from_id"],
                "entity_type": row["from_type"],
                "entity_name": row["from_name"],
                "source": "relationship",
                "field": "end_date",
                "title": f"{row['relationship_type']} ends",
                "due_date": due,
                "related": [
                    {
                        "entity_id": row["to_id"],
                        "entity_type": row["to_type"],
                        "entity_name": row["to_name"],
                        "relationship_type": row["relationship_type"],
                    }
                ],
            }
        )
    return items


# A recurring reminder has unboundedly many past occurrences, so unlike
# the other two sources this one cannot ignore `from`: expanding a daily
# RRULE backwards from today would not terminate in any useful sense.
# That asymmetry is deliberate and is the reason `from` exists at all.
def _reminder_items(
    connection: sqlite3.Connection, from_date: date, to_date: date
) -> list[dict]:
    rows = connection.execute(
        "SELECT r.id, r.title, r.recurrence_rule, r.start_date, r.end_date, "
        "e.id AS entity_id, e.type AS entity_type, e.name AS entity_name "
        "FROM reminders r LEFT JOIN entities e ON e.id = r.entity_id"
    ).fetchall()

    items = []
    for row in rows:
        scheduled = set(_occurrences(row, from_date, to_date))
        moved = connection.execute(
            "SELECT due_date FROM reminder_instances WHERE reminder_id = ? "
            "AND status = 'snoozed' AND snoozed_to BETWEEN ? AND ?",
            (row["id"], from_date.isoformat(), to_date.isoformat()),
        ).fetchall()
        scheduled.update(date.fromisoformat(item["due_date"]) for item in moved)
        for due in sorted(scheduled):
            occurrence_date = due.isoformat()
            status, snoozed_to = _instance_state(connection, row["id"], due)
            if status in ("done", "skipped"):
                continue
            if status == "snoozed":
                # The schema's own semantics: a snoozed occurrence is not
                # due on its original date, it is due on snoozed_to. A
                # snooze past the window drops out rather than nagging.
                if snoozed_to is None or not from_date <= snoozed_to <= to_date:
                    continue
                due = snoozed_to
            items.append(
                {
                    "entity_id": row["entity_id"],
                    "entity_type": row["entity_type"],
                    "entity_name": row["entity_name"],
                    "source": "reminder",
                    "reminder_id": row["id"],
                    "occurrence_date": occurrence_date,
                    "field": "reminder",
                    "title": row["title"],
                    "due_date": due,
                    "related": [],
                }
            )
    return items


def _occurrences(row: sqlite3.Row, from_date: date, to_date: date) -> list[date]:
    start = _parse_date(row["start_date"])
    if start is None:
        logger.warning(
            "skipping reminder %s: unparseable start_date %r",
            row["id"],
            row["start_date"],
            extra={"reminder_id": row["id"], "source": "reminder"},
        )
        return []
    rule = row["recurrence_rule"]
    if not rule:
        return [start] if from_date <= start <= to_date else []

    # A reminder's own end_date ends the series regardless of the query
    # window — a lease reminder does not keep firing after the lease.
    series_end = _parse_date(row["end_date"])
    last = min(to_date, series_end) if series_end is not None else to_date
    if last < from_date:
        return []
    try:
        occurrences = rrulestr(rule, dtstart=_as_datetime(start)).between(
            _as_datetime(from_date), _as_datetime(last), inc=True
        )
    except (ValueError, TypeError) as exc:
        # A malformed RRULE is one bad row, not a reason to fail the
        # whole digest — but it is logged at WARNING (D23), because the
        # silent version of this is a reminder that never fires and
        # therefore never gets questioned. Until POST /reminders
        # validates the rule at write time (ticket T-12) the log is
        # the only place a typo surfaces at all.
        logger.warning(
            "skipping reminder %s: unusable recurrence_rule %r (%s)",
            row["id"],
            rule,
            exc,
            extra={
                "reminder_id": row["id"],
                "source": "reminder",
            },
        )
        return []
    return [occurrence.date() for occurrence in occurrences]


def _as_datetime(value: date):
    from datetime import datetime

    return datetime(value.year, value.month, value.day)


#: How many occurrences a refusal will name before it stops listing them.
#: A daily RRULE has unboundedly many, and an error message that tries to
#: print them all is not an error message.
NAMED_OCCURRENCE_LIMIT = 8

#: How far either side of the requested date a refusal looks for
#: occurrences to name. Only used to build the message -- never to decide
#: whether the requested date is valid, which `_occurrences(row, d, d)`
#: answers exactly.
NAMING_WINDOW_DAYS = 365


def _nameable_occurrences(row: sqlite3.Row, requested: date) -> list[date]:
    """Occurrences worth putting in a refusal, nearest the requested date.

    A non-recurring reminder has exactly one, and it is named whatever
    the requested date was -- "the only occurrence is X" is the whole
    answer there, and a window could miss it and say nothing.
    """
    if not row["recurrence_rule"]:
        start = _parse_date(row["start_date"])
        return [start] if start is not None else []
    window = timedelta(days=NAMING_WINDOW_DAYS)
    found = _occurrences(row, requested - window, requested + window)
    found.sort(key=lambda occurrence: abs(occurrence - requested))
    return sorted(found[:NAMED_OCCURRENCE_LIMIT])


def resolve_occurrence(
    connection: sqlite3.Connection, row: sqlite3.Row, requested: date
) -> date:
    """The occurrence a caller's `due_date` identifies, or a 422.

    THE DATE `GET /due` REPORTS MUST BE A DATE THE WRITERS ACCEPT, and
    before this it was not. An occurrence is identified by its SCHEDULED
    date -- that is the `reminder_instances` key -- but `_reminder_items`
    reports a snoozed occurrence at its `snoozed_to`, because that is
    when it is actually due and that is the honest thing to show. So the
    one date a caller was given back was the one date that did not
    identify anything: completing it wrote a second, unrelated instance
    and the occurrence stayed due, answered 200. `complete_reminder`'s
    own description says "pass the due_date exactly as list_due_items
    reported it", so the surface was instructing callers into it.

    Resolved here rather than by changing what `/due` reports: the
    snoozed date is the true answer to "when is this due", and moving
    the identity into the read would make the display wrong to make the
    write convenient.

    Nothing is guessed. A date that is neither a scheduled occurrence nor
    a date some occurrence was snoozed to identifies nothing at all, and
    is refused naming the ones that exist -- the same shape the entity
    and relationship enums use, because a caller who cannot see the
    permitted values cannot correct themselves.
    """
    # ONE CANDIDATE SET FROM BOTH SOURCES, then the one/many/none split.
    # An earlier version returned here the moment the date was on the
    # series and never asked whether it was ALSO some other occurrence's
    # snoozed_to. Both can be true at once: a weekly reminder snoozed
    # +7 -> +14 puts the moved occurrence on top of the series
    # occurrence at +14, /due lists BOTH rows at that date, and
    # complete(+14) silently cleared whichever the first branch reached
    # while the other stayed due -- this endpoint's own bug one
    # configuration over (review-2 on #136). Pre-existing, but the fix
    # here is what the title and the tool descriptions claim.
    candidates: list[date] = []

    # Exact, and bounded for an unbounded RRULE: `between(d, d, inc=True)`
    # asks only whether this one date is on the series.
    on_series = bool(_occurrences(row, requested, requested))

    # MEMBERSHIP IS NOT ENOUGH; it has to be DUE here. The candidate set
    # counted a scheduled occurrence on membership alone while /due
    # counts one only if its instance state leaves it outstanding, and
    # the two disagreed (review-2 on #136). Measured: weekly from +7,
    # complete(+14), snooze(+7 -> +14) -- /due shows ONE row at +14,
    # because the resident occurrence there is done and only the moved
    # one is outstanding, and this refused it as "ambiguous: identifies
    # 2 occurrences". A false refusal, where the same disagreement used
    # to be a silent 200.
    #
    # `_instance_state` is the same function /due reads, deliberately:
    # two functions deciding "is this due" is how they come to disagree,
    # which is this bug.
    if on_series:
        status, snoozed_to = _instance_state(connection, row["id"], requested)
        outstanding = status not in ("done", "skipped") and not (
            status == "snoozed" and snoozed_to != requested
        )
        if outstanding:
            candidates.append(requested)

    moved = connection.execute(
        "SELECT due_date FROM reminder_instances "
        "WHERE reminder_id = ? AND status = 'snoozed' AND snoozed_to = ?",
        (row["id"], requested.isoformat()),
    ).fetchall()
    for found in moved:
        parsed = _parse_date(found["due_date"])
        # An occurrence snoozed ONTO ITS OWN DATE is one occurrence, not
        # two; without this it would refuse itself as ambiguous.
        #
        # NO API PATH PRODUCES THAT STATE -- ReminderSnooze refuses
        # snoozed_to on or before due_date with a 422 (an earlier commit), so this
        # guards a row only a direct write can create. Kept, because the
        # table has no constraint enforcing it and a restore or a repair
        # script writes rows directly; pinned by a test that inserts one
        # with raw SQL rather than through the API, since the API cannot
        # reach it (review-2 on #136).
        if parsed is not None and parsed not in candidates:
            candidates.append(parsed)

    # SCHEDULED BUT NOT DUE HERE STILL RESOLVES TO ITSELF. Dropping a
    # `done` occurrence outright would break two things that are not
    # bugs: completing the same date twice, which that commit calls normal
    # and the UPSERT exists for, and snoozing an occurrence that was
    # already completed, which is the only way to undo a completion
    # recorded by mistake. Neither is ambiguous -- there is nothing else
    # competing for the date -- so the fallback applies only when
    # nothing else claimed it.
    if not candidates and on_series:
        candidates.append(requested)

    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        # The date identifies more than one occurrence, and /due lists a
        # row for each, so picking one would complete an occurrence the
        # caller did not name. Their SCHEDULED dates are distinct, which
        # is what the message hands back.
        # NAMES ONLY WHAT ACTUALLY WORKS. This used to list every
        # candidate "instead", including `requested` itself -- which
        # refuses identically, so the remedy sent the caller back to the
        # same 422. That is #138's shape one endpoint over: a refusal
        # whose suggested remedy does not work.
        #
        # Measured: with the moved occurrence and the resident one both
        # outstanding at the same day, completing the MOVED one's own
        # scheduled date succeeds, and the shared date then succeeds too
        # because only one occupant is left. So the moved dates are
        # addressable now and the shared date becomes addressable after.
        addressable = sorted(found for found in candidates if found != requested)
        listed = ", ".join(found.isoformat() for found in addressable)
        raise RequestValidationError(
            [
                {
                    "type": "value_error",
                    "loc": ("body", "due_date"),
                    "msg": (
                        f"{requested.isoformat()} is ambiguous: "
                        f"{len(candidates)} occurrences of this reminder are "
                        "outstanding on that day, because a snoozed one was "
                        "moved onto a date another already occupies. Name a "
                        f"moved occurrence by its own scheduled date: {listed}"
                        f". {requested.isoformat()} itself works again once "
                        "only one occurrence is left on it."
                    ),
                    "input": requested.isoformat(),
                }
            ]
        )

    nameable = _nameable_occurrences(row, requested)
    if not nameable:
        # DISTINGUISHED, because the old message asserted the wrong one.
        # It said "no occurrences at all -- start_date unparseable or
        # recurrence_rule unusable" whenever the ±1y search came back
        # empty, which is false for a perfectly good BOUNDED series
        # asked about a far date: review-2 measured it on
        # FREQ=WEEKLY with end_date=+30, and on COUNT=1. A refusal that
        # names the wrong cause sends the reader to fix a rule that is
        # not broken.
        start = _parse_date(row["start_date"])
        if start is None:
            detail = (
                "this reminder has no usable start_date, so it has no "
                "occurrences at all — GET /due logs that and skips it"
            )
        else:
            bounds = f"it starts {start.isoformat()}"
            series_end = _parse_date(row["end_date"])
            if series_end is not None:
                bounds += f" and ends {series_end.isoformat()}"
            detail = (
                "it has no occurrences within a year either side of that "
                f"date ({bounds}); ask GET /due for the window you mean"
            )
    else:
        listed = ", ".join(occurrence.isoformat() for occurrence in nameable)
        detail = (
            f"its occurrences are: {listed}"
            if len(nameable) < NAMED_OCCURRENCE_LIMIT
            else f"its nearest occurrences are: {listed}"
        )
    raise RequestValidationError(
        [
            {
                "type": "value_error",
                "loc": ("body", "due_date"),
                "msg": (
                    f"{requested.isoformat()} is not an occurrence of this "
                    f"reminder, so there is nothing there to complete or "
                    f"snooze — {detail}. A snoozed occurrence can also be "
                    "named by the date GET /due reports it at."
                ),
                "input": requested.isoformat(),
            }
        ]
    )


def _instance_state(
    connection: sqlite3.Connection, reminder_id: str, due: date
) -> tuple[str, date | None]:
    row = connection.execute(
        "SELECT status, snoozed_to FROM reminder_instances "
        "WHERE reminder_id = ? AND due_date = ?",
        (reminder_id, due.isoformat()),
    ).fetchone()
    if row is None:
        return "pending", None
    return row["status"], _parse_date(row["snoozed_to"])


#: The order this endpoint returns, stated once beside the query
#: that produces it (items.sort(key=...) on a total key led by due_date). The tool description
#: interpolates this rather than retyping it, and a behavioural
#: test asserts the rows actually come back this way -- a flipped
#: ORDER BY is invisible to every caller who believed the sentence.
DUE_ORDER = "by due_date, soonest first"


@router.get("/due")
def list_due_items(
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = Query(default=None),
    source: str | None = Query(default=None),
    entity_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    today = date.today()
    from_date = from_ if from_ is not None else today
    to_date = to if to is not None else from_date + timedelta(days=DEFAULT_WINDOW_DAYS)

    if to_date < from_date:
        raise RequestValidationError(
            [
                {
                    "type": "value_error",
                    "loc": ("query", "to"),
                    "msg": f"to ({to_date}) must not precede from ({from_date})",
                    "input": str(to_date),
                }
            ]
        )
    if source is not None and source not in SOURCES:
        raise RequestValidationError(
            [
                {
                    "type": "value_error",
                    "loc": ("query", "source"),
                    "msg": f"source must be one of {', '.join(SOURCES)}",
                    "input": source,
                }
            ]
        )

    # A filter naming something that does not exist is refused, not
    # answered with an empty page: the two are indistinguishable to
    # the caller, and only one of them is true (ticket T-08).
    require_reference(connection, "entities", entity_id, "entity")

    items: list[dict] = []
    if source in (None, "attribute"):
        items += _attribute_items(connection, to_date)
    if source in (None, "relationship"):
        items += _relationship_items(connection, to_date)
    if source in (None, "reminder"):
        items += _reminder_items(connection, from_date if from_ is not None else today - timedelta(days=7), to_date)

    if entity_id is not None:
        items = [item for item in items if item["entity_id"] == entity_id]

    # Sorted on a total key, not just the date. Three sources merged in a
    # fixed call order would otherwise make same-day ordering depend on
    # which source produced them, and page 2 of a paginated read could
    # repeat or skip a row if the underlying order were unstable. Same
    # lesson as the merchant-rule tie-break (D15): if a caller can see the
    # order, it needs to be decided rather than inherited.
    items.sort(
        key=lambda item: (
            item["due_date"],
            item["source"],
            item["entity_name"] or "",
            item["field"],
            item["entity_id"] or "",
            item.get("reminder_id", ""),
            item.get("occurrence_date", ""),
        )
    )

    for item in items:
        item["due_date"] = item["due_date"].isoformat()
        # Relative to TODAY, not to `from`: "days left" means days from
        # now, and a caller asking about next month still wants to know
        # that something in it is 40 days away rather than 0.
        item["days_left"] = (date.fromisoformat(item["due_date"]) - today).days

    return {"items": items[offset : offset + limit], "total": len(items)}

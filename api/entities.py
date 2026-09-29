import json
import sqlite3
from datetime import date
from uuid import uuid4

from fastapi import APIRouter, Depends, Query
from fastapi.exceptions import RequestValidationError

from api.auth import require_token
from api.birthdays import maintain_birthday
from api.google_calendar import serialized_reminder_write, sync_reminder_safely
from api.db import MAX_LIMIT, get_connection, paginate
from api.errors import ConflictError, InvalidReferenceError, NotFoundError
from api.models import (
    resolve_patch,
    cleared,
    DATED_RELATIONSHIP_TYPES,
    EntityCreate,
    EntityResponse,
    EntityUpdate,
    RelationshipCreate,
    RelationshipUpdate,
    RelationshipResponse,
    SYMMETRIC_RELATIONSHIP_TYPES,
    validate_entity_attributes,
)

# require_token is applied at the router level, not as a named parameter
# dependency, so an unauthenticated request is rejected before
# get_connection opens a DB connection. See api/auth.py's docstring.
router = APIRouter(dependencies=[Depends(require_token)])


def _row_to_entity(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["attributes"] = json.loads(data["attributes"] or "{}")
    return data


def _get_entity_row(connection: sqlite3.Connection, entity_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM entities WHERE id = ?", (entity_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"No entity with id {entity_id!r}")
    return row


def _check_repository_assignment(
    connection: sqlite3.Connection, entity_type: str, attributes: dict,
    entity_id: str,
) -> None:
    """Called under the writer lock, so checking and assigning are atomic."""
    repository_path = attributes.get("repository_path")
    if entity_type != "project" or repository_path is None:
        return
    existing = connection.execute(
        "SELECT id FROM entities WHERE type = 'project' AND id != ? "
        "AND json_extract(attributes, '$.repository_path') = ?",
        (entity_id, repository_path),
    ).fetchone()
    if existing is not None:
        raise ConflictError(
            f"Repository path is already assigned to project {existing['id']!r}; "
            "clear that assignment before reassigning it."
        )


@router.post("/entities", response_model=EntityResponse)
@serialized_reminder_write
def create_entity(
    body: EntityCreate,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    attributes = validate_entity_attributes(body.type, body.attributes)
    entity_id = str(uuid4())
    # Serialize the check and write even across separate server processes.
    connection.execute("BEGIN IMMEDIATE")
    _check_repository_assignment(connection, body.type, attributes, entity_id)
    connection.execute(
        "INSERT INTO entities (id, type, name, status, attributes) "
        "VALUES (?, ?, ?, ?, ?)",
        (entity_id, body.type, body.name, body.status, json.dumps(attributes)),
    )
    reminder_id = maintain_birthday(connection, entity_id)
    connection.commit()
    if reminder_id:
        sync_reminder_safely(connection, reminder_id)
    return _row_to_entity(_get_entity_row(connection, entity_id))


@router.get("/entities/{entity_id}", response_model=EntityResponse)
def get_entity(
    entity_id: str,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    return _row_to_entity(_get_entity_row(connection, entity_id))


#: The order this endpoint returns, stated once beside the query
#: that produces it (ORDER BY name COLLATE NOCASE, id). The tool description
#: interpolates this rather than retyping it, and a behavioural
#: test asserts the rows actually come back this way -- a flipped
#: ORDER BY is invisible to every caller who believed the sentence.
LIST_ENTITIES_ORDER = "by name, case-insensitive"


@router.get("/entities")
def list_entities(
    entity_type: str | None = Query(default=None, alias="type"),
    status: str | None = Query(default=None),
    # ge=1: paginate() raises ValueError on a non-positive limit, so it has
    # to be rejected here as a 422 rather than surfacing as a 500.
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    query = "SELECT * FROM entities WHERE 1=1"
    params: list = []
    if entity_type is not None:
        query += " AND type = ?"
        params.append(entity_type)
    if status is not None:
        query += " AND status = ?"
        params.append(status)
    # COLLATE NOCASE: SQLite's default binary collation sorts uppercase
    # before lowercase ("Zebra" < "apple"), which reads as broken in a
    # personal inventory listing. The `, id` tiebreak is load-bearing for
    # pagination: NOCASE makes "Apple" and "apple" compare equal, and
    # SQLite guarantees no order among equal keys, so without it an index
    # on name could reorder tied rows between two paginated requests and
    # repeat or skip one.
    query += " ORDER BY name COLLATE NOCASE, id"

    result = paginate(connection, query, tuple(params), limit=limit, offset=offset)
    result["items"] = [
        {**item, "attributes": json.loads(item["attributes"] or "{}")}
        for item in result["items"]
    ]
    return result


def _differing_fields(existing: sqlite3.Row, body: "RelationshipCreate") -> list[str]:
    """Which payload fields the caller sent differently from the stored row.

    Compared as VALUES, not as text: `attributes` round-trips through
    JSON, so `{}` stored and `{}` sent are equal even though their
    serialisations need not be, and a date is compared as an ISO string
    against the ISO string that was stored.

    `relationship_type` and direction are excluded on purpose — the
    first is part of the match, and the second is reversed by design for
    symmetric types.
    """
    stored_attributes = json.loads(existing["attributes"] or "{}")
    candidates = {
        "start_date": (
            existing["start_date"],
            body.start_date.isoformat() if body.start_date else None,
        ),
        "end_date": (
            existing["end_date"],
            body.end_date.isoformat() if body.end_date else None,
        ),
        "attributes": (stored_attributes, body.attributes),
    }
    return [name for name, (was, now) in candidates.items() if was != now]


def _row_to_relationship(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["attributes"] = json.loads(data["attributes"] or "{}")
    return data


def _get_relationship_row(
    connection: sqlite3.Connection, relationship_id: str
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM entity_relationships WHERE id = ?", (relationship_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"No relationship with id {relationship_id!r}")
    return row


#: Stated once, beside the code that enforces it (the `existing is not None` branch returns before any INSERT).
#: Interpolated into the tool description; exercised by a test in
#: api/tests/test_claim_contracts.py.
#: Reworded by D71 and deliberately narrowed. It used to read "a
#: duplicate returns the existing relationship and creates nothing",
#: which stopped being true when a DIFFERING payload became a refusal
#: rather than a silent discard — the old phrase would have kept
#: passing its pin while describing behaviour the code no longer has,
#: which is the failure #115's pinning exists to prevent.
RELATIONSHIP_DEDUP_CONTRACT = (
    "an identical duplicate returns the existing relationship and creates "
    "nothing, and one differing in dates or attributes is refused"
)


@router.post(
    "/entities/{entity_id}/relationships", response_model=RelationshipResponse
)
def create_relationship(
    entity_id: str,
    body: RelationshipCreate,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    _get_entity_row(connection, entity_id)

    to_entity = connection.execute(
        "SELECT id FROM entities WHERE id = ?", (body.to_entity_id,)
    ).fetchone()
    if to_entity is None:
        raise InvalidReferenceError(f"No entity with id {body.to_entity_id!r}")

    # No relationship type is legitimately self-referential (D71). All
    # eight relate two distinct things; "A owns A" is not a weaker claim
    # than "A owns B", it is a malformed one, and `parent_of` pointing at
    # itself is a 1-cycle in a graph anything downstream may walk.
    #
    # 422 rather than InvalidReferenceError's 400, deliberately: D17 made
    # 400 mean "refers to something that does not exist", and this refers
    # to something that plainly does. It is a value outside its permitted
    # range, which is the shape validate_ticket_status and
    # validate_entity_attributes already use. Two meanings for one status
    # would leave the next reader consulting the source.
    if body.to_entity_id == entity_id:
        raise RequestValidationError(
            [
                {
                    "type": "value_error",
                    "loc": ("body", "to_entity_id"),
                    "msg": (
                        "an entity cannot have a relationship with itself; "
                        f"to_entity_id must differ from {entity_id!r}"
                    ),
                    "input": body.to_entity_id,
                }
            ]
        )

    # Symmetric is checked before dated so that if a type is ever both, the
    # store-once rule wins over the accumulate rule; they are disjoint today.
    existing = None
    if body.relationship_type in SYMMETRIC_RELATIONSHIP_TYPES:
        existing = connection.execute(
            "SELECT * FROM entity_relationships WHERE relationship_type = ? AND "
            "((from_entity_id = ? AND to_entity_id = ?) "
            "OR (from_entity_id = ? AND to_entity_id = ?))",
            (
                body.relationship_type,
                entity_id,
                body.to_entity_id,
                body.to_entity_id,
                entity_id,
            ),
        ).fetchone()
    elif body.relationship_type not in DATED_RELATIONSHIP_TYPES:
        existing = connection.execute(
            "SELECT * FROM entity_relationships WHERE from_entity_id = ? "
            "AND to_entity_id = ? AND relationship_type = ?",
            (entity_id, body.to_entity_id, body.relationship_type),
        ).fetchone()
    if existing is not None:
        # Idempotent on an identical re-POST, 409-shaped on a differing
        # one (D71). A skill step re-run or a network retry sends the
        # same body and must still succeed; a caller sending DIFFERENT
        # dates or attributes has made a change that silently did not
        # happen, and used to get a 200 carrying the OLD values back.
        #
        # Direction is deliberately not compared. A symmetric type is
        # stored once in whichever direction it was first recorded, so
        # `A spouse_of B` legitimately matches a stored `B spouse_of A`
        # — comparing direction would refuse the correct case.
        #
        # There is no PATCH /relationships/{id} yet, so this refusal
        # currently points at a door that does not exist. That is still
        # better than reporting success for a write that did not happen:
        # a 409 names the row and the fields, which is enough to act on.
        differing = _differing_fields(existing, body)
        if differing:
            raise ConflictError(
                f"Relationship {existing['id']!r} already exists with "
                f"different {', '.join(differing)}. Creating it again does "
                f"not update it, so this change was refused rather than "
                f"silently discarded; there is no endpoint to amend a "
                f"relationship yet."
            )
        return _row_to_relationship(existing)

    relationship_id = str(uuid4())
    connection.execute(
        "INSERT INTO entity_relationships "
        "(id, from_entity_id, to_entity_id, relationship_type, start_date, end_date, attributes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            relationship_id,
            entity_id,
            body.to_entity_id,
            body.relationship_type,
            body.start_date.isoformat() if body.start_date else None,
            body.end_date.isoformat() if body.end_date else None,
            json.dumps(body.attributes),
        ),
    )
    connection.commit()
    return _row_to_relationship(_get_relationship_row(connection, relationship_id))


@router.get("/entities/{entity_id}/relationships")
def list_relationships(
    entity_id: str,
    # ge=1 mirrors list_entities: paginate() raises ValueError on a
    # non-positive limit, so it has to be rejected here as a 422 rather
    # than surfacing as an unhandled 500.
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    _get_entity_row(connection, entity_id)

    # ORDER BY id (the uuid primary key) is already unique and total, so
    # unlike list_entities' name ordering it needs no COLLATE NOCASE or
    # tiebreak column to keep pagination stable across requests.
    result = paginate(
        connection,
        "SELECT * FROM entity_relationships "
        "WHERE from_entity_id = ? OR to_entity_id = ? ORDER BY id",
        (entity_id, entity_id),
        limit=limit,
        offset=offset,
    )
    result["items"] = [
        {**item, "attributes": json.loads(item["attributes"] or "{}")}
        for item in result["items"]
    ]
    return result


#: Stated once so the tool description and the docstring cannot drift
#: apart (the contract-pin style from #115).
#: Read by the MCP tool description as well as this module, so it states
#: only what is true at BOTH layers. It used to end "and an explicit null
#: clears a field while omitting it leaves it alone" -- true of this API,
#: and false of the tool, where the SDK cannot tell a null argument from
#: an omitted one (#126). Interpolated into update_relationship it
#: contradicted the clear note two paragraphs below it. The null half now
#: lives where it is true: this route's own handling, and D93.
RELATIONSHIP_UPDATE_CONTRACT = (
    "dates and attributes can be amended; the pair and the type cannot, "
    "and omitting a field leaves it alone"
)


@router.get("/relationships/{relationship_id}", response_model=RelationshipResponse)
def get_relationship(
    relationship_id: str,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    """One relationship by id.

    Added with PATCH rather than after it: without this the PATCH
    response would be the only way to read a single relationship by id,
    so checking what a row holds would mean writing to it.
    """
    return _row_to_relationship(_get_relationship_row(connection, relationship_id))


@router.patch(
    "/relationships/{relationship_id}", response_model=RelationshipResponse
)
def update_relationship(
    relationship_id: str,
    body: RelationshipUpdate,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    """Amend a relationship's dates or attributes.

    THE PAIR AND THE TYPE ARE NOT AMENDABLE, and the reason is
    mechanical rather than stylistic: `(from_entity_id, to_entity_id,
    relationship_type)` is the key `create_relationship` dedups on.
    Changing one would not amend this relationship, it would make it a
    different one — and the existing row would still be there.

    A consequence worth stating, because it is the question a reviewer
    asks next: **this endpoint cannot create a duplicate that
    `create_relationship` would have refused.** Dates and attributes are
    not part of that key, so no PATCH can make two rows collide.

    THE SYMMETRIC REVERSAL DOES NOT APPLY HERE, by construction rather
    than by care. `create_relationship` searches both directions because
    it is addressed by the PAIR; this is addressed by the ID, so there
    is exactly one row and no direction to resolve. Patching it changes
    what both sides see, which is correct — `list_relationships` already
    reads the single stored row from either end. Do not add a
    direction-aware branch here; it could never fire.

    An explicit `null` CLEARS a field; omitting the key leaves it alone.
    See `RelationshipUpdate.cleared` for why those must differ.
    """
    row = _get_relationship_row(connection, relationship_id)

    def resolved(field: str, current):
        if body.cleared(field):
            return None
        supplied = getattr(body, field)
        return current if supplied is None else supplied

    start_date = resolved("start_date", row["start_date"])
    end_date = resolved("end_date", row["end_date"])
    if body.cleared("attributes"):
        attributes = {}
    elif body.attributes is not None:
        # Replaces wholesale rather than merging. A merge would make it
        # impossible to REMOVE a key, which is the same trap one level
        # down from the one `cleared()` exists to avoid.
        attributes = body.attributes
    else:
        attributes = json.loads(row["attributes"] or "{}")

    connection.execute(
        "UPDATE entity_relationships SET start_date = ?, end_date = ?, "
        "attributes = ? WHERE id = ?",
        (
            start_date.isoformat() if isinstance(start_date, date) else start_date,
            end_date.isoformat() if isinstance(end_date, date) else end_date,
            json.dumps(attributes),
            relationship_id,
        ),
    )
    connection.commit()
    return _row_to_relationship(_get_relationship_row(connection, relationship_id))


@router.delete("/relationships/{relationship_id}")
def delete_relationship(
    relationship_id: str,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    _get_relationship_row(connection, relationship_id)
    connection.execute(
        "DELETE FROM entity_relationships WHERE id = ?", (relationship_id,)
    )
    connection.commit()
    return {"deleted": True}


@router.patch("/entities/{entity_id}", response_model=EntityResponse)
@serialized_reminder_write
def update_entity(
    entity_id: str,
    body: EntityUpdate,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    # Lock before reading: a concurrent PATCH must not restore stale attributes
    # and accidentally reassign a repository another request has just released.
    connection.execute("BEGIN IMMEDIATE")
    row = _get_entity_row(connection, entity_id)

    name = resolve_patch(body, "name", row["name"])
    status = resolve_patch(body, "status", row["status"])
    # attributes is the one clearable field here, and clearing it writes a
    # real SQL NULL rather than the string "null" -- json.dumps(None) would
    # store the latter, which reads back as the JSON value null and is not
    # the same as an absent blob.
    if cleared(body, "attributes"):
        attributes_column = None
    elif body.attributes is not None:
        attributes_column = json.dumps(
            validate_entity_attributes(row["type"], body.attributes)
        )
    else:
        attributes_column = row["attributes"]

    _check_repository_assignment(
        connection, row["type"], json.loads(attributes_column or "{}"), entity_id,
    )

    # updated_at uses SQLite's CURRENT_TIMESTAMP rather than a Python-side
    # timestamp so it matches created_at's format exactly ("YYYY-MM-DD
    # HH:MM:SS", UTC). A Python datetime.now(UTC).isoformat() here would put
    # two different timestamp formats in the same response body, and
    # EntityResponse types both fields as a bare str, so nothing would catch it.
    connection.execute(
        "UPDATE entities SET name = ?, status = ?, attributes = ?, "
        "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (name, status, attributes_column, entity_id),
    )
    reminder_id = maintain_birthday(connection, entity_id)
    connection.commit()
    if reminder_id:
        sync_reminder_safely(connection, reminder_id)
    return _row_to_entity(_get_entity_row(connection, entity_id))


#: Every place an entity id can be referenced, as (table, column).
#:
#: This list exists for the REFUSAL MESSAGE, not for the refusal itself:
#: the nine FK-backed entries are enforced by SQLite regardless of what is
#: written here, and the `sqlite3.IntegrityError` catch below remains as
#: the backstop. What the list adds is the ability to say WHICH reference
#: blocks a delete, which the bare IntegrityError cannot.
#:
#: `note_links` is the exception that motivated all of this. Its
#: `target_id` is polymorphic and carries no FK -- db/schema.sql:55-57
#: explicitly assigns its referential integrity to "the API layer, not the
#: DB" -- so SQLite will never refuse on its behalf and this layer has to.
#:
#: Kept honest by test_entity_reference_registry.py, which derives the
#: FK-backed set from the database itself and fails if a table reaches
#: entities without appearing here.
ENTITY_REFERENCES: tuple[tuple[str, str], ...] = (
    ("entity_relationships", "from_entity_id"),
    ("entity_relationships", "to_entity_id"),
    ("reminders", "entity_id"),
    ("statements", "account_id"),
    ("transactions", "account_id"),
    ("source_import_rows", "account_id"),
    ("transactions", "entity_id"),
    ("merchant_rules", "entity_id"),
    ("documents", "entity_id"),
    ("boards", "entity_id"),
    ("note_links", "target_id"),
    ("artifact_links", "target_id"),
)

#: Tables whose reference is polymorphic: matching `target_id` alone would
#: count a note attached to a transaction that happens to share the id.
_POLYMORPHIC = {"note_links": ("target_type", "entity"), "artifact_links": ("target_type", "entity")}


def _reference_counts(
    connection: sqlite3.Connection, entity_id: str
) -> dict[str, int]:
    """Non-zero reference counts, keyed "table.column"."""
    counts: dict[str, int] = {}
    for table, column in ENTITY_REFERENCES:
        sql = f"SELECT count(*) FROM {table} WHERE {column} = ?"
        params: tuple = (entity_id,)
        if table in _POLYMORPHIC:
            type_column, type_value = _POLYMORPHIC[table]
            sql += f" AND {type_column} = ?"
            params += (type_value,)
        found = connection.execute(sql, params).fetchone()[0]
        if found:
            counts[f"{table}.{column}"] = found
    return counts


def _blocking_note_ids(connection: sqlite3.Connection, entity_id: str) -> list[str]:
    return [
        row[0]
        for row in connection.execute(
            "SELECT note_id FROM note_links "
            "WHERE target_type = 'entity' AND target_id = ? ORDER BY note_id",
            (entity_id,),
        )
    ]


#: Said in the refusal and in the MCP tool description, because the case
#: where this refusal is annoying is exactly the case where the caller
#: wanted to archive instead.
ARCHIVE_HINT = (
    "Hard delete is for mistakes, and an entity created by mistake has "
    "nothing attached to it. To stop tracking something you really do own, "
    "set status to 'archived' (or 'sold', 'closed', 'deceased') -- that "
    "keeps the row and every reference to it valid."
)


@router.delete("/entities/{entity_id}")
def delete_entity(
    entity_id: str,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    _get_entity_row(connection, entity_id)

    # Checked BEFORE the DELETE rather than caught after it. The FK-backed
    # references would raise either way, but note_links would not -- it has
    # no FK, so the delete would succeed and leave a link pointing at an id
    # that no longer resolves. Counting first is also what lets the refusal
    # name the blockers instead of saying "other records".
    counts = _reference_counts(connection, entity_id)
    if counts:
        where = ", ".join(f"{name} ({n})" for name, n in sorted(counts.items()))
        message = (
            f"Cannot delete entity {entity_id!r}: still referenced by "
            f"{where}."
        )
        note_ids = _blocking_note_ids(connection, entity_id)
        if note_ids:
            message += (
                f" Notes linked to it: {', '.join(repr(n) for n in note_ids)} "
                f"-- unlink them first; deleting the entity would leave those "
                f"links pointing at nothing."
            )
        linked_artifacts = counts.get("artifact_links.target_id")
        if linked_artifacts:
            message += (
                f" Artifacts linked to it: artifact_links ({linked_artifacts}) "
                f"-- unlink them first (unlink_artifact); the artifacts themselves stay."
            )
        raise ConflictError(f"{message} {ARCHIVE_HINT}")

    try:
        connection.execute("DELETE FROM entities WHERE id = ?", (entity_id,))
        connection.commit()
    except sqlite3.IntegrityError as exc:
        # Backstop, not the main guard. Reachable only if a table gains an FK
        # to entities without being added to ENTITY_REFERENCES -- which
        # test_entity_reference_registry.py is there to prevent. Kept because
        # a wrong refusal is recoverable and a wrong deletion is not.
        connection.rollback()
        raise ConflictError(
            f"Cannot delete entity {entity_id!r}: still referenced by a "
            f"record this check does not yet enumerate. {ARCHIVE_HINT}"
        ) from exc
    return {"deleted": True}

import hashlib
import re
import sqlite3
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile

from api.artifact_links import ticket_artifacts
from api.auth import require_token
from api.config import Settings, get_settings
from api.db import require_reference, MAX_LIMIT, get_connection, paginate
from api.documents import (
    AttachmentNotRedactedError,
    MAX_ATTACHMENT_BYTES,
    AttachmentTooLargeError,
    refuse_unredacted_attachment,
)
from api.errors import (
    ConflictError,
    DataIntegrityError,
    InvalidReferenceError,
    NotFoundError,
)
from api.models import (
    KEY_PREFIX_PATTERN,
    AttachmentResponse,
    BoardCreate,
    ClaimRequest,
    BoardResponse,
    BoardUpdate,
    TicketCreate,
    TicketDetailResponse,
    TicketResponse,
    TicketUpdate,
    TransitionCreate,
    validate_parent_rank,
    validate_ticket_status,
)
from api.privacy import refuse_sensitive_numbers

# require_token at the router level, not as a named parameter dependency, so
# an unauthenticated request is rejected before get_connection opens a DB
# connection. Same reasoning as api/entities.py — see api/auth.py's docstring.
router = APIRouter(dependencies=[Depends(require_token)])

# Column order for the board view. Fixed, and every column is always
# present even when empty, so a client can render a stable board.
ALL_STATUS_ORDER = [
    "backlog",
    "in_progress",
    "agent_ready",
    "agent_coding",
    "review",
    "blocked",
    "done",
]


#: Most tickets embedded per board column. The board is the operating-mode
#: entry point and must stay one call, so it cannot grow without bound —
#: but a cap that fires on an ordinary board would be a silent data cut
#: dressed as a safety feature. 100 is far above a human-sized column;
#: `total` is always present so a capped column is visible rather than a
#: quiet lie. Tests parametrize it down to 2, because an untested cap is
#: the same as no cap and reads as protection.
BOARD_COLUMN_CAP = 100

#: Same reasoning for the attachments embedded in get_ticket. They are
#: small metadata rows, so this is about unbounded growth rather than
#: payload size today.
TICKET_ATTACHMENT_CAP = 100

#: What the board embeds per ticket. Deliberately not the whole row:
#: measured on a working board, descriptions were most of the payload, and
#: it had grown by more than half in a day as tickets were added.
#: `description` is reachable in one more call via get_ticket — the change
#: is where it lives, not whether it exists. claimed_by is included
#: because "who is working on what" is the most common skim and leaving
#: it out would force that second call for the commonest read.
BOARD_SUMMARY_FIELDS = (
    "id",
    "key",
    "type",
    "title",
    "status",
    "position",
    "parent_id",
    "claimed_by",
)


# --- Ticket keys (ticket T-19) ---------------------------------------
#
# A ticket's key, e.g. KA-12, is its board's key_prefix, a hyphen and its
# per-board number. It is an alias: the UUID stays the primary id and every
# stored reference (parent_id, history, attachments, note links) holds the
# UUID. A key is resolved to that UUID at the edge, once per request, and
# composed on read from the two columns rather than stored a second time.
#
# Numbers come from boards.next_ticket_number, which create_ticket bumps in
# the same BEGIN IMMEDIATE transaction as the INSERT, so concurrent creates
# serialize on the write lock and cannot share a number (the unique index
# on (board_id, number) is the backstop). The counter only goes up: a
# deleted ticket leaves a gap and its number is never handed out again, so
# a key seen once never comes to mean a different ticket. A create that
# fails rolls the counter back with it, so a failed create costs nothing.
#
# The prefix half of that guarantee: a prefix changes only while the board
# has never had a ticket (next_ticket_number = 1), and a prefix that ever
# issued a key is retired when its board is deleted -- recorded in
# retired_key_prefixes and never issued again, explicitly or by
# derivation. Without that, a deleted board's CAN would be derived again
# for the next Canvas board and its CAN-1 would name a different ticket.
# A prefix that never issued a key protects nothing and is simply freed,
# so a typo'd prefix on a new board can be corrected and reused.
# Tickets cannot move between boards (update_ticket has no board field),
# which is what makes a key permanent; adding a move must keep the old key
# resolvable.

#: A ticket key as a caller may type it: any case, a hyphen, the number.
#: At most nine digits, so a typed key can never overflow SQLite's integer
#: binding; anything longer is not a key and is looked up as a UUID, which
#: misses cleanly. A UUID has four hyphens, so it never matches.
TICKET_KEY_PATTERN = re.compile(r"^([A-Za-z][A-Za-z0-9]{1,5})-([0-9]{1,9})$")

#: Stated once, beside resolve_ticket_id, and interpolated into every tool
#: description that takes a ticket id.
TICKET_REF_CONTRACT = (
    "a ticket is named by its UUID or by its key such as KA-12, and a key "
    "matches in any case (ka-12 works)"
)

#: The ticket columns plus the composed key. Every ticket read selects
#: through this, so no read can forget the key.
TICKET_SELECT = (
    "SELECT tickets.*, boards.key_prefix || '-' || tickets.number AS key "
    "FROM tickets JOIN boards ON boards.id = tickets.board_id"
)


def resolve_ticket_id(connection: sqlite3.Connection, ref: str) -> str | None:
    """The UUID a ticket reference names, or None when it names nothing.

    A key-shaped reference is looked up by (prefix, number), upper-casing
    the prefix because prefixes are stored upper-case; anything else is
    taken as a UUID. The two shapes cannot overlap, so there is no order
    of preference to get wrong.
    """
    match = TICKET_KEY_PATTERN.match(ref)
    if match is not None:
        row = connection.execute(
            "SELECT tickets.id FROM tickets JOIN boards "
            "ON boards.id = tickets.board_id "
            "WHERE boards.key_prefix = ? AND tickets.number = ?",
            (match.group(1).upper(), int(match.group(2))),
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT id FROM tickets WHERE id = ?", (ref,)
        ).fetchone()
    return None if row is None else row[0]


def _describe_ref(ref: str) -> str:
    kind = "key" if TICKET_KEY_PATTERN.match(ref) else "id"
    return f"No ticket with {kind} {ref!r}"


def require_ticket_reference(
    connection: sqlite3.Connection, ref: str | None
) -> str | None:
    """A ticket reference inside a request body or filter, as its UUID.

    400 invalid_reference when it names nothing -- the id-filter convention
    (ticket T-08): the URL is fine, a value inside the request is not.
    """
    if ref is None:
        return None
    ticket_id = resolve_ticket_id(connection, ref)
    if ticket_id is None:
        raise InvalidReferenceError(_describe_ref(ref))
    return ticket_id


def _key_prefix_stem(name: str) -> str:
    """The prefix a name derives to before any uniqueness numbering.

    Mirrors the SQL in db/migrations/0014_ticket_keys.sql, which cannot
    call Python (migrations stay SQL-only). Only ASCII letters and digits
    count, upper-cased the way SQLite's upper() does, so a non-ASCII
    character separates words exactly as it does there.
    """
    words: list[str] = []
    current = ""
    for char in name:
        if char.isascii() and char.isalnum():
            current += char.upper()
        elif current:
            words.append(current)
            current = ""
    if current:
        words.append(current)
    if len(words) >= 2:
        stem = "".join(word[0] for word in words)[:6]
    else:
        stem = words[0][:3] if words else ""
    if stem == "" or stem[0].isdigit():
        stem = ("B" + stem)[:6]
    if len(stem) < 2:
        stem += "B"
    return stem


def derive_key_prefix(name: str, taken: set[str]) -> str:
    """A unique prefix from an entity name: initials of two or more words
    ("Toyota Corolla" -> TC), otherwise the first three characters ("Canvas"
    -> CAN), numbered when taken (CAN2, CAN3, ...)."""
    stem = _key_prefix_stem(name)
    if stem not in taken:
        return stem
    occurrence = 2
    while True:
        suffix = str(occurrence)
        candidate = stem[: 6 - len(suffix)] + suffix
        if candidate not in taken:
            return candidate
        occurrence += 1


def _taken_prefixes(
    connection: sqlite3.Connection, excluding_board: str | None = None
) -> set[str]:
    """Prefixes no board may take: those in use by another board, and every
    retired one (a prefix that issued keys on a board since deleted)."""
    return {
        row[0]
        for row in connection.execute(
            "SELECT key_prefix FROM boards WHERE id IS NOT ? "
            "UNION SELECT prefix FROM retired_key_prefixes",
            (excluding_board,),
        ).fetchall()
    }


def _refuse_taken_prefix(
    connection: sqlite3.Connection, prefix: str, board_id: str | None = None
) -> None:
    if prefix in _taken_prefixes(connection, board_id):
        raise ConflictError(
            f"Key prefix {prefix!r} is in use by another board or retired; "
            "a retired prefix is never reissued"
        )


def _release_prefix(connection: sqlite3.Connection, board: sqlite3.Row) -> None:
    """A board is letting go of its prefix. Retire it if it ever issued a
    key (next_ticket_number > 1); otherwise it named nothing and is simply
    free again. Never commits."""
    if board["next_ticket_number"] > 1:
        connection.execute(
            "INSERT OR IGNORE INTO retired_key_prefixes (prefix, board_id) "
            "VALUES (?, ?)",
            (board["key_prefix"], board["id"]),
        )


def _get_board_row(connection: sqlite3.Connection, board_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM boards WHERE id = ?", (board_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"No board with id {board_id!r}")
    return row


def _require_entity(connection: sqlite3.Connection, entity_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM entities WHERE id = ?", (entity_id,)
    ).fetchone()
    if row is None:
        raise InvalidReferenceError(f"No entity with id {entity_id!r}")
    return row


@router.post("/boards", response_model=BoardResponse)
def create_board(
    body: BoardCreate,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    # The write lock first, so the uniqueness check and the INSERT see the
    # same set of prefixes; the unique index is the backstop.
    connection.execute("BEGIN IMMEDIATE")
    entity = _require_entity(connection, body.entity_id)
    if body.key_prefix is not None:
        _refuse_taken_prefix(connection, body.key_prefix)
        key_prefix = body.key_prefix
    else:
        key_prefix = derive_key_prefix(entity["name"], _taken_prefixes(connection))
    board_id = str(uuid4())
    connection.execute(
        "INSERT INTO boards (id, entity_id, title, key_prefix) VALUES (?, ?, ?, ?)",
        (board_id, body.entity_id, body.title, key_prefix),
    )
    connection.commit()
    return dict(_get_board_row(connection, board_id))


@router.get("/boards")
def list_boards(
    entity_id: str | None = Query(default=None),
    # ge=1: paginate() raises ValueError on a non-positive limit, so it has to
    # be rejected here as a 422 rather than surfacing as a 500. Same guard as
    # list_entities and list_relationships.
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    # A filter naming something that does not exist is refused, not
    # answered with an empty page: the two are indistinguishable to
    # the caller, and only one of them is true (ticket T-08).
    require_reference(connection, "entities", entity_id, "entity")
    query = "SELECT * FROM boards WHERE 1=1"
    params: list = []
    if entity_id is not None:
        query += " AND entity_id = ?"
        params.append(entity_id)
    # created_at alone is not unique — boards created in the same second tie,
    # and SQLite guarantees no order among equal keys, so pagination could
    # repeat or skip one. id breaks the tie. Same reasoning as list_entities.
    query += " ORDER BY created_at, id"
    return paginate(connection, query, tuple(params), limit, offset)


@router.get("/boards/{board_id}")
def get_board(
    board_id: str,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    board = _get_board_row(connection, board_id)
    entity = connection.execute(
        "SELECT id, type, name, status FROM entities WHERE id = ?",
        (board["entity_id"],),
    ).fetchone()
    rows = connection.execute(
        f"{TICKET_SELECT} WHERE tickets.board_id = ? "
        "ORDER BY tickets.position IS NULL, tickets.position, "
        "tickets.created_at, tickets.id",
        (board_id,),
    ).fetchall()

    columns: dict[str, dict] = {
        status: {"items": [], "total": 0} for status in ALL_STATUS_ORDER
    }
    for row in rows:
        # A status outside ALL_STATUS_ORDER would be a bare KeyError here,
        # which escapes as a plain-text traceback rather than the error
        # envelope. Unreachable through the API — TicketStatus and this list
        # are pinned equal by test_board_columns_match_the_status_vocabulary —
        # so reaching it means the row was written around the API.
        if row["status"] not in columns:
            raise DataIntegrityError(
                f"Ticket {row['id']!r} has status {row['status']!r}, which is "
                "not a board column; the board cannot be rendered"
            )
        column = columns[row["status"]]
        column["total"] += 1
        if len(column["items"]) < BOARD_COLUMN_CAP:
            column["items"].append(
                {field: row[field] for field in BOARD_SUMMARY_FIELDS}
            )

    return {
        "id": board["id"],
        "title": board["title"],
        "key_prefix": board["key_prefix"],
        "created_at": board["created_at"],
        "entity": dict(entity),
        "columns": columns,
    }


#: Stated once, beside the next_ticket_number check in update_board that enforces it,
#: and interpolated into the update_board tool description.
KEY_PREFIX_CHANGE_CONTRACT = (
    "a key prefix can change only while the board has never had a ticket, "
    "and a prefix that ever issued a key is never reissued"
)


@router.patch("/boards/{board_id}", response_model=BoardResponse)
def update_board(
    board_id: str,
    body: BoardUpdate,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    connection.execute("BEGIN IMMEDIATE")
    board = _get_board_row(connection, board_id)
    if body.key_prefix is not None and body.key_prefix != board["key_prefix"]:
        # Only while the board has NEVER had a ticket -- the counter, not a
        # count of current tickets, because a deleted ticket's key was
        # still seen and written down, and renaming would free its prefix
        # for reuse. A prefix is half of every key on the board.
        if board["next_ticket_number"] != 1:
            raise ConflictError(
                f"Board {board_id!r} has had tickets; {KEY_PREFIX_CHANGE_CONTRACT}"
            )
        _refuse_taken_prefix(connection, body.key_prefix, board_id)
        _release_prefix(connection, board)
        connection.execute(
            "UPDATE boards SET key_prefix = ? WHERE id = ?",
            (body.key_prefix, board_id),
        )
    if body.title is not None:
        connection.execute(
            "UPDATE boards SET title = ? WHERE id = ?", (body.title, board_id)
        )
    connection.commit()
    return dict(_get_board_row(connection, board_id))


#: Stated once, beside the ticket COUNT(*) check that raises
#: ConflictError below. Interpolated into the tool description, so the
#: refusal a caller will meet is the sentence they were given.
DELETE_BOARD_CONTRACT = "a board that still has tickets is refused"


@router.delete("/boards/{board_id}")
def delete_board(
    board_id: str,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    connection.execute("BEGIN IMMEDIATE")
    board = _get_board_row(connection, board_id)
    remaining = connection.execute(
        "SELECT COUNT(*) FROM tickets WHERE board_id = ?", (board_id,)
    ).fetchone()[0]
    if remaining:
        raise ConflictError(
            f"Board {board_id!r} still has {remaining} ticket(s); delete them first"
        )
    # If it ever issued a key, its prefix goes with it for good: keys from
    # deleted tickets on this board may still be written down somewhere,
    # and a new board deriving the same prefix would make them name
    # different tickets. A never-used prefix is simply freed.
    _release_prefix(connection, board)
    connection.execute("DELETE FROM boards WHERE id = ?", (board_id,))
    connection.commit()
    return {"deleted": True}


def _get_ticket_row(connection: sqlite3.Connection, ref: str) -> sqlite3.Row:
    """The ticket a UUID or key names, with its key; 404 when none.

    Callers use row["id"] from here on, never the reference they were
    given, so a key never reaches a stored column or a path on disk.
    """
    ticket_id = resolve_ticket_id(connection, ref)
    row = (
        None
        if ticket_id is None
        else connection.execute(
            f"{TICKET_SELECT} WHERE tickets.id = ?", (ticket_id,)
        ).fetchone()
    )
    if row is None:
        raise NotFoundError(_describe_ref(ref))
    return row


def _next_position(connection: sqlite3.Connection, board_id: str, status: str) -> int:
    row = connection.execute(
        "SELECT MAX(position) AS top FROM tickets WHERE board_id = ? AND status = ?",
        (board_id, status),
    ).fetchone()
    return 0 if row["top"] is None else row["top"] + 1


#: Stated once, beside the code that enforces it (_record_transition never commits; the caller commits both together).
#: Interpolated into the tool description; exercised by a test in
#: api/tests/test_claim_contracts.py.
TRANSITION_ATOMICITY_CONTRACT = "the history row is written in the same transaction as the move"


def _record_transition(
    connection: sqlite3.Connection,
    ticket_id: str,
    from_status: str | None,
    to_status: str,
    actor: str,
    note: str | None,
) -> None:
    """Append to the audit log. Never commits — the caller commits the
    status change and this row together so they cannot come apart."""
    connection.execute(
        "INSERT INTO ticket_transitions "
        "(id, ticket_id, from_status, to_status, actor, note) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid4()), ticket_id, from_status, to_status, actor, note),
    )


def _resolve_parent(
    connection: sqlite3.Connection, board_id: str, parent_ref: str, child_type: str
) -> str:
    """The parent's UUID, after checking it may be this ticket's parent.
    parent_ref may be a UUID or a key; only the UUID is ever stored."""
    parent_id = require_ticket_reference(connection, parent_ref)
    parent = connection.execute(
        "SELECT * FROM tickets WHERE id = ?", (parent_id,)
    ).fetchone()
    # Resolved and read are two statements. Under create_ticket's BEGIN
    # IMMEDIATE nothing can delete it in between, but update_ticket takes
    # no write lock first, so a concurrent delete can: answer that as the
    # 400 it is rather than a TypeError 500.
    if parent is None:
        raise InvalidReferenceError(_describe_ref(parent_ref))
    if parent["board_id"] != board_id:
        raise InvalidReferenceError("Parent ticket is on a different board")
    # Strict rank also rules out self-parenting and, transitively, cycles:
    # a ticket never outranks itself, and rank strictly decreases from parent
    # to child so no chain can return to where it started.
    validate_parent_rank(parent["type"], child_type)
    return parent_id


# response_model=None is required, not stylistic: the return annotation is a
# union of Response and dict, and without this FastAPI tries to build a
# response model from it and fails at import with "Invalid args for response
# field". The union is the point — this route answers 200-with-a-ticket or a
# bodyless 204.
@router.post("/tickets/claim", response_model=None)
def claim_ticket(
    body: ClaimRequest,
    connection: sqlite3.Connection = Depends(get_connection),
) -> Response | dict:
    """Atomically take the next agent_ready ticket. 204 when the column is
    drained.

    The UPDATE is conditional on status still being 'agent_ready', so when a
    polling loop and an interactive session race, the loser sees rowcount 0
    and moves to the next candidate rather than stealing a ticket already
    being worked. Never a read followed by a write.

    No validate_ticket_status call is needed: only story/bug/task can reach
    agent_ready in the first place, since the transition endpoint enforces
    that, so every candidate here is a type for which agent_coding is legal.

    Declared above create_ticket so the literal path is matched before
    /tickets/{ticket_id}.
    """
    query = "SELECT * FROM tickets WHERE status = 'agent_ready'"
    params: list = []
    if body.board_id is not None:
        query += " AND board_id = ?"
        params.append(body.board_id)
    # Ordering is load-bearing, not cosmetic: a loop drains this queue with no
    # chance for a human to intervene between pickups, so taking the wrong
    # ticket first means work built on the wrong base.
    query += " ORDER BY position IS NULL, position, created_at, id"

    for candidate in connection.execute(query, tuple(params)).fetchall():
        cursor = connection.execute(
            "UPDATE tickets SET status = 'agent_coding', claimed_by = ?, "
            "claimed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND status = 'agent_ready'",
            (body.actor, candidate["id"]),
        )
        if cursor.rowcount == 1:
            _record_transition(
                connection,
                candidate["id"],
                "agent_ready",
                "agent_coding",
                body.actor,
                "claimed by agent loop",
            )
            connection.commit()
            return dict(_get_ticket_row(connection, candidate["id"]))
        connection.rollback()

    return Response(status_code=204)


@router.post("/tickets", response_model=TicketResponse)
def create_ticket(
    body: TicketCreate,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    # The write lock before anything is read. The number comes from the
    # board's counter, and taking it inside this transaction is what makes
    # two concurrent creates on one board serialize rather than both read
    # the same value: the second waits on the lock (busy_timeout) and then
    # sees the first one's increment.
    connection.execute("BEGIN IMMEDIATE")
    board = connection.execute(
        "SELECT id FROM boards WHERE id = ?", (body.board_id,)
    ).fetchone()
    if board is None:
        raise InvalidReferenceError(f"No board with id {body.board_id!r}")
    parent_id = None
    if body.parent_id is not None:
        parent_id = _resolve_parent(
            connection, body.board_id, body.parent_id, body.type
        )

    number = connection.execute(
        "UPDATE boards SET next_ticket_number = next_ticket_number + 1 "
        "WHERE id = ? RETURNING next_ticket_number - 1",
        (body.board_id,),
    ).fetchone()[0]
    ticket_id = str(uuid4())
    connection.execute(
        "INSERT INTO tickets "
        "(id, board_id, parent_id, type, title, description, status, position, "
        "number) VALUES (?, ?, ?, ?, ?, ?, 'backlog', ?, ?)",
        (
            ticket_id,
            body.board_id,
            parent_id,
            body.type,
            body.title,
            body.description,
            _next_position(connection, body.board_id, "backlog"),
            number,
        ),
    )
    # Written here, not in Task 5's transition endpoint, so the history has no
    # gap at the start: MIN(created_at) over a ticket's transitions is its
    # creation time. Committed together with the INSERT below.
    _record_transition(connection, ticket_id, None, "backlog", body.actor, None)
    connection.commit()
    return dict(_get_ticket_row(connection, ticket_id))


@router.get("/tickets/{ticket_id}", response_model=TicketDetailResponse)
def get_ticket(
    ticket_id: str,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    """One ticket plus the context the design promises: its attachments and
    its child refs. parent_id is already on the row and is the parent ref.

    Only this route embeds. A listing that expanded every ticket's
    attachments would turn one query into N, which is the cost the board
    view deliberately avoids, and create/update keep returning the plain
    row for the same reason. The case this serves is an agent asking to see
    a ticket and its screenshots, which was two round trips.
    """
    ticket = dict(_get_ticket_row(connection, ticket_id))
    ticket_id = ticket["id"]
    attachments = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM ticket_attachments WHERE ticket_id = ? "
            "ORDER BY uploaded_at, rowid",
            (ticket_id,),
        ).fetchall()
    ]
    # Capped and counted. list_attachments was retired, so this embed is
    # the only path to attachments through MCP and had no paged form —
    # attachment_count makes a truncated list visible rather than looking
    # like the whole set.
    ticket["attachment_count"] = len(attachments)
    ticket["attachments"] = attachments[:TICKET_ATTACHMENT_CAP]
    ticket["artifacts"], ticket["artifact_count"] = ticket_artifacts(connection, ticket_id)
    # Refs, not whole rows: description is what would make a large epic's
    # response unbounded, so it is deliberately not selected.
    ticket["children"] = [
        dict(row)
        for row in connection.execute(
            f"SELECT id, key, type, title, status FROM ({TICKET_SELECT}) "
            "WHERE parent_id = ? "
            "ORDER BY position IS NULL, position, created_at, id",
            (ticket_id,),
        ).fetchall()
    ]
    return ticket


@router.get("/tickets")
def list_tickets(
    board_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    ticket_type: str | None = Query(default=None, alias="type"),
    parent_id: str | None = Query(default=None),
    # Stale-claim recovery: surfaces tickets stuck in agent_coding because the
    # agent holding them died. Recovery is an ordinary transition back to
    # agent_ready, so the abandonment stays visible in the history.
    claimed_before: str | None = Query(default=None),
    # ge=1: paginate() raises ValueError on a non-positive limit, which would
    # surface as a 500 rather than a 422.
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    # A filter naming something that does not exist is refused, not
    # answered with an empty page: the two are indistinguishable to
    # the caller, and only one of them is true (ticket T-08).
    require_reference(connection, "boards", board_id, "board")
    parent_id = require_ticket_reference(connection, parent_id)
    # Wrapped so the filters and ORDER BY below name the ticket's own
    # columns: joined bare, id/title/created_at would be ambiguous with the
    # board's.
    query = f"SELECT * FROM ({TICKET_SELECT}) WHERE 1=1"
    params: list = []
    # Column names come from this fixed tuple, never from user input, so the
    # f-string below cannot carry an injection.
    for column, value in (
        ("board_id", board_id),
        ("status", status),
        ("type", ticket_type),
        ("parent_id", parent_id),
    ):
        if value is not None:
            query += f" AND {column} = ?"
            params.append(value)
    if claimed_before is not None:
        # IS NOT NULL is explicit rather than relying on NULL < x being NULL:
        # correct either way today, but it survives a rewrite of this clause.
        query += " AND claimed_at IS NOT NULL AND claimed_at < ?"
        params.append(claimed_before)
    # id tiebreak: position may be NULL and created_at is not unique, so
    # without it two tied rows could repeat or vanish across paginated reads.
    query += " ORDER BY position IS NULL, position, created_at, id"
    return paginate(connection, query, tuple(params), limit, offset)


@router.patch("/tickets/{ticket_id}", response_model=TicketResponse)
def update_ticket(
    ticket_id: str,
    body: TicketUpdate,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    ticket = _get_ticket_row(connection, ticket_id)
    ticket_id = ticket["id"]
    updates = body.model_dump(exclude_unset=True)
    if "parent_id" in updates and updates["parent_id"] is not None:
        updates["parent_id"] = _resolve_parent(
            connection, ticket["board_id"], updates["parent_id"], ticket["type"]
        )
    if updates:
        # Keys are TicketUpdate's own field names — the model forbids extras,
        # so nothing caller-controlled reaches this f-string.
        assignments = ", ".join(f"{column} = ?" for column in updates)
        connection.execute(
            f"UPDATE tickets SET {assignments}, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (*updates.values(), ticket_id),
        )
        connection.commit()
    return dict(_get_ticket_row(connection, ticket_id))


#: Stated once, beside the code that enforces it (a child COUNT(*) check raising ConflictError before any delete).
#: Interpolated into the tool description; exercised by a test in
#: api/tests/test_claim_contracts.py.
DELETE_TICKET_CONTRACT = "a ticket with children is refused"


@router.delete("/tickets/{ticket_id}")
def delete_ticket(
    ticket_id: str,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    # 200 + {"deleted": true} — see delete_board; one response contract for
    # "delete succeeded" across the whole API.
    ticket_id = _get_ticket_row(connection, ticket_id)["id"]
    children = connection.execute(
        "SELECT COUNT(*) FROM tickets WHERE parent_id = ?", (ticket_id,)
    ).fetchone()[0]
    if children:
        raise ConflictError(f"Ticket {ticket_id!r} still has {children} child ticket(s)")
    # Attachment files first: a ticket's bytes must not outlive it, or
    # data/jyra/ accumulates orphans no row points at.
    for attachment in connection.execute(
        "SELECT file_path FROM ticket_attachments WHERE ticket_id = ?", (ticket_id,)
    ).fetchall():
        Path(attachment["file_path"]).unlink(missing_ok=True)
    connection.execute(
        "DELETE FROM ticket_attachments WHERE ticket_id = ?", (ticket_id,)
    )
    # Transitions next: ticket_transitions.ticket_id is a real FK, so the
    # ticket cannot go while its history remains. Deleting a ticket therefore
    # destroys its audit trail — deliberate, and pinned by a test.
    connection.execute(
        "DELETE FROM ticket_transitions WHERE ticket_id = ?", (ticket_id,)
    )
    # Canvas links go with the ticket, like its attachments. The linked
    # artifacts stay, with their other links (The owner, 2026-09-26).
    connection.execute(
        "DELETE FROM artifact_links WHERE target_type = 'ticket' AND target_id = ?",
        (ticket_id,),
    )
    connection.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
    connection.commit()
    return {"deleted": True}


#: The order this endpoint returns, stated once beside the query
#: that produces it (ORDER BY created_at, rowid). The tool description
#: interpolates this rather than retyping it, and a behavioural
#: test asserts the rows actually come back this way -- a flipped
#: ORDER BY is invisible to every caller who believed the sentence.
TICKET_HISTORY_ORDER = "oldest first"


@router.get("/tickets/{ticket_id}/transitions")
def list_transitions(
    ticket_id: str,
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    ticket = _get_ticket_row(connection, ticket_id)
    # rowid, not id, as the tiebreak. created_at has one-second resolution, so
    # several transitions on one ticket routinely share a timestamp — and id is
    # a random uuid4, which would order those ties arbitrarily and scramble the
    # history. rowid is monotonic in insertion order, so it resolves ties the
    # only way an append-only audit log can be read: oldest first.
    #
    # (This table has a rowid: a TEXT PRIMARY KEY does not make it WITHOUT
    # ROWID, which would have to be declared explicitly.)
    return paginate(
        connection,
        # ticket_key on every entry, so a page of history read on its own
        # still says which ticket it belongs to in the form people use.
        "SELECT *, ? AS ticket_key FROM ticket_transitions WHERE ticket_id = ? "
        "ORDER BY created_at, rowid",
        (ticket["key"], ticket["id"]),
        limit,
        offset,
    )


@router.post("/tickets/{ticket_id}/transition", response_model=TicketResponse)
def transition_ticket(
    ticket_id: str,
    body: TransitionCreate,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    # Acquire the writer lock before observing history; checking only status or
    # second-resolution timestamps misses human roundtrips to the same status.
    connection.execute("BEGIN IMMEDIATE")
    ticket = _get_ticket_row(connection, ticket_id)
    ticket_id = ticket["id"]
    if body.expected_transition_id is not None:
        latest = connection.execute(
            "SELECT id FROM ticket_transitions WHERE ticket_id = ? ORDER BY rowid DESC LIMIT 1",
            (ticket_id,),
        ).fetchone()
        if latest is None or latest["id"] != str(body.expected_transition_id):
            raise ConflictError("Ticket history changed; read current history before retrying the transition.")
    # field="to_status" so a rejection points at the field the caller actually
    # sent; the create route sends "status" instead.
    validate_ticket_status(ticket["type"], body.to_status, field="to_status")

    from_status = ticket["status"]
    # Moving into a new column puts the ticket at the bottom of it. A move to
    # the status it already holds keeps its place — it is still a real event
    # worth recording, but re-positioning would shuffle it for no reason.
    position = (
        ticket["position"]
        if body.to_status == from_status
        else _next_position(connection, ticket["board_id"], body.to_status)
    )
    connection.execute(
        "UPDATE tickets SET status = ?, position = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE id = ?",
        (body.to_status, position, ticket_id),
    )
    # Same transaction as the UPDATE above: a status change and its history
    # row are committed together, so a ticket's history cannot have gaps and a
    # rejected move leaves neither behind.
    _record_transition(
        connection, ticket_id, from_status, body.to_status, body.actor, body.note
    )
    connection.commit()
    return dict(_get_ticket_row(connection, ticket_id))


# The on-disk name is <attachment-uuid><suffix>, and a filesystem path
# component is capped at 255 bytes — so an unbounded caller-supplied suffix
# reached open() and raised OSError: File name too long, escaping as a
# plain-text 500 with no error envelope. That was fixed by truncating to
# MAX_SUFFIX = 32; the bound is now structural instead, since
# `storable_suffix` returns a member of ATTACHMENT_MEDIA_TYPES or nothing
# at all, and the longest of those is five characters. The truncation
# constant is gone rather than kept "just in case": a second bound that
# can never bind is a thing future readers reason about for no reason.


def _safe_filename(raw: str) -> str:
    """Reduce a client-supplied filename to a bare basename.

    Windows separators are normalized first: Path("a\\b").name is the whole
    string on POSIX, so a backslash-separated path would survive untouched.
    Leading dots go too, which handles "..", "..." and hidden-file names in
    one step. Null bytes are stripped because they raise ValueError at open()
    time rather than being rejected anywhere useful.

    Only ever used for the display name and the stored suffix — the file on
    disk is named by attachment UUID — so this is the second line of defence,
    not the only one.
    """
    candidate = Path(raw.replace("\\", "/")).name
    candidate = candidate.replace("\x00", "").strip().lstrip(".")
    return candidate or "attachment"


#: Suffix -> media type for serving stored attachment bytes. Small and
#: explicit rather than mimetypes.guess_type, which consults the host's
#: mime database: the same attachment would then be served differently on
#: two machines, and "what type is this" would depend on where the server
#: runs rather than on what was stored.
ATTACHMENT_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
}
DEFAULT_ATTACHMENT_MEDIA_TYPE = "application/octet-stream"


def media_type_for_suffix(suffix: str) -> str:
    """The ONE derivation of an attachment's media type.

    Used at upload to compute the stored `content_type`, and by the
    content route for rows that predate that column. A single function
    rather than two lookups because a list and a fetch of the same
    attachment must not be able to disagree about what it is -- and two
    call sites doing "the same" dict lookup is a property maintained by
    vigilance rather than by construction.
    """
    return ATTACHMENT_MEDIA_TYPES.get(suffix.lower(), DEFAULT_ATTACHMENT_MEDIA_TYPE)


def storable_suffix(filename: str) -> str:
    """The suffix to put on disk: an allow-listed one, or none (ticket T-20).

    Previously the client's own suffix was carried over, bounded to 32
    bytes. That was contained -- no separator survives `_safe_filename`,
    so it stayed one path component -- but it made the on-disk name carry
    arbitrary client bytes, and it made the served media type a function
    of a client-controlled string.

    Ruled for the allow-list because `ATTACHMENT_MEDIA_TYPES` already IS
    one: applying it at WRITE time as well as read time collapses the two
    derivations into a closed set, and stops the filesystem holding
    anything the caller chose.
    """
    suffix = Path(filename).suffix.lower()
    return suffix if suffix in ATTACHMENT_MEDIA_TYPES else ""


@router.get("/attachments/{attachment_id}/content")
def read_attachment_content(
    attachment_id: str,
    connection: sqlite3.Connection = Depends(get_connection),
) -> Response:
    """Serve a stored attachment BY ID. The path comes from the row.

    There is no parameter for a path and there cannot be one: the route
    takes an id, looks the path up from `ticket_attachments`, and serves
    what is there. That is the whole point of the ticket behind it --
    returning `file_path` "sets up a predictable future bypass... the
    obvious design for a future download route is to accept the path the
    API already handed back" (review-1, 2026). This route consults the
    id-based storage scheme rather than trusting a caller's string.

    It serves the stored BYTES, not extracted text, because bytes are
    what is stored: upload writes the payload verbatim. Nothing holds an
    extracted copy, and a screenshot -- the first example in the tool's
    own description -- has no text to hold.
    """
    row = connection.execute(
        "SELECT * FROM ticket_attachments WHERE id = ?", (attachment_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"No attachment with id {attachment_id!r}")

    stored = Path(row["file_path"])
    if not stored.is_file():
        # The row outlived its bytes. Says so plainly rather than 404ing
        # as though the attachment never existed -- those are different
        # facts and lead to different actions.
        raise NotFoundError(
            f"Attachment {attachment_id!r} is recorded but its file is missing"
        )

    # The stored column is authoritative; the suffix is consulted only for
    # rows written before that column existed. Same function either way,
    # so the type served here and the type a listing reports cannot come
    # from different rules.
    recorded = row["content_type"] if "content_type" in row.keys() else None
    return Response(
        content=stored.read_bytes(),
        media_type=recorded or media_type_for_suffix(stored.suffix),
    )


@router.post("/tickets/{ticket_id}/attachments", response_model=AttachmentResponse)
def upload_attachment(
    ticket_id: str,
    upload: UploadFile = File(...),
    connection: sqlite3.Connection = Depends(get_connection),
    settings: Settings = Depends(get_settings),
) -> dict:
    # The UUID from here on: it names the attachment directory on disk,
    # and a key there would split one ticket's files across two names.
    ticket_id = _get_ticket_row(connection, ticket_id)["id"]

    payload = upload.file.read()

    # Cap first: refuse a large file before doing anything else with it.
    if len(payload) > MAX_ATTACHMENT_BYTES:
        raise AttachmentTooLargeError(
            f"attachment is {len(payload)} bytes, over the "
            f"{MAX_ATTACHMENT_BYTES}-byte limit"
        )

    # Then content. Enforced HERE as well as in the tool, and not because
    # two checks are tidier: the tool's check is what stops a bad path
    # being READ, and this one is the only thing standing between the
    # attachment store and anything that did not arrive through the tool.
    refuse_unredacted_attachment(payload, upload.filename or "attachment")

    attachment_id = str(uuid4())
    filename = _safe_filename(upload.filename or "attachment")
    try:
        refuse_sensitive_numbers(filename)
    except ValueError as exc:
        raise AttachmentNotRedactedError(
            "the attachment filename contains an account-shaped digit run; "
            "rename it with only a masked last four before uploading"
        ) from exc

    directory = Path(settings.jyra_dir) / ticket_id
    directory.mkdir(parents=True, exist_ok=True)
    # Named by attachment UUID, keeping only the sanitized suffix, so two
    # uploads called screenshot.png cannot overwrite each other. The suffix
    # does carry part of the client's string -- "..%2F..%2Fetc%2Fpasswd"
    # lands as "{uuid}.%2Fetc%2Fpasswd" -- but no SEPARATOR survives
    # _safe_filename, so it stays one path component rather than becoming
    # several. The is_relative_to check below is what proves that.
    suffix = storable_suffix(filename)
    destination = (directory / f"{attachment_id}{suffix}").resolve()

    # Defence in depth: _safe_filename already strips separators, but verify
    # the resolved path really landed under jyra_dir before writing anything.
    # resolve() first means a symlinked jyra_dir is compared like for like.
    root = Path(settings.jyra_dir).resolve()
    if not destination.is_relative_to(root):
        raise InvalidReferenceError("Attachment path escapes the jyra directory")

    destination.write_bytes(payload)

    connection.execute(
        "INSERT INTO ticket_attachments "
        "(id, ticket_id, filename, file_path, content_hash, size_bytes, "
        "content_type) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            attachment_id,
            ticket_id,
            filename,
            str(destination),
            hashlib.sha256(payload).hexdigest(),
            # Measured from the stored bytes, not from a client-supplied
            # length and not from the file afterwards: this IS what was
            # written, on the line that writes it.
            len(payload),
            media_type_for_suffix(suffix),
        ),
    )
    connection.commit()
    row = connection.execute(
        "SELECT * FROM ticket_attachments WHERE id = ?", (attachment_id,)
    ).fetchone()
    return dict(row)


@router.get("/tickets/{ticket_id}/attachments")
def list_attachments(
    ticket_id: str,
    # ge=1: the plan omits it here as it did on four earlier listings. See
    # a todo — paginate() now rejects a bad limit itself, but the route
    # still declares the bound so the error names the constraint.
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    ticket_id = _get_ticket_row(connection, ticket_id)["id"]
    # Columns named rather than SELECT *, and file_path deliberately not
    # among them. This route never used AttachmentResponse, so when #130
    # dropped the field from that model the column still flowed straight
    # back out of here -- and this is the route `list_attachments`
    # exposes over MCP, which would have put every attachment's absolute
    # path into a model's context: the exact bypass D107 closed, in the
    # exact medium it was found in.
    return paginate(
        connection,
        "SELECT id, ticket_id, filename, content_hash, size_bytes, "
        "content_type, uploaded_at FROM ticket_attachments "
        "WHERE ticket_id = ? ORDER BY uploaded_at, rowid",
        (ticket_id,),
        limit,
        offset,
    )


@router.delete("/attachments/{attachment_id}")
def delete_attachment(
    attachment_id: str,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    # 200 + {"deleted": true} — see delete_board.
    row = connection.execute(
        "SELECT * FROM ticket_attachments WHERE id = ?", (attachment_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"No attachment with id {attachment_id!r}")
    Path(row["file_path"]).unlink(missing_ok=True)
    connection.execute("DELETE FROM ticket_attachments WHERE id = ?", (attachment_id,))
    connection.commit()
    return {"deleted": True}

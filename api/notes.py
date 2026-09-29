"""CRUD for `notes` and their polymorphic `note_links` (ticket T-23).

A note is freeform text that is revised in place: iterating on a draft is
the main use, so an edit is a single PATCH of the body, not a new revision.
It can stand alone or be linked to any number of things; a link is
(target_type, target_id) with no SQL foreign key, so this module is where
the target's existence is checked (db/schema.sql says so beside the table).

Titles are derived, not stored: the first non-blank line of the body with
leading Markdown `#`s removed. A separate column would be a second place
the name lives, drifting from the body the moment a draft is edited
(decisions-log, "Notes: title is the first line").

List responses carry a title and a short preview rather than whole bodies.
A plan document can be long, and listing twenty of them should not put all
twenty into a model's context; `GET /notes/{id}` returns the full body.
"""

import sqlite3
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from api.auth import require_token
from api.db import MAX_LIMIT, get_connection, paginate
from api.errors import InvalidReferenceError, NotFoundError
from api.jyra import require_ticket_reference, resolve_ticket_id
from api.privacy import StoredText

router = APIRouter(dependencies=[Depends(require_token)])

NoteTargetType = Literal["entity", "transaction", "document", "reminder", "statement", "ticket"]

#: Where each target type lives. Transactions and statements are read through
#: their `active_*` views (test_archive_scope.py polices raw reads): an
#: archived row is an undone import, and a note should not attach to it.
TARGET_TABLES: dict[str, str] = {
    "entity": "entities",
    "transaction": "active_transactions",
    "document": "documents",
    "reminder": "reminders",
    "statement": "active_statements",
    "ticket": "tickets",
}

#: Generous for a long plan, bounded so one note cannot swamp a response.
MAX_BODY_CHARS = 100_000
TITLE_CHARS = 120
PREVIEW_CHARS = 240


class NoteLinkTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_type: NoteTargetType
    target_id: str = Field(min_length=1, max_length=200)


class NoteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: StoredText = Field(min_length=1, max_length=MAX_BODY_CHARS)
    links: list[NoteLinkTarget] = Field(default_factory=list, max_length=50)


class NoteUpdate(BaseModel):
    """Body is the only editable field, so it is required: an empty PATCH
    would report success without changing anything (the `*Update` sweep)."""

    model_config = ConfigDict(extra="forbid")

    body: StoredText = Field(min_length=1, max_length=MAX_BODY_CHARS)


def derive_title(body: str) -> str:
    for line in body.splitlines():
        text = line.strip().lstrip("#").strip()
        if text:
            return text if len(text) <= TITLE_CHARS else text[: TITLE_CHARS - 1].rstrip() + "…"
    return "(untitled)"


def _preview(body: str) -> str:
    flat = " ".join(body.split())
    return flat if len(flat) <= PREVIEW_CHARS else flat[: PREVIEW_CHARS - 1].rstrip() + "…"


def _get_note_row(connection: sqlite3.Connection, note_id: str) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"No note with id {note_id!r}")
    return row


def _links(connection: sqlite3.Connection, note_id: str) -> list[dict]:
    # key: a ticket target's human-readable key (KA-12), so a link reads the
    # way people name the ticket; null for every other target type.
    return [
        dict(row)
        for row in connection.execute(
            "SELECT l.id, l.target_type, l.target_id, "
            "b.key_prefix || '-' || t.number AS key FROM note_links l "
            "LEFT JOIN tickets t ON l.target_type = 'ticket' AND t.id = l.target_id "
            "LEFT JOIN boards b ON b.id = t.board_id "
            "WHERE l.note_id = ? ORDER BY l.target_type, l.target_id, l.id",
            (note_id,),
        )
    ]


def _note_response(connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "title": derive_title(row["body"]),
        "body": row["body"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "links": _links(connection, row["id"]),
    }


def require_target(connection: sqlite3.Connection, target_type: str, target_id: str) -> str:
    """The target's stored id; 400 when it does not exist, since no FK will
    refuse it. A ticket may be named by its key (KA-12), which resolves to
    its UUID here so a link always stores the UUID."""
    if target_type == "ticket":
        return require_ticket_reference(connection, target_id)
    table = TARGET_TABLES[target_type]  # target_type already validated as a Literal
    found = connection.execute(
        f"SELECT 1 FROM {table} WHERE id = ?", (target_id,)  # noqa: S608 - table from a fixed map
    ).fetchone()
    if found is None:
        archived = " (archived rows cannot be linked)" if table.startswith("active_") else ""
        raise InvalidReferenceError(f"No {target_type} with id {target_id!r}{archived}")
    return target_id


def require_any_target(connection: sqlite3.Connection, target_id: str) -> str:
    """The stored id `target_id` names, of some linkable type; 400 when it
    names nothing. A ticket key resolves to the ticket's UUID."""
    for table in TARGET_TABLES.values():
        if connection.execute(
            f"SELECT 1 FROM {table} WHERE id = ?", (target_id,)  # noqa: S608 - table from a fixed map
        ).fetchone():
            return target_id
    ticket_id = resolve_ticket_id(connection, target_id)
    if ticket_id is not None:
        return ticket_id
    raise InvalidReferenceError(f"Nothing linkable has id {target_id!r}")


def _insert_link(connection: sqlite3.Connection, note_id: str, target: NoteLinkTarget) -> str:
    """Idempotent: re-linking the same target returns the existing link."""
    existing = connection.execute(
        "SELECT id FROM note_links WHERE note_id = ? AND target_type = ? AND target_id = ?",
        (note_id, target.target_type, target.target_id),
    ).fetchone()
    if existing is not None:
        return existing["id"]
    link_id = str(uuid4())
    connection.execute(
        "INSERT INTO note_links (id, note_id, target_type, target_id) VALUES (?, ?, ?, ?)",
        (link_id, note_id, target.target_type, target.target_id),
    )
    return link_id


@router.post("/notes")
def create_note(body: NoteCreate, connection: sqlite3.Connection = Depends(get_connection)) -> dict:
    # Every target checked before anything is written, so a bad link in the
    # list refuses the whole create rather than leaving a half-linked note.
    links = [
        target.model_copy(
            update={"target_id": require_target(connection, target.target_type, target.target_id)}
        )
        for target in body.links
    ]
    note_id = str(uuid4())
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("INSERT INTO notes (id, body) VALUES (?, ?)", (note_id, body.body))
    for target in links:
        _insert_link(connection, note_id, target)
    connection.commit()
    return _note_response(connection, _get_note_row(connection, note_id))


@router.get("/notes/{note_id}")
def get_note(note_id: str, connection: sqlite3.Connection = Depends(get_connection)) -> dict:
    return _note_response(connection, _get_note_row(connection, note_id))


#: Stated once beside the query that produces it; the tool description
#: interpolates it and a test asserts the rows really come back this way.
LIST_NOTES_ORDER = "most recently edited first (created time for never-edited notes), then id"


@router.get("/notes")
def list_notes(
    target_type: NoteTargetType | None = Query(default=None),
    target_id: str | None = Query(default=None),
    q: str | None = Query(default=None, min_length=1, max_length=200),
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    query = "SELECT * FROM notes n WHERE 1=1"
    params: list = []
    # The two filters are independent: a type alone lists notes linked to
    # anything of that type, an id alone lists notes linked to that thing
    # (ids are UUIDs, unique across tables), both together name one target.
    # A filter naming something that does not exist is refused, not
    # answered with an empty page (ticket T-08).
    if target_type is not None and target_id is not None:
        target_id = require_target(connection, target_type, target_id)
    elif target_id is not None:
        target_id = require_any_target(connection, target_id)
    if target_type is not None or target_id is not None:
        clause = "SELECT 1 FROM note_links l WHERE l.note_id = n.id"
        if target_type is not None:
            clause += " AND l.target_type = ?"
            params.append(target_type)
        if target_id is not None:
            clause += " AND l.target_id = ?"
            params.append(target_id)
        query += f" AND EXISTS ({clause})"
    if q is not None:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query += " AND n.body LIKE ? ESCAPE '\\'"
        params.append(f"%{escaped}%")
    query += " ORDER BY COALESCE(n.updated_at, n.created_at) DESC, n.id"
    page = paginate(connection, query, tuple(params), limit=limit, offset=offset)
    page["items"] = [
        {
            "id": row["id"],
            "title": derive_title(row["body"]),
            "preview": _preview(row["body"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "link_count": connection.execute(
                "SELECT count(*) FROM note_links WHERE note_id = ?", (row["id"],)
            ).fetchone()[0],
        }
        for row in page["items"]
    ]
    return page


@router.patch("/notes/{note_id}")
def update_note(note_id: str, body: NoteUpdate, connection: sqlite3.Connection = Depends(get_connection)) -> dict:
    _get_note_row(connection, note_id)
    connection.execute(
        "UPDATE notes SET body = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (body.body, note_id)
    )
    connection.commit()
    return _note_response(connection, _get_note_row(connection, note_id))


@router.delete("/notes/{note_id}")
def delete_note(note_id: str, connection: sqlite3.Connection = Depends(get_connection)) -> dict:
    _get_note_row(connection, note_id)
    connection.execute("BEGIN IMMEDIATE")
    removed = connection.execute("DELETE FROM note_links WHERE note_id = ?", (note_id,)).rowcount
    connection.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    connection.commit()
    return {"deleted": note_id, "links_removed": removed}


@router.post("/notes/{note_id}/links")
def link_note(note_id: str, body: NoteLinkTarget, connection: sqlite3.Connection = Depends(get_connection)) -> dict:
    _get_note_row(connection, note_id)
    body = body.model_copy(
        update={"target_id": require_target(connection, body.target_type, body.target_id)}
    )
    link_id = _insert_link(connection, note_id, body)
    connection.commit()
    response = _note_response(connection, _get_note_row(connection, note_id))
    response["link_id"] = link_id
    return response


@router.delete("/notes/{note_id}/links/{link_id}")
def unlink_note(note_id: str, link_id: str, connection: sqlite3.Connection = Depends(get_connection)) -> dict:
    _get_note_row(connection, note_id)
    removed = connection.execute(
        "DELETE FROM note_links WHERE id = ? AND note_id = ?", (link_id, note_id)
    ).rowcount
    if not removed:
        raise NotFoundError(f"Note {note_id!r} has no link with id {link_id!r}")
    connection.commit()
    return _note_response(connection, _get_note_row(connection, note_id))

"""Links from an artifact to tickets and entities (Canvas).

Polymorphic like note_links: `target_id` has no FK, so this module checks the
target exists when linking. Entity delete is refused while linked (the
registry in api/entities.py); ticket delete removes only its links
(api/jyra.py). Artifacts themselves are never deleted by either.
"""
import sqlite3
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query
from pydantic import Field

from api.artifact_kinds import viewer_path
from api.artifacts import artifact_exists, now
from api.auth import require_token
from api.briefing_models import Model
from api.db import MAX_LIMIT, get_connection, paginate
from api.errors import InvalidReferenceError, NotFoundError
from api.privacy import StoredText

router = APIRouter(dependencies=[Depends(require_token)])

#: Stated once beside the query that enforces it. Pinned in
#: api/tests/test_ordering_contracts.py and interpolated into the MCP tool.
LIST_ARTIFACT_LINKS_ORDER = "oldest first"

#: get_ticket embeds at most this many linked artifacts; artifact_count is the
#: true total, as with TICKET_ATTACHMENT_CAP.
TICKET_ARTIFACT_CAP = 100

TARGET_TABLES = {'ticket': 'tickets', 'entity': 'entities'}

LINK_COLUMNS = 'l.id, l.artifact_id, l.target_type, l.target_id, l.actor, l.created_at'


class ArtifactLinkCreate(Model):
    target_type: Literal['ticket', 'entity']
    target_id: str = Field(min_length=1, max_length=100)
    actor: StoredText = Field(min_length=1, max_length=100)


def require_link_target(connection, target_type, target_id):
    """The target's stored id; 400 when it names nothing. A ticket may be
    named by its key (KA-12, any case), which resolves to its UUID here so a
    link always stores the UUID."""
    if target_type == 'ticket':
        # Function-local: api.jyra imports ticket_artifacts from this module.
        from api.jyra import require_ticket_reference

        return require_ticket_reference(connection, target_id)
    table = TARGET_TABLES[target_type]  # target_type is validated as a Literal
    if connection.execute(f'SELECT 1 FROM {table} WHERE id = ?', (target_id,)).fetchone() is None:  # noqa: S608 - fixed map
        raise InvalidReferenceError(f'No {target_type} with id {target_id!r}')
    return target_id


def link_row(connection, link_id):
    return dict(connection.execute(f'SELECT {LINK_COLUMNS} FROM artifact_links l WHERE l.id = ?', (link_id,)).fetchone())


@router.post('/artifacts/{artifact_id}/links')
def link_artifact(artifact_id: UUID, body: ArtifactLinkCreate, connection: sqlite3.Connection = Depends(get_connection)):
    """Idempotent: linking the same target again returns the existing link."""
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        artifact_exists(connection, artifact_id)
        target_id = require_link_target(connection, body.target_type, body.target_id)
        existing = connection.execute(
            'SELECT id FROM artifact_links WHERE artifact_id = ? AND target_type = ? AND target_id = ?',
            (str(artifact_id), body.target_type, target_id)).fetchone()
        if existing:
            return {**link_row(connection, existing['id']), 'created': False}
        link_id = str(uuid4())
        connection.execute('INSERT INTO artifact_links (id, artifact_id, target_type, target_id, actor, created_at) '
                           'VALUES (?, ?, ?, ?, ?, ?)',
                           (link_id, str(artifact_id), body.target_type, target_id, body.actor, now()))
        return {**link_row(connection, link_id), 'created': True}


@router.delete('/artifacts/{artifact_id}/links/{link_id}')
def unlink_artifact(artifact_id: UUID, link_id: str, connection: sqlite3.Connection = Depends(get_connection)):
    with connection:
        removed = connection.execute('DELETE FROM artifact_links WHERE id = ? AND artifact_id = ?',
                                     (link_id, str(artifact_id))).rowcount
        if not removed:
            raise NotFoundError(f'Artifact {artifact_id} has no link with id {link_id!r}')
    return {'deleted': True}


@router.get('/artifacts/{artifact_id}/links')
def list_artifact_links(artifact_id: UUID, limit: int = Query(50, ge=1, le=MAX_LIMIT), offset: int = Query(0, ge=0),
                        connection: sqlite3.Connection = Depends(get_connection)):
    """Link rows with the target's ticket key and title or entity name,
    oldest first. key (KA-12) is null for an entity target."""
    artifact_exists(connection, artifact_id)
    query = f'''SELECT {LINK_COLUMNS},
        CASE l.target_type WHEN 'ticket' THEN b.key_prefix || '-' || t.number END AS key,
        CASE l.target_type WHEN 'ticket' THEN t.title END AS title,
        CASE l.target_type WHEN 'entity' THEN e.name END AS name
        FROM artifact_links l
        LEFT JOIN tickets t ON l.target_type = 'ticket' AND t.id = l.target_id
        LEFT JOIN boards b ON b.id = t.board_id
        LEFT JOIN entities e ON l.target_type = 'entity' AND e.id = l.target_id
        WHERE l.artifact_id = ? ORDER BY l.created_at, l.rowid'''
    return paginate(connection, query, (str(artifact_id),), limit, offset)


def ticket_artifacts(connection, ticket_id):
    """(refs, total) for get_ticket: capped at TICKET_ARTIFACT_CAP, oldest link first."""
    rows = connection.execute('''SELECT l.id AS link_id, a.id AS artifact_id, a.kind, a.revision AS current_revision,
        json_extract(r.payload, '$.title') AS title
        FROM artifact_links l JOIN artifacts a ON a.id = l.artifact_id
        JOIN artifact_revisions r ON r.artifact_id = a.id AND r.revision = a.revision
        WHERE l.target_type = 'ticket' AND l.target_id = ? ORDER BY l.created_at, l.rowid''', (ticket_id,)).fetchall()
    refs = [{**dict(row), 'viewer_path': viewer_path(row['kind'], row['artifact_id'])} for row in rows[:TICKET_ARTIFACT_CAP]]
    return refs, len(rows)

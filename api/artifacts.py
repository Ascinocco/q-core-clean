"""Private immutable revisions, one atomic transaction per mutation."""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from api.artifact_kinds import ALL_KINDS, KINDS, viewer_path
from api.canvas_diagram_ops import EditDiagram, OpError, apply_ops, diagram_summary
from api.auth import require_token
from api.briefing_content import screen_payload
from api.artifact_models import ArtifactCreate, ArtifactEdit, ArtifactUpdate
from api.config import Settings, get_settings
from api.db import MAX_LIMIT, get_connection, paginate, require_reference
from api.errors import ConflictError, InvalidReferenceError, NotFoundError
from api.personal_redaction import PersonalRedactionError

router = APIRouter(dependencies=[Depends(require_token)])

REVISION_INSERT = ('INSERT INTO artifact_revisions (artifact_id, revision, payload, actor, note, created_at) '
                   'VALUES (?, ?, ?, ?, ?, ?)')


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def packed(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(packed(value).encode()).hexdigest()


def artifact(connection, artifact_id, revision=None):
    row = connection.execute('SELECT * FROM artifacts WHERE id = ?', (str(artifact_id),)).fetchone()
    if row is None:
        raise NotFoundError('Artifact not found')
    selected = row['revision'] if revision is None else revision
    version = connection.execute('SELECT * FROM artifact_revisions WHERE artifact_id = ? AND revision = ?', (str(artifact_id), selected)).fetchone()
    if version is None:
        raise NotFoundError('Artifact revision not found')
    return {'id': row['id'], 'kind': row['kind'], 'created_at': row['created_at'],
            'updated_at': row['updated_at'], 'current_revision': row['revision'],
            'revision': selected, 'revision_created_at': version['created_at'],
            'actor': version['actor'], 'note': version['note'],
            'document': json.loads(version['payload']), 'viewer_path': viewer_path(row['kind'], row['id'])}


def artifact_exists(connection, artifact_id):
    if connection.execute('SELECT 1 FROM artifacts WHERE id = ?', (str(artifact_id),)).fetchone() is None:
        raise NotFoundError('Artifact not found')


def rules_for(kind, loc=('body', 'kind')):
    """The kind's rules, or a 422 at `loc`: the request's own kind on create,
    the document on update (whose kind is the stored row's, not a body field)."""
    rules = KINDS.get(kind)
    if rules is None:
        raise RequestValidationError([{'loc': loc, 'msg': f"Artifact kind '{kind}' is not enabled yet", 'type': 'value_error'}])
    return rules


def screened(kind, payload, settings, loc=('body', 'kind')):
    """Validate the document against its kind's model, then screen the payload."""
    rules = rules_for(kind, loc)
    payload = dict(payload)
    try:
        payload['document'] = rules.document_model.model_validate(payload['document']).model_dump(mode='json')
    except ValidationError as error:
        raise RequestValidationError([{**e, 'loc': ('body', 'document', *e['loc'])}
                                      for e in error.errors(include_url=False, include_context=False)]) from None
    if rules.screen_document is None:
        return screen_payload(payload, settings, html=rules.screen_html, refusal=rules.refusal, detailed=rules.detailed_errors)
    # Structured kinds screen their own document: the generic walk would
    # rewrite values (such as diagram ids) the kind's screen must see intact.
    document = payload.pop('document')
    payload = screen_payload(payload, settings, refusal=rules.refusal, detailed=rules.detailed_errors)
    try:
        payload['document'] = rules.screen_document(document, settings)
    except ValueError as error:
        message = f'{rules.refusal}: {error}' if rules.detailed_errors else rules.refusal
        raise RequestValidationError([{'loc': ('body', 'content'), 'msg': message, 'type': 'value_error'}]) from None
    return payload


STALE = 'Artifact changed; reread before editing'

#: What a refused edit raises: a 422 from validation or a screen, a 400
#: invalid_reference or 409 conflict from an operation, or a missing privacy
#: profile. On a stale request any of these means "reread", not "fix the
#: content" (ticket T-03). Named exactly, not HTTPException: a 404 (a
#: revision that has gone) or any other failure is real and propagates.
REFUSALS = (RequestValidationError, InvalidReferenceError, ConflictError, PersonalRedactionError)


def write_revision(connection, artifact_id, expected_revision, compute, refuse=None):
    """Write revision expected_revision + 1, stale-first. The one path for PUT and both PATCHes.

    Inside BEGIN IMMEDIATE:
    - refuse(current) raises for what the artifact itself forbids (a wrong or
      unregistered kind), whatever the revision;
    - a revision from the future is 409;
    - current: compute(current) builds and screens the payload
      ({document, actor, note}), its refusals propagate, and the revision is written;
    - stale: the only success is an exact retry, a result equal to revision
      expected_revision + 1. A result that cannot be computed or screened now
      cannot be one, so every refusal becomes 409 STALE.

    Request-shape validation (the body model) has already run before this, so
    a malformed request is 422 whatever its revision (decisions-log, 2026-09-28).
    """
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        current = artifact(connection, artifact_id)
        if refuse is not None:
            refuse(current)
        if expected_revision > current['current_revision']:
            raise ConflictError(STALE)
        stale = expected_revision != current['current_revision']
        try:
            payload = compute(current)
        except REFUSALS:
            if stale:
                raise ConflictError(STALE) from None
            raise
        encoded = packed(payload['document'])
        if stale:
            prior = connection.execute('SELECT * FROM artifact_revisions WHERE artifact_id = ? AND revision = ?',
                                       (str(artifact_id), expected_revision + 1)).fetchone()
            if prior and (prior['payload'], prior['actor'], prior['note']) == (encoded, payload['actor'], payload['note']):
                return artifact(connection, artifact_id, expected_revision + 1)
            raise ConflictError(STALE)
        revision, stamp = expected_revision + 1, now()
        connection.execute(REVISION_INSERT, (str(artifact_id), revision, encoded, payload['actor'], payload['note'], stamp))
        connection.execute('UPDATE artifacts SET revision = ?, updated_at = ? WHERE id = ?', (revision, stamp, str(artifact_id)))
        return artifact(connection, artifact_id)


@router.post('/artifacts')
def create_artifact(body: ArtifactCreate, settings: Settings = Depends(get_settings), connection: sqlite3.Connection = Depends(get_connection)):
    payload = screened(body.kind, body.model_dump(mode='json'), settings)
    fingerprint = digest(payload)
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        existing = connection.execute('SELECT * FROM artifacts WHERE request_id = ?', (str(body.request_id),)).fetchone()
        if existing:
            if existing['request_hash'] != fingerprint:
                raise ConflictError('Request ID already used with different content')
            return artifact(connection, existing['id'], 1)
        identifier, stamp = str(uuid4()), now()
        connection.execute('INSERT INTO artifacts (id, request_id, request_hash, kind, created_at, updated_at, revision) '
                           'VALUES (?, ?, ?, ?, ?, ?, ?)',
                           (identifier, str(body.request_id), fingerprint, body.kind, stamp, stamp, 1))
        connection.execute(REVISION_INSERT, (identifier, 1, packed(payload['document']), payload['actor'], payload['note'], stamp))
        return artifact(connection, identifier)


@router.get('/artifacts')
def list_artifacts(kind: Literal['daily', 'weekly', 'page', 'diagram'] | None = None,
                   ticket_id: str | None = None, entity_id: str | None = None,
                   limit: int = Query(50, ge=1, le=MAX_LIMIT), offset: int = Query(0, ge=0),
                   connection: sqlite3.Connection = Depends(get_connection)):
    # Function-local: api.jyra imports api.artifact_links, which imports
    # this module. A ticket key (KA-12) resolves to the UUID links store.
    from api.jyra import require_ticket_reference

    ticket_id = require_ticket_reference(connection, ticket_id)
    require_reference(connection, 'entities', entity_id, 'entity')
    return artifact_rows(connection, kind=kind, ticket_id=ticket_id, entity_id=entity_id, limit=limit, offset=offset)


def artifact_rows(connection, *, kind=None, kinds=ALL_KINDS, ticket_id=None, entity_id=None, limit=50, offset=0):
    """One page of artifacts. `kinds` is internal (the brief UI lists briefs only)."""
    query = f'''SELECT a.id, a.kind, a.created_at, a.updated_at, a.revision,
        json_extract(r.payload, '$.title') AS title,
        (SELECT COUNT(*) FROM artifact_links l WHERE l.artifact_id = a.id) AS link_count FROM artifacts a
        JOIN artifact_revisions r ON r.artifact_id = a.id AND r.revision = a.revision
        WHERE a.kind IN ({', '.join('?' for _ in kinds)})'''
    params = tuple(kinds)
    if kind:
        query += ' AND a.kind = ?'
        params += (kind,)
    for target_type, target_id in (('ticket', ticket_id), ('entity', entity_id)):
        if target_id is not None:
            query += (' AND EXISTS (SELECT 1 FROM artifact_links l WHERE l.artifact_id = a.id'
                      ' AND l.target_type = ? AND l.target_id = ?)')
            params += (target_type, target_id)
    return paginate(connection, query + ' ORDER BY a.created_at DESC, a.id DESC', params, limit, offset)


@router.get('/artifacts/{artifact_id}')
def get_artifact(artifact_id: UUID, revision: int | None = Query(None, ge=1),
                 view: Literal['full', 'summary'] = 'full',
                 connection: sqlite3.Connection = Depends(get_connection)):
    value = artifact(connection, artifact_id, revision)
    if view == 'full':
        return value
    if value['kind'] != 'diagram':
        raise RequestValidationError([{'loc': ('query', 'view'), 'msg': 'summary is only available for diagram artifacts',
                                       'type': 'value_error'}])
    # The same envelope, with a planning summary in place of the document.
    return {**{k: v for k, v in value.items() if k != 'document'}, 'summary': diagram_summary(value['document'])}


@router.put('/artifacts/{artifact_id}')
def update_artifact(artifact_id: UUID, body: ArtifactUpdate, settings: Settings = Depends(get_settings), connection: sqlite3.Connection = Depends(get_connection)):
    """Replace the document as a new revision, validated against the stored kind."""
    def refuse(current):
        rules_for(current['kind'], ('body', 'document'))

    def compute(current):
        return screened(current['kind'], body.model_dump(mode='json'), settings, loc=('body', 'document'))

    return write_revision(connection, artifact_id, body.expected_revision, compute, refuse)


EDIT_MISS = ("Replacement {index}: 'old' matched {matches} times; it must match exactly once in the stored HTML "
             "returned by get_artifact (entities such as &quot; are escaped)")


def occurrences(text, old):
    """Overlapping matches count separately: 'aa' in 'aaa' is ambiguous."""
    count, start = 0, text.find(old)
    while start != -1:
        count += 1
        start = text.find(old, start + 1)
    return count


def edited_document(document, body):
    document = dict(document)
    if body.replacements:
        html = document.get('html')
        if not isinstance(html, str):
            raise RequestValidationError([{'loc': ('body', 'replacements'), 'msg': 'This artifact has no html to edit', 'type': 'value_error'}])
        for index, replacement in enumerate(body.replacements):
            matches = occurrences(html, replacement.old)
            if matches != 1:
                # Never echo `old`: it may hold exactly what the screen refuses.
                raise RequestValidationError([{'loc': ('body', 'replacements', index),
                                               'msg': EDIT_MISS.format(index=index, matches=matches), 'type': 'value_error'}])
            html = html.replace(replacement.old, replacement.new, 1)
        document['html'] = html
    for field in ('title', 'tags', 'provenance'):
        if field in body.model_fields_set:
            document[field] = body.model_dump(mode='json')[field]
    return document


@router.patch('/artifacts/{artifact_id}')
def edit_artifact(artifact_id: UUID, body: ArtifactEdit, settings: Settings = Depends(get_settings), connection: sqlite3.Connection = Depends(get_connection)):
    """Find-and-replace against revision `expected_revision`, one revision per call."""
    def refuse(current):
        if current['kind'] == 'diagram':
            raise ConflictError('edit_artifact edits pages and briefs; use edit_diagram for diagrams')
        rules_for(current['kind'], ('body', 'document'))

    def compute(current):
        # From expected_revision, not the current revision, so an exact
        # retry reproduces what was written.
        base = artifact(connection, artifact_id, body.expected_revision)
        document = edited_document(base['document'], body)
        return screened(current['kind'], {'document': document, 'actor': body.actor, 'note': body.note}, settings,
                        loc=('body', 'document'))

    return write_revision(connection, artifact_id, body.expected_revision, compute, refuse)


def raise_op_error(error: OpError):
    if error.status == 400:
        raise InvalidReferenceError(str(error))
    if error.status == 409:
        raise ConflictError(str(error))
    loc = ('body', 'document') if error.field == 'document' else ('body', 'operations', error.index)
    raise RequestValidationError([{'loc': loc, 'msg': str(error), 'type': 'value_error'}])


@router.patch('/artifacts/{artifact_id}/diagram')
def edit_diagram(artifact_id: UUID, body: EditDiagram, settings: Settings = Depends(get_settings),
                 connection: sqlite3.Connection = Depends(get_connection)):
    """An all-or-nothing batch of diagram operations, as one new revision.

    The batch applies to revision expected_revision, not the current one, so
    an exact retry (same operations, actor and note) reproduces what was
    written and returns that revision; any other stale batch is refused.
    """
    rules = KINDS['diagram']

    def refuse(current):
        if current['kind'] != 'diagram':
            raise ConflictError('edit_diagram only edits diagram artifacts; use edit_artifact or revise_artifact')

    def compute(current):
        base = artifact(connection, artifact_id, body.expected_revision)
        try:
            document = apply_ops(base['document'], body.operations)
        except OpError as error:
            raise_op_error(error)
        # The envelope through the generic screen, the document through the kind's own.
        envelope = screen_payload({'actor': body.actor, 'note': body.note}, settings, refusal=rules.refusal)
        return {**envelope, 'document': rules.screen_document(document, settings)}

    return write_revision(connection, artifact_id, body.expected_revision, compute, refuse)

"""Persistent discrepancies, separate from artifacts and the development board."""
import json
import sqlite3
from datetime import date, datetime
from typing import Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query

from api.artifacts import digest, now, packed
from api.auth import require_token
from api.briefing_content import screen_payload
from api.briefing_models import ReviewCreate, ReviewUpdate
from api.config import Settings, get_settings
from api.db import MAX_LIMIT, get_connection, paginate
from api.errors import ConflictError, NotFoundError, InvalidReferenceError
from fastapi.exceptions import RequestValidationError

router = APIRouter(dependencies=[Depends(require_token)])


def unpack(row):
    value = dict(row)
    value['content'] = json.loads(value.pop('payload'))
    value.pop('request_hash', None)
    return value


def item(connection, item_id):
    row = connection.execute('SELECT * FROM review_items WHERE id = ?', (str(item_id),)).fetchone()
    if row is None:
        raise NotFoundError('Review item not found')
    return unpack(row)


@router.post('/review-items')
def create_review_item(body: ReviewCreate, settings: Settings = Depends(get_settings), connection: sqlite3.Connection = Depends(get_connection)):
    payload = screen_payload(body.model_dump(mode='json'), settings)
    fingerprint = digest(payload)
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        existing = connection.execute('SELECT * FROM review_items WHERE source_key = ?', (str(body.source_key),)).fetchone()
        if existing:
            return {'created': False, 'item': unpack(existing), 'source_changed': existing['request_hash'] != fingerprint}
        identifier, stamp = str(uuid4()), now()
        content = packed(payload['content'])
        connection.execute('INSERT INTO review_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                           (identifier, str(body.source_key), fingerprint, content, 'open', None, 1, stamp, stamp))
        connection.execute('INSERT INTO review_item_history VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                           (identifier, 1, content, 'open', None, payload['actor'], payload['note'], stamp))
        return {'created': True, 'item': item(connection, identifier), 'source_changed': False}


@router.get('/review-items')
def list_review_items(state: Literal['open', 'deferred', 'resolved', 'dismissed'] | None = None,
                      actionable: bool = False, source_key: UUID | None = None,
                      limit: int = Query(50, ge=1, le=MAX_LIMIT), offset: int = Query(0, ge=0),
                      connection: sqlite3.Connection = Depends(get_connection)):
    query, params = 'SELECT * FROM review_items WHERE 1=1', []
    if state:
        query += ' AND state = ?'
        params.append(state)
    if source_key:
        query += ' AND source_key = ?'
        params.append(str(source_key))
    if actionable:
        query += " AND (state = 'open' OR (state = 'deferred' AND revisit_date <= ?))"
        params.append(datetime.now(ZoneInfo('America/New_York')).date().isoformat())
    result = paginate(connection, query + ' ORDER BY created_at, id', tuple(params), limit, offset)
    result['items'] = [unpack(r) for r in result['items']]
    return result


@router.get('/review-items/{item_id}')
def get_review_item(item_id: UUID, connection: sqlite3.Connection = Depends(get_connection)):
    return item(connection, item_id)


@router.put('/review-items/{item_id}')
def update_review_item(item_id: UUID, body: ReviewUpdate, settings: Settings = Depends(get_settings), connection: sqlite3.Connection = Depends(get_connection)):
    payload = screen_payload(body.model_dump(mode='json'), settings)
    content = packed(payload['content'])
    target = (content, body.state, payload['revisit_date'], payload['actor'], payload['note'])
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        current = item(connection, item_id)
        prior = connection.execute('SELECT * FROM review_item_history WHERE item_id = ? AND revision = ?', (str(item_id), body.expected_revision + 1)).fetchone()
        if prior and tuple(prior[k] for k in ('payload', 'state', 'revisit_date', 'actor', 'note')) == target:
            return {'item': current, 'applied_revision': body.expected_revision + 1, 'replayed': True}
        if current['revision'] != body.expected_revision:
            raise ConflictError('Review item changed; reread before updating')
        if body.revisit_date and body.revisit_date <= datetime.now(ZoneInfo('America/New_York')).date():
            raise RequestValidationError([{'loc': ('body', 'revisit_date'), 'msg': 'Deferral must be after today in America/New_York', 'type': 'value_error'}])
        revision, stamp = body.expected_revision + 1, now()
        connection.execute('INSERT INTO review_item_history VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                           (str(item_id), revision, content, body.state, payload['revisit_date'], payload['actor'], payload['note'], stamp))
        connection.execute('UPDATE review_items SET payload = ?, state = ?, revisit_date = ?, revision = ?, updated_at = ? WHERE id = ?',
                           (content, body.state, payload['revisit_date'], revision, stamp, str(item_id)))
        return {'item': item(connection, item_id), 'applied_revision': revision, 'replayed': False}


@router.get('/review-history')
def list_review_history(item_id: UUID | None = None, date_from: date | None = None, date_to: date | None = None,
                        limit: int = Query(50, ge=1, le=MAX_LIMIT), offset: int = Query(0, ge=0),
                        connection: sqlite3.Connection = Depends(get_connection)):
    from api.briefing_data import utc_bounds
    query, params = 'SELECT * FROM review_item_history WHERE 1=1', []
    if item_id:
        if connection.execute('SELECT 1 FROM review_items WHERE id = ?', (str(item_id),)).fetchone() is None:
            raise InvalidReferenceError(f'No review item with id {item_id}')
        query += ' AND item_id = ?'
        params.append(str(item_id))
    if date_from is not None or date_to is not None:
        lower, upper = utc_bounds(date_from, date_to)
        query += ' AND julianday(created_at) >= julianday(?) AND julianday(created_at) < julianday(?)'
        params += [lower, upper]
    result = paginate(connection, query + ' ORDER BY created_at, item_id, revision', tuple(params), limit, offset)
    result['items'] = [unpack(r) for r in result['items']]
    return result

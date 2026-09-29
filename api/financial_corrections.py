"""Explicit metadata repairs; immutable transaction facts stay immutable."""

import json
import sqlite3
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from api.auth import require_token
from api.db import get_connection
from api.errors import ConflictError, NotFoundError
from api.models import KeyDate, _check_period
from api.privacy import StoredText

router = APIRouter(dependencies=[Depends(require_token)])


class CorrectionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    correction_id: UUID
    actor: StoredText = Field(min_length=1, max_length=200)
    reason: StoredText = Field(min_length=1, max_length=2000)


class CorrectPeriodRequest(CorrectionRequest):
    expected_period_start: KeyDate
    expected_period_end: KeyDate
    period_start: KeyDate
    period_end: KeyDate

    def model_post_init(self, __context):
        _check_period(self.expected_period_start, self.expected_period_end)
        _check_period(self.period_start, self.period_end)


class ReassignStatementRequest(CorrectionRequest):
    expected_statement_id: str = Field(min_length=1)
    statement_id: str = Field(min_length=1)


def _audit(row, *, replayed=False):
    return {'correction_id': row['id'], 'operation': row['operation'], 'target_id': row['target_id'],
            'actor': row['actor'], 'reason': row['reason'], 'created_at': row['created_at'],
            'before': json.loads(row['before_json']), 'after': json.loads(row['after_json']),
            'replayed': replayed}


def _apply(connection, operation, target_id, body):
    request = json.dumps({'operation': operation, 'target_id': target_id,
                          'body': body.model_dump(mode='json')}, sort_keys=True)
    correction_id = str(body.correction_id)
    # Serialize precondition checks, mutation and audit in one transaction.
    connection.execute('BEGIN IMMEDIATE')
    try:
        previous = connection.execute('SELECT * FROM financial_corrections WHERE id = ?', (correction_id,)).fetchone()
        if previous:
            if previous['request_json'] != request:
                raise ConflictError('Correction ID already used for a different request')
            connection.commit()
            return _audit(previous, replayed=True)
        if operation == 'statement_period':
            row = connection.execute('SELECT * FROM active_statements WHERE id = ?', (target_id,)).fetchone()
            if not row:
                raise NotFoundError('Active statement not found')
            before = {k: row[k] for k in ('period_start', 'period_end')}
            expected = {'period_start': body.expected_period_start.isoformat(), 'period_end': body.expected_period_end.isoformat()}
            after = {'period_start': body.period_start.isoformat(), 'period_end': body.period_end.isoformat()}
            if before != expected:
                raise ConflictError('Statement period changed since inspection; nothing corrected')
            if before == after:
                raise ConflictError('Period already has requested values; nothing corrected')
            connection.execute('UPDATE statements SET period_start = ?, period_end = ? WHERE id = ?',
                               (after['period_start'], after['period_end'], target_id))
        else:
            row = connection.execute('SELECT * FROM active_transactions WHERE id = ?', (target_id,)).fetchone()
            if not row:
                raise NotFoundError('Active transaction not found')
            before = {'statement_id': row['statement_id']}
            after = {'statement_id': body.statement_id}
            if row['statement_id'] != body.expected_statement_id:
                raise ConflictError('Transaction statement changed since inspection; nothing corrected')
            if before == after:
                raise ConflictError('Transaction already belongs to requested statement')
            for statement_id in (row['statement_id'], body.statement_id):
                statement = connection.execute('SELECT * FROM active_statements WHERE id = ?', (statement_id,)).fetchone()
                if not statement:
                    raise NotFoundError('Active source or destination statement not found')
                if statement['account_id'] != row['account_id']:
                    raise ConflictError('Statement reassignment cannot cross accounts')
            # Source table membership can differ from transaction-date ranges.
            # Do not rewrite dates or enforce date bucketing here.
            connection.execute('UPDATE transactions SET statement_id = ? WHERE id = ?', (body.statement_id, target_id))
        connection.execute(
            'INSERT INTO financial_corrections (id, operation, target_id, actor, reason, request_json, before_json, after_json) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (correction_id, operation, target_id, body.actor, body.reason, request,
             json.dumps(before, sort_keys=True), json.dumps(after, sort_keys=True)))
        record = connection.execute('SELECT * FROM financial_corrections WHERE id = ?', (correction_id,)).fetchone()
        connection.commit()
        return _audit(record)
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        raise ConflictError('Correction conflicts with existing data; nothing changed') from exc
    except Exception:
        connection.rollback()
        raise


@router.post('/statements/{statement_id}/correct-period')
def correct_statement_period(statement_id: str, body: CorrectPeriodRequest,
                             connection: sqlite3.Connection = Depends(get_connection)):
    return _apply(connection, 'statement_period', statement_id, body)


@router.post('/transactions/{transaction_id}/reassign-statement')
def reassign_transaction_statement(transaction_id: str, body: ReassignStatementRequest,
                                   connection: sqlite3.Connection = Depends(get_connection)):
    return _apply(connection, 'transaction_statement', transaction_id, body)


@router.get('/financial-corrections/{correction_id}')
def get_financial_correction(correction_id: UUID, connection: sqlite3.Connection = Depends(get_connection)):
    row = connection.execute('SELECT * FROM financial_corrections WHERE id = ?', (str(correction_id),)).fetchone()
    if not row:
        raise NotFoundError('Financial correction not found')
    return _audit(row)

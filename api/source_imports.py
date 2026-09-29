"""Explicit source occurrence identity and conservative cross-source review.

Caller-supplied source hashes/physical locators are attestations, not bank IDs.
No fuzzy matching, source-file reads, or historical transaction rewrites.

Candidates share account and signed cents and fall within NEAR_DATE_DAYS of the
source date: two exports of one purchase can post it a day or two apart
(statement date vs effective date, month-end vs the 1st). A candidate is never
proof: every candidate row needs an explicit new/link decision and reason.
"""
import hashlib
import json
import sqlite3
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends
from pydantic import Field, model_validator

from api.auth import require_token
from api.db import get_connection
from api.errors import ConflictError
from api import financial
from api.financial import _require_account_entity
from api.models import ImportStatementRequest, TransactionInput
from api.privacy import StoredText
from api.transaction_identity import comparison_description

router = APIRouter(dependencies=[Depends(require_token)])

# A source locator is a structured transport identifier, not free-form text.
# Current PDF locators deliberately contain a 64-character content hash, which
# can have account-shaped digit runs by chance. Validate the structure instead
# of applying the free-text digit guard; the bounded legacy form keeps older
# receipts usable without accepting an arbitrary card/account number.
SOURCE_ROW_PATTERN = (
    r'^(?:csv:v1:record:[1-9][0-9]{0,5}|'
    r'(?:text-v1|canon-v1):[0-9a-f]{64}:[0-9]{1,8}:[0-9]{1,8}|'
    r'[A-Za-z][A-Za-z0-9._/-]{0,31}:(?:record:)?(?:0|[1-9][0-9]{0,5}))$'
)


class SourceRow(TransactionInput):
    source_row: str = Field(pattern=SOURCE_ROW_PATTERN)
    decision: Literal['new', 'link'] | None = None
    transaction_id: str | None = None
    reason: StoredText | None = Field(default=None, max_length=2000)

    @model_validator(mode='after')
    def decision_shape(self):
        if self.decision and not (self.reason and self.reason.strip()):
            raise ValueError('Explicit decisions require a review reason')
        if (self.decision == 'link') != bool(self.transaction_id):
            raise ValueError('Only a link decision names an existing transaction')
        return self


class SourceImport(ImportStatementRequest):
    source_id: str = Field(pattern=r'^[0-9a-f]{64}$')
    actor: StoredText = Field(min_length=1, max_length=200)
    transactions: list[SourceRow] = Field(min_length=1, max_length=2000)
    review_token: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def unique_rows(self):
        keys = [r.source_row for r in self.transactions]
        if len(set(keys)) != len(keys):
            raise ValueError('Source row locators must be unique within the source')
        if not self.actor.strip():
            raise ValueError('Actor must not be blank')
        return self


# Two exports of one charge can disagree on its posting date by a day or two.
# The window is deliberately small and fixed: it widens review, never decides.
NEAR_DATE_DAYS = 3  # messages below say "3 days" literally; keep them in step


def _candidates(db, account_id, row):
    """Active same-account, same-cents rows within the window; exact dates first."""
    day = row.txn_date.isoformat()
    found = [dict(r) for r in db.execute(
        'SELECT *, CAST(ABS(julianday(txn_date) - julianday(?)) AS INTEGER) AS days_apart '
        'FROM active_transactions WHERE account_id = ? AND amount_cents = ? '
        "AND txn_date BETWEEN date(?, ?) AND date(?, ?) ORDER BY days_apart, id",
        (day, account_id, row.amount_cents, day, f'-{NEAR_DATE_DAYS} days', day, f'+{NEAR_DATE_DAYS} days'))]
    return found


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _preview(db, body):
    from api.telemetry import tracer

    with tracer.start_as_current_span("intake.preview") as span:
        result = _preview_untraced(db, body)
        span.set_attributes({
            "q_core.rows": len(result["rows"]),
            "q_core.needs_review": result["needs_review"],
            "q_core.source_conflicts": result["source_conflicts"],
        })
        return result


def _preview_untraced(db, body):
    _require_account_entity(db, body.account_id)
    results = []
    # Include rule state in the token: a stale preview must not classify new
    # rows differently from the rule set it inspected.
    rules = [dict(r) for r in db.execute('SELECT * FROM merchant_rules ORDER BY id')]
    statement = db.execute('SELECT * FROM active_statements WHERE account_id = ? AND period_start = ? AND period_end = ?',
                           (body.account_id, body.period_start.isoformat(), body.period_end.isoformat())).fetchone()
    for row in body.transactions:
        receipt = db.execute('SELECT * FROM source_import_rows WHERE account_id = ? AND source_id = ? AND source_row = ?',
                             (body.account_id, body.source_id, row.source_row)).fetchone()
        candidates = _candidates(db, body.account_id, row)
        match = None if not candidates else ('exact' if candidates[0]['days_apart'] == 0 else 'near_date')
        result = {'source_row': row.source_row, 'status': 'new' if not candidates else 'needs_review',
                  'match': match, 'candidates': candidates, 'receipt': dict(receipt) if receipt else None}
        if receipt:
            # A link to a near-date candidate stays recognisable: its target is
            # within the window, so it is among this row's candidates.
            target = next((r for r in candidates if r['id'] == receipt['transaction_id']), None)
            original = json.loads(receipt['input_json'])
            active_parent = db.execute('SELECT id FROM active_statements WHERE id = ?', (target['statement_id'],)).fetchone() if target else None
            result['status'] = 'known_source' if target and active_parent and original['txn_date'] == row.txn_date.isoformat() and original['amount_cents'] == row.amount_cents else 'source_conflict'
        result['matching_description_ids'] = [r['id'] for r in candidates
            if comparison_description(r['description']) == comparison_description(row.description)]
        results.append(result)
    # Decisions are deliberately excluded: review returns a token before the
    # operator supplies new/link resolutions. All input facts remain bound.
    facts = body.model_dump(mode='json', exclude={'review_token'})
    facts['transactions'] = [{k:v for k,v in r.items() if k not in ('decision','transaction_id','reason')} for r in facts['transactions']]
    token = _hash({'input':facts, 'rows':results, 'statement':dict(statement) if statement else None, 'rules':rules})
    return {'review_token':token, 'rows':results, 'needs_review':sum(r['status']=='needs_review' for r in results),
            'source_conflicts':sum(r['status']=='source_conflict' for r in results),
            'interpretation':'Same account and cents within 3 days identifies candidates, not duplicates; match is exact (same date) or near_date. Matching descriptions also require review across unlinked sources. Source locators must come from the source, not extraction output order.'}


@router.post('/source_imports/preview')
def preview_source_import(body: SourceImport, connection: sqlite3.Connection = Depends(get_connection)):
    connection.execute('BEGIN')
    try:
        result = _preview(connection, body)
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise


@router.post('/source_imports/commit')
def commit_source_import(body: SourceImport, connection: sqlite3.Connection = Depends(get_connection)):
    from api.telemetry import tracer

    with tracer.start_as_current_span("intake.commit") as span:
        result = _commit(body, connection)
        span.set_attributes({"q_core.created": result["created"], "q_core.linked": result["linked"],
                             "q_core.recognized": result["recognized"]})
        return result


def _commit(body: SourceImport, connection: sqlite3.Connection):
    connection.execute('BEGIN IMMEDIATE')
    try:
        preview = _preview(connection, body)
        if not body.review_token or body.review_token != preview['review_token']:
            raise ConflictError('Missing or stale review token; preview again before importing')
        actions = []
        linked_ids = set()
        for row, review in zip(body.transactions, preview['rows']):
            status = review['status']
            if status == 'source_conflict':
                raise ConflictError('Source occurrence changed facts or its target is archived; nothing imported')
            if status == 'known_source':
                target_id = review['receipt']['transaction_id']
                if row.transaction_id and row.transaction_id != target_id:
                    raise ConflictError('Source occurrence already linked to another transaction')
                action = 'known_source'
            elif row.decision == 'link':
                candidates = {r['id']:r for r in review['candidates']}
                if row.transaction_id not in candidates:
                    raise ConflictError('Link target must be an active same-account, same-cents candidate within 3 days')
                target_id = row.transaction_id
                if not connection.execute('SELECT id FROM active_statements WHERE id = ?', (candidates[target_id]['statement_id'],)).fetchone():
                    raise ConflictError('Link target statement is archived')
                action = 'link'
            elif status == 'needs_review' and row.decision != 'new':
                raise ConflictError('Ambiguous overlap requires an explicit new/link decision and reason; nothing imported')
            else:
                target_id = str(uuid4())
                action = 'new'
            if target_id in linked_ids:
                raise ConflictError('Distinct source occurrences cannot link to the same transaction')
            linked_ids.add(target_id)
            actions.append((row, action, target_id))
        statement_id = None
        if any(action=='new' for _,action,_ in actions):
            statement = connection.execute('SELECT id FROM active_statements WHERE account_id = ? AND period_start = ? AND period_end = ?',
                (body.account_id, body.period_start.isoformat(), body.period_end.isoformat())).fetchone()
            statement_id = statement['id'] if statement else str(uuid4())
            if not statement:
                connection.execute('INSERT INTO statements (id,account_id,period_start,period_end) VALUES (?,?,?,?)',
                    (statement_id, body.account_id, body.period_start.isoformat(), body.period_end.isoformat()))
        output = []
        for row, action, target_id in actions:
            if action == 'new':
                category, entity = financial._classify(connection, row.description)
                connection.execute('INSERT INTO transactions (id,statement_id,account_id,txn_date,description,amount_cents,category_id,entity_id) VALUES (?,?,?,?,?,?,?,?)',
                    (target_id,statement_id,body.account_id,row.txn_date.isoformat(),row.description,row.amount_cents,category,entity))
            if action != 'known_source':
                connection.execute('INSERT INTO source_import_rows (account_id,source_id,source_row,transaction_id,input_json,decision,actor,reason) VALUES (?,?,?,?,?,?,?,?)',
                    (body.account_id,body.source_id,row.source_row,target_id,json.dumps(row.model_dump(mode='json'),sort_keys=True),action,body.actor,
                     row.reason or 'No existing same-account, same-cents candidate within 3 days at commit'))
            output.append({'source_row':row.source_row,'transaction_id':target_id,'action':action})
        connection.commit()
        return {'rows':output,'created':sum(r['action']=='new' for r in output),'linked':sum(r['action']=='link' for r in output),
                'recognized':sum(r['action']=='known_source' for r in output)}
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        raise ConflictError('Source identity or statement conflict; nothing imported') from exc
    except Exception:
        connection.rollback()
        raise

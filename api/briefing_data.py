"""Bounded read surfaces missing from the existing briefing source tools."""
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from pydantic import AwareDatetime

from api.auth import require_token
from api.config import Settings, get_settings
from api.db import MAX_LIMIT, get_connection, paginate
from fastapi.exceptions import RequestValidationError
from api.forecast import read_plan
from api.forecast_model import Plan, occurrences

router = APIRouter(dependencies=[Depends(require_token)])
LOCAL_ZONE = ZoneInfo('America/New_York')


def utc_bounds(date_from, date_to):
    if date_from is None or date_to is None or date_to < date_from or (date_to-date_from).days > 31:
        raise RequestValidationError([{'loc': ('query', 'date_from'), 'msg': 'Supply ordered date_from/date_to within 32 calendar dates', 'type': 'value_error'}])
    start = datetime.combine(date_from, time.min, LOCAL_ZONE)
    end = datetime.combine(date_to + timedelta(days=1), time.min, LOCAL_ZONE)
    return start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()


@router.get('/briefing-window')
def briefing_window(kind: Literal['daily', 'weekly'] = 'daily'):
    stamp = datetime.now(LOCAL_ZONE)
    start = stamp.replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == 'weekly':
        start -= timedelta(days=start.weekday())
    outlook = stamp.date() + timedelta(days=7 if kind == 'daily' else 6-stamp.weekday())
    return {'kind': kind, 'timezone': 'America/New_York', 'period_start': start.isoformat(),
            'as_of': stamp.isoformat(), 'date_from': start.date().isoformat(),
            'date_to': stamp.date().isoformat(), 'outlook_to': outlook.isoformat(),
            'date_precision': 'Transactions and expected occurrences are date-only, not verified posting times.'}


@router.get('/reminder-completions')
def list_reminder_completions(date_from: date, date_to: date, as_of: AwareDatetime | None = None,
                              limit: int = Query(50, ge=1, le=MAX_LIMIT), offset: int = Query(0, ge=0),
                              connection: sqlite3.Connection = Depends(get_connection)):
    lower, upper = utc_bounds(date_from, date_to)
    query = '''SELECT i.id, i.reminder_id, i.due_date, i.completed_at, r.title, r.entity_id
        FROM reminder_instances i JOIN reminders r ON r.id = i.reminder_id
        WHERE i.status = 'done' AND julianday(i.completed_at) >= julianday(?)
        AND julianday(i.completed_at) < julianday(?)'''
    params = [lower, upper]
    if as_of:
        query += ' AND julianday(i.completed_at) <= julianday(?)'
        params.append(as_of.isoformat())
    result = paginate(connection, query + ' ORDER BY i.completed_at, i.id', tuple(params), limit, offset)
    result['coverage'] = 'Current recorded completions only; prior overrides/deleted reminders are not an event history.'
    return result


@router.get('/expected-payments')
def list_expected_payments(date_from: date, date_to: date,
                           limit: int = Query(50, ge=1, le=MAX_LIMIT), offset: int = Query(0, ge=0),
                           settings: Settings = Depends(get_settings)):
    utc_bounds(date_from, date_to)
    saved = read_plan(settings)
    plan = Plan.model_validate(saved['plan'])
    accounts = {a.key: a for a in plan.accounts}
    events = []
    for rule in plan.rules:
        for due, amount in occurrences(rule, date_from, date_to):
            events.append({'date': due.isoformat(), 'rule': rule.key, 'label': rule.label,
                           'kind': rule.kind, 'account': rule.account, 'destination': rule.destination,
                           'amount_cents': amount, 'confidence': rule.confidence, 'evidence': rule.evidence,
                           'account_kind': accounts[rule.account].kind,
                           'cadence': rule.cadence, 'basis': 'current_plan_expectation'})
    events.sort(key=lambda e: (e['date'], e['rule']))
    return {'items': events[offset:offset+limit], 'total': len(events), 'limit': limit, 'offset': offset,
            'plan_revision': saved['revision'], 'gaps': plan.gaps,
            'snapshots': [a.model_dump(mode='json') for a in plan.accounts],
            'coverage': 'Expected dates from the current plan, including past dates; not historical plan versions or proof of payment. Daily budgets are allocated estimates.'}


@router.get('/reminder-calendar-links')
def list_reminder_calendar_links(limit: int = Query(50, ge=1, le=MAX_LIMIT), offset: int = Query(0, ge=0),
                                 connection: sqlite3.Connection = Depends(get_connection)):
    return paginate(connection, '''SELECT e.reminder_id, e.event_id, c.calendar_id
        FROM google_calendar_events e CROSS JOIN google_calendar_connection c
        ORDER BY e.reminder_id''', (), limit, offset)

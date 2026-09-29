"""Deterministic scenario accounting. No database, network or live ledger writes."""
import calendar
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from api.privacy import StoredText


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Account(Model):
    key: str = Field(pattern=r'^[a-z][a-z0-9_-]{0,39}$')
    label: StoredText = Field(min_length=1, max_length=100)
    kind: Literal['cash', 'debt']
    balance_cents: StrictInt | None = None
    as_of: date
    evidence: StoredText = Field(min_length=1, max_length=500)


class Rule(Model):
    key: str = Field(pattern=r'^[a-z][a-z0-9_-]{0,39}$')
    label: StoredText = Field(min_length=1, max_length=100)
    kind: Literal['income', 'expense', 'transfer', 'debt_payment']
    account: str
    destination: str | None = None
    amount_cents: StrictInt | None = Field(default=None, ge=0, le=100000000)
    cadence: Literal['monthly', 'interval', 'once', 'daily_budget'] = 'monthly'
    days: list[StrictInt] = Field(default_factory=list, max_length=31)
    interval_days: StrictInt = Field(default=14, ge=1, le=366)
    start: date
    end: date | None = None
    weekend: Literal['none', 'previous', 'next'] = 'none'
    confidence: Literal['confirmed', 'estimated', 'unknown']
    property_label: StoredText = Field(default='Other', max_length=100)
    evidence: StoredText = Field(min_length=1, max_length=700)

    @model_validator(mode='after')
    def schedule(self):
        if self.end and self.end < self.start:
            raise ValueError('Schedule end precedes start')
        if self.cadence == 'monthly' and (not self.days or len(set(self.days)) != len(self.days)
                                         or any(d < 1 or d > 31 for d in self.days)):
            raise ValueError('Monthly rules require unique days 1..31; 31 means month end')
        if self.amount_cents is None and self.confidence != 'unknown':
            raise ValueError('Missing amount must be marked unknown')
        return self


class Plan(Model):
    schema_version: Literal[1] = 1
    accounts: list[Account] = Field(min_length=1, max_length=20)
    rules: list[Rule] = Field(default_factory=list, max_length=150)
    runway_account: str
    snowball_rule: str | None = None
    runway_growth_target_cents: StrictInt = Field(default=200000, ge=0, le=100000000)
    gaps: list[StoredText] = Field(default_factory=list, max_length=40)

    @model_validator(mode='after')
    def links(self):
        accounts = {a.key: a for a in self.accounts}
        if len(accounts) != len(self.accounts) or len({r.key for r in self.rules}) != len(self.rules):
            raise ValueError('Account and rule keys must be unique')
        cash = [a for a in self.accounts if a.kind == 'cash']
        if not cash or len({a.as_of for a in cash}) != 1 or any(a.balance_cents is None for a in cash):
            raise ValueError('Cash snapshots require balances on one common date')
        if self.runway_account not in accounts or accounts[self.runway_account].kind != 'cash':
            raise ValueError('Runway must name a cash account')
        if any(len(g) > 700 for g in self.gaps):
            raise ValueError('Gap note too long')
        for r in self.rules:
            if r.account not in accounts:
                raise ValueError('Unknown schedule account')
            if r.kind in ('transfer', 'debt_payment'):
                if r.destination not in accounts or r.destination == r.account or accounts[r.account].kind != 'cash':
                    raise ValueError('Invalid transfer source or destination')
                expected = 'cash' if r.kind == 'transfer' else 'debt'
                if accounts[r.destination].kind != expected:
                    raise ValueError('Transfer destination has wrong account kind')
            elif r.destination is not None:
                raise ValueError('Only transfers and debt payments have destinations')
        if self.snowball_rule is not None and not any(r.key == self.snowball_rule and r.kind == 'debt_payment' for r in self.rules):
            raise ValueError('Snowball rule must identify a debt payment')
        return self


def monthly_cents(rule):
    if rule.amount_cents is None or rule.cadence == 'once':
        return None
    factor = Decimal(365) / rule.interval_days / 12 if rule.cadence == 'interval' else Decimal(len(rule.days) if rule.cadence == 'monthly' else 1)
    return int((rule.amount_cents * factor).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def occurrences(rule, lower, upper):
    """Nominal day bounds first, then weekend shift; include shifted boundaries."""
    cursor = lower - timedelta(days=3)
    while cursor <= upper + timedelta(days=3):
        last = calendar.monthrange(cursor.year, cursor.month)[1]
        due = (rule.cadence == 'daily_budget' or
               rule.cadence == 'once' and cursor == rule.start or
               rule.cadence == 'interval' and (cursor-rule.start).days % rule.interval_days == 0 or
               rule.cadence == 'monthly' and cursor.day in {min(d, last) for d in rule.days})
        if due and cursor >= rule.start and (rule.end is None or cursor <= rule.end):
            effective = cursor
            if rule.cadence != 'daily_budget' and rule.weekend != 'none':
                while effective.weekday() > 4:
                    effective += timedelta(days=1 if rule.weekend == 'next' else -1)
            if lower <= effective <= upper:
                amount = rule.amount_cents
                if amount is not None and rule.cadence == 'daily_budget':
                    base, remainder = divmod(amount, last)
                    amount = base + int(cursor.day <= remainder)
                yield effective, amount
        cursor += timedelta(days=1)


def forecast(plan, until, today, extra_payment_cents=0, extra_payment_date=None, snowball_monthly_cents=None):
    plan = plan.model_copy(deep=True)
    accounts = {a.key: a for a in plan.accounts}
    anchor = next(a.as_of for a in plan.accounts if a.kind == 'cash')
    if not anchor < until <= anchor + timedelta(days=730):
        raise ValueError('End must be after cash snapshot and within 730 days')
    if extra_payment_cents < 0 or extra_payment_cents > 100000000:
        raise ValueError('Invalid extra payment')
    if extra_payment_cents or snowball_monthly_cents is not None:
        rule = next((r for r in plan.rules if r.key == plan.snowball_rule), None)
        if rule is None:
            raise ValueError('Configure a snowball debt payment first')
        if snowball_monthly_cents is not None:
            if not 0 <= snowball_monthly_cents <= 100000000 or rule.cadence != 'monthly' or len(rule.days) != 1:
                raise ValueError('Allowance requires one monthly payment')
            rule.amount_cents = snowball_monthly_cents
            rule.confidence = 'estimated'
        if extra_payment_cents:
            if extra_payment_date is None or not max(anchor, today-timedelta(days=1)) < extra_payment_date <= until:
                raise ValueError('Extra payment date must be within the future forecast')
            plan.rules.append(rule.model_copy(update={'key':'scenario-extra', 'label':'Scenario: extra debt payment',
                              'amount_cents':extra_payment_cents, 'cadence':'once', 'start':extra_payment_date,
                              'end':None, 'weekend':'none', 'confidence':'estimated'}))
    events = []
    unknown = []
    for rule in plan.rules:
        if rule.amount_cents is None:
            unknown.append(rule.label)
            continue
        for due, amount in occurrences(rule, anchor+timedelta(days=1), until):
            events.append({'date':due.isoformat(), 'rule':rule.key, 'label':rule.label, 'kind':rule.kind,
                           'account':rule.account, 'destination':rule.destination, 'amount_cents':amount,
                           'confidence':rule.confidence, 'property_label':rule.property_label,
                           'cadence':rule.cadence})
    # Conservative within-day order: obligations before receipts; no overdraft guarantee.
    events.sort(key=lambda e:(e['date'], e['kind']=='income', e['rule']))
    balances = {a.key: a.balance_cents if a.as_of <= anchor else None for a in plan.accounts}
    daily, months = [], {}
    cursor = anchor
    index = 0
    while cursor <= until:
        opening = dict(balances)
        for a in plan.accounts:
            if cursor == a.as_of:
                balances[a.key] = a.balance_cents
        month = cursor.strftime('%Y-%m')
        if month not in months:
            months[month] = {'month':month,'income_cents':0,'running_costs_cents':0,'debt_payments_cents':0,
                             'runway_opening_cents':balances[plan.runway_account], 'properties':{},
                             'complete_month':cursor > anchor and cursor.day == 1 and date(cursor.year,cursor.month,calendar.monthrange(cursor.year,cursor.month)[1]) <= until}
        m = months[month]
        low = dict(balances)
        while index < len(events) and events[index]['date'] == cursor.isoformat():
            event = events[index]; index += 1
            amount = event['amount_cents']; a = accounts[event['account']]
            def change(account, delta):
                if cursor > account.as_of and balances[account.key] is not None:
                    balances[account.key] += delta
            change(a, (amount if event['kind']=='income' else -amount) * (1 if a.kind=='cash' else -1))
            if event['destination']:
                destination = accounts[event['destination']]
                change(destination, amount if destination.kind=='cash' else -amount)
            for key, value in balances.items():
                if value is not None:
                    low[key] = min(low[key],value) if low[key] is not None else value
            field = {'income':'income_cents','expense':'running_costs_cents','debt_payment':'debt_payments_cents'}.get(event['kind'])
            if field:
                m[field] += amount
            if event['kind'] in ('income','expense'):
                prop = m['properties'].setdefault(event['property_label'], {'income_cents':0,'cost_cents':0})
                prop['income_cents' if event['kind']=='income' else 'cost_cents'] += amount
        m['runway_closing_cents'] = balances[plan.runway_account]
        m['runway_growth_cents'] = m['runway_closing_cents']-m['runway_opening_cents']
        daily.append({'date':cursor.isoformat(),'balances':dict(balances),'cash_total_cents':sum(balances[a.key] for a in plan.accounts if a.kind=='cash'), 'low_balances':low})
        cursor += timedelta(days=1)
    future = [d for d in daily if d['date'] >= today.isoformat()] or daily
    lows = {}
    for a in plan.accounts:
        if a.kind=='cash':
            point = min(future,key=lambda d:d['low_balances'][a.key])
            lows[a.key] = {'date':point['date'],'amount_cents':point['low_balances'][a.key]}
    return {'anchor':anchor.isoformat(),'today':today.isoformat(),'until':until.isoformat(),
            'snapshot_age_days':max(0,(today-anchor).days),'accounts':[a.model_dump(mode='json') for a in plan.accounts],
            'rules':[dict(r.model_dump(mode='json'),monthly_equivalent_cents=monthly_cents(r)) for r in plan.rules],
            'daily':daily,'events':events,'months':list(months.values()),'lowest_projected_cash':lows,
            'unknown_payments':unknown,'gaps':plan.gaps,'runway_account':plan.runway_account,
            'runway_growth_target_cents':plan.runway_growth_target_cents,
            'warning':'Scenario only: no automatic reconciliation with later transactions. Card purchases increase debt, not cash outflow; payments reduce cash, not running costs. Unknown payments and interest can overstate remaining cash. Weekend shifts exclude holidays. Mortgage costs include principal, not just economic expense.'}

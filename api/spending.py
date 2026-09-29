"""Read-only public reporting surface for the local spending page.

These routes are deliberately kept in their own router so the only API
endpoints that bypass the bearer token are easy to audit.  They are safe to
serve without a token because the API binds only to loopback and neither route
can change data.
"""

import re
import sqlite3
from collections import defaultdict
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query

from api.auth import require_reader
from api.db import get_connection, require_reference
from api.financial import statement_coverage

router = APIRouter()

SPENDING_DEFINITION = (
    "Spending is negative active transactions grouped by their top-level "
    "category. Transfers and income are excluded; uncategorized debits are "
    "included. Amounts are integer cents."
)

COVERAGE_DEFINITION = (
    "Reporting accounts are active accounts with an active statement that "
    "overlaps the selected range. A period is complete when the combined "
    "statement coverage for every reporting account spans every day in it."
)

_PREFIX_RE = re.compile(r"^(?:TST-|SQ \*|LS |PAYPAL \*|WL \*)", re.I)
_MERCHANT_TAIL_RE = re.compile(r"(?:#|\d{3,}).*$")


def _merchant_name(description: str) -> str:
    """A useful label, not a merchant registry identity."""
    name = _PREFIX_RE.sub("", description).strip()
    name = _MERCHANT_TAIL_RE.sub("", name).strip()
    return name or description.strip()


def _periods(start: date, end: date, granularity: str) -> list[tuple[date, date]]:
    if granularity == "month":
        cursor = start.replace(day=1)
        result = []
        while cursor <= end:
            next_month = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
            result.append((max(cursor, start), min(next_month - timedelta(days=1), end)))
            cursor = next_month
        return result
    cursor = start - timedelta(days=start.weekday())
    result = []
    while cursor <= end:
        result.append((max(cursor, start), min(cursor + timedelta(days=6), end)))
        cursor += timedelta(days=7)
    return result


def _coverage(connection: sqlite3.Connection, start: date, end: date, account_id: str | None) -> dict[str, list[tuple[date, date]]]:
    account_clause = " AND e.id = ?" if account_id else ""
    query = (
        "WITH reporting_accounts AS ("
        " SELECT DISTINCT e.id, e.name FROM entities e "
        " JOIN active_statements qualifying ON qualifying.account_id = e.id "
        " WHERE e.type = 'account' AND e.status = 'active'" + account_clause + " "
        " AND qualifying.period_start <= ? AND qualifying.period_end >= ?"
        ") "
        "SELECT e.id AS account_id, e.name, s.period_start, s.period_end "
        "FROM reporting_accounts e JOIN active_statements s ON s.account_id = e.id"
    )
    params: list[str] = ([account_id] if account_id else []) + [end.isoformat(), start.isoformat()]
    rows = connection.execute(query, params).fetchall()
    accounts: dict[str, list[tuple[date, date]]] = defaultdict(list)
    names: dict[str, str] = {}
    for row in rows:
        if row["period_start"] is not None:
            accounts[row["account_id"]].append((date.fromisoformat(row["period_start"]), date.fromisoformat(row["period_end"])))
        names[row["account_id"]] = row["name"]
    result: dict[str, list[tuple[date, date]]] = {}
    for account, name in names.items():
        result[name] = sorted(accounts[account])
    return result


def _is_covered(spans: list[tuple[date, date]], start: date, end: date) -> bool:
    """Whether the union of statement spans covers every day in a period."""
    cursor = start
    for left, right in spans:
        if right < cursor:
            continue
        if left > cursor:
            return False
        cursor = right + timedelta(days=1)
        if cursor > end:
            return True
    return False


@router.get("/statements/coverage", dependencies=[Depends(require_reader)])
def public_statement_coverage(
    account_id: str | None = Query(default=None),
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    return statement_coverage(account_id=account_id, connection=connection)


@router.get("/spending/periods", dependencies=[Depends(require_reader)])
def spending_periods(
    granularity: str = Query(default="month", pattern="^(month|week)$"),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    account_id: str | None = Query(default=None),
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    date_to = date_to or date.today()
    date_from = date_from or date_to - timedelta(days=365)
    if date_to < date_from:
        raise HTTPException(status_code=422, detail="date_to must not precede date_from")
    if (date_to - date_from).days > 365:
        raise HTTPException(status_code=422, detail="date range must be 366 days or fewer")
    require_reference(connection, "entities", account_id, "entity")

    periods = _periods(date_from, date_to, granularity)
    category_rows = connection.execute("SELECT id, parent_id FROM categories").fetchall()
    parents = {row["id"]: row["parent_id"] or row["id"] for row in category_rows}
    query = (
        "SELECT txn_date, description, amount_cents, category_id, account_id "
        "FROM active_transactions WHERE txn_date >= ? AND txn_date <= ?"
    )
    params: list[str] = [date_from.isoformat(), date_to.isoformat()]
    if account_id:
        query += " AND account_id = ?"
        params.append(account_id)
    rows = connection.execute(query, params).fetchall()
    coverage = _coverage(connection, date_from, date_to, account_id)
    payload = []
    for start, end in periods:
        by_group: dict[str, int] = defaultdict(int)
        merchant_rows: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
        out_cents = in_cents = income_cents = 0
        for row in rows:
            txn_date = date.fromisoformat(row["txn_date"])
            if not start <= txn_date <= end:
                continue
            group = parents.get(row["category_id"], row["category_id"]) if row["category_id"] else "uncategorized"
            amount = row["amount_cents"]
            if group == "transfers":
                if amount < 0:
                    out_cents += -amount
                else:
                    in_cents += amount
            elif group == "income" or amount > 0:
                income_cents += amount
            elif amount < 0:
                spend = -amount
                by_group[group] += spend
                merchant = merchant_rows[group][_merchant_name(row["description"])]
                merchant[0] += spend
                merchant[1] += 1
        merchants = {}
        for group, values in merchant_rows.items():
            ranked = sorted(values.items(), key=lambda item: (-item[1][0], item[0]))
            shown, remainder = ranked[:8], ranked[8:]
            entries = [{"merchant": name, "cents": cents, "count": count} for name, (cents, count) in shown]
            if remainder:
                entries.append({"merchant": "Everything else", "cents": sum(v[0] for _, v in remainder), "count": sum(v[1] for _, v in remainder)})
            merchants[group] = entries
        missing = []
        for name, spans in coverage.items():
            if not _is_covered(spans, start, end):
                missing.append(name)
        payload.append({
            "start": start.isoformat(), "end": end.isoformat(),
            "spend_cents": sum(by_group.values()), "by_group": dict(by_group),
            "transfers_out_cents": out_cents, "transfers_in_cents": in_cents,
            "income_cents": income_cents, "missing_accounts": missing,
            "merchants": merchants,
        })
    complete = [item for item in payload if not item["missing_accounts"]]
    return {
        "definition": SPENDING_DEFINITION, "coverage_definition": COVERAGE_DEFINITION,
        "granularity": granularity,
        "date_from": date_from.isoformat(), "date_to": date_to.isoformat(),
        "periods": payload, "complete_periods": len(complete),
        "avg_complete_period_cents": (sum(item["spend_cents"] for item in complete) // len(complete)) if complete else None,
        "reporting_account_count": len(coverage),
        "coverage": {
            name: [f"{left.isoformat()}..{right.isoformat()}" for left, right in spans]
            for name, spans in coverage.items()
        },
    }

"""Frozen pre-retirement simulator, never registered as an API/MCP route.

For historical regression evidence only. Refuses disk-backed databases.
"""
import sqlite3
from uuid import uuid4
from api import financial
from api.financial import _require_account_entity
from api.models import ImportStatementRequest, TransactionResponse
from api.transaction_identity import comparison_description


def historical_import(
    body: ImportStatementRequest,
    connection: sqlite3.Connection,
) -> dict:
    if any(row[2] for row in connection.execute('PRAGMA database_list')):
        raise ValueError('Historical evaluator requires an in-memory database')
    # Same account-entity rule as create_statement: this path creates a
    # statement too, so validating only there would leave the hole open.
    _require_account_entity(connection, body.account_id)

    statement = connection.execute(
        "SELECT * FROM statements WHERE account_id = ? AND period_start = ? "
        "AND period_end = ?",
        (body.account_id, body.period_start.isoformat(), body.period_end.isoformat()),
    ).fetchone()

    # Deliberately the raw table, not active_statements. `statements` has a
    # UNIQUE(account_id, period_start, period_end), so an archived
    # statement still occupies its period: looking only at active rows
    # would try to INSERT a second one and fail on the constraint. That
    # would make re-importing a corrected file impossible for exactly the
    # period someone had just archived -- the one case the whole feature
    # exists to serve.
    #
    # So the statement ROW is reactivated while its transactions stay
    # archived. That is the honest state rather than a workaround: the
    # period does have current activity again, and the rows from the bad
    # import remain archived, attached and auditable underneath it. The
    # alternative -- a partial unique index ignoring archived statements --
    # needs a full table rebuild of a table `transactions` references, and
    # is not worth that risk for a state this represents correctly.
    if statement is not None and statement["archived_at"] is not None:
        connection.execute(
            "UPDATE statements SET archived_at = NULL WHERE id = ?",
            (statement["id"],),
        )
    if statement is None:
        statement_id = str(uuid4())
        connection.execute(
            "INSERT INTO statements (id, account_id, period_start, period_end) "
            "VALUES (?, ?, ?, ?)",
            (
                statement_id,
                body.account_id,
                body.period_start.isoformat(),
                body.period_end.isoformat(),
            ),
        )
    else:
        statement_id = statement["id"]

    # Ordinal tiebreak: count how many times this (date, comparison description,
    # amount_cents) tuple has already appeared earlier in THIS batch, so two
    # genuinely identical transactions survive rather than collapsing into
    # one. Originally this fed a row_hash; since the account-scoped check
    # below compares counts directly, the ordinal is the whole mechanism
    # and the hash is gone (a todo, later generalized).
    seen_counts: dict[tuple, int] = {}
    created = 0
    skipped_duplicates = 0
    unmatched: list[dict] = []

    for txn in body.transactions:
        description_key = comparison_description(txn.description)
        key = (txn.txn_date, description_key, txn.amount_cents)
        ordinal = seen_counts.get(key, 0)
        seen_counts[key] = ordinal + 1

        # Account-scoped, not statement-scoped, and a COUNT rather than an
        # existence check. Bank export windows overlap on boundary days, so
        # the same row legitimately arrives inside two different statements
        # (runbooks/statement-intake.md) — a statement-scoped probe inserts
        # it twice and reports both as created. Counting the account's
        # existing matches and inserting only the excess generalizes the
        # in-batch ordinal tiebreak above across that boundary instead of
        # replacing it: two genuinely identical same-day charges still both
        # survive, and an overlapping export carrying two of them when one
        # is already stored adds exactly the one that is new. A boolean
        # "does this key exist" would lose that second charge.
        # Indexed account/date probe, then narrow same-amount comparison.
        # Stored descriptions and classification inputs remain untouched.
        candidates = connection.execute(
            "SELECT description FROM active_transactions WHERE account_id = ? "
            "AND txn_date = ? AND amount_cents = ?",
            (
                body.account_id,
                txn.txn_date.isoformat(),
                txn.amount_cents,
            ),
        ).fetchall()
        existing = sum(comparison_description(row['description']) == description_key
                       for row in candidates)
        if ordinal < existing:
            skipped_duplicates += 1
            continue

        category_id, entity_id = financial._classify(connection, txn.description)

        txn_id = str(uuid4())
        connection.execute(
            "INSERT INTO transactions "
            "(id, statement_id, account_id, txn_date, description, amount_cents, "
            "category_id, entity_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                txn_id,
                statement_id,
                body.account_id,
                txn.txn_date.isoformat(),
                txn.description,
                txn.amount_cents,
                category_id,
                entity_id,
            ),
        )
        created += 1
        # `unmatched` means "still needs a category decision", not "no rule
        # matched". A rule carrying only entity_id is legitimate but leaves
        # category_id NULL — keying this on `rule is None` would mark such a
        # transaction handled and it would never be surfaced, accumulating
        # silently as permanently uncategorized. See
        # runbooks/category-taxonomy.md on uncategorized trending to zero.
        if category_id is None:
            row = connection.execute(
                "SELECT * FROM transactions WHERE id = ?", (txn_id,)
            ).fetchone()
            unmatched.append(TransactionResponse(**dict(row)).model_dump(mode="json"))

    connection.commit()
    return {
        "statement_id": statement_id,
        "created": created,
        "skipped_duplicates": skipped_duplicates,
        "unmatched": unmatched,
    }


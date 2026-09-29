import re
import sqlite3
from datetime import date, timedelta
from uuid import uuid4

from fastapi.exceptions import RequestValidationError
from fastapi import APIRouter, Depends, Query

from api.auth import require_token
from api.db import require_reference, MAX_LIMIT, get_connection, paginate
from api.errors import ConflictError, InvalidReferenceError, NotFoundError
from api.models import (
    resolve_patch,
    ApplyAsRuleRequest,
    ArchiveStatementResponse,
    ArchiveTransactionResponse,
    ReapplyRulesRequest,
    ReapplyRulesResponse,
    CategoryCreate,
    CategoryResponse,
    CategoryUpdate,
    MerchantRuleCreate,
    MerchantRuleResponse,
    MerchantRuleUpdate,
    StatementCreate,
    StatementResponse,
    TransactionResponse,
    TransactionUpdate,
)

# require_token is applied at the router level, not as a named parameter
# dependency, so an unauthenticated request is rejected before
# get_connection opens a DB connection. See api/auth.py's docstring.
router = APIRouter(dependencies=[Depends(require_token)])


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "category"


def _unique_category_slug(
    connection: sqlite3.Connection, name: str, parent_id: str | None
) -> str:
    # Child slugs are "{parent_id}_{slug}", matching db/seed_categories.sql's
    # existing convention (auto_insurance, housing_utilities, ...) — not
    # just _slugify(name). Several names (Insurance, Utilities, Repairs)
    # legitimately appear under more than one parent in the seed data, so
    # without the parent prefix a plain slugify collides with an unrelated
    # top-level category of the same name and falls through to a
    # meaningless "_2" suffix instead of the conventional compound id.
    base = f"{parent_id}_{_slugify(name)}" if parent_id else _slugify(name)
    slug = base
    suffix = 2
    while connection.execute(
        "SELECT 1 FROM categories WHERE id = ?", (slug,)
    ).fetchone():
        slug = f"{base}_{suffix}"
        suffix += 1
    return slug


def _require_parent_exists(
    connection: sqlite3.Connection, parent_id: str | None
) -> None:
    """Reject an unknown parent_id before the FK does.

    categories.parent_id is a foreign key, so an unknown value raises
    sqlite3.IntegrityError from the INSERT/UPDATE — uncaught, that leaves
    the route as a bare 500 with none of this API's error envelope. Checked
    up front instead, matching how entities.py validates a relationship's
    target entity.
    """
    if parent_id is None:
        return
    if (
        connection.execute(
            "SELECT 1 FROM categories WHERE id = ?", (parent_id,)
        ).fetchone()
        is None
    ):
        raise InvalidReferenceError(f"No category with id {parent_id!r}")


def _get_category_row(connection: sqlite3.Connection, category_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM categories WHERE id = ?", (category_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"No category with id {category_id!r}")
    return row


@router.post("/categories", response_model=CategoryResponse)
def create_category(
    body: CategoryCreate,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    _require_parent_exists(connection, body.parent_id)
    category_id = _unique_category_slug(connection, body.name, body.parent_id)
    connection.execute(
        "INSERT INTO categories (id, name, parent_id) VALUES (?, ?, ?)",
        (category_id, body.name, body.parent_id),
    )
    connection.commit()
    return dict(_get_category_row(connection, category_id))


@router.get("/categories/{category_id}", response_model=CategoryResponse)
def get_category(
    category_id: str,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    return dict(_get_category_row(connection, category_id))


@router.get("/categories")
def list_categories(
    parent_id: str | None = Query(default=None),
    # ge=1: paginate() raises ValueError on a non-positive limit, so it has
    # to be rejected as a 422 before reaching that helper.
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    # A filter naming something that does not exist is refused, not
    # answered with an empty page: the two are indistinguishable to
    # the caller, and only one of them is true (ticket T-08).
    require_reference(connection, "categories", parent_id, "category")
    query = "SELECT * FROM categories WHERE 1=1"
    params: list = []
    if parent_id is not None:
        query += " AND parent_id = ?"
        params.append(parent_id)
    query += " ORDER BY name COLLATE NOCASE, id"
    return paginate(connection, query, tuple(params), limit=limit, offset=offset)


@router.patch("/categories/{category_id}", response_model=CategoryResponse)
def update_category(
    category_id: str,
    body: CategoryUpdate,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    row = _get_category_row(connection, category_id)
    _require_parent_exists(connection, body.parent_id)
    name = resolve_patch(body, "name", row["name"])
    parent_id = resolve_patch(body, "parent_id", row["parent_id"])
    connection.execute(
        "UPDATE categories SET name = ?, parent_id = ? WHERE id = ?",
        (name, parent_id, category_id),
    )
    connection.commit()
    return dict(_get_category_row(connection, category_id))


@router.delete("/categories/{category_id}")
def delete_category(
    category_id: str,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    _get_category_row(connection, category_id)
    try:
        connection.execute("DELETE FROM categories WHERE id = ?", (category_id,))
        connection.commit()
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        raise ConflictError(
            f"Cannot delete category {category_id!r}: still referenced by other records"
        ) from exc
    return {"deleted": True}


def _require_account_entity(connection: sqlite3.Connection, account_id: str) -> None:
    """Require that `account_id` names an entity of type 'account'.

    statements.account_id is REFERENCES entities(id) — *any* entity — so the
    foreign key alone accepts a statement against a pet or a vehicle.
    account_subtype already covers loan/insurance/credit_card under type
    'account', so any other type is always an error.
    """
    row = connection.execute(
        "SELECT type FROM entities WHERE id = ?", (account_id,)
    ).fetchone()
    if row is None:
        raise InvalidReferenceError(f"No entity with id {account_id!r}")
    if row["type"] != "account":
        # Name what it actually is: "no such account" would be actively
        # misleading when the entity exists and is merely the wrong kind.
        raise InvalidReferenceError(
            f"Entity {account_id!r} is a {row['type']}, not an account"
        )


def _get_statement_row(
    connection: sqlite3.Connection, statement_id: str
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM statements WHERE id = ?", (statement_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"No statement with id {statement_id!r}")
    return row


@router.post("/statements", response_model=StatementResponse)
def create_statement(
    body: StatementCreate,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    _require_account_entity(connection, body.account_id)

    statement_id = str(uuid4())
    try:
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
        connection.commit()
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        raise ConflictError(
            f"A statement for account {body.account_id!r} covering "
            f"{body.period_start}\u2013{body.period_end} already exists"
        ) from exc
    return dict(_get_statement_row(connection, statement_id))


# Declared above GET /statements/{statement_id} so the literal path is
# matched first. FastAPI resolves in declaration order, so with this
# below it "coverage" is read as a statement id and the endpoint answers
# 404 "No statement with id 'coverage'" — which is what it did until a
# request was actually sent. The openapi listing showed the path either
# way; a route being registered is not the same as it being reachable.
# Same hazard as api/jyra.py's /tickets/claim.
def statement_coverage(
    account_id: str | None = Query(default=None),
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    """Adjacency of a account's statement periods: gaps and overlaps.

    WHAT THIS CANNOT DO, stated first because the obvious stronger
    version is a trap someone will propose again (D56). It cannot see a
    missing PAGE. A statement that imported without its second page
    looks complete here and everywhere else — its rows are present, its
    period is whole, and it sits flush against its neighbours.

    Nor can any in-document check see it. Storing opening/closing
    balances and asserting `opening + sum == closing` was the original
    proposal and was rejected for a measured reason: a balance chain
    reconciles WITHIN the pages you have, so the run that dropped the
    page also read the closing balance off a page it did have. The
    check would pass on exactly the import it exists to catch, with a
    stored number behind it — evidence of completeness manufactured
    from the same extraction that lost the data. An in-document check
    inherits the extraction's blindness.

    What adjacency does catch is a whole statement MISSING, which the
    balance approach cannot see at all: a chain across the statements
    you have is as self-consistent as a chain across the pages you
    have.

    Over `active_statements`, so archiving a statement OPENS a gap
    rather than closing silently over it. That is the intended
    reading — an archived statement's rows no longer count, so the
    period it covered is genuinely unaccounted for.
    """
    query = (
        "SELECT s.id, s.account_id, s.period_start, s.period_end, e.name AS "
        "account_name FROM active_statements s "
        "LEFT JOIN entities e ON e.id = s.account_id WHERE 1=1"
    )
    params: list = []
    if account_id is not None:
        require_reference(connection, "entities", account_id, "entity")
        query += " AND s.account_id = ?"
        params.append(account_id)
    # Ordered on a total key. Two statements can share a period_start —
    # a re-export overlapping an existing window — and adjacency is
    # computed pairwise down this list, so an unstable order would make
    # the reported gaps depend on scan order.
    query += " ORDER BY s.account_id, s.period_start, s.period_end, s.id"

    rows = [dict(row) for row in connection.execute(query, tuple(params)).fetchall()]

    accounts: dict[str, list[dict]] = {}
    for row in rows:
        accounts.setdefault(row["account_id"], []).append(row)

    gaps: list[dict] = []
    overlaps: list[dict] = []
    for statements in accounts.values():
        for earlier, later in zip(statements, statements[1:]):
            delta = (
                date.fromisoformat(later["period_start"])
                - date.fromisoformat(earlier["period_end"])
            ).days
            if delta == 1:
                continue
            entry = {
                "account_id": earlier["account_id"],
                "account_name": earlier["account_name"],
                "before_id": earlier["id"],
                "after_id": later["id"],
            }
            # delta == 0 means `later` starts the day `earlier` ended, and
            # negative means it starts before — both are the same thing,
            # a re-export covering ground already imported. Reported, not
            # flagged: overlapping export windows are documented and
            # expected for CSV card exports such as card-a (runbooks/statement-intake.md), and
            # dedup is what keeps them from double-counting.
            #
            # A GAP AND AN OVERLAP GET DIFFERENT FIELD NAMES, and that is
            # the whole point of the shape (ticket T-13). They used to share
            # `gap_start`/`gap_end` and a single `days` that went NEGATIVE
            # for an overlap. The values were right and unreadable: a
            # model summarising the overlaps array sees fields called
            # `gap_*` and can very reasonably report "a gap on
            # 2026-04-09", which is the opposite of the truth — and a
            # false gap is the expensive direction, because a gap is the
            # alarming case this tool exists to surface. The description
            # promised "the days between them", which for an overlap is
            # not a thing that exists.
            #
            # So the numbers now read correctly with no description in
            # hand: a gap counts days MISSING, an overlap counts days
            # COVERED TWICE, and both are non-negative.
            if delta > 1:
                # The first and last MISSING day, not the covered days
                # either side of the hole (ticket T-14). Those bracketed it:
                # `gap_start`/`gap_end` named a 30-day span beside
                # `days: 28`, so the range and the count disagreed by two.
                #
                # Harmless in isolation and not in context. Since #145 the
                # overlap entry sits beside this one with PARALLEL names,
                # and there the named range IS what `days` counts. A
                # reader who learns the convention from one sibling
                # reports a 28-day gap as 30. I introduced that hazard by
                # making the overlap self-consistent and leaving this
                # bracketing, which is a worse defect than the one I was
                # fixing: the two now looked like the same shape.
                #
                # One convention, both lists: the named range IS the
                # thing `days` counts.
                last_covered = date.fromisoformat(earlier["period_end"])
                first_uncovered = date.fromisoformat(later["period_start"])
                gaps.append(
                    {
                        **entry,
                        "gap_start": (last_covered + timedelta(days=1)).isoformat(),
                        "gap_end": (first_uncovered - timedelta(days=1)).isoformat(),
                        "days": delta - 1,
                    }
                )
            else:
                # The intersection, so start <= end. Reusing the gap
                # spelling here put the later date first and the earlier
                # one second — a range that reads backwards.
                overlaps.append(
                    {
                        **entry,
                        "overlap_start": later["period_start"],
                        "overlap_end": earlier["period_end"],
                        "days": 1 - delta,
                    }
                )

    return {
        "statements": len(rows),
        "accounts": len(accounts),
        "gaps": gaps,
        "overlaps": overlaps,
    }


@router.get("/statements/{statement_id}", response_model=StatementResponse)
def get_statement(
    statement_id: str,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    return dict(_get_statement_row(connection, statement_id))


@router.get("/statements")
def list_statements(
    account_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    # A filter naming something that does not exist is refused, not
    # answered with an empty page: the two are indistinguishable to
    # the caller, and only one of them is true (ticket T-08).
    require_reference(connection, "entities", account_id, "account entity")
    query = "SELECT * FROM active_statements WHERE 1=1"
    params: list = []
    if account_id is not None:
        query += " AND account_id = ?"
        params.append(account_id)
    query += " ORDER BY period_start DESC, id"
    return paginate(connection, query, tuple(params), limit=limit, offset=offset)


#: The matcher's behaviour in one line, stated ONCE, beside the code that
#: implements it. Every tool description that claims how matching works
#: interpolates this rather than retyping it.
#:
#: The reason is not tidiness. `apply_transaction_as_rule`'s description
#: said "matching is by substring" for four PRs after #72 made it
#: token-bounded, and it was noticed only because someone happened to be
#: editing four lines above. A model reading that would expect `ACME FUEL`
#: to match `ACMEFUEL41` and write rules on that basis.
#:
#: A constant alone would not have prevented that — a constant can go
#: stale beside its code just as prose can. What closes the gap is the
#: pair: this phrase is interpolated into every description (so they
#: cannot disagree with it), AND
#: `test_the_matcher_behaves_as_its_contract_states` exercises each clause
#: against the real matcher (so it cannot disagree with the behaviour).
#: Changing the regex or the ORDER BY fails that test by name.
MATCHER_CONTRACT = (
    "token-bounded and case-insensitive, longest pattern wins, "
    "lowest id on ties"
)


def _pattern_matches(pattern: str, description: str) -> bool:
    """Is `pattern` present in `description` as a whole token?

    Token-bounded, not bare substring. Bare substring is how `ESSO` matched
    "ZESSOR ACADEMY" and `SHELL` matched "ZUMSHELLA SALON" — a tutoring
    charge categorised as auto fuel. Longest-pattern-wins cannot rescue
    that: it resolves a short form losing to a long form of the SAME
    merchant, not a pattern buried inside an unrelated word.

    A match needs a non-alphanumeric character (or a string edge) on both
    sides. That keeps the usual shapes working — e.g. "ESSO 1234 ANYTOWN",
    "FUELCO/ESSO", "Shopco.ca*482KX7RT2", "SHPC Mktp CA*1A2B3C" —
    because statement descriptions separate fields with spaces, stars,
    hashes and slashes.

    Digits count as alphanumeric, so `ESSO1234` does NOT match. That is
    deliberate rather than an oversight: the alternative reopens the same
    ambiguity from the other end, and the cost of being wrong here is
    asymmetric. An unmatched transaction lands in `unmatched` and asks a
    human; a miscategorised one answers wrongly and never asks again,
    permanently, because the category is written at import time.

    `re.escape` matters and is not defensive habit: patterns are real
    merchant strings containing dots and stars (`Amazon.ca`), which
    unescaped would be regex wildcards and match `AmazonXca`.
    """
    bounded = rf"(?<![A-Za-z0-9]){re.escape(pattern)}(?![A-Za-z0-9])"
    return re.search(bounded, description, re.IGNORECASE) is not None


def _match_merchant_rule(
    connection: sqlite3.Connection, description: str
) -> sqlite3.Row | None:
    # Case-insensitive: a rule pattern that merely differs in case from a
    # real statement description (e.g. "Amazon" vs "AMAZON MKTPLACE PMTS")
    # would otherwise silently never match — no error, no unmatched flag,
    # just permanently uncategorized. That's the same silent-wrong-answer
    # shape as a todo's bootstrap symptoms, so it's closed here
    # rather than left latent even though bank descriptions are
    # conventionally uppercase in practice.
    # Precedence is expressed in the ORDER BY, so the first surviving row
    # is the winner and there is no second ranking step to disagree with
    # it. The previous form was an unordered SELECT plus max(), and max()
    # returns the FIRST maximal element — so two equal-length patterns
    # matching one description were resolved by SQLite's scan order,
    # which no statement guarantees, while the intake skill's preview
    # resolved them by id. Preview and import could then disagree on
    # exactly the shape the parity check exists to catch (D15).
    #
    # length(pattern) DESC, id ASC: longest wins, then lowest id. Lowest
    # id is arbitrary with respect to age — ids are uuid4 and the table
    # has no created_at — but it is deterministic, stable across
    # re-imports, and identical to what the preview already computes, and
    # those are the properties the tie-break is for. See
    # runbooks/merchant-rules-conventions.md.
    rules = connection.execute(
        "SELECT * FROM merchant_rules ORDER BY length(pattern) DESC, id ASC"
    ).fetchall()
    # Filtered after ordering, never before: a longer pattern that fails
    # the token-boundary test must not beat a shorter one that passed, so
    # the boundary test decides membership and the ORDER BY only decides
    # rank among the survivors.
    for rule in rules:
        if _pattern_matches(rule["pattern"], description):
            return rule
    return None


def _classify(
    connection: sqlite3.Connection, description: str
) -> tuple[str | None, str | None]:
    """The category/entity a description earns from the current rules.

    Import and reapply MUST agree, and the only way to guarantee that is
    for them to be the same code rather than two copies that look alike.
    `test_reapply_and_import_share_the_classifier` proves it by patching
    this function and observing both paths change -- a copy would not.
    """
    rule = _match_merchant_rule(connection, description)
    if rule is None:
        return None, None
    return rule["category_id"], rule["entity_id"]


def _get_transaction_row(
    connection: sqlite3.Connection, transaction_id: str
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM transactions WHERE id = ?", (transaction_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"No transaction with id {transaction_id!r}")
    return row


@router.get("/transactions/{transaction_id}", response_model=TransactionResponse)
def get_transaction(
    transaction_id: str,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    return dict(_get_transaction_row(connection, transaction_id))


#: The order this endpoint returns, stated once beside the query
#: that produces it (ORDER BY txn_date DESC, id). The tool description
#: interpolates this rather than retyping it, and a behavioural
#: test asserts the rows actually come back this way -- a flipped
#: ORDER BY is invisible to every caller who believed the sentence.
LIST_TRANSACTIONS_ORDER = "newest first"


@router.get("/transactions")
def list_transactions(
    account_id: str | None = Query(default=None),
    entity_id: str | None = Query(default=None),
    category_id: str | None = Query(default=None),
    # Typed as dates, not strings: as strings these were interpolated into
    # a SQL string comparison and never validated, so a malformed value
    # returned 200 with an empty page (reading as "no transactions") and a
    # US-formatted date returned everything, since '2026-05-10' >=
    # '05/01/2026' holds lexically. Typing them buys a 422 for free.
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    # A filter naming something that does not exist is refused, not
    # answered with an empty page: the two are indistinguishable to
    # the caller, and only one of them is true (ticket T-08).
    require_reference(connection, "entities", account_id, "account entity")
    require_reference(connection, "entities", entity_id, "entity")
    require_reference(connection, "categories", category_id, "category")
    # An impossible range cannot match anything, so an empty page is not
    # an answer -- it is a refusal wearing the clothes of one. /due
    # already refuses this and names both values; two endpoints taking
    # the same pair of dates should not disagree about whether swapping
    # them is a mistake (ticket T-15).
    if date_from is not None and date_to is not None and date_to < date_from:
        raise RequestValidationError(
            [
                {
                    "type": "value_error",
                    "loc": ("query", "date_to"),
                    "msg": (
                        f"date_to ({date_to}) must not precede "
                        f"date_from ({date_from})"
                    ),
                    "input": str(date_to),
                }
            ]
        )

    # Columns named rather than SELECT *. The occasion was row_hash, an
    # internal de-duplication artefact that the listing leaked and that
    # this branch has since dropped from the table entirely — but the
    # reason outlives it: with SELECT * the next column added to
    # transactions joins the response by default, and nobody notices
    # until a caller depends on it. Matches TransactionResponse, which
    # get_transaction already returns, so the listing and the single read
    # are one shape.
    query = (
        "SELECT id, statement_id, account_id, txn_date, description, "
        "amount_cents, category_id, entity_id FROM active_transactions WHERE 1=1"
    )
    params: list = []
    if account_id is not None:
        query += " AND account_id = ?"
        params.append(account_id)
    if entity_id is not None:
        query += " AND entity_id = ?"
        params.append(entity_id)
    if category_id is not None:
        query += " AND category_id = ?"
        params.append(category_id)
    if date_from is not None:
        query += " AND txn_date >= ?"
        params.append(date_from.isoformat())
    if date_to is not None:
        query += " AND txn_date <= ?"
        params.append(date_to.isoformat())
    query += " ORDER BY txn_date DESC, id"
    return paginate(connection, query, tuple(params), limit=limit, offset=offset)


@router.patch("/transactions/{transaction_id}", response_model=TransactionResponse)
def update_transaction(
    transaction_id: str,
    body: TransactionUpdate,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    # Correction-only: only category_id/entity_id are patchable. txn_date,
    # description, and amount come from the source statement — correcting
    # those would diverge the record from the document it was imported
    # from, which is exactly the trustworthy-record property this system
    # exists for. See runbooks/merchant-rules-conventions.md.
    row = _get_transaction_row(connection, transaction_id)
    require_reference(connection, "categories", body.category_id, "category")
    require_reference(connection, "entities", body.entity_id, "entity")
    category_id = resolve_patch(body, "category_id", row["category_id"])
    entity_id = resolve_patch(body, "entity_id", row["entity_id"])
    # edited_at, not merely the new values: a re-import after archiving
    # brings rows back WITHOUT this work, and nothing errors when it does.
    # Recording the edit is what lets archive_statement say so in advance.
    connection.execute(
        "UPDATE transactions SET category_id = ?, entity_id = ?, "
        "edited_at = CURRENT_TIMESTAMP WHERE id = ?",
        (category_id, entity_id, transaction_id),
    )
    connection.commit()
    return dict(_get_transaction_row(connection, transaction_id))


def _set_archived(
    connection: sqlite3.Connection, statement_id: str, archived: bool
) -> dict:
    """Archive or restore a statement AND its rows, in one transaction.

    Both or neither. A statement archived without its transactions is the
    exact silent-wrong-number state this feature exists to prevent: the
    statement leaves the list while every total still counts its rows.

    **"Hand-edited" means precisely this:** a row whose `category_id` or
    `entity_id` was changed through `PATCH /transactions/{id}` at any point
    after it was imported. That is recorded by `edited_at`, which only that
    endpoint sets.

    It deliberately does NOT mean "category_id is not null". An import
    assigns categories itself from the merchant rules, so that test would
    count rows nobody ever touched, and the warning would be noise on
    every statement — a warning that fires when nothing is wrong is one
    people learn to skip past.

    It also cannot include the description. `TransactionUpdate` accepts
    only `category_id` and `entity_id`, so a description cannot be changed
    through the API at all; there is no such edit to detect. If
    descriptions ever become editable, that endpoint must set `edited_at`
    too, and this docstring is where to find out why.
    """
    stamp = "CURRENT_TIMESTAMP" if archived else "NULL"
    hand_edited_ids = [
        row[0]
        for row in connection.execute(
            "SELECT id FROM transactions WHERE statement_id = ? "
            "AND edited_at IS NOT NULL ORDER BY txn_date, id",
            (statement_id,),
        )
    ]
    cursor = connection.execute(
        f"UPDATE transactions SET archived_at = {stamp} WHERE statement_id = ?",
        (statement_id,),
    )
    affected = cursor.rowcount
    connection.execute(
        f"UPDATE statements SET archived_at = {stamp} WHERE id = ?", (statement_id,)
    )
    connection.commit()
    return {
        "id": statement_id,
        "archived": archived,
        "archived_transactions": affected,
        "hand_edited": len(hand_edited_ids),
        "hand_edited_ids": hand_edited_ids,
    }


@router.post(
    "/statements/{statement_id}/archive", response_model=ArchiveStatementResponse
)
def archive_statement(
    statement_id: str,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    """Remove a statement and its transactions from every total.

    Archived, never deleted: the rows stay on disk and stay auditable,
    because a financial record's value is that what was imported remains
    answerable afterwards. Archived rows also stop counting for
    de-duplication, so the corrected file can be imported straight after --
    which is the main reason anyone archives at all.
    """
    _get_statement_row(connection, statement_id)
    return _set_archived(connection, statement_id, archived=True)


@router.post(
    "/statements/{statement_id}/unarchive", response_model=ArchiveStatementResponse
)
def unarchive_statement(
    statement_id: str,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    """Put a statement and its rows back into every total.

    Ships with archiving rather than after it: archiving is one UPDATE and
    so is reversing it, and without this a mistaken archive is permanent.
    """
    _get_statement_row(connection, statement_id)
    return _set_archived(connection, statement_id, archived=False)


@router.post(
    "/transactions/{transaction_id}/archive",
    response_model=ArchiveTransactionResponse,
)
def archive_transaction(
    transaction_id: str,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    """Archive one row, for a duplicate or a mis-parsed line."""
    row = _get_transaction_row(connection, transaction_id)
    connection.execute(
        "UPDATE transactions SET archived_at = CURRENT_TIMESTAMP WHERE id = ?",
        (transaction_id,),
    )
    connection.commit()
    return {
        "id": transaction_id,
        "archived": True,
        "hand_edited": 1 if row["edited_at"] is not None else 0,
        "hand_edited_ids": [transaction_id] if row["edited_at"] is not None else [],
    }


@router.post(
    "/transactions/{transaction_id}/unarchive",
    response_model=ArchiveTransactionResponse,
)
def unarchive_transaction(
    transaction_id: str,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    row = _get_transaction_row(connection, transaction_id)
    connection.execute(
        "UPDATE transactions SET archived_at = NULL WHERE id = ?", (transaction_id,)
    )
    connection.commit()
    return {
        "id": transaction_id,
        "archived": False,
        "hand_edited": 1 if row["edited_at"] is not None else 0,
        "hand_edited_ids": [transaction_id] if row["edited_at"] is not None else [],
    }


def _get_merchant_rule_row(
    connection: sqlite3.Connection, rule_id: str
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM merchant_rules WHERE id = ?", (rule_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"No merchant rule with id {rule_id!r}")
    return row


#: The fields of a merchant rule whose changes are recorded. Listed rather
#: than derived from the row, so adding a column is a decision about
#: whether it belongs in the audit trail instead of a silent yes.
AUDITED_RULE_FIELDS = ("pattern", "category_id", "entity_id")


def _record_rule_change(
    connection: sqlite3.Connection,
    rule_id: str,
    actor: str,
    field: str | None = None,
    old_value: str | None = None,
    new_value: str | None = None,
) -> None:
    """Append to the rule audit trail. NEVER commits.

    The caller commits the change and this row together, so they cannot
    come apart — the same contract `_record_transition` has for tickets,
    and the reason this is a helper rather than an inline INSERT: a
    function that never commits is a rule a reader can check at a glance,
    where an inline write invites a `connection.commit()` beside it.

    A creation or deletion row carries `field=None`. Neither is a change
    TO a field, and inventing one would put a fact in the log that did not
    happen. A rule's INITIAL values stay recoverable regardless: from the
    first update's `old_value` if it was ever edited, and from the rule
    itself if it was not.
    """
    connection.execute(
        "INSERT INTO merchant_rule_changes "
        "(id, rule_id, actor, field, old_value, new_value) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid4()), rule_id, actor, field, old_value, new_value),
    )


@router.post("/merchant_rules", response_model=MerchantRuleResponse)
def create_merchant_rule(
    body: MerchantRuleCreate,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    require_reference(connection, "categories", body.category_id, "category")
    require_reference(connection, "entities", body.entity_id, "entity")
    rule_id = str(uuid4())
    connection.execute(
        "INSERT INTO merchant_rules (id, pattern, category_id, entity_id) "
        "VALUES (?, ?, ?, ?)",
        (rule_id, body.pattern, body.category_id, body.entity_id),
    )
    # Before the commit, so the rule and its record land together.
    _record_rule_change(connection, rule_id, body.actor)
    connection.commit()
    return dict(_get_merchant_rule_row(connection, rule_id))


@router.get("/merchant_rules/{rule_id}", response_model=MerchantRuleResponse)
def get_merchant_rule(
    rule_id: str,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    return dict(_get_merchant_rule_row(connection, rule_id))


@router.get("/merchant_rules")
def list_merchant_rules(
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    return paginate(
        connection,
        "SELECT * FROM merchant_rules ORDER BY id",
        (),
        limit=limit,
        offset=offset,
    )


#: The order this endpoint returns, stated once beside the query
#: that produces it (ORDER BY changed_at, field). The tool description
#: interpolates this rather than retyping it, and a behavioural
#: test asserts the rows actually come back this way -- a flipped
#: ORDER BY is invisible to every caller who believed the sentence.
#:
#: The second-granularity caveat is IN the phrase, not only in a test
#: docstring, because a caller reading the rendered description is
#: exactly who needs it: changed_at has one-second resolution, so a
#: two-field PATCH writes rows that tie, and `field` decides. Saying
#: only "oldest first" would be true at the scale anyone checks and
#: wrong at the scale that bites -- two edits landing together.
RULE_HISTORY_ORDER = (
    "oldest first; two changes in the same second come back in "
    "field order, not in the order they were made"
)


@router.get("/merchant_rules/{rule_id}/history")
def merchant_rule_history(
    rule_id: str,
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    """A rule's changes, oldest first.

    Deliberately NOT gated on the rule still existing. A deleted rule's
    past is exactly when someone wants to read it — "what did this say
    before it was removed, and who removed it" is unanswerable if the
    lookup 404s on the deletion it is asking about.
    `merchant_rule_changes.rule_id` carries no foreign key for that
    reason, so the history outlives the row.

    GATED ON THE ID HAVING BEEN SEEN AT ALL, which is a different
    question and the one ticket T-16 was really about. Before this, a
    mistyped id returned {"items": [], "total": 0} — the same body a
    real rule with no changes returns, so "you asked about nothing" and
    "this rule was never edited" were indistinguishable, and an audit
    trail exists to answer exactly that.

    The ticket asked for a plain 404 on a missing rule and that would
    have been wrong: it breaks the deleted-rule read above, which is the
    primary use. An id with history but no rule is a DELETED rule and
    still answers; an id with neither is one nobody has ever used.
    """
    known = connection.execute(
        "SELECT 1 FROM merchant_rules WHERE id = ? "
        "UNION ALL SELECT 1 FROM merchant_rule_changes WHERE rule_id = ? "
        "LIMIT 1",
        (rule_id, rule_id),
    ).fetchone()
    if known is None:
        raise NotFoundError(
            f"No merchant rule with id {rule_id!r}, and no history under "
            "that id either — nothing has ever been recorded against it. "
            "A rule that was deleted still returns its history here."
        )
    return paginate(
        connection,
        "SELECT * FROM merchant_rule_changes WHERE rule_id = ? "
        "ORDER BY changed_at, field",
        (rule_id,),
        limit=limit,
        offset=offset,
    )


@router.post("/merchant_rules/reapply", response_model=ReapplyRulesResponse)
def reapply_merchant_rules(
    body: ReapplyRulesRequest,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    """Apply current rules to rows that were never classified.

    Rules run at import time only, so adding or correcting a rule changes
    nothing already stored: "we added the rules" and "the totals reflect
    them" are different claims until this runs.

    NEVER OVERWRITES. Only rows with no category AND no entity are
    touched, so a row that already carries a classification -- from an
    earlier import or from a human -- is left alone. That also means
    correcting a WRONG rule does not retroactively fix the rows it already
    miscategorized; those need a separate, explicit decision.

    `edited_at IS NULL` IS PART OF THE PREDICATE, and it was not before.

    This previously read "the predicate's safety rests on an absence" --
    the absence being that a deliberate null could not be produced, since
    `update_transaction` fell back to the existing value when passed None.
    On that basis D59 REMOVED this clause as inert, and was right to: the
    at-least-one guard meant any accepted PATCH left a non-null category
    or entity, which the null test already excluded.

    That entry named its own trigger -- "when something makes a deliberate
    null reachable -- a clearing endpoint" -- and ticket T-17 is exactly
    that. `{"category_id": null}` now clears. So a row with no
    classification no longer means "never classified": it can equally mean
    "a person looked at this and decided it belongs to nothing", and
    re-running rules over that decision silently overwrites it.

    The old entry doubted `edited_at` could tell those apart on its own,
    and for a general model it could not. It can here, because
    `TransactionUpdate` is patchable ONLY in `category_id` and
    `entity_id`: any accepted PATCH names one of them, so `edited_at IS
    NOT NULL` on a row with neither set means a person cleared them. There
    is no third field that could stamp `edited_at` without touching
    classification.

    THAT IS A CONTINGENT ARGUMENT, so it is pinned rather than trusted:
    `test_the_edited_at_clause_depends_on_this_field_set` asserts
    `TransactionUpdate` has exactly those two settable fields. Adding a
    third breaks the reasoning above, and that test fails and says so.
    """
    # active_transactions, not the raw table: an ARCHIVED import's rows
    # must not be reclassified. Archiving says "this import should not
    # count"; silently re-categorizing its rows would make it count again,
    # in the one column a reader trusts. #94's scope guard caught this --
    # the raw read was an oversight, not a decision.
    query = (
        "SELECT id, description, amount_cents FROM active_transactions "
        # edited_at IS NULL: since ticket T-17 a null classification can be a
        # DECISION rather than an absence. See the docstring -- this clause
        # is what keeps reapply from overwriting one.
        "WHERE category_id IS NULL AND entity_id IS NULL "
        "AND edited_at IS NULL"
    )
    params: list[str] = []
    if body.statement_id is not None:
        require_reference(connection, "statements", body.statement_id, "statement")
        query += " AND statement_id = ?"
        params.append(body.statement_id)
    if body.entity_id is not None:
        require_reference(connection, "entities", body.entity_id, "entity")
        query += " AND account_id = ?"
        params.append(body.entity_id)

    rows = connection.execute(query, tuple(params)).fetchall()
    cents_by_category: dict[str, int] = {}
    changed_ids: list[str] = []

    for row in rows:
        category_id, entity_id = _classify(connection, row["description"])
        if category_id is None and entity_id is None:
            continue
        changed_ids.append(row["id"])
        if not body.dry_run:
            connection.execute(
                "UPDATE transactions SET category_id = ?, entity_id = ? WHERE id = ?",
                (category_id, entity_id, row["id"]),
            )
        if category_id is not None:
            cents_by_category[category_id] = (
                cents_by_category.get(category_id, 0) + row["amount_cents"]
            )

    if not body.dry_run:
        connection.commit()
    return {
        "dry_run": body.dry_run,
        "examined": len(rows),
        "changed": len(changed_ids),
        "cents_by_category": cents_by_category,
        "changed_ids": changed_ids,
    }


@router.patch("/merchant_rules/{rule_id}", response_model=MerchantRuleResponse)
def update_merchant_rule(
    rule_id: str,
    body: MerchantRuleUpdate,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    row = _get_merchant_rule_row(connection, rule_id)
    require_reference(connection, "categories", body.category_id, "category")
    require_reference(connection, "entities", body.entity_id, "entity")
    pattern = resolve_patch(body, "pattern", row["pattern"])
    category_id = resolve_patch(body, "category_id", row["category_id"])
    entity_id = resolve_patch(body, "entity_id", row["entity_id"])
    connection.execute(
        "UPDATE merchant_rules SET pattern = ?, category_id = ?, entity_id = ? "
        "WHERE id = ?",
        (pattern, category_id, entity_id, rule_id),
    )
    # One row per field that actually CHANGED. A row saying a value went
    # from X to X pads the trail someone reads when they are looking for
    # the change that mattered.
    for name, new_value in zip(
        AUDITED_RULE_FIELDS, (pattern, category_id, entity_id), strict=True
    ):
        if row[name] != new_value:
            _record_rule_change(
                connection, rule_id, body.actor, name, row[name], new_value
            )
    connection.commit()
    return dict(_get_merchant_rule_row(connection, rule_id))


@router.delete("/merchant_rules/{rule_id}")
def delete_merchant_rule(
    rule_id: str,
    actor: str = Query(min_length=1),
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    """`actor` is a QUERY parameter here, and only here.

    Everywhere else in this API it rides in the body, but a DELETE body is
    legal and poorly supported, and there is no precedent to copy:
    delete_ticket takes no actor because tickets never record deletion.
    The asymmetry is deliberate and named rather than silently picked
    (D52) — a caller who expects a body will get a 422 naming `actor` as a
    missing query parameter, which is a better failure than a body
    accepted and ignored.
    """
    _get_merchant_rule_row(connection, rule_id)
    connection.execute("DELETE FROM merchant_rules WHERE id = ?", (rule_id,))
    # A terminal row, written after the delete and before the commit. It
    # outlives the rule on purpose: see the migration's note on why
    # rule_id carries no REFERENCES clause.
    _record_rule_change(connection, rule_id, actor)
    connection.commit()
    return {"deleted": True}


@router.post(
    "/transactions/{transaction_id}/apply_as_rule",
    response_model=MerchantRuleResponse,
)
def apply_transaction_as_rule(
    transaction_id: str,
    body: ApplyAsRuleRequest,
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    # Creates a new, more specific rule from this transaction's exact
    # description rather than editing whatever broader rule currently
    # matches it — see runbooks/merchant-rules-conventions.md. The new
    # rule's pattern (the full description) is longer than any broader
    # rule that matched it, so precedence naturally takes over for this
    # merchant going forward without touching the old rule.
    txn = _get_transaction_row(connection, transaction_id)
    if txn["category_id"] is None and txn["entity_id"] is None:
        # The same invariant POST /merchant_rules enforces via
        # MerchantRuleCreate. This path inserts directly, so without this
        # check it could mint the exact rule the other path rejects: one
        # that classifies nothing.
        raise ConflictError(
            f"Transaction {transaction_id!r} has no category or entity to turn "
            "into a rule — correct it first, then apply it"
        )

    existing = connection.execute(
        "SELECT id FROM merchant_rules WHERE pattern = ?", (txn["description"],)
    ).fetchone()
    if existing is not None:
        # Two rules sharing a pattern make categorization depend on row
        # order, since matching picks the first longest match — measured to
        # silently uncategorize a merchant when the loser carried a NULL
        # category. Surfacing the collision keeps editing a rule an explicit
        # act, per runbooks/merchant-rules-conventions.md.
        # The PATTERN is not echoed, and this is the one site in the
        # sweep where the omitted value is not even the caller's own
        # input: they sent a transaction_id, and the pattern is that
        # transaction's description, read out of the database. A
        # statement description is free text from the bank and is
        # exactly what the scrubber treats as capable of carrying an
        # account number -- rows can read like "AB123 XFER-IN [REDACTED]" --
        # so repeating one into an error body would undo the redaction
        # for the rows where it mattered.
        #
        # Nothing diagnostic is lost: the existing rule's id is what the
        # caller needs in order to go and edit it, and the description
        # is one get_transaction away for anyone who wants it.
        raise ConflictError(
            f"A merchant rule already exists for this transaction's "
            f"description (rule id {existing['id']!r}) — edit that rule "
            "instead of creating a second one"
        )

    rule_id = str(uuid4())
    connection.execute(
        "INSERT INTO merchant_rules (id, pattern, category_id, entity_id) "
        "VALUES (?, ?, ?, ?)",
        (rule_id, txn["description"], txn["category_id"], txn["entity_id"]),
    )
    # The second creation path, and the one an endpoint-by-endpoint
    # reading misses. It records exactly as POST /merchant_rules does.
    _record_rule_change(connection, rule_id, body.actor)
    connection.commit()
    return dict(_get_merchant_rule_row(connection, rule_id))


def _period_bounds(period: str) -> tuple[str, str]:
    """Half-open [start, end) date bounds for a 'YYYY' or 'YYYY-MM' period."""
    if len(period) == 4:
        year = int(period)
        return f"{year:04d}-01-01", f"{year + 1:04d}-01-01"
    year, month = (int(part) for part in period.split("-"))
    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1
    return f"{year:04d}-{month:02d}-01", f"{next_year:04d}-{next_month:02d}-01"


# The taxonomy's non-spending branch. Money moving between the owner's own
# accounts is not spending, and it appears TWICE in a cross-account read
# — once leaving one account, once arriving at another — so an aggregate
# that sums both legs reports a purchase that never happened.
#
# The slug, not the display name: a category renamed in the UI must stay
# excluded, and matching on "Transfers" would stop working the moment
# someone edits it. test_a_renamed_transfer_category_is_still_excluded
# pins that, and test_the_transfers_branch_exists pins that this slug is
# real — if the seed ever renamed it, every exclusion below would
# silently become a no-op and the flag would read as working.
TRANSFERS_CATEGORY_ID = "transfers"

# Direct children plus the branch root. The taxonomy is fixed and
# two-level (runbooks/category-taxonomy.md), so this is the whole
# subtree; a recursive CTE would be machinery for a depth the schema
# does not have. If the tree ever grows a third level this must become
# recursive, which is why the depth assumption is written down here
# rather than left in the SQL.
_TRANSFERS_PREDICATE = (
    "category_id IN (SELECT id FROM categories "
    "WHERE id = ? OR parent_id = ?)"
)

# NOT the negation of _TRANSFERS_PREDICATE, and the difference is not
# cosmetic. SQL is three-valued: `NULL IN (...)` is NULL, and `NOT NULL`
# is NULL, so a bare `NOT <predicate>` drops every UNCATEGORIZED row
# instead of keeping it: every unclassified row would be silently absent from every total, while the endpoint
# returns 200 and a smaller, plausible number.
#
# Found by test_aggregates_are_exact_over_many_rows, which imports 100
# uncategorized rows and got an empty summary. Pinned directly by
# test_uncategorized_rows_survive_the_exclusion.
_NOT_TRANSFERS_PREDICATE = f"(category_id IS NULL OR NOT {_TRANSFERS_PREDICATE})"


def _transfers_params() -> tuple:
    return (TRANSFERS_CATEGORY_ID, TRANSFERS_CATEGORY_ID)


def _is_transfer_category(connection: sqlite3.Connection, category_id: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM categories WHERE id = ? AND (id = ? OR parent_id = ?)",
            (category_id, TRANSFERS_CATEGORY_ID, TRANSFERS_CATEGORY_ID),
        ).fetchone()
        is not None
    )


def _excluded_totals(
    connection: sqlite3.Connection, where: str, params: tuple
) -> tuple[int, int]:
    """Signed sum AND row count of what the exclusion removed.

    The count is not redundant with the sum, and the reason is the whole
    point of the feature. Once both legs of a transfer are categorized —
    which is the state we are working towards — they net to zero, so
    `transfers_excluded_cents` reads 0 in exactly the healthy case. A
    lone `0` is indistinguishable from "there were no transfers", which
    puts the exclusion right back to being invisible, which is what the
    field exists to prevent. The count says how many rows were removed
    regardless of what they summed to.
    """
    # The view, like every other aggregate read. This one function feeds
    # BOTH transfers_excluded_cents/count and uncategorized_cents, so an
    # archived row counted here would be reported as excluded-or-
    # uncategorized by two different fields while appearing in no total --
    # the arithmetic would not add up and nothing would say why.
    row = connection.execute(
        f"SELECT COALESCE(SUM(amount_cents), 0), COUNT(*) "  # noqa: S608
        f"FROM active_transactions WHERE {where}",
        params,
    ).fetchone()
    return row[0], row[1]


#: The null-bucket field tracks group_by, in name and predicate. Stated
#: once beside the code that chooses it.
NULL_BUCKET_CONTRACT = (
    "the null-bucket total is uncategorized_cents when grouping by "
    "category and unattributed_cents when grouping by entity; exactly "
    "one appears"
)


#: Stated once, beside the code that enforces it (exclude_transfers=Query(default=True)).
#: Interpolated into the tool description; exercised by a test in
#: api/tests/test_claim_contracts.py.
TRANSFERS_CONTRACT = "transfers between the owner's own accounts are excluded by default"


@router.get("/spending_summary")
def spending_summary(
    period: str | None = Query(default=None, pattern=r"^\d{4}(-\d{2})?$"),
    group_by: str = Query(default="category", pattern="^(category|entity)$"),
    exclude_transfers: bool = Query(default=True),
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    # Sums signed amounts (not just debits) grouped by category or entity.
    # This is deliberate, not an oversight: runbooks/category-taxonomy.md's
    # Transfers category exists specifically so a credit-card payment from
    # checking nets to ~0 across its two legs instead of double-counting
    # as spending — that only works if both legs' signed amounts are summed,
    # not filtered to debits only.
    column = "category_id" if group_by == "category" else "entity_id"
    query = (
        f"SELECT {column} AS key, COALESCE(SUM(amount_cents), 0) AS total "
        "FROM active_transactions WHERE 1=1"
    )
    params: list = []
    period_clause = ""
    period_params: tuple = ()
    if period is not None:
        period_clause = " AND txn_date >= ? AND txn_date < ?"
        period_params = tuple(_period_bounds(period))
        query += period_clause
        params.extend(period_params)

    if exclude_transfers:
        query += f" AND {_NOT_TRANSFERS_PREDICATE}"
        params.extend(_transfers_params())
    query += f" GROUP BY {column} ORDER BY {column}"

    result = paginate(connection, query, tuple(params), limit=limit, offset=offset)
    result["items"] = [
        {"key": item["key"], "total": item["total"]} for item in result["items"]
    ]
    # Always reported, including as 0 when nothing was excluded and when
    # the flag is off. A field that appears only sometimes is a field
    # callers stop looking for, and the number this endpoint returns is
    # not interpretable without knowing what was left out of it.
    result["transfers_excluded_cents"], result["transfers_excluded_count"] = (
        _excluded_totals(
            connection,
            f"1=1{period_clause} AND {_TRANSFERS_PREDICATE}",
            (*period_params, *_transfers_params()),
        )
        if exclude_transfers
        else (0, 0)
    )
    # Reported alongside, because an exclusion that reads as
    # completeness is the failure this whole change exists to close.
    # "transfers_excluded_cents: 12345" invites "so the rest is
    # spending" — but often most transfers are not categorized as
    # transfers at all; they sit in the uncategorized bucket looking like
    # purchases. A reader needs both
    # numbers to know how much of the total is actually classified.
    #
    # No matching count, and the asymmetry is deliberate: the count
    # exists for transfers because two legs of one transfer cancel to
    # zero, so the cents go silent in the healthy case. Uncategorized
    # rows are not paired, so their cents do not collapse that way.
    #
    # THE NAME TRACKS group_by, AND SO DOES WHAT IT SUMS (D48). The
    # `key: null` bucket in `items` means "not grouped", and what "not
    # grouped" means changes with group_by — no category, or no entity.
    # A fixed `uncategorized_cents` put two different nulls in one
    # response: an entity summary carried a null bucket meaning "no
    # entity" beside a field meaning "no category", with different
    # numbers and nothing saying which was which. That was a regression
    # this endpoint shipped in #93.
    #
    # Renaming the null KEY was rejected instead: `uncategorized` is a
    # real category id, and the taxonomy is edited by plain SQL, so any
    # sentinel string is a future category — the collision would merge
    # "no category" with "filed as Uncategorized" and sum them silently.
    # See runbooks/category-taxonomy.md.
    # See NULL_BUCKET_CONTRACT below for the one-line version the tool
    # description carries.
    null_field, null_predicate = (
        ("uncategorized_cents", "category_id IS NULL")
        if group_by == "category"
        else ("unattributed_cents", "entity_id IS NULL")
    )
    # Same filters as `items`, including the transfers exclusion, so the
    # figure always equals the null bucket's total rather than merely
    # resembling it.
    #
    # Observable in ENTITY mode only, and worth saying so: there the
    # null predicate is `entity_id IS NULL` and a transfer row can have
    # no entity, so without this clause the field and its own bucket
    # disagree by exactly the transfers. In category mode the clause is
    # inert — `category_id IS NULL` and "has a category under
    # transfers" cannot both hold — and it is kept there only because
    # one expression for both modes is harder to get wrong than two.
    # test_the_field_equals_the_null_bucket_in_items exercises the
    # reachable case; the fixture carries a transfer with no entity for
    # exactly that reason.
    null_where = f"1=1{period_clause} AND {null_predicate}"
    null_params: tuple = tuple(period_params)
    if exclude_transfers:
        null_where += f" AND {_NOT_TRANSFERS_PREDICATE}"
        null_params = (*null_params, *_transfers_params())
    result[null_field] = _excluded_totals(connection, null_where, null_params)[0]
    return result


#: The clock the trend window opens from. A function, and bound as a
#: parameter rather than written into the SQL as the literal 'now', so a
#: test can freeze it: the bug this replaced was invisible to any test
#: that ran on a single unknown day, because the size of the error IS
#: the day of the month.
def _now_anchor() -> str:
    return "now"


@router.get("/trend")
def trend(
    category_id: str | None = Query(default=None),
    entity_id: str | None = Query(default=None),
    months: int = Query(default=6, ge=1, le=60),
    exclude_transfers: bool = Query(default=True),
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    if (category_id is None) == (entity_id is None):
        raise InvalidReferenceError(
            "Exactly one of category_id or entity_id is required"
        )
    # Validate the id exists. Without this an unknown id produced a 200
    # with an empty series — indistinguishable from "nothing was spent
    # here", which is an answer people act on. A real category with no
    # transactions still returns an empty series, which is legitimate.
    require_reference(connection, "categories", category_id, "category")
    require_reference(connection, "entities", entity_id, "entity")

    column = "category_id" if category_id is not None else "entity_id"
    value = category_id if category_id is not None else entity_id

    # An explicit request for a transfers category beats the default.
    # Asking "how have my credit-card payments trended" and getting an
    # empty series would be the worst answer this endpoint can give: it
    # is indistinguishable from "you made none", which is a thing people
    # act on, and it is the same failure the id-validation above exists
    # to prevent. The flag is about aggregates that SUM ACROSS
    # categories; a single-category series is already the thing asked
    # for.
    requested_transfers = category_id is not None and _is_transfer_category(
        connection, category_id
    )
    applies = exclude_transfers and not requested_transfers

    # The window opens on a MONTH BOUNDARY, and that is the whole of
    # ticket T-18. It used to be `date('now', '-N months')` — a day-
    # precision cutoff feeding a GROUP BY calendar month, so the window
    # opened partway through a month, that month was grouped and
    # labelled whole, and the series gained an extra bucket at the left
    # edge: months=1 and months=2 could report the same calendar month
    # with very different totals, a swing decided by a parameter that only says how far back to look.
    #
    # This is the "is this going up or down" report, so the leftmost
    # point is the baseline everything is read against, and it was
    # understated by however far into the month today happened to be:
    # nearly empty on the 3rd, nearly whole on the 28th.
    #
    # 'start of month' first, then -(N-1) months, gives exactly N
    # buckets: the earliest whole, the latest the current month to
    # date. The current month being partial is unavoidable and is what
    # a reader expects, which is why the description now says so.
    #
    # The anchor is a bound parameter rather than the literal 'now' so
    # a test can freeze the clock and prove the result does not depend
    # on the day of the month — the property that was broken.
    # AN UPPER BOUND TOO, and it is not decoration. The window only ever
    # had a lower one, so a transaction dated in the FUTURE landed in the
    # series whatever `months` said: anchored at 2025-03-20 with
    # months=2, a 2025-09 row produced a third bucket six months ahead.
    # "exactly N buckets" is not true without this, and neither is
    # "the latest bucket is month-to-date" -- found while testing the
    # fix, not reported separately, because the claim this ticket adds
    # to the description is false while it stands.
    window = (_now_anchor(), f"-{months - 1} months")

    query = (
        "SELECT strftime('%Y-%m', txn_date) AS month, "
        "COALESCE(SUM(amount_cents), 0) AS total "
        f"FROM active_transactions WHERE {column} = ? "
        "AND txn_date >= date(?, 'start of month', ?) "
        "AND txn_date <= date(?) "
    )
    params: list = [value, *window, window[0]]
    if applies:
        query += f"AND {_NOT_TRANSFERS_PREDICATE} "
        params.extend(_transfers_params())
    query += "GROUP BY month ORDER BY month"

    result = paginate(connection, query, tuple(params), limit=limit, offset=offset)
    result["items"] = [
        {"month": item["month"], "total": item["total"]} for item in result["items"]
    ]
    # The SAME window, not a second copy of the expression: this figure
    # says how much was excluded FROM THIS SERIES, so a window that
    # disagreed with the one above would be reporting an exclusion from
    # a span the caller is not looking at.
    result["transfers_excluded_cents"], result["transfers_excluded_count"] = (
        _excluded_totals(
            connection,
            f"{column} = ? AND txn_date >= date(?, 'start of month', ?) "
            f"AND txn_date <= date(?) "
            f"AND {_TRANSFERS_PREDICATE}",
            (value, *window, window[0], *_transfers_params()),
        )
        if applies
        else (0, 0)
    )
    # DELIBERATELY NOT RENAMED, unlike spending_summary's (D48). The
    # rename there exists because that response has a `key: null`
    # bucket whose meaning changes with group_by, and the field had to
    # agree with it. A trend's items are MONTHS, so there is no null
    # bucket and nothing to disagree with.
    #
    # And `unattributed_cents` would be wrong here in substance, not
    # just in name: an entity series is already filtered to one entity,
    # so no row in it can be unattributed. The figure is about
    # CATEGORIES either way — how much of this series has none — which
    # is what `uncategorized_cents` says. Renaming it for symmetry with
    # spending_summary would make the name describe something the
    # series cannot contain. Pinned by
    # test_trend_does_not_rename_its_field_for_symmetry.
    #
    # Always 0 for a category series, since those rows are selected BY
    # category. Reported anyway rather than omitted: a field that
    # appears only sometimes is a field callers stop reading.
    #
    # No transfers clause here, and that is a fact about the predicates
    # rather than an omission. A row counted below has `category_id IS
    # NULL`; a row excluded as a transfer has a category IN the
    # transfers branch. The two cannot both hold, so the clause could
    # never change this number — it would be code no mutation can
    # distinguish, which is worse than absent because it reads as
    # load-bearing and the next person preserves it.
    #
    # spending_summary DOES need it, in entity mode only: there the
    # null predicate is `entity_id IS NULL`, and a transfer row can
    # have no entity. Same-looking clause, different reachability.
    result["uncategorized_cents"] = _excluded_totals(
        connection,
        f"{column} = ? AND txn_date >= date('now', ?) AND category_id IS NULL",
        (value, f"-{months} months"),
    )[0]
    return result


@router.get("/entities/{entity_id}/cost_of_ownership")
def cost_of_ownership(
    entity_id: str,
    period: str | None = Query(default=None, pattern=r"^\d{4}(-\d{2})?$"),
    connection: sqlite3.Connection = Depends(get_connection),
) -> dict:
    """Total cost booked against an entity, directly or via its financing.

    ATTRIBUTION IS WHOLE-COST, NOT PRO-RATA, and that is a deliberate
    tradeoff rather than an oversight. Every transaction booked under an
    entity that finances or insures this one counts in full here — so a
    single policy account covering two vehicles contributes its entire
    premium to *each* of them.

    The per-entity number is the defensible reading ("what does this car
    cost me, counting the policy that covers it"), and apportioning would
    need a per-asset share the schema does not store. The consequence to
    know: summing cost_of_ownership across entities double-counts any
    shared policy or loan. Revisit if cross-entity totals are ever built
    on top of this.
    """
    entity = connection.execute(
        "SELECT id FROM entities WHERE id = ?", (entity_id,)
    ).fetchone()
    if entity is None:
        raise NotFoundError(f"No entity with id {entity_id!r}")

    # Direct costs (entity_id on the transaction itself) plus costs booked
    # under any entity that finances or insures this one (e.g. a loan or
    # policy account whose premium/payment transactions are tagged with
    # THAT account's id, not the car's).
    linked_entity_ids = [
        row["from_entity_id"]
        for row in connection.execute(
            "SELECT from_entity_id FROM entity_relationships "
            "WHERE to_entity_id = ? AND relationship_type IN ('finances', 'insures')",
            (entity_id,),
        ).fetchall()
    ]
    entity_ids = [entity_id, *linked_entity_ids]
    placeholders = ",".join("?" for _ in entity_ids)

    # Every OTHER entity the same linked accounts also cover. The total
    # below counts each linked account's transactions in FULL, so when a
    # policy covers two cars both answers include the whole premium and
    # adding them double-counts it. Reported rather than apportioned
    # (D66): the per-entity number answers the question actually asked —
    # "what does this car cost me, counting the policy that covers it" —
    # and halving it would answer a question nobody asked while making
    # the ordinary single-entity case silently wrong.
    #
    # ENDED RELATIONSHIPS ARE INCLUDED, and that is not an oversight. The
    # total does not filter on `end_date`, so a lapsed policy's premiums
    # are still in both entities' figures and still double-count. A
    # `shared_with` that dropped them would under-report an overlap the
    # number beside it contains — the field lying about its own total,
    # which is the defect D48 fixed on spending_summary. Each entry
    # carries its `end_date` so a caller can tell current cover from
    # historical, without the count quietly disagreeing with the money.
    shared_rows = []
    if linked_entity_ids:
        link_placeholders = ",".join("?" for _ in linked_entity_ids)
        shared_rows = connection.execute(
            f"SELECT r.from_entity_id AS via_entity_id, v.name AS via_name, "  # noqa: S608
            f"r.relationship_type, r.end_date, "
            f"e.id AS entity_id, e.type AS entity_type, e.name AS entity_name "
            f"FROM entity_relationships r "
            f"JOIN entities e ON e.id = r.to_entity_id "
            f"JOIN entities v ON v.id = r.from_entity_id "
            f"WHERE r.from_entity_id IN ({link_placeholders}) "
            f"AND r.to_entity_id != ? "
            f"AND r.relationship_type IN ('finances', 'insures') "
            # Total key: two accounts can cover one entity, and a caller
            # comparing two responses needs the order decided rather than
            # inherited from scan order.
            f"ORDER BY e.name COLLATE NOCASE, v.name COLLATE NOCASE, r.id",
            (*linked_entity_ids, entity_id),
        ).fetchall()

    shared_with = [
        {
            "entity_id": row["entity_id"],
            "entity_type": row["entity_type"],
            "entity_name": row["entity_name"],
            "via_entity_id": row["via_entity_id"],
            "via_entity_name": row["via_name"],
            "relationship_type": row["relationship_type"],
            "end_date": row["end_date"],
        }
        for row in shared_rows
    ]

    query = (
        f"SELECT category_id, amount_cents FROM active_transactions "
        f"WHERE entity_id IN ({placeholders})"
    )
    params: list = list(entity_ids)
    if period is not None:
        query += " AND txn_date >= ? AND txn_date < ?"
        params.extend(_period_bounds(period))

    rows = connection.execute(query, tuple(params)).fetchall()
    breakdown: dict[str | None, float] = {}
    for row in rows:
        breakdown[row["category_id"]] = (
            breakdown.get(row["category_id"], 0) + row["amount_cents"]
        )

    return {
        "entity_id": entity_id,
        "period": period,
        "total": sum(breakdown.values()),
        # Distinct entities, not rows: two accounts covering the same
        # other car is one overlapping entity, and a caller checking
        # "is this safe to add" wants the answer in entities.
        "shared_cost_entities": len({item["entity_id"] for item in shared_with}),
        "shared_with": shared_with,
        "breakdown": [
            {"category_id": category_id, "total": total}
            for category_id, total in breakdown.items()
        ],
    }

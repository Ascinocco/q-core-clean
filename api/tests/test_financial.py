
from tests.source_import_support import seed_source_batch
import sqlite3
from datetime import date

import pytest
def _auth(test_settings):
    return {"Authorization": f"Bearer {test_settings.api_token}"}


def test_create_category_slugifies_the_name(client, test_settings):
    response = client.post(
        "/categories", json={"name": "Streaming Services"}, headers=_auth(test_settings)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "streaming_services"
    assert body["name"] == "Streaming Services"
    assert body["parent_id"] is None


def test_create_category_appends_numeric_suffix_on_slug_collision(
    client, test_settings
):
    headers = _auth(test_settings)
    first = client.post("/categories", json={"name": "Widgets"}, headers=headers)
    second = client.post("/categories", json={"name": "Widgets!!"}, headers=headers)
    assert first.json()["id"] == "widgets"
    assert second.json()["id"] == "widgets_2"


def test_create_child_category_slug_is_prefixed_with_parent_id(client, test_settings):
    # Matches db/seed_categories.sql's convention (auto_insurance,
    # housing_utilities, ...) — several names legitimately recur under
    # different parents, so a bare slugify(name) would collide. "Detailing"
    # isn't in the seed data under any parent, so this exercises the
    # prefix path without also hitting the numeric-suffix fallback.
    response = client.post(
        "/categories",
        json={"name": "Detailing", "parent_id": "auto"},
        headers=_auth(test_settings),
    )
    assert response.json()["id"] == "auto_detailing"


def test_get_category_returns_404_for_unknown_id(client, test_settings):
    response = client.get("/categories/does-not-exist", headers=_auth(test_settings))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_list_categories_includes_seeded_taxonomy(client, test_settings):
    response = client.get("/categories?limit=200", headers=_auth(test_settings))
    listing = response.json()
    # One call at MAX_LIMIT, and the fixture is db/seed_categories.sql -- a
    # file outside this test that can grow. If it ever passes 200, the two
    # membership assertions below would be checking a truncated page and
    # could fail for a reason that has nothing to do with the taxonomy. This
    # makes that failure loud now instead of confusing later.
    assert listing["total"] <= 200, (
        f"{listing['total']} categories exceeds one page -- this read-back "
        "must drain before the assertions below mean anything"
    )
    ids = [item["id"] for item in listing["items"]]
    assert "housing" in ids
    assert "food" in ids


def test_patch_category_updates_name_and_parent(client, test_settings):
    headers = _auth(test_settings)
    created = client.post(
        "/categories", json={"name": "Parking"}, headers=headers
    ).json()
    response = client.patch(
        f"/categories/{created['id']}",
        json={"parent_id": "auto"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["parent_id"] == "auto"


def test_delete_category_blocked_by_foreign_key(client, test_settings):
    # "housing" is seeded and merchant_rules/transactions can reference it;
    # deleting a category still in use by the seeded taxonomy's own
    # children is blocked the same way entity deletion is blocked.
    headers = _auth(test_settings)
    response = client.delete("/categories/housing", headers=headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


def test_create_category_with_unknown_parent_is_a_400_not_a_500(client, test_settings):
    """An unknown parent_id must not escape as a bare 500.

    parent_id is a FK, so an unknown one raises sqlite3.IntegrityError out
    of the route — uncaught, that is a 500 with no error envelope, which
    the plan's own global constraint rules out ("every error path uses
    api/errors.py's ... InvalidReferenceError"). Same shape entities.py
    already handles for relationship targets.
    """
    response = client.post(
        "/categories",
        json={"name": "Ghost Child", "parent_id": "no_such_parent"},
        headers=_auth(test_settings),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_reference"
    assert "no_such_parent" in response.json()["error"]["message"]


def test_patch_category_with_unknown_parent_is_a_400_not_a_500(client, test_settings):
    headers = _auth(test_settings)
    created = client.post(
        "/categories", json={"name": "Parking"}, headers=headers
    ).json()

    response = client.patch(
        f"/categories/{created['id']}",
        json={"parent_id": "no_such_parent"},
        headers=headers,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_reference"


def test_create_statement_for_known_account(client, test_settings):
    headers = _auth(test_settings)
    account = client.post(
        "/entities", json={"type": "account", "name": "Checking"}, headers=headers
    ).json()
    response = client.post(
        "/statements",
        json={
            "account_id": account["id"],
            "period_start": "2026-01-01",
            "period_end": "2026-01-31",
        },
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["account_id"] == account["id"]


def test_create_statement_rejects_unknown_account(client, test_settings):
    response = client.post(
        "/statements",
        json={
            "account_id": "does-not-exist",
            "period_start": "2026-01-01",
            "period_end": "2026-01-31",
        },
        headers=_auth(test_settings),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_reference"


def test_create_statement_rejects_duplicate_period(client, test_settings):
    headers = _auth(test_settings)
    account = client.post(
        "/entities", json={"type": "account", "name": "Savings"}, headers=headers
    ).json()
    body = {
        "account_id": account["id"],
        "period_start": "2026-02-01",
        "period_end": "2026-02-28",
    }
    client.post("/statements", json=body, headers=headers)
    response = client.post("/statements", json=body, headers=headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


def test_get_statement_returns_404_for_unknown_id(client, test_settings):
    response = client.get("/statements/does-not-exist", headers=_auth(test_settings))
    assert response.status_code == 404


def test_list_statements_filters_by_account(client, test_settings):
    headers = _auth(test_settings)
    account_a = client.post(
        "/entities", json={"type": "account", "name": "Account A"}, headers=headers
    ).json()
    account_b = client.post(
        "/entities", json={"type": "account", "name": "Account B"}, headers=headers
    ).json()
    client.post(
        "/statements",
        json={
            "account_id": account_a["id"],
            "period_start": "2026-03-01",
            "period_end": "2026-03-31",
        },
        headers=headers,
    )
    client.post(
        "/statements",
        json={
            "account_id": account_b["id"],
            "period_start": "2026-03-01",
            "period_end": "2026-03-31",
        },
        headers=headers,
    )
    response = client.get(f"/statements?account_id={account_a['id']}", headers=headers)
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["account_id"] == account_a["id"]


def test_create_statement_rejects_an_entity_that_is_not_an_account(
    client, test_settings
):
    """statements.account_id is REFERENCES entities(id) — any entity — so the
    FK alone happily accepts a statement against a pet. account_subtype
    already covers loan/insurance/credit_card under type 'account', so a
    statement against any other type is always an error, never leniency."""
    headers = _auth(test_settings)
    pet = client.post(
        "/entities", json={"type": "pet", "name": "Rufus"}, headers=headers
    ).json()

    response = client.post(
        "/statements",
        json={
            "account_id": pet["id"],
            "period_start": "2026-01-01",
            "period_end": "2026-01-31",
        },
        headers=headers,
    )

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "invalid_reference"
    # The message must say what it actually is — "no such account" would be
    # actively misleading when the entity exists and is simply the wrong kind.
    assert "pet" in body["error"]["message"]


def test_create_statement_rejects_a_reversed_period(client, test_settings):
    headers = _auth(test_settings)
    account = client.post(
        "/entities", json={"type": "account", "name": "Chk"}, headers=headers
    ).json()

    response = client.post(
        "/statements",
        json={
            "account_id": account["id"],
            "period_start": "2026-06-30",
            "period_end": "2026-06-01",
        },
        headers=headers,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_create_statement_allows_a_single_day_period(client, test_settings):
    """The check rejects end < start, not end == start — a one-day period is
    unusual but not wrong, and rejecting it would be a silent narrowing."""
    headers = _auth(test_settings)
    account = client.post(
        "/entities", json={"type": "account", "name": "OneDay"}, headers=headers
    ).json()

    response = client.post(
        "/statements",
        json={
            "account_id": account["id"],
            "period_start": "2026-04-01",
            "period_end": "2026-04-01",
        },
        headers=headers,
    )

    assert response.status_code == 200


def _account(client, headers):
    return client.post(
        "/entities", json={"type": "account", "name": "Checking"}, headers=headers
    ).json()


def test_import_statement_creates_transactions_and_finds_statement(
    client, test_settings
):
    headers = _auth(test_settings)
    account = _account(client, headers)
    response = seed_source_batch(client, json={
            "account_id": account["id"],
            "period_start": "2026-01-01",
            "period_end": "2026-01-31",
            "transactions": [
                {
                    "txn_date": "2026-01-05",
                    "description": "COFFEE SHOP",
                    "amount_cents": -450,
                },
                {"txn_date": "2026-01-10", "description": "PAYCHECK", "amount_cents": 200000},
            ],
        }, headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["created"] == 2
    assert body["skipped_duplicates"] == 0
    assert len(body["unmatched"]) == 2  # no merchant_rules exist yet


def test_import_statement_is_idempotent_on_reimport(client, test_settings):
    headers = _auth(test_settings)
    account = _account(client, headers)
    payload = {
        "account_id": account["id"],
        "period_start": "2026-02-01",
        "period_end": "2026-02-28",
        "transactions": [
            {"txn_date": "2026-02-03", "description": "GROCERY STORE", "amount_cents": -6000}
        ],
    }
    first = seed_source_batch(client, json=payload, headers=headers)
    second = seed_source_batch(client, json=payload, headers=headers)
    assert first.json()["created"] == 1
    assert second.json()["created"] == 0
    assert second.json()["skipped_duplicates"] == 1
    # same statement, not a new one
    assert first.json()["statement_id"] == second.json()["statement_id"]


def test_import_statement_ordinal_tiebreak_keeps_identical_transactions_distinct(
    client, test_settings
):
    # Two genuinely identical same-day/same-amount/same-description
    # transactions in one statement must both be kept, not deduped into
    # one — this is a project-management.json todo.
    headers = _auth(test_settings)
    account = _account(client, headers)
    response = seed_source_batch(client, json={
            "account_id": account["id"],
            "period_start": "2026-03-01",
            "period_end": "2026-03-31",
            "transactions": [
                {
                    "txn_date": "2026-03-05",
                    "description": "VENDING MACHINE",
                    "amount_cents": -200,
                },
                {
                    "txn_date": "2026-03-05",
                    "description": "VENDING MACHINE",
                    "amount_cents": -200,
                },
            ],
        }, headers=headers)
    assert response.json()["created"] == 2


def test_import_statement_applies_longest_matching_merchant_rule(client, test_settings):
    headers = _auth(test_settings)
    account = _account(client, headers)
    # Assert the setup succeeded before relying on it. Until Task 6 adds
    # POST /merchant_rules these 404, and without this the test would fail
    # later on a confusing assertion about `unmatched` instead of saying
    # plainly that its setup never happened.
    broad = client.post(
        "/merchant_rules",
        json={"pattern": "AMAZON", "category_id": "personal_shopping", "actor": "test"},
        headers=headers,
    )
    specific = client.post(
        "/merchant_rules",
        json={"pattern": "AMAZON WEB SERVICES", "category_id": "business", "actor": "test"},
        headers=headers,
    )
    assert broad.status_code == 200, f"rule setup failed: {broad.status_code}"
    assert specific.status_code == 200, f"rule setup failed: {specific.status_code}"
    response = seed_source_batch(client, json={
            "account_id": account["id"],
            "period_start": "2026-04-01",
            "period_end": "2026-04-30",
            "transactions": [
                {
                    "txn_date": "2026-04-02",
                    "description": "AMAZON WEB SERVICES AWS.AMAZON.COM",
                    "amount_cents": -1200,
                }
            ],
        }, headers=headers)
    body = response.json()
    assert body["created"] == 1
    assert body["unmatched"] == []
    # Asserting which rule won, not just that some rule did. Until this
    # line the test named "longest matching rule" passed identically
    # under shortest-wins: both patterns carry a category, so either
    # winner leaves `unmatched` empty and `created` at 1. Verified by
    # mutation — flipping the matcher's ORDER BY to `length(pattern) ASC`
    # left it green.
    txn = client.get(
        f"/transactions?account_id={account['id']}", headers=headers
    ).json()["items"][0]
    assert txn["category_id"] == "business", (
        "the longer pattern AMAZON WEB SERVICES must win over AMAZON"
    )


def test_import_statement_matches_merchant_rule_case_insensitively(
    client, test_settings
):
    # A rule typed in mixed case must still match an all-caps bank
    # description (or vice versa) — otherwise it silently never matches,
    # with no error and no unmatched flag to surface the miss.
    headers = _auth(test_settings)
    account = _account(client, headers)
    rule = client.post(
        "/merchant_rules",
        json={"pattern": "Netflix", "category_id": "entertainment", "actor": "test"},
        headers=headers,
    )
    assert rule.status_code == 200, f"rule setup failed: {rule.status_code}"
    response = seed_source_batch(client, json={
            "account_id": account["id"],
            "period_start": "2026-04-01",
            "period_end": "2026-04-30",
            "transactions": [
                {
                    "txn_date": "2026-04-05",
                    "description": "NETFLIX.COM",
                    "amount_cents": -1599,
                }
            ],
        }, headers=headers)
    assert response.json()["unmatched"] == []


def test_import_statement_is_atomic_on_mid_batch_failure(
    client, test_settings, monkeypatch
):
    """A failed import must leave nothing behind — not even the statement.

    The plan calls this an "atomic statement import", but nothing asserted
    it: atomicity here is implicit, resting on get_connection closing
    without committing. That is easy to break by adding a well-meaning
    commit inside the loop, and the damage would be a half-imported
    statement that a later re-import would then treat as partly-present.
    """
    import sqlite3 as _sqlite3

    import api.financial

    headers = _auth(test_settings)
    account = _account(client, headers)

    # Repurposed from monkeypatching _row_hash, which was removed with the
    # column (a todo / D14). _match_merchant_rule is the replacement
    # because it is called once per row on the same path, so failing its
    # second call still interrupts the batch mid-way — which is what this
    # test is about, not which function happens to raise.
    real_match = api.financial._match_merchant_rule
    calls = {"count": 0}

    def fail_on_second(*args):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("simulated mid-import failure")
        return real_match(*args)

    monkeypatch.setattr(api.financial, "_match_merchant_rule", fail_on_second)

    # Asserting the failure is *reported* matters as much as the rollback:
    # an import that silently swallowed it would claim success for a
    # statement it never wrote.
    #
    # This used to assert the RuntimeError propagated out of TestClient,
    # which was really asserting a test-harness artifact -- under uvicorn
    # the caller never saw the exception, only a 500. Since a todo's
    # catch-all, an unhandled exception is converted to this API's error
    # envelope inside the app, so the check is now the response a real
    # caller actually gets. The rollback assertions below are unchanged,
    # and still the point of the test.
    response = seed_source_batch(client, json={
            "account_id": account["id"],
            "period_start": "2026-05-01",
            "period_end": "2026-05-31",
            "transactions": [
                {"txn_date": "2026-05-01", "description": "A", "amount_cents": -100},
                {"txn_date": "2026-05-02", "description": "B", "amount_cents": -200},
            ],
        }, headers=headers)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"

    connection = _sqlite3.connect(test_settings.db_path)
    try:
        statements = connection.execute("SELECT COUNT(*) FROM statements").fetchone()[0]
        transactions = connection.execute(
            "SELECT COUNT(*) FROM transactions"
        ).fetchone()[0]
    finally:
        connection.close()

    assert statements == 0, "a failed import left a statement behind"
    assert transactions == 0, "a failed import left transactions behind"


def test_import_statement_flags_entity_only_rule_matches_as_unmatched(
    client, test_settings
):
    """`unmatched` means "still needs a category", not "no rule fired".

    A rule with entity_id but no category_id is legitimate — tagging a
    charge to a property without asserting what kind of spending it is. But
    keying `unmatched` on whether a rule matched would mark such a
    transaction handled while leaving category_id NULL, so it would never
    be surfaced for a decision and would accumulate silently as
    permanently uncategorized. runbooks/category-taxonomy.md expects
    uncategorized spending to trend toward zero, and this list is the
    mechanism that makes that possible.
    """
    headers = _auth(test_settings)
    account = _account(client, headers)
    property_entity = client.post(
        "/entities", json={"type": "property", "name": "Rental"}, headers=headers
    ).json()
    rule = client.post(
        "/merchant_rules",
        json={"pattern": "HOME DEPOT", "entity_id": property_entity["id"], "actor": "test"},
        headers=headers,
    )
    assert rule.status_code == 200, "entity-only rules must be allowed"

    response = seed_source_batch(client, json={
            "account_id": account["id"],
            "period_start": "2026-08-01",
            "period_end": "2026-08-31",
            "transactions": [
                {
                    "txn_date": "2026-08-02",
                    "description": "HOME DEPOT #123",
                    "amount_cents": -5000,
                }
            ],
        }, headers=headers)

    body = response.json()
    assert body["created"] == 1
    # Surfaced for a category decision...
    assert len(body["unmatched"]) == 1
    assert body["unmatched"][0]["category_id"] is None
    # ...while the rule's entity tagging still took effect.
    assert body["unmatched"][0]["entity_id"] == property_entity["id"]


def _imported_transaction(client, headers, **overrides):
    account = _account(client, headers)
    body = {
        "account_id": account["id"],
        "period_start": "2026-05-01",
        "period_end": "2026-05-31",
        "transactions": [
            {
                "txn_date": "2026-05-10",
                "description": "HARDWARE STORE",
                "amount_cents": -3500,
            }
        ],
    }
    body.update(overrides)
    response = seed_source_batch(client, json=body, headers=headers)
    txn_id = response.json()["unmatched"][0]["id"]
    return account, txn_id


def test_get_transaction_returns_404_for_unknown_id(client, test_settings):
    response = client.get("/transactions/does-not-exist", headers=_auth(test_settings))
    assert response.status_code == 404


def test_list_transactions_filters_by_account(client, test_settings):
    headers = _auth(test_settings)
    account, txn_id = _imported_transaction(client, headers)
    response = client.get(f"/transactions?account_id={account['id']}", headers=headers)
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == txn_id


def test_list_transactions_filters_by_date_range(client, test_settings):
    headers = _auth(test_settings)
    account, txn_id = _imported_transaction(client, headers)
    in_range = client.get(
        f"/transactions?account_id={account['id']}"
        "&date_from=2026-05-01&date_to=2026-05-31",
        headers=headers,
    )
    out_of_range = client.get(
        f"/transactions?account_id={account['id']}"
        "&date_from=2026-06-01&date_to=2026-06-30",
        headers=headers,
    )
    assert in_range.json()["total"] == 1
    assert out_of_range.json()["total"] == 0


def test_patch_transaction_corrects_category_only(client, test_settings):
    headers = _auth(test_settings)
    _, txn_id = _imported_transaction(client, headers)
    response = client.patch(
        f"/transactions/{txn_id}", json={"category_id": "housing"}, headers=headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["category_id"] == "housing"
    assert body["description"] == "HARDWARE STORE"  # untouched


def test_patch_transaction_rejects_unknown_category(client, test_settings):
    """category_id is a FK, so an unknown one would otherwise raise
    sqlite3.IntegrityError out of the route as a bare 500."""
    headers = _auth(test_settings)
    _, txn_id = _imported_transaction(client, headers)
    response = client.patch(
        f"/transactions/{txn_id}", json={"category_id": "no_such_cat"}, headers=headers
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_reference"


def test_patch_transaction_rejects_unknown_entity(client, test_settings):
    headers = _auth(test_settings)
    _, txn_id = _imported_transaction(client, headers)
    response = client.patch(
        f"/transactions/{txn_id}", json={"entity_id": "no_such_entity"}, headers=headers
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_reference"


def test_patch_transaction_cannot_rewrite_the_imported_record(client, test_settings):
    """Corrections may set category/entity, never the source facts.

    txn_date, description and amount come from the statement document. If
    they were patchable the stored record could silently diverge from the
    paper it was imported from, which defeats the point of keeping it. The
    plan states this; nothing asserted it, and it holds only as long as
    TransactionUpdate keeps extra="forbid" — one relaxed model_config away
    from being silently lost.
    """
    headers = _auth(test_settings)
    _, txn_id = _imported_transaction(client, headers)

    for field, value in (
        ("description", "TAMPERED"),
        ("amount", -1.0),
        ("txn_date", "2020-01-01"),
        ("statement_id", "somewhere-else"),
    ):
        response = client.patch(
            f"/transactions/{txn_id}", json={field: value}, headers=headers
        )
        assert response.status_code == 422, f"{field} must not be patchable"

    unchanged = client.get(f"/transactions/{txn_id}", headers=headers).json()
    assert unchanged["description"] == "HARDWARE STORE"
    assert unchanged["amount_cents"] == -3500
    assert unchanged["txn_date"] == "2026-05-10"


def test_list_transactions_rejects_a_malformed_date_filter(client, test_settings):
    """Untyped date filters fail silently and confidently.

    As plain strings these went straight into a SQL string comparison, so
    `date_from=not-a-date` returned 200 with an empty page — indistinguishable
    from "you have no transactions" — and a US-formatted `05/01/2026`
    returned *everything*, because '2026-05-10' >= '05/01/2026' holds
    lexically. The second is the dangerous one: a plausible-looking result
    set that quietly ignored the filter.
    """
    headers = _auth(test_settings)
    _imported_transaction(client, headers)

    for bad in ("not-a-date", "05/01/2026", "2026-13-01"):
        response = client.get(f"/transactions?date_from={bad}", headers=headers)
        assert response.status_code == 422, f"{bad!r} was accepted"
        assert response.json()["error"]["code"] == "validation_error"


def test_list_transactions_still_filters_on_valid_dates(client, test_settings):
    """The counterweight: rejecting malformed input must not narrow valid
    input. ISO dates keep working exactly as before."""
    headers = _auth(test_settings)
    account, txn_id = _imported_transaction(client, headers)

    hit = client.get(
        f"/transactions?account_id={account['id']}&date_from=2026-05-10"
        "&date_to=2026-05-10",
        headers=headers,
    )
    miss = client.get(
        f"/transactions?account_id={account['id']}&date_from=2026-05-11",
        headers=headers,
    )

    assert hit.json()["total"] == 1
    assert hit.json()["items"][0]["id"] == txn_id
    assert miss.json()["total"] == 0


def test_create_merchant_rule(client, test_settings):
    response = client.post(
        "/merchant_rules",
        json={"pattern": "NETFLIX", "category_id": "entertainment", "actor": "test"},
        headers=_auth(test_settings),
    )
    assert response.status_code == 200
    assert response.json()["pattern"] == "NETFLIX"


def test_create_merchant_rule_rejects_a_rule_with_no_target(client, test_settings):
    response = client.post(
        "/merchant_rules", json={"pattern": "NETFLIX"}, headers=_auth(test_settings)
    )
    assert response.status_code == 422


def test_get_merchant_rule_returns_404_for_unknown_id(client, test_settings):
    response = client.get(
        "/merchant_rules/does-not-exist", headers=_auth(test_settings)
    )
    assert response.status_code == 404


def test_list_merchant_rules_paginates(client, test_settings):
    headers = _auth(test_settings)
    for i in range(3):
        client.post(
            "/merchant_rules",
            json={"pattern": f"MERCHANT {i}", "category_id": "food", "actor": "test"},
            headers=headers,
        )
    response = client.get("/merchant_rules?limit=2", headers=headers)
    body = response.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2


def test_patch_merchant_rule_fixes_the_rule_itself(client, test_settings):
    headers = _auth(test_settings)
    created = client.post(
        "/merchant_rules",
        json={"pattern": "STARBKS", "category_id": "food", "actor": "test"},
        headers=headers,
    ).json()
    response = client.patch(
        f"/merchant_rules/{created['id']}",
        json={"pattern": "STARBUCKS", "actor": "test"},
        headers=headers,
    )
    assert response.json()["pattern"] == "STARBUCKS"


def test_delete_merchant_rule(client, test_settings):
    headers = _auth(test_settings)
    created = client.post(
        "/merchant_rules",
        json={"pattern": "TEMP RULE", "category_id": "food", "actor": "test"},
        headers=headers,
    ).json()
    response = client.delete(f"/merchant_rules/{created['id']}?actor=test", headers=headers)
    assert response.json() == {"deleted": True}
    assert (
        client.get(f"/merchant_rules/{created['id']}", headers=headers).status_code
        == 404
    )


def test_create_merchant_rule_rejects_unknown_category(client, test_settings):
    """category_id/entity_id are FKs — unknown values would be bare 500s."""
    response = client.post(
        "/merchant_rules",
        json={"pattern": "X", "category_id": "no_such_cat", "actor": "test"},
        headers=_auth(test_settings),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_reference"


def test_patch_merchant_rule_rejects_unknown_entity(client, test_settings):
    headers = _auth(test_settings)
    created = client.post(
        "/merchant_rules",
        json={"pattern": "Y", "category_id": "food", "actor": "test"},
        headers=headers,
    ).json()
    response = client.patch(
        f"/merchant_rules/{created['id']}",
        json={"entity_id": "no_such_entity", "actor": "test"},
        headers=headers,
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_reference"


def test_apply_as_rule_creates_a_new_specific_rule_not_editing_existing(
    client, test_settings
):
    # runbooks/merchant-rules-conventions.md: correcting one transaction
    # never silently rewrites an existing broad rule.
    headers = _auth(test_settings)
    client.post(
        "/merchant_rules",
        json={"pattern": "AMAZON", "category_id": "personal_shopping", "actor": "test"},
        headers=headers,
    )
    account = _account(client, headers)
    seed_source_batch(client, json={
            "account_id": account["id"],
            "period_start": "2026-06-01",
            "period_end": "2026-06-30",
            "transactions": [
                {
                    "txn_date": "2026-06-01",
                    "description": "AMAZON MKTPLACE PMTS",
                    "amount_cents": -2000,
                }
            ],
        }, headers=headers)
    # "AMAZON MKTPLACE PMTS" contains "AMAZON" as a substring, so import
    # auto-matched it to the broad rule above (category personal_shopping)
    # — it's NOT in the import response's unmatched list. Fetch it back to
    # correct it, then promote the correction to a rule.
    txn_id = client.get(
        f"/transactions?account_id={account['id']}", headers=headers
    ).json()["items"][0]["id"]
    client.patch(
        f"/transactions/{txn_id}", json={"category_id": "business"}, headers=headers
    )
    response = client.post(
        f"/transactions/{txn_id}/apply_as_rule",
        json={"actor": "test"},
        headers=headers,
    )
    assert response.status_code == 200
    new_rule = response.json()
    assert new_rule["pattern"] == "AMAZON MKTPLACE PMTS"
    assert new_rule["category_id"] == "business"

    # The original broad "AMAZON" rule is untouched.
    rules = client.get("/merchant_rules?limit=200", headers=headers).json()["items"]
    amazon_rule = next(r for r in rules if r["pattern"] == "AMAZON")
    assert amazon_rule["category_id"] == "personal_shopping"


def _seed_categorized_transactions(client, headers):
    account = _account(client, headers)
    seed_source_batch(client, json={
            "account_id": account["id"],
            "period_start": "2026-07-01",
            "period_end": "2026-07-31",
            "transactions": [
                {"txn_date": "2026-07-02", "description": "RENT", "amount_cents": -150000},
                {"txn_date": "2026-07-10", "description": "PAYCHECK", "amount_cents": 300000},
            ],
        }, headers=headers)
    txns = client.get(
        f"/transactions?account_id={account['id']}", headers=headers
    ).json()["items"]
    rent = next(t for t in txns if t["description"] == "RENT")
    pay = next(t for t in txns if t["description"] == "PAYCHECK")
    client.patch(
        f"/transactions/{rent['id']}", json={"category_id": "housing"}, headers=headers
    )
    client.patch(
        f"/transactions/{pay['id']}", json={"category_id": "income"}, headers=headers
    )
    return account


def test_spending_summary_groups_by_category(client, test_settings):
    headers = _auth(test_settings)
    _seed_categorized_transactions(client, headers)
    response = client.get(
        "/spending_summary?period=2026-07&group_by=category", headers=headers
    )
    assert response.status_code == 200
    items = {item["key"]: item["total"] for item in response.json()["items"]}
    assert items["housing"] == -150000
    assert items["income"] == 300000


def test_spending_summary_filters_to_the_requested_period(client, test_settings):
    headers = _auth(test_settings)
    _seed_categorized_transactions(client, headers)
    response = client.get(
        "/spending_summary?period=2026-08&group_by=category", headers=headers
    )
    assert response.json()["items"] == []


def test_trend_returns_one_bucket_per_month_for_a_category(client, test_settings):
    """Seeded relative to today, deliberately.

    /trend's window is `txn_date >= date('now', '-N months')`, so a test
    with a hardcoded transaction date only passes while real time happens
    to sit near it. The plan's version seeded 2026-07-02 and asserted it
    appeared with months=3 — true when written, false from 2026-10-02
    onward, at which point the test fails for reasons having nothing to do
    with the code. Anchoring the fixture to today keeps it testing the
    aggregation instead of the calendar.
    """
    from datetime import date

    headers = _auth(test_settings)
    account = _account(client, headers)
    first_of_this_month = date.today().replace(day=1)
    month_key = first_of_this_month.strftime("%Y-%m")

    seed_source_batch(client, json={
            "account_id": account["id"],
            "period_start": first_of_this_month.isoformat(),
            "period_end": date.today().isoformat(),
            "transactions": [
                {
                    "txn_date": first_of_this_month.isoformat(),
                    "description": "RENT",
                    "amount_cents": -150000,
                }
            ],
        }, headers=headers)
    txn = client.get(
        f"/transactions?account_id={account['id']}", headers=headers
    ).json()["items"][0]
    client.patch(
        f"/transactions/{txn['id']}", json={"category_id": "housing"}, headers=headers
    )

    response = client.get("/trend?category_id=housing&months=3", headers=headers)

    assert response.status_code == 200
    months = {item["month"]: item["total"] for item in response.json()["items"]}
    assert months.get(month_key) == -150000


def test_trend_requires_exactly_one_of_category_or_entity(client, test_settings):
    response = client.get("/trend?months=3", headers=_auth(test_settings))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_reference"

    both = client.get(
        "/trend?category_id=housing&entity_id=abc&months=3",
        headers=_auth(test_settings),
    )
    assert both.status_code == 400


def test_apply_as_rule_rejects_a_transaction_with_no_classification(
    client, test_settings
):
    """apply_as_rule must enforce what POST /merchant_rules enforces.

    It inserts directly, so it bypassed MerchantRuleCreate's "needs a
    target" rule: a transaction with neither category nor entity produced a
    rule with both NULL — the very rule the API rejects with 422 on the
    other path. Same invariant, two entry points; it has to hold on both.
    """
    headers = _auth(test_settings)
    account = _account(client, headers)
    imported = seed_source_batch(client, json={
            "account_id": account["id"],
            "period_start": "2026-09-01",
            "period_end": "2026-09-30",
            "transactions": [
                {
                    "txn_date": "2026-09-02",
                    "description": "MYSTERY MERCHANT",
                    "amount_cents": -900,
                }
            ],
        }, headers=headers).json()
    txn_id = imported["unmatched"][0]["id"]

    response = client.post(
        f"/transactions/{txn_id}/apply_as_rule",
        json={"actor": "test"},
        headers=headers,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"
    assert client.get("/merchant_rules", headers=headers).json()["total"] == 0


def test_apply_as_rule_rejects_a_duplicate_pattern(client, test_settings):
    """Two rules with the same pattern let row order decide categorization.

    Measured before this guard: applying twice left three rules sharing one
    pattern with categories ['food', None, 'food'], and a later matching
    transaction came back uncategorized because max(matches, key=len)
    returns the first maximal element. runbooks/merchant-rules-conventions.md
    anticipates same-length collisions needing "a deliberate winner";
    rejecting keeps editing a rule an explicit act rather than something
    that silently happens by insertion order.
    """
    headers = _auth(test_settings)
    account = _account(client, headers)
    imported = seed_source_batch(client, json={
            "account_id": account["id"],
            "period_start": "2026-09-01",
            "period_end": "2026-09-30",
            "transactions": [
                {
                    "txn_date": "2026-09-02",
                    "description": "MYSTERY MERCHANT",
                    "amount_cents": -900,
                }
            ],
        }, headers=headers).json()
    txn_id = imported["unmatched"][0]["id"]
    client.patch(
        f"/transactions/{txn_id}", json={"category_id": "food"}, headers=headers
    )

    first = client.post(
        f"/transactions/{txn_id}/apply_as_rule",
        json={"actor": "test"},
        headers=headers,
    )
    second = client.post(
        f"/transactions/{txn_id}/apply_as_rule",
        json={"actor": "test"},
        headers=headers,
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "conflict"
    # The message points at the rule that already covers this pattern, so
    # the caller knows what to PATCH instead of guessing.
    assert first.json()["id"] in second.json()["error"]["message"]
    assert client.get("/merchant_rules", headers=headers).json()["total"] == 1


def test_trend_rejects_an_unknown_category(client, test_settings):
    """An empty spend chart is an answer people believe.

    Without this, /trend?category_id=no_such_cat returned 200 with items:
    [] — indistinguishable from "you spent nothing in this category". A
    typo'd or stale id would render a confident, wrong, empty chart rather
    than an error.
    """
    response = client.get(
        "/trend?category_id=no_such_cat&months=6", headers=_auth(test_settings)
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_reference"


def test_trend_rejects_an_unknown_entity(client, test_settings):
    response = client.get(
        "/trend?entity_id=no_such_entity&months=6", headers=_auth(test_settings)
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_reference"


def test_trend_still_returns_an_empty_series_for_a_real_but_unused_category(
    client, test_settings
):
    """The counterweight: "no spending yet" is a legitimate answer and must
    stay a 200. Rejecting unknown ids must not start rejecting real ones
    that simply have no transactions."""
    response = client.get(
        "/trend?category_id=pets&months=6", headers=_auth(test_settings)
    )
    assert response.status_code == 200
    assert response.json()["items"] == []


def test_cost_of_ownership_includes_direct_and_relationship_linked_transactions(
    client, test_settings
):
    headers = _auth(test_settings)
    car = client.post(
        "/entities", json={"type": "vehicle", "name": "Corolla"}, headers=headers
    ).json()
    policy_account = client.post(
        "/entities",
        json={"type": "account", "name": "Insurance Autopay"},
        headers=headers,
    ).json()
    # policy_account insures car
    client.post(
        f"/entities/{policy_account['id']}/relationships",
        json={"to_entity_id": car["id"], "relationship_type": "insures"},
        headers=headers,
    )

    main_account = _account(client, headers)
    seed_source_batch(client, json={
            "account_id": main_account["id"],
            "period_start": "2026-08-01",
            "period_end": "2026-08-31",
            "transactions": [
                {
                    "txn_date": "2026-08-05",
                    "description": "GAS STATION",
                    "amount_cents": -4000,
                }
            ],
        }, headers=headers)
    direct_txn = client.get(
        f"/transactions?account_id={main_account['id']}", headers=headers
    ).json()["items"][0]
    client.patch(
        f"/transactions/{direct_txn['id']}",
        json={"entity_id": car["id"]},
        headers=headers,
    )

    seed_source_batch(client, json={
            "account_id": policy_account["id"],
            "period_start": "2026-08-01",
            "period_end": "2026-08-31",
            "transactions": [
                {
                    "txn_date": "2026-08-01",
                    "description": "AUTO PREMIUM",
                    "amount_cents": -8000,
                }
            ],
        }, headers=headers)
    premium_txn = client.get(
        f"/transactions?account_id={policy_account['id']}", headers=headers
    ).json()["items"][0]
    client.patch(
        f"/transactions/{premium_txn['id']}",
        json={"entity_id": policy_account["id"]},
        headers=headers,
    )

    response = client.get(
        f"/entities/{car['id']}/cost_of_ownership?period=2026-08", headers=headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == -12000  # -4000 direct + -8000 via the insures relationship


def test_cost_of_ownership_returns_404_for_unknown_entity(client, test_settings):
    response = client.get(
        "/entities/does-not-exist/cost_of_ownership", headers=_auth(test_settings)
    )
    assert response.status_code == 404


def test_cost_of_ownership_does_not_double_count_duplicate_relationship_rows(
    client, test_settings
):
    """One account may both finance and insure the same vehicle.

    That produces two rows in entity_relationships for the same pair, and
    naively expanding them into the IN clause twice would count every one
    of that account's transactions twice. SQL's IN dedups, so this holds —
    but it holds by accident of the query shape, not by design, so it is
    worth pinning before someone rewrites the lookup as a JOIN, where the
    duplication would be real.
    """
    headers = _auth(test_settings)
    car = client.post(
        "/entities", json={"type": "vehicle", "name": "Corolla"}, headers=headers
    ).json()
    policy = client.post(
        "/entities", json={"type": "account", "name": "Auto Policy"}, headers=headers
    ).json()
    for relationship_type in ("insures", "finances"):
        client.post(
            f"/entities/{policy['id']}/relationships",
            json={"to_entity_id": car["id"], "relationship_type": relationship_type},
            headers=headers,
        )

    seed_source_batch(client, json={
            "account_id": policy["id"],
            "period_start": "2026-08-01",
            "period_end": "2026-08-31",
            "transactions": [
                {
                    "txn_date": "2026-08-01",
                    "description": "AUTO PREMIUM",
                    "amount_cents": -10000,
                }
            ],
        }, headers=headers)
    txn = client.get(
        f"/transactions?account_id={policy['id']}", headers=headers
    ).json()["items"][0]
    client.patch(
        f"/transactions/{txn['id']}", json={"entity_id": policy["id"]}, headers=headers
    )

    body = client.get(
        f"/entities/{car['id']}/cost_of_ownership?period=2026-08", headers=headers
    ).json()

    assert body["total"] == -10000, "the premium was counted once per relationship row"


# --- money is integer cents (a todo) ---------------------------------


def _cents_account(client, test_settings, name="Chase"):
    return client.post(
        "/entities",
        json={"type": "account", "name": name},
        headers=_auth(test_settings),
    ).json()


def _cents_import(client, test_settings, account_id, rows, start="2026-03-01",
                 end="2026-03-31"):
    return seed_source_batch(client, json={
            "account_id": account_id,
            "period_start": start,
            "period_end": end,
            "transactions": rows,
        }, headers=_auth(test_settings))


def test_amount_is_stored_and_returned_as_integer_cents(client, test_settings):
    account = _cents_account(client, test_settings)

    response = _cents_import(
        client,
        test_settings,
        account["id"],
        [{"txn_date": "2026-03-04", "description": "SHELL", "amount_cents": -5210}],
    )

    assert response.status_code == 200
    listed = client.get("/transactions", headers=_auth(test_settings)).json()["items"][0]
    assert listed["amount_cents"] == -5210
    assert isinstance(listed["amount_cents"], int)
    assert "amount" not in listed


def test_a_fractional_amount_is_rejected_rather_than_rounded(client, test_settings):
    """Silent rounding is how a cents column ends up holding float-derived
    values. 52.10 dollars sent as cents is a caller bug, and a 422 says so
    while a round() would bury it."""
    account = _cents_account(client, test_settings)

    response = _cents_import(
        client,
        test_settings,
        account["id"],
        [{"txn_date": "2026-03-04", "description": "SHELL", "amount_cents": -52.10}],
    )

    assert response.status_code == 422
    assert "amount_cents" in response.text


@pytest.mark.parametrize("value", [-5210.0, -52.00, -52.0, 52.00])
def test_whole_number_floats_are_also_rejected(client, test_settings, value):
    """A float with no fractional part is the dangerous case, not the safe one.

    -5210.0 is what round(x * 100) hands you. -52.00 is a round dollar
    amount typed as cents — and round amounts are exactly where a
    dollars/cents mix-up is likeliest (rent, transfers, ATM withdrawals),
    where accepting it would book 52 cents instead of $52.00: a 100x error
    with nothing to notice it.
    """
    account = _cents_account(client, test_settings)

    response = _cents_import(
        client,
        test_settings,
        account["id"],
        [{"txn_date": "2026-03-04", "description": "SHELL", "amount_cents": value}],
    )

    assert response.status_code == 422


def test_the_stored_amount_is_exactly_what_was_sent(client, test_settings):
    """Pins the sign and magnitude behaviourally, against the real column.

    The existing sign coverage was `assert "negative" in description` — a
    string test — and transfers-netting is blind to a global flip, since
    both legs invert together. Inverting the sign at the INSERT has to turn
    a test red, and this is that test.
    """
    account = _cents_account(client, test_settings)
    _cents_import(
        client,
        test_settings,
        account["id"],
        [
            {"txn_date": "2026-03-04", "description": "DEBIT", "amount_cents": -5210},
            {"txn_date": "2026-03-05", "description": "CREDIT", "amount_cents": 200000},
        ],
    )

    by_description = {
        row["description"]: row["amount_cents"]
        for row in client.get(
            "/transactions", headers=_auth(test_settings)
        ).json()["items"]
    }

    assert by_description["DEBIT"] == -5210
    assert by_description["CREDIT"] == 200000


def test_aggregates_are_exact_over_many_rows(client, test_settings):
    """The property the whole change exists for.

    A hundred rows of $20.15 sum to exactly -201500 cents. The same hundred
    as floats sum to -2014.9999999999998, not -2015.00 — and that drift is
    what makes a query disagree with a hand-added column.
    """
    account = _cents_account(client, test_settings)
    _cents_import(
        client,
        test_settings,
        account["id"],
        [
            {
                "txn_date": "2026-03-04",
                "description": f"MERCHANT {i}",
                "amount_cents": -2015,
            }
            for i in range(100)
        ],
    )

    summary = client.get(
        "/spending_summary?period=2026-03", headers=_auth(test_settings)
    ).json()

    assert summary["items"][0]["total"] == -201500
    assert isinstance(summary["items"][0]["total"], int)


def test_float_summation_of_the_same_values_would_not_be_exact():
    """Pins the premise, so the test above is not just asserting arithmetic.

    Not every value drifts — sum([-10.07] * 100) is exactly -1007.0, so a
    test built on that one would have "proved" the case for integers using
    an example where floats were already fine. 20.15 actually drifts.
    """
    assert sum([-20.15] * 100) != -2015.00
    assert sum([-20.15] * 100) == -2014.9999999999998


# --- account-scoped duplicate detection (a todo) ---------------------


def test_overlapping_exports_require_confirmation_before_linking(client, test_settings):
    from tests.source_import_support import source_payload
    account = _cents_account(client, test_settings, "Card")
    row = {"txn_date": "2026-04-09", "description": "SHELL", "amount_cents": -5210}
    _cents_import(client, test_settings, account["id"], [row], "2026-03-10", "2026-04-09")
    headers = _auth(test_settings)
    original = client.get('/transactions', headers=headers).json()['items'][0]
    payload = source_payload({'account_id':account['id'], 'period_start':'2026-04-09',
                              'period_end':'2026-05-09', 'transactions':[row]})
    payload['transactions'][0].pop('decision')
    payload['transactions'][0].pop('reason')
    preview = client.post('/source_imports/preview', json=payload, headers=headers).json()
    assert preview['needs_review'] == 1
    payload['review_token'] = preview['review_token']
    assert client.post('/source_imports/commit', json=payload, headers=headers).status_code == 409
    payload['transactions'][0].update(decision='link', transaction_id=original['id'],
                                      reason='Authored fixture: overlapping export of the same purchase')
    result = client.post('/source_imports/commit', json=payload, headers=headers).json()
    assert result['created'] == 0 and result['linked'] == 1
    assert client.get('/transactions', headers=headers).json()['total'] == 1
    assert client.get('/statements', headers=headers).json()['total'] == 1  # link adds no coverage


def test_two_identical_same_day_charges_both_survive(client, test_settings):
    """The tiebreak this must not lose: two real coffees, same shop, same
    day, same amount are two transactions, not one."""
    account = _cents_account(client, test_settings)
    row = {"txn_date": "2026-03-04", "description": "CAFE", "amount_cents": -450}

    result = _cents_import(client, test_settings, account["id"], [row, row]).json()

    assert result["created"] == 2
    assert client.get("/transactions", headers=_auth(test_settings)).json()["total"] == 2


def test_reviewed_overlap_preserves_a_separate_identical_purchase(client, test_settings):
    from tests.source_import_support import source_payload
    account = _cents_account(client, test_settings)
    row = {"txn_date":"2026-03-04", "description":"CAFE", "amount_cents":-450}
    _cents_import(client, test_settings, account['id'], [row])
    headers = _auth(test_settings)
    original = client.get('/transactions', headers=headers).json()['items'][0]
    payload = source_payload({'account_id':account['id'], 'period_start':'2026-03-04',
                              'period_end':'2026-04-03', 'transactions':[row, row]})
    payload['transactions'][0].update(decision='link', transaction_id=original['id'],
                                      reason='Authored fixture: first record is same purchase')
    preview = client.post('/source_imports/preview', json=payload, headers=headers).json()
    assert preview['needs_review'] == 2
    result = client.post('/source_imports/commit', json={**payload, 'review_token':preview['review_token']}, headers=headers).json()
    assert result['created'] == result['linked'] == 1
    assert client.get('/transactions', headers=headers).json()['total'] == 2


def test_the_same_charge_on_two_accounts_stays_two_charges(client, test_settings):
    """Dedup is per account. A subscription billed to two cards is two
    real charges, and collapsing them would understate spending."""
    visa = _cents_account(client, test_settings, "Visa")
    amex = _cents_account(client, test_settings, "Amex")
    row = {"txn_date": "2026-03-04", "description": "NETFLIX", "amount_cents": -1599}

    _cents_import(client, test_settings, visa["id"], [row])
    _cents_import(client, test_settings, amex["id"], [row])

    assert client.get("/transactions", headers=_auth(test_settings)).json()["total"] == 2


def test_reimporting_an_identical_file_still_skips_everything(client, test_settings):
    account = _cents_account(client, test_settings)
    rows = [
        {"txn_date": "2026-03-04", "description": "SHELL", "amount_cents": -5210},
        {"txn_date": "2026-03-06", "description": "KROGER", "amount_cents": -8100},
    ]

    _cents_import(client, test_settings, account["id"], rows)
    again = _cents_import(client, test_settings, account["id"], rows).json()

    assert again["created"] == 0
    assert again["skipped_duplicates"] == 2


def test_a_differing_amount_is_a_different_transaction(client, test_settings):
    """The key includes the amount, so same merchant same day at a
    different price is not a duplicate."""
    account = _cents_account(client, test_settings)

    _cents_import(
        client,
        test_settings,
        account["id"],
        [{"txn_date": "2026-03-04", "description": "SHELL", "amount_cents": -5210}],
    )
    second = _cents_import(
        client,
        test_settings,
        account["id"],
        [{"txn_date": "2026-03-04", "description": "SHELL", "amount_cents": -4900}],
        "2026-03-04",
        "2026-04-03",
    ).json()

    assert second["created"] == 1
    assert client.get("/transactions", headers=_auth(test_settings)).json()["total"] == 2


def test_the_dedup_index_exists(client, test_settings):
    """The lookup is by (account_id, txn_date) and runs once per imported
    row. Without an index every row scans transactions, so import cost
    grows with total history — the concern a todo raised before #26
    moved where the hot lookup is.
    """
    import sqlite3

    client.get("/transactions", headers=_auth(test_settings))  # force bootstrap
    connection = sqlite3.connect(test_settings.db_path)
    try:
        plan = connection.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT description FROM active_transactions WHERE account_id = ? "
            "AND txn_date = ? AND amount_cents = ?",
            ("a", "2026-03-04", -1),
        ).fetchall()
    finally:
        connection.close()

    # Asserting the plan, not just that the index exists: an index SQLite
    # declines to use is the same as no index, and "it is in sqlite_master"
    # cannot tell those apart.
    detail = " ".join(str(row[-1]) for row in plan)
    assert "idx_txn_dedup" in detail, detail
    assert "SCAN transactions" not in detail, detail


# --- the listing's response shape is closed (a todo) ------------------


TRANSACTION_RESPONSE_FIELDS = {
    "id",
    "statement_id",
    "account_id",
    "txn_date",
    "description",
    "amount_cents",
    "category_id",
    "entity_id",
}


def test_the_listing_returns_exactly_the_transaction_response_fields(
    client, test_settings
):
    """The listing returns the declared response shape and nothing else.

    Named for what it checks. It was
    test_list_transactions_does_not_leak_row_hash, written when the
    listing leaked an internal de-duplication artefact — but #77 dropped
    that column from the table, so the assertion that named it became
    vacuous and the name outlived the field. The surviving guard is the
    set equality: it catches the next column added to `transactions`
    joining the response, which is the failure the named-columns query
    exists to prevent.
    """
    account = _cents_account(client, test_settings)
    _cents_import(
        client,
        test_settings,
        account["id"],
        [{"txn_date": "2026-03-04", "description": "SHELL", "amount_cents": -5210}],
    )

    listed = client.get("/transactions", headers=_auth(test_settings)).json()["items"][0]

    assert set(listed) == TRANSACTION_RESPONSE_FIELDS


def test_the_listing_matches_the_single_transaction_shape(client, test_settings):
    """The listing had no response_model, so it returned whatever SELECT *
    produced while get_transaction returned a filtered shape. Two shapes
    for one resource is how a caller ends up depending on the wider one.
    """
    account = _cents_account(client, test_settings)
    _cents_import(
        client,
        test_settings,
        account["id"],
        [{"txn_date": "2026-03-04", "description": "SHELL", "amount_cents": -5210}],
    )

    listed = client.get("/transactions", headers=_auth(test_settings)).json()["items"][0]
    fetched = client.get(
        f"/transactions/{listed['id']}", headers=_auth(test_settings)
    ).json()

    assert set(listed) == set(fetched)


def test_a_new_internal_column_would_not_leak(client, test_settings):
    """Pins the mechanism, not just today's field list.

    Selecting columns explicitly rather than SELECT * is what makes this
    true; with SELECT * the next column added to the table joins the
    response by default and nobody notices until it is depended on.
    """
    import inspect

    import api.financial

    source = inspect.getsource(api.financial.list_transactions)
    # Comments stripped first: the explanation above the query says
    # "SELECT *" to describe what it is avoiding, and a grep over raw
    # source matched the prose rather than the code — the test failed for
    # the wrong reason and would equally have passed for one.
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )

    assert "SELECT *" not in code, (
        "list_transactions must name its columns, or the next column added "
        "to transactions leaks the same way row_hash did"
    )
    assert "SELECT id, statement_id, account_id" in code


# --- own-account transfers are not spending (ticket T-40) ---------------------


def _transfer_pair(client, test_settings):
    """Two accounts and one transfer that appears on both sides.

    The chequing account shows the money leaving, the card shows the
    same money arriving. The
    card also carries one real purchase, so the fixture can tell "real
    spending" apart from "real spending plus the transfer".
    """
    headers = _auth(test_settings)
    checking = _cents_account(client, test_settings, name="Checking")
    card = _cents_account(client, test_settings, name="Card")

    _cents_import(
        client,
        test_settings,
        checking["id"],
        [
            {
                "txn_date": "2026-03-10",
                "description": "ONLINE PAYMENT TO CARD",
                "amount_cents": -100000,
            }
        ],
    )
    _cents_import(
        client,
        test_settings,
        card["id"],
        [
            {
                "txn_date": "2026-03-10",
                "description": "PAYMENT - THANK YOU",
                "amount_cents": 100000,
            },
            {
                "txn_date": "2026-03-12",
                "description": "GROCERY STORE",
                "amount_cents": -5000,
            },
        ],
    )

    for txn in client.get("/transactions", headers=headers).json()["items"]:
        category = (
            "transfers_credit_card_payment"
            if "GROCERY" not in txn["description"]
            else "food_groceries"
        )
        client.patch(
            f"/transactions/{txn['id']}",
            json={"category_id": category},
            headers=headers,
        )
    return {"checking": checking, "card": card}


def _summary_total(payload) -> int:
    return sum(item["total"] for item in payload["items"])


def test_a_two_sided_transfer_is_excluded_from_the_cross_account_total(
    client, test_settings
):
    """The property the whole ticket exists for.

    Real spending here is 5000. Both legs of the transfer are
    categorized, so with them included the total is unchanged — they
    cancel — but the two legs are still in the per-category breakdown
    and in any per-entity read, where they do NOT cancel. Excluding
    them leaves only what was actually spent.
    """
    _transfer_pair(client, test_settings)
    headers = _auth(test_settings)

    excluded = client.get("/spending_summary?period=2026-03", headers=headers).json()

    assert _summary_total(excluded) == -5000
    assert [item["key"] for item in excluded["items"]] == ["food_groceries"]


def test_a_transfer_straddling_a_period_boundary_is_not_spending(
    client, test_settings
):
    """The case signed summing cannot fix, and the one that makes the
    flag worth having rather than tidy.

    Netting only works when BOTH legs fall inside the query. A transfer
    sent on the 31st and posted on the 1st puts one leg in each month,
    so March is short by the whole transfer and April is long by it —
    and each month's number looks entirely reasonable on its own. Real
    March spending here is 5000; without the exclusion it reports
    105000.
    """
    headers = _auth(test_settings)
    checking = _cents_account(client, test_settings, name="Checking")
    card = _cents_account(client, test_settings, name="Card")
    _cents_import(
        client,
        test_settings,
        checking["id"],
        [
            {
                "txn_date": "2026-03-31",
                "description": "ONLINE PAYMENT TO CARD",
                "amount_cents": -100000,
            },
            {
                "txn_date": "2026-03-12",
                "description": "GROCERY STORE",
                "amount_cents": -5000,
            },
        ],
    )
    # The other leg lands in April, which is the whole point.
    _cents_import(
        client,
        test_settings,
        card["id"],
        [
            {
                "txn_date": "2026-04-01",
                "description": "PAYMENT - THANK YOU",
                "amount_cents": 100000,
            }
        ],
        start="2026-04-01",
        end="2026-04-30",
    )
    for txn in client.get("/transactions", headers=headers).json()["items"]:
        category = (
            "food_groceries"
            if "GROCERY" in txn["description"]
            else "transfers_credit_card_payment"
        )
        client.patch(
            f"/transactions/{txn['id']}",
            json={"category_id": category},
            headers=headers,
        )

    included = client.get(
        "/spending_summary?period=2026-03&exclude_transfers=false", headers=headers
    ).json()
    assert _summary_total(included) == -105000, "the leg with no partner in March"

    excluded = client.get(
        "/spending_summary?period=2026-03", headers=headers
    ).json()
    assert _summary_total(excluded) == -5000
    assert excluded["transfers_excluded_cents"] == -100000
    assert excluded["transfers_excluded_count"] == 1


def test_exclude_transfers_false_returns_the_old_behaviour(client, test_settings):
    _transfer_pair(client, test_settings)

    included = client.get(
        "/spending_summary?period=2026-03&exclude_transfers=false",
        headers=_auth(test_settings),
    ).json()

    keys = {item["key"] for item in included["items"]}
    assert "transfers_credit_card_payment" in keys
    assert included["transfers_excluded_cents"] == 0
    assert included["transfers_excluded_count"] == 0


def test_the_excluded_count_distinguishes_zero_from_none(client, test_settings):
    """Two netting legs sum to 0, so the cents alone cannot tell
    "nothing was excluded" from "a transfer was removed" — and netting
    is the HEALTHY state once both legs are categorized, so the
    ambiguous case is the common one."""
    _transfer_pair(client, test_settings)

    with_transfer = client.get(
        "/spending_summary?period=2026-03", headers=_auth(test_settings)
    ).json()

    assert with_transfer["transfers_excluded_cents"] == 0
    assert with_transfer["transfers_excluded_count"] == 2


def test_a_one_legged_transfer_shows_its_cents(client, test_settings):
    """A common state: only the card's inflow is categorized and its
    outflow twin is not, so the excluded sum is the inflow alone."""
    headers = _auth(test_settings)
    card = _cents_account(client, test_settings, name="Card")
    _cents_import(
        client,
        test_settings,
        card["id"],
        [
            {
                "txn_date": "2026-03-10",
                "description": "PAYMENT - THANK YOU",
                "amount_cents": 100000,
            }
        ],
    )
    txn = client.get("/transactions", headers=headers).json()["items"][0]
    client.patch(
        f"/transactions/{txn['id']}",
        json={"category_id": "transfers_credit_card_payment"},
        headers=headers,
    )

    summary = client.get("/spending_summary?period=2026-03", headers=headers).json()

    assert summary["transfers_excluded_cents"] == 100000
    assert summary["transfers_excluded_count"] == 1


def test_uncategorized_rows_survive_the_exclusion(client, test_settings):
    """SQL is three-valued: `NULL IN (...)` is NULL and `NOT NULL` is
    NULL, so a bare negation drops every uncategorized row. On a live
    database that is every unclassified row vanishing from every
    total, with a 200 and a smaller, plausible number in its place.
    """
    account = _cents_account(client, test_settings)
    _cents_import(
        client,
        test_settings,
        account["id"],
        [
            {
                "txn_date": "2026-03-04",
                "description": "SOMETHING UNMATCHED",
                "amount_cents": -2500,
            }
        ],
    )

    summary = client.get(
        "/spending_summary?period=2026-03", headers=_auth(test_settings)
    ).json()

    assert [item["key"] for item in summary["items"]] == [None]
    assert summary["items"][0]["total"] == -2500


def test_a_renamed_transfer_category_is_still_excluded(client, test_settings):
    """Exclusion is by parent_id, not by display name. A category
    renamed in the taxonomy must stay non-spending, or the guard is
    one edit away from silently switching off."""
    headers = _auth(test_settings)
    _transfer_pair(client, test_settings)

    connection = sqlite3.connect(test_settings.db_path)
    try:
        connection.execute(
            "UPDATE categories SET name = 'Moving money about' "
            "WHERE id = 'transfers_credit_card_payment'"
        )
        connection.commit()
    finally:
        connection.close()

    summary = client.get("/spending_summary?period=2026-03", headers=headers).json()

    assert [item["key"] for item in summary["items"]] == ["food_groceries"]
    assert summary["transfers_excluded_count"] == 2


def test_the_transfers_branch_exists(client, test_settings):
    """If the seed ever renamed or removed the `transfers` slug, every
    exclusion above would quietly match nothing and the flag would keep
    reporting success. Pins the constant against the real taxonomy."""
    from api.financial import TRANSFERS_CATEGORY_ID

    listing = client.get("/categories?limit=200", headers=_auth(test_settings)).json()
    assert len(listing["items"]) == listing["total"], "categories listing truncated"
    by_id = {item["id"]: item for item in listing["items"]}

    assert TRANSFERS_CATEGORY_ID in by_id
    children = [
        item for item in listing["items"] if item["parent_id"] == TRANSFERS_CATEGORY_ID
    ]
    assert children, "the transfers branch has no children to exclude"


def test_trend_excludes_transfers_for_an_entity(client, test_settings):
    """`trend?entity_id=` filters on transactions.entity_id — the entity
    a row is booked AGAINST, not the account it moved through — so the
    rows have to be linked explicitly. Worth stating: an earlier version
    of this test passed the account id and got an empty series, which
    looked like the exclusion working and was the column being wrong.
    """
    accounts = _transfer_pair(client, test_settings)
    headers = _auth(test_settings)
    card_entity = accounts["card"]["id"]
    for txn in client.get("/transactions", headers=headers).json()["items"]:
        client.patch(
            f"/transactions/{txn['id']}",
            json={"entity_id": card_entity},
            headers=headers,
        )

    series = client.get(
        f"/trend?entity_id={card_entity}&months=60", headers=headers
    ).json()

    assert series["items"], "the fixture must produce a non-empty series"
    assert sum(item["total"] for item in series["items"]) == -5000
    assert series["transfers_excluded_cents"] == 0
    assert series["transfers_excluded_count"] == 2


def test_trend_for_a_transfers_category_still_returns_its_series(
    client, test_settings
):
    """Asking "how have my credit card payments trended" must not answer
    with an empty series. An empty series is indistinguishable from "you
    made none", which is a thing people act on — the same failure the
    unknown-id validation upstream exists to prevent. An explicit
    request beats the default.
    """
    _transfer_pair(client, test_settings)

    series = client.get(
        "/trend?category_id=transfers_credit_card_payment&months=60",
        headers=_auth(test_settings),
    ).json()

    assert series["items"], "an explicit transfers category must not be emptied"
    assert sum(item["total"] for item in series["items"]) == 0
    assert series["transfers_excluded_count"] == 0


def test_the_summary_reports_how_much_is_still_unclassified(client, test_settings):
    """An exclusion that reads as completeness is the failure this whole
    change exists to close.

    When most transfers are NOT categorized as transfers, they sit in the uncategorized bucket looking like purchases — so
    `transfers_excluded_cents` on its own invites "and the rest is
    spending", which is wrong by more than the figure it reports.
    """
    headers = _auth(test_settings)
    account = _cents_account(client, test_settings)
    _cents_import(
        client,
        test_settings,
        account["id"],
        [
            {
                "txn_date": "2026-03-04",
                "description": "GROCERY STORE",
                "amount_cents": -5000,
            },
            {
                "txn_date": "2026-03-05",
                "description": "ONLINE PAYMENT TO CARD",
                "amount_cents": -100000,
            },
        ],
    )
    for txn in client.get("/transactions", headers=headers).json()["items"]:
        if "GROCERY" in txn["description"]:
            client.patch(
                f"/transactions/{txn['id']}",
                json={"category_id": "food_groceries"},
                headers=headers,
            )

    summary = client.get("/spending_summary?period=2026-03", headers=headers).json()

    # The transfer's outflow leg is unlabelled, so nothing is excluded
    # and the -100000 is still counted as if it were spending. The
    # uncategorized figure is what says so.
    assert summary["transfers_excluded_count"] == 0
    assert summary["uncategorized_cents"] == -100000
    assert _summary_total(summary) == -105000


def test_uncategorized_cents_respects_the_period(client, test_settings):
    """A figure that ignored the filter would contradict the totals it
    sits beside, which is worse than not reporting it."""
    headers = _auth(test_settings)
    account = _cents_account(client, test_settings)
    _cents_import(
        client,
        test_settings,
        account["id"],
        [{"txn_date": "2026-03-04", "description": "A", "amount_cents": -1000}],
    )
    _cents_import(
        client,
        test_settings,
        account["id"],
        [{"txn_date": "2026-04-04", "description": "B", "amount_cents": -2500}],
        start="2026-04-01",
        end="2026-04-30",
    )

    march = client.get("/spending_summary?period=2026-03", headers=headers).json()
    all_time = client.get("/spending_summary", headers=headers).json()

    assert march["uncategorized_cents"] == -1000
    assert all_time["uncategorized_cents"] == -3500


def test_trend_reports_uncategorized_for_an_entity_series(client, test_settings):
    headers = _auth(test_settings)
    accounts = _transfer_pair(client, test_settings)
    card_entity = accounts["card"]["id"]
    for txn in client.get("/transactions", headers=headers).json()["items"]:
        client.patch(
            f"/transactions/{txn['id']}",
            json={"entity_id": card_entity},
            headers=headers,
        )

    series = client.get(
        f"/trend?entity_id={card_entity}&months=60", headers=headers
    ).json()

    # Everything in the fixture is categorized, so this is the honest 0
    # rather than a missing field.
    assert series["uncategorized_cents"] == 0


def test_empty_merchant_rule_update_is_refused(client, test_settings):
    """A PATCH that sets nothing used to be a 200 reporting the rule back.

    That reads exactly like a successful edit, so a caller correcting a
    wrong rule would believe the fix landed while the rule kept
    misclassifying every future import. Same silent-no-op shape as
    update_entity's dropped fields.

    "At least one of the three", not "at least one of category/entity": a
    pattern-only PATCH is legitimate at this layer. The restriction
    against pattern changes is a TOOL-surface decision (a todo), and it
    lives in the tool signature rather than here.
    """
    headers = _auth(test_settings)
    created = client.post(
        "/merchant_rules",
        json={"pattern": "ZORBLAX", "category_id": "food_dining", "actor": "test"},
        headers=headers,
    ).json()

    # `actor` is supplied so this still exercises the at-least-one rule
    # rather than the actor rule — an empty body would now 422 for the
    # wrong reason and the test would pass without testing anything.
    response = client.patch(
        f"/merchant_rules/{created['id']}", json={"actor": "test"}, headers=headers
    )
    assert response.status_code == 422, response.text
    assert "at least one" in response.text

    # And the rule is untouched.
    after = client.get(f"/merchant_rules/{created['id']}", headers=headers).json()
    assert after == created

# --- the null bucket and the field that tracks it (D48) --------------------


def _two_kinds_of_null(client, test_settings):
    """A fixture where "no category" and "no entity" are DIFFERENT rows.

    Without that they coincide and every assertion below passes under
    either predicate — which is how a field summing the wrong one
    shipped in #93 and stayed green.
    """
    headers = _auth(test_settings)
    account = _cents_account(client, test_settings, name="Chequing")
    car = client.post(
        "/entities", json={"type": "vehicle", "name": "RAV4"}, headers=headers
    ).json()
    _cents_import(
        client,
        test_settings,
        account["id"],
        [
            # categorized, attributed  -> in neither null bucket
            {"txn_date": "2026-03-02", "description": "GARAGE", "amount_cents": -3000},
            # categorized, NOT attributed -> only the entity null bucket
            {"txn_date": "2026-03-03", "description": "GROCERY", "amount_cents": -5000},
            # NOT categorized, attributed -> only the category null bucket
            {"txn_date": "2026-03-04", "description": "MYSTERY", "amount_cents": -700},
            # A TRANSFER with no entity. Excluded from `items` by
            # exclude_transfers, so the field must exclude it too or the
            # two disagree by exactly this row — which is the whole
            # point of computing the field with the same filters.
            # Without this row the fixture's "no entity" rows are all
            # non-transfers and the exclusion cannot be observed.
            {
                "txn_date": "2026-03-05",
                "description": "ONLINE PAYMENT TO CARD",
                "amount_cents": -100000,
            },
        ],
    )
    for txn in client.get("/transactions", headers=headers).json()["items"]:
        patch: dict = {}
        if txn["description"] == "GARAGE":
            patch = {"category_id": "auto_maintenance", "entity_id": car["id"]}
        elif txn["description"] == "GROCERY":
            patch = {"category_id": "food_groceries"}
        elif txn["description"] == "MYSTERY":
            patch = {"entity_id": car["id"]}
        elif "PAYMENT TO CARD" in txn["description"]:
            patch = {"category_id": "transfers_credit_card_payment"}
        if patch:
            client.patch(
                f"/transactions/{txn['id']}", json=patch, headers=headers
            )
    return {"car": car, "account": account}


def _summary(client, test_settings, query=""):
    return client.get(
        f"/spending_summary?period=2026-03{query}", headers=_auth(test_settings)
    ).json()


def test_category_mode_reports_uncategorized_cents(client, test_settings):
    _two_kinds_of_null(client, test_settings)

    body = _summary(client, test_settings)

    assert body["uncategorized_cents"] == -700
    assert "unattributed_cents" not in body


def test_entity_mode_reports_unattributed_cents(client, test_settings):
    """The substance of the fix, not just the name: in entity mode the
    figure must sum rows with no ENTITY (-5000), not rows with no
    CATEGORY (-700). Renaming without changing the predicate would
    leave a correctly-labelled wrong number, which is worse than the
    wrongly-labelled one it replaced."""
    _two_kinds_of_null(client, test_settings)

    body = _summary(client, test_settings, "&group_by=entity")

    assert body["unattributed_cents"] == -5000
    assert "uncategorized_cents" not in body


@pytest.mark.parametrize("group_by", ["category", "entity"])
def test_the_two_null_fields_never_appear_together(client, test_settings, group_by):
    """One response, one meaning of null. Both fields present would put
    the reader back where #93 left them — two numbers, no way to tell
    which question each answers."""
    _two_kinds_of_null(client, test_settings)

    body = _summary(client, test_settings, f"&group_by={group_by}")

    present = {"uncategorized_cents", "unattributed_cents"} & set(body)
    assert len(present) == 1, present


@pytest.mark.parametrize(
    ("group_by", "field"),
    [("category", "uncategorized_cents"), ("entity", "unattributed_cents")],
)
def test_the_field_equals_the_null_bucket_in_items(
    client, test_settings, group_by, field
):
    """The invariant that makes the field checkable rather than merely
    present: it is the same number the `key: null` row carries, under
    the same filters — transfers exclusion included. A transfer row can
    have no entity, so computing the field without that exclusion would
    make it disagree with its own bucket by exactly the transfers.
    """
    _two_kinds_of_null(client, test_settings)

    body = _summary(client, test_settings, f"&group_by={group_by}")
    bucket = [item for item in body["items"] if item["key"] is None]

    assert bucket, "the fixture must produce a null bucket in this mode"
    assert body[field] == bucket[0]["total"]


def test_the_null_bucket_stays_in_items(client, test_settings):
    """Dropping it turns "total spending" into "total categorized
    spending". A machine reading
    `items` would make the same mistake with no one to notice."""
    _two_kinds_of_null(client, test_settings)

    body = _summary(client, test_settings)

    assert None in {item["key"] for item in body["items"]}
    assert sum(item["total"] for item in body["items"]) == -8700


def test_trend_does_not_rename_its_field_for_symmetry(client, test_settings):
    """`trend` keeps `uncategorized_cents` in BOTH forms, deliberately.

    Its items are months, so there is no null bucket to agree with, and
    an entity series is already filtered to one entity — no row in it
    can be unattributed. `unattributed_cents` would name something the
    series cannot contain. This exists so the next person aligning the
    two endpoints for consistency finds the reason first.
    """
    fixture = _two_kinds_of_null(client, test_settings)

    series = client.get(
        f"/trend?entity_id={fixture['car']['id']}&months=60",
        headers=_auth(test_settings),
    ).json()

    assert "unattributed_cents" not in series
    assert series["uncategorized_cents"] == -700


@pytest.mark.parametrize(
    ("group_by", "field"),
    [("category", "uncategorized_cents"), ("entity", "unattributed_cents")],
)
def test_the_field_and_its_bucket_agree_about_archived_rows(
    client, test_settings, group_by, field
):
    """The invariant across a third dimension: archiving (#94).

    Both sides read `active_transactions`, so they agree by
    construction — which is exactly the kind of claim that has been
    wrong twice today. Archiving a row that sits in the null bucket
    must move BOTH numbers, or the field silently describes a
    population `items` no longer contains.
    """
    headers = _auth(test_settings)
    _two_kinds_of_null(client, test_settings)

    before = _summary(client, test_settings, f"&group_by={group_by}")
    bucket_before = [i for i in before["items"] if i["key"] is None][0]["total"]
    assert before[field] == bucket_before

    # Archive one row that is in this mode's null bucket.
    target = "MYSTERY" if group_by == "category" else "GROCERY"
    txn = next(
        t
        for t in client.get("/transactions", headers=headers).json()["items"]
        if t["description"] == target
    )
    archived = client.post(f"/transactions/{txn['id']}/archive", headers=headers)
    assert archived.status_code == 200, archived.text

    after = _summary(client, test_settings, f"&group_by={group_by}")
    bucket_after = [i for i in after["items"] if i["key"] is None]

    assert after[field] == (bucket_after[0]["total"] if bucket_after else 0)
    assert after[field] != before[field], (
        "archiving a row in the null bucket must change the figure that "
        "tracks it, or one of the two is reading the base table"
    )


# --- statement coverage: adjacency, not balances (ticket T-41 / D56) ---------


def _statement(client, test_settings, account_id, start, end):
    return client.post(
        "/statements",
        json={"account_id": account_id, "period_start": start, "period_end": end},
        headers=_auth(test_settings),
    )


def _coverage(client, test_settings, query=""):
    return client.get(
        f"/statements/coverage{query}", headers=_auth(test_settings)
    ).json()


def test_coverage_is_reachable_and_not_read_as_a_statement_id(
    client, test_settings
):
    """Route order, pinned.

    `/statements/{statement_id}` is declared in the same router, and
    FastAPI resolves in declaration order — so with `coverage` below it
    the endpoint answers 404 "No statement with id 'coverage'". It did,
    until a request was sent. The openapi listing showed the path in
    both arrangements, so registration is not reachability.
    """
    response = client.get("/statements/coverage", headers=_auth(test_settings))

    assert response.status_code == 200, response.text
    assert set(response.json()) == {"statements", "accounts", "gaps", "overlaps"}


def test_contiguous_statements_report_no_gap(client, test_settings):
    account = _cents_account(client, test_settings, name="Chequing")
    _statement(client, test_settings, account["id"], "2026-03-01", "2026-03-31")
    _statement(client, test_settings, account["id"], "2026-04-01", "2026-04-30")

    body = _coverage(client, test_settings)

    assert body["statements"] == 2
    assert body["gaps"] == []
    assert body["overlaps"] == []


def test_a_missing_statement_is_reported_as_a_gap(client, test_settings):
    """The failure adjacency exists for, and the one no balance check
    can see: a chain across the statements you have is as
    self-consistent as a chain across the pages you have."""
    account = _cents_account(client, test_settings, name="Chequing")
    _statement(client, test_settings, account["id"], "2026-03-01", "2026-03-31")
    # April never imported.
    _statement(client, test_settings, account["id"], "2026-05-01", "2026-05-31")

    body = _coverage(client, test_settings)

    assert len(body["gaps"]) == 1
    gap = body["gaps"][0]
    # The first and last MISSING day (ticket T-14), not the covered days
    # either side. Previously 2026-03-31/2026-05-01, which named a span
    # two days wider than the `days` beside it.
    assert gap["gap_start"] == "2026-04-01"
    assert gap["gap_end"] == "2026-04-30"
    assert gap["days"] == 30
    assert gap["account_name"] == "Chequing"


def test_a_re_export_is_an_overlap_not_a_gap(client, test_settings):
    """Overlapping export windows are documented and expected for
    card-a-style CSV exports, and dedup is what stops them double-counting. Reporting
    them as gaps would make the check cry wolf on the one source that
    overlaps by design — and a report that cries wolf is one nobody
    reads, which is the same outcome as not having it."""
    account = _cents_account(client, test_settings, name="Card")
    _statement(client, test_settings, account["id"], "2026-03-10", "2026-04-09")
    _statement(client, test_settings, account["id"], "2026-04-09", "2026-05-09")

    body = _coverage(client, test_settings)

    assert body["gaps"] == []
    assert len(body["overlaps"]) == 1
    # Days covered TWICE, positive. This asserted -1 before ticket T-13,
    # when gaps and overlaps shared one field whose sign carried the
    # distinction -- a model reading the array never sees the sign
    # convention, only fields called gap_* on a thing that is not a gap.
    assert body["overlaps"][0]["days"] == 1
    assert body["overlaps"][0]["overlap_start"] == "2026-04-09"
    assert body["overlaps"][0]["overlap_end"] == "2026-04-09"
    assert "gap_start" not in body["overlaps"][0]
    assert "gap_end" not in body["overlaps"][0]


def test_archiving_a_statement_opens_a_gap(client, test_settings):
    """Over `active_statements`, deliberately. An archived statement's
    rows no longer count toward any total, so the period it covered is
    genuinely unaccounted for — closing silently over it would report
    coverage the data does not have."""
    headers = _auth(test_settings)
    account = _cents_account(client, test_settings, name="Chequing")
    _statement(client, test_settings, account["id"], "2026-03-01", "2026-03-31")
    middle = _statement(
        client, test_settings, account["id"], "2026-04-01", "2026-04-30"
    ).json()
    _statement(client, test_settings, account["id"], "2026-05-01", "2026-05-31")
    assert _coverage(client, test_settings)["gaps"] == []

    archived = client.post(
        f"/statements/{middle['id']}/archive", headers=headers
    )
    assert archived.status_code == 200, archived.text

    body = _coverage(client, test_settings)
    assert body["statements"] == 2
    assert len(body["gaps"]) == 1
    assert body["gaps"][0]["days"] == 30


def test_accounts_are_chained_separately(client, test_settings):
    """Two accounts whose periods interleave must not chain into each
    other. Without the per-account split every second statement would
    look like a gap, and the report would be noise from the first day
    two accounts existed."""
    one = _cents_account(client, test_settings, name="Chequing")
    two = _cents_account(client, test_settings, name="Card")
    _statement(client, test_settings, one["id"], "2026-03-01", "2026-03-31")
    _statement(client, test_settings, two["id"], "2026-03-15", "2026-04-14")
    _statement(client, test_settings, one["id"], "2026-04-01", "2026-04-30")
    _statement(client, test_settings, two["id"], "2026-04-15", "2026-05-14")

    body = _coverage(client, test_settings)

    assert body["accounts"] == 2
    assert body["gaps"] == []


def test_coverage_can_be_scoped_to_one_account(client, test_settings):
    one = _cents_account(client, test_settings, name="Chequing")
    two = _cents_account(client, test_settings, name="Card")
    _statement(client, test_settings, one["id"], "2026-03-01", "2026-03-31")
    _statement(client, test_settings, one["id"], "2026-05-01", "2026-05-31")
    _statement(client, test_settings, two["id"], "2026-03-15", "2026-04-14")

    body = _coverage(client, test_settings, f"?account_id={one['id']}")

    assert body["accounts"] == 1
    assert len(body["gaps"]) == 1


def test_coverage_for_an_unknown_account_is_a_400(client, test_settings):
    """Not an empty report. An empty coverage report for a typo'd id
    reads as "this account is fully covered", which is the confident
    wrong answer this endpoint exists to prevent elsewhere."""
    response = client.get(
        "/statements/coverage?account_id=nope", headers=_auth(test_settings)
    )

    assert response.status_code == 400


def test_a_single_statement_has_nothing_to_chain(client, test_settings):
    account = _cents_account(client, test_settings, name="Chequing")
    _statement(client, test_settings, account["id"], "2026-03-01", "2026-03-31")

    body = _coverage(client, test_settings)

    assert body["statements"] == 1
    assert body["gaps"] == []
    assert body["overlaps"] == []


CORPUS_SHAPED_PERIODS = {
    "Primary": [
        ("2025-10-01", "2025-10-31"),
        ("2025-11-01", "2025-11-30"),
        ("2025-12-01", "2025-12-31"),
    ],
    "Export": [
        ("2025-10-01", "2025-10-31"),
        ("2025-10-31", "2025-11-30"),  # a re-export sharing its boundary day
        ("2025-12-01", "2025-12-31"),
    ],
    "Window": [("2025-10-01", "2025-12-31")],
}


def test_a_mixed_corpus_shape_reports_no_gaps_and_one_overlap(
    client, test_settings
):
    """Seven invented periods over three invented accounts: plain monthly
    statements, a CSV-style re-export sharing one boundary day, and one long
    activity window that the runbook treats as a single row.

    A report that fired on an ordinary mix like this would be useless on
    day one.
    """
    for name, periods in CORPUS_SHAPED_PERIODS.items():
        account = _cents_account(client, test_settings, name=name)
        for start, end in periods:
            created = _statement(client, test_settings, account["id"], start, end)
            assert created.status_code == 200, created.text

    body = _coverage(client, test_settings)

    assert body["statements"] == 7
    assert body["accounts"] == 3
    assert body["gaps"] == [], body["gaps"]
    assert len(body["overlaps"]) == 1
    assert body["overlaps"][0]["account_name"] == "Export"


# --- a shared policy is reported, not apportioned (ticket T-42 / D66) --------


def _covered_pair(client, test_settings, end_date=None):
    """One policy insuring two cars — the shape the whole ticket is about."""
    headers = _auth(test_settings)
    policy = _cents_account(client, test_settings, name="Auto policy")
    cars = []
    for name in ("Car A", "Car B"):
        cars.append(
            client.post(
                "/entities", json={"type": "vehicle", "name": name}, headers=headers
            ).json()
        )
        body: dict = {
            "to_entity_id": cars[-1]["id"],
            "relationship_type": "insures",
        }
        if end_date is not None:
            body["end_date"] = end_date
        created = client.post(
            f"/entities/{policy['id']}/relationships", json=body, headers=headers
        )
        assert created.status_code == 200, created.text
    _cents_import(
        client,
        test_settings,
        policy["id"],
        [
            {
                "txn_date": "2026-03-04",
                "description": "PREMIUM",
                "amount_cents": -10000,
            }
        ],
    )
    txn = client.get("/transactions", headers=headers).json()["items"][0]
    client.patch(
        f"/transactions/{txn['id']}",
        json={"entity_id": policy["id"], "category_id": "auto_maintenance"},
        headers=headers,
    )
    return {"policy": policy, "cars": cars}


def _cost(client, test_settings, entity_id):
    return client.get(
        f"/entities/{entity_id}/cost_of_ownership", headers=_auth(test_settings)
    ).json()


def test_an_unshared_entity_reports_no_overlap(client, test_settings):
    """The common case must stay quiet. A field that is always non-zero
    is a field callers learn to ignore."""
    headers = _auth(test_settings)
    car = client.post(
        "/entities", json={"type": "vehicle", "name": "Only car"}, headers=headers
    ).json()

    body = _cost(client, test_settings, car["id"])

    assert body["shared_cost_entities"] == 0
    assert body["shared_with"] == []


def test_two_cars_on_one_policy_each_report_the_other(client, test_settings):
    """The defect, made visible rather than apportioned.

    Both totals contain the full -10000 premium, so adding them reports
    -20000 for a -10000 charge. The number is unchanged on purpose; what
    is new is that each response now says who it overlaps with.
    """
    fixture = _covered_pair(client, test_settings)
    a, b = fixture["cars"]

    cost_a = _cost(client, test_settings, a["id"])
    cost_b = _cost(client, test_settings, b["id"])

    assert cost_a["total"] == -10000
    assert cost_b["total"] == -10000

    assert cost_a["shared_cost_entities"] == 1
    assert [s["entity_id"] for s in cost_a["shared_with"]] == [b["id"]]
    assert cost_a["shared_with"][0]["entity_name"] == "Car B"
    assert cost_a["shared_with"][0]["via_entity_name"] == "Auto policy"
    assert cost_a["shared_with"][0]["relationship_type"] == "insures"

    assert cost_b["shared_cost_entities"] == 1
    assert [s["entity_id"] for s in cost_b["shared_with"]] == [a["id"]]


def test_the_total_is_not_apportioned(client, test_settings):
    """Pinned separately, because "fixing" this by halving is the
    obvious next change and it would make the single-entity answer
    silently wrong — the question actually asked is "what does this car
    cost me, counting the policy that covers it"."""
    fixture = _covered_pair(client, test_settings)

    assert _cost(client, test_settings, fixture["cars"][0]["id"])["total"] == -10000


def test_an_ended_policy_still_counts_because_the_total_still_counts_it(
    client, test_settings
):
    """The brief said an ended relationship should not count. It has to,
    and the reason is arithmetic rather than semantics.

    `cost_of_ownership` does not filter on `end_date` — a lapsed
    policy's premiums remain in both cars' totals and still
    double-count. A `shared_with` that dropped them would under-report
    an overlap the number beside it contains, which is the field lying
    about its own total (the D48 defect, one endpoint over).

    So ended links are reported and counted, with `end_date` set so a
    caller can tell current cover from historical without the count
    disagreeing with the money.
    """
    fixture = _covered_pair(client, test_settings, end_date="2026-01-31")
    a, b = fixture["cars"]

    cost_a = _cost(client, test_settings, a["id"])

    assert cost_a["total"] == -10000, "the ended policy is still in the total"
    assert cost_a["shared_cost_entities"] == 1
    assert cost_a["shared_with"][0]["end_date"] == "2026-01-31"
    assert cost_a["shared_with"][0]["entity_id"] == b["id"]


def test_the_count_matches_what_shared_with_lists(client, test_settings):
    """The invariant that makes the count checkable rather than merely
    present — the same pairing D48 pinned between a figure and its
    bucket."""
    fixture = _covered_pair(client, test_settings)

    for car in fixture["cars"]:
        body = _cost(client, test_settings, car["id"])
        assert body["shared_cost_entities"] == len(
            {item["entity_id"] for item in body["shared_with"]}
        )


def test_the_entity_does_not_report_itself(client, test_settings):
    fixture = _covered_pair(client, test_settings)
    a = fixture["cars"][0]

    body = _cost(client, test_settings, a["id"])

    assert a["id"] not in {item["entity_id"] for item in body["shared_with"]}


def test_two_policies_covering_one_other_car_count_it_once(client, test_settings):
    """Distinct entities, not rows. Two accounts covering the same other
    car is ONE overlapping entity, and a caller asking "is this safe to
    add" wants the answer in entities."""
    headers = _auth(test_settings)
    car_a = client.post(
        "/entities", json={"type": "vehicle", "name": "Car A"}, headers=headers
    ).json()
    car_b = client.post(
        "/entities", json={"type": "vehicle", "name": "Car B"}, headers=headers
    ).json()
    for name in ("Policy one", "Policy two"):
        account = _cents_account(client, test_settings, name=name)
        for car in (car_a, car_b):
            client.post(
                f"/entities/{account['id']}/relationships",
                json={"to_entity_id": car["id"], "relationship_type": "insures"},
                headers=headers,
            )

    body = _cost(client, test_settings, car_a["id"])

    assert len(body["shared_with"]) == 2, "one row per covering account"
    assert body["shared_cost_entities"] == 1, "but one overlapping entity"


# --- trend's window opens on a month boundary (ticket T-18) ------------------


def _trend_rows(client, headers, dates_and_cents, category="food"):
    """One account, one statement, rows on the dates given, all categorized."""
    account = _account(client, headers)
    imported = seed_source_batch(client, json={
            "account_id": account["id"],
            "period_start": "2026-01-01",
            "period_end": "2026-12-31",
            "transactions": [
                {
                    "txn_date": txn_date,
                    "description": f"MERCHANT {n}",
                    "amount_cents": cents,
                }
                for n, (txn_date, cents) in enumerate(dates_and_cents)
            ],
        }, headers=headers).json()
    for row in imported["unmatched"]:
        client.patch(
            f"/transactions/{row['id']}",
            json={"category_id": category},
            headers=headers,
        )
    return account


def _freeze(monkeypatch, today: str):
    """Freeze the clock the trend window opens from.

    The bug this file now guards was INVISIBLE on any single day: the
    size of the error is the day of the month, so a test that ran once,
    on an unknown date, could not see it. Freezing is what makes the
    claim checkable.
    """
    import api.financial

    monkeypatch.setattr(api.financial, "_now_anchor", lambda: today)


def test_trend_returns_exactly_the_months_asked_for(
    client, test_settings, monkeypatch
):
    """It returned N+1. `date('now', '-N months')` is day-precision and
    the grouping is by calendar month, so the window opened partway
    through a month and that month became an extra bucket."""
    headers = _auth(test_settings)
    _trend_rows(
        client,
        headers,
        [("2026-06-10", -100), ("2026-07-10", -200), ("2026-08-10", -300),
         ("2026-09-10", -400)],
    )
    _freeze(monkeypatch, "2026-09-19")

    body = client.get("/trend?category_id=food&months=3", headers=headers).json()

    assert [item["month"] for item in body["items"]] == [
        "2026-07",
        "2026-08",
        "2026-09",
    ], "months=3 must be three buckets, not four"


def test_trends_leading_bucket_is_the_same_on_the_3rd_and_the_28th(
    client, test_settings, monkeypatch
):
    """THE ACTUAL DEFECT, and the only test here that could have caught it.

    Same rows, same `months`, two different days of the same month. Under
    the old day-precision window the leading bucket was truncated at
    today-minus-N-months, so it was nearly empty on the 3rd and nearly
    whole on the 28th.

    THE ROWS HAVE TO SIT IN THE MONTH THE OLD CUTOFF TRUNCATED, and that
    month is N back from today -- June for months=3 in September. It is
    NOT the leading bucket of the fixed version, which is July: the whole
    point is that day-precision reaches back into a month the month-
    boundary window excludes outright.

    Two earlier drafts of this test passed against the bug and the
    mutation caught both. The first put its earliest rows in July, so the
    cutoff landed in an empty June. The second moved the rows to June but
    asked for months=4, which puts the cutoff in an empty May. Reading
    the test did not find either; reverting the code did.

    With June rows on the 2nd and the 20th and months=3, day-precision
    gives a June bucket of -200 on the 3rd (cutoff 06-03, keeps the 20th)
    and NO June bucket at all on the 28th (cutoff 06-28, keeps neither).
    So the month lists themselves disagree, which is what is asserted:
    under a month boundary both days return exactly July, August,
    September.
    """
    headers = _auth(test_settings)
    _trend_rows(
        client,
        headers,
        [
            ("2026-06-02", -100),
            ("2026-06-20", -200),
            ("2026-07-15", -400),
            ("2026-08-10", -800),
            ("2026-09-02", -1600),
        ],
    )

    _freeze(monkeypatch, "2026-09-03")
    early = client.get("/trend?category_id=food&months=3", headers=headers).json()
    _freeze(monkeypatch, "2026-09-28")
    late = client.get("/trend?category_id=food&months=3", headers=headers).json()

    assert [i["month"] for i in early["items"]] == ["2026-07", "2026-08", "2026-09"], (
        f"on the 3rd the series reached back past its own window: {early['items']}"
    )
    assert [i["month"] for i in late["items"]] == ["2026-07", "2026-08", "2026-09"], (
        f"on the 28th the series was a different shape: {late['items']}"
    )
    leading_early = next(i for i in early["items"] if i["month"] == "2026-07")
    leading_late = next(i for i in late["items"] if i["month"] == "2026-07")
    assert leading_early["total"] == leading_late["total"] == -400, (
        "the leading bucket changed with the day of the month: "
        f"{leading_early} on the 3rd, {leading_late} on the 28th"
    )


def test_trend_reports_a_whole_month_the_same_however_far_back_you_look(
    client, test_settings, monkeypatch
):
    """The other face of it: a month's total must not depend on `months`.

    Live, one month's total at months=1 was several times smaller than
    at months>=2. August is August.
    """
    headers = _auth(test_settings)
    _trend_rows(
        client,
        headers,
        [("2026-08-01", -100), ("2026-08-20", -900), ("2026-09-05", -50)],
    )
    _freeze(monkeypatch, "2026-09-19")

    totals = {}
    for months in (1, 2, 3, 6):
        body = client.get(
            f"/trend?category_id=food&months={months}", headers=headers
        ).json()
        for item in body["items"]:
            totals.setdefault(item["month"], set()).add(item["total"])

    disagreeing = {m: v for m, v in totals.items() if len(v) > 1}
    assert not disagreeing, (
        f"the same calendar month reported different totals in different "
        f"windows: {disagreeing}"
    )
    assert totals["2026-08"] == {-1000}


def test_trend_excluded_totals_cover_the_same_window_as_the_series(
    client, test_settings, monkeypatch
):
    """Two copies of the window expression is how they come to disagree.

    `transfers_excluded_cents` says how much was excluded FROM THIS
    SERIES, so a window that differed from the buckets' would report an
    exclusion from a span the caller is not looking at. A transfer in the
    month BEFORE the window must not be counted.
    """
    headers = _auth(test_settings)
    account = _account(client, headers)
    imported = seed_source_batch(client, json={
            "account_id": account["id"],
            "period_start": "2026-01-01",
            "period_end": "2026-12-31",
            "transactions": [
                {"txn_date": "2026-06-15", "description": "OLD TRANSFER",
                 "amount_cents": -5000},
                {"txn_date": "2026-08-15", "description": "NEW TRANSFER",
                 "amount_cents": -7000},
                # After the anchor. Tests the UPPER bound of the shared
                # window: without one on the excluded-totals query this
                # row is counted as excluded from a series it was never
                # in. Measured -- dropping that bound passed every other
                # test in this file.
                {"txn_date": "2026-10-15", "description": "FUTURE TRANSFER",
                 "amount_cents": -9000},
            ],
        }, headers=headers).json()
    # entity_id, not account_id: a trend keyed by entity filters on
    # transactions.entity_id, so attributing them to the account entity
    # is what puts them in the series at all.
    for row in imported["unmatched"]:
        client.patch(
            f"/transactions/{row['id']}",
            json={
                "category_id": "transfers_account_transfer",
                "entity_id": account["id"],
            },
            headers=headers,
        )
    _freeze(monkeypatch, "2026-09-19")

    body = client.get(
        "/trend?entity_id=" + account["id"] + "&months=2", headers=headers
    ).json()

    assert body["transfers_excluded_count"] == 1, (
        "only the August transfer is inside the window 2026-08-01..2026-09-19. "
        "The June one is before it and the October one is after it; counting "
        "either means the excluded figure uses a different window than the "
        f"buckets do. Got {body['transfers_excluded_count']}."
    )
    assert body["transfers_excluded_cents"] == -7000


def test_the_frozen_clock_actually_reaches_the_query(
    client, test_settings, monkeypatch
):
    """Proves the freeze the tests above depend on.

    Without this the freeze is unverified, and a mutation replacing
    `_now_anchor()` with the literal "now" passes the whole file --
    measured. It passes because the real clock happens to sit in the
    same month as the anchors those tests pick, so they run unfrozen and
    still agree. They would keep passing today and start failing in a
    later month, for a reason no failure message would name.

    So this one anchors in a DIFFERENT YEAR, to a window the real clock
    cannot produce: if the monkeypatch is not reaching the query, the
    2025 rows are outside any window "now" could open and the series
    comes back empty.
    """
    headers = _auth(test_settings)
    _trend_rows(
        client,
        headers,
        [("2025-02-10", -100), ("2025-03-10", -200), ("2025-09-10", -400)],
    )
    _freeze(monkeypatch, "2025-03-20")

    body = client.get("/trend?category_id=food&months=2", headers=headers).json()

    assert [i["month"] for i in body["items"]] == ["2025-02", "2025-03"], (
        "the frozen clock did not reach the query -- the window was built "
        f"from the real date, so these tests prove nothing: {body['items']}"
    )
    assert body["items"][0]["total"] == -100


def test_trend_does_not_reach_into_the_future(client, test_settings, monkeypatch):
    """The window had no upper bound at all, only a lower one.

    A future-dated row therefore appeared in the series whatever `months`
    said, so neither "exactly N buckets" nor "the latest bucket is
    month-to-date" was true while one existed. Found while writing the
    frozen-clock test above, which produced a third bucket six months
    ahead of its own window.
    """
    headers = _auth(test_settings)
    _trend_rows(
        client,
        headers,
        [("2025-02-10", -100), ("2025-03-10", -200), ("2025-09-10", -400)],
    )
    _freeze(monkeypatch, "2025-03-20")

    body = client.get("/trend?category_id=food&months=2", headers=headers).json()

    assert [i["month"] for i in body["items"]] == ["2025-02", "2025-03"], (
        f"a row dated after the anchor reached the series: {body['items']}"
    )


# --- gaps and overlaps are different shapes (ticket T-13) -------------------


def test_an_overlap_entry_carries_no_gap_fields(client, test_settings):
    """The defect this shape change fixes, asserted directly.

    An overlap used to be reported in fields named `gap_start`/`gap_end`
    with a NEGATIVE `days`. Every value was correct and the entry was
    unreadable without the source: a model summarising the overlaps array
    sees gap-named fields on a thing that is not a gap, and reporting "a
    gap on 2026-04-09" is the reasonable reading. A false gap is the
    expensive direction -- a gap is the case this tool exists to raise.
    """
    headers = _auth(test_settings)
    account = client.post(
        "/entities",
        json={"type": "account", "name": "SCRATCH-impl-1-overlap-shape"},
        headers=headers,
    ).json()
    for start, end in (("2026-01-01", "2026-01-31"), ("2026-01-31", "2026-02-28")):
        created = client.post(
            "/statements",
            json={
                "account_id": account["id"],
                "period_start": start,
                "period_end": end,
            },
            headers=headers,
        )
        assert created.status_code == 200, created.text

    body = _coverage(client, test_settings)
    assert body["gaps"] == []
    assert len(body["overlaps"]) == 1
    overlap = body["overlaps"][0]

    # The whole point: no field on an overlap is spelled like a gap.
    assert not [key for key in overlap if key.startswith("gap")], overlap
    assert overlap["overlap_start"] == "2026-01-31"
    assert overlap["overlap_end"] == "2026-01-31"
    assert overlap["days"] == 1, "one day is covered twice"


def test_a_gap_keeps_its_own_shape_and_a_positive_count(client, test_settings):
    """The companion. A change that renamed BOTH entries, or made gaps
    negative to match, would pass the test above and break the case that
    actually matters."""
    headers = _auth(test_settings)
    account = client.post(
        "/entities",
        json={"type": "account", "name": "SCRATCH-impl-1-gap-shape"},
        headers=headers,
    ).json()
    for start, end in (("2026-01-01", "2026-01-31"), ("2026-03-01", "2026-03-31")):
        client.post(
            "/statements",
            json={
                "account_id": account["id"],
                "period_start": start,
                "period_end": end,
            },
            headers=headers,
        )

    body = _coverage(client, test_settings)
    assert body["overlaps"] == []
    assert len(body["gaps"]) == 1
    gap = body["gaps"][0]

    assert not [key for key in gap if key.startswith("overlap")], gap
    assert gap["gap_start"] == "2026-02-01", "the first MISSING day"
    assert gap["gap_end"] == "2026-02-28", "the last MISSING day"
    assert gap["days"] == 28, "days MISSING between the two periods"


def test_both_counts_are_positive_so_the_sign_carries_nothing(client, test_settings):
    """The sign used to be the only thing distinguishing the two cases.

    A reader holding one entry and no description could not tell which
    kind it was; now the field names carry it and the number never has
    to.
    """
    headers = _auth(test_settings)
    account = client.post(
        "/entities",
        json={"type": "account", "name": "SCRATCH-impl-1-both-shapes"},
        headers=headers,
    ).json()
    for start, end in (
        ("2026-01-01", "2026-01-31"),
        ("2026-01-31", "2026-02-28"),
        ("2026-04-01", "2026-04-30"),
    ):
        client.post(
            "/statements",
            json={
                "account_id": account["id"],
                "period_start": start,
                "period_end": end,
            },
            headers=headers,
        )

    body = _coverage(client, test_settings)
    assert body["gaps"] and body["overlaps"], body
    for entry in body["gaps"] + body["overlaps"]:
        assert entry["days"] > 0, entry


def test_a_multi_day_overlap_reads_forwards(client, test_settings):
    """A one-day overlap cannot tell start from end.

    The first shape test uses statements that touch on a single day, so
    `overlap_start == overlap_end` and swapping them is undetectable --
    measured: reversing the pair in the source failed nothing. A fixture
    that cannot distinguish the thing it asserts is the same vacuity as a
    corpus with no ambiguous ties in it.

    Two days of genuine overlap, so the range has a direction to get
    wrong.
    """
    headers = _auth(test_settings)
    account = client.post(
        "/entities",
        json={"type": "account", "name": "SCRATCH-impl-1-multiday-overlap"},
        headers=headers,
    ).json()
    # The second period starts two days BEFORE the first one ends.
    for start, end in (("2026-01-01", "2026-01-31"), ("2026-01-30", "2026-02-28")):
        client.post(
            "/statements",
            json={
                "account_id": account["id"],
                "period_start": start,
                "period_end": end,
            },
            headers=headers,
        )

    body = _coverage(client, test_settings)
    assert len(body["overlaps"]) == 1, body
    overlap = body["overlaps"][0]

    assert overlap["overlap_start"] == "2026-01-30"
    assert overlap["overlap_end"] == "2026-01-31"
    assert overlap["overlap_start"] < overlap["overlap_end"], (
        "the overlap range reads backwards"
    )
    assert overlap["days"] == 2, "30th and 31st are both covered twice"


def test_the_named_range_is_exactly_what_days_counts_on_both_lists(
    client, test_settings
):
    """ONE convention, pinned on both lists (ticket T-14).

    The two entries sit side by side with parallel field names, so a
    reader learns the convention from whichever one they see first. Before
    this, a gap's bounds BRACKETED the hole (the last covered day and the
    first covered day after it) while an overlap's bounds WERE the
    overlapping span -- so `range == days` held for overlaps and was off
    by two for gaps, and the same-looking shapes obeyed different rules.

    Asserted as an identity rather than against fixed dates, so it holds
    for any periods rather than for the ones I happened to choose.
    """
    headers = _auth(test_settings)
    account = client.post(
        "/entities",
        json={"type": "account", "name": "SCRATCH-impl-1-range-equals-days"},
        headers=headers,
    ).json()
    # A gap (Feb missing) and an overlap (Apr 29-30 doubled), one account.
    for start, end in (
        ("2026-01-01", "2026-01-31"),
        ("2026-03-01", "2026-03-31"),
        ("2026-03-29", "2026-04-30"),
    ):
        client.post(
            "/statements",
            json={
                "account_id": account["id"],
                "period_start": start,
                "period_end": end,
            },
            headers=headers,
        )

    body = _coverage(client, test_settings)
    assert body["gaps"], "no gap produced -- this test would assert nothing"
    assert body["overlaps"], "no overlap produced -- likewise"

    for entry in body["gaps"]:
        span = (
            date.fromisoformat(entry["gap_end"])
            - date.fromisoformat(entry["gap_start"])
        ).days + 1
        assert span == entry["days"], (
            f"a gap's named range spans {span} days but reports "
            f"days={entry['days']}: {entry}"
        )

    for entry in body["overlaps"]:
        span = (
            date.fromisoformat(entry["overlap_end"])
            - date.fromisoformat(entry["overlap_start"])
        ).days + 1
        assert span == entry["days"], (
            f"an overlap's named range spans {span} days but reports "
            f"days={entry['days']}: {entry}"
        )


def test_the_description_states_one_convention_and_drops_the_old_claim(
    test_settings,
):
    """Exact sentence, plus a NEGATIVE assertion that the old one is gone.

    The previous pins were keyword lower bounds, so reverting the
    description to its pre-#145 text -- including "the days between
    them", which is meaningless for an overlap -- left the suite green.
    A pin that only checks the new words are PRESENT cannot notice the
    old claim coming back beside them.
    """
    import httpx

    from q_core_mcp.client import QCoreClient
    from q_core_mcp.server import build_server

    client = QCoreClient(
        test_settings,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
    )
    import anyio

    tools = {t.name: t for t in anyio.run(build_server(client).list_tools)}
    description = tools["statement_coverage"].description or ""

    assert (
        "ONE CONVENTION ON BOTH LISTS: the named range IS what days counts"
        in description
    ), description
    # The claim that was wrong for overlaps, and must not return.
    assert "the days between them" not in description, (
        "the pre-#145 wording is back in the description: an overlap has "
        "no days BETWEEN the two statements, they intersect"
    )

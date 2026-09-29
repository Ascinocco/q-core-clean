"""Archiving an import, and the property that makes it real.

The flag flipping is the easy half. The half that goes wrong silently is
every aggregate that sums transactions directly: unless each one stops
counting archived rows, archiving changes nothing a user can see and the
totals keep reporting rows they were told were removed. That is a
wrong-number-that-looks-right, the same class as the statement-scoped
dedup bug.

So these tests assert the *totals*, not the flag. A test that only
asserted `archived_at IS NOT NULL` would pass against an implementation
where archiving does nothing at all.
"""

from tests.source_import_support import seed_source_batch

import sqlite3

import pytest


def _auth(settings):
    return {"Authorization": f"Bearer {settings.api_token}"}


@pytest.fixture()
def imported(client, test_settings):
    """An account with one imported statement of three transactions."""
    headers = _auth(test_settings)
    account = client.post(
        "/entities", json={"type": "account", "name": "Chequing"}, headers=headers
    ).json()
    vehicle = client.post(
        "/entities", json={"type": "vehicle", "name": "Toyota"}, headers=headers
    ).json()
    category = client.get("/categories", headers=headers).json()["items"][0]
    response = seed_source_batch(client, json={
            "account_id": account["id"],
            "period_start": "2026-02-01",
            "period_end": "2026-02-28",
            "transactions": [
                {"txn_date": "2026-02-03", "description": "SHELL", "amount_cents": -5000},
                {"txn_date": "2026-02-10", "description": "MARTZ", "amount_cents": -3000},
                {"txn_date": "2026-02-20", "description": "PAYCHEQUE", "amount_cents": 100000},
            ],
        }, headers=headers)
    assert response.status_code == 200, response.text
    statement_id = response.json()["statement_id"]
    rows = client.get(
        "/transactions", params={"account_id": account["id"], "limit": 200},
        headers=headers,
    ).json()["items"]
    # Categorise and link one row, so the aggregates have something to report.
    client.patch(
        f"/transactions/{rows[0]['id']}",
        json={"category_id": category["id"], "entity_id": vehicle["id"]},
        headers=headers,
    )
    return {
        "headers": headers,
        "account": account,
        "vehicle": vehicle,
        "category": category,
        "statement_id": statement_id,
        "rows": rows,
    }


def _archive_statement(client, imported):
    return client.post(
        f"/statements/{imported['statement_id']}/archive", headers=imported["headers"]
    )


# --- the flag, and then the things that actually matter -------------------


def test_archiving_a_statement_archives_its_transactions(client, imported):
    """The cascade, in one transaction.

    A statement archived without its rows is exactly the silent-wrong-number
    state this ticket warns about: the statement disappears from view while
    every total still counts its transactions.
    """
    response = _archive_statement(client, imported)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["archived_transactions"] == 3

    remaining = client.get(
        "/transactions", params={"account_id": imported["account"]["id"], "limit": 200},
        headers=imported["headers"],
    ).json()
    assert remaining["items"] == []
    assert remaining["total"] == 0


def test_spending_summary_stops_counting_archived_rows(client, imported):
    headers = imported["headers"]
    before = client.get("/spending_summary", headers=headers).json()
    # `items[].total` is the money; the envelope's own `total` is the row
    # count. Asserting the money, because the row count going to zero is
    # also what a broken aggregate that returns nothing would do.
    assert any(row["total"] for row in before["items"]), "nothing to lose"

    _archive_statement(client, imported)

    after = client.get("/spending_summary", headers=headers).json()
    assert all(row["total"] == 0 for row in after["items"])


def test_trend_stops_counting_archived_rows(client, imported):
    headers = imported["headers"]
    params = {"category_id": imported["category"]["id"], "months": 12}
    before = client.get("/trend", params=params, headers=headers).json()
    assert any(p["total"] for p in before["items"]), "nothing to lose"

    _archive_statement(client, imported)

    after = client.get("/trend", params=params, headers=headers).json()
    assert all(p["total"] == 0 for p in after["items"])


def test_cost_of_ownership_stops_counting_archived_rows(client, imported):
    headers = imported["headers"]
    url = f"/entities/{imported['vehicle']['id']}/cost_of_ownership"
    before = client.get(url, headers=headers).json()
    assert before["total"] != 0, "nothing to lose"

    _archive_statement(client, imported)

    after = client.get(url, headers=headers).json()
    assert after["total"] == 0


# --- the interaction the ticket says to decide explicitly -----------------


def test_an_archived_period_blocks_implicit_reactivation(
    client, imported
):
    """Source imports never reactivate archived statements implicitly."""
    _archive_statement(client, imported)

    again = seed_source_batch(client, json={
            "account_id": imported["account"]["id"],
            "period_start": "2026-02-01",
            "period_end": "2026-02-28",
            "transactions": [
                {"txn_date": "2026-02-03", "description": "SHELL", "amount_cents": -5000},
            ],
        }, headers=imported["headers"])

    assert again.status_code == 409, again.text
    assert client.get('/transactions', headers=imported['headers']).json()['total'] == 0


def test_an_active_row_still_blocks_a_duplicate(client, imported):
    """The dedup rule itself must survive the change."""
    again = seed_source_batch(client, json={
            "account_id": imported["account"]["id"],
            "period_start": "2026-02-01",
            "period_end": "2026-02-28",
            "transactions": [{k: r[k] for k in ('txn_date', 'description', 'amount_cents')}
                             for r in sorted(imported['rows'], key=lambda row: row['txn_date'])],
        }, headers=imported["headers"])

    assert again.json()["created"] == 0
    assert again.json()["recognized"] == 3


# --- reversibility and audit ---------------------------------------------


def test_unarchiving_a_statement_restores_its_rows_to_every_total(client, imported):
    headers = imported["headers"]
    url = f"/entities/{imported['vehicle']['id']}/cost_of_ownership"
    before = client.get(url, headers=headers).json()["total"]

    _archive_statement(client, imported)
    response = client.post(
        f"/statements/{imported['statement_id']}/unarchive", headers=headers
    )

    assert response.status_code == 200, response.text
    assert client.get(url, headers=headers).json()["total"] == before


def test_archived_rows_are_still_there_to_be_audited(client, imported, test_settings):
    """Archived, not deleted. The point of the system is that what was
    imported stays answerable afterwards."""
    _archive_statement(client, imported)

    connection = sqlite3.connect(test_settings.db_path)
    try:
        rows = connection.execute(
            "SELECT COUNT(*) FROM transactions WHERE archived_at IS NOT NULL"
        ).fetchone()[0]
    finally:
        connection.close()

    assert rows == 3, "the rows survive; only their visibility changes"


def test_archiving_reports_how_many_rows_were_hand_edited(client, imported):
    """Report, do not refuse.

    Nothing is destroyed by archiving, so refusing would be too strong.
    But re-importing the corrected file brings rows WITHOUT the hand
    categorisation, and nobody gets an error for that — the user simply
    loses the work and finds out when a summary looks thin. So the count
    goes in front of them at the moment they can act on it.
    """
    response = _archive_statement(client, imported)

    body = response.json()
    assert body["hand_edited"] == 1, "one row was categorised by hand in the fixture"
    assert body["hand_edited_ids"] == [imported["rows"][0]["id"]], (
        "which rows, not just how many -- a count says something was lost, "
        "the ids let the caller look at it before re-importing"
    )


def test_a_rule_matched_row_does_not_count_as_hand_edited(client, test_settings):
    """The definition has to exclude what the import did itself.

    An import assigns categories from the merchant rules, so counting
    "category_id is not null" would report every rule-matched row as hand
    work. The warning would then fire on every statement, and a warning
    that fires when nothing is wrong is one people learn to skip.
    """
    headers = _auth(test_settings)
    account = client.post(
        "/entities", json={"type": "account", "name": "Visa"}, headers=headers
    ).json()
    category = client.get("/categories", headers=headers).json()["items"][0]
    client.post(
        "/merchant_rules",
        json={"pattern": "PETRO", "category_id": category["id"], "actor": "test"},
        headers=headers,
    )
    statement = seed_source_batch(client, json={
            "account_id": account["id"],
            "period_start": "2026-03-01",
            "period_end": "2026-03-31",
            "transactions": [
                {"txn_date": "2026-03-05", "description": "PETRO CANADA",
                 "amount_cents": -6000},
            ],
        }, headers=headers).json()

    rows = client.get(
        "/transactions", params={"account_id": account["id"]}, headers=headers
    ).json()["items"]
    assert rows[0]["category_id"] is not None, "the rule matched, so there is a category"

    body = client.post(
        f"/statements/{statement['statement_id']}/archive", headers=headers
    ).json()

    assert body["hand_edited"] == 0, "a rule match is not a hand edit"
    assert body["hand_edited_ids"] == []


def test_reimporting_into_an_archived_period_leaves_the_old_rows_archived(
    client, imported
):
    """An attempted import cannot resurrect a statement or its old rows."""
    _archive_statement(client, imported)

    seed_source_batch(client, json={
            "account_id": imported["account"]["id"],
            "period_start": "2026-02-01",
            "period_end": "2026-02-28",
            "transactions": [
                {"txn_date": "2026-02-03", "description": "SHELL", "amount_cents": -5000},
            ],
        }, headers=imported["headers"])

    active = client.get(
        "/transactions", params={"account_id": imported["account"]["id"], "limit": 200},
        headers=imported["headers"],
    ).json()
    assert active["total"] == 0, "the refused import creates no active rows"

    statements = client.get(
        "/statements", params={"account_id": imported["account"]["id"]},
        headers=imported["headers"],
    ).json()
    assert statements["total"] == 0, "the statement remains archived"


def test_archiving_one_transaction_leaves_its_siblings_alone(client, imported):
    headers = imported["headers"]
    target = imported["rows"][1]

    response = client.post(f"/transactions/{target['id']}/archive", headers=headers)

    assert response.status_code == 200, response.text
    remaining = client.get(
        "/transactions", params={"account_id": imported["account"]["id"], "limit": 200},
        headers=headers,
    ).json()
    assert remaining["total"] == 2
    assert target["id"] not in [row["id"] for row in remaining["items"]]


def test_archived_rows_leave_the_reported_side_totals_too(client, test_settings):
    """The fields beside the total, which are easy to forget.

    #93 added `uncategorized_cents` and `transfers_excluded_cents/count`
    alongside spending_summary's rows. They exist so an exclusion cannot
    read as completeness — so if an archived row still counted here, it
    would be reported as uncategorized-or-excluded by one field while
    appearing in no total at all. The arithmetic would stop adding up and
    nothing would say why, which is the precise failure these fields were
    added to prevent, arriving through the door this ticket opened.

    Both feed from one function, so routing that through the view fixes
    both at once — which is the argument for one scope rather than a
    filter per aggregate, tested rather than asserted.
    """
    headers = _auth(test_settings)
    account = client.post(
        "/entities", json={"type": "account", "name": "Chequing"}, headers=headers
    ).json()
    statement = seed_source_batch(client, json={
            "account_id": account["id"],
            "period_start": "2026-04-01",
            "period_end": "2026-04-30",
            "transactions": [
                {"txn_date": "2026-04-05", "description": "UNMATCHED THING",
                 "amount_cents": -7700},
            ],
        }, headers=headers).json()

    before = client.get("/spending_summary", headers=headers).json()
    assert before["uncategorized_cents"] == -7700, "nothing to lose"

    client.post(
        f"/statements/{statement['statement_id']}/archive", headers=headers
    )

    after = client.get("/spending_summary", headers=headers).json()
    assert after["uncategorized_cents"] == 0, (
        "an archived row is not uncategorized spending; it is not spending"
    )
    assert after["transfers_excluded_count"] == 0
    assert after["transfers_excluded_cents"] == 0

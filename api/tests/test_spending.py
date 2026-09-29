"""Contracts for the unauthenticated, loopback-only spending report."""

from tests.source_import_support import seed_source_batch

from api.tests.reader_routes import assert_reader_only_get

# split part 2: the read-only routes now need a credential like every route.
READER = {"Authorization": "Bearer test-token"}


def _auth(test_settings):
    return {"Authorization": f"Bearer {test_settings.api_token}"}


def _account(client, test_settings, name, status="active"):
    return client.post(
        "/entities", json={"type": "account", "name": name, "status": status}, headers=_auth(test_settings)
    ).json()


def _import(client, test_settings, account, rows, period_start="2026-06-01", period_end="2026-06-30"):
    if not rows:
        # Reporting fixture: explicit coverage metadata, not an empty import.
        response = client.post('/statements', headers=_auth(test_settings), json={
            'account_id': account['id'], 'period_start': period_start, 'period_end': period_end})
        assert response.status_code == 200, response.text
        return {}
    response = seed_source_batch(client, json={
            "account_id": account["id"], "period_start": period_start, "period_end": period_end, "transactions": rows,
        }, headers=_auth(test_settings))
    assert response.status_code == 200, response.text
    transactions = client.get(f"/transactions?account_id={account['id']}", headers=_auth(test_settings)).json()["items"]
    return {item["description"]: item for item in transactions}


def test_spending_periods_is_public_but_writes_remain_authenticated(client, test_settings):
    account = _account(client, test_settings, "Spend account")
    rows = _import(client, test_settings, account, [{"txn_date": "2026-06-02", "description": "TST- BREW #123", "amount_cents": -1234}])
    client.patch(f"/transactions/{rows['TST- BREW #123']['id']}", json={"category_id": "food_dining"}, headers=_auth(test_settings))

    report = client.get("/spending/periods?granularity=month&date_from=2026-06-01&date_to=2026-06-30", headers=READER)
    assert report.status_code == 200, report.text
    period = report.json()["periods"][0]
    assert period["by_group"] == {"food": 1234}
    assert period["merchants"]["food"] == [{"merchant": "BREW", "cents": 1234, "count": 1}]
    assert client.post("/entities", json={"type": "account", "name": "Unauthorised"}).status_code == 401


def test_spending_periods_excludes_transfers_income_and_marks_reporting_coverage_gaps(client, test_settings):
    account = _account(client, test_settings, "Coverage account")
    _account(client, test_settings, "Unreported account")
    inactive = _account(client, test_settings, "Inactive account", status="inactive")
    partial = _account(client, test_settings, "Partial reporting account")
    rows = _import(client, test_settings, account, [
        {"txn_date": "2026-06-02", "description": "GROCER", "amount_cents": -1000},
        {"txn_date": "2026-06-03", "description": "PAYMENT", "amount_cents": -3000},
        {"txn_date": "2026-06-04", "description": "PAY", "amount_cents": 5000},
    ])
    for description, category in (("GROCER", "food_dining"), ("PAYMENT", "transfers_credit_card_payment"), ("PAY", "income_salary")):
        client.patch(f"/transactions/{rows[description]['id']}", json={"category_id": category}, headers=_auth(test_settings))
    _import(client, test_settings, inactive, [], period_start="2026-06-01", period_end="2026-06-30")
    _import(client, test_settings, partial, [], period_start="2026-06-03", period_end="2026-06-30")
    report = client.get("/spending/periods?granularity=week&date_from=2026-06-01&date_to=2026-06-07", headers=READER).json()
    period = report["periods"][0]
    assert period["spend_cents"] == 1000
    assert period["transfers_out_cents"] == 3000
    assert period["income_cents"] == 5000
    assert period["missing_accounts"] == ["Partial reporting account"]
    assert report["reporting_account_count"] == 2
    assert set(report["coverage"]) == {"Coverage account", "Partial reporting account"}


def test_spending_periods_combines_adjacent_statement_coverage(client, test_settings):
    account = _account(client, test_settings, "Adjacent coverage account")
    _import(client, test_settings, account, [], period_start="2026-06-01", period_end="2026-06-15")
    _import(client, test_settings, account, [], period_start="2026-06-16", period_end="2026-06-30")

    report = client.get("/spending/periods?granularity=month&date_from=2026-06-01&date_to=2026-06-30", headers=READER).json()

    assert report["complete_periods"] == 1
    assert report["periods"][0]["missing_accounts"] == []


def test_spending_periods_refuses_bad_or_too_long_ranges(client):
    assert client.get("/spending/periods?granularity=month&date_from=2026-06-02&date_to=2026-06-01", headers=READER).status_code == 422
    assert client.get("/spending/periods?granularity=month&date_from=2025-01-01&date_to=2026-01-02", headers=READER).status_code == 422





def test_spending_read_routes_need_a_reader_and_contain_no_write_method(client):
    """The old public exemption is gone (split, part 2): a credential is required."""
    from api.spending import router
    assert_reader_only_get(router.routes)
    assert {r.path for r in router.routes} == {"/spending/periods", "/statements/coverage"}
    assert client.get("/spending/periods").status_code == 401


def test_breakdown_amounts_cannot_wrap_and_the_page_is_wide(client):
    """Ticket T-55: amounts wrapped mid-number ($1,/234) in a narrow card.

    Layout is verified in a real browser; this pins the two properties that
    fixed it, so a later style edit cannot quietly undo either.
    """
    html = client.get("/ui/spending", headers=READER).text
    assert '<span class="merchant-amount">${money(r.cents)}</span>' in html
    assert ".merchant-amount{flex:none;white-space:nowrap" in html
    # Needs the id: shell.css loads after this page's <style> and wins a tie.
    assert "#page-content.app-content{max-width:1600px}" in html
    # The override is page-local: the shared shell keeps its default width.
    assert "max-width:1600px" not in client.get("/ui/briefs", headers=READER).text

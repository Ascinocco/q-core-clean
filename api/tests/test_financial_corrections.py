
from tests.source_import_support import seed_source_batch
import sqlite3
from uuid import uuid4

import pytest

AUTH = {'Authorization': 'Bearer test-token'}


def setup_rows(client):
    account = client.post('/entities', headers=AUTH, json={'type': 'account', 'name': 'Synthetic account'}).json()['id']
    statements = []
    for month in (1, 2):
        result = seed_source_batch(client, headers=AUTH, json={
            'account_id': account, 'period_start': f'2026-{month:02}-01', 'period_end': f'2026-{month:02}-28',
            'transactions': [{'txn_date': f'2026-{month:02}-10', 'description': 'CAFE', 'amount_cents': -500}]}).json()
        statements.append(result['statement_id'])
    tx = client.get('/transactions', headers=AUTH, params={'account_id': account}).json()['items']
    return statements, next(r for r in tx if r['statement_id'] == statements[0])


def period_body(**updates):
    return {'correction_id': str(uuid4()), 'actor': 'test', 'reason': 'Verified source period',
            'expected_period_start': '2026-01-01', 'expected_period_end': '2026-01-28',
            'period_start': '2026-01-01', 'period_end': '2026-01-27', **updates}


def test_period_correction_audit_idempotency_and_stale_refusal(client):
    statements, row = setup_rows(client)
    body = period_body();path = f'/statements/{statements[0]}/correct-period'
    response = client.post(path, headers=AUTH, json=body)
    assert response.status_code == 200
    audit = response.json()
    assert audit['before']['period_end'] == '2026-01-28'
    assert audit['after']['period_end'] == '2026-01-27'
    assert not audit['replayed']
    assert client.get(f"/financial-corrections/{body['correction_id']}", headers=AUTH).json() == audit
    assert client.post(path, headers=AUTH, json=body).json()['replayed']
    assert client.post(path, headers=AUTH, json={**body, 'reason': 'different'}).status_code == 409
    assert client.post(path, headers=AUTH, json={**body, 'correction_id': str(uuid4())}).status_code == 409
    assert client.get(f"/transactions/{row['id']}", headers=AUTH).json() == row


def test_reassignment_preserves_all_fields_except_parent_even_outside_dates(client, test_settings):
    statements, row = setup_rows(client)
    client.patch(f"/transactions/{row['id']}", headers=AUTH, json={'category_id': 'food_groceries'})
    before = client.get(f"/transactions/{row['id']}", headers=AUTH).json()
    with sqlite3.connect(test_settings.db_path) as db:
        db.row_factory = sqlite3.Row
        full_before = dict(db.execute('SELECT * FROM transactions WHERE id = ?', (row['id'],)).fetchone())
    body = {'correction_id': str(uuid4()), 'actor': 'test', 'reason': 'Verified source table',
            'expected_statement_id': statements[0], 'statement_id': statements[1]}
    path = f"/transactions/{row['id']}/reassign-statement"
    result = client.post(path, headers=AUTH, json=body)
    assert result.status_code == 200
    assert client.get(f"/transactions/{row['id']}", headers=AUTH).json() == {**before, 'statement_id': statements[1]}
    assert client.post(path, headers=AUTH, json=body).json()['replayed']
    assert client.post(path, headers=AUTH, json={**body, 'correction_id': str(uuid4())}).status_code == 409
    with sqlite3.connect(test_settings.db_path) as db:
        db.row_factory = sqlite3.Row
        full_after = dict(db.execute('SELECT * FROM transactions WHERE id = ?', (row['id'],)).fetchone())
    assert full_after == {**full_before, 'statement_id': statements[1]}


@pytest.mark.parametrize('failure', ['cross-account', 'archived-destination', 'archived-source', 'archived-row', 'missing'])
def test_reassignment_refuses_unsafe_targets(client, failure):
    statements, row = setup_rows(client)
    target = statements[1]
    if failure == 'cross-account':
        other, _ = setup_rows(client); target = other[1]
    elif failure == 'archived-destination':
        client.post(f'/statements/{target}/archive', headers=AUTH)
    elif failure == 'archived-source':
        client.post(f'/statements/{statements[0]}/archive', headers=AUTH)
    elif failure == 'archived-row':
        client.post(f"/transactions/{row['id']}/archive", headers=AUTH)
    else:
        target = 'missing'
    body = {'correction_id': str(uuid4()), 'actor': 'test', 'reason': 'test',
            'expected_statement_id': statements[0], 'statement_id': target}
    assert client.post(f"/transactions/{row['id']}/reassign-statement", headers=AUTH, json=body).status_code == (409 if failure == 'cross-account' else 404)
    assert client.get(f"/financial-corrections/{body['correction_id']}", headers=AUTH).status_code == 404


@pytest.mark.parametrize('change', [{'actor': '  '}, {'reason': ' '}, {'correction_id': 'not-uuid'},
                                   {'period_end': '2025-01-01'}, {'amount_cents': 123}])
def test_invalid_corrections_refused(client, change):
    statements, _ = setup_rows(client)
    assert client.post(f'/statements/{statements[0]}/correct-period', headers=AUTH, json=period_body(**change)).status_code == 422


def test_unique_period_conflict_rolls_back(client):
    statements, _ = setup_rows(client)
    body = period_body(period_start='2026-02-01', period_end='2026-02-28')
    assert client.post(f'/statements/{statements[0]}/correct-period', headers=AUTH, json=body).status_code == 409
    assert client.get(f'/statements/{statements[0]}', headers=AUTH).json()['period_end'] == '2026-01-28'
    assert client.get(f"/financial-corrections/{body['correction_id']}", headers=AUTH).status_code == 404


def test_audit_failure_rolls_back_mutation(client, test_settings):
    statements, _ = setup_rows(client)
    with sqlite3.connect(test_settings.db_path) as db:
        db.execute("CREATE TRIGGER refuse_audit BEFORE INSERT ON financial_corrections BEGIN SELECT RAISE(ABORT, 'test'); END")
    assert client.post(f'/statements/{statements[0]}/correct-period', headers=AUTH, json=period_body()).status_code == 409
    assert client.get(f'/statements/{statements[0]}', headers=AUTH).json()['period_end'] == '2026-01-28'


def test_corrections_require_auth(client):
    assert client.post('/statements/missing/correct-period', json=period_body()).status_code == 401
    assert client.get(f'/financial-corrections/{uuid4()}').status_code == 401


def test_noop_corrections_refused(client):
    statements, row = setup_rows(client)
    assert client.post(f'/statements/{statements[0]}/correct-period', headers=AUTH,
                       json=period_body(period_end='2026-01-28')).status_code == 409
    body = {'correction_id': str(uuid4()), 'actor': 'test', 'reason': 'test',
            'expected_statement_id': statements[0], 'statement_id': statements[0]}
    assert client.post(f"/transactions/{row['id']}/reassign-statement", headers=AUTH, json=body).status_code == 409


def test_concurrent_conflicting_corrections_have_one_winner(client, test_settings):
    from concurrent.futures import ThreadPoolExecutor
    statements, _ = setup_rows(client)
    path = f'/statements/{statements[0]}/correct-period'
    requests = [period_body(period_end='2026-01-26'), period_body(period_end='2026-01-27')]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda body: client.post(path, headers=AUTH, json=body), requests))
    assert sorted(r.status_code for r in responses) == [200, 409]
    with sqlite3.connect(test_settings.db_path) as db:
        assert db.execute('SELECT COUNT(*) FROM financial_corrections').fetchone()[0] == 1


from tests.source_import_support import seed_source_batch
import pytest
from api.transaction_identity import comparison_description

AUTH = {'Authorization': 'Bearer test-token'}


@pytest.mark.parametrize('left,right,equal', [
    ('Transfer [REDACTED] Bank', 'Transfer Bank', True),
    ('  NEW\tMERCHANT\n#123 ', 'NEW MERCHANT #123', True),
    ('[REDACTED] CAFE [REDACTED]', 'CAFE', True),
    ('CAFE[REDACTED]', 'CAFE', False),
    ('CAFE [redacted]', 'CAFE', False),
    ('CAFE [REDACTED],', 'CAFE', False),
    ('CAFE', 'Cafe', False),
    ('PAYMENT – THANK YOU', 'PAYMENT - THANK YOU', False),
    ('CAFE #12', 'CAFE #13', False),
    ('Transfer ****1234', 'Transfer ****5678', False),
    ('[REDACTED]', '', False),
])
def test_comparison_is_narrow(left, right, equal):
    assert (comparison_description(left) == comparison_description(right)) is equal
    assert comparison_description(comparison_description(left)) == comparison_description(left)


@pytest.mark.parametrize('reverse', [False, True])
def test_multiplicity_and_original_fields_preserved(client, reverse):
    from tests.source_import_support import source_payload
    account = client.post('/entities', headers=AUTH, json={'type':'account','name':'Synthetic'}).json()['id']
    old, new = ('CAFE [REDACTED]', 'CAFE') if not reverse else ('CAFE', 'CAFE [REDACTED]')
    payload = source_payload({'account_id':account, 'period_start':'2026-06-01', 'period_end':'2026-06-30',
        'transactions':[{'txn_date':'2026-06-15','description':old,'amount_cents':-500} for _ in range(2)]})
    def commit():
        preview = client.post('/source_imports/preview', json=payload, headers=AUTH).json()
        return client.post('/source_imports/commit', json={**payload,'review_token':preview['review_token']}, headers=AUTH)
    assert commit().json()['created'] == 2
    originals = client.get('/transactions', headers=AUTH).json()['items']
    for row in payload['transactions']:
        row['description'] = new  # same physical records, changed extraction description
    assert commit().json()['recognized'] == 2
    assert client.get('/transactions', headers=AUTH).json()['items'] == originals
    client.post(f"/transactions/{originals[0]['id']}/archive", headers=AUTH)
    assert commit().status_code == 409  # must not re-create an archived occurrence


def test_merchant_differences_still_insert(client):
    account = client.post('/entities', headers=AUTH, json={'type': 'account', 'name': 'Synthetic'}).json()['id']
    descriptions = ['CAFE', 'Cafe', 'CAFE #12', 'CAFE #13', 'CAFE[REDACTED]', 'CAFE ****1234', 'CAFE ****5678']
    response = seed_source_batch(client, headers=AUTH, json={
        'account_id': account, 'period_start': '2026-06-01', 'period_end': '2026-06-30',
        'transactions': [{'txn_date': '2026-06-15', 'description': d, 'amount_cents': -500} for d in descriptions]})
    assert response.json()['created'] == len(descriptions)

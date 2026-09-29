"""Old cached clients cannot bypass source review or write legacy imports."""
import pytest
from tests.source_import_support import seed_source_batch

# split part 2: the read-only routes now need a credential like every route.
READER = {"Authorization": "Bearer test-token"}

AUTH = {'Authorization': 'Bearer test-token'}


@pytest.mark.parametrize('headers', [AUTH, {}])
def test_retired_route_is_absent_and_cannot_write(client, headers):
    account = client.post('/entities', headers=AUTH, json={'type':'account','name':'Synthetic'}).json()['id']
    body = {'account_id':account, 'period_start':'2026-06-01','period_end':'2026-06-30',
            'transactions':[{'txn_date':'2026-06-15','description':'CAFE','amount_cents':-500}]}
    assert seed_source_batch(client, json=body, headers=AUTH).status_code == 200
    before = {path:client.get(path, headers=AUTH).json() for path in ('/transactions','/statements')}
    response = client.post('/import_statement', json=body, headers=headers)
    # Without a credential the gate refuses before routing; with one, the route is gone.
    assert response.status_code == (404 if headers else 401)
    assert '/import_statement' not in client.get('/openapi.json', headers=READER).json()['paths']
    assert {path:client.get(path, headers=AUTH).json() for path in before} == before


def test_production_does_not_import_the_historical_simulator():
    import ast
    from api.config import REPO_ROOT
    for directory in ('api','q_core_mcp'):
        for path in (REPO_ROOT/directory).rglob('*.py'):
            if 'tests' in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node,ast.ImportFrom):
                    assert not (node.module or '').startswith('financial_evals'), path
                elif isinstance(node,ast.Import):
                    assert not any(n.name.startswith('financial_evals') for n in node.names), path


from tests.source_import_support import seed_source_tool
from uuid import uuid4

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from api.config import get_settings
from q_core_mcp.client import QCoreClient
from q_core_mcp.server import build_server


def test_corrections_round_trip_through_mcp(test_settings, call_tool):
    from api.main import app
    app.dependency_overrides[get_settings] = lambda: test_settings
    server = build_server(QCoreClient(test_settings, transport=httpx.ASGITransport(app=app)))
    try:
        account = call_tool(server, 'create_entity', {'entity_type': 'account', 'name': 'Synthetic'})
        batches = []
        for month in (1, 2):
            batches.append(seed_source_tool(call_tool, server, {
                'account_id': account['id'], 'period_start': f'2026-{month:02}-01',
                'period_end': f'2026-{month:02}-28', 'transactions': [
                    {'txn_date': f'2026-{month:02}-10', 'description': 'CAFE', 'amount_cents': -500}]}))
        rows = call_tool(server, 'list_transactions', {'all': True})['items']
        row = next(r for r in rows if r['statement_id'] == batches[0]['statement_id'])
        request = {'statement_id': batches[0]['statement_id'], 'expected_period_start': '2026-01-01',
                   'expected_period_end': '2026-01-28', 'period_start': '2026-01-01', 'period_end': '2026-01-27',
                   'actor': 'test', 'reason': 'Source reviewed', 'correction_id': str(uuid4())}
        result = call_tool(server, 'correct_statement_period', request)
        assert result['after']['period_end'] == '2026-01-27'
        assert call_tool(server, 'get_financial_correction', {'correction_id': request['correction_id']}) == result
        assert call_tool(server, 'correct_statement_period', request)['replayed']
        with pytest.raises(ToolError):
            call_tool(server, 'correct_statement_period', {**request, 'correction_id': str(uuid4())})
        result = call_tool(server, 'reassign_transaction_statement', {
            'transaction_id': row['id'], 'expected_statement_id': batches[0]['statement_id'],
            'statement_id': batches[1]['statement_id'], 'actor': 'test', 'reason': 'Source table reviewed',
            'correction_id': str(uuid4())})
        assert result['before']['statement_id'] == batches[0]['statement_id']
        after = call_tool(server, 'list_transactions', {'all': True})['items']
        assert next(r for r in after if r['id'] == row['id']) == {**row, 'statement_id': batches[1]['statement_id']}
    finally:
        app.dependency_overrides.clear()

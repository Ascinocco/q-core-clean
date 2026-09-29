"""The retired tool is unreachable; intake uses source preview/commit."""
import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from q_core_mcp.client import QCoreClient
from q_core_mcp.server import build_server
from tests.source_import_support import seed_source_tool


def test_retired_import_tool_is_absent_and_cached_calls_fail(test_settings, call_tool, tools_by_name):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={})
    server = build_server(QCoreClient(test_settings, transport=httpx.MockTransport(handler)))
    assert 'import_statement' not in tools_by_name(server)
    with pytest.raises(ToolError, match='Unknown tool'):
        call_tool(server, 'import_statement', {})
    assert requests == []


def test_current_import_descriptions_preserve_intake_contract(test_settings, tools_by_name):
    server = build_server(QCoreClient(test_settings, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))))
    tools = tools_by_name(server)
    preview = tools['preview_source_import']
    commit = tools['commit_source_import']
    assert preview.annotations.read_only_hint is True
    assert commit.annotations.read_only_hint is not True
    assert 'negative' in preview.description.lower()
    assert 'unmatched' in commit.description
    assert 'update_transaction' in commit.description
    assert 'apply_transaction_as_rule' in commit.description


def test_source_retry_preserves_rows_and_surfaces_uncategorized(test_settings, call_tool):
    from api.config import get_settings
    from api.main import app
    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = build_server(QCoreClient(test_settings, transport=httpx.ASGITransport(app=app)))
        account = call_tool(server, 'create_entity', {'entity_type':'account','name':'Synthetic'})
        batch = {'account_id':account['id'], 'period_start':'2026-06-01', 'period_end':'2026-06-30',
                 'transactions':[{'txn_date':'2026-06-15','description':'CAFE','amount_cents':-500}]}
        first = seed_source_tool(call_tool, server, batch)
        second = seed_source_tool(call_tool, server, batch)
        assert first['created'] == 1
        assert len(first['unmatched']) == 1
        assert first['unmatched'][0]['category_id'] is None
        assert second['created'] == 0 and second['recognized'] == 1
        assert first['statement_id'] == second['statement_id']
        assert call_tool(server, 'list_transactions', {'all':True})['total'] == 1
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize('invalid', ['period', 'account', 'empty'])
def test_source_preview_rejects_invalid_imports(test_settings, call_tool, invalid):
    from api.config import get_settings
    from api.main import app
    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        server = build_server(QCoreClient(test_settings, transport=httpx.ASGITransport(app=app)))
        account = call_tool(server, 'create_entity', {'entity_type':'vehicle' if invalid=='account' else 'account','name':'Synthetic'})
        batch = {'account_id':account['id'], 'period_start':'2026-06-30' if invalid=='period' else '2026-06-01',
                 'period_end':'2026-06-15', 'transactions':[] if invalid=='empty' else [
                 {'txn_date':'2026-06-10','description':'CAFE','amount_cents':-500}]}
        with pytest.raises(ToolError):
            seed_source_tool(call_tool, server, batch)
        assert call_tool(server, 'list_transactions', {'all':True})['total'] == 0
    finally:
        app.dependency_overrides.clear()

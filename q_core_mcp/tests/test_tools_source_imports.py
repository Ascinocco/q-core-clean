import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from api.config import get_settings
from q_core_mcp.client import QCoreClient
from q_core_mcp.server import build_server


def test_source_import_mcp_roundtrip(test_settings,call_tool):
    from api.main import app
    app.dependency_overrides[get_settings]=lambda:test_settings
    server=build_server(QCoreClient(test_settings,transport=httpx.ASGITransport(app=app)))
    try:
        account=call_tool(server,'create_entity',{'entity_type':'account','name':'Synthetic'})
        payload={'account_id':account['id'],'period_start':'2026-06-01','period_end':'2026-06-30',
                 'source_id':'a'*64,'actor':'test','transactions':[{'source_row':'csv:1','txn_date':'2026-06-15','description':'CAFE','amount_cents':-500}]}
        p=call_tool(server,'preview_source_import',{'payload':payload})
        result=call_tool(server,'commit_source_import',{'payload':{**payload,'review_token':p['review_token']}})
        assert result['created']==1
        payload['source_id']='b'*64
        p=call_tool(server,'preview_source_import',{'payload':payload})
        assert p['needs_review']==1
        with pytest.raises(ToolError):
            call_tool(server,'commit_source_import',{'payload':{**payload,'review_token':p['review_token']}})
        payload['transactions'][0].update(decision='link',transaction_id=result['rows'][0]['transaction_id'],reason='Synthetic reviewed duplicate')
        linked=call_tool(server,'commit_source_import',{'payload':{**payload,'review_token':p['review_token']}})
        assert linked['linked']==1 and linked['created']==0
    finally:
        app.dependency_overrides.clear()

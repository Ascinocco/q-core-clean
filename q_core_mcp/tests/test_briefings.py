"""Production-style lazy wiring and the actual briefing/inbox request schemas."""
import asyncio
from uuid import uuid4

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from api.config import get_settings
from api.main import _LazyClient, app
from q_core_mcp.client import QCoreClient
from q_core_mcp.server import build_server


@pytest.fixture()
def briefing_server(test_settings, monkeypatch):
    app.dependency_overrides[get_settings] = lambda: test_settings
    monkeypatch.setattr('api.main.get_settings', lambda: test_settings)
    monkeypatch.setattr('api.main.QCoreClient', lambda settings: QCoreClient(settings, transport=httpx.ASGITransport(app=app)))
    try:
        yield build_server(_LazyClient())
    finally:
        app.dependency_overrides.clear()


def test_create_revise_and_resolve_through_lazy_mcp(briefing_server, call_tool):
    def call(name, **args):
        return call_tool(briefing_server, name, args)
    window = call('get_briefing_window', kind='weekly')
    from datetime import datetime
    assert datetime.fromisoformat(window['period_start']).weekday() == 0
    record = call('create_review_item', payload={'source_key':str(uuid4()),'content':{'summary':'Quote amount needs confirmation.'}, 'actor':'Test','note':'Synthetic discrepancy'})['item']
    doc = {'title':'Weekly report','html':'<h1>This week</h1><p>Quote amount needs confirmation.</p>',
           'period_start':window['period_start'],'as_of':window['as_of'],
           'coverage':[{'source':'gmail','status':'unavailable','detail':'Synthetic offline run.'}]}
    saved = call('create_artifact', payload={'request_id':str(uuid4()),'kind':'weekly','document':doc,'actor':'Test','note':'Requested'})
    call('revise_review_item', item_id=record['id'], payload={'expected_revision':1,'content':record['content'],'state':'resolved','revisit_date':None,'actor':'Test','note':'Operator verified the recorded amount without changing it.'})
    assert call('list_review_items', actionable=True, all=True)['total'] == 0
    assert call('get_artifact', artifact_id=saved['id'])['document']['html'] == doc['html']
    doc['html'] = '<h1>This week</h1><p>Quote reviewed.</p>'
    updated = call('revise_artifact', artifact_id=saved['id'], payload={'expected_revision':1,'document':doc,'actor':'Test','note':'Requested wording revision'})
    assert updated['revision'] == 2
    assert 'needs confirmation' in call('get_artifact',artifact_id=saved['id'],revision=1)['document']['html']
    assert call('list_artifacts',all=True)['total'] == 1
    history = call('list_review_history', item_id=record['id'], all=True)
    assert [r['state'] for r in history['items']] == ['open','resolved']
    assert call('list_reminder_completions',date_from=window['date_from'],date_to=window['date_to'],as_of=window['as_of'],all=True)['total'] == 0
    assert call('list_reminder_calendar_links',all=True)['total'] == 0


def test_expected_paging_preserves_metadata_and_refuses_plan_change(test_settings):
    from q_core_mcp.paging import list_request
    async def exercise(changing):
        def handler(request):
            offset=int(request.url.params.get('offset',0))
            return httpx.Response(200,json={'items':[{'n':offset}], 'total':2,
                'plan_revision':'second' if changing and offset else 'first', 'gaps':['Missing tax amount'],
                'snapshots':[{'as_of':'2026-09-01'}], 'coverage':'Expectations only'})
        client=QCoreClient(test_settings,transport=httpx.MockTransport(handler))
        return await list_request(client,'/expected-payments',fetch_all=True,metadata_keys=('plan_revision','gaps','snapshots','coverage'))
    result=asyncio.run(exercise(False))
    assert result['total']==2 and result['gaps']==['Missing tax amount']
    assert result['plan_revision']=='first'
    with pytest.raises(ToolError, match='source_changed'):
        asyncio.run(exercise(True))

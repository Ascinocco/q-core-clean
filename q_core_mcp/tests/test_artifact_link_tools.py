"""Canvas link tools over MCP, through the production lazy wiring. Invented data only."""
from uuid import uuid4

import httpx
import pytest

from api.artifact_links import LIST_ARTIFACT_LINKS_ORDER
from api.config import get_settings
from api.main import _LazyClient, app
from q_core_mcp.client import QCoreClient
from q_core_mcp.server import build_server


@pytest.fixture()
def server(test_settings, monkeypatch):
    app.dependency_overrides[get_settings] = lambda: test_settings
    monkeypatch.setattr('api.main.get_settings', lambda: test_settings)
    monkeypatch.setattr('api.main.QCoreClient', lambda settings: QCoreClient(settings, transport=httpx.ASGITransport(app=app)))
    try:
        yield build_server(_LazyClient())
    finally:
        app.dependency_overrides.clear()


def test_link_list_embed_and_unlink(server, call_tool):
    def call(tool, **args):
        return call_tool(server, tool, args)
    project = call('create_entity', entity_type='project', name='Invented project')
    board = call('create_board', entity_id=project['id'], title='Work')
    ticket = call('create_ticket', board_id=board['id'], ticket_type='task', title='Build the queue', actor='owner')
    document = {'title': 'Queue notes', 'html': '<p>Design notes.</p>',
                'period_start': '2026-09-21T00:00:00-04:00', 'as_of': '2026-09-23T09:00:00-04:00',
                'coverage': [{'source': 'gmail', 'status': 'unavailable', 'detail': 'Synthetic offline run.'}]}
    saved = call('create_artifact', payload={'request_id': str(uuid4()), 'kind': 'daily', 'document': document,
                                             'actor': 'Test', 'note': 'Requested'})

    linked = call('link_artifact', artifact_id=saved['id'], target_type='ticket', target_id=ticket['id'], actor='Test')
    assert linked['created'] is True
    links = call('list_artifact_links', artifact_id=saved['id'], all=True)
    assert [(r['id'], r['title']) for r in links['items']] == [(linked['id'], 'Build the queue')]
    detail = call('get_ticket', ticket_id=ticket['id'])
    assert detail['artifact_count'] == 1 and detail['artifacts'][0]['title'] == 'Queue notes'
    assert [r['id'] for r in call('list_artifacts', ticket_id=ticket['id'], all=True)['items']] == [saved['id']]
    assert call('unlink_artifact', artifact_id=saved['id'], link_id=linked['id']) == {'deleted': True}
    assert call('get_ticket', ticket_id=ticket['id'])['artifact_count'] == 0


def test_descriptions_carry_the_contracts(server):
    import anyio
    tools = {t.name: t for t in anyio.run(server.list_tools)}
    assert LIST_ARTIFACT_LINKS_ORDER in tools['list_artifact_links'].description
    assert 'artifacts lists linked Canvas docs (artifact_count is the true total).' in tools['get_ticket'].description

"""edit_artifact over MCP, through the production lazy wiring. Invented data only."""
from uuid import uuid4

import httpx
import pytest

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


def test_edit_artifact_round_trip(server, call_tool):
    def call(name, **args):
        return call_tool(server, name, args)
    document = {'title': 'Weekly report', 'html': '<h1>This week</h1><p>Quote needs confirmation.</p>',
                'period_start': '2026-09-21T00:00:00-04:00', 'as_of': '2026-09-23T09:00:00-04:00',
                'coverage': [{'source': 'gmail', 'status': 'unavailable', 'detail': 'Synthetic offline run.'}]}
    saved = call('create_artifact', payload={'request_id': str(uuid4()), 'kind': 'weekly', 'document': document,
                                             'actor': 'Test', 'note': 'Requested'})
    payload = {'expected_revision': 1, 'replacements': [{'old': 'needs confirmation', 'new': 'was confirmed'}],
               'actor': 'Test', 'note': 'Typo-sized fix'}
    edited = call('edit_artifact', artifact_id=saved['id'], payload=payload)
    assert edited['revision'] == 2
    assert call('get_artifact', artifact_id=saved['id'])['document']['html'] == '<h1>This week</h1><p>Quote was confirmed.</p>'
    # An exact retry returns the applied revision rather than failing.
    assert call('edit_artifact', artifact_id=saved['id'], payload=payload)['revision'] == 2
    assert call('get_artifact', artifact_id=saved['id'])['current_revision'] == 2

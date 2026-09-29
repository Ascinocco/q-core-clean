"""edit_diagram and the summary view over MCP (Canvas). Invented data only."""
import copy
import re
from uuid import uuid4

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from api.canvas_diagram_ops import COMMON_OPS, GRAPH_OPS, SEQUENCE_OPS, Operation
from api.config import get_settings
from api.main import _LazyClient, app
from api.tests.test_canvas_diagram import EXAMPLES
from q_core_mcp.client import QCoreClient
from q_core_mcp.server import build_server
from q_core_mcp.tools.canvas import COMMON_OPERATIONS, GRAPH_OPERATIONS, SEQUENCE_OPERATIONS


@pytest.fixture()
def server(test_settings, monkeypatch):
    app.dependency_overrides[get_settings] = lambda: test_settings
    monkeypatch.setattr('api.main.get_settings', lambda: test_settings)
    monkeypatch.setattr('api.main.QCoreClient', lambda settings: QCoreClient(settings, transport=httpx.ASGITransport(app=app)))
    try:
        yield build_server(_LazyClient())
    finally:
        app.dependency_overrides.clear()


def test_edit_diagram_then_summary(server, call_tool):
    def call(tool, **args):
        return call_tool(server, tool, args)
    saved = call('create_artifact', payload={'request_id': str(uuid4()), 'kind': 'diagram',
                                             'document': copy.deepcopy(EXAMPLES['sequence']), 'actor': 'Test', 'note': 'Created'})
    payload = {'expected_revision': 1, 'actor': 'Test', 'note': 'Edited over MCP', 'operations': [
        {'op': 'add_participant', 'participant': {'id': 'cache', 'label': 'Cache', 'role': 'database'}, 'where': {'after': 'api'}},
        {'op': 'insert_item', 'item': {'kind': 'message', 'id': 'm8', 'source': 'api', 'target': 'cache', 'label': 'evict'},
         'where': {'fragment': 'f1', 'section': 1}}]}
    edited = call('edit_diagram', artifact_id=saved['id'], payload=payload)
    assert edited['revision'] == 2
    summary = call('get_artifact', artifact_id=saved['id'], view='summary')['summary']
    assert [p['id'] for p in summary['participants']] == ['claude', 'mcp', 'api', 'cache', 'db']
    m8 = next(row for row in summary['outline'] if row['id'] == 'm8')
    assert (m8['parent'], m8['section'], m8['depth']) == ('f1', 1, 1)
    assert call('get_artifact', artifact_id=saved['id'])['document']['type'] == 'sequence'
    # MCP surfaces the API's error code, not its HTTP status (decisions-log,
    # "API error codes remain visible through MCP"): a 409 arrives as conflict.
    with pytest.raises(ToolError, match=r'edit_diagram: conflict: Artifact changed; reread before editing'):
        call('edit_diagram', artifact_id=saved['id'], payload={**payload, 'note': 'Stale'})


def names(text):
    return re.findall(r'[a-z_]+(?=\(|,|$)', text)


def test_the_described_vocabulary_equals_the_union():
    listed = names(GRAPH_OPERATIONS) + names(SEQUENCE_OPERATIONS) + names(COMMON_OPERATIONS)
    union = {model.model_fields['op'].annotation.__args__[0] for model in Operation.__origin__.__args__}
    assert len(listed) == len(set(listed)), 'an operation is listed twice'
    assert set(listed) == union
    assert (names(GRAPH_OPERATIONS), names(SEQUENCE_OPERATIONS), names(COMMON_OPERATIONS)) == (
        list(GRAPH_OPS), list(SEQUENCE_OPS), list(COMMON_OPS))


def test_descriptions(server):
    import anyio
    tools = {t.name: t for t in anyio.run(server.list_tools)}
    assert 'use view=summary before editing a diagram' in tools['get_artifact'].description
    for vocabulary in (GRAPH_OPERATIONS, SEQUENCE_OPERATIONS, COMMON_OPERATIONS):
        assert vocabulary in tools['edit_diagram'].description

"""PATCH /artifacts/{id}/diagram and the summary view (Canvas). Invented data only."""
import copy
import json
import time
from uuid import uuid4

from api.tests.test_canvas_diagram import EXAMPLES

HEADERS = {'Authorization': 'Bearer test-token'}
# A seven-digit run, built rather than written.
RUN = ''.join(str(i % 10) for i in range(1, 8))


def create(client, document, kind='diagram'):
    response = client.post('/artifacts', headers=HEADERS, json={
        'request_id': str(uuid4()), 'kind': kind, 'document': document, 'actor': 'Test', 'note': 'Created'})
    assert response.status_code == 200, response.text
    return response.json()


def create_example(client, name):
    return create(client, copy.deepcopy(EXAMPLES[name]))


def patch(client, artifact_id, operations, expected_revision=1, note='Edit'):
    return client.patch(f'/artifacts/{artifact_id}/diagram', headers=HEADERS, json={
        'expected_revision': expected_revision, 'operations': operations, 'actor': 'Test', 'note': note})


def current(client, artifact_id):
    return client.get(f'/artifacts/{artifact_id}', headers=HEADERS).json()


def details(response):
    return response.json()['error']['details']


# M5
def test_patch_roundtrip(client):
    saved = create_example(client, 'architecture')
    response = patch(client, saved['id'], [
        {'op': 'update_node', 'id': 'api', 'set': {'label': 'FastAPI'}},
        {'op': 'add_node', 'node': {'id': 'cache', 'label': 'Cache', 'role': 'cache', 'group': 'home-server'}},
        {'op': 'add_edge', 'edge': {'id': 'e5', 'source': 'api', 'target': 'cache'}}])
    assert response.status_code == 200, response.text
    edited = response.json()
    assert edited['current_revision'] == 2 and edited['revision'] == 2 and edited['note'] == 'Edit'
    assert current(client, saved['id']) == edited
    assert [n['label'] for n in edited['document']['nodes'] if n['id'] in ('api', 'cache')] == ['FastAPI', 'Cache']
    # Revision 1 is kept as it was.
    first = client.get(f"/artifacts/{saved['id']}?revision=1", headers=HEADERS).json()
    assert first['document'] == saved['document']


def test_set_meta_title_shows_in_the_list(client):
    saved = create_example(client, 'flow')
    assert patch(client, saved['id'], [{'op': 'set_meta', 'set': {'title': 'Intake, revised'}}]).status_code == 200
    rows = client.get('/artifacts?kind=diagram', headers=HEADERS).json()['items']
    assert [r['title'] for r in rows] == ['Intake, revised']


# M2 through the API
def test_a_failed_batch_writes_nothing(client):
    saved = create_example(client, 'architecture')
    response = patch(client, saved['id'], [
        {'op': 'update_node', 'id': 'owner', 'set': {'label': 'Me'}},
        {'op': 'remove_edge', 'id': 'e404'},
        {'op': 'remove_node', 'id': 'claude', 'dependents': 'cascade'}])
    assert response.status_code == 400
    assert response.json()['error'] == {'code': 'invalid_reference',
                                        'message': 'operations[1] (remove_edge): unknown edge at id'}
    assert current(client, saved['id'])['current_revision'] == 1


# M3 through the API
def test_refusals_and_missing_policies(client):
    arch, seq = create_example(client, 'architecture'), create_example(client, 'sequence')
    refused = patch(client, arch['id'], [{'op': 'remove_node', 'id': 'api', 'dependents': 'refuse'}])
    assert refused.status_code == 409 and refused.json()['error']['code'] == 'conflict'
    assert 'edges[2], edges[3]' in refused.json()['error']['message']
    assert patch(client, arch['id'], [{'op': 'remove_group', 'id': 'home-server', 'members': 'refuse'}]).status_code == 409
    assert patch(client, seq['id'], [{'op': 'remove_item', 'id': 'f1'}]).status_code == 409
    assert patch(client, seq['id'], [{'op': 'remove_participant', 'id': 'db', 'dependents': 'refuse'}]).status_code == 409
    missing = patch(client, arch['id'], [{'op': 'remove_node', 'id': 'api'}])
    assert missing.status_code == 422
    assert details(missing)[0]['field'].startswith('operations.0')


# M4 through the API: error fields
def test_validation_errors_name_the_operation_or_the_document(client):
    arch, seq = create_example(client, 'architecture'), create_example(client, 'sequence')
    wrong_type = patch(client, seq['id'], [{'op': 'add_node', 'node': {'id': 'x', 'label': 'X'}}])
    assert wrong_type.status_code == 422
    assert details(wrong_type) == [{'field': 'operations.0',
                                    'message': 'operations[0] (add_node): not available for a sequence diagram'}]
    cycle = patch(client, arch['id'], [
        {'op': 'add_group', 'group': {'id': 'inner', 'label': 'Inner', 'parent': 'home-server'}},
        {'op': 'move_to_group', 'ids': ['home-server'], 'group': 'inner'}])
    assert cycle.status_code == 422
    assert details(cycle)[0]['field'] == 'document'
    assert details(cycle)[0]['message'].startswith('the edited diagram is invalid: ')
    assert 'nesting cycle' in details(cycle)[0]['message']
    malformed = patch(client, arch['id'], [{'op': 'add_node', 'node': {'id': 'x', 'label': ''}}])
    assert malformed.status_code == 422 and details(malformed)[0]['field'] == 'operations.0.add_node.node.label'
    unknown = patch(client, arch['id'], [{'op': 'teleport', 'id': 'x'}])
    assert unknown.status_code == 422
    assert current(client, arch['id'])['current_revision'] == 1


# M6
def test_retry_and_conflict(client, test_settings):
    import sqlite3
    saved = create_example(client, 'architecture')
    operations = [{'op': 'update_node', 'id': 'db', 'set': {'label': 'SQLite (WAL)'}}]
    first = patch(client, saved['id'], operations)
    retry = patch(client, saved['id'], operations)
    assert first.status_code == retry.status_code == 200
    assert first.json()['revision'] == retry.json()['revision'] == 2
    assert first.json()['document'] == retry.json()['document']
    other = patch(client, saved['id'], [{'op': 'update_node', 'id': 'db', 'set': {'label': 'Postgres'}}])
    assert other.status_code == 409
    assert other.json()['error'] == {'code': 'conflict', 'message': 'Artifact changed; reread before editing'}
    assert patch(client, saved['id'], operations, note='A different note').status_code == 409
    assert patch(client, saved['id'], operations, expected_revision=7).status_code == 409
    with sqlite3.connect(test_settings.db_path) as db:
        revisions = [r[0] for r in db.execute('SELECT revision FROM artifact_revisions WHERE artifact_id = ?', (saved['id'],))]
    assert sorted(revisions) == [1, 2]


# M7
def test_not_a_diagram(client):
    brief = create(client, {'title': 'Morning brief', 'html': '<p>Review the roof quote.</p>',
                            'period_start': '2026-09-21T00:00:00-04:00', 'as_of': '2026-09-23T09:00:00-04:00',
                            'coverage': [{'source': 'gmail', 'status': 'unavailable', 'detail': 'Fixture.'}]}, kind='daily')
    response = patch(client, brief['id'], [{'op': 'set_meta', 'set': {'title': 'X'}}])
    assert response.status_code == 409
    assert response.json()['error']['message'] == 'edit_diagram only edits diagram artifacts; use edit_artifact or revise_artifact'
    assert current(client, brief['id']) == brief
    assert patch(client, str(uuid4()), [{'op': 'set_meta', 'set': {'title': 'X'}}]).status_code == 404


# M8
def test_privacy(client):
    saved = create_example(client, 'architecture')
    named = patch(client, saved['id'], [{'op': 'update_node', 'id': 'owner', 'set': {'label': 'Owner: Alex Example'}}])
    assert named.status_code == 200, named.text
    assert [n['label'] for n in named.json()['document']['nodes'] if n['id'] == 'owner'] == ['Owner: [REDACTED]']

    id_refused = patch(client, saved['id'], [{'op': 'add_node', 'node': {'id': 'alex-example', 'label': 'Somebody'}}],
                       expected_revision=2)
    assert id_refused.status_code == 422
    assert details(id_refused)[0]['field'].startswith('document.nodes.')

    digits = patch(client, saved['id'], [{'op': 'update_node', 'id': 'owner', 'set': {'label': 'Call ' + RUN}}],
                   expected_revision=2)
    assert digits.status_code == 422
    assert RUN not in digits.text
    envelope = client.patch(f"/artifacts/{saved['id']}/diagram", headers=HEADERS, json={
        'expected_revision': 2, 'operations': [{'op': 'set_meta', 'set': {'title': 'X'}}],
        'actor': 'Alex Example', 'note': 'Edit'})
    assert envelope.status_code == 200 and envelope.json()['actor'] == '[REDACTED]'
    assert current(client, saved['id'])['current_revision'] == 3


# M9
ARCHITECTURE_SUMMARY = {
    'type': 'architecture', 'title': 'q-core containers',
    'counts': {'nodes': 5, 'edges': 4, 'groups': 1, 'notes': 1, 'participants': 0, 'items': 0, 'messages': 0,
               'fragments': 0},
    'nodes': [{'id': 'owner', 'label': 'Owner', 'role': 'person', 'pinned': False},
              {'id': 'claude', 'label': 'Claude Code', 'role': 'client', 'pinned': False},
              {'id': 'mcp', 'label': 'q_core_mcp', 'role': 'service', 'group': 'home-server', 'pinned': False},
              {'id': 'api', 'label': 'FastAPI app', 'role': 'service', 'group': 'home-server', 'pinned': False},
              {'id': 'db', 'label': 'SQLite', 'role': 'database', 'group': 'home-server', 'pinned': False}],
    'edges': [{'id': 'e1', 'source': 'owner', 'target': 'claude', 'label': 'chats'},
              {'id': 'e2', 'source': 'claude', 'target': 'mcp', 'label': 'MCP over Tailscale'},
              {'id': 'e3', 'source': 'mcp', 'target': 'api', 'label': 'HTTP'},
              {'id': 'e4', 'source': 'api', 'target': 'db', 'label': 'reads/writes'}],
    'groups': [{'id': 'home-server', 'label': 'Home server (NixOS)'}],
    'notes': [{'id': 'n1', 'attach': 'db'}],
}
SEQUENCE_SUMMARY = {
    'type': 'sequence', 'title': 'Revise an artifact',
    'counts': {'nodes': 0, 'edges': 0, 'groups': 0, 'notes': 0, 'participants': 4, 'items': 9, 'messages': 7,
               'fragments': 1},
    'participants': [{'id': 'claude', 'label': 'Claude'}, {'id': 'mcp', 'label': 'q_core_mcp'},
                     {'id': 'api', 'label': 'API'}, {'id': 'db', 'label': 'SQLite'}],
    'outline': [
        {'id': 'm1', 'kind': 'message', 'depth': 0, 'source': 'claude', 'target': 'mcp', 'label': 'revise_artifact(id, payload)'},
        {'id': 'm2', 'kind': 'message', 'depth': 0, 'source': 'mcp', 'target': 'api', 'label': 'PUT /artifacts/{id}'},
        {'id': 'n1', 'kind': 'note', 'depth': 0},
        {'id': 'm3', 'kind': 'message', 'depth': 0, 'source': 'api', 'target': 'api', 'label': 'screen_payload'},
        {'id': 'f1', 'kind': 'fragment', 'depth': 0, 'operator': 'alt'},
        {'id': 'm4', 'kind': 'message', 'depth': 1, 'parent': 'f1', 'section': 0, 'source': 'api', 'target': 'db',
         'label': 'insert revision'},
        {'id': 'm5', 'kind': 'message', 'depth': 1, 'parent': 'f1', 'section': 0, 'source': 'api', 'target': 'mcp',
         'label': '200 artifact'},
        {'id': 'm6', 'kind': 'message', 'depth': 1, 'parent': 'f1', 'section': 1, 'source': 'api', 'target': 'mcp',
         'label': '409 conflict'},
        {'id': 'm7', 'kind': 'message', 'depth': 0, 'source': 'mcp', 'target': 'claude', 'label': 'result'},
    ],
}


def big_diagram(edges=600, description=60):
    nodes = [{'id': f'n{i}', 'label': f'Service {i}', 'role': 'service',
              'description': f'Handles the invented workload number {i} for the fixture. '.ljust(description, '.')}
             for i in range(300)]
    links = [{'id': f'e{i}', 'source': f'n{i % 300}', 'target': f'n{(i * 7 + 1) % 300}', 'label': f'call {i}',
              'line': 'dashed'} for i in range(edges)]
    return {'type': 'architecture', 'title': 'Big fixture', 'layout': {'direction': 'LR'}, 'nodes': nodes, 'edges': links}


def test_summary_view(client):
    arch, seq = create_example(client, 'architecture'), create_example(client, 'sequence')
    for saved, expected in ((arch, ARCHITECTURE_SUMMARY), (seq, SEQUENCE_SUMMARY)):
        response = client.get(f"/artifacts/{saved['id']}?view=summary", headers=HEADERS)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body['summary'] == expected
        assert 'document' not in body
        assert {k: v for k, v in body.items() if k != 'summary'} == {k: v for k, v in saved.items() if k != 'document'}

    brief = create(client, {'title': 'Morning brief', 'html': '<p>Review.</p>',
                            'period_start': '2026-09-21T00:00:00-04:00', 'as_of': '2026-09-23T09:00:00-04:00',
                            'coverage': [{'source': 'gmail', 'status': 'unavailable', 'detail': 'Fixture.'}]}, kind='daily')
    refused = client.get(f"/artifacts/{brief['id']}?view=summary", headers=HEADERS)
    assert refused.status_code == 422
    assert details(refused) == [{'field': 'query.view', 'message': 'summary is only available for diagram artifacts'}]
    assert client.get(f"/artifacts/{arch['id']}?view=everything", headers=HEADERS).status_code == 422

    # The dense case: 300 nodes, 600 labelled edges, short descriptions, and
    # notes with text. Edge labels stay in the summary because they help plan
    # edits, so its size is bounded at 65% here (lead decision, replacing the
    # ticket's 40%, which kept labels could not reach on this shape).
    dense = big_diagram()
    dense['notes'] = [{'id': f'note{i}', 'text': f'Invented note text {i}.', 'attach': f'n{i}'} for i in range(50)]
    for edge in dense['edges'][:100]:
        edge.update(arrow='both', tone='muted')
    big = create(client, dense)
    full_document = current(client, big['id'])['document']
    summary_document = client.get(f"/artifacts/{big['id']}?view=summary", headers=HEADERS).json()['summary']

    def keys(value):
        if isinstance(value, dict):
            return set(value) | {k for v in value.values() for k in keys(v)}
        if isinstance(value, list):
            return {k for v in value for k in keys(v)}
        return set()

    # (a) What the summary is for: no descriptions, note text, ports or styles.
    assert keys(summary_document).isdisjoint({'description', 'text', 'ports', 'line', 'arrow', 'style', 'tone',
                                              'position', 'layout'})
    assert 'Handles the invented workload' not in json.dumps(summary_document)
    assert 'Invented note text' not in json.dumps(summary_document)
    # (b) Size, measured and bounded.
    full, summary = len(json.dumps(full_document)), len(json.dumps(summary_document))
    print(f'summary is {summary / full:.1%} of the full document ({summary} of {full} bytes)')
    assert summary <= 0.65 * full, (summary, full)


# M10
def test_performance(client):
    big = create(client, big_diagram())
    operations = ([{'op': 'update_node', 'id': f'n{i}', 'set': {'label': f'Renamed {i}'}} for i in range(100)]
                  + [{'op': 'update_edge', 'id': f'e{i}', 'set': {'line': 'solid'}} for i in range(50)]
                  + [{'op': 'pin', 'id': f'n{i}', 'x': i, 'y': i} for i in range(50)])
    assert len(operations) == 200
    started = time.perf_counter()
    response = patch(client, big['id'], operations)
    elapsed = time.perf_counter() - started
    assert response.status_code == 200, response.text
    assert elapsed < 0.5, f'{elapsed * 1000:.0f} ms'

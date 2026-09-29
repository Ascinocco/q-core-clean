"""Artifact links to tickets and entities (Canvas). Invented data only."""
import sqlite3
from datetime import datetime, timedelta
from uuid import uuid4

HEADERS = {'Authorization': 'Bearer test-token'}


def brief(client, title='Morning brief'):
    document = {'title': title, 'html': '<p>Review the roof quote.</p>', 'period_start': '2026-09-21T00:00:00-04:00',
                'as_of': '2026-09-23T09:00:00-04:00',
                'coverage': [{'source': 'gmail', 'status': 'unavailable', 'detail': 'Not connected in this fixture.'}]}
    response = client.post('/artifacts', headers=HEADERS, json={'request_id': str(uuid4()), 'kind': 'daily',
                                                                'document': document, 'actor': 'Test', 'note': 'Requested'})
    assert response.status_code == 200, response.text
    return response.json()


def entity(client, name='Invented Car', type_='vehicle'):
    response = client.post('/entities', headers=HEADERS, json={'type': type_, 'name': name})
    assert response.status_code == 200, response.text
    return response.json()['id']


def ticket(client, title='Build the queue', board_id=None):
    if board_id is None:
        project = entity(client, 'Invented project', 'project')
        board_id = client.post('/boards', headers=HEADERS, json={'entity_id': project, 'title': 'Work'}).json()['id']
    response = client.post('/tickets', headers=HEADERS, json={'board_id': board_id, 'type': 'task', 'title': title, 'actor': 'owner'})
    assert response.status_code == 200, response.text
    return response.json()


def link(client, artifact_id, target_type, target_id):
    return client.post(f'/artifacts/{artifact_id}/links', headers=HEADERS,
                       json={'target_type': target_type, 'target_id': target_id, 'actor': 'Test'})


def link_rows(settings, **where):
    clause = ' AND '.join(f'{k} = ?' for k in where) or '1'
    with sqlite3.connect(settings.db_path) as db:
        return db.execute(f'SELECT COUNT(*) FROM artifact_links WHERE {clause}', tuple(where.values())).fetchone()[0]


# M1
def test_link_unlink_and_duplicate(client, test_settings):
    a, t = brief(client), ticket(client)
    first = link(client, a['id'], 'ticket', t['id'])
    assert first.status_code == 200, first.text
    row = first.json()
    assert row['created'] is True
    assert {k: row[k] for k in ('artifact_id', 'target_type', 'target_id', 'actor')} == {
        'artifact_id': a['id'], 'target_type': 'ticket', 'target_id': t['id'], 'actor': 'Test'}
    again = link(client, a['id'], 'ticket', t['id'])
    assert again.status_code == 200
    assert again.json()['created'] is False and again.json()['id'] == row['id']
    assert link_rows(test_settings) == 1
    unlink = client.delete(f"/artifacts/{a['id']}/links/{row['id']}", headers=HEADERS)
    assert unlink.status_code == 200 and unlink.json() == {'deleted': True}
    assert client.delete(f"/artifacts/{a['id']}/links/{row['id']}", headers=HEADERS).status_code == 404
    assert link_rows(test_settings) == 0


def test_unlink_through_the_wrong_artifact_is_404(client, test_settings):
    a, b, t = brief(client), brief(client, 'Other'), ticket(client)
    row = link(client, a['id'], 'ticket', t['id']).json()
    assert client.delete(f"/artifacts/{b['id']}/links/{row['id']}", headers=HEADERS).status_code == 404
    assert link_rows(test_settings) == 1


# M2
def test_ghost_ids(client):
    a, t = brief(client), ticket(client)
    ghost = str(uuid4())
    assert link(client, ghost, 'ticket', t['id']).status_code == 404
    for target_type in ('ticket', 'entity'):
        response = link(client, a['id'], target_type, ghost)
        assert response.status_code == 400
        assert response.json()['error']['code'] == 'invalid_reference'
    assert link(client, a['id'], 'note', t['id']).status_code == 422
    assert client.get(f'/artifacts/{ghost}/links', headers=HEADERS).status_code == 404
    for name in ('ticket_id', 'entity_id'):
        response = client.get(f'/artifacts?{name}={ghost}', headers=HEADERS)
        assert response.status_code == 400
        assert response.json()['error']['code'] == 'invalid_reference'


# M3
def test_list_both_directions(client):
    t, e = ticket(client), entity(client)
    a, b, c = brief(client, 'First'), brief(client, 'Second'), brief(client, 'Unlinked')
    for artifact_id in (a['id'], b['id']):
        assert link(client, artifact_id, 'ticket', t['id']).status_code == 200
    assert link(client, a['id'], 'entity', e).status_code == 200

    by_ticket = client.get(f"/artifacts?ticket_id={t['id']}", headers=HEADERS).json()
    assert {r['id'] for r in by_ticket['items']} == {a['id'], b['id']} and by_ticket['total'] == 2
    paged = client.get(f"/artifacts?ticket_id={t['id']}&limit=1", headers=HEADERS).json()
    assert paged['total'] == 2 and len(paged['items']) == 1
    both = client.get(f"/artifacts?ticket_id={t['id']}&entity_id={e}", headers=HEADERS).json()
    assert [r['id'] for r in both['items']] == [a['id']]
    counts = {r['id']: r['link_count'] for r in client.get('/artifacts', headers=HEADERS).json()['items']}
    assert counts == {a['id']: 2, b['id']: 1, c['id']: 0}

    links = client.get(f"/artifacts/{a['id']}/links", headers=HEADERS).json()
    assert links['total'] == 2
    assert [(r['target_type'], r['title'], r['name']) for r in links['items']] == [
        ('ticket', 'Build the queue', None), ('entity', None, 'Invented Car')]
    first = client.get(f"/artifacts/{a['id']}/links?limit=1", headers=HEADERS).json()
    assert first['total'] == 2 and len(first['items']) == 1


# M4
def test_get_ticket_embeds_artifacts(client, test_settings):
    t = ticket(client)
    a = brief(client, 'Linked brief')
    first = link(client, a['id'], 'ticket', t['id']).json()
    detail = client.get(f"/tickets/{t['id']}", headers=HEADERS).json()
    assert detail['artifact_count'] == 1
    ref = detail['artifacts'][0]
    assert ref['artifact_id'] == a['id'] and ref['kind'] == 'daily' and ref['title'] == 'Linked brief'
    assert ref['current_revision'] == 1 and ref['viewer_path'] == f"/ui/briefs/{a['id']}"
    # 100 more, inserted directly: the cap, not the create path, is under test.
    # A day after the real link, not a fixed date, so it stays "later" whenever this runs.
    later = (datetime.fromisoformat(first['created_at']) + timedelta(days=1)).isoformat()
    with sqlite3.connect(test_settings.db_path) as db:
        for n in range(100):
            artifact_id = str(uuid4())
            db.execute("INSERT INTO artifacts (id, request_id, request_hash, kind, created_at, updated_at, revision) "
                       "VALUES (?, ?, 'h', 'page', '2026-09-26T10:00:00+00:00', '2026-09-26T10:00:00+00:00', 1)",
                       (artifact_id, str(uuid4())))
            db.execute("INSERT INTO artifact_revisions (artifact_id, revision, payload, actor, note, created_at) "
                       "VALUES (?, 1, json_object('title', ?), 'Test', 'Direct', '2026-09-26T10:00:00+00:00')",
                       (artifact_id, f'Page {n}'))
            db.execute("INSERT INTO artifact_links (id, artifact_id, target_type, target_id, actor, created_at) "
                       "VALUES (?, ?, 'ticket', ?, 'Test', ?)",
                       (str(uuid4()), artifact_id, t['id'], later))
    detail = client.get(f"/tickets/{t['id']}", headers=HEADERS).json()
    assert len(detail['artifacts']) == 100 and detail['artifact_count'] == 101
    assert detail['artifacts'][0]['artifact_id'] == a['id']  # oldest link first
    assert detail['artifacts'][1]['viewer_path'].startswith('/ui/canvas/')


def test_get_ticket_without_links(client):
    detail = client.get(f"/tickets/{ticket(client)['id']}", headers=HEADERS).json()
    assert detail['artifacts'] == [] and detail['artifact_count'] == 0


# M5
def test_ticket_delete_removes_links(client, test_settings):
    t, e, a = ticket(client), entity(client), brief(client)
    link(client, a['id'], 'ticket', t['id'])
    kept = link(client, a['id'], 'entity', e).json()
    before = client.get(f"/artifacts/{a['id']}", headers=HEADERS).json()
    response = client.delete(f"/tickets/{t['id']}", headers=HEADERS)
    assert response.status_code == 200, response.text
    assert link_rows(test_settings, target_type='ticket', target_id=t['id']) == 0
    after = client.get(f"/artifacts/{a['id']}", headers=HEADERS)
    assert after.status_code == 200
    assert after.json()['current_revision'] == before['current_revision']
    assert after.json()['document'] == before['document']
    links = client.get(f"/artifacts/{a['id']}/links", headers=HEADERS).json()
    assert [r['id'] for r in links['items']] == [kept['id']]


# M6
def test_entity_delete_refused_while_linked(client):
    e, a = entity(client), brief(client)
    row = link(client, a['id'], 'entity', e).json()
    refused = client.delete(f'/entities/{e}', headers=HEADERS)
    assert refused.status_code == 409
    message = refused.json()['error']['message']
    assert 'artifact_links (1)' in message and 'unlink' in message
    client.delete(f"/artifacts/{a['id']}/links/{row['id']}", headers=HEADERS)
    assert client.delete(f'/entities/{e}', headers=HEADERS).status_code == 200
    assert client.get(f"/artifacts/{a['id']}", headers=HEADERS).status_code == 200


def test_an_entity_link_does_not_block_a_ticket_sharing_its_id(client, test_settings):
    """The registry counts only target_type='entity' rows."""
    e, a = entity(client), brief(client)
    with sqlite3.connect(test_settings.db_path) as db:
        db.execute("INSERT INTO artifact_links (id, artifact_id, target_type, target_id, actor, created_at) "
                   "VALUES (?, ?, 'ticket', ?, 'Test', '2026-09-26T00:00:00+00:00')", (str(uuid4()), a['id'], e))
    assert client.delete(f'/entities/{e}', headers=HEADERS).status_code == 200


# M7
def test_links_are_listed_oldest_first(client, test_settings):
    a = brief(client)
    targets = [ticket(client, f'Ticket {n}')['id'] for n in range(3)]
    ids = [link(client, a['id'], 'ticket', target).json()['id'] for target in targets]
    # Same created_at: rowid breaks the tie in insertion order.
    with sqlite3.connect(test_settings.db_path) as db:
        db.execute("UPDATE artifact_links SET created_at = '2026-09-26T00:00:00+00:00'")
    listed = client.get(f"/artifacts/{a['id']}/links", headers=HEADERS).json()['items']
    assert [r['id'] for r in listed] == ids
    offset = client.get(f"/artifacts/{a['id']}/links?limit=1&offset=2", headers=HEADERS).json()['items']
    assert [r['id'] for r in offset] == ids[2:]


# Ticket keys (ticket T-19): a ticket target and the ticket_id filter take
# a key in any case; the link stores the UUID.


def test_link_a_ticket_by_key_stores_the_uuid(client, test_settings):
    a, t = brief(client), ticket(client)
    assert t['key'] == 'IP-1'  # "Invented project" derives IP
    first = link(client, a['id'], 'ticket', 'ip-1')
    assert first.status_code == 200, first.text
    assert first.json()['target_id'] == t['id']
    # Idempotent across forms: the key and the UUID name one target.
    again = link(client, a['id'], 'ticket', t['id'])
    assert again.json()['created'] is False
    assert link_rows(test_settings, target_type='ticket', target_id=t['id']) == 1
    assert link(client, a['id'], 'ticket', 'IP-9').status_code == 400


def test_list_artifacts_by_ticket_key(client):
    a, other, t = brief(client), brief(client, 'Other'), ticket(client)
    link(client, a['id'], 'ticket', t['id'])
    for ref in ('IP-1', 'ip-1', t['id']):
        page = client.get(f'/artifacts?ticket_id={ref}', headers=HEADERS).json()
        assert [row['id'] for row in page['items']] == [a['id']], ref
    unknown = client.get('/artifacts?ticket_id=IP-9', headers=HEADERS)
    assert unknown.status_code == 400
    assert unknown.json()['error']['code'] == 'invalid_reference'
    assert other['id']


def test_deleting_a_ticket_by_key_removes_its_links(client, test_settings):
    a, t = brief(client), ticket(client)
    link(client, a['id'], 'ticket', 'IP-1')
    assert client.delete('/tickets/ip-1', headers=HEADERS).status_code == 200
    assert link_rows(test_settings, target_type='ticket', target_id=t['id']) == 0
    embedded = ticket(client, 'second')
    detail = client.get(f"/tickets/{embedded['key']}", headers=HEADERS).json()
    assert detail['artifacts'] == [] and detail['artifact_count'] == 0


def test_link_listing_shows_the_ticket_key(client):
    a, t, car = brief(client), ticket(client), entity(client)
    link(client, a['id'], 'ticket', t['id'])
    link(client, a['id'], 'entity', car)
    rows = client.get(f"/artifacts/{a['id']}/links", headers=HEADERS).json()['items']
    by_type = {row['target_type']: row for row in rows}
    assert by_type['ticket']['key'] == 'IP-1'
    assert by_type['ticket']['target_id'] == t['id']
    assert by_type['entity']['key'] is None

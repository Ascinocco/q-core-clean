"""Find-and-replace edits (Canvas). Invented data only."""
import sqlite3
from uuid import uuid4

import pytest

from api.artifact_kinds import KINDS

HEADERS = {'Authorization': 'Bearer test-token'}

BRIEF_REFUSAL = ('Content refused: use privacy-safe summaries and balanced static HTML '
                 'without attributes, links or active content')

# Never a literal: a seven-digit run in source is what the screen refuses.
DIGITS = ''.join(str(n) for n in range(1, 8))


def brief_document(html='<h1>Today</h1><p>Review the roof quote.</p>', title='Morning brief'):
    return {'title': title, 'html': html, 'period_start': '2026-09-21T00:00:00-04:00',
            'as_of': '2026-09-23T09:00:00-04:00', 'coverage': [
                {'source': 'gmail', 'status': 'unavailable', 'detail': 'Not connected in this fixture.'}]}


def create_brief(client, **document):
    body = dict(request_id=str(uuid4()), kind='daily', document=brief_document(**document), actor='Test', note='Requested')
    response = client.post('/artifacts', headers=HEADERS, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def edit(client, artifact_id, **changes):
    body = {'expected_revision': 1, 'actor': 'Test', 'note': 'Fix wording'}
    body.update(changes)
    return client.patch(f'/artifacts/{artifact_id}', headers=HEADERS, json=body)


def revisions(settings, artifact_id):
    with sqlite3.connect(settings.db_path) as db:
        return db.execute('SELECT COUNT(*) FROM artifact_revisions WHERE artifact_id = ?', (artifact_id,)).fetchone()[0]


def details(response):
    return response.json()['error']['details']


# M1
def test_edit_applies_in_order_and_writes_one_revision(client, test_settings):
    brief = create_brief(client)
    response = edit(client, brief['id'], replacements=[
        {'old': 'roof quote', 'new': 'gutter quote'},
        {'old': 'gutter quote.', 'new': 'gutter quote today.'}])
    assert response.status_code == 200, response.text
    value = response.json()
    assert value['revision'] == 2 and value['current_revision'] == 2
    assert value['document']['html'] == '<h1>Today</h1><p>Review the gutter quote today.</p>'
    assert value['document']['title'] == 'Morning brief'
    assert value['note'] == 'Fix wording'
    assert revisions(test_settings, brief['id']) == 2
    # Revision 1 is kept, untouched.
    first = client.get(f"/artifacts/{brief['id']}?revision=1", headers=HEADERS).json()
    assert first['document']['html'] == brief['document']['html']


def test_whitespace_in_replacements_is_kept(client):
    brief = create_brief(client)
    response = edit(client, brief['id'], replacements=[{'old': 'the roof', 'new': 'the  roof '}])
    assert response.status_code == 200, response.text
    assert 'the  roof  quote' in response.json()['document']['html']


# M2
def test_zero_and_multiple_matches_refuse_whole_edit(client, test_settings):
    brief = create_brief(client, html='<p>one</p><p>one</p><p>two</p>')
    missing = edit(client, brief['id'], replacements=[{'old': 'three', 'new': 'x'}])
    assert missing.status_code == 422
    assert details(missing) == [{'field': 'replacements.0', 'message':
        "Replacement 0: 'old' matched 0 times; it must match exactly once in the stored HTML returned by "
        "get_artifact (entities such as &quot; are escaped)"}]
    twice = edit(client, brief['id'], replacements=[{'old': 'two', 'new': 'TWO'}, {'old': 'one', 'new': 'x'}])
    assert twice.status_code == 422
    assert details(twice)[0]['field'] == 'replacements.1'
    assert 'matched 2 times' in details(twice)[0]['message']
    assert 'three' not in missing.text  # `old` is never echoed
    assert revisions(test_settings, brief['id']) == 1
    assert client.get(f"/artifacts/{brief['id']}", headers=HEADERS).json()['current_revision'] == 1


def test_overlapping_matches_count_separately(client):
    brief = create_brief(client, html='<p>aaa</p>')
    response = edit(client, brief['id'], replacements=[{'old': 'aa', 'new': 'b'}])
    assert response.status_code == 422
    assert 'matched 2 times' in details(response)[0]['message']


# M3
def test_match_is_against_stored_html(client):
    brief = create_brief(client, html='<p>He said "hi" today.</p>')
    assert '&quot;hi&quot;' in brief['document']['html']
    assert edit(client, brief['id'], replacements=[{'old': '"hi"', 'new': '"hello"'}]).status_code == 422
    response = edit(client, brief['id'], replacements=[{'old': '&quot;hi&quot;', 'new': '&quot;hello&quot;'}])
    assert response.status_code == 200, response.text
    assert response.json()['document']['html'] == '<p>He said &quot;hello&quot; today.</p>'


# M4
def test_stale_revision_conflicts(client, test_settings):
    brief = create_brief(client)
    assert edit(client, brief['id'], replacements=[{'old': 'roof', 'new': 'deck'}]).status_code == 200
    stale = edit(client, brief['id'], replacements=[{'old': 'roof', 'new': 'fence'}])
    assert stale.status_code == 409
    assert stale.json()['error'] == {'code': 'conflict', 'message': 'Artifact changed; reread before editing'}
    assert revisions(test_settings, brief['id']) == 2


def test_missing_artifact_is_404_and_a_future_revision_409(client):
    brief = create_brief(client)
    # A revision from the future is a stale request, as on PUT and edit_diagram (ticket T-03).
    assert edit(client, brief['id'], expected_revision=5, title='Later').status_code == 409
    assert edit(client, str(uuid4()), title='Nowhere').status_code == 404


# M5
def test_exact_retry_returns_applied_revision(client, test_settings):
    brief = create_brief(client)
    body = {'expected_revision': 1, 'replacements': [{'old': 'roof', 'new': 'deck'}], 'actor': 'Test', 'note': 'Retry me'}
    first = client.patch(f"/artifacts/{brief['id']}", headers=HEADERS, json=body)
    second = client.patch(f"/artifacts/{brief['id']}", headers=HEADERS, json=body)
    assert first.status_code == second.status_code == 200
    assert first.json()['revision'] == second.json()['revision'] == 2
    assert first.json()['document'] == second.json()['document']
    assert revisions(test_settings, brief['id']) == 2
    # A retry after someone else moved on still returns the applied revision.
    assert edit(client, brief['id'], expected_revision=2, title='Moved on').status_code == 200
    assert client.patch(f"/artifacts/{brief['id']}", headers=HEADERS, json=body).json()['revision'] == 2
    assert revisions(test_settings, brief['id']) == 3


# M6
def test_edit_result_is_privacy_screened(client, test_settings):
    brief = create_brief(client)
    digits = edit(client, brief['id'], replacements=[{'old': 'roof quote', 'new': 'call ' + DIGITS}])
    assert digits.status_code == 422
    assert [d['message'] for d in details(digits)] == [BRIEF_REFUSAL]
    assert DIGITS not in digits.text
    attribute = edit(client, brief['id'], replacements=[{'old': '<p>', 'new': '<p class="x">'}])
    assert attribute.status_code == 422
    assert [d['message'] for d in details(attribute)] == [BRIEF_REFUSAL]
    assert revisions(test_settings, brief['id']) == 1
    # Names from the private profile are redacted, as on create.
    named = edit(client, brief['id'], replacements=[{'old': 'roof quote', 'new': 'quote from Alex Example'}])
    assert named.status_code == 200, named.text
    assert 'Alex Example' not in named.json()['document']['html']


# M7
def test_diagram_and_meta_rules(client, test_settings):
    client.get('/artifacts', headers=HEADERS)
    diagram_id = str(uuid4())
    with sqlite3.connect(test_settings.db_path) as db:
        db.execute("INSERT INTO artifacts (id, request_id, request_hash, kind, created_at, updated_at, revision) "
                   "VALUES (?, ?, 'h', 'diagram', '2026-09-26T10:00:00+00:00', '2026-09-26T10:00:00+00:00', 1)",
                   (diagram_id, str(uuid4())))
        db.execute("INSERT INTO artifact_revisions (artifact_id, revision, payload, actor, note, created_at) "
                   "VALUES (?, 1, json_object('title', 'Flow'), 'Test', 'Direct', '2026-09-26T10:00:00+00:00')", (diagram_id,))
    diagram = edit(client, diagram_id, title='Renamed')
    assert diagram.status_code == 409
    assert diagram.json()['error'] == {'code': 'conflict',
                                       'message': 'edit_artifact edits pages and briefs; use edit_diagram for diagrams'}
    brief = create_brief(client)
    tags = edit(client, brief['id'], tags=['design'])
    assert tags.status_code == 422
    assert details(tags)[0]['field'] == 'document.tags'
    empty = edit(client, brief['id'])
    assert empty.status_code == 422
    assert edit(client, brief['id'], replacements=[]).status_code == 422
    retitled = edit(client, brief['id'], title='Evening brief')
    assert retitled.status_code == 200, retitled.text
    assert retitled.json()['document']['title'] == 'Evening brief'
    assert retitled.json()['document']['html'] == brief['document']['html']
    assert revisions(test_settings, brief['id']) == 2


def test_unregistered_page_kind_is_refused(client, test_settings):
    client.get('/artifacts', headers=HEADERS)
    page_id = str(uuid4())
    with sqlite3.connect(test_settings.db_path) as db:
        db.execute("INSERT INTO artifacts (id, request_id, request_hash, kind, created_at, updated_at, revision) "
                   "VALUES (?, ?, 'h', 'page', '2026-09-26T10:00:00+00:00', '2026-09-26T10:00:00+00:00', 1)",
                   (page_id, str(uuid4())))
        db.execute("INSERT INTO artifact_revisions (artifact_id, revision, payload, actor, note, created_at) "
                   "VALUES (?, 1, json_object('title', 'Page', 'html', '<p>x</p>'), 'Test', 'Direct', "
                   "'2026-09-26T10:00:00+00:00')", (page_id,))
    response = edit(client, page_id, replacements=[{'old': 'x', 'new': 'y'}])
    assert response.status_code == 422
    # Like PUT: the kind is the stored row's, so the error points at the document.
    assert details(response) == [{'field': 'document', 'message': "Artifact kind 'page' is not enabled yet"}]


# M8 — runs once A4 registers the page kind.
@pytest.mark.skipif('page' not in KINDS, reason='page kind is registered by A4')
def test_page_kind_edit(client, test_settings):
    sha = 'c' * 40
    body = dict(request_id=str(uuid4()), kind='page', actor='Test', note='Requested', document={
        'format': 'canvas.page/v1', 'title': 'Queue design', 'html': '<h1>Queue</h1><p>Draft design.</p>',
        'tags': ['design'], 'provenance': [{'repo': 'q-core', 'sha': sha, 'paths': ['api/artifacts.py']}]})
    page = client.post('/artifacts', headers=HEADERS, json=body)
    assert page.status_code == 200, page.text
    page_id = page.json()['id']
    response = edit(client, page_id, replacements=[{'old': 'Draft design.', 'new': 'Final design.'}],
                    tags=['design', 'queue'])
    assert response.status_code == 200, response.text
    document = response.json()['document']
    assert 'Final design.' in document['html']
    assert document['tags'] == ['design', 'queue']
    assert document['provenance'][0]['sha'] == sha
    assert revisions(test_settings, page_id) == 2

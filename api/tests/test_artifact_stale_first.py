"""Stale-first revision writes (ticket T-03). Invented data only.

PUT revise_artifact, PATCH edit_artifact and PATCH edit_diagram share
api.artifacts.write_revision: a stale request is 409 whatever its content or
the server's privacy configuration, and only an exact retry succeeds.
Request-shape validation still runs first, so a malformed body is 422.
"""
import copy
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from api.tests.test_canvas_diagram import EXAMPLES

HEADERS = {'Authorization': 'Bearer test-token'}
STALE = {'code': 'conflict', 'message': 'Artifact changed; reread before editing'}
# A seven-digit run, built rather than written.
RUN = ''.join(str(i % 10) for i in range(1, 8))


def brief_document(title='Morning brief', html='<p>Review the roof quote.</p>'):
    return {'title': title, 'html': html, 'period_start': '2026-09-21T00:00:00-04:00',
            'as_of': '2026-09-23T09:00:00-04:00',
            'coverage': [{'source': 'gmail', 'status': 'unavailable', 'detail': 'Not connected in this fixture.'}]}


def create(client, kind='daily', document=None):
    response = client.post('/artifacts', headers=HEADERS, json={
        'request_id': str(uuid4()), 'kind': kind, 'document': document or brief_document(),
        'actor': 'Test', 'note': 'Created'})
    assert response.status_code == 200, response.text
    return response.json()['id']


# Each route: a clean request (the setup edit), another that still applies to
# revision 2, one whose content a screen refuses, and one with an op or match
# error; all against a given expected_revision.
def put(client, aid, expected, variant='clean', note='Edit'):
    html = {'clean': '<p>Review the deck quote.</p>', 'current': '<p>Review the deck estimate.</p>', 'tripping': f'<p>Call {RUN}.</p>', 'refused': '<p class="x">Styled.</p>'}[variant]
    return client.put(f'/artifacts/{aid}', headers=HEADERS, json={
        'expected_revision': expected, 'document': brief_document(html=html), 'actor': 'Test', 'note': note})


def edit(client, aid, expected, variant='clean', note='Edit'):
    replacement = {'clean': {'old': 'roof', 'new': 'deck'}, 'current': {'old': 'quote', 'new': 'estimate'}, 'tripping': {'old': 'roof', 'new': f'call {RUN}'},
                   'refused': {'old': 'not in the text', 'new': 'x'}}[variant]
    return client.patch(f'/artifacts/{aid}', headers=HEADERS, json={
        'expected_revision': expected, 'replacements': [replacement], 'actor': 'Test', 'note': note})


def edit_diagram(client, aid, expected, variant='clean', note='Edit'):
    operation = {'clean': {'op': 'set_meta', 'set': {'title': 'Containers v3'}},
                 'current': {'op': 'set_meta', 'set': {'title': 'Containers v4'}},
                 'tripping': {'op': 'add_node', 'node': {'id': 'alex-example', 'label': 'Somebody'}},
                 'refused': {'op': 'remove_edge', 'id': 'e404'}}[variant]
    return client.patch(f'/artifacts/{aid}/diagram', headers=HEADERS, json={
        'expected_revision': expected, 'operations': [operation], 'actor': 'Test', 'note': note})


ROUTES = {
    'put': (put, 'daily', None),
    'edit_artifact': (edit, 'daily', None),
    'edit_diagram': (edit_diagram, 'diagram', lambda: copy.deepcopy(EXAMPLES['architecture'])),
}


@pytest.fixture(params=sorted(ROUTES))
def route(request, client):
    """(send, artifact id) with the artifact already at revision 2, so expected_revision=1 is stale."""
    send, kind, document = ROUTES[request.param]
    aid = create(client, kind, document() if document else None)
    first = send(client, aid, 1, note='The first edit')
    assert first.status_code == 200, first.text
    return send, aid


def revisions(settings, aid):
    with sqlite3.connect(settings.db_path) as db:
        return db.execute('SELECT COUNT(*) FROM artifact_revisions WHERE artifact_id = ?', (aid,)).fetchone()[0]


@pytest.mark.parametrize('variant', ['clean', 'tripping', 'refused'])
def test_stale_is_409_whatever_the_content(route, client, test_settings, variant):
    send, aid = route
    response = send(client, aid, 1, variant, note='A later edit')
    assert response.status_code == 409, response.text
    assert response.json()['error'] == STALE
    assert RUN not in response.text
    assert revisions(test_settings, aid) == 2


def test_stale_is_409_without_a_privacy_profile(route, client, test_settings):
    send, aid = route
    Path(test_settings.privacy_profile_path).unlink()
    response = send(client, aid, 1, note='A later edit')
    assert response.status_code == 409 and response.json()['error'] == STALE


def test_current_without_a_privacy_profile_is_still_refused(route, client, test_settings):
    send, aid = route
    profile = Path(test_settings.privacy_profile_path)
    saved = profile.read_bytes()
    profile.unlink()
    response = send(client, aid, 2, 'current')
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'personal_redaction_required'
    assert revisions(test_settings, aid) == 2
    # The same request with the profile back is accepted: only the profile refused it.
    profile.write_bytes(saved)
    profile.chmod(0o600)
    assert send(client, aid, 2, 'current').status_code == 200


@pytest.mark.parametrize('variant', ['tripping', 'refused'])
def test_current_content_errors_still_name_the_content(route, client, variant):
    send, aid = route
    response = send(client, aid, 2, variant)
    assert response.status_code in (400, 422), response.text
    assert response.json()['error'] != STALE


def test_exact_retry_returns_the_applied_revision(route, client, test_settings):
    send, aid = route
    retry = send(client, aid, 1, note='The first edit')
    assert retry.status_code == 200 and retry.json()['revision'] == 2
    assert revisions(test_settings, aid) == 2


def test_an_exact_retry_after_the_profile_is_lost_is_stale(route, client, test_settings):
    """A retry is recognised by comparing the screened result with the stored
    revision, and screening needs the profile. Without it the retry cannot be
    verified, so it is answered as stale: rereading shows the applied edit."""
    send, aid = route
    Path(test_settings.privacy_profile_path).unlink()
    response = send(client, aid, 1, note='The first edit')
    assert response.status_code == 409 and response.json()['error'] == STALE
    assert revisions(test_settings, aid) == 2


def test_a_future_revision_is_409(route, client):
    send, aid = route
    response = send(client, aid, 9)
    assert response.status_code == 409 and response.json()['error'] == STALE


def test_request_shape_still_wins(route, client):
    """The body model rejects a malformed request before any revision check."""
    send, aid = route
    response = send(client, aid, 1, note='Call ' + RUN)
    assert response.status_code == 422
    assert [d['field'] for d in response.json()['error']['details']] == ['note']
    assert RUN not in response.text


def test_kind_refusals_do_not_depend_on_the_revision(client):
    diagram = create(client, 'diagram', copy.deepcopy(EXAMPLES['architecture']))
    brief = create(client)
    for expected in (1, 9):
        wrong_tool = edit(client, diagram, expected)
        assert wrong_tool.status_code == 409
        assert wrong_tool.json()['error']['message'] == 'edit_artifact edits pages and briefs; use edit_diagram for diagrams'
        not_a_diagram = edit_diagram(client, brief, expected)
        assert not_a_diagram.status_code == 409
        assert not_a_diagram.json()['error']['message'].startswith('edit_diagram only edits diagram artifacts')


@pytest.mark.parametrize('name', ['edit_artifact', 'edit_diagram'])
def test_a_real_failure_is_not_disguised_as_stale(client, test_settings, name):
    """Only content, op and screen refusals become 409 on a stale request. A
    404 from compute (the base revision has gone) propagates as itself."""
    send, kind, document = ROUTES[name]
    aid = create(client, kind, document() if document else None)
    assert send(client, aid, 1, note='The first edit').status_code == 200
    with sqlite3.connect(test_settings.db_path) as db:
        db.execute('DELETE FROM artifact_revisions WHERE artifact_id = ? AND revision = 1', (aid,))
    response = send(client, aid, 1, note='A later edit')
    assert response.status_code == 404, response.text
    assert response.json()['error'] == {'code': 'not_found', 'message': 'Artifact revision not found'}

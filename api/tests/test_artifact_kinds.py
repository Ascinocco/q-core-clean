"""Artifact kinds (Canvas). Invented data only.

Briefs keep working unchanged; page and diagram are schema-valid but refused
until A4/A7 register them. Fake kinds registered with monkeypatch prove the
dispatch uses each kind's own rules.
"""
import sqlite3
from uuid import uuid4

import pytest
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from api.artifact_kinds import KINDS, KindRules, is_provenance_sha, screen_brief_html
from api.artifact_models import ArtifactMeta, Model, PageDocument, Provenance
from api.briefing_content import screen_payload
from api.db import apply_startup_migrations, init_db
from api.tests.test_migrations import PRE_0015_BRIEF_ID, restore_pre_0015_artifacts

HEADERS = {'Authorization': 'Bearer test-token'}

BRIEF_REFUSAL = ('Content refused: use privacy-safe summaries and balanced static HTML '
                 'without attributes, links or active content')

# Never a literal: a seven-digit run in source is what the screen refuses.
DIGITS = ''.join(str(n) for n in range(1, 8))
SHA = ('ab' + DIGITS + 'cdef' * 10)[:40]


def brief_document(title='Morning brief', html='<p>Review the roof quote.</p>'):
    return {'title': title, 'html': html, 'period_start': '2026-09-21T00:00:00-04:00',
            'as_of': '2026-09-23T09:00:00-04:00', 'coverage': [
                {'source': 'gmail', 'status': 'unavailable', 'detail': 'Not connected in this fixture.'}]}


def page_document(**changes):
    document = {'format': 'canvas.page/v1', 'title': 'Queue design', 'html': '<p>hello page</p>',
                'tags': ['design', 'q-core'],
                'provenance': [{'repo': 'q-core', 'sha': 'a' * 40, 'paths': ['api/artifacts.py']}]}
    document.update(changes)
    return document


def post(client, kind, document, **changes):
    body = dict(request_id=str(uuid4()), kind=kind, document=document, actor='Test', note='Requested')
    body.update(changes)
    return client.post('/artifacts', headers=HEADERS, json=body)


def messages(response):
    return [d['message'] for d in response.json()['error']['details']]


def insert_page_row(db_path, title='Invented page title'):
    identifier = str(uuid4())
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO artifacts (id, request_id, request_hash, kind, created_at, updated_at, revision) "
                   "VALUES (?, ?, 'h', 'page', '2026-09-26T10:00:00+00:00', '2026-09-26T10:00:00+00:00', 1)",
                   (identifier, str(uuid4())))
        db.execute("INSERT INTO artifact_revisions (artifact_id, revision, payload, actor, note, created_at) "
                   "VALUES (?, 1, json_object('title', ?, 'format', 'canvas.page/v1', 'html', '<p>x</p>'), "
                   "'Test', 'Direct', '2026-09-26T10:00:00+00:00')", (identifier, title))
    return identifier


@pytest.fixture()
def page_kind(monkeypatch):
    """A page-like kind registered for the test, screened with brief html rules."""
    rules = KindRules(document_model=PageDocument, refusal='Page refused',
                      viewer_path=lambda i: f'/ui/canvas/{i}', screen_html=screen_brief_html)
    monkeypatch.setitem(KINDS, 'page', rules)
    return rules


# M3
def test_pre_migration_brief_opens(client, test_settings):
    init_db(test_settings)
    restore_pre_0015_artifacts(test_settings.db_path)
    with client:  # the app's own startup applies 0015
        response = client.get(f'/ui/briefs/{PRE_0015_BRIEF_ID}', headers=HEADERS)
        assert response.status_code == 200
        assert 'Second draft of the week' in response.text
        first = client.get(f'/ui/briefs/{PRE_0015_BRIEF_ID}?revision=1', headers=HEADERS)
        assert first.status_code == 200
        assert 'First draft of the week' in first.text
        value = client.get(f'/artifacts/{PRE_0015_BRIEF_ID}', headers=HEADERS).json()
        assert value['viewer_path'] == f'/ui/briefs/{PRE_0015_BRIEF_ID}'
        # And the migrated tables still take a revision with foreign keys on.
        edit = dict(expected_revision=2, document=brief_document('Third'), actor='Test', note='After upgrade')
        assert client.put(f'/artifacts/{PRE_0015_BRIEF_ID}', headers=HEADERS, json=edit).json()['revision'] == 3


# M4
def test_unregistered_kind_refused(client):
    response = post(client, 'page', page_document())
    assert response.status_code == 422
    assert response.json()['error']['details'] == [
        {'field': 'kind', 'message': "Artifact kind 'page' is not enabled yet"}]
    assert client.get('/artifacts', headers=HEADERS).json()['total'] == 0


def test_revision_of_an_unregistered_kind_is_refused(client, test_settings):
    client.get('/artifacts', headers=HEADERS)
    page_id = insert_page_row(test_settings.db_path)
    edit = dict(expected_revision=1, document=page_document(), actor='Test', note='Edit')
    response = client.put(f'/artifacts/{page_id}', headers=HEADERS, json=edit)
    assert response.status_code == 422
    # The kind is the stored row's, not a field of the PUT body, so the
    # error points at the document rather than at a `kind` it never sent.
    assert response.json()['error']['details'] == [
        {'field': 'document', 'message': "Artifact kind 'page' is not enabled yet"}]


def test_revision_is_validated_against_the_stored_kind(client, page_kind):
    brief = post(client, 'daily', brief_document()).json()
    edit = dict(expected_revision=1, document=page_document(), actor='Test', note='Wrong shape')
    response = client.put(f"/artifacts/{brief['id']}", headers=HEADERS, json=edit)
    assert response.status_code == 422
    assert any(d['field'].startswith('document.') for d in response.json()['error']['details'])
    missing = client.put(f'/artifacts/{uuid4()}', headers=HEADERS, json=edit)
    assert missing.status_code == 404


# M5
def test_briefs_list_excludes_other_kinds(client, test_settings):
    brief = post(client, 'daily', brief_document()).json()
    page_id = insert_page_row(test_settings.db_path)
    listing = client.get('/ui/briefs', headers=HEADERS).text
    assert 'Invented page title' not in listing
    assert f"/ui/briefs/{brief['id']}" in listing
    assert client.get(f'/ui/briefs/{page_id}', headers=HEADERS).status_code == 404
    pages = client.get('/artifacts?kind=page', headers=HEADERS).json()
    assert [(r['id'], r['title']) for r in pages['items']] == [(page_id, 'Invented page title')]
    everything = client.get('/artifacts', headers=HEADERS).json()
    assert {r['id'] for r in everything['items']} == {brief['id'], page_id}
    assert all(r['title'] for r in everything['items'])
    assert client.get('/artifacts?kind=bogus', headers=HEADERS).status_code == 422
    assert client.get(f'/artifacts/{page_id}', headers=HEADERS).json()['viewer_path'] == f'/ui/canvas/{page_id}'


# M6
def test_models_validate_meta():
    page = PageDocument.model_validate(page_document())
    assert page.provenance[0].sha == 'a' * 40
    assert ArtifactMeta.model_validate({'title': 'Plain'}).tags == []
    assert Provenance.model_validate({'repo': 'q-core', 'sha': 'b' * 40}).paths == []


@pytest.mark.parametrize('change', [
    {'tags': ['Bad_Tag']},
    {'tags': [f't{n}' for n in range(21)]},
    {'tags': ['design', 'design']},
    {'provenance': [{'repo': 'q-core', 'sha': 'a' * 39}]},
    {'provenance': [{'repo': 'q-core', 'sha': 'A' * 40}]},
    {'provenance': [{'repo': 'q-core', 'sha': 'a' * 40, 'paths': ['../secrets']}]},
    {'provenance': [{'repo': 'q-core', 'sha': 'a' * 40, 'paths': ['/etc/hosts']}]},
    {'html': '<p>' + 'x' * 500000 + '</p>'},
], ids=['bad-tag', 'too-many-tags', 'duplicate-tag', 'sha-39', 'sha-upper', 'dotdot-path', 'absolute-path', 'html-over-cap'])
def test_models_refuse(change):
    with pytest.raises(ValidationError):
        PageDocument.model_validate(page_document(**change))


# M7
def test_screen_payload_dispatch(client, monkeypatch):
    brief = post(client, 'daily', brief_document(html='<p class="x">styled</p>'))
    assert brief.status_code == 422
    assert messages(brief) == [BRIEF_REFUSAL]

    monkeypatch.setitem(KINDS, 'page', KindRules(
        document_model=PageDocument, refusal='Page refused', viewer_path=lambda i: f'/ui/canvas/{i}',
        screen_html=lambda html, profile: html.upper()))
    page = post(client, 'page', page_document())
    assert page.status_code == 200, page.text
    assert page.json()['document']['html'] == '<P>HELLO PAGE</P>'
    assert page.json()['viewer_path'] == f"/ui/canvas/{page.json()['id']}"

    class FakeDiagram(Model):
        title: str
        label: str

    received = []

    def screen_document(document, settings):
        received.append(document)
        return document

    monkeypatch.setitem(KINDS, 'diagram', KindRules(
        document_model=FakeDiagram, refusal='Diagram refused', viewer_path=lambda i: f'/ui/canvas/{i}',
        screen_document=screen_document))
    document = {'title': 'Flow', 'label': 'Alex Example'}
    saved = post(client, 'diagram', document, actor='Alex Example')
    assert saved.status_code == 200, saved.text
    assert received == [document]
    # The kind's screen owns the document: screen_payload did not redact it...
    assert saved.json()['document']['label'] == 'Alex Example'
    # ...but still screened the envelope.
    assert saved.json()['actor'] == '[REDACTED]'
    # (The envelope model refuses the digit run in note even before the screen.)
    assert post(client, 'diagram', document, note='Call ' + DIGITS).status_code == 422


def test_screen_document_refusal_uses_the_kind_message(client, monkeypatch):
    class FakeDiagram(Model):
        title: str

    def refuse(document, settings):
        raise ValueError('node label too long')

    monkeypatch.setitem(KINDS, 'diagram', KindRules(
        document_model=FakeDiagram, refusal='Diagram refused', viewer_path=lambda i: f'/ui/canvas/{i}',
        screen_document=refuse))
    assert messages(post(client, 'diagram', {'title': 'Flow'})) == ['Diagram refused']
    monkeypatch.setitem(KINDS, 'diagram', KindRules(
        document_model=FakeDiagram, refusal='Diagram refused', viewer_path=lambda i: f'/ui/canvas/{i}',
        screen_document=refuse, detailed_errors=True))
    assert messages(post(client, 'diagram', {'title': 'Flow'})) == ['Diagram refused: node label too long']
    assert client.get('/artifacts', headers=HEADERS).json()['total'] == 0


def test_screen_document_validation_errors_propagate(client, monkeypatch):
    """A7's screen_diagram raises its own RequestValidationError with a precise
    loc. The dispatch must pass it through, not wrap it in the kind's refusal."""
    class FakeDiagram(Model):
        title: str

    def precise(document, settings):
        raise RequestValidationError([{'loc': ('body', 'document', 'nodes', 0, 'id'),
                                       'msg': 'Duplicate node id', 'type': 'value_error'}])

    monkeypatch.setitem(KINDS, 'diagram', KindRules(
        document_model=FakeDiagram, refusal='Diagram refused', viewer_path=lambda i: f'/ui/canvas/{i}',
        screen_document=precise))
    response = post(client, 'diagram', {'title': 'Flow'})
    assert response.status_code == 422
    assert response.json()['error']['details'] == [{'field': 'document.nodes.0.id', 'message': 'Duplicate node id'}]


def test_title_length_is_checked_and_published():
    """The length limit binds to the string, so it reads plainly and stays in the schema."""
    assert ArtifactMeta.model_json_schema()['properties']['title']['maxLength'] == 180
    with pytest.raises(ValidationError, match='at most 180 characters'):
        ArtifactMeta.model_validate({'title': 'x' * 181})


def test_detailed_errors_carry_the_html_reason(client, monkeypatch):
    monkeypatch.setitem(KINDS, 'page', KindRules(
        document_model=PageDocument, refusal='Page refused', viewer_path=lambda i: f'/ui/canvas/{i}',
        screen_html=screen_brief_html, detailed_errors=True))
    response = post(client, 'page', page_document(html='<p>open'))
    assert messages(response) == ['Page refused: Document must contain text and balanced tags']


# M11
def test_provenance_sha_exemption(client, page_kind, test_settings):
    assert any(DIGITS in SHA[i:i + 7] for i in range(34))  # the fixture really holds the run
    provenance = [{'repo': 'q-core', 'sha': SHA, 'paths': ['api/artifact_kinds.py']}]
    saved = post(client, 'page', page_document(provenance=provenance))
    assert saved.status_code == 200, saved.text
    assert saved.json()['document']['provenance'][0]['sha'] == SHA
    stored = client.get(f"/artifacts/{saved.json()['id']}", headers=HEADERS).json()
    assert stored['document']['provenance'][0]['sha'] == SHA

    assert post(client, 'page', page_document(title='Build ' + DIGITS)).status_code == 422
    for bad in (SHA[:39], SHA.upper()):
        response = post(client, 'page', page_document(provenance=[{'repo': 'q-core', 'sha': bad}]))
        assert response.status_code == 422
        assert [d['field'] for d in response.json()['error']['details']] == ['document.provenance.0.sha']

    # A sha key anywhere but document.provenance[i].sha is screened as usual.
    class ShaElsewhere(PageDocument):
        notes: list[dict] = []

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setitem(KINDS, 'page', KindRules(
            document_model=ShaElsewhere, refusal='Page refused', viewer_path=lambda i: f'/ui/canvas/{i}',
            screen_html=screen_brief_html))
        elsewhere = post(client, 'page', page_document(notes=[{'sha': SHA}]))
        assert elsewhere.status_code == 422
        assert messages(elsewhere) == ['Page refused']
    finally:
        monkeypatch.undo()

    with pytest.raises(RequestValidationError):
        screen_payload({'document': {'sources': [{'sha': SHA}]}}, test_settings)
    with pytest.raises(RequestValidationError):
        screen_payload({'sha': SHA}, test_settings)
    assert screen_payload({'document': {'provenance': [{'sha': SHA}]}}, test_settings) == {
        'document': {'provenance': [{'sha': SHA}]}}


@pytest.mark.parametrize('path,value,expected', [
    ('document.provenance[0].sha', SHA, True),
    ('document.provenance[12].sha', SHA, True),
    ('document.provenance[0].sha', SHA.upper(), False),
    ('document.provenance[0].sha', SHA[:39], False),
    ('document.provenance[0].sha', SHA + '\n', False),
    ('document.provenance.sha', SHA, False),
    ('document.sources[0].sha', SHA, False),
    ('provenance[0].sha', SHA, False),
    ('document.provenance[0].sha.x', SHA, False),
    ('xdocument.provenance[0].sha', SHA, False),
])
def test_is_provenance_sha_is_narrow(path, value, expected):
    assert is_provenance_sha(path, value) is expected

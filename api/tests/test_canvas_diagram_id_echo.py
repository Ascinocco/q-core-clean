"""A name-shaped id is never echoed in an error (ticket T-04). Invented data only.

Ids are name-screened by screen_diagram, which runs after validation, so a
validation or operation error that quoted an id could echo a personal name
before the screen refused it. Every message names a JSON path or an
operation's field instead. 'alex-example' is the test profile's name as an id.
"""
import copy
from uuid import uuid4

import pytest

from api.tests.test_canvas_diagram import EXAMPLES

HEADERS = {'Authorization': 'Bearer test-token'}
NAME = 'alex-example'


def create(client, document):
    return client.post('/artifacts', headers=HEADERS, json={
        'request_id': str(uuid4()), 'kind': 'diagram', 'document': document, 'actor': 'Test', 'note': 'Created'})


def dangling_edge():
    document = copy.deepcopy(EXAMPLES['architecture'])
    document['edges'].append({'id': 'e9', 'source': 'api', 'target': NAME})
    return document


def duplicate_ids():
    document = copy.deepcopy(EXAMPLES['architecture'])
    document['nodes'] += [{'id': NAME, 'label': 'One'}, {'id': NAME, 'label': 'Two'}]
    return document


def sequence_to_nobody():
    document = copy.deepcopy(EXAMPLES['sequence'])
    document['items'][0]['target'] = NAME
    return document


def assert_not_echoed(response, status=422):
    assert response.status_code == status, response.text
    assert 'alex' not in response.text.lower()


@pytest.mark.parametrize('document', [dangling_edge, duplicate_ids, sequence_to_nobody],
                         ids=['dangling-reference', 'duplicate-id', 'unknown-participant'])
def test_create_and_revise_do_not_echo_the_id(client, document):
    assert_not_echoed(create(client, document()))
    saved = create(client, copy.deepcopy(EXAMPLES['architecture' if document is not sequence_to_nobody else 'sequence']))
    assert saved.status_code == 200, saved.text
    revise = client.put(f"/artifacts/{saved.json()['id']}", headers=HEADERS, json={
        'expected_revision': 1, 'document': document(), 'actor': 'Test', 'note': 'Revised'})
    assert_not_echoed(revise)


def test_edit_diagram_does_not_echo_the_id(client):
    arch = create(client, copy.deepcopy(EXAMPLES['architecture'])).json()['id']
    seq = create(client, copy.deepcopy(EXAMPLES['sequence'])).json()['id']

    def patch(aid, operation):
        return client.patch(f'/artifacts/{aid}/diagram', headers=HEADERS, json={
            'expected_revision': 1, 'operations': [operation], 'actor': 'Test', 'note': 'Edit'})

    # The result fails whole-document validation (422 at document).
    assert_not_echoed(patch(arch, {'op': 'add_edge', 'edge': {'id': 'e9', 'source': 'api', 'target': NAME}}))
    # An operation names an id that does not exist (400 at the op's field).
    for operation in ({'op': 'remove_edge', 'id': NAME},
                      {'op': 'update_node', 'id': NAME, 'set': {'label': 'X'}},
                      {'op': 'move_to_group', 'ids': [NAME], 'group': 'home-server'},
                      {'op': 'move_to_group', 'ids': ['api'], 'group': NAME},
                      {'op': 'pin', 'id': NAME, 'x': 0, 'y': 0}):
        response = patch(arch, operation)
        assert_not_echoed(response, 400)
        assert response.json()['error']['message'].startswith(f"operations[0] ({operation['op']}): unknown ")
    for operation in ({'op': 'insert_item', 'item': {'kind': 'message', 'id': 'm9', 'source': 'api', 'target': 'db',
                                                     'label': 'x'}, 'where': {'before': NAME}},
                      {'op': 'insert_item', 'item': {'kind': 'message', 'id': 'm9', 'source': 'api', 'target': 'db',
                                                     'label': 'x'}, 'where': {'fragment': NAME}},
                      {'op': 'move_participant', 'id': NAME, 'where': {'before': NAME}},
                      {'op': 'add_section', 'fragment': NAME},
                      {'op': 'remove_item', 'id': NAME}):
        assert_not_echoed(patch(seq, operation), 400)
    # A name-shaped id that does get as far as the screen is refused with the path only.
    added = patch(arch, {'op': 'add_node', 'node': {'id': NAME, 'label': 'Somebody'}})
    assert_not_echoed(added)
    assert added.json()['error']['details'][0]['field'].startswith('document.nodes.')


def test_ids_added_earlier_in_the_batch_are_not_echoed(client):
    """Nothing in a batch is name-screened until the end, so an id an earlier
    op added is as unscreened as one that matches nothing."""
    arch = create(client, copy.deepcopy(EXAMPLES['architecture'])).json()['id']
    seq = create(client, copy.deepcopy(EXAMPLES['sequence'])).json()['id']

    def batch(aid, *operations):
        return client.patch(f'/artifacts/{aid}/diagram', headers=HEADERS, json={
            'expected_revision': 1, 'operations': list(operations), 'actor': 'Test', 'note': 'Edit'})

    node = {'op': 'add_node', 'node': {'id': NAME, 'label': 'Somebody'}}
    twice = batch(arch, node, node)
    assert_not_echoed(twice, 409)
    assert twice.json()['error']['message'] == 'operations[1] (add_node): node.id is already used'

    refused = batch(arch, node, {'op': 'add_edge', 'edge': {'id': 'e9', 'source': 'api', 'target': NAME}},
                    {'op': 'remove_node', 'id': NAME, 'dependents': 'refuse'})
    assert_not_echoed(refused, 409)
    assert 'depend on the node at id: edges[4]' in refused.json()['error']['message']

    group = {'op': 'add_group', 'group': {'id': NAME, 'label': 'Crew'}}
    assert_not_echoed(batch(arch, group, {'op': 'move_to_group', 'ids': ['api'], 'group': NAME},
                            {'op': 'remove_group', 'id': NAME, 'members': 'refuse'}), 409)
    assert_not_echoed(batch(arch, {'op': 'add_note', 'note': {'id': NAME, 'text': 'Hi.', 'attach': 'db'}},
                            {'op': 'pin', 'id': NAME, 'x': 0, 'y': 0}), 409)

    participant = {'op': 'add_participant', 'participant': {'id': NAME, 'label': 'Somebody'}}
    assert_not_echoed(batch(seq, participant, participant), 409)
    message = {'kind': 'message', 'id': 'm9', 'source': NAME, 'target': 'api', 'label': 'hello'}
    assert_not_echoed(batch(seq, participant, {'op': 'insert_item', 'item': message},
                            {'op': 'remove_participant', 'id': NAME, 'dependents': 'refuse'}), 409)
    assert_not_echoed(batch(seq, participant, {'op': 'move_participant', 'id': NAME, 'where': {'before': NAME}}), 409)
    fragment = {'kind': 'fragment', 'id': NAME, 'operator': 'opt', 'sections': [{'items': [message | {'source': 'api'}]}]}
    for operation in ({'op': 'remove_item', 'id': NAME},
                      {'op': 'move_item', 'id': NAME, 'where': {'fragment': NAME}},
                      {'op': 'insert_item', 'item': message | {'id': 'm10', 'source': 'api'}, 'where': {'fragment': NAME, 'section': 3}},
                      {'op': 'update_section', 'fragment': NAME, 'index': 5},
                      {'op': 'remove_section', 'fragment': NAME, 'index': 0, 'contents': 'cascade'}):
        response = batch(seq, {'op': 'insert_item', 'item': fragment}, operation)
        assert_not_echoed(response, response.status_code)
        assert response.status_code in (400, 409), response.text
    fragment_twice = batch(seq, {'op': 'insert_item', 'item': fragment}, {'op': 'insert_item', 'item': fragment})
    assert_not_echoed(fragment_twice, 409)

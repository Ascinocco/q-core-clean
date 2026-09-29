"""apply_ops, one operation at a time (Canvas). Invented data only.

Each test states the exact resulting document: the normalised A7 example,
changed by hand the way the operation should change it.
"""
import copy

import pytest
from pydantic import TypeAdapter, ValidationError

from api.canvas_diagram import DiagramDocument
from api.canvas_diagram_ops import (
    COMMON_OPS, GRAPH_OPS, SEQUENCE_OPS, Operation, OpError, apply_ops, diagram_summary,
)
from api.tests.test_canvas_diagram import EXAMPLES

OPS = TypeAdapter(list[Operation])


def norm(document):
    return DiagramDocument.model_validate(document).model_dump(mode='json', exclude_none=True)


def base(name):
    return norm(copy.deepcopy(EXAMPLES[name]))


def run(document, *operations):
    return apply_ops(document, OPS.validate_python(list(operations)))


def fails(document, *operations):
    with pytest.raises(OpError) as caught:
        run(document, *operations)
    return caught.value


def node(doc, id_):
    return next(n for n in doc['nodes'] if n['id'] == id_)


def nested_sequence():
    """The sequence example with an opt fragment f2 inside f1's first section: depth 2."""
    doc = base('sequence')
    f1 = doc['items'][4]
    f1['sections'][0]['items'].append({'kind': 'fragment', 'id': 'f2', 'operator': 'opt', 'sections': [
        {'guard': 'cached', 'items': [{'kind': 'message', 'id': 'm8', 'source': 'api', 'target': 'db', 'label': 'read cache'}]}]})
    return norm(doc)


def msg(id_, source='api', target='db', label='probe'):
    return {'kind': 'message', 'id': id_, 'source': source, 'target': target, 'label': label}


# --- M1: graph operations -------------------------------------------------

def test_add_node():
    doc = base('architecture')
    expected = copy.deepcopy(doc)
    expected['nodes'].append({'id': 'cache', 'label': 'Redis', 'role': 'cache', 'group': 'home-server', 'ports': []})
    assert run(doc, {'op': 'add_node', 'node': {'id': 'cache', 'label': 'Redis', 'role': 'cache',
                                                 'group': 'home-server'}}) == norm(expected)


def test_update_node_sets_and_clears():
    doc = base('architecture')
    expected = copy.deepcopy(doc)
    claude = node(expected, 'claude')
    claude.update(label='Claude Desktop', tone='info')
    del claude['description']
    assert run(doc, {'op': 'update_node', 'id': 'claude', 'set': {'label': 'Claude Desktop', 'tone': 'info'},
                     'clear': ['description']}) == norm(expected)


def test_remove_node_cascade_removes_edges_and_attached_notes():
    doc = base('architecture')
    expected = copy.deepcopy(doc)
    expected['nodes'] = [n for n in expected['nodes'] if n['id'] != 'db']
    expected['edges'] = [e for e in expected['edges'] if e['id'] != 'e4']
    expected['notes'] = []
    assert run(doc, {'op': 'remove_node', 'id': 'db', 'dependents': 'cascade'}) == norm(expected)


def test_add_update_remove_edge():
    doc = base('architecture')
    expected = copy.deepcopy(doc)
    expected['edges'].append({'id': 'e5', 'source': 'api', 'target': 'owner', 'label': 'alerts', 'line': 'dashed',
                              'arrow': 'forward'})
    assert run(doc, {'op': 'add_edge', 'edge': {'id': 'e5', 'source': 'api', 'target': 'owner', 'label': 'alerts',
                                                 'line': 'dashed'}}) == norm(expected)

    expected = copy.deepcopy(doc)
    expected['edges'][0].update(line='dotted', arrow='both', tone='muted')
    del expected['edges'][0]['label']
    assert run(doc, {'op': 'update_edge', 'id': 'e1', 'set': {'line': 'dotted', 'arrow': 'both', 'tone': 'muted'},
                     'clear': ['label']}) == norm(expected)

    expected = copy.deepcopy(doc)
    del expected['edges'][0]
    assert run(doc, {'op': 'remove_edge', 'id': 'e1'}) == norm(expected)


def test_add_and_update_group():
    doc = base('architecture')
    expected = copy.deepcopy(doc)
    expected['groups'].append({'id': 'workers', 'label': 'Workers', 'parent': 'home-server', 'style': 'cluster'})
    assert run(doc, {'op': 'add_group', 'group': {'id': 'workers', 'label': 'Workers', 'parent': 'home-server'}}) == norm(expected)

    expected = copy.deepcopy(doc)
    expected['groups'][0].update(label='Home server', style='cluster')
    assert run(doc, {'op': 'update_group', 'id': 'home-server', 'set': {'label': 'Home server', 'tone': 'info',
                                                                          'style': 'cluster'},
                     'clear': ['tone']}) == norm(expected)


def test_move_to_group_clears_pins():
    doc = base('architecture')
    expected = copy.deepcopy(doc)
    node(expected, 'owner')['group'] = 'home-server'
    del node(expected, 'mcp')['group']
    result = run(doc, {'op': 'pin', 'id': 'owner', 'x': 10, 'y': 20},
                 {'op': 'pin', 'id': 'mcp', 'x': 5, 'y': 5},
                 {'op': 'move_to_group', 'ids': ['owner'], 'group': 'home-server'},
                 {'op': 'move_to_group', 'ids': ['mcp'], 'group': None})
    assert result == norm(expected)
    assert 'position' not in node(result, 'owner') and 'position' not in node(result, 'mcp')


def with_outer_group():
    """home-server nested in an outer group, with its nodes pinned."""
    doc = base('architecture')
    doc['groups'].insert(0, {'id': 'outer', 'label': 'Rack', 'style': 'cluster'})
    doc['groups'][1]['parent'] = 'outer'
    for id_ in ('mcp', 'api'):
        node(doc, id_)['position'] = {'x': 1.0, 'y': 2.0}
    return norm(doc)


def test_remove_group_ungroup_reparents_to_the_grandparent():
    doc = with_outer_group()
    expected = copy.deepcopy(doc)
    expected['groups'] = [g for g in expected['groups'] if g['id'] != 'home-server']
    for id_ in ('mcp', 'api', 'db'):
        node(expected, id_)['group'] = 'outer'
        node(expected, id_).pop('position', None)
    assert run(doc, {'op': 'remove_group', 'id': 'home-server', 'members': 'ungroup'}) == norm(expected)


def test_remove_group_ungroup_at_top_level_and_group_edges_go():
    doc = base('architecture')
    doc['edges'].append({'id': 'e9', 'source': 'owner', 'target': 'home-server'})
    doc['notes'].append({'id': 'n9', 'text': 'Boundary note.', 'attach': 'home-server'})
    doc = norm(doc)
    expected = copy.deepcopy(doc)
    expected['groups'] = []
    for id_ in ('mcp', 'api', 'db'):
        del node(expected, id_)['group']
    expected['edges'] = [e for e in expected['edges'] if e['id'] != 'e9']
    expected['notes'] = [n for n in expected['notes'] if n['id'] != 'n9']
    assert run(doc, {'op': 'remove_group', 'id': 'home-server', 'members': 'ungroup'}) == norm(expected)


def test_remove_group_cascade_removes_the_subtree_and_its_edges_and_notes():
    doc = base('architecture')
    doc['groups'].append({'id': 'inner', 'label': 'Workers', 'parent': 'home-server'})
    doc['nodes'].append({'id': 'worker', 'label': 'Worker', 'group': 'inner'})
    doc['edges'].append({'id': 'e9', 'source': 'worker', 'target': 'owner'})
    doc['notes'].append({'id': 'n9', 'text': 'Inner note.', 'attach': 'inner'})
    doc = norm(doc)
    expected = copy.deepcopy(doc)
    expected['groups'] = []
    expected['nodes'] = [n for n in expected['nodes'] if n['id'] in ('owner', 'claude')]
    expected['edges'] = [e for e in expected['edges'] if e['id'] == 'e1']
    expected['notes'] = []
    assert run(doc, {'op': 'remove_group', 'id': 'home-server', 'members': 'cascade'}) == norm(expected)


def test_add_update_remove_graph_note():
    doc = base('architecture')
    expected = copy.deepcopy(doc)
    expected['notes'].append({'id': 'n2', 'text': 'Backups nightly.', 'attach': 'db'})
    assert run(doc, {'op': 'add_note', 'note': {'id': 'n2', 'text': 'Backups nightly.', 'attach': 'db'}}) == norm(expected)

    expected = copy.deepcopy(doc)
    expected['notes'][0] = {'id': 'n1', 'text': 'Only the API writes.'}
    assert run(doc, {'op': 'update_note', 'id': 'n1', 'set': {'text': 'Only the API writes.'},
                     'clear': ['attach']}) == norm(expected)

    expected = copy.deepcopy(doc)
    expected['notes'] = []
    assert run(doc, {'op': 'remove_note', 'id': 'n1'}) == norm(expected)


def test_set_layout_replaces_it():
    doc = base('flow')
    expected = copy.deepcopy(doc)
    expected['layout'] = {'spacing': 'roomy'}
    assert run(doc, {'op': 'set_layout', 'layout': {'spacing': 'roomy'}}) == norm(expected)


def test_pin_and_unpin():
    doc = base('flow')
    expected = copy.deepcopy(doc)
    node(expected, 'scrub')['position'] = {'x': 10.0, 'y': -20.5}
    del node(expected, 'done')['position']
    assert run(doc, {'op': 'pin', 'id': 'scrub', 'x': 10, 'y': -20.5},
               {'op': 'unpin', 'id': 'done'}) == norm(expected)
    assert fails(base('architecture'), {'op': 'pin', 'id': 'n1', 'x': 0, 'y': 0}).status == 409


def test_set_meta():
    doc = base('architecture')
    doc['description'] = 'Old description.'
    doc = norm(doc)
    expected = copy.deepcopy(doc)
    sha = 'e' * 40
    expected.update(title='Containers v2', tags=['arch', 'q-core'],
                    provenance=[{'repo': 'q-core', 'sha': sha, 'paths': ['api/main.py']}])
    del expected['description']
    assert run(doc, {'op': 'set_meta', 'set': {'title': 'Containers v2', 'tags': ['arch', 'q-core'],
                                               'provenance': [{'repo': 'q-core', 'sha': sha, 'paths': ['api/main.py']}]},
                     'clear': ['description']}) == norm(expected)


# --- M1: sequence operations ----------------------------------------------

def test_add_participant_before_after_and_append():
    doc = base('sequence')
    cache = {'id': 'cache', 'label': 'Cache', 'role': 'database'}
    for where, position in (({'before': 'api'}, 2), ({'after': 'api'}, 3), ({}, 4)):
        expected = copy.deepcopy(doc)
        expected['participants'].insert(position, cache)
        assert run(doc, {'op': 'add_participant', 'participant': cache, 'where': where}) == norm(expected)


def test_update_and_move_participant():
    doc = base('sequence')
    expected = copy.deepcopy(doc)
    expected['participants'][1].update(label='MCP server', role='client')
    assert run(doc, {'op': 'update_participant', 'id': 'mcp', 'set': {'label': 'MCP server', 'role': 'client',
                                                                        'tone': 'info'}, 'clear': ['tone']}) == norm(expected)

    expected = copy.deepcopy(doc)
    db = expected['participants'].pop(3)
    expected['participants'].insert(0, db)
    assert run(doc, {'op': 'move_participant', 'id': 'db', 'where': {'before': 'claude'}}) == norm(expected)


def test_remove_participant_cascade():
    doc = base('sequence')
    expected = copy.deepcopy(doc)
    # api: m2, m3, m4, m5 and m6 involve it; the two-participant note n1 keeps mcp.
    expected['items'] = [expected['items'][0], {**expected['items'][2], 'over': ['mcp']},
                         {**expected['items'][4], 'sections': [
                             {'guard': 'revision is current', 'items': []}, {'guard': 'stale revision', 'items': []}]},
                         expected['items'][5]]
    expected['participants'] = [p for p in expected['participants'] if p['id'] != 'api']
    assert run(doc, {'op': 'remove_participant', 'id': 'api', 'dependents': 'cascade'}) == norm(expected)


def test_insert_item_where_at_depth_two():
    doc = nested_sequence()
    new = msg('m9')

    def section_items(document):
        return document['items'][4]['sections'][0]['items'][2]['sections'][0]['items']

    for where, expected_ids in (({'before': 'm8'}, ['m9', 'm8']), ({'after': 'm8'}, ['m8', 'm9']),
                                ({'fragment': 'f2', 'at': 'start'}, ['m9', 'm8']),
                                ({'fragment': 'f2', 'at': 'end'}, ['m8', 'm9'])):
        result = run(doc, {'op': 'insert_item', 'item': new, 'where': where})
        assert [x['id'] for x in section_items(result)] == expected_ids, where
        expected = copy.deepcopy(doc)
        section_items(expected).insert(expected_ids.index('m9'), norm_item(new))
        assert result == norm(expected)


def norm_item(item):
    return {'style': 'call', 'activate': False, 'deactivate': False, **item}


def test_insert_item_top_level_and_into_a_section():
    doc = base('sequence')
    expected = copy.deepcopy(doc)
    expected['items'].append(norm_item(msg('m9')))
    assert run(doc, {'op': 'insert_item', 'item': msg('m9')}) == norm(expected)
    expected = copy.deepcopy(doc)
    expected['items'][4]['sections'][1]['items'].insert(0, norm_item(msg('m9')))
    assert run(doc, {'op': 'insert_item', 'item': msg('m9'),
                     'where': {'fragment': 'f1', 'section': 1, 'at': 'start'}}) == norm(expected)


def test_move_item():
    doc = base('sequence')
    expected = copy.deepcopy(doc)
    m3 = expected['items'].pop(3)
    expected['items'].append(m3)
    assert run(doc, {'op': 'move_item', 'id': 'm3', 'where': {'after': 'm7'}}) == norm(expected)
    assert fails(doc, {'op': 'move_item', 'id': 'f1', 'where': {'fragment': 'f1'}}).status == 409
    assert fails(nested_sequence(), {'op': 'move_item', 'id': 'f1', 'where': {'before': 'm8'}}).status == 409


def test_remove_item_unwrap_keeps_order_and_cascade_removes_all():
    doc = base('sequence')
    f1 = doc['items'][4]
    expected = copy.deepcopy(doc)
    expected['items'][4:5] = f1['sections'][0]['items'] + f1['sections'][1]['items']
    assert run(doc, {'op': 'remove_item', 'id': 'f1', 'contents': 'unwrap'}) == norm(expected)
    assert [x['id'] for x in expected['items']] == ['m1', 'm2', 'n1', 'm3', 'm4', 'm5', 'm6', 'm7']

    expected = copy.deepcopy(doc)
    del expected['items'][4]
    assert run(doc, {'op': 'remove_item', 'id': 'f1', 'contents': 'cascade'}) == norm(expected)

    expected = copy.deepcopy(doc)
    del expected['items'][3]
    assert run(doc, {'op': 'remove_item', 'id': 'm3'}) == norm(expected)


def test_update_message_and_sequence_note():
    doc = base('sequence')
    expected = copy.deepcopy(doc)
    expected['items'][3].update(label='screen_document', style='async')
    expected['items'][2].update(text='Retries are safe.', over=['api'])
    assert run(doc, {'op': 'update_message', 'id': 'm3', 'set': {'label': 'screen_document', 'style': 'async'}},
               {'op': 'update_note', 'id': 'n1', 'set': {'text': 'Retries are safe.', 'over': ['api']}}) == norm(expected)
    assert fails(doc, {'op': 'update_note', 'id': 'n1', 'set': {'attach': 'api'}}).status == 422


def test_update_fragment_and_sections():
    doc = base('sequence')
    expected = copy.deepcopy(doc)
    expected['items'][4]['operator'] = 'par'
    assert run(doc, {'op': 'update_fragment', 'id': 'f1', 'set': {'operator': 'par'}}) == norm(expected)

    expected = copy.deepcopy(doc)
    expected['items'][4]['sections'].insert(1, {'guard': 'retrying', 'items': []})
    assert run(doc, {'op': 'add_section', 'fragment': 'f1', 'guard': 'retrying', 'index': 1}) == norm(expected)
    expected = copy.deepcopy(doc)
    expected['items'][4]['sections'].append({'items': []})
    assert run(doc, {'op': 'add_section', 'fragment': 'f1'}) == norm(expected)

    expected = copy.deepcopy(doc)
    expected['items'][4]['sections'][0]['guard'] = 'fresh'
    del expected['items'][4]['sections'][1]['guard']
    assert run(doc, {'op': 'update_section', 'fragment': 'f1', 'index': 0, 'guard': 'fresh'},
               {'op': 'update_section', 'fragment': 'f1', 'index': 1, 'clear': ['guard']}) == norm(expected)

    expected = copy.deepcopy(doc)
    del expected['items'][4]['sections'][1]
    assert run(doc, {'op': 'remove_section', 'fragment': 'f1', 'index': 1, 'contents': 'cascade'}) == norm(expected)


def test_set_meta_on_a_sequence_diagram():
    doc = base('sequence')
    expected = copy.deepcopy(doc)
    expected['title'] = 'Revise, retried'
    assert run(doc, {'op': 'set_meta', 'set': {'title': 'Revise, retried'}}) == norm(expected)


# --- M2-M4 ---------------------------------------------------------------

def test_batch_is_atomic():
    doc = base('architecture')
    before = copy.deepcopy(doc)
    error = fails(doc, {'op': 'update_node', 'id': 'owner', 'set': {'label': 'Me'}},
                  {'op': 'remove_edge', 'id': 'e404'},
                  {'op': 'remove_node', 'id': 'claude', 'dependents': 'cascade'})
    assert (error.index, error.status) == (1, 400)
    assert str(error) == 'operations[1] (remove_edge): unknown edge at id'
    assert doc == before


def test_refuse_policies():
    error = fails(base('architecture'), {'op': 'remove_node', 'id': 'api', 'dependents': 'refuse'})
    assert error.status == 409 and 'edges[2], edges[3]' in str(error) and "dependents='cascade'" in str(error)
    assert fails(base('architecture'), {'op': 'remove_group', 'id': 'home-server', 'members': 'refuse'}).status == 409
    assert fails(base('sequence'), {'op': 'remove_item', 'id': 'f1'}).status == 409
    error = fails(base('sequence'), {'op': 'remove_participant', 'id': 'db', 'dependents': 'refuse'})
    assert error.status == 409 and 'items[4].sections[0].items[0]' in str(error)
    assert fails(base('sequence'), {'op': 'remove_section', 'fragment': 'f1', 'index': 0, 'contents': 'refuse'}).status == 409
    doc = base('sequence')
    doc['items'][4]['operator'] = 'opt'
    del doc['items'][4]['sections'][1]
    assert fails(norm(doc), {'op': 'remove_section', 'fragment': 'f1', 'index': 0, 'contents': 'cascade'}).status == 409
    for op in ({'op': 'remove_node', 'id': 'api'}, {'op': 'remove_group', 'id': 'home-server'},
               {'op': 'remove_participant', 'id': 'db'}, {'op': 'remove_section', 'fragment': 'f1', 'index': 0}):
        with pytest.raises(ValidationError):
            OPS.validate_python([op])


def test_result_validation():
    doc = base('architecture')
    error = fails(doc, {'op': 'add_group', 'group': {'id': 'inner', 'label': 'Inner', 'parent': 'home-server'}},
                  {'op': 'move_to_group', 'ids': ['home-server'], 'group': 'inner'})
    assert error.status == 422 and error.field == 'document' and 'nesting cycle' in str(error)
    assert str(error).startswith('the edited diagram is invalid: ')

    error = fails(base('sequence'), {'op': 'move_item', 'id': 'm7', 'where': {'before': 'm1'}})
    assert error.status == 422 and 'no open activation' in str(error)
    error = fails(base('sequence'), {'op': 'update_fragment', 'id': 'f1', 'set': {'operator': 'loop'}})
    assert error.status == 422 and 'exactly one section' in str(error)
    error = fails(base('sequence'), {'op': 'add_node', 'node': {'id': 'x', 'label': 'X'}})
    assert (error.status, str(error)) == (422, 'operations[0] (add_node): not available for a sequence diagram')
    error = fails(base('flow'), {'op': 'add_section', 'fragment': 'f1'})
    assert str(error) == 'operations[0] (add_section): not available for a flow diagram'


def test_an_intermediate_invalid_state_is_fine():
    """Validated once at the end: an edge may be added before its endpoint."""
    doc = base('architecture')
    result = run(doc, {'op': 'add_edge', 'edge': {'id': 'e5', 'source': 'api', 'target': 'cache'}},
                 {'op': 'add_node', 'node': {'id': 'cache', 'label': 'Cache', 'role': 'cache'}})
    assert node(result, 'cache')['label'] == 'Cache'


def test_reference_errors_name_the_operation_and_id():
    doc = base('sequence')
    cases = [
        ({'op': 'insert_item', 'item': msg('m9'), 'where': {'before': 'zz'}}, 400, 'unknown position reference at where.before'),
        ({'op': 'insert_item', 'item': msg('m9'), 'where': {'fragment': 'f1', 'section': 5}}, 400, "has no section 5"),
        ({'op': 'insert_item', 'item': msg('m9'), 'where': {'before': 'm1', 'after': 'm2'}}, 422, 'more than one anchor'),
        ({'op': 'insert_item', 'item': msg('m1')}, 409, 'item.id is already used'),
        ({'op': 'update_message', 'id': 'f1'}, 400, 'unknown message at id'),
        ({'op': 'add_participant', 'participant': {'id': 'x', 'label': 'X'}, 'where': {'fragment': 'f1'}}, 422, 'before or after'),
    ]
    for op, status, text in cases:
        error = fails(doc, op)
        assert error.status == status and text in str(error), (op, str(error))
        assert str(error).startswith(f"operations[0] ({op['op']}): ")


def test_vocabulary_partitions_the_union():
    union = {model.model_fields['op'].annotation.__args__[0] for model in Operation.__origin__.__args__}
    listed = [*COMMON_OPS, *GRAPH_OPS, *SEQUENCE_OPS]
    assert len(listed) == len(set(listed)) and set(listed) == union


def test_summary_has_no_prose():
    summary = diagram_summary(base('architecture'))
    assert 'MCP client on each machine' not in str(summary)
    assert 'Only the API touches the database.' not in str(summary)

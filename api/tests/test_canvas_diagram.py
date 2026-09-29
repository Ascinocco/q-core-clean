"""canvas.diagram/v1 (Canvas). Invented data only.

No run of seven or more digits appears in this file: every such run is built
in code, because the account-number guard is what several tests exercise.
"""
import copy
import json
import re
import statistics
import subprocess
import sys
import time
from uuid import uuid4

import pytest
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from api.canvas_diagram import (
    LIMITS, OBJECTS, OBJECT_LISTS, REFERENCE_FIELDS, TEXT_FIELDS, TOKENS,
    DiagramDocument, Fragment, Group, Layout, Message, Node, Note, Participant, Port, Position,
    Section, SequenceNote, Edge, encoded_size, screen_diagram, walk_items,
)
from api.artifact_models import Provenance
from api.privacy import STORED_TEXT_REFUSAL
from api.config import REPO_ROOT
from api.main import _error_detail

# Verbatim from the A7 ticket; test_format_doc_examples_match pins the doc to these.
EXAMPLES_JSON = {
    'architecture': '{"type": "architecture", "title": "q-core containers", "layout": {"direction": "LR"}, "groups": [{"id": "home-server", "label": "Home server (NixOS)", "style": "boundary"}], "nodes": [{"id": "owner", "label": "Owner", "role": "person"}, {"id": "claude", "label": "Claude Code", "role": "client", "description": "MCP client on each machine"}, {"id": "mcp", "label": "q_core_mcp", "role": "service", "group": "home-server"}, {"id": "api", "label": "FastAPI app", "role": "service", "group": "home-server", "tone": "accent"}, {"id": "db", "label": "SQLite", "role": "database", "group": "home-server"}], "edges": [{"id": "e1", "source": "owner", "target": "claude", "label": "chats"}, {"id": "e2", "source": "claude", "target": "mcp", "label": "MCP over Tailscale"}, {"id": "e3", "source": "mcp", "target": "api", "label": "HTTP"}, {"id": "e4", "source": "api", "target": "db", "label": "reads/writes"}], "notes": [{"id": "n1", "text": "Only the API touches the database.", "attach": "db"}]}',
    'flow': '{"type": "flow", "title": "Statement intake", "layout": {"direction": "TB", "spacing": "compact"}, "nodes": [{"id": "start", "label": "Statement dropped", "role": "start"}, {"id": "scrub", "label": "Server-side scrub", "role": "step"}, {"id": "ok", "label": "Scrub clean?", "role": "decision"}, {"id": "preview", "label": "Preview import", "role": "step"}, {"id": "review", "label": "Local review", "role": "io", "tone": "warning"}, {"id": "done", "label": "Imported", "role": "end", "position": {"x": 400, "y": 520}}], "edges": [{"id": "f1", "source": "start", "target": "scrub"}, {"id": "f2", "source": "scrub", "target": "ok"}, {"id": "f3", "source": "ok", "target": "preview", "label": "yes"}, {"id": "f4", "source": "ok", "target": "review", "label": "no", "line": "dashed"}, {"id": "f5", "source": "preview", "target": "done"}]}',
    'data-model': '{"type": "data-model", "title": "Artifacts", "layout": {"direction": "LR"}, "nodes": [{"id": "artifacts", "label": "artifacts", "role": "table", "ports": [{"id": "id", "label": "id", "detail": "TEXT", "key": "pk"}, {"id": "kind", "label": "kind", "detail": "TEXT"}, {"id": "revision", "label": "revision", "detail": "INTEGER"}]}, {"id": "revisions", "label": "artifact_revisions", "role": "table", "ports": [{"id": "artifact-id", "label": "artifact_id", "detail": "TEXT", "key": "pk-fk"}, {"id": "revision", "label": "revision", "detail": "INTEGER", "key": "pk"}, {"id": "payload", "label": "payload", "detail": "TEXT"}]}], "edges": [{"id": "r1", "source": "revisions", "source_port": "artifact-id", "target": "artifacts", "target_port": "id", "cardinality": "n:1"}]}',
    'sequence': '{"type": "sequence", "title": "Revise an artifact", "participants": [{"id": "claude", "label": "Claude", "role": "actor"}, {"id": "mcp", "label": "q_core_mcp"}, {"id": "api", "label": "API"}, {"id": "db", "label": "SQLite", "role": "database"}], "items": [{"kind": "message", "id": "m1", "source": "claude", "target": "mcp", "label": "revise_artifact(id, payload)", "activate": true}, {"kind": "message", "id": "m2", "source": "mcp", "target": "api", "label": "PUT /artifacts/{id}", "activate": true}, {"kind": "note", "id": "n1", "over": ["mcp", "api"], "text": "Retries return the same revision."}, {"kind": "message", "id": "m3", "source": "api", "target": "api", "label": "screen_payload"}, {"kind": "fragment", "id": "f1", "operator": "alt", "sections": [{"guard": "revision is current", "items": [{"kind": "message", "id": "m4", "source": "api", "target": "db", "label": "insert revision"}, {"kind": "message", "id": "m5", "source": "api", "target": "mcp", "label": "200 artifact", "style": "return", "deactivate": true}]}, {"guard": "stale revision", "items": [{"kind": "message", "id": "m6", "source": "api", "target": "mcp", "label": "409 conflict", "style": "return"}]}]}, {"kind": "message", "id": "m7", "source": "mcp", "target": "claude", "label": "result", "style": "return", "deactivate": true}]}',
}
EXAMPLES = {name: json.loads(text) for name, text in EXAMPLES_JSON.items()}
SENTINEL = 'Sentinel prose that must never be echoed'
# A seven-digit run, built rather than written.
RUN = ''.join(str(i % 10) for i in range(1, 8))


def example(name):
    return copy.deepcopy(EXAMPLES[name])


def rendered(error):
    """The {field, message} details the API would return, as 'field: message'."""
    if isinstance(error, RequestValidationError):
        errors = error.errors()
    else:
        errors = [{**e, 'loc': ('body', 'document', *e['loc'])}
                  for e in error.errors(include_url=False, include_input=False)]
    return [_error_detail(e) for e in errors]


def texts(value, key=None):
    """Every free-text value in a document (the things errors must not echo)."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield from texts(v, k)
    elif isinstance(value, list):
        for v in value:
            yield from texts(v, key)
    elif isinstance(value, str) and key in TEXT_FIELDS:
        yield value


def graph(n_nodes=0, n_edges=0, **extra):
    nodes = [{'id': f'n{i}', 'label': f'Node {i}'} for i in range(n_nodes)]
    edges = [{'id': f'e{i}', 'source': f'n{i % n_nodes}', 'target': f'n{(i + 1) % n_nodes}'} for i in range(n_edges)]
    return {'type': 'architecture', 'title': 'Generated', 'nodes': nodes, 'edges': edges, **extra}


def sequence(items, participants=('a', 'b')):
    return {'type': 'sequence', 'title': 'Generated', 'items': items,
            'participants': [{'id': p, 'label': p.upper()} for p in participants]}


def message(i, **extra):
    return {'kind': 'message', 'id': f'm{i}', 'source': 'a', 'target': 'b', 'label': f'call {i}', **extra}


def nested_fragments(depth):
    items = [message(0)]
    for level in range(depth):
        items = [{'kind': 'fragment', 'id': f'f{level}', 'operator': 'opt', 'sections': [{'items': items}]}]
    return items


# --- M1 ---

@pytest.mark.parametrize('name', list(EXAMPLES))
def test_examples_validate(name):
    doc = DiagramDocument.model_validate(example(name))
    assert doc.type == name
    stored = doc.model_dump(mode='json', exclude_none=True)
    assert DiagramDocument.model_validate(stored).model_dump(mode='json', exclude_none=True) == stored


def test_format_doc_examples_match():
    doc = (REPO_ROOT / 'docs' / 'canvas-diagram-format.md').read_text()
    blocks = re.findall(r'```json\n(.*?)\n```', doc, re.S)
    assert blocks == list(EXAMPLES_JSON.values())


def test_examples_match_the_generated_json_schema():
    jsonschema = pytest.importorskip('jsonschema')
    schema = json.loads((REPO_ROOT / 'docs' / 'canvas-diagram.schema.json').read_text())
    for doc in EXAMPLES.values():
        jsonschema.validate(doc, schema)


def test_walk_items_is_depth_first_document_order():
    doc = DiagramDocument.model_validate(example('sequence'))
    walked = [(path, item.id, depth) for path, item, depth in walk_items(doc.items)]
    assert walked == [
        ('items[0]', 'm1', 0), ('items[1]', 'm2', 0), ('items[2]', 'n1', 0), ('items[3]', 'm3', 0),
        ('items[4]', 'f1', 1), ('items[4].sections[0].items[0]', 'm4', 1),
        ('items[4].sections[0].items[1]', 'm5', 1), ('items[4].sections[1].items[0]', 'm6', 1),
        ('items[5]', 'm7', 0)]


# --- M2 ---

def edit(name, change):
    def build():
        doc = example(name)
        doc['description'] = SENTINEL
        change(doc)
        return doc
    return build


def whole(doc):
    return lambda: doc


def set_path(path, value):
    def change(doc):
        *parents, last = path
        target = doc
        for part in parents:
            target = target[part]
        target[last] = value
    return change


def seq_item(*path):
    return ('items', *path)


CYCLE = graph(1, groups=[{'id': 'core', 'label': 'Core', 'parent': 'edge'},
                         {'id': 'edge', 'label': 'Edge', 'parent': 'core'}])
DEEP_GROUPS = graph(1, groups=[{'id': f'g{i}', 'label': f'G {i}', **({'parent': f'g{i - 1}'} if i else {})}
                               for i in range(LIMITS['group_depth'] + 1)])

REJECTIONS = {
    # 1. per-type objects
    'sequence-with-nodes': (edit('sequence', set_path(('nodes',), [{'id': 'x', 'label': 'X'}])),
                            'document: nodes: not allowed in a sequence diagram'),
    'graph-with-participants': (edit('architecture', set_path(('participants',), [{'id': 'x', 'label': 'X'}])),
                                'participants: not allowed in an architecture diagram'),
    'graph-with-items': (edit('flow', set_path(('items',), [message(9)])), 'items: not allowed in a flow diagram'),
    'sequence-with-direction': (edit('sequence', set_path(('layout',), {'direction': 'LR'})),
                                'layout.direction: not allowed in a sequence diagram'),
    # 2. one id namespace
    'duplicate-nested-id': (edit('sequence', set_path(seq_item(4, 'sections', 1, 'items', 0, 'id'), 'm1')),
                            'items[4].sections[1].items[0].id: already used by items[0]'),
    'duplicate-id-across-kinds': (edit('architecture', set_path(('edges', 0, 'id'), 'db')),
                                  'edges[0].id: already used by nodes[4]'),
    # 3. roles and ports
    'role-for-type': (edit('architecture', set_path(('nodes', 0, 'role'), 'table')),
                      "nodes[0].role: 'table' not allowed in an architecture diagram"),
    'ports-outside-data-model': (edit('flow', set_path(('nodes', 0, 'ports'), [{'id': 'p', 'label': 'P'}])),
                                 'nodes[0].ports: only data-model diagrams have ports'),
    'duplicate-port': (edit('data-model', set_path(('nodes', 0, 'ports', 1, 'id'), 'id')),
                       'nodes[0].ports[1].id: already used in nodes[0]'),
    # 4. references resolve
    'unknown-group': (edit('architecture', set_path(('nodes', 2, 'group'), 'nowhere')),
                      'nodes[2].group: unknown group'),
    'unknown-parent': (edit('architecture', set_path(('groups', 0, 'parent'), 'nowhere')),
                       'groups[0].parent: unknown group'),
    'unknown-edge-target': (edit('architecture', set_path(('edges', 0, 'target'), 'nowhere')),
                            'edges[0].target: unknown node or group'),
    'missing-port': (edit('data-model', set_path(('edges', 0, 'target_port'), 'x')),
                     'edges[0].target_port: the target has no such port'),
    'port-on-group': (edit('architecture', lambda d: d['edges'][1].update(target='home-server', target_port='x')),
                      'edges[1].target_port: the target has no such port'),
    'unknown-attach': (edit('architecture', set_path(('notes', 0, 'attach'), 'nowhere')),
                       'notes[0].attach: unknown node or group'),
    # 5. group nesting
    'group-cycle': (whole(CYCLE), 'groups[0].parent: nesting cycle through groups[0]'),
    'groups-too-deep': (whole(DEEP_GROUPS), 'groups[6].parent: groups nest deeper than 6'),
    # 6. data-model only cardinality; attached notes are placed by their target
    'cardinality-outside-data-model': (edit('flow', set_path(('edges', 0, 'cardinality'), '1:n')),
                                       'edges[0].cardinality: only data-model diagrams have cardinality'),
    'attached-note-with-position': (edit('architecture', set_path(('notes', 0, 'position'), {'x': 1, 'y': 2})),
                                    'notes[0].position: an attached note is placed by its target'),
    # 7. sequence references
    'message-to-unknown-participant': (edit('sequence', set_path(seq_item(0, 'target'), 'nowhere')),
                                       'items[0].target: unknown participant'),
    'note-over-unknown-participant': (edit('sequence', set_path(seq_item(2, 'over'), ['mcp', 'nowhere'])),
                                      'items[2].over[1]: unknown participant'),
    # 8. activation balance, depth-first in document order
    'deactivate-without-activation': (edit('sequence', set_path(seq_item(0, 'deactivate'), True)),
                                      'items[0].deactivate: the source has no open activation'),
    'alt-branches-walked-in-order': (edit('sequence', set_path(seq_item(4, 'sections', 1, 'items', 0, 'deactivate'), True)),
                                     'items[4].sections[1].items[0].deactivate: the source has no open activation'),
    # 9. fragment shape, depth and totals
    'opt-with-two-sections': (edit('sequence', set_path(seq_item(4, 'operator'), 'opt')),
                              'items[4].sections: opt has exactly one section'),
    'loop-with-two-sections': (edit('sequence', set_path(seq_item(4, 'operator'), 'loop')),
                               'items[4].sections: loop has exactly one section'),
    'fragments-nested-5-deep': (whole(sequence(nested_fragments(5))),
                                'items[0].sections[0].items[0].sections[0].items[0].sections[0].items[0]'
                                '.sections[0].items[0]: fragments nest deeper than 4'),
    'too-many-messages': (whole(sequence([message(i) for i in range(LIMITS['messages'] + 1)])),
                          'items: more than 400 messages'),
    'too-many-fragments': (whole(sequence([{'kind': 'fragment', 'id': f'f{i}', 'operator': 'opt', 'sections': [{}]}
                                           for i in range(LIMITS['fragments'] + 1)])),
                           'items: more than 60 fragments'),
    'too-many-items-in-total': (whole(sequence([{'kind': 'fragment', 'id': 'f', 'operator': 'opt', 'sections': [
        {'items': [{'kind': 'note', 'id': f'n{i}', 'over': ['a'], 'text': 'Note'} for i in range(LIMITS['items'])]}]}])),
                                'items: more than 500 items in total'),
    # field-level
    'bad-id-pattern': (edit('architecture', set_path(('nodes', 0, 'id'), 'Not A Slug')),
                       'document.nodes.0.id: String should match pattern'),
    'digit-run-in-label': (edit('architecture', set_path(('nodes', 0, 'label'), f'Build {RUN}')),
                           'document.nodes.0.label: Value error, text that may be stored'),
    'digit-run-in-id': (edit('architecture', set_path(('nodes', 0, 'id'), f'n{RUN}')),
                        'document.nodes.0.id: Value error, text that may be stored'),
    'infinite-coordinate': (edit('flow', set_path(('nodes', 5, 'position', 'x'), float('inf'))),
                            'document.nodes.5.position.x: Input should be a finite number'),
    'coordinate-out-of-range': (edit('flow', set_path(('nodes', 5, 'position', 'y'), 100001)),
                                'document.nodes.5.position.y: Input should be less than or equal to'),
    '301-nodes': (whole(graph(LIMITS['nodes'] + 1)), 'document.nodes: List should have at most 300 items'),
    '61-ports': (edit('data-model', set_path(('nodes', 0, 'ports'), [{'id': f'p{i}', 'label': 'P'} for i in range(61)])),
                 'document.nodes.0.ports: List should have at most 60 items'),
    '3-participant-note': (edit('sequence', set_path(seq_item(2, 'over'), ['claude', 'mcp', 'api'])),
                           'document.items.2.note.over: List should have at most 2 items'),
    'unknown-key': (edit('architecture', set_path(('nodes', 0, 'colour'), 'red')),
                    'document.nodes.0.colour: Extra inputs are not permitted'),
    'operator-break': (edit('sequence', set_path(seq_item(4, 'operator'), 'break')),
                       "document.items.4.fragment.operator: Input should be 'alt', 'opt', 'loop' or 'par'"),
    'operator-critical': (edit('sequence', set_path(seq_item(4, 'operator'), 'critical')),
                          "document.items.4.fragment.operator: Input should be 'alt', 'opt', 'loop' or 'par'"),
}


@pytest.mark.parametrize('case', list(REJECTIONS))
def test_rejects(case):
    build, expected = REJECTIONS[case]
    doc = build()
    with pytest.raises(ValidationError) as caught:
        DiagramDocument.model_validate(doc)
    details = rendered(caught.value)
    messages = [f"{d['field']}: {d['message']}" for d in details]
    assert any(expected in m for m in messages), messages
    reply = json.dumps(details)
    assert RUN not in reply
    # No label or other text is echoed; short words ('yes', 'API') could occur by chance.
    for text in texts(doc):
        if len(text) > 12:
            assert text not in reply


def test_rejection_summary_is_capped():
    doc = graph(1, edges=[{'id': f'e{i}', 'source': 'n0', 'target': f'x{i}'} for i in range(12)])
    with pytest.raises(ValidationError) as caught:
        DiagramDocument.model_validate(doc)
    [detail] = rendered(caught.value)
    assert detail['message'].count('unknown node or group') == 10
    assert detail['message'].endswith('(+2 more)')


# --- M3, M4: the privacy screen ---

NAME, SLUG = 'Alex Example', 'alex-example'


def screened_errors(doc, settings):
    with pytest.raises(RequestValidationError) as caught:
        screen_diagram(doc, settings)
    return rendered(caught.value)


def test_screen_keeps_ids_and_redacts_labels(test_settings):
    doc = example('architecture')
    doc['nodes'][0]['label'] = f'Owner: {NAME}'
    stored = screen_diagram(doc, test_settings)
    assert stored['nodes'][0]['label'] == 'Owner: [REDACTED]'
    assert stored['nodes'][0]['id'] == 'owner'

    # An id equal to the profile name, slugified, is refused and never rewritten.
    doc = example('architecture')
    doc['nodes'][0]['id'] = SLUG
    doc['edges'][0]['source'] = SLUG
    details = screened_errors(doc, test_settings)
    assert details[0] == {'field': 'document.nodes.0.id',
                          'message': 'nodes[0].id: ids must not contain personal names or addresses'}
    assert {'document.nodes.0.id', 'document.edges.0.source'} <= {d['field'] for d in details}

    doc = example('architecture')
    doc['nodes'][4]['id'] = doc['notes'][0]['attach'] = doc['edges'][3]['target'] = SLUG
    assert 'document.notes.0.attach' in {d['field'] for d in screened_errors(doc, test_settings)}

    # Rename the participant everywhere, so the document is otherwise valid.
    doc = json.loads(EXAMPLES_JSON['sequence'].replace('"mcp"', f'"{SLUG}"'))
    assert doc['items'][2]['over'][0] == SLUG
    details = screened_errors(doc, test_settings)
    assert 'document.items.2.over.0' in {d['field'] for d in details}
    assert NAME not in json.dumps(details) and SLUG not in json.dumps(details)


def test_screen_never_stores_redacted_references(test_settings):
    for name in EXAMPLES:
        doc = example(name)

        def name_every_text(value, key=None):
            if isinstance(value, dict):
                for k, v in value.items():
                    if k in TEXT_FIELDS and isinstance(v, str):
                        value[k] = f'{v[:40]} {NAME}'
                    else:
                        name_every_text(v, k)
            elif isinstance(value, list):
                for v in value:
                    name_every_text(v, key)
        name_every_text(doc)
        stored = screen_diagram(doc, test_settings)

        def check(value, key=None):
            if isinstance(value, dict):
                for k, v in value.items():
                    check(v, k)
            elif isinstance(value, list):
                for v in value:
                    check(v, key)
            elif key in REFERENCE_FIELDS or key == 'over':
                assert value != '[REDACTED]' and 'REDACTED' not in value, (name, key)
            elif key in TEXT_FIELDS:
                assert NAME not in value and value.endswith('[REDACTED]'), (name, key)
        check(stored)
        assert stored == DiagramDocument.model_validate(stored).model_dump(mode='json', exclude_none=True)


def test_screen_refuses_text_that_hides_a_digit_run(test_settings):
    # A zero-width space passes the model's digit guard; the screen removes it first.
    doc = example('flow')
    doc['edges'][0]['label'] = f'{RUN[:3]}​{RUN[3:]}'
    details = screened_errors(doc, test_settings)
    assert details == [{'field': 'document.edges.0.label',
                        'message': 'edges[0].label: text that may be stored or returned must not contain a full '
                                   'account, routing, card or SIN-shaped number; use a masked last four instead'}]


def test_screen_revalidates(test_settings):
    # An email address redacts to the longer '[REDACTED]': 120 characters would become 124.
    label = 'x' * 110 + ' x@y.io ok'
    assert len(label) == 120
    doc = example('architecture')
    doc['nodes'][0]['label'] = label
    DiagramDocument.model_validate(doc)
    details = screened_errors(doc, test_settings)
    assert details == [{'field': 'document.nodes.0.label', 'message': 'String should have at most 120 characters'}]


def test_every_model_field_has_a_privacy_rule():
    models = (DiagramDocument, Layout, Node, Port, Position, Edge, Group, Note, Participant,
              Message, SequenceNote, Section, Fragment)
    handled = TEXT_FIELDS | REFERENCE_FIELDS | OBJECT_LISTS | OBJECTS | TOKENS | {'over', 'tags', 'provenance'}
    for model in models:
        assert set(model.model_fields) <= handled, model.__name__
    assert set(Provenance.model_fields) == {'repo', 'sha', 'paths'}


# --- M5, M9: limits ---

def full_graph():
    groups = [{'id': f'g{i}', 'label': f'Group {i}'} for i in range(LIMITS['groups'])]
    nodes = [{'id': f'n{i}', 'label': f'Service number {i}', 'description': 'Handles requests ' * 5,
              'group': f'g{i % LIMITS["groups"]}', 'role': 'service'} for i in range(LIMITS['nodes'])]
    edges = [{'id': f'e{i}', 'source': f'n{i % 300}', 'target': f'n{(i * 7 + 1) % 300}', 'label': f'calls {i}'}
             for i in range(LIMITS['edges'])]
    notes = [{'id': f'note{i}', 'text': 'A note about this part.', 'attach': f'n{i}'} for i in range(LIMITS['notes'])]
    return {'type': 'architecture', 'title': 'At the limits', 'groups': groups, 'nodes': nodes,
            'edges': edges, 'notes': notes}


def test_limits_and_timing():
    doc = full_graph()
    DiagramDocument.model_validate(doc)
    runs = []
    for _ in range(20):
        started = time.perf_counter()
        DiagramDocument.model_validate(doc)
        runs.append(time.perf_counter() - started)
    assert statistics.median(runs) < 0.2, runs


def sized(target):
    """A diagram whose stored encoding is exactly `target` bytes (multi-byte text fills it)."""
    notes = [{'id': f'note{i}', 'text': '€' * 1000} for i in range(LIMITS['notes'])]
    doc = {'type': 'architecture', 'title': 'Sized', 'description': 'a', 'nodes': [], 'notes': notes}
    stored = lambda: encoded_size(DiagramDocument.model_validate(doc).model_dump(mode='json', exclude_none=True))
    # Nodes of about 1.3 KB each, stopping short enough for the description (≤2000) to finish the job.
    size = stored()
    while size < target - 1900:
        node = {'id': f'n{len(doc["nodes"])}', 'label': '€' * 120, 'description': '€' * 300}
        size += encoded_size(Node.model_validate(node).model_dump(mode='json', exclude_none=True)) + bool(doc['nodes'])
        doc['nodes'].append(node)
    size = stored()
    doc['description'] = 'a' * (1 + target - size)
    return doc


def test_hard_limits(test_settings):
    assert LIMITS['nodes'] == 300 and LIMITS['edges'] == 600 and LIMITS['messages'] == 400
    DiagramDocument.model_validate(graph(LIMITS['nodes']))
    with pytest.raises(ValidationError):
        DiagramDocument.model_validate(graph(LIMITS['nodes'] + 1))
    DiagramDocument.model_validate(graph(2, LIMITS['edges']))
    with pytest.raises(ValidationError):
        DiagramDocument.model_validate(graph(2, LIMITS['edges'] + 1))
    DiagramDocument.model_validate(sequence([message(i) for i in range(LIMITS['messages'])]))
    with pytest.raises(ValidationError):
        DiagramDocument.model_validate(sequence([message(i) for i in range(LIMITS['messages'] + 1)]))

    limit = LIMITS['document_bytes']
    assert limit == 512 * 1024
    stored = screen_diagram(sized(limit), test_settings)
    assert encoded_size(stored) == limit
    details = screened_errors(sized(limit + 1), test_settings)
    assert details == [{'field': 'document', 'message': 'document: diagram exceeds 512 KiB'}]


# --- M6 ---

def test_json_schema_is_current():
    printed = subprocess.run([sys.executable, str(REPO_ROOT / 'scripts' / 'canvas_diagram_schema.py')],
                             capture_output=True, text=True, check=True, cwd=REPO_ROOT).stdout
    assert printed == (REPO_ROOT / 'docs' / 'canvas-diagram.schema.json').read_text()


# --- M10 (screen level; the HTTP round trip joins once the kind is registered) ---

def provenance_sha():
    # 40 lowercase hex characters that contain a seven-digit run.
    return ('ab' + RUN + 'cdef' * 8)[:40]


def test_meta_and_provenance_screen(test_settings):
    sha = provenance_sha()
    assert re.fullmatch(r'[0-9a-f]{40}', sha) and RUN in sha
    doc = example('architecture')
    doc['tags'] = ['architecture', 'q-core']
    doc['provenance'] = [{'repo': 'q-core', 'sha': sha, 'paths': ['api/artifacts.py', 'db/schema.sql']}]
    stored = screen_diagram(doc, test_settings)
    assert stored['provenance'] == doc['provenance'] and stored['tags'] == doc['tags']

    doc['tags'] = ['architecture', SLUG]
    details = screened_errors(doc, test_settings)
    assert details == [{'field': 'document.tags.1',
                        'message': 'tags[1]: tags must not contain personal names or addresses'}]

    doc = example('architecture')
    doc['description'] = f'Built from {sha}'
    with pytest.raises(ValidationError):
        DiagramDocument.model_validate(doc)


def test_pathological_nesting_is_a_validation_error():
    # Pydantic's own recursion guard answers first; it must stay a 422, never a 500.
    with pytest.raises(ValidationError):
        DiagramDocument.model_validate(sequence(nested_fragments(3000)))


def test_provenance_repo_and_paths_are_refused_not_rewritten(test_settings):
    doc = example('flow')
    doc['provenance'] = [{'repo': SLUG, 'sha': 'ab' * 20, 'paths': [f'docs/{SLUG}.md']}]
    fields = {d['field'] for d in screened_errors(doc, test_settings)}
    assert fields == {'document.provenance.0.repo', 'document.provenance.0.paths.0'}


# --- M7, M10 and the screen through the real handler ---

HEADERS = {'Authorization': 'Bearer test-token'}


def post(client, document, **changes):
    body = {'request_id': str(uuid4()), 'kind': 'diagram', 'document': document,
            'actor': 'Test', 'note': 'Diagram fixture', **changes}
    return client.post('/artifacts', headers=HEADERS, json=body)


def canonical(document):
    return DiagramDocument.model_validate(document).model_dump(mode='json', exclude_none=True)


def test_diagram_artifact_roundtrip(client, test_settings):
    created = {}
    for name in EXAMPLES:
        response = post(client, example(name))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body['kind'] == 'diagram' and body['viewer_path'] == f"/ui/canvas/{body['id']}"
        assert body['document'] == canonical(example(name))
        fetched = client.get(f"/artifacts/{body['id']}", headers=HEADERS)
        assert fetched.status_code == 200 and fetched.json()['document'] == body['document']
        created[name] = body

    listed = client.get('/artifacts?kind=diagram', headers=HEADERS).json()
    assert {row['title'] for row in listed['items']} == {e['title'] for e in EXAMPLES.values()}

    # A revision is validated against the stored kind; an invalid one writes nothing.
    target = created['architecture']
    invalid = example('architecture')
    invalid['edges'][0]['target'] = 'nowhere'
    edit = {'expected_revision': 1, 'document': invalid, 'actor': 'Test', 'note': 'Break a reference'}
    response = client.put(f"/artifacts/{target['id']}", headers=HEADERS, json=edit)
    assert response.status_code == 422
    assert 'edges[0].target: unknown node or group' in response.text
    assert 'nowhere' not in response.text
    valid = example('architecture')
    valid['nodes'][0]['label'] = 'Owner (revised)'
    edit['document'] = valid
    response = client.put(f"/artifacts/{target['id']}", headers=HEADERS, json=edit)
    assert response.status_code == 200 and response.json()['revision'] == 2

    # Over the encoded-size cap: refused with the documented message, nothing stored.
    response = post(client, sized(LIMITS['document_bytes'] + 1024))
    assert response.status_code == 422
    assert response.json()['error']['details'] == [
        {'field': 'document', 'message': 'document: diagram exceeds 512 KiB'}]
    assert client.get('/artifacts?kind=diagram', headers=HEADERS).json()['total'] == len(EXAMPLES)


def test_diagram_privacy_through_the_api(client):
    doc = example('architecture')
    doc['nodes'][0]['label'] = f'Owner: {NAME}'
    response = post(client, doc)
    assert response.status_code == 200, response.text
    node = response.json()['document']['nodes'][0]
    assert (node['id'], node['label']) == ('owner', 'Owner: [REDACTED]')

    doc = json.loads(EXAMPLES_JSON['sequence'].replace('"mcp"', f'"{SLUG}"'))
    response = post(client, doc)
    assert response.status_code == 422
    details = response.json()['error']['details']
    assert details[0] == {'field': 'document.participants.1.id',
                          'message': 'participants[1].id: ids must not contain personal names or addresses'}
    assert 'document.items.2.over.0' in {d['field'] for d in details}
    assert NAME not in response.text and SLUG not in json.dumps([d['message'] for d in details])
    assert client.get('/artifacts?kind=diagram', headers=HEADERS).json()['total'] == 1


def test_meta_and_provenance(client):
    sha = provenance_sha()
    doc = example('data-model')
    doc['tags'] = ['schema', 'q-core']
    doc['provenance'] = [{'repo': 'q-core', 'sha': sha, 'paths': ['db/schema.sql']}]
    response = post(client, doc)
    assert response.status_code == 200, response.text
    stored = client.get(f"/artifacts/{response.json()['id']}", headers=HEADERS).json()['document']
    assert stored['provenance'] == doc['provenance'] and stored['tags'] == doc['tags']

    doc['tags'] = ['schema', SLUG]
    response = post(client, doc)
    assert response.status_code == 422
    assert response.json()['error']['details'] == [
        {'field': 'document.tags.1', 'message': 'tags[1]: tags must not contain personal names or addresses'}]


def test_digit_runs_in_tags_and_paths_get_the_digit_reason(test_settings):
    # Tags and provenance paths are not digit-guarded by the model, so the screen
    # is what refuses them, and it must give the digit reason, not the names one.
    doc = example('flow')
    doc['tags'] = [f'build{RUN}', SLUG]
    doc['provenance'] = [{'repo': 'q-core', 'sha': 'ab' * 20, 'paths': [f'logs/run-{RUN}.txt']}]
    details = {d['field']: d['message'] for d in screened_errors(doc, test_settings)}
    digit = STORED_TEXT_REFUSAL
    assert details == {
        'document.tags.0': f'tags[0]: {digit}',
        'document.tags.1': 'tags[1]: tags must not contain personal names or addresses',
        'document.provenance.0.paths.0': f'provenance[0].paths[0]: {digit}',
    }
    assert RUN not in json.dumps(details)

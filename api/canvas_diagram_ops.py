"""Diagram editing operations (Canvas): small, meaningful edits in one batch.

A batch is all-or-nothing. Operations apply in order to a copy of revision
`expected_revision`; reference problems (an unknown id, a used id, a refusing
removal policy) stop the batch at that operation's index, and whole-document
rules (activation balance, group cycles, limits) are checked once on the
result by A7's DiagramDocument. Checking after every operation would refuse
legitimate batches whose intermediate states are invalid, such as an edge
added before the node it points at.

Messages name operation indexes and ids, never text: labels are free prose
and error bodies reach MCP transcripts.
"""
from __future__ import annotations

import copy
from typing import Annotated, Literal, Union

from pydantic import Field, StrictInt, ValidationError

from api.artifact_models import Provenance, Tag
from api.canvas_diagram import (
    Coord, DiagramDocument, Edge, Group, Id, Item, Label, Layout, Node, NodeRole, Note, Participant,
    ParticipantRole, Port, Text, Tone, screened,
)
from api.briefing_models import Model
from api.privacy import StoredText


class OpError(Exception):
    """One operation failed. status: 400 invalid_reference, 409 conflict, 422 validation_error."""

    def __init__(self, index: int, op: str, status: int, message: str, field: str | None = None):
        self.index, self.op, self.status, self.problem = index, op, status, message
        #: 422 only: the error's field, when it is not the operation itself.
        self.field = field
        super().__init__(message if field else f'operations[{index}] ({op}): {message}')


# --- Partial updates: `set` changes only the keys given, `clear` unsets optional fields ---

class NodeSet(Model):
    label: Label | None = None
    description: screened(500) | None = None
    role: NodeRole | None = None
    tone: Tone | None = None
    ports: list[Port] | None = None


class EdgeSet(Model):
    source: Id | None = None
    target: Id | None = None
    source_port: Id | None = None
    target_port: Id | None = None
    label: Label | None = None
    line: Literal['solid', 'dashed', 'dotted'] | None = None
    arrow: Literal['forward', 'backward', 'both', 'none'] | None = None
    cardinality: Literal['1:1', '1:n', 'n:1', 'n:m'] | None = None
    tone: Tone | None = None


class GroupSet(Model):
    label: Label | None = None
    style: Literal['boundary', 'cluster'] | None = None
    tone: Tone | None = None


class NoteSet(Model):
    """Graph notes take text and attach; sequence notes take text and over."""
    text: Text | None = None
    attach: Id | None = None
    over: list[Id] | None = Field(None, min_length=1, max_length=2)


class ParticipantSet(Model):
    label: Label | None = None
    role: ParticipantRole | None = None
    tone: Tone | None = None


class MessageSet(Model):
    source: Id | None = None
    target: Id | None = None
    label: screened(200) | None = None
    style: Literal['call', 'return', 'async'] | None = None
    activate: bool | None = None
    deactivate: bool | None = None


class FragmentSet(Model):
    operator: Literal['alt', 'opt', 'loop', 'par'] | None = None


class MetaSet(Model):
    """tags and provenance replace the whole list, as in edit_artifact."""
    title: Annotated[StoredText, Field(min_length=1, max_length=180)] | None = None
    description: screened(2000) | None = None
    tags: list[Tag] | None = Field(None, max_length=20)
    provenance: list[Provenance] | None = Field(None, max_length=20)


class Where(Model):
    """Where a participant or sequence item goes: at most one anchor.

    before/after name an item (or participant) and place next to it, in
    whatever list it lives in; fragment places at the start or end of that
    fragment's section; none appends to the top level.
    """
    before: Id | None = None
    after: Id | None = None
    fragment: Id | None = None
    section: int = Field(0, ge=0)
    at: Literal['start', 'end'] = 'end'


Dependents = Literal['cascade', 'refuse']


# --- The operations. Removal policies have no default (The owner, 2026-09-26),
# except remove_item's refuse, which only matters for a non-empty fragment. ---

class SetMeta(Model):
    op: Literal['set_meta']
    set: MetaSet = Field(default_factory=MetaSet)
    clear: list[Literal['description']] = []


class AddNode(Model):
    op: Literal['add_node']
    node: Node


class UpdateNode(Model):
    op: Literal['update_node']
    id: Id
    set: NodeSet = Field(default_factory=NodeSet)
    clear: list[Literal['description', 'role', 'tone']] = []


class RemoveNode(Model):
    op: Literal['remove_node']
    id: Id
    dependents: Dependents


class AddEdge(Model):
    op: Literal['add_edge']
    edge: Edge


class UpdateEdge(Model):
    op: Literal['update_edge']
    id: Id
    set: EdgeSet = Field(default_factory=EdgeSet)
    clear: list[Literal['source_port', 'target_port', 'label', 'cardinality', 'tone']] = []


class RemoveEdge(Model):
    op: Literal['remove_edge']
    id: Id


class AddGroup(Model):
    op: Literal['add_group']
    group: Group


class UpdateGroup(Model):
    op: Literal['update_group']
    id: Id
    set: GroupSet = Field(default_factory=GroupSet)
    clear: list[Literal['tone']] = []


class MoveToGroup(Model):
    op: Literal['move_to_group']
    ids: list[Id] = Field(min_length=1, max_length=100)
    group: Id | None  # null: the top level. Required, so a forgotten group is not a silent ungroup.


class RemoveGroup(Model):
    op: Literal['remove_group']
    id: Id
    members: Literal['ungroup', 'cascade', 'refuse']


class AddNote(Model):
    op: Literal['add_note']
    note: Note


class UpdateNote(Model):
    op: Literal['update_note']
    id: Id
    set: NoteSet = Field(default_factory=NoteSet)
    clear: list[Literal['attach', 'position']] = []


class RemoveNote(Model):
    op: Literal['remove_note']
    id: Id


class SetLayout(Model):
    op: Literal['set_layout']
    layout: Layout


class Pin(Model):
    op: Literal['pin']
    id: Id
    x: Coord
    y: Coord


class Unpin(Model):
    op: Literal['unpin']
    id: Id


class AddParticipant(Model):
    op: Literal['add_participant']
    participant: Participant
    where: Where = Field(default_factory=Where)


class UpdateParticipant(Model):
    op: Literal['update_participant']
    id: Id
    set: ParticipantSet = Field(default_factory=ParticipantSet)
    clear: list[Literal['tone']] = []


class MoveParticipant(Model):
    op: Literal['move_participant']
    id: Id
    where: Where


class RemoveParticipant(Model):
    op: Literal['remove_participant']
    id: Id
    dependents: Dependents


class InsertItem(Model):
    op: Literal['insert_item']
    item: Item
    where: Where = Field(default_factory=Where)


class MoveItem(Model):
    op: Literal['move_item']
    id: Id
    where: Where


class RemoveItem(Model):
    op: Literal['remove_item']
    id: Id
    contents: Literal['refuse', 'unwrap', 'cascade'] = 'refuse'


class UpdateMessage(Model):
    op: Literal['update_message']
    id: Id
    set: MessageSet = Field(default_factory=MessageSet)


class UpdateFragment(Model):
    op: Literal['update_fragment']
    id: Id
    set: FragmentSet = Field(default_factory=FragmentSet)


class AddSection(Model):
    op: Literal['add_section']
    fragment: Id
    guard: Label | None = None
    index: int | None = Field(None, ge=0)


class UpdateSection(Model):
    op: Literal['update_section']
    fragment: Id
    index: int = Field(ge=0)
    guard: Label | None = None
    clear: list[Literal['guard']] = []


class RemoveSection(Model):
    op: Literal['remove_section']
    fragment: Id
    index: int = Field(ge=0)
    contents: Literal['refuse', 'cascade']


Operation = Annotated[Union[
    SetMeta, UpdateNote,
    AddNode, UpdateNode, RemoveNode, AddEdge, UpdateEdge, RemoveEdge, AddGroup, UpdateGroup, MoveToGroup,
    RemoveGroup, AddNote, RemoveNote, SetLayout, Pin, Unpin,
    AddParticipant, UpdateParticipant, MoveParticipant, RemoveParticipant, InsertItem, MoveItem, RemoveItem,
    UpdateMessage, UpdateFragment, AddSection, UpdateSection, RemoveSection,
], Field(discriminator='op')]

#: Which diagram types each operation works on. update_note is shared: graph
#: notes take text/attach, sequence notes text/over.
COMMON_OPS = ('set_meta', 'update_note')
GRAPH_OPS = ('add_node', 'update_node', 'remove_node', 'add_edge', 'update_edge', 'remove_edge', 'add_group',
             'update_group', 'move_to_group', 'remove_group', 'add_note', 'remove_note', 'set_layout', 'pin', 'unpin')
SEQUENCE_OPS = ('add_participant', 'update_participant', 'move_participant', 'remove_participant', 'insert_item',
                'move_item', 'remove_item', 'update_message', 'update_fragment', 'add_section', 'update_section',
                'remove_section')


class EditDiagram(Model):
    expected_revision: StrictInt = Field(ge=1)
    operations: list[Operation] = Field(min_length=1, max_length=200)
    actor: StoredText = Field(min_length=1, max_length=100)
    note: StoredText = Field(min_length=1, max_length=700)


GRAPH_LISTS = ('nodes', 'edges', 'groups', 'notes')
ID_LISTS = ('nodes', 'edges', 'groups', 'notes', 'participants')
MAX_LISTED = 20


def _a(diagram_type: str) -> str:
    return f"{'an' if diagram_type[0] in 'aeiou' else 'a'} {diagram_type} diagram"


def _listed(paths: list[str]) -> str:
    """Dependents by JSON path in the edited document, never by id."""
    shown = ', '.join(paths[:MAX_LISTED])
    return shown + (f' (+{len(paths) - MAX_LISTED} more)' if len(paths) > MAX_LISTED else '')


def _item_paths(items: list, prefix: str = 'items'):
    """Every sequence item depth-first with its JSON path: (path, item)."""
    for k, item in enumerate(items):
        path = f'{prefix}[{k}]'
        yield path, item
        for j, section in enumerate(item.get('sections', [])):
            yield from _item_paths(section['items'], f'{path}.sections[{j}].items')


def _items(items: list, parent: str | None = None, section: int | None = None):
    """Every sequence item depth-first: (containing list, index, item, parent fragment id, section)."""
    for k, item in enumerate(items):
        yield items, k, item, parent, section
        for j, sec in enumerate(item.get('sections', [])):
            yield from _items(sec['items'], item['id'], j)


def _subtree_ids(item: dict) -> set[str]:
    return {x['id'] for _, _, x, _, _ in _items([item])}


def _patch(obj: dict, updates, clear: list[str]) -> None:
    for key, value in updates.model_dump(mode='json', exclude_unset=True).items():
        obj[key] = value
    for key in clear:
        obj.pop(key, None)


class _Batch:
    """One batch against one document copy. Every failure names operation i."""

    def __init__(self, document: dict):
        self.d = copy.deepcopy(document)
        for key in (*ID_LISTS, 'items'):
            self.d.setdefault(key, [])
        self.type = self.d['type']
        self.i, self.op = 0, ''

    def fail(self, status: int, message: str):
        raise OpError(self.i, self.op, status, message)

    # --- lookups ---

    def used_ids(self) -> set[str]:
        return ({x['id'] for key in ID_LISTS for x in self.d[key]}
                | {x['id'] for _, _, x, _, _ in _items(self.d['items'])})

    # No message here quotes an id. Nothing in a batch is name-screened until
    # screen_diagram runs on the result, and an id can come from the request
    # directly or from an earlier op in the same batch, so even an id that
    # matches may be unscreened. Messages name the op's field or a JSON path
    # (ticket T-04).
    def index(self, key: str, id_: str, what: str | None = None, field: str = 'id') -> int:
        for k, obj in enumerate(self.d[key]):
            if obj['id'] == id_:
                return k
        self.fail(400, f'unknown {what or key[:-1]} at {field}')

    def find(self, key: str, id_: str) -> dict | None:
        return next((obj for obj in self.d[key] if obj['id'] == id_), None)

    def locate(self, id_: str, kind: str | None = None, what: str = 'item', field: str = 'id'):
        for lst, k, item, _, _ in _items(self.d['items']):
            if item['id'] == id_ and (kind is None or item['kind'] == kind):
                return lst, k
        self.fail(400, f'unknown {what} at {field}')

    def new(self, model, field: str, extra_ids: set[str] = frozenset()) -> dict:
        value = model.model_dump(mode='json', exclude_none=True)
        taken = self.used_ids()
        if value['id'] in taken:
            self.fail(409, f'{field}.id is already used')
        if extra_ids & taken:
            self.fail(409, f'an id inside {field} is already used')
        return value

    # --- placement (sequence) ---

    def anchor(self, where: Where) -> str | None:
        given = [a for a in (where.before, where.after, where.fragment) if a is not None]
        if len(given) > 1:
            self.fail(422, 'where names more than one anchor; pass one of before, after or fragment')
        return given[0] if given else None

    def place_participant(self, participant: dict, where: Where) -> None:
        self.anchor(where)
        if where.fragment is not None:
            self.fail(422, 'participants are placed with before or after, not fragment')
        ref = where.before or where.after
        if ref is None:
            self.d['participants'].append(participant)
            return
        k = self.index('participants', ref, 'participant', 'where.before' if where.before else 'where.after')
        self.d['participants'].insert(k if where.before else k + 1, participant)

    def place_item(self, item: dict, where: Where) -> None:
        self.anchor(where)
        if where.fragment is not None:
            lst, k = self.locate(where.fragment, 'fragment', 'fragment', 'where.fragment')
            sections = lst[k]['sections']
            if where.section >= len(sections):
                self.fail(400, f'the fragment at where.fragment has no section {where.section}')
            target = sections[where.section]['items']
            target.insert(0 if where.at == 'start' else len(target), item)
            return
        ref = where.before or where.after
        if ref is None:
            self.d['items'].append(item)
            return
        lst, k = self.locate(ref, what='position reference', field='where.before' if where.before else 'where.after')
        lst.insert(k if where.before else k + 1, item)

    # --- operations ---

    def run(self, operations: list) -> dict:
        for self.i, operation in enumerate(operations):
            self.op = operation.op
            sequence = self.type == 'sequence'
            if (self.op in GRAPH_OPS and sequence) or (self.op in SEQUENCE_OPS and not sequence):
                self.fail(422, f'not available for {_a(self.type)}')
            getattr(self, self.op)(operation)
        return self.d

    def set_meta(self, o: SetMeta) -> None:
        _patch(self.d, o.set, o.clear)

    def add_node(self, o: AddNode) -> None:
        self.d['nodes'].append(self.new(o.node, 'node'))

    def update_node(self, o: UpdateNode) -> None:
        _patch(self.d['nodes'][self.index('nodes', o.id)], o.set, o.clear)

    def remove_node(self, o: RemoveNode) -> None:
        k = self.index('nodes', o.id)
        edges = [e['id'] for e in self.d['edges'] if o.id in (e['source'], e['target'])]
        notes = [n['id'] for n in self.d['notes'] if n.get('attach') == o.id]
        if (edges or notes) and o.dependents == 'refuse':
            paths = ([f'edges[{i}]' for i, e in enumerate(self.d['edges']) if e['id'] in edges]
                     + [f'notes[{i}]' for i, n in enumerate(self.d['notes']) if n['id'] in notes])
            self.fail(409, f"{len(edges)} edge(s) and {len(notes)} note(s) depend on the node at id: "
                           f"{_listed(paths)}; remove them first or pass dependents='cascade'")
        self.d['edges'] = [e for e in self.d['edges'] if e['id'] not in edges]
        self.d['notes'] = [n for n in self.d['notes'] if n['id'] not in notes]
        del self.d['nodes'][k]

    def add_edge(self, o: AddEdge) -> None:
        self.d['edges'].append(self.new(o.edge, 'edge'))

    def update_edge(self, o: UpdateEdge) -> None:
        _patch(self.d['edges'][self.index('edges', o.id)], o.set, o.clear)

    def remove_edge(self, o: RemoveEdge) -> None:
        del self.d['edges'][self.index('edges', o.id)]

    def add_group(self, o: AddGroup) -> None:
        self.d['groups'].append(self.new(o.group, 'group'))

    def update_group(self, o: UpdateGroup) -> None:
        _patch(self.d['groups'][self.index('groups', o.id)], o.set, o.clear)

    def move_to_group(self, o: MoveToGroup) -> None:
        if o.group is not None:
            self.index('groups', o.group, field='group')
        for j, id_ in enumerate(o.ids):
            node = self.find('nodes', id_)
            item, field = (node, 'group') if node else (self.find('groups', id_), 'parent')
            if item is None:
                self.fail(400, f'unknown node or group at ids[{j}]')
            if o.group is None:
                item.pop(field, None)
            else:
                item[field] = o.group
            # A pin is relative to the parent, so a move invalidates it.
            item.pop('position', None)

    def remove_group(self, o: RemoveGroup) -> None:
        group = self.d['groups'][self.index('groups', o.id)]
        nodes = [n for n in self.d['nodes'] if n.get('group') == o.id]
        groups = [g for g in self.d['groups'] if g.get('parent') == o.id]
        if (nodes or groups) and o.members == 'refuse':
            paths = ([f'nodes[{i}]' for i, n in enumerate(self.d['nodes']) if n.get('group') == o.id]
                     + [f'groups[{i}]' for i, g in enumerate(self.d['groups']) if g.get('parent') == o.id])
            self.fail(409, f"the group at id holds {len(nodes)} node(s) and {len(groups)} group(s): "
                           f"{_listed(paths)}; pass members='ungroup' or 'cascade'")
        gone = {o.id}
        if o.members == 'ungroup':
            # Children move to the removed group's parent, unpinned.
            for child, field in [(n, 'group') for n in nodes] + [(g, 'parent') for g in groups]:
                child.pop('position', None)
                if group.get('parent'):
                    child[field] = group['parent']
                else:
                    child.pop(field, None)
        elif o.members == 'cascade':
            frontier = [o.id]
            while frontier:
                current = frontier.pop()
                for g in self.d['groups']:
                    if g.get('parent') == current and g['id'] not in gone:
                        gone.add(g['id'])
                        frontier.append(g['id'])
            gone |= {n['id'] for n in self.d['nodes'] if n.get('group') in gone}
        # Edges and notes on anything removed, the group itself included, go with it.
        self.d['nodes'] = [n for n in self.d['nodes'] if n['id'] not in gone]
        self.d['groups'] = [g for g in self.d['groups'] if g['id'] not in gone]
        self.d['edges'] = [e for e in self.d['edges'] if e['source'] not in gone and e['target'] not in gone]
        self.d['notes'] = [n for n in self.d['notes'] if n.get('attach') not in gone]

    def add_note(self, o: AddNote) -> None:
        self.d['notes'].append(self.new(o.note, 'note'))

    def update_note(self, o: UpdateNote) -> None:
        given = o.set.model_fields_set
        if self.type == 'sequence':
            if 'attach' in given or o.clear:
                self.fail(422, 'sequence notes use over; attach and position are for graph notes')
            lst, k = self.locate(o.id, 'note', 'note')
            _patch(lst[k], o.set, [])
        else:
            if 'over' in given:
                self.fail(422, 'graph notes use attach; over is for sequence notes')
            _patch(self.d['notes'][self.index('notes', o.id)], o.set, o.clear)

    def remove_note(self, o: RemoveNote) -> None:
        del self.d['notes'][self.index('notes', o.id)]

    def set_layout(self, o: SetLayout) -> None:
        self.d['layout'] = o.layout.model_dump(mode='json', exclude_none=True)

    def _pinnable(self, id_: str) -> dict:
        item = self.find('nodes', id_) or self.find('notes', id_)
        if item is None:
            self.fail(400, 'unknown node or note at id')
        return item

    def pin(self, o: Pin) -> None:
        item = self._pinnable(o.id)
        if item.get('attach'):
            self.fail(409, 'the note at id is attached, so its target places it; clear attach first')
        item['position'] = {'x': o.x, 'y': o.y}

    def unpin(self, o: Unpin) -> None:
        self._pinnable(o.id).pop('position', None)

    def add_participant(self, o: AddParticipant) -> None:
        self.place_participant(self.new(o.participant, 'participant'), o.where)

    def update_participant(self, o: UpdateParticipant) -> None:
        _patch(self.d['participants'][self.index('participants', o.id)], o.set, o.clear)

    def move_participant(self, o: MoveParticipant) -> None:
        if o.where.before is None and o.where.after is None:
            self.fail(422, 'move_participant needs where.before or where.after')
        k = self.index('participants', o.id)
        if o.id in (o.where.before, o.where.after):
            self.fail(409, 'cannot place the participant at id relative to itself')
        participant = self.d['participants'].pop(k)
        self.place_participant(participant, o.where)

    def remove_participant(self, o: RemoveParticipant) -> None:
        k = self.index('participants', o.id)
        involved = [item['id'] for _, _, item, _, _ in _items(self.d['items'])
                    if (item['kind'] == 'message' and o.id in (item['source'], item['target']))
                    or (item['kind'] == 'note' and o.id in item['over'])]
        if involved and o.dependents == 'refuse':
            paths = [path for path, item in _item_paths(self.d['items']) if item['id'] in involved]
            self.fail(409, f"{len(involved)} message(s) or note(s) involve the participant at id: {_listed(paths)}; "
                           "remove them first or pass dependents='cascade'")
        for id_ in involved:
            lst, j = self.locate(id_)
            item = lst[j]
            if item['kind'] == 'note' and len(set(item['over'])) == 2:
                item['over'] = [p for p in item['over'] if p != o.id]
            else:
                del lst[j]
        del self.d['participants'][k]

    def insert_item(self, o: InsertItem) -> None:
        value = o.item.model_dump(mode='json', exclude_none=True)
        self.place_item(self.new(o.item, 'item', _subtree_ids(value) - {value['id']}), o.where)

    def move_item(self, o: MoveItem) -> None:
        lst, k = self.locate(o.id)
        inside = _subtree_ids(lst[k])
        target = self.anchor(o.where)
        if target in inside:
            self.fail(409, 'cannot move the item at id into or relative to itself')
        item = lst.pop(k)
        self.place_item(item, o.where)

    def remove_item(self, o: RemoveItem) -> None:
        lst, k = self.locate(o.id)
        item = lst[k]
        inner = [x for section in item.get('sections', []) for x in section['items']]
        if inner and o.contents == 'refuse':
            self.fail(409, f"the fragment at id holds {len(inner)} item(s); pass contents='unwrap' or 'cascade'")
        lst[k:k + 1] = inner if o.contents == 'unwrap' else []

    def update_message(self, o: UpdateMessage) -> None:
        lst, k = self.locate(o.id, 'message', 'message')
        _patch(lst[k], o.set, [])

    def _fragment(self, id_: str, field: str = 'id') -> dict:
        lst, k = self.locate(id_, 'fragment', 'fragment', field)
        return lst[k]

    def _section(self, fragment: dict, index: int) -> None:
        if index >= len(fragment['sections']):
            self.fail(400, f'the fragment at fragment has no section {index}')

    def update_fragment(self, o: UpdateFragment) -> None:
        _patch(self._fragment(o.id), o.set, [])

    def add_section(self, o: AddSection) -> None:
        sections = self._fragment(o.fragment, 'fragment')['sections']
        index = len(sections) if o.index is None else o.index
        if index > len(sections):
            self.fail(400, f'the fragment at fragment has no section {index}')
        sections.insert(index, {'items': [], **({'guard': o.guard} if o.guard is not None else {})})

    def update_section(self, o: UpdateSection) -> None:
        fragment = self._fragment(o.fragment, 'fragment')
        self._section(fragment, o.index)
        section = fragment['sections'][o.index]
        if o.guard is not None:
            section['guard'] = o.guard
        if 'guard' in o.clear:
            section.pop('guard', None)

    def remove_section(self, o: RemoveSection) -> None:
        fragment = self._fragment(o.fragment, 'fragment')
        self._section(fragment, o.index)
        sections = fragment['sections']
        if len(sections) == 1:
            self.fail(409, 'the fragment at fragment keeps at least one section; use remove_item')
        if sections[o.index]['items'] and o.contents == 'refuse':
            self.fail(409, f"section {o.index} of the fragment at fragment holds {len(sections[o.index]['items'])} "
                           "item(s); pass contents='cascade'")
        del sections[o.index]


def _path(loc: tuple) -> str:
    return ''.join(f'[{part}]' if isinstance(part, int) else f'.{part}' for part in loc).lstrip('.')


def apply_ops(document: dict, operations: list) -> dict:
    """Apply a batch to a copy of `document`; the input is never modified.

    Raises OpError at the first failing operation, or with field='document'
    when the result breaks a whole-document rule.
    """
    batch = _Batch(document)
    result = batch.run(operations)
    try:
        return DiagramDocument.model_validate(result).model_dump(mode='json', exclude_none=True)
    except ValidationError as error:
        first = error.errors(include_url=False, include_input=False, include_context=False)[0]
        where = _path(tuple(first['loc']))
        problem = first['msg'] if not where else f"{where}: {first['msg']}"
        raise OpError(len(operations) - 1, 'result', 422,
                      f'the edited diagram is invalid: {problem}', field='document') from None


# --- Summary view: enough to plan edits, without prose ---

def _optional(obj: dict, *keys: str) -> dict:
    return {key: obj[key] for key in keys if key in obj}


def _outline(items: list, depth: int = 0, parent: str | None = None, section: int | None = None) -> list[dict]:
    rows = []
    for item in items:
        row = {'id': item['id'], 'kind': item['kind'], 'depth': depth}
        if parent is not None:
            row.update(parent=parent, section=section)
        if item['kind'] == 'message':
            row.update(source=item['source'], target=item['target'], label=item['label'])
        elif item['kind'] == 'fragment':
            row['operator'] = item['operator']
        rows.append(row)
        for j, sec in enumerate(item.get('sections', [])):
            rows.extend(_outline(sec['items'], depth + 1, item['id'], j))
    return rows


def diagram_summary(document: dict) -> dict:
    """ids, labels and structure; no descriptions, note text, ports or styles."""
    walked = list(_items(document.get('items', [])))
    kinds = [item['kind'] for _, _, item, _, _ in walked]
    summary = {
        'type': document['type'], 'title': document['title'],
        'counts': {
            **{key: len(document.get(key, [])) for key in (*GRAPH_LISTS, 'participants')},
            'items': len(walked), 'messages': kinds.count('message'), 'fragments': kinds.count('fragment'),
        },
    }
    if document['type'] == 'sequence':
        summary['participants'] = [{'id': p['id'], 'label': p['label']} for p in document.get('participants', [])]
        summary['outline'] = _outline(document.get('items', []))
    else:
        summary['nodes'] = [{'id': n['id'], 'label': n['label'], **_optional(n, 'role', 'group'),
                             'pinned': 'position' in n} for n in document.get('nodes', [])]
        summary['edges'] = [{'id': e['id'], 'source': e['source'], 'target': e['target'], **_optional(e, 'label')}
                            for e in document.get('edges', [])]
        summary['groups'] = [{'id': g['id'], 'label': g['label'], **_optional(g, 'parent')}
                             for g in document.get('groups', [])]
        summary['notes'] = [{'id': n['id'], **_optional(n, 'attach')} for n in document.get('notes', [])]
    return summary

"""canvas.diagram/v1: the semantic diagram document Claude edits over MCP.

The document is ids, labels and relations; coordinates are optional hints.
It is the artifact's stored document for kind 'diagram' (no envelope), and
the only rule set: renderers and the JSON Schema in docs/ take it as given.
docs/canvas-diagram-format.md is the prose specification.

Every error names paths and ids, never label text: ids are pattern-restricted
and number-screened, labels are not, and 422 bodies reach MCP transcripts.
"""
from __future__ import annotations

import json
from typing import Annotated, Any, Iterator, Literal, Union

from fastapi.exceptions import RequestValidationError
from pydantic import AfterValidator, Field, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from api.artifact_models import ArtifactMeta
from api.briefing_content import clean_text
from api.briefing_models import Model
from api.personal_redaction import PersonalRedactionError, load_profile
from api.privacy import STORED_TEXT_REFUSAL, refuse_sensitive_numbers


LIMITS = {
    'nodes': 300, 'edges': 600, 'groups': 100, 'notes': 100, 'ports': 60,
    'group_depth': 6,
    'participants': 40, 'items': 500, 'messages': 400, 'fragments': 60, 'fragment_depth': 4,
    'sections': 6,
    'document_bytes': 512 * 1024,
}




def screened(max_length: int, **constraints):
    """Stored text: string limits first (so they reach the JSON Schema), then the digit guard."""
    return Annotated[str, Field(min_length=1, max_length=max_length, **constraints),
                     AfterValidator(refuse_sensitive_numbers)]


Id = screened(48, pattern=r'^[a-z][a-z0-9_-]{0,47}$')
Label = screened(120)
Text = screened(1000)
Coord = Annotated[float, Field(ge=-100000, le=100000, allow_inf_nan=False)]

DiagramType = Literal['flow', 'architecture', 'data-model', 'sequence']
Tone = Literal['neutral', 'accent', 'info', 'success', 'warning', 'danger', 'muted']
ROLES_BY_TYPE = {
    'architecture': ('person', 'system', 'external', 'container', 'service', 'database',
                     'queue', 'cache', 'storage', 'gateway', 'client', 'job'),
    'flow': ('start', 'end', 'step', 'decision', 'io', 'subprocess', 'state', 'person', 'system', 'external'),
    'data-model': ('table', 'view', 'enum'),
}
# A node without a role is drawn as its type's default; the renderer applies it.
DEFAULT_ROLE = {'architecture': 'service', 'flow': 'step', 'data-model': 'table'}
NodeRole = Literal[tuple(dict.fromkeys(r for roles in ROLES_BY_TYPE.values() for r in roles))]
ParticipantRole = Literal['actor', 'client', 'service', 'database', 'queue', 'external']


class Position(Model):
    """A pinned hint, relative to the parent group's top-left (or the origin)."""
    x: Coord
    y: Coord


class Port(Model):
    id: Id
    label: Label
    detail: screened(60) | None = None
    key: Literal['pk', 'fk', 'pk-fk'] | None = None


class Node(Model):
    id: Id
    label: Label
    description: screened(500) | None = None
    role: NodeRole | None = None
    tone: Tone | None = None
    group: Id | None = None
    ports: list[Port] = Field(default_factory=list, max_length=LIMITS['ports'])
    position: Position | None = None


class Edge(Model):
    id: Id
    source: Id
    target: Id
    source_port: Id | None = None
    target_port: Id | None = None
    label: Label | None = None
    line: Literal['solid', 'dashed', 'dotted'] = 'solid'
    arrow: Literal['forward', 'backward', 'both', 'none'] = 'forward'
    cardinality: Literal['1:1', '1:n', 'n:1', 'n:m'] | None = None
    tone: Tone | None = None


class Group(Model):
    id: Id
    label: Label
    parent: Id | None = None
    style: Literal['boundary', 'cluster'] = 'cluster'
    tone: Tone | None = None


class Note(Model):
    id: Id
    text: Text
    attach: Id | None = None
    position: Position | None = None


class Participant(Model):
    id: Id
    label: Label
    role: ParticipantRole = 'service'
    tone: Tone | None = None


class Message(Model):
    kind: Literal['message']
    id: Id
    source: Id
    target: Id
    label: screened(200)
    style: Literal['call', 'return', 'async'] = 'call'
    activate: bool = False     # pushes an activation onto the target
    deactivate: bool = False   # pops the source's activation; checked before activate


class SequenceNote(Model):
    kind: Literal['note']
    id: Id
    over: list[Id] = Field(min_length=1, max_length=2)
    text: Text


class Section(Model):
    guard: Label | None = None
    items: list[Item] = Field(default_factory=list, max_length=LIMITS['items'])


class Fragment(Model):
    kind: Literal['fragment']
    id: Id
    operator: Literal['alt', 'opt', 'loop', 'par']
    sections: list[Section] = Field(min_length=1, max_length=LIMITS['sections'])


Item = Annotated[Union[Message, SequenceNote, Fragment], Field(discriminator='kind')]
Section.model_rebuild()


class Layout(Model):
    direction: Literal['LR', 'RL', 'TB', 'BT'] | None = None
    spacing: Literal['compact', 'normal', 'roomy'] = 'normal'


class DiagramDocument(ArtifactMeta):
    format: Literal['canvas.diagram/v1'] = 'canvas.diagram/v1'
    type: DiagramType
    description: screened(2000) | None = None
    layout: Layout = Field(default_factory=Layout)
    nodes: list[Node] = Field(default_factory=list, max_length=LIMITS['nodes'])
    edges: list[Edge] = Field(default_factory=list, max_length=LIMITS['edges'])
    groups: list[Group] = Field(default_factory=list, max_length=LIMITS['groups'])
    notes: list[Note] = Field(default_factory=list, max_length=LIMITS['notes'])
    participants: list[Participant] = Field(default_factory=list, max_length=LIMITS['participants'])
    items: list[Item] = Field(default_factory=list, max_length=LIMITS['items'])

    @model_validator(mode='after')
    def consistent(self):
        errors = diagram_errors(self)
        if errors:
            summary = '; '.join(errors[:10]) + (f' (+{len(errors) - 10} more)' if len(errors) > 10 else '')
            # The message travels as context, so ids are never read as format fields.
            raise PydanticCustomError('diagram_invalid', '{summary}', {'summary': summary})
        return self


def walk_items(items, prefix: str = 'items', depth: int = 0) -> Iterator[tuple[str, Any, int]]:
    """Every sequence item, depth-first in document order: (path, item, fragment depth).

    A top-level fragment has depth 1; a message or note has the depth of the
    fragment it sits in (0 at the top level). This is the order the sequence renderer draws in.
    """
    for i, item in enumerate(items):
        path = f'{prefix}[{i}]'
        if item.kind == 'fragment':
            yield path, item, depth + 1
            for j, section in enumerate(item.sections):
                yield from walk_items(section.items, f'{path}.sections[{j}].items', depth + 1)
        else:
            yield path, item, depth


def diagram_errors(d: DiagramDocument) -> list[str]:
    """Every cross-field rule, as '<path>: <problem>'.

    Paths only, never an id or reference value: these messages are built
    before screen_diagram name-screens ids, so a name-shaped id would be
    echoed in a 422 (ticket T-04). A reference is named by where it sits.
    """
    errors = []
    sequence = d.type == 'sequence'
    a_type = f"{'an' if d.type[0] in 'aeiou' else 'a'} {d.type} diagram"

    # 1. Each type uses its own objects.
    for name in (('nodes', 'edges', 'groups', 'notes') if sequence else ('participants', 'items')):
        if getattr(d, name):
            errors.append(f'{name}: not allowed in {a_type}')
    if sequence and d.layout.direction:
        errors.append('layout.direction: not allowed in a sequence diagram')

    # 2. One id namespace for the whole document, nested items included.
    items = list(walk_items(d.items))
    seen = {}
    objects = [(f'{name}[{i}]', obj) for name in ('nodes', 'edges', 'groups', 'notes', 'participants')
               for i, obj in enumerate(getattr(d, name))]
    for path, obj in objects + [(path, item) for path, item, _ in items]:
        if obj.id in seen:
            errors.append(f'{path}.id: already used by {seen[obj.id]}')
        else:
            seen[obj.id] = path

    # 3. Roles per type; ports are data-model only and unique within their node.
    nodes = {n.id: n for n in d.nodes}
    groups = {g.id: g for g in d.groups}
    group_index = {g.id: i for i, g in enumerate(d.groups)}
    for i, node in enumerate(d.nodes):
        path = f'nodes[{i}]'
        if node.role and node.role not in ROLES_BY_TYPE.get(d.type, ()):
            errors.append(f'{path}.role: {node.role!r} not allowed in {a_type}')
        if node.ports and d.type != 'data-model':
            errors.append(f'{path}.ports: only data-model diagrams have ports')
        ports = set()
        for j, port in enumerate(node.ports):
            if port.id in ports:
                errors.append(f'{path}.ports[{j}].id: already used in {path}')
            ports.add(port.id)
        # 4. References resolve.
        if node.group and node.group not in groups:
            errors.append(f'{path}.group: unknown group')

    # 5. Group nesting: resolvable, acyclic and at most six deep.
    for i, group in enumerate(d.groups):
        if group.parent and group.parent not in groups:
            errors.append(f'groups[{i}].parent: unknown group')
            continue
        chain, parent = [group.id], group.parent
        while parent in groups:
            if parent in chain:
                errors.append(f'groups[{i}].parent: nesting cycle through groups[{group_index[parent]}]')
                break
            chain.append(parent)
            parent = groups[parent].parent
        else:
            if len(chain) > LIMITS['group_depth']:
                errors.append(f'groups[{i}].parent: groups nest deeper than {LIMITS["group_depth"]}')

    endpoints = nodes.keys() | groups.keys()
    for i, edge in enumerate(d.edges):
        path = f'edges[{i}]'
        for end, port in (('source', edge.source_port), ('target', edge.target_port)):
            ref = getattr(edge, end)
            if ref not in endpoints:
                errors.append(f'{path}.{end}: unknown node or group')
            elif port is not None and (ref not in nodes or port not in {p.id for p in nodes[ref].ports}):
                errors.append(f'{path}.{end}_port: the {end} has no such port')
        # 6. Cardinality is data-model only.
        if edge.cardinality and d.type != 'data-model':
            errors.append(f'{path}.cardinality: only data-model diagrams have cardinality')

    for i, note in enumerate(d.notes):
        path = f'notes[{i}]'
        if note.attach and note.attach not in endpoints:
            errors.append(f'{path}.attach: unknown node or group')
        if note.attach and note.position:
            errors.append(f'{path}.position: an attached note is placed by its target')

    # 7-9. Sequence references, activation balance and the item tree's limits.
    participants = {p.id for p in d.participants}
    active: dict[str, int] = {}
    messages = fragments = 0
    for path, item, depth in items:
        if item.kind == 'message':
            messages += 1
            for end in ('source', 'target'):
                if getattr(item, end) not in participants:
                    errors.append(f'{path}.{end}: unknown participant')
            if item.deactivate:
                if active.get(item.source):
                    active[item.source] -= 1
                else:
                    errors.append(f'{path}.deactivate: the source has no open activation')
            if item.activate:
                active[item.target] = active.get(item.target, 0) + 1
        elif item.kind == 'note':
            for j, ref in enumerate(item.over):
                if ref not in participants:
                    errors.append(f'{path}.over[{j}]: unknown participant')
        else:
            fragments += 1
            if item.operator in ('opt', 'loop') and len(item.sections) != 1:
                errors.append(f'{path}.sections: {item.operator} has exactly one section')
            if depth > LIMITS['fragment_depth']:
                errors.append(f'{path}: fragments nest deeper than {LIMITS["fragment_depth"]}')
    if len(items) > LIMITS['items']:
        errors.append(f'items: more than {LIMITS["items"]} items in total')
    if messages > LIMITS['messages']:
        errors.append(f'items: more than {LIMITS["messages"]} messages')
    if fragments > LIMITS['fragments']:
        errors.append(f'items: more than {LIMITS["fragments"]} fragments')
    return errors


# --- Privacy screen ---

TEXT_FIELDS = frozenset({'title', 'description', 'label', 'text', 'guard', 'detail'})
REFERENCE_FIELDS = frozenset({'id', 'group', 'parent', 'source', 'target', 'source_port', 'target_port', 'attach'})
OBJECT_LISTS = frozenset({'nodes', 'edges', 'groups', 'notes', 'participants', 'items', 'ports', 'sections'})
OBJECTS = frozenset({'layout', 'position'})
# Literals, booleans and numbers: already constrained by the model, stored as given.
TOKENS = frozenset({'format', 'type', 'role', 'tone', 'style', 'line', 'arrow', 'cardinality', 'key',
                    'kind', 'operator', 'direction', 'spacing', 'activate', 'deactivate', 'x', 'y'})
ID_REFUSAL = 'ids must not contain personal names or addresses'
TAG_REFUSAL = 'tags must not contain personal names or addresses'
PROVENANCE_REFUSAL = 'provenance must not contain personal names or addresses'
SIZE_REFUSAL = f'document: diagram exceeds {LIMITS["document_bytes"] // 1024} KiB'


def encoded_size(document: dict) -> int:
    """Bytes of the document as api.artifacts stores it (compact, sorted, UTF-8)."""
    return len(json.dumps(document, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode())


def _refuse(loc: tuple, message: str) -> dict:
    path = ''.join(f'[{part}]' if isinstance(part, int) else f'.{part}' for part in loc).lstrip('.')
    return {'type': 'value_error', 'loc': ('body', 'document', *loc), 'msg': f'{path}: {message}'}


def _check_size(document: dict) -> None:
    if encoded_size(document) > LIMITS['document_bytes']:
        raise RequestValidationError([{'type': 'value_error', 'loc': ('body', 'document'), 'msg': SIZE_REFUSAL}])


def _validated(document: dict) -> dict:
    try:
        return DiagramDocument.model_validate(document).model_dump(mode='json', exclude_none=True)
    except ValidationError as error:
        raise RequestValidationError([
            {'type': e['type'], 'loc': ('body', 'document', *e['loc']), 'msg': e['msg']}
            for e in error.errors(include_url=False, include_input=False, include_context=False)]) from None


def screen_diagram(document: dict, settings) -> dict:
    """Screen a diagram field by field, then validate it again.

    The generic screen_payload must never see a diagram: it rewrites every
    string, ids included, so a name-shaped id would be stored as [REDACTED]
    and break every reference to it. Here text is cleaned, while ids,
    references and tags are refused if cleaning would change them.
    Refusals name the JSON path, never the value.
    """
    # Imported here: api.artifact_kinds registers this kind by importing this module.
    from api.artifact_kinds import is_provenance_sha

    document = _validated(document)
    _check_size(document)
    profile = load_profile(settings.privacy_profile_path)
    refusals = []

    def unchanged(value, loc, message):
        try:
            if clean_text(value, profile) == value:
                return value
        except ValueError:
            # The digit guard: say so, rather than blaming a name.
            message = STORED_TEXT_REFUSAL
        except PersonalRedactionError:
            pass
        refusals.append(_refuse(loc, message))
        return value

    def walk(obj: dict, loc: tuple) -> dict:
        clean = {}
        for key, value in obj.items():
            here = (*loc, key)
            if key in TEXT_FIELDS:
                try:
                    clean[key] = clean_text(value, profile)
                except ValueError:
                    refusals.append(_refuse(here, STORED_TEXT_REFUSAL))
                except PersonalRedactionError as error:  # fixed messages only
                    refusals.append(_refuse(here, str(error)))
            elif key in REFERENCE_FIELDS:
                clean[key] = unchanged(value, here, ID_REFUSAL)
            elif key == 'over':
                clean[key] = [unchanged(v, (*here, i), ID_REFUSAL) for i, v in enumerate(value)]
            elif key in OBJECT_LISTS:
                clean[key] = [walk(v, (*here, i)) for i, v in enumerate(value)]
            elif key in OBJECTS:
                clean[key] = walk(value, here)
            elif key in TOKENS:
                clean[key] = value
            elif key == 'tags' and not loc:
                clean[key] = [unchanged(v, (*here, i), TAG_REFUSAL) for i, v in enumerate(value)]
            elif key == 'provenance' and not loc:
                clean[key] = [provenance(v, (*here, i)) for i, v in enumerate(value)]
            else:
                # A field the model gained without a screening rule: fail closed.
                raise RuntimeError(f'diagram field {key!r} has no privacy rule')
        return clean

    def provenance(entry: dict, loc: tuple) -> dict:
        i = loc[-1]
        sha = entry['sha']
        if not is_provenance_sha(f'document.provenance[{i}].sha', sha):
            sha = unchanged(sha, (*loc, 'sha'), STORED_TEXT_REFUSAL)
        # Stricter than refuse_sensitive_numbers alone: a repo or path that
        # the personal screen would change is refused, like an id.
        return {'repo': unchanged(entry['repo'], (*loc, 'repo'), PROVENANCE_REFUSAL), 'sha': sha,
                'paths': [unchanged(p, (*loc, 'paths', j), PROVENANCE_REFUSAL)
                          for j, p in enumerate(entry['paths'])]}

    walked = walk(document, ())
    if refusals:
        raise RequestValidationError(refusals)
    # Redaction can lengthen text past a limit; nothing is stored unvalidated.
    result = _validated(walked)
    _check_size(result)
    return result

# Canvas diagram format (`canvas.diagram/v1`)

A diagram is a Canvas artifact of kind `diagram`. Its stored document is a
semantic description: objects with stable ids, labels and relations. Claude
writes and edits it over MCP in small steps. Layout is computed by the
renderers (ELK for graph types; a sequence renderer for sequences), so
coordinates are optional hints.

- **Rules live in one place.** `api/canvas_diagram.py` validates every write,
  so renderers only ever receive valid documents. They still draw a visible
  error box for anything they cannot render.
- **JSON Schema.** `docs/canvas-diagram.schema.json` is generated from the
  models by `python scripts/canvas_diagram_schema.py --write`, for JS tests and
  the Canvas skill. It describes fields, types and limits. The cross-field
  rules below (references, uniqueness, activation balance) are enforced only by
  the API.

## Document

The document **is** the artifact's stored document: diagram fields sit at the
top level, beside the artifact metadata every Canvas kind shares.

| Field | Type | Default | Limit |
|---|---|---|---|
| `format` | `"canvas.diagram/v1"` | `"canvas.diagram/v1"` | |
| `type` | `flow`, `architecture`, `data-model` or `sequence` | required | |
| `title` | text; becomes the artifact's listed title | required | 1–180 |
| `tags` | slugs `^[a-z0-9][a-z0-9-]{0,31}$`, unique | `[]` | 20 |
| `provenance` | `[{repo, sha, paths}]`: where the diagram came from | `[]` | 20 |
| `description` | text | | 2000 |
| `layout` | `{direction: LR\|RL\|TB\|BT, spacing: compact\|normal\|roomy}` | spacing `normal` | |
| `nodes`, `edges`, `groups`, `notes` | graph objects | `[]` | 300, 600, 100, 100 |
| `participants`, `items` | sequence objects | `[]` | 40, 500 |

Graph types (`flow`, `architecture`, `data-model`) use nodes, edges, groups
and notes. A `sequence` diagram uses participants and items and has no
`layout.direction`. Mixing them is refused.

**Repository and commit belong in `provenance`**, never in labels or notes:
`{"repo": "q-core", "sha": "<40 hex from git rev-parse HEAD>", "paths": ["api/artifacts.py"]}`.
The full SHA is exempt from the digit screen there, and only there.

### Ids

Ids match `^[a-z][a-z0-9_-]{0,47}$`: stable, readable slugs chosen by Claude.
**Every object in a document shares one id namespace**: nodes, edges, groups,
notes, participants and every sequence item at any depth. Port ids are unique
within their node.

### Text and numbers

Every text field and every id is refused if it contains **seven or more
consecutive digits** (the account-number guard). Avoid epoch timestamps, long
version numbers and long numeric identifiers; `port 8080`, `2026-09-26` and a
short SHA pass.

## Graph objects

- **Node**: `id`, `label` (1–120), `description` (≤500), `role`, `tone`,
  `group` (a group id), `ports` (≤60, data-model only), `position {x, y}`.
- **Port**: `id`, `label` (1–120), `detail` (≤60, e.g. a column type), `key`
  (`pk`, `fk` or `pk-fk`).
- **Edge**: `id`; `source` and `target`, each a node **or group** id, with
  optional `source_port` / `target_port`; `label` (≤120); `line` (`solid`,
  `dashed`, `dotted`; default solid); `arrow` (`forward`, `backward`, `both`,
  `none`; default forward); `cardinality` (`1:1`, `1:n`, `n:1`, `n:m`;
  data-model only); `tone`.
- **Group**: `id`, `label`, `parent` (a group id), `style` (`boundary` or
  `cluster`, default cluster), `tone`. Groups nest at most 6 deep, without
  cycles.
- **Note**: `id`, `text` (1–1000), `attach` (a node or group id), `position`
  (unattached notes only; an attached note is placed by its target).

**Containment uses parent pointers** (`node.group`, `group.parent`), not
member lists, so every object has at most one container and moving it is one
field change.

**Roles** allowed per type (a node without a role is drawn as the default):

| Type | Roles | Default |
|---|---|---|
| architecture | person, system, external, container, service, database, queue, cache, storage, gateway, client, job | service |
| flow | start, end, step, decision, io, subprocess, state, person, system, external | step |
| data-model | table, view, enum | table |

### Positions are hints

`position` holds finite numbers with |v| ≤ 100000, relative to the top-left of
the parent group, or to the diagram origin for a top-level node. Under ELK's
layered layout a pin is **only a hint unless every node in that scope is
pinned**, in which case the layout honours it exactly. Don't expect exact
placement of one isolated pin. Sequence diagrams have no positions.

## Sequence objects

- **Participant**: `id`, `label`, `role` (`actor`, `client`, `service`,
  `database`, `queue`, `external`; default service), `tone`. Array order is
  left-to-right order.
- **`items`** is an ordered list in document order, each item tagged by `kind`:
  - **message**: `id`, `source`, `target` (participant ids), `label` (1–200),
    `style` (`call`, `return`, `async`; default call), `activate` and
    `deactivate` (default false). `source == target` is a self-call.
    `activate` pushes an activation onto the target; `deactivate` pops the
    source's activation, and is applied first.
  - **note**: `id`, `over` (1–2 participant ids), `text` (1–1000). One id
    sits over that lifeline; two span from the first to the second.
  - **fragment**: `id`, `operator` (`alt`, `opt`, `loop`, `par`), `sections`
    (`[{guard (≤120), items: [...]}]`). `opt` and `loop` have exactly one
    section; `alt` and `par` have 1–6. Sections nest items recursively.
    `break`, `critical` and `ref` are not part of v1.

Whether an item sits inside or outside a fragment is explicit in the tree.
The whole tree holds at most 500 items, 400 messages and 60 fragments, with
fragments nested at most 4 deep.

**Activation balance** is checked depth-first in document order, the order the
renderer draws in: a `deactivate` needs an open activation on its source.
Activations still open at the end are allowed and drawn faded. The branches
of an `alt` or `par` are walked one after another like everything else, so an
activation opened in one branch counts as open in the next. This is a known
limitation of v1.

## Style tokens

Diagrams use tokens, never CSS. `tone` is one of `neutral`, `accent`, `info`,
`success`, `warning`, `danger` or `muted`, and maps to `--canvas-<tone>` (stroke
and text) and `--canvas-<tone>-fill`. The theme is dark-only in v1; the
renderer derives the values from the shell tokens.

## Limits

Hard limits stop runaway documents; a readable diagram is far smaller (about
30 nodes). They live in `LIMITS` in `api/canvas_diagram.py`:
300 nodes, 600 edges, 100 groups, 100 notes, 60 ports per node; 40
participants, 500 items, 400 messages, 60 fragments nested at most 4 deep;
and **512 KiB per encoded document** (`document: diagram exceeds 512 KiB`).

## Errors

A refused document returns 422 `validation_error`. Every message has the form
`<path>: <problem>`, with paths like `items[4].sections[1].items[0]`. It names
**neither label text nor id values**: a reference is identified by where it
sits. Validation runs before ids are name-screened, so quoting an id could echo
a personal name. Examples:

- `nodes: not allowed in a sequence diagram`
- `items[4].sections[1].items[0].id: already used by items[0]`
- `edges[0].target_port: the target has no such port`
- `groups[1].parent: nesting cycle through groups[0]`
- `items[3].deactivate: the source has no open activation`

`edit_diagram`'s operation errors follow the same rule. Nothing in a batch is
name-screened until the whole result is, and an id can come from the request
or from an earlier operation in the same batch. So operation errors name the
operation's field and list dependents by path, for example
`operations[1] (remove_edge): unknown edge at id`,
`operations[1] (add_node): node.id is already used` and
`… depend on the node at id: edges[2], edges[3]`.

## Privacy

Text fields (`title`, `description`, `label`, `text`, `guard`, `detail`) pass
through the server-side personal-data screen, so a name from the private
profile is stored as `[REDACTED]`. Ids, references, tags and provenance repos
and paths are never rewritten: one the screen would change is refused
(`nodes[0].id: ids must not contain personal names or addresses`, with the
JSON path in `details[].field`). The screened document is validated again,
so text that grows under redaction past its limit is refused, never stored.

## Examples

Architecture:

```json
{"type": "architecture", "title": "q-core containers", "layout": {"direction": "LR"}, "groups": [{"id": "home-server", "label": "Home server (NixOS)", "style": "boundary"}], "nodes": [{"id": "owner", "label": "Owner", "role": "person"}, {"id": "claude", "label": "Claude Code", "role": "client", "description": "MCP client on each machine"}, {"id": "mcp", "label": "q_core_mcp", "role": "service", "group": "home-server"}, {"id": "api", "label": "FastAPI app", "role": "service", "group": "home-server", "tone": "accent"}, {"id": "db", "label": "SQLite", "role": "database", "group": "home-server"}], "edges": [{"id": "e1", "source": "owner", "target": "claude", "label": "chats"}, {"id": "e2", "source": "claude", "target": "mcp", "label": "MCP over Tailscale"}, {"id": "e3", "source": "mcp", "target": "api", "label": "HTTP"}, {"id": "e4", "source": "api", "target": "db", "label": "reads/writes"}], "notes": [{"id": "n1", "text": "Only the API touches the database.", "attach": "db"}]}
```

Flow:

```json
{"type": "flow", "title": "Statement intake", "layout": {"direction": "TB", "spacing": "compact"}, "nodes": [{"id": "start", "label": "Statement dropped", "role": "start"}, {"id": "scrub", "label": "Server-side scrub", "role": "step"}, {"id": "ok", "label": "Scrub clean?", "role": "decision"}, {"id": "preview", "label": "Preview import", "role": "step"}, {"id": "review", "label": "Local review", "role": "io", "tone": "warning"}, {"id": "done", "label": "Imported", "role": "end", "position": {"x": 400, "y": 520}}], "edges": [{"id": "f1", "source": "start", "target": "scrub"}, {"id": "f2", "source": "scrub", "target": "ok"}, {"id": "f3", "source": "ok", "target": "preview", "label": "yes"}, {"id": "f4", "source": "ok", "target": "review", "label": "no", "line": "dashed"}, {"id": "f5", "source": "preview", "target": "done"}]}
```

Data model:

```json
{"type": "data-model", "title": "Artifacts", "layout": {"direction": "LR"}, "nodes": [{"id": "artifacts", "label": "artifacts", "role": "table", "ports": [{"id": "id", "label": "id", "detail": "TEXT", "key": "pk"}, {"id": "kind", "label": "kind", "detail": "TEXT"}, {"id": "revision", "label": "revision", "detail": "INTEGER"}]}, {"id": "revisions", "label": "artifact_revisions", "role": "table", "ports": [{"id": "artifact-id", "label": "artifact_id", "detail": "TEXT", "key": "pk-fk"}, {"id": "revision", "label": "revision", "detail": "INTEGER", "key": "pk"}, {"id": "payload", "label": "payload", "detail": "TEXT"}]}], "edges": [{"id": "r1", "source": "revisions", "source_port": "artifact-id", "target": "artifacts", "target_port": "id", "cardinality": "n:1"}]}
```

Sequence:

```json
{"type": "sequence", "title": "Revise an artifact", "participants": [{"id": "claude", "label": "Claude", "role": "actor"}, {"id": "mcp", "label": "q_core_mcp"}, {"id": "api", "label": "API"}, {"id": "db", "label": "SQLite", "role": "database"}], "items": [{"kind": "message", "id": "m1", "source": "claude", "target": "mcp", "label": "revise_artifact(id, payload)", "activate": true}, {"kind": "message", "id": "m2", "source": "mcp", "target": "api", "label": "PUT /artifacts/{id}", "activate": true}, {"kind": "note", "id": "n1", "over": ["mcp", "api"], "text": "Retries return the same revision."}, {"kind": "message", "id": "m3", "source": "api", "target": "api", "label": "screen_payload"}, {"kind": "fragment", "id": "f1", "operator": "alt", "sections": [{"guard": "revision is current", "items": [{"kind": "message", "id": "m4", "source": "api", "target": "db", "label": "insert revision"}, {"kind": "message", "id": "m5", "source": "api", "target": "mcp", "label": "200 artifact", "style": "return", "deactivate": true}]}, {"guard": "stale revision", "items": [{"kind": "message", "id": "m6", "source": "api", "target": "mcp", "label": "409 conflict", "style": "return"}]}]}, {"kind": "message", "id": "m7", "source": "mcp", "target": "claude", "label": "result", "style": "return", "deactivate": true}]}
```

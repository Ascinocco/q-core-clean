# q-core: the system

What exists and how it fits together.

> **Two halves, written differently.** The architecture below is prose.
> The inventory at the end is **generated from the tree** by
> `scripts/system_inventory.py` and checked by `tests/test_system_doc.py`,
> so it cannot quietly disagree with the code. That split is deliberate:
> this repo has already watched a hand-maintained description go stale
> while everything stayed green (the MCP tool descriptions against
> `EntityType`, before #107), and prose is exactly the part that rots
> silently because nothing executes it.
>
> **If you added a route, tool, table, migration or skill, regenerate
> before opening your PR:** `python scripts/system_inventory.py --write`,
> and commit the result. `tests/test_system_doc.py` fails when the block
> and the tree disagree, so a surface change that skips this turns the
> composed suite red in someone else's PR rather than your own.


## The three layers

**`api/` owns the database. Nothing else touches it.** A FastAPI app over
stdlib `sqlite3`, no ORM (`decisions-log.md`, "API server: Python,
FastAPI, stdlib sqlite3, no ORM"). Every write, every constraint and every
refusal lives here, which is what makes the guarantees checkable in one
place: if a rule is not enforced in `api/`, it is not enforced.

**`q_core_mcp/` is a thin client of `api/`.** It has no database access
of its own and holds no business rules — a tool is a signature, a
description written for a model to read, and an HTTP call. It runs
**inside the API process**, mounted at `/mcp`, rather than as a separate
stdio server (`decisions-log.md`, "The MCP server runs inside the API
process, over HTTP, not as a separate stdio server"). Where tools carry
logic it is about the *surface*: refusing an immutable field by name
rather than dropping it silently, or turning `clear=["field"]` into the
explicit null the API understands, because a Python signature cannot
express the difference between an omitted argument and one passed as
`None`.

**The plugin's skills chain tool calls into procedures.** Each is a
`SKILL.md` under `plugin/skills/`, shipped with the runbooks it reads as the
q-core plugin, installed on every machine and kept current by its own
SessionStart hook (`runbooks/plugin.md`; `decisions-log.md`, "q-core
split", decision 2). Before the split they lived at
`.claude/skills/` and loaded only in this repository. They are instructions, not code: nothing executes
them, so their conventions are **copied verbatim** into each file and
pinned by `tests/test_skill_conventions.py` and
`tests/test_skill_vocabularies.py` rather than imported. A skill that
disagrees with `runbooks/skill-conventions.md` fails the suite by name.

## Runtime

One process, on one machine, for one person.

- **`launchd` agent** (`ops/launchd/tech.q-core.api.plist`) runs
  `python -m api.run` — not the uvicorn CLI, so the logging config has
  one definition rather than a second hand-maintained copy on disk.
- **Bound to `127.0.0.1:8420`,** plus, when configured, a private Unix
  socket (`serve_socket`, directory 0700) that Tailscale Serve publishes to
  the tailnet. Every request on either listener needs a credential: a token
  (a named, scoped client token or the service token), or through Serve an
  allowlisted Tailscale identity for the browser pages. The one exception is
  the Google OAuth callback on the service port, authenticated by its
  single-use state (`api/serve_gate.py`, `api/tokens.py`; `decisions-log.md`,
  "q-core split"). Nothing binds to the LAN. That entry
  supersedes the single-Mac "No authentication, bound to loopback only".
- **`data/` is gitignored in full** and holds everything that is not
  code: `q-core.db` (SQLite in WAL mode), `documents/` (plaintext on
  disk — `decisions-log.md`, "Document storage"), `jyra/` for ticket
  attachments, `attachments-inbox/` as the single permitted source root
  for `attach_file`, and `logs/` with one JSON object per line in
  `api.log` (`runbooks/observability.md`).
- **WAL mode means `data/q-core.db` cannot be backed up by plain file
  copy** — a copy without its `-wal` is a torn snapshot. Use SQLite's own
  `.backup` (`decisions-log.md` has the entry and the reason).

## Bootstrap and migrations

`init_db` runs on **every request** and classifies what is at the
database path: `missing`, `corrupt`, `incomplete`, or `ready`. Only
`missing` is ever written to, so a fresh install works and a damaged file
is never overwritten — the evidence survives and nothing silently
replaces a populated database. Ambiguity resolves to `ready`, never to a
writable state; a locked database is `ready`.

Migrations apply in exactly **one** place — `apply_startup_migrations`,
from the app's lifespan — and never per-request, so there is a single
path that can change schema. Each runs inside one `BEGIN IMMEDIATE`
transaction with its `schema_migrations` row, so a failed migration
leaves nothing behind and a half-applied state cannot exist. That
atomicity is what `init_db`'s stale-state re-read depends on, which is
why migration files must not manage transactions themselves.

Both the bootstrap and the migration path had races, found and closed
separately (`decisions-log.md`, "`init_db`'s bootstrap is now race-proof
by construction" and "The migration path had its own race"). `init_db`'s
per-request cost was measured rather than assumed and left alone — the
numbers are in `decisions-log.md`.

## The guard families that shape the code

These recur across the codebase. Each exists because something failed
quietly once.

**Source-reading scope guards.** Some rules cannot be expressed as a
test of behaviour, so a test reads the source instead: that no conflict
is raised as a bare 400, that every skill carrying a drain fence carries
*the* fence byte-for-byte, that no migration file contains transaction
control. They catch a class of mistake at the moment it is written rather
than when it eventually misfires.

**Derived, not declared.** Wherever a list in code has a true answer
somewhere else, the test derives it and compares. Every table with a
foreign key to `entities` must appear in the delete guard's registry
(`PRAGMA foreign_key_list`); every date column inside a `UNIQUE`
constraint must be guarded (parsed from `db/schema.sql`); every runbook
must be listed in `INDEX.md` (the directory); every skill's vocabulary
must equal the API's. **The rule is that a judgement can be wrong and a
constraint cannot** — and each such check asserts it found something,
because a derivation that finds nothing passes silently and looks exactly
like one that checked everything.

**`KeyDate`.** Pydantic accepts a datetime *string* for a `date` field
when the time is exactly midnight, keeping the calendar date and
discarding the offset. For a field that is part of a key that is a key
bug, not a formatting one: two callers naming the same instant in
different offsets write different rows. Key-family date fields are
annotated `KeyDate`, which refuses anything but a calendar date, and the
classification is asserted against the schema's `UNIQUE` constraints
rather than described in prose.

**`model_fields_set` and `clear`.** Across the codebase `None` used to
mean "not supplied", so a nullable field was write-once — you could set
it and never take it back. PATCH models now read `model_fields_set`, so
an explicit `null` clears and an absent key leaves alone, and clearing a
`NOT NULL` column is refused by name rather than reaching SQLite as a
500. The MCP layer says the same thing differently, with `clear=[...]`,
because **HTTP/JSON can express "supplied as null" and a Python signature
cannot.**

**Containment holes.** `attach_file` resolves paths and refuses anything
outside `attachment_roots`, plus a set of paths refused whatever that
setting says — the attachment store itself, the database, `.env`. The
holes are carved because a permissive root would otherwise swallow them:
`attachment_roots = "/"` once made every path legal.

**The content check.** Several guards require a field to be non-empty in
**content**, not merely present. A required `note` satisfied by `""`
writes a history row explaining nothing, which meets the rule and defeats
its purpose in the same call. The transition endpoint enforces this for
`note` and `actor`, which is what makes the ticket audit trail worth
reading.

## Inventory

Everything below is generated. See the note at the top of this document.

<!-- BEGIN GENERATED INVENTORY -->

> Generated by `scripts/system_inventory.py`. Do not edit by hand —
> `tests/test_system_doc.py` fails when this block and the tree
> disagree. Regenerate with `python scripts/system_inventory.py --write`.

### HTTP routes

**`api/artifact_links.py`**

- `GET /artifacts/{artifact_id}/links`
- `POST /artifacts/{artifact_id}/links`
- `DELETE /artifacts/{artifact_id}/links/{link_id}`

**`api/artifacts.py`**

- `GET /artifacts`
- `POST /artifacts`
- `GET /artifacts/{artifact_id}`
- `PATCH /artifacts/{artifact_id}`
- `PUT /artifacts/{artifact_id}`
- `PATCH /artifacts/{artifact_id}/diagram`

**`api/briefing_data.py`**

- `GET /briefing-window`
- `GET /expected-payments`
- `GET /reminder-calendar-links`
- `GET /reminder-completions`

**`api/briefing_viewer.py`**

- `GET /ui/briefs`
- `GET /ui/briefs/{artifact_id}`

**`api/documents.py`**

- `GET /documents`
- `POST /documents`
- `POST /documents/extract`
- `DELETE /documents/{document_id}`
- `GET /documents/{document_id}`
- `POST /documents/{document_id}/extract`

**`api/due.py`**

- `GET /due`

**`api/entities.py`**

- `GET /entities`
- `POST /entities`
- `DELETE /entities/{entity_id}`
- `GET /entities/{entity_id}`
- `PATCH /entities/{entity_id}`
- `GET /entities/{entity_id}/relationships`
- `POST /entities/{entity_id}/relationships`
- `DELETE /relationships/{relationship_id}`
- `GET /relationships/{relationship_id}`
- `PATCH /relationships/{relationship_id}`

**`api/evaluations.py`**

- `GET /evals/runs`
- `GET /ui/evals`

**`api/financial.py`**

- `GET /categories`
- `POST /categories`
- `DELETE /categories/{category_id}`
- `GET /categories/{category_id}`
- `PATCH /categories/{category_id}`
- `GET /entities/{entity_id}/cost_of_ownership`
- `GET /merchant_rules`
- `POST /merchant_rules`
- `POST /merchant_rules/reapply`
- `DELETE /merchant_rules/{rule_id}`
- `GET /merchant_rules/{rule_id}`
- `PATCH /merchant_rules/{rule_id}`
- `GET /merchant_rules/{rule_id}/history`
- `GET /spending_summary`
- `GET /statements`
- `POST /statements`
- `GET /statements/{statement_id}`
- `POST /statements/{statement_id}/archive`
- `POST /statements/{statement_id}/unarchive`
- `GET /transactions`
- `GET /transactions/{transaction_id}`
- `PATCH /transactions/{transaction_id}`
- `POST /transactions/{transaction_id}/apply_as_rule`
- `POST /transactions/{transaction_id}/archive`
- `POST /transactions/{transaction_id}/unarchive`
- `GET /trend`

**`api/financial_corrections.py`**

- `GET /financial-corrections/{correction_id}`
- `POST /statements/{statement_id}/correct-period`
- `POST /transactions/{transaction_id}/reassign-statement`

**`api/forecast.py`**

- `GET /forecast/plan`
- `PUT /forecast/plan`
- `GET /forecast/projection`
- `GET /ui/forecast`

**`api/google_calendar.py`**

- `POST /integrations/google/connect`
- `GET /integrations/google/status`
- `POST /integrations/google/sync`

**`api/jyra.py`**

- `DELETE /attachments/{attachment_id}`
- `GET /attachments/{attachment_id}/content`
- `GET /boards`
- `POST /boards`
- `DELETE /boards/{board_id}`
- `GET /boards/{board_id}`
- `PATCH /boards/{board_id}`
- `GET /tickets`
- `POST /tickets`
- `POST /tickets/claim`
- `DELETE /tickets/{ticket_id}`
- `GET /tickets/{ticket_id}`
- `PATCH /tickets/{ticket_id}`
- `GET /tickets/{ticket_id}/attachments`
- `POST /tickets/{ticket_id}/attachments`
- `POST /tickets/{ticket_id}/transition`
- `GET /tickets/{ticket_id}/transitions`

**`api/main.py`**

- `GET /`
- `GET /health`
- `GET /ui/assets/highcharts-accessibility.js`
- `GET /ui/assets/highcharts.js`
- `GET /ui/spending`

**`api/notes.py`**

- `GET /notes`
- `POST /notes`
- `DELETE /notes/{note_id}`
- `GET /notes/{note_id}`
- `PATCH /notes/{note_id}`
- `POST /notes/{note_id}/links`
- `DELETE /notes/{note_id}/links/{link_id}`

**`api/reminders.py`**

- `GET /reminders`
- `POST /reminders`
- `DELETE /reminders/{reminder_id}`
- `GET /reminders/{reminder_id}`
- `PATCH /reminders/{reminder_id}`
- `POST /reminders/{reminder_id}/complete`
- `POST /reminders/{reminder_id}/snooze`

**`api/review_inbox.py`**

- `GET /review-history`
- `GET /review-items`
- `POST /review-items`
- `GET /review-items/{item_id}`
- `PUT /review-items/{item_id}`

**`api/source_imports.py`**

- `POST /source_imports/commit`
- `POST /source_imports/preview`

**`api/spending.py`**

- `GET /spending/periods`
- `GET /statements/coverage`

**`api/token_ui.py`**

- `GET /ui/tokens`
- `POST /ui/tokens/create`
- `GET /ui/tokens/list`
- `POST /ui/tokens/revoke`

**`api/transcription.py`**

- `POST /transcriptions`

### MCP tools

| tool | purpose |
|---|---|
| `apply_transaction_as_rule` | Learn a categorization rule from one transaction you have already corrected, so matching transactions are cat… |
| `archive_statement` | Archive a whole imported statement and every transaction in it, for a mis-imported or duplicated statement. |
| `archive_transaction` | Archive ONE transaction, for a duplicate or a mis-parsed line, leaving the rest of its statement alone. |
| `attach_file` | Attach a local file to a ticket — a screenshot, a log, a sample export. |
| `claim_ticket` | Take the next ticket waiting for an agent. |
| `commit_source_import` | Commit the exact source payload inspected by preview_source_import, with its review_token. |
| `complete_reminder` | Mark ONE occurrence of a reminder done, so it stops appearing in list_due_items. |
| `connect_google_calendar` | Start the one-time Google Calendar OAuth connection. |
| `correct_statement_period` | Correct only an active statement period using source evidence. |
| `cost_of_ownership` | What one thing has cost: every transaction booked against this entity, plus those booked against any account… |
| `create_artifact` | Save an artifact. |
| `create_board` | Create a board on an entity. |
| `create_entity` | Create an entity. |
| `create_merchant_rule` | Create a merchant rule from a pattern you choose, so future imports whose description CONTAINS that pattern a… |
| `create_note` | Create a freeform note: a plan, a draft, a checklist, anything worth keeping. |
| `create_relationship` | Link two entities, e.g. a person owns a vehicle. |
| `create_reminder` | Create a reminder. |
| `create_review_item` | Persist a review discrepancy. |
| `create_ticket` | Create a ticket on a board. |
| `delete_attachment` | Delete one attachment and its file from disk. |
| `delete_board` | Delete a board permanently. |
| `delete_document` | Permanently delete a stored document: both its record and the file on disk. |
| `delete_entity` | Permanently delete an entity. |
| `delete_note` | Delete a note and all of its links. |
| `delete_relationship` | Permanently delete one relationship link between two entities. |
| `delete_reminder` | Delete a reminder and every record of its occurrences. |
| `delete_ticket` | Delete a ticket permanently. |
| `edit_artifact` | Targeted edit of a page or brief: payload expected_revision, replacements [{old,new}] applied in order, each… |
| `edit_diagram` | Apply an all-or-nothing batch of diagram operations as one new revision. |
| `extract_document_text` | Read a statement or receipt from intake/ or data/documents/ as text, with account, routing, card and SSN numb… |
| `extract_registered_document_text` | Extract and server-side scrub a document that is already registered, using its document id. |
| `get_artifact` | Read a saved artifact, or an earlier revision by number. |
| `get_board` | Get a board and what is on it, grouped into columns. |
| `get_briefing_window` | Capture one authoritative America/New_York as_of and daily or Monday-to-now weekly report window. |
| `get_cash_forecast` | Calculate a read-only cash/debt scenario from dated snapshots, not actual current balances. |
| `get_document` | Get one stored document's metadata by id. |
| `get_entity` | Get one entity (person, property, vehicle, pet, account, project) by id. |
| `get_financial_correction` | Read the immutable audit record of a financial metadata correction by its UUID. |
| `get_forecast_plan` | Read the financial forecast plan and revision. |
| `get_note` | Get one note by id with its FULL body, derived title, timestamps and links (each link has its own id, used by… |
| `get_relationship` | Get one relationship by id: the two entities, the type, its dates and attributes. |
| `get_reminder` | Get one reminder by id: its title, notes, linked entity, recurrence rule and dates. |
| `get_review_item` | Read a current review item and revision before discussing or changing it. |
| `get_ticket` | Get one ticket by id or key, with everything you need to work it in one call: its key, board, type, title, de… |
| `get_ticket_history` | Paginated: returned is this page, total is every matching row, and returned < total means you are holding a p… |
| `google_calendar_status` | Report whether the one-way Google Calendar reminder projection is connected and its last sync health. |
| `link_artifact` | Link an artifact to a Jyra ticket or an entity. |
| `link_note` | Link a note to something: target_type one of entity, transaction, document, reminder, statement, ticket, plus… |
| `list_artifact_links` | Paginated: returned is this page, total is every matching row, and returned < total means you are holding a p… |
| `list_artifacts` | Paginated: returned is this page, total is every matching row, and returned < total means you are holding a p… |
| `list_attachments` | Paginated: returned is this page, total is every matching row, and returned < total means you are holding a p… |
| `list_boards` | Paginated: returned is this page, total is every matching row, and returned < total means you are holding a p… |
| `list_categories` | Paginated: returned is this page, total is every matching row, and returned < total means you are holding a p… |
| `list_documents` | Paginated: returned is this page, total is every matching row, and returned < total means you are holding a p… |
| `list_due_items` | Paginated: returned is this page, total is every matching row, and returned < total means you are holding a p… |
| `list_entities` | Paginated: returned is this page, total is every matching row, and returned < total means you are holding a p… |
| `list_expected_payments` | Paginated: returned is this page, total is every matching row, and returned < total means you are holding a p… |
| `list_merchant_rules` | Paginated: returned is this page, total is every matching row, and returned < total means you are holding a p… |
| `list_notes` | Paginated: returned is this page, total is every matching row, and returned < total means you are holding a p… |
| `list_relationships` | Paginated: returned is this page, total is every matching row, and returned < total means you are holding a p… |
| `list_reminder_calendar_links` | Paginated: returned is this page, total is every matching row, and returned < total means you are holding a p… |
| `list_reminder_completions` | Paginated: returned is this page, total is every matching row, and returned < total means you are holding a p… |
| `list_reminders` | Paginated: returned is this page, total is every matching row, and returned < total means you are holding a p… |
| `list_review_history` | Paginated: returned is this page, total is every matching row, and returned < total means you are holding a p… |
| `list_review_items` | Paginated: returned is this page, total is every matching row, and returned < total means you are holding a p… |
| `list_statements` | Paginated: returned is this page, total is every matching row, and returned < total means you are holding a p… |
| `list_tickets` | Paginated: returned is this page, total is every matching row, and returned < total means you are holding a p… |
| `list_transactions` | Paginated: returned is this page, total is every matching row, and returned < total means you are holding a p… |
| `merchant_rule_history` | Paginated: returned is this page, total is every matching row, and returned < total means you are holding a p… |
| `preview_source_import` | Preview a source-tracked statement import without writes. |
| `read_attachment` | Read a ticket attachment BY ID. |
| `reapply_merchant_rules` | Apply the current merchant rules to transactions that were never classified. |
| `reassign_transaction_statement` | Move one active transaction to an existing active statement on the SAME account, using source evidence. |
| `register_document` | Take a local file into the documents store and record it. |
| `replace_forecast_plan` | Replace a forecast plan only with operator-authorized assumptions, not inferred certainty. |
| `revise_artifact` | Append an immutable artifact revision. |
| `revise_review_item` | Update a selected review item only as directed by the user. |
| `snooze_reminder` | Move ONE occurrence of a reminder to a later date. |
| `spending_periods` | Spend by calendar month or Monday–Sunday week for an inclusive date range of at most 366 days. |
| `spending_summary` | Paginated: returned is this page, total is every matching row, and returned < total means you are holding a p… |
| `statement_coverage` | Check whether an account's imported statements cover a continuous stretch of time, or whether one is missing. |
| `sync_google_calendar` | Synchronize every active q-core reminder to its dedicated Google Calendar. |
| `transition_ticket` | Move a ticket to a new status. |
| `trend` | Paginated: returned is this page, total is every matching row, and returned < total means you are holding a p… |
| `unarchive_statement` | Put an archived statement and its transactions back into every total. |
| `unarchive_transaction` | Put one archived transaction back into every total. |
| `unlink_artifact` | Remove one link from an artifact by link_id (from list_artifact_links). |
| `unlink_note` | Remove one link from a note by the link's id (from get_note's links). |
| `update_board` | Rename a board or change its ticket-key prefix. |
| `update_entity` | Change an entity's name, status or attributes. |
| `update_merchant_rule` | Correct the classification of an existing merchant rule — the category or entity it assigns. |
| `update_note` | Revise a note in place: body REPLACES the whole stored text and stamps updated_at. |
| `update_relationship` | Amend a relationship: dates and attributes can be amended; the pair and the type cannot, and omitting a field… |
| `update_reminder` | Edit a reminder series, retaining its id. |
| `update_ticket` | Change a ticket's title, description, parent or position. |
| `update_transaction` | Correct which category or entity a transaction is booked against. |

### Schema

**Tables (30)** — `api_tokens`, `artifact_links`, `artifact_revisions`, `artifacts`, `boards`, `categories`, `documents`, `entities`, `entity_relationships`, `financial_corrections`, `google_calendar_connection`, `google_calendar_deletions`, `google_calendar_events`, `google_calendar_occurrences`, `merchant_rule_changes`, `merchant_rules`, `note_links`, `notes`, `reminder_instances`, `reminders`, `retired_key_prefixes`, `review_item_history`, `review_items`, `schema_migrations`, `source_import_rows`, `statements`, `ticket_attachments`, `ticket_transitions`, `tickets`, `transactions`

**Views (2)** — `active_statements`, `active_transactions`

### Migrations

- `0001_schema_migrations.sql`
- `0002_jyra_tables.sql`
- `0003_idx_txn_dedup.sql`
- `0004_drop_row_hash.sql`
- `0005_archive_imports.sql`
- `0006_merchant_rule_changes.sql`
- `0007_attachment_size_and_type.sql`
- `0008_google_calendar_reminders.sql`
- `0009_financial_corrections.sql`
- `0010_source_import_rows.sql`
- `0011_reminder_lifecycle.sql`
- `0012_briefings.sql`
- `0013_api_tokens.sql`
- `0014_ticket_keys.sql`
- `0015_artifact_kinds.sql`
- `0016_artifact_links.sql`

### Skills

| skill | what it does |
|---|---|
| `daily-brief` | Create or revise a saved daily brief or current-week-to-date report with Claude, or work through the persiste… |
| `document-intake` | File a receipt, tax document, policy, lease, warranty or piece of correspondence into q-core — extract it thr… |
| `email-triage` | Triage the owner's Gmail for statements, receipts and renewal notices, and hand each one to the skill that ow… |
| `financial-evals` | Run or compare q-core financial regression evals, assess extraction/classification/deduplication changes, and… |
| `reminder-digest` | Answer "what's due" — renewals, expiries, lease and policy end dates, and reminders — from q-core, grouped by… |
| `statement-intake` | Process a bank or credit-card statement or new folder in intake/ through the q-core financial audit system —… |

### Tests

| package | test files | test functions |
|---|---|---|
| `api/tests/` | 96 | 1317 |
| `q_core_mcp/tests/` | 35 | 374 |
| `tests/` | 14 | 118 |
| **total** | **145** | **1809** |

<!-- END GENERATED INVENTORY -->

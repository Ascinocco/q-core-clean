# Decisions log

## Near-date candidates for source review (2026-09-26)

Source-review candidates now cover the same account and signed cents within
3 days of the row's date, not only the same date. Posting-date conventions differ between exports (statement vs effective date,
month-end vs the 1st), so an exact-date rule protects only same-date overlaps.

This extends "Source occurrences are not bank transaction IDs"; it does not
reverse it. Candidates still never merge automatically: each needs an
explicit new/link decision with a reason. The window is fixed and small (3
days) because it only widens review. Amount and account still match exactly.
A near-date candidate may be **linked**, so the existing row keeps its
date, category and entity, rather than committed as `new` and archived by hand.
That archive route would insert a duplicate first and depend on a manual
clean-up step. Preview marks each row `match: exact|near_date` and each
candidate `days_apart`, exact dates first. Existing receipts and exact-date
behaviour are unchanged, and a near-date link stays `known_source` on
re-import because its target is within the window.

## Telemetry to the server's observability stack, allowlisted (2026-09-25)

q-core sends OpenTelemetry traces and logs to the OTLP endpoint on its own
host: the server's Alloy at `127.0.0.1:4318`, which feeds Tempo and Loki (host
configuration epic ticket T-69, ticket T-70). This is a new outbound data flow, so
it's recorded here. It isn't a change to exposure: nothing new listens, and
the collector is on loopback.

1. **Off by default.** It's enabled only by `OTEL_EXPORTER_OTLP_ENDPOINT`
   (the module's `telemetry.otlpEndpoint`). There's no telemetry on the Mac or
   in tests.
2. **The privacy rule is enforced at the exporter, by allowlist.**
   Instrumentation libraries decide what they record, and they record query
   strings, exception messages and stack traces. So only allowlisted
   attribute keys leave the process:
   - status descriptions are dropped;
   - exception events keep only the type;
   - log messages are scrubbed of account-number-shaped runs.

   A deny-list would have to anticipate every library's choices; that is the
   failure mode #70 removed from attachments.
3. **Span names stay a closed set.** Routes are recorded as templates, and
   tool names only if they're registered tools. The MCP SDK's span, which
   carries the client-supplied name, keeps only its method.
4. **Only q-core's own reviewed log messages are exported.** Records from
   other loggers are dropped from export by name (`LOG_LOGGERS`), whatever
   their level. Libraries write what they like: httpx logs every request's
   full URL, query string included, and the MCP tools put free text there
   (the review of q-core #11 found a name exported this way). httpx and
   httpcore are also held at WARNING, so that line no longer reaches
   `data/logs/api.log` either. The exported messages are the ones that file
   already holds under runbooks/observability.md's "What is deliberately
   *not* logged", scrubbed of account-number-shaped runs, and Loki is on the
   same host. A new message from an allowlisted logger must meet the same
   rules as any log line: no names, emails, query strings or financial
   values.

## q-core split: publication, access and sync (2026-09-25)

q-core was split out of an earlier combined repository into its own
repository (API, MCP, database, financial pipeline, Jyra, Whisper, life
skills). The decisions below are binding here:

1. **Service split.** q-core is its own repository, and other tooling talks
   to it only over HTTP.
2. **Life skills ship as a q-core plugin, versioned with the API** (built in
   q-core PR #6, `runbooks/plugin.md`).
   - Plugin versions track commits.
   - Native plugin auto-update is off by default for third-party
     marketplaces, runs only interactively and fails silently (spike
     ticket T-60). So the plugin carries its own SessionStart hook that runs
     the explicit marketplace and plugin update commands; turning on the
     marketplace's auto-update is a second path, not a dependency.
   - Automatic update with no manual step is a hard requirement. A plugin
     that can go stale silently is not acceptable.
3. **Tailscale Serve publication, amending the loopback-only rule.** The API
   still binds only to `127.0.0.1`. `tailscale serve` publishes it as HTTPS on
   the tailnet, and tailnet access rules admit only the owner's nodes. This
   replaces "No authentication, bound to loopback only" below, which
   described a single-Mac system. Implementation:
   - **Serve proxies to a private Unix socket** (`serve_socket`, e.g.
     `/run/q-core/serve.sock`), never to the TCP service port (8420). Its
     directory must be 0700 and owned by the service user, and startup
     refuses otherwise. Only tailscaled (root) and the service user can
     connect, so an identity header arriving there was set by Serve, which
     deletes client-supplied ones first and sets none for tagged devices.
     The owner's browsing devices therefore stay user-owned (ticket T-71).
     On the TCP service port identity headers mean nothing.
   - **`api/serve_gate.py` default-denies at the ASGI layer on both
     listeners**, so docs, health, mounts and any future route are covered
     without being listed:
     - On the socket, a request needs an allowlisted `Tailscale-User-Login`
       (`ui_allowed_logins`), which opens only the exact `BROWSER_SURFACE`,
       or a valid token.
     - On the service port it needs a valid token. The sole exception is
       the Google OAuth callback, which is authenticated by its single-use
       state.
     - Routes then enforce scope. The browser routes use `require_reader`
       (a full token or Serve identity), and the generated docs need a full
       token.
     - Tests walk every route on both listeners and pin what is reachable
       with no credential, with identity, with a `transcription` token and
       with the configured phone token.
   - **MCP through Serve** needs `serve_hostname` (the `*.ts.net` name).
     Serve keeps the client's Host header, and the MCP transport's
     DNS-rebinding guard refuses (421) any Host it was not given. Only that
     one name is added to the loopback set.
   - **Every former public read-only route is closed** (spending, briefs,
     evals, forecast, `/health`). This was first proposed as "local trust on
     the service port"; review of q-core PR #3 judged that a gap against
     the spec, so it was closed rather than accepted.
4. **Per-machine scoped tokens with a management page.**
   - `api_tokens` (migration 0013) stores SHA-256 hashes, a display prefix,
     `last_used_at` and `revoked_at`.
   - Scopes are `full` (API and MCP) and `transcription` (`/transcriptions`
     only).
   - Tokens are created and revoked with `python -m api.tokens` on the
     server, or at `/ui/tokens` by **a person through Serve only**. The
     token endpoints need an allowlisted identity on the Serve socket. A
     bearer token of any scope cannot manage tokens: a phone must never mint
     a full token. The service port always refuses.
   - Writes on that page are pinned to configuration: Host must be
     `serve_hostname`, and Origin must be present and equal
     `https://<serve_hostname>`, with a custom header and a JSON body. A
     Serve identity is ambient on every browser request, and a
     DNS-rebinding page controls its own Host (q-core PR #4 review).
   - **The service token (`api_token`) is configuration, not a row.** The
     in-process MCP server uses it for its own loopback calls, so making it
     revocable would let one revoke break every MCP tool. It is retired by
     *rotation* in the server's `.env` at cutover (ticket T-72), not by
     revoking a `legacy-bootstrap` row as the spec first said (review of
     q-core PR #2, R1-F4).
5. **Intake push is a skill step; extraction stays server-side.** On a
   machine that isn't the server, statement-intake copies the named local
   intake folder to the server's `intake/<name>`, printing only file names
   and counts. Scrubbed extraction, preview and commit happen on the server,
   exactly as before.
6. **launchd stays a supported macOS alternative to systemd.** The decision:
   q-core moves to the home server under systemd, and `ops/launchd/` stays
   maintained for running a q-core service on the Mac when a job needs its
   hardware or network. *Amended 2026-09-25:* the server is NixOS,
   not Arch, so the units are a NixOS module in this repository's flake
   (`nix/module.nix`, runbooks/deploy-nixos.md). Nix builds the app from the
   flake's source and a hash-pinned `requirements.lock`, and the host's
   system flake imports it: a deploy is a lock bump there and `nixos-rebuild
   switch`, not a checkout on the server. Serve, secrets and restic are
   configured in the host's system flake.
7. **Remote shells use OpenSSH over the tailnet; Tailscale SSH is not enabled
   on q-core hosts** (`tailscale up --ssh` stays off). Tailscale is the
   network; shells, intake copies and the mobile client use ordinary OpenSSH at hosts'
   Tailscale addresses. Reasons:
   - one trust model across macOS, which can't be a Tailscale SSH server
     with the standard apps, and Arch;
   - per-device keys revocable independently of the tailnet;
   - no "check"-mode browser re-authentication breaking non-interactive
     mobile-client connections;
   - the mobile client's host pinning, SFTP and forwarding are verified against
     OpenSSH.

   Port 22 is limited by the tailnet access rules (ticket T-71), sshd
   is key-only and password login is refused. Full reasoning: ticket
   ticket T-73.

**The mobile-client SSH exception** ("2026-09-22: mobile dictation through
authenticated SSH", below) stays valid until ticket T-02 moves
dictation to the Serve URL with a `transcription` token.

## Dated, explicit cash-flow scenarios (2026-09-21)

Forecasts use private versioned plans managed by authenticated API/MCP, with
a read-only loopback Highcharts page. No ledger mutation, schema migration,
standing classifier or bank feed. Plans retain dated balances and confidence/
evidence on recurring assumptions. Null debt balances/payments remain unknown.
Cash transfers conserve cash; card purchases are costs/debt, repayments move
cash without counting the cost twice. Housing costs include mortgage principal.
A runway account can carry a growth target without being treated as a savings
deduction. Extra debt payments are explicit scenarios, never automatic surplus
allocation. Future registered-account contributions are not assumed. Actual transactions
must not be silently blended with schedules without reconciliation. Private
immutable plan revisions and optimistic concurrency prevent accidental lost
updates; they remain distinct from source-backed ledger records.

## Repo-discoverable evals and summary-only read UI (2026-09-21)

`CLAUDE.md` routes plain-language financial intake and eval requests to distinct
project skills; `AGENTS.md` points repo-based agents to that shared guidance.
New intake subfolders use the existing scrub/preview/commit workflow, never raw
reads. Eval requests use existing offline scorers, not live ledger mutations or
a standing model service. Fresh extraction is distinct from saved-output replay.

The loopback-only `/ui/evals` and `/evals/runs` GET routes read explicitly
published, whitelisted summaries. No execution/upload/download or write routes,
private artifact scanning, or importing eval runners into the production API.
The shared data-only schema lives in the API; the offline publisher consumes it.
Reports retain detailed evidence privately and publication preserves report and
dataset hashes. Corpus labels are operator attestations, not proof of unseen
accuracy. Classification remains “measured” unless acceptance criteria are
separately defined. Existing development trials do not become holdouts.

## Measure source-aware intake without gold-assisted imports (2026-09-21)

Fresh agents receive only approved scrubbed packets and current instructions.
Frozen source references and accepted ledger labels stay outside their context.
The offline runner scores extraction and source evidence separately from
classification, then replays actual submissions through disposable production
routes. Never repair outputs or supply gold-derived link/new decisions. Unknown
overlaps must refuse; wrong signs/dates can evade matching and must remain
visible as failures. The intake contract explicitly distinguishes balances
held from balances owed. Scoped agent context
is not OS isolation, and development-corpus agreement is not unseen accuracy.

## Retire the legacy statement importer (2026-09-21)

User approved removing the legacy production write path now. Remove both its
HTTP route and MCP tool; source preview/commit is mandatory, without a bypass
for missing locators or ambiguous overlaps. Preserve existing ledger data and
source receipts unchanged. Test fixture writers now use the production source
path. Keep the frozen legacy simulator only for explicitly labeled historical
evaluations, restricted to disposable in-memory databases. Reviewed-ledger
replay uses current production imports; fixture identities are not bank IDs.

## Source occurrences are not bank transaction IDs (2026-09-21)

Add a separate preview/commit path rather than silently changing legacy
dedupe semantics. Original file SHA plus a stable physical row locator
identifies a source occurrence, scoped to an account. Re-exports do not share
that identity. Cross-source account/date/cents candidates require explicit
new/link decisions even when descriptions match; no fuzzy auto-merging.
Receipts and new rows commit atomically under BEGIN IMMEDIATE with a stale
preview guard. Existing transactions and classifications are never rewritten.
Migration 0010 adds receipts only; no historical backfill or live reimport.
Trusted source locators are a caller responsibility; a missing bank ID is not
replaced by an invented one. Refusals are scored separately from resolution.

## Narrow extraction comparison compatibility (2026-09-21)

User approved ignoring standalone `[REDACTED]` tokens and whitespace in the
importer's description comparison only. Stored descriptions and classification
inputs remain untouched; no schema migration or historical rewrite. Both the
incoming occurrence counter and existing active-row comparison use this key.
Account, transaction date and integer cents must still match exactly, as must
merchant case, punctuation and masked suffixes. Indexed account/date candidate
selection is retained. Placeholder-only descriptions do not collapse to an
empty key. This is not fuzzy matching or a claim to solve ambiguous partial
exports.

Architecture decisions for q-core, in the order they were made, with the
reasoning. Update this when a decision changes rather than just silently
doing something different — the point is to stop the same debate from
happening twice.

## Local-only, no Tailscale/Headscale (original decision; scoped exception below)

Runs entirely on one laptop. No remote access, no mobile client, no
tailnet. Removes an entire layer (Headscale self-hosting, iOS
coordination-server config, always-on-host tradeoffs) that was designed for
a need that doesn't exist yet. If remote/mobile access becomes a real need
later, revisit Tailscale (or its hosted free tier) then — don't build
placeholders for it now.

## API server: Python, FastAPI, stdlib sqlite3, no ORM

- **FastAPI + Uvicorn** — request validation and API docs for free, low
  ceremony.
- **Pydantic models** validate the per-type shape of `entities.attributes`
  (Property/Vehicle/Person/Account/...) at the API boundary. SQL just
  stores the JSON blob; the shape contract lives in code.
- **stdlib `sqlite3`, no ORM.** Schema is simple enough that SQLAlchemy
  would add ceremony without buying much at single-user scale.
- **`dateutil.rrule`** for lazy RRULE expansion (reminders computed at
  query time, not materialized — see original design doc rationale).
- **One `GET /due`, not `GET /reminders/due`.** The design doc lists
  `GET /reminders/due?start=&end=` under Reminders; that line is
  **superseded** (D17). Three different things carry a future date — a
  forward-dated entity attribute, a relationship's `end_date`, and a
  `reminders` row — and an endpoint answering only the third would let
  the other two go unasked. A digest that reports one source out of
  three and returns 200 is worse than no digest, because "nothing is
  due" is indistinguishable from "I did not look". So: one endpoint,
  `GET /due?from=&to=`, unioning all three with each item tagged by
  `source`, one MCP tool `list_due_items`, and no second thing a model
  has to remember to also call. Attribute and relationship dates in the
  past come back as overdue with a negative `days_left` rather than
  being filtered by `from` (D19) — nothing rolls a point date forward,
  so a range filter would hide a lapsed registration exactly when it
  matters. Reminders are the exception and are expanded only within the
  window, because a recurring rule has unboundedly many past
  occurrences.

## No authentication, bound to loopback only

> **Superseded (2026-09-25)** by "q-core split: publication,
> access and sync", decisions 3 and 4: every route is authenticated on both
> listeners (the loopback service port and the Serve socket), and the
> loopback bind is kept but published through Tailscale Serve. Kept as
> history.

The API server binds to `127.0.0.1` only. No login/session system — at
single-user, localhost-only scale, the real access boundary is "who can
run code as this Mac user account," which a login flow can't improve on.

One piece of cheap insurance worth keeping: a static shared-secret bearer
token (random value in a local, gitignored `.env`) that the MCP server
sends on every call. Not identity-based auth — a tripwire against (a) some
other local process/browser tab probing the port, and (b) an accidental
bind to `0.0.0.0` going unnoticed. Low cost, add it.

## Document storage: plaintext on disk, cloud-drive backed up, no object-store service

Considered MinIO (self-hostable S3-alternative) for document storage —
decided against it. MinIO solves for multi-consumer/remote access, which
doesn't apply on a single local machine.

Originally planned to encrypt each file individually with `age`. Reversed
after direct discussion during the API server design pass:

- `data/documents/` is **plaintext**. full-disk encryption
  already covers the at-rest threat while the Mac is off/locked; once
  logged in, anything running as the user account reads the same
  plaintext either way, so per-file encryption doesn't add real
  protection beyond what full-disk encryption already provides — the same trust
  boundary already accepted for skipping API auth. This makes documents
  directly Finder-openable, no decrypt step, no keypair to manage.
- `data/documents/` also syncs to a cloud drive as backup, as plaintext —
  an informed choice made after flagging that a consumer cloud drive is not
  zero-knowledge-encrypted (the provider's own systems can access plaintext
  stored there, unlike a stolen/locked laptop protected by full-disk encryption).
- `documents` table still holds metadata only (id, entity link, title,
  type, absolute file path, content hash, timestamp) — that part is
  unchanged.
- `data/` remains gitignored entirely — the DB file and documents never
  get committed to git, even though the repo is private and even though
  they're no longer encrypted; git is a poor fit for binary blobs
  regardless (unbounded history growth), and "private repo" isn't a
  substitute for "not in git" given how many ways a repo's access can
  change.
- Mobile intake uses a separate `inbox/` folder (transient staging,
  cloud-drive mirrored, deleted after processing) — see
  the API server design for the full
  intake flow and the document filename convention.

If remote/multi-device access beyond cloud-drive sync ever comes into scope,
MinIO (or SeaweedFS/Garage) is the thing to reach for then, not now.

## One repo, doubles as project directory and knowledge base

`~/Projects/q-core` is both the git repo and the working directory —
there's no separate "project folder" distinct from the repo. Private
GitHub repo to be created later (not yet created as of this writing).

The repo holds code (`api/`, `mcp/`, `skills/`) alongside `runbooks/` — a
plain-Markdown knowledge base that's the stopgap between ad hoc skill
instructions and a real MCP-exposed knowledge tool. `runbooks/INDEX.md`
mirrors the same index-plus-one-file-per-fact pattern used for Claude's
own persistent memory, so it stays skimmable instead of turning into one
giant file.

`CLAUDE.md` at the repo root establishes that opening a Claude Code
session here means either *operating* q-core (default assumption for
ambiguous requests — use the tools, don't touch code) or *building* it
(schema/API/MCP/skills work — normal engineering rules, check this log
first).

## Entity types, attributes, and relationships

Per-type `attributes` JSON shapes are written up in
`runbooks/entity-attribute-schemas.md`. Key calls made alongside that:

- **`document` removed from the entity type enum.** The dedicated
  `documents` table (added earlier) already covers documents fully via a
  direct `entity_id` FK; documents don't need multi-hop
  `entity_relationships`, so having them in both places was redundant.
  Entity types are now: `person | property | vehicle | pet | account`.
- **Rentals:** tenants are `person` entities (`relationship_to_you:
  "tenant"`); a new `leases` relationship type carries the lease terms.
  This required giving `entity_relationships` its own `start_date`,
  `end_date`, and `attributes` columns — relationships aren't just typed
  edges anymore, some carry state. Rent income/expense still flows through
  `transactions.entity_id → property`, no new plumbing needed there.
  Confirmed: one `property` entity = one rentable unit for now; multi-unit
  buildings are out of scope until a `project-management.json` todo.
- **Insurance modeled as `account`** (`account_subtype: "insurance"`)
  rather than a separate table, linked to what it covers via `insures`.
  Keeps the "generalize, don't proliferate tables" principle intact.
- **Dropped the planned `UNIQUE` constraint on `entity_relationships`.**
  Cardinality expectations differ by `relationship_type` — `owns`
  shouldn't duplicate, `leases` should accumulate rows over time on
  renewal — and SQL can't express "unique unless dated" cleanly given
  NULL-handling in unique constraints. Dedup for point-in-time types is
  an API-layer responsibility now.
- **Mileage/odometer history:** decided out of scope, not modeled.
- Added `status` + `updated_at` to `entities` so selling a property or
  totaling a car doesn't require deleting the row (and orphaning linked
  transactions/documents) — mark it `inactive`/`sold`/`totaled` instead.

## Notes: generic, polymorphic

Added `notes` + `note_links(target_type, target_id)` so a note can attach
to any entity, transaction, document, reminder, or statement. `target_id`
has no SQL foreign key since the target table varies by row — referential
integrity there is an API-layer check, not a DB constraint. Standard
trade-off for a polymorphic association, acceptable at single-user scale.

## Task tracking: project-management.json

Lightweight todo list at the repo root — `{"todos": [{"id", "title",
"description"}]}`. Deliberately minimal (no status/priority fields) unless
that proves insufficient. Multi-unit property support is a todo. Open
items below are design questions still being talked through; once one
turns into concrete work, it belongs in `project-management.json`, not
just this log.

## Category taxonomy: fixed, two-level, FK'd

Resolved fixed-vs-freeform in favor of fixed: a real `categories` table
(id, name, parent_id) rather than a freeform string column, seeded from
`db/seed_categories.sql`. `transactions.category` and
`merchant_rules.category` renamed to `category_id` FKs accordingly. Full
tree and rationale in `runbooks/category-taxonomy.md`; "fixed" means no
typo/casing drift, not "hard to change" — editing the tree is a SQL
insert, not a migration.

Key rule: category describes the *kind* of spending, never the asset —
`entity_id` already carries "which property/vehicle," so the tree doesn't
fork per-asset (one `Housing > Utilities`, not separate rental/home
versions).

## Merchant rule precedence: longest pattern wins

When a transaction description matches multiple `merchant_rules` rows,
the most specific (longest) `pattern` wins, computed at match time — no
priority column to maintain. Full reasoning in
`runbooks/merchant-rules-conventions.md`. Still open there: whether
correcting a transaction's category should ever rewrite an existing rule
vs. only ever create new ones.

## Correction behavior: override by default, new rule on explicit opt-in

Correcting a transaction's category always updates just that transaction.
It never silently edits an existing `merchant_rules` row — a broad rule
like `"AMAZON"` being mutated because one purchase was miscategorized
would misclassify unrelated past/future charges matched by that same
rule. If the correction should apply going forward, the flow asks once
("always categorize [merchant] this way?") and, on yes, **creates a new,
more specific rule** rather than editing the broad one — precedence
(longest pattern wins) means the new rule takes over for that merchant
without touching the old one. Directly editing an existing rule's
classification is a separate, explicit action, not a side effect of
correcting a transaction. Full detail in
`runbooks/merchant-rules-conventions.md`.

## Migration tooling: deferred, with a convention in mind

No framework chosen, and none needed yet — nothing has real data in it,
so schema changes just mean reapplying `db/schema.sql` +
`db/seed_categories.sql` to a fresh SQLite file. Once real data exists
that a wipe-and-recreate would destroy, the intended approach is numbered
plain-SQL files under `db/migrations/` plus a `schema_migrations` tracking
table, run by a small script — not Alembic or similar, since those are
built around an ORM and the API server deliberately has none. Not
building the runner now; revisit when the first real migration is needed.

## Skills live at `.claude/skills/`, not top-level `skills/`

> **Superseded (2026-09-25):** the skills moved to `plugin/skills/` and ship as
> the q-core plugin ("q-core split", decision 2). Kept as
> history.

Claude Code auto-discovers project skills from `.claude/skills/<name>/SKILL.md`
— a plain top-level `skills/` directory (as originally sketched) would
never actually get picked up by the Skill tool. Moved before any real
content existed there, so no migration cost. `statement-intake` is gated
on the API server + MCP server existing (it needs to call tools like
`import_statement` that don't exist yet) — see `project-management.json`.

## API errors: `HTTPException` subclasses, no separate `APIError` hierarchy

The Phase 2 entities plan (Task 1) originally specified a standalone `APIError` exception hierarchy
plus a new `@app.exception_handler(APIError)` in `main.py`. Revised before
implementation: Phase 1 had already landed `main.py`'s
`@app.exception_handler(StarletteHTTPException)`, which passes an
exception's `.detail` straight through as the response body whenever it's
already shaped `{"error": {"code", "message"}}` — the exact envelope the
plan wanted, via a mechanism that already existed. Building a second,
parallel hierarchy plus handler for the same contract would mean two ways
to produce the same JSON shape, no clear rule for which to use, and a
correctness question about handler precedence between them.

`api/errors.py` therefore defines `NotFoundError`, `InvalidReferenceError`,
and `ConflictError` as thin subclasses of `fastapi.HTTPException`, each
setting `detail={"error": {"code": ..., "message": ...}}` in `__init__` —
call-site ergonomics (`raise NotFoundError("...")`) are unchanged from the
original plan, only the internals differ. No changes to `main.py` were
needed. See the implementing PR for details.

**Which status each rejection carries, and why they must be read
together.** Amended by ticket T-33: `ConflictError` was 400 and is now
**409**.

| Class | Status | Means |
|---|---|---|
| `NotFoundError` | 404 | the thing addressed by the URL does not exist |
| `InvalidReferenceError` | 400 | the request **refers to something that does not exist** |
| `ConflictError` | 409 | the request **conflicts with something that does** |
| `RequestValidationError` | 422 | **a value outside its permitted range** |

**400 against 422: is the field a reference or a value?** The two lines
above are close enough to be misread, and were — impl-3 filed
ticket T-74 arguing 400 for a `due_date` that is not an occurrence of a
reminder, reading "refers to something that does not exist" literally.
The ruling is 422, and the test is what the field *is*:

- **400** is for a **reference** — an id naming a row that is not
  there. `entity_id` pointing at no entity. The caller's value is
  well-formed; what it points at is missing. (This bullet used to end
  "and no list of permitted values could be written down", which the
  retraction below contradicts — `/documents/extract` refuses at 400
  and names all three allowed directories. Whether the set can be
  listed is not what decides the code.)
- **422** is for a **value** judged against a set the request itself
  defines. A bad enum member, a `period` failing its pattern, a
  `due_date` that is not on the reminder's occurrence series.

A `due_date` is a value, not a reference: it addresses no row, and the
occurrence series is exactly the permitted set, so the refusal names
the occurrences the way the entity and relationship enums name theirs.

**"Can the refusal list the permitted values?" is a useful heuristic,
not a law, and the first version of this entry overstated it.** It said
the permitted set being enumerable is *why* these refuse at 422 "and
400s do not". review-2 measured the counterexamples: `/documents/extract`
refuses a path outside three directories with a **400** that names all
three, so a 400 can list its set; `/trend` with neither `category_id`
nor `entity_id` is a 400 with no referent at all; snoozing to the same
day is a **422** over a set nobody could enumerate; and six
`InvalidReferenceError` sites are value or state refusals rather than
dangling references.

So the heuristic predicts the cases it was derived from — the enums,
`period`'s pattern, `due_date` — and it is worth applying in that shape.
The rule underneath it is still the one above: **is this field a
reference to a row, or a value judged against a set?** Where the two
disagree, the question decides and the heuristic yields.

`ConflictError` at 400 made that code mean two different things, so a
caller could not tell a bad reference from a state conflict without
reading the message — and a 400 is the one a caller might retry after
correcting their input, which never helps for a conflict. The eight
raise sites are all state conflicts: something still points at the row
being deleted, the period already exists, the relationship exists with
different values. None is fixable by correcting the request.

The distinction only survives if the four are stated in one place. Two
of them were defined apart, which is how the collision happened rather
than anyone deciding 400 should mean both — so
`api/tests/test_errors.py` asserts all of them in a single test, and a
source-reading test refuses any conflict-shaped rejection that answers
400.

Known gap, not yet closed: no test asserts the full request/response path
(a route raising one of these actually returns the JSON envelope through a
real `TestClient` call) — the existing tests only assert `.status_code`/
`.detail` on the exception objects directly. A regression in `main.py`'s
handler (e.g. the pass-through guard being dropped) would silently degrade
every error response without failing any current test. Tracked in
`project-management.json`.

## Pagination `ORDER BY` convention: `COLLATE NOCASE` + tiebreak is a text-column rule, not universal

Phase 2 Task 5 (`GET /entities`, `ORDER BY name`) originally sorted
case-sensitively (`['Mid', 'Zeta', 'alpha']`); fixed to
`ORDER BY name COLLATE NOCASE, id` — case-insensitive for the human
expectation, `id` as a tiebreak since `COLLATE NOCASE` makes case-variant
names compare equal and SQLite doesn't guarantee tie order otherwise.

Task 6 (`GET /entities/{id}/relationships`, `ORDER BY id`) deliberately
did **not** apply the same pattern — `id` is the UUID primary key, already
unique and already a total order, so neither `COLLATE NOCASE` nor an
additional tiebreak column adds anything. The rule is specifically about
sorting on a *text column a human reads* (names, labels) where collation
and duplicate values are both real possibilities — not a blanket "every
`ORDER BY` needs `COLLATE NOCASE` and a tiebreak." Apply it where the sort
key is text and could plausibly tie or need case-folding; skip it where
the sort key is already a unique, total-order column like a UUID PK.

## WAL mode means `data/q-core.db` can no longer be backed up by plain file copy

`api/db.py`'s `get_connection` sets `PRAGMA journal_mode=WAL` on every
connection (a `project-management.json` todo, PR #7), fixing the
reader/writer blocking a concurrent poller (Jyra) would otherwise hit.
Side effect worth knowing before it causes a bad backup: WAL keeps
uncommitted/recently-committed data in `-wal` and `-shm` sidecar files
next to `data/q-core.db`, not always in the main DB file itself. Copying
`q-core.db` alone while the API is running can now miss committed
transactions that are still sitting in the WAL file — this wasn't true
under the old rollback-journal default. Any future backup approach
(manual, cloud-drive sync of `data/`, a cron job, whatever) needs to either stop
the server first, or use `sqlite3 data/q-core.db ".backup <dest>"` /
`VACUUM INTO`, which are WAL-aware. No backup automation exists yet, so
nothing to fix today — just don't let a naive `cp` become the pattern.

## Sensitive-field length caps are a shape defense, not a content defense

Todo #5 (PR #8) caps `last4`/`id_last4` at 4 characters and redacts them
out of 422 error bodies. Worth being explicit about what this does and
doesn't cover, since "we cap `last4`" easily misreads as "the API prevents
account numbers being stored" — it doesn't. A full card or account number
submitted under any *other* key name (e.g. an `account` entity's free-form
`institution` field, or any future free-text attribute) is accepted and
stored as-is; `extra="forbid"` rejects unknown keys but does nothing about
a real number placed under a real, allowed key. CLAUDE.md correctly
assigns that redaction to the intake-skill layer, before extraction output
reaches the API at all — the API's field caps only defend the specific
shapes (`last4`, `id_last4`) it knows are sensitive by name, not content in
general. Don't extend this pattern to "cap more fields" as a substitute
for actually redacting at intake; they're different layers of the same
rule (CLAUDE.md: SSNs/full account numbers "never sent to any LLM and
never stored in the DB") and both need to hold independently.

Related: `api/main.py`'s `RequestValidationError` handler (a todo,
PR #9) drops Pydantic's `input`/`url` fields from *every* validation
error unconditionally, not just ones touching `last4`/`id_last4` by name —
this closes the disclosure side (a rejected value echoed back through a
422 body) for any field, not only the two currently capped. It does not
and cannot close the storage/content side above; that boundary is
inherent to what a field-name check can see.

**It closes `input`, and `msg` was the other half** (#146). A
hand-built message that interpolates the rejected value into its own
text reaches the caller regardless, because the handler keeps `msg`.
Three did: a date field echoed the raw string it refused, the RRULE
validator forwarded dateutil's message (which quotes the caller's
string back, so it was echoing input at one remove — **a third-party
exception is untrusted text for this purpose**), and the merchant-rule
collision echoed a transaction description, which is scrubbed bank text
read out of the database rather than anything the caller sent.
`api/tests/test_error_message_interpolation.py` now requires every
interpolation into an error message to be classified, keyed by module
so one name can be safe in one file and not in another.

**One classification in that table is contingently safe, and the
contingency is not visible from the table.** `main.py:name` is the
attribute name in `_LazyClient.__getattr__`, which is safe only because
every caller asks for a fixed attribute — nothing dispatches on a
caller-supplied string. That is the same shape as `edited_at`'s
single-writer premise: true today, and nothing fails if it stops being
true. If dynamic dispatch is ever added there, this classification must
be revisited rather than inherited.

## `updated_at` uses SQLite `CURRENT_TIMESTAMP`, accepting 1-second resolution

Phase 2 Task 7 (PR #6) originally set `updated_at` via Python's
`datetime.now(UTC).isoformat()`, which produced a different format from
`created_at`'s SQL-generated `CURRENT_TIMESTAMP` in the same response
(space vs. `T` separator, no microseconds vs. microseconds, implicit vs.
explicit UTC offset) — found in review, fixed by switching `updated_at` to
`CURRENT_TIMESTAMP` too, matching `created_at`'s mechanism.

Deliberate trade-off worth recording: SQLite's `CURRENT_TIMESTAMP` has
1-second resolution, where the Python-generated value had microseconds.
Two `PATCH`es to the same entity within the same second now produce an
identical `updated_at`, so it can't serve as a tiebreaker for ordering
rapid successive edits. Accepted because matching `created_at` (which
already had that resolution) beats sub-second precision at this system's
scale, and the alternative would have meant changing `created_at` and the
schema instead of `updated_at`. If something later needs sub-second edit
ordering (unlikely at personal-inventory scale, but Jyra's ticket
transitions might care), that's the actual constraint to revisit — not
"why do these differ from ISO 8601."

## `init_db`'s bootstrap is now race-proof by construction, not probability (closed: a todo)

Started as one line in Task 8's (PR #11) README verification — the
concurrency claim didn't hold on a cold DB — and became a multi-round
investigation across several PRs before landing on the actual fix. Worth
recording the full arc, because almost every step corrected an earlier
step, and the corrections are the reusable part:

1. **Initial repro:** cold-start concurrent requests against a
   not-yet-bootstrapped DB raced on `init_db`'s `if db_file.exists():
   return` guard, producing lost writes and 500s.
2. **First split (wrong in one detail):** reasoned the guard-trusts-a-
   broken-file bug (a 0-byte `data/q-core.db` — which had actually
   happened, permanently breaking the API against its default path) was
   fully independent from the concurrency race. Measured-and-corrected
   twice: the race can *also* produce the guard's exact failure mode at
   race speed (a caller sees another racer's in-progress empty file), so
   they share a root cause even though the guard bug is independently
   triggerable with zero concurrency.
3. **Fix, round 1 — temp file + `os.replace`:** closed the two original
   loud symptoms (`table already exists`, `no such table`). Verified
   clean at 103/103 and passed review. Then review-1 measured the
   *shipped* code anyway (not just the design) and found a third,
   undocumented symptom at ~1-in-13: `attempt to write a readonly
   database` — `os.replace` swapping the file's inode out from under
   another caller's already-open connection.
4. **Fix, round 2 — `os.link()`:** atomic create-if-absent instead of an
   unconditional swap. In the process of testing it, impl-2's own
   concurrent-bootstrap test turned out to be read-only and structurally
   blind to write errors — the exact same shape of blind spot as an
   earlier reviewer's readiness-gated harness missing the narrow race
   window. Fixed the test to write-then-read and found a *fourth*
   symptom, worse than the first three: 11 of 300 requests returning
   `200` with silently wrong data (partial category counts), from a
   racer reading the table mid-seed. No exception, nothing to alert on.
5. **Fix, round 3 — drop auto-repair entirely:** the remaining gap was
   the broken-file *repair* path (`FileExistsError` falling back to
   `os.replace`), reachable only when a pre-existing broken file meets
   concurrent cold starts. Decision: don't build an advisory lock:
   instead a broken file now raises `CorruptDatabaseError` /
   `IncompleteDatabaseError` (named path, explicit remediation) rather
   than self-healing. Reasoning — this reaches the same
   architectural-certainty standard as the rest of the fix rather than a
   narrower probability, avoids new lock-related failure modes, and a
   database file silently repairing itself after going corrupt is
   arguably the wrong default anyway for a system whose job is being a
   trustworthy record. Measured clean: 200/200 trials.
6. **Final verification, independently reproduced:** all four symptoms
   confirmed present on the old code and absent on the new, including
   genuine multi-*process* concurrency (Jyra's actual shape, not just
   threads). One more self-correction here too: a stress test that
   released 10 processes on a shared start signal caught a specific
   regression 0 times out of 10 — not because the guard was weak, but
   because process-startup jitter meant the losing branch was never
   actually reached. A stress test that never enters a branch is
   indistinguishable from one that enters it and passes; only the
   deterministic mutation-tested injection test actually held the line.

`_install`'s docstring states the two invariants that must keep holding —
build-then-install, create-if-absent — as requirements for the next
person to preserve, not a record of what was measured once.

**Addendum — the original 0-byte file's actual cause, found later
(Jyra Task 8, PR #32):** step 2 above records that a 0-byte
`data/q-core.db` had genuinely happened at some point before this
investigation, attributed at the time to a server process briefly
touching the default path. The real mechanism was simpler and unrelated
to any server or race: running `sqlite3 data/q-core.db "<query>"` to
check whether the database exists or holds expected data **creates the
file itself**, even when the query then fails against it — the CLI
opens the path for write before running the query. Reproduced
deliberately once found. This means the file can appear from a
completely innocent diagnostic command, no concurrency or server
involved, and given `_database_state` classifies a 0-byte file as
corrupt and `init_db` now refuses to auto-repair it (round 3 above),
that diagnostic command is capable of bricking the app until a human
removes the file. **Never point `sqlite3` at `data/q-core.db` to check
existence or contents — use `ls`, or open it read-only explicitly
(`sqlite3 "file:data/q-core.db?mode=ro" -readonly`, or Python's
`sqlite3.connect(f"file:{path}?mode=ro", uri=True)`).**

Both todos closed 2026-09-18, merged as PR #13 (see next entry for why
the number jumped from #12).

**Postscript, worth recording separately from the fix itself:** the
`BASELINE_TABLES` migration guard (below) got its first real-world test
days later, not a simulated one — Jyra's Task 1 added four tables and
initially forgot the migration for them, and the guard caught it and
named exactly what was missing before any code shipped. A guard that has
only ever passed a deliberately-injected mistake is one kind of evidence;
one that has caught an actual, unplanned mistake made by someone who
wasn't trying to test it is a different, stronger kind.

## GitHub's commit-message auto-close doesn't parse negation

A commit pushed to `main` containing the sentence "don't close #12
without that test's actual result" caused GitHub to auto-close PR #12
mid-review — its keyword linkifier matches `close #<N>` as a substring
regardless of what precedes it, including "don't." The close was silent
(same shared bot account as every other automated action here, so nothing
looked unusual in the log) and review continued being posted to the
closed PR for over ten minutes before anyone noticed. Compounding it: by
the time it was noticed, later force-pushes to the same branch (normal,
already-reviewed iteration) meant GitHub refused to reopen the original
PR at all ("branch was force-pushed or recreated") — had to be recreated
as a new PR number (#13) from the same branch/commits/review history.
Lesson: never write "close", "closes", "closed", "fix(es/ed)", or
"resolve(s/d)" immediately followed by a `#<number>` in a commit message
or PR body, even inside a sentence telling someone *not* to do that
thing — rephrase around it (e.g. "leave #12 open until X" instead of
"don't close #12 until X").

## `paginate()` raises an HTTP-shaped error directly, not a bare `ValueError`

`api/db.py`'s `paginate()` raises `RequestValidationError` itself when
`limit` is out of bounds, rather than a bare `ValueError` caught by a
handler in `api/main.py`. This looks like a DB-layer helper reaching up
into HTTP concerns it shouldn't know about — flagged as exactly that in
PR #28's own body, alongside the layer-pure alternative (a `ValueError`
handler in `main.py`, still 422, but losing the `loc` pointing at
`query.limit`).

The layer-pure alternative was tested, not just weighed, before deciding
against it (PR #28 review): its natural shape is
`@app.exception_handler(ValueError)`, which catches *every* `ValueError`
in the codebase — and `json.JSONDecodeError` is a `ValueError` subclass.
`api/entities.py` alone has five `json.loads` calls reading `attributes`
back from a stored row. Implementing the handler and corrupting a stored
`attributes` blob produced a 422 `validation_error` for a DB-corruption
failure — reported to the caller as *their* mistake. That directly
reverses the `DataIntegrityError` decision two PRs earlier (see the
board-view entry above), which exists precisely so this class of failure
reports 500, on the grounds that the caller did nothing wrong and cannot
fix it. A blanket `ValueError` handler would systematically
re-misclassify exactly the failures that decision was written to
classify correctly.

So the real trade isn't "layer purity vs. a better `loc`" — it's "layer
purity vs. silently mislabelling data corruption as client error," and at
that price the wart is cheap. `paginate()` keeps raising the HTTP-shaped
error directly, matching the existing precedent in
`validate_entity_attributes`.

One option was named and deliberately not taken: a typed error in
`api/errors.py` carrying the field name, mapped by a handler in
`main.py` — keeps `db.py` free of `fastapi` imports, preserves `loc`,
avoids the blanket catch. Not used because every existing
`api/errors.py` type is an `HTTPException` subclass producing the flat
envelope, while this needs the 422-with-`details` shape `main.py`'s
`RequestValidationError` handler already builds — it would start a
second error family rather than join the existing one, for a marginal
layering gain not worth that cost. Recorded here so it isn't
rediscovered later as an unexplored path; if a future case needs both
`loc`-accuracy and layer purity badly enough to justify a second error
family, this is the place that decision would revisit.

## Unhandled exceptions: middleware, deliberately not `@app.exception_handler(Exception)`

Todo #22. An unhandled exception used to escape as Starlette's plain-text
`Internal Server Error`, breaking the `{"error": {"code", "message"}}`
envelope every other path honours. Found independently twice (the
observability work, and impl-1 while fixing the attachment-suffix crash),
across four instances of one shape: a builtin raised by a stdlib call on
caller-influenced input, in a route with no local try/except.

The fix is `ErrorEnvelopeMiddleware` in `api/errors.py`, **not** a handler
registered for `Exception`. This looks like the long way round and is not:
Starlette routes a handler registered for `Exception` (or `500`) to
`ServerErrorMiddleware`, which sits *outside* every user middleware and
sends through its own `send`. A response from there never passes through
`RequestLoggingMiddleware`'s send wrapper, so it carries no
`X-Request-ID` — leaving the one response class a user is most likely to
report as the only one whose id they cannot quote back. Measured, not
reasoned: building the handler version returns a perfectly correct
envelope body and fails exactly the two header tests.

Consequences worth knowing before changing any of this:

- **Install order in `api/main.py` is load-bearing.** `add_middleware`
  inserts at the front, so the last call is outermost, and the required
  stack is ServerError -> RequestLogging -> ErrorEnvelope -> routes.
  Reversing the two lines silently drops the header from every 500;
  `test_the_real_app_returns_the_envelope_with_a_request_id` is the test
  that pins it, and it hits the real app precisely because the other
  tests build their own and would not notice.
- **The exception is swallowed, not re-raised.** Re-raising after the
  response is sent makes the server log the same traceback a second time,
  which is the duplicate-line problem the observability work already
  fixed once. The practical effect is that the app no longer propagates
  its own exceptions to the ASGI layer, which retired a test documenting
  an `httpx.ASGITransport` hazard that no longer exists.
- **`DatabaseNotUsableError` keeps a distinct code and message**
  (`database_unusable`) rather than collapsing into `internal_error`. It
  is the one 500 with a specific remedy, and `q_core_mcp/client.py`
  surfaces that message verbatim to whoever called the tool. The log path
  and the `python -m api.run` command in it are load-bearing, not
  decorative — `q_core_mcp/tests/test_client.py` pins their presence.
- **The body is a fixed string, never `str(exc)`.** An exception message
  can carry anything a route interpolated into it, and this body reaches
  the MCP client and so a model's context.

## Money is integer cents, and this change shipped without a migration

`transactions.amount` was `NUMERIC`, which SQLite stores as a C double.
Most cent values have no exact binary representation, so every aggregate
that summed them accumulated error. It is now `amount_cents INTEGER NOT
NULL`, and the HTTP edge speaks integer cents too — `TransactionInput`
uses `StrictInt`, so a float is **rejected with a 422 rather than
rounded**. Accepting dollars would have put a `round(amount * 100)` at
exactly the boundary where the representation error lives, and silent
rounding is how a cents column ends up holding float-derived values. The
intake harness reads CSV text, so it can produce exact cents without ever
passing through binary floating point. Display formatting belongs to
whoever displays.

This also makes the Transfers-nets-to-zero property real rather than
approximate: the two legs of a card payment now cancel to exactly `0`,
not to something that prints as zero.

**No `db/migrations/` entry was added, deliberately.** A column-only
migration cannot execute under the current runner:

- `init_db`'s `"ready"` branch returns *before* checking for pending
  migrations, on purpose — that check is a query, and a locked database
  classifies as ready, so querying there would turn a live write into a
  failed request.
- Migrations are therefore reachable only through the `"incomplete"`
  branch, which triggers on a missing **table**.

That same `"ready"`/`"incomplete"` control flow is described again, from a
different angle, in "The migration path had its own race" below — that
entry covers what happens when two callers reach the `"incomplete"` branch
at once. Two entries now depend on this branch structure; if it changes,
both need updating, not just whichever one the change came through.

The tempting workaround is the SQLite rebuild pattern — `CREATE TABLE
transactions_new`, copy, drop, rename — because it satisfies
`test_repo_migrations_are_table_additive`'s `CREATE TABLE` regex. It does
not work: on a ready database the runner is never reached, so the file
would exist, the test would pass, and nothing would happen. That is worse
than no file, because it looks like the change is covered.

A database created before this change would be classified "ready", skip
migrations and fail on every financial endpoint with `no such column:
amount_cents`; such a database is refused at startup (see "Migrations stay
SQL-only" below). Index-only changes are delivered as migrations.

**Closed (2026-09-19).** The runner now applies migrations at startup, so
an index no longer needs a hand step: `db/migrations/0003_idx_txn_dedup.sql`
carries it with `CREATE INDEX IF NOT EXISTS`, which records the
hand-patched database as up to date without touching it and creates the
index anywhere it is genuinely missing. This one-off is not a pattern to
repeat — it was the last of its kind.

**Superseded (2026-09-19), for the runner limitation only.** Migrations
now apply at startup (`apply_startup_migrations`, called from
`api/main.py`'s lifespan), so a column- or index-only migration *does*
execute and the reasoning above no longer applies to new work — see "The
migration runner applies migrations at startup". The decision it
justified stands: #23 still shipped without a migration file, correctly,
because no database predated that change.

## The migration path had its own race, separate from the bootstrap one

Todo #30. The bootstrap hardening above made `init_db`'s *install* path
race-proof by construction. The *migration* path was not, and the gap
outlived it by several PRs.

`init_db` sampled the database state at the top of the function and acted
on it about thirty lines later. In between, a concurrent caller could
apply the pending migration. The loser then found `pending_migrations`
empty — the racer had already recorded it — skipped the apply branch, and
refused on the stale verdict, against a database that was healthy by then.
Measured at 14% of isolated runs and 27% under load, always
`IncompleteDatabaseError`. The tell was the error naming no table at all
("missing required tables: unknown"), because it recomputes the missing
set when raising, finds nothing missing, and raises anyway.

The same `"ready"`/`"incomplete"` branch structure is documented from the
other side in "Money is integer cents" above, which explains why a
column-only migration cannot execute under this runner at all. Both
entries rest on that control flow — change it and both go stale together.

Not a test-only problem: `get_connection` calls `init_db` on every
request, and every route is a sync `def`, which FastAPI serves from a
threadpool — so concurrent requests run `init_db` in parallel threads.
The window is the first concurrent requests after a migration ships, and
the symptom is a 500 telling the owner their database is unusable and must
be repaired by hand.

The fix re-reads the state before refusing. Deliberately **not** a retry:
a retry would have hidden exactly the class of bug this code exists to
catch, which is why the flaky test was root-caused rather than muted.

**The load-bearing dependency, and the reason this is "by construction"
rather than "less likely":** `_apply_migrations` commits a migration's DDL
and its `schema_migrations` row in ONE transaction. A re-read therefore
observes strictly before or strictly after a migration, never a
half-applied state. Split that into two commits and this fix silently
degrades into a probability argument — so the invariant is pinned by
`test_a_migrations_ddl_and_its_record_commit_in_one_transaction`, asserted
on the statement sequence so it fails deterministically, and parametrized
over both a single-statement and a multi-statement migration. The
multi-statement case is the one that matters: `_statements()` splits a
migration file and executes each statement separately, so "one
transaction" has to hold across N executes. A single-statement test would
pass even if the runner committed per statement — which is precisely the
decay mode the guard exists to reject. Atomicity was measured for both
shapes (~57k samples single-statement, ~35k for a 25-statement migration
watching the last table it creates; zero mixed observations either way).

That pin watches the *runner*, and cannot see the other way the invariant
breaks: a migration **file** containing its own `BEGIN`/`COMMIT`. The
runner can be perfect and a bare `COMMIT;` mid-file still ends the span
early, committing the DDL before it alone and leaving the
`schema_migrations` row in a separate transaction — the exact
half-applied state the re-read assumes cannot exist. The constraint is
invisible from inside the file being written, where explicit transaction
control is ordinary SQL, so
`test_repo_migrations_contain_no_transaction_control` guards the files as
well (a todo). Both halves are needed; neither sees the other's side.

**Two instrument failures worth remembering, both caught before they
misled anyone:**

- The probe that first measured atomicity read the two facts as separate
  autocommitted statements, so the writer's COMMIT could land between
  them. It reported 15 "non-atomic" observations — a signature the
  instrument manufactured itself. Re-run with both reads inside one
  snapshot: zero mixed observations in ~57k samples.
- A 400-run soak of the fix was run *while the working tree was being
  edited*, and its 14 failures were entirely the edit windows. Soak and
  edit cannot share a working tree; commit first, then soak the frozen
  checkout.


## The MCP server runs inside the API process, over HTTP, not as a separate stdio server

The MCP server (then under an earlier package name) used to be spawned per session by the Claude client over
stdio. It is now mounted on the API's own FastAPI app at
`127.0.0.1:8420/mcp/`, behind the same bearer token as every other route.
One launchd job, one checkout, one version, one restart.

Two structural failure modes drove it:

- **Two processes with two lifecycles desync by construction.** An API
  process running older code while newer tool code sits on disk serves
  new tools against an old server, and nothing says so.
- **Per-session Python imports resolve against the session's cwd.** A
  client started from a git worktree can import that worktree's `api/`
  ahead of the main checkout, resolve `REPO_ROOT` to a checkout with no
  `.env`, and fail on `api_token`. Removing the per-session import removes
  the class.

The tools still reach the API over loopback HTTP even though they now run
inside it. Calling the route functions directly would be faster and would
make `q_core_mcp` a client of the API's internals rather than its public
surface — and would quietly change what every round-trip test proves.

Four things measured while building it, each of which would have been a
plausible wrong guess:

- **A mounted ASGI app is invisible to route-level `Depends`.**
  `tools/list` answered 200 with no `Authorization` header at all until
  the mount was wrapped in its own bearer check.
- **Starlette does not run a mounted app's lifespan**, and the streamable
  HTTP transport starts its session manager there. Without the parent
  entering it explicitly, every request fails with "Task group is not
  initialized".
- **`StreamableHTTPSessionManager.run()` refuses a second call on the same
  instance**, so the app is built per-lifespan behind a stable mount
  object rather than once at import. A module-level app can be started
  exactly once per process.
- **The transport rejects a non-loopback `Host` with 421.** Useful — a
  second lock on the local-only rule — but it means `localhost` in a
  client config fails, and tests must use a `127.0.0.1` base URL.
  (2026-09-25: the one Tailscale Serve name, `serve_hostname`, is now
  allowed as well; see the split entry, decision 3.)

Mounting at `/` was tried and rejected: it shadowed every unmatched route,
so `/health` answered 401 from the MCP auth wrapper and an unknown path
became a 500. Mounted at `/mcp`, clients use the trailing slash — `Mount`
matches `/mcp/...` but not the bare prefix, which 307-redirects before
auth runs.

Consequences: the stdio entry point, the stdout-purity module and its
subprocess test, and the `PYTHONPATH`/`PYTHONSAFEPATH` machinery are all
deleted. The MCP package (then under an earlier name, now `q_core_mcp`)
moved from under `mcp/` to beside `api/` so the API process can import
it from the repo root with no path configuration. MCP logs fold into
`data/logs/api.log` — one process, one log, logger name for provenance.

## Document intake: move from `inbox/`, copy from everywhere else

`POST /documents` takes a local file into `data/documents/` and records it.
The spec says the file is **moved**, and for `inbox/` that is
right: it is transient staging for mobile capture, so processing it should
empty it. A file left behind there gets re-processed.

`intake/` is different, and the difference is a constraint rather than a
preference. It holds statements awaiting import and is not to be modified
— so a source there is **copied** and
the original stays exactly where it was.

The asymmetry is therefore deliberate and worth leaving alone:

- **`inbox/` → move.** The directory is meant to drain.
- **anywhere else, including `intake/` → copy.** The original is not ours
  to consume.

If the do-not-modify constraint is ever lifted, the copy branch should be
removed as a decision, not tidied away as a redundant special case — it
reads like an inconsistency precisely because the reason for it lives
outside the code. `api/tests/test_documents_crud.py` asserts both halves,
so removing either one has to be done on purpose.

**De-duplication moves nothing at all.** Identical bytes (SHA-256 of the
file) return the existing record, and no file is moved, copied or written.
That is the case where a move would be destructive and silent in the same
step: the caller gets a valid-looking record back while its source has
quietly disappeared. Asserted by a test that checks the *source still
exists*, not merely that the response looks right.

**Delete re-checks the stored path.** `DELETE /documents/{id}` removes the
row and the file together — no orphan in either direction — but only after
confirming the stored `file_path` is inside `data/documents/`. That path
was written by this API, and it is still a value in a table; trusting it
would turn one bad row into an arbitrary file deletion.

## Open items carried forward

None currently outstanding — see `project-management.json` for active
build todos instead.

## Projects are an entity type, not their own table

`project` joins `person | property | vehicle | pet | account` in
`entities.type`; the title is `entities.name`, the optional description
is `attributes.description`, and `archived` joins the status vocabulary.
Same "generalize, don't proliferate tables" call as insurance-as-account.

Two concrete payoffs, not just tidiness:

- **Boards need no polymorphic owner.** `boards.entity_id -> entities(id)`
  is one real foreign key. A board on the Toyota points at a vehicle; a
  board on a coding project points at a project. A dedicated `projects`
  table would have forced `boards` into the `target_type`/`target_id` pair
  `note_links` accepts — worth it there, where targets genuinely span five
  tables, not worth it here to avoid one enum value.
- **Projects inherit the existing plumbing.** `note_links`,
  `documents.entity_id`, `reminders.entity_id` and
  `transactions.entity_id` all point at `entities`, so notes, documents,
  reminders and spend attach to a project with no new code. "What did this
  project cost me" is answerable through the existing spending endpoints.

Pinned by `test_a_project_inherits_the_existing_entity_plumbing` — if that
ever fails, this trade-off has stopped paying for itself.

## Ticket status moves only through the transition endpoint

`PATCH /tickets/{id}` can change title, description, parent and position.
It cannot change status; `TicketUpdate` has no such field and forbids
extras, so attempting it is a 422. The only door is
`POST /tickets/{id}/transition`, which requires `to_status`, `actor` and
`note`, and writes the `ticket_transitions` row in the same SQLite
transaction as the update.

This is what makes the audit trail trustworthy rather than aspirational.
If status were an ordinary patchable column, the history row would be
something callers are supposed to remember to write — and the one time it
matters most, an agent failing at 2am, is exactly when it would be
missing. Making the transition the only door means a ticket's history
cannot have gaps by construction.

Creation and the atomic claim write their own rows too (creation with
`from_status = NULL`), so the history is complete at both ends:
`MIN(created_at)` over a ticket's transitions equals its creation time.

Corollary worth knowing: `GET /tickets/{id}/transitions` orders by
`(created_at, rowid)`, not `(created_at, id)`. `created_at` has
one-second resolution and `id` is a random uuid4, so an id tiebreak
scrambles same-second events. An id tiebreak is correct for an arbitrary
listing and wrong for a chronological one, where the order *is* the
content.

## The single-concurrent-caller assumption is retired

`api/db.py` used to note that its exists-then-bootstrap race was an
"accepted risk for this single-user, single-process local server". Jyra's
polling agent loop, running alongside an interactive session, is the
second caller that assumption was waiting for.

What actually shipped, which differs from what the Jyra design sketched:

- **WAL is on every connection**, set unconditionally rather than only at
  bootstrap, because `journal_mode` is persisted in the database file and
  a file created before the change would otherwise stay in rollback-journal
  mode forever. The `PRAGMA` is wrapped in `try/except OperationalError`:
  the migration needs a brief EXCLUSIVE lock and can lose to an in-flight
  reader, which is harmless and self-heals on the next uncontended request.
- **`busy_timeout` is 10 seconds, not the 5 the design suggested**, set via
  `sqlite3.connect(timeout=...)` rather than a `PRAGMA`. The reasoning is
  written at `BUSY_TIMEOUT_SECONDS` in `api/db.py` and pinned by a test
  that asserts the literal value, so changing it forces that reasoning to
  be revisited rather than quietly edited. Note a `PRAGMA busy_timeout`
  would silently override the connect-time value.
- **The bootstrap race itself was fixed separately** (a todo), by
  building the database in a private temp file and installing it with
  `os.link`, an atomic create-if-absent. WAL and `busy_timeout` do not
  address that race — they act on an already-created database, while the
  race exists only in the window before the file exists at all.

## Imports are archived, never deleted; two tables, two views, one guard

Decided 2026-09-19. A mis-imported or duplicated statement is *archived* with its
transactions (`archived_at`, migration 0005), never deleted: the
point of the system is that what was imported stays auditable, and
entities already model the same idea with `status = archived`.
Archiving is honoured by every aggregate by construction rather than
by care — there is one read scope.

Archive semantics apply to `transactions` and `statements`. The read
path for each is its view — `active_transactions` and
`active_statements` — and `api/tests/test_archive_scope.py` enforces
that for both, with per-table allow-lists for the reads that must see
archived rows (a row fetched by its own id so it can be unarchived;
the `UNIQUE(account_id, period_start, period_end)` lookup on import,
because an archived statement still occupies its period and the
statement *row* reactivates on re-import while its old transactions
stay archived). Each allow-list entry must name its subject explicitly
and carry no aggregate, and the same function on both lists carries
two different reasons, asserted different — an exemption earned once
cannot be spent twice.

Two things the guard does not do, stated so nobody assumes them: it
reads source, so a stale table name behind one level of indirection
passes it (every aggregate therefore also has a behavioural test that
fails on the outcome); and dedup ignores archived rows, so the
corrected file can be re-imported. `archive_statement` reports which
rows were hand-edited (`edited_at`, set only by an accepted PATCH) so
the caller knows what a re-import will not carry over.

## `init_db`'s per-request cost was measured and left alone

`init_db` runs on **every request** — it is `get_connection`'s first call —
and had grown across three PRs without anyone measuring the total: three
`mkdir(exist_ok=True)` calls, two `refuse_real_path_under_pytest` guards,
and a `_database_state` probe. Each addition was individually trivial.
Nothing had confirmed the sum still was (ticket T-75, from review-1 on #18).

Measured, 2000 iterations, ready database, macOS / Python 3.13:

| | median | p95 |
|---|---|---|
| `init_db`, whole per-request call | **0.188 ms** | 0.207 ms |
| — `_database_state` | 0.168 ms | 0.192 ms |
| — the three `mkdir` calls | 0.011 ms | 0.012 ms |
| — both pytest guards (production path) | 0.001 ms | 0.001 ms |
| Phase 1 equivalent (parent `mkdir` only) | 0.004 ms | 0.004 ms |
| `GET /entities`, whole in-process request | 2.37 ms | 2.70 ms |

**Decision: no change.** The growth factor sounds alarming and the absolute
number is not — it is ~50x Phase 1's call and still well under a
millisecond, a small fraction of an in-process request. For a single-user
local system this is not worth trading for cacheing, and cacheing is not
free: `_database_state` is re-read precisely because `init_db` runs
concurrently, and the stale-state re-read is what stops a racer's freshly
migrated database being rejected as incomplete. Take care with ratios here:
pairing a cost measured on one database with a request time measured on
another is the easiest arithmetic to get wrong.

**If it ever does matter, `_database_state` is the 89% — that is the only
part worth touching**, and touching it means preserving that re-read.

Two notes on method, because both would have produced a wrong number:

- **Do not benchmark this under pytest.** `refuse_real_path_under_pytest`
  returns immediately unless `PYTEST_CURRENT_TEST` is set, so a pytest-hosted
  benchmark measures a `Path.resolve()`-heavy branch production never takes.
  The benchmark asserts the variable is absent.
- **Do not measure against a fresh temp database.** It understates
  `_database_state` against a populated file.

## `trend`'s window is a whole number of calendar months, closed at today

Two changes in #149, both to `GET /trend`'s window, and the second is a
**visible behaviour change**.

**The window opens on a month boundary.** It was
`date('now', '-N months')` — a day-precision cutoff feeding a `GROUP BY`
over calendar months, so it opened partway through a month, that month
was grouped and labelled whole, and the series carried an extra bucket at
the left edge: the same calendar month could report a partial total at
`months=1` and a larger one at `months=2`, decided by a parameter that only
says how far back to look. It is now
`date(anchor, 'start of month', '-(N-1) months')`: exactly N buckets, the
earliest whole.

**The window is closed at the anchor, and future-dated rows are now
excluded — before #149 they produced extra buckets.** There was no upper
bound at all: anchored at 2025-03-20 with `months=2`, a 2025-09 row
produced a third bucket six months ahead. Anyone holding future-dated
transactions will see them leave the series. That is the cost of the
description being true: "the last N months" and "the latest bucket is
month-to-date" are both false while a row from next year can appear, and
shipping the sentence without the cap would have shipped a lie.

**The anchor is a bound parameter, not the literal `'now'`.** The size of
the first bug *is* the day of the month, so no test running once, on an
unknown day, could see it. A test proves the injected clock reaches the
query by anchoring in a different **year** — without that, replacing
`_now_anchor()` with `'now'` passed the whole file, because the real
clock happened to share a month with the anchors the other tests picked.

The window is built **once** and used by both the buckets and
`transfers_excluded_cents`. That figure says how much was excluded *from
this series*; two copies of the expression is the "two call sites staying
in step" property, and they only have to disagree once.

---

## Dollars convert to cents rounded half away from zero

**Decision.** If a database is ever found holding the historical
`transactions.amount` (NUMERIC dollars), the by-hand conversion to
`amount_cents` (INTEGER) is:

```sql
CAST(ROUND(amount * 100) AS INTEGER)
```

SQLite's `ROUND` is **half away from zero**: `0.005` becomes `1` cent and
`0.015` becomes `2`.

**Why not banker's rounding.** Half-to-even is the usual default in
numeric libraries — Python's own `round()` does it — and it is the wrong
default here. The statements this data is transcribed from round half
away from zero, so half-to-even would disagree with the source document
on exactly the values a reconciliation would examine, and disagree in a
way that reads as a **transcription error** rather than a policy
difference.

**Values that discriminate.** `0.005` and `0.015` give `1, 2` under half
away from zero and `0, 2` under half-to-even. Any test or spot-check of
this conversion must use values like those: `12.34` rounds identically
under both rules, so checking it would prove nothing about which rule is
in force.

**This is a by-hand procedure, not a migration** — see the next entry for
why there is no migration to run.

## Migrations stay SQL-only; a stuck database is refused, not migrated

**Decision.** The migration runner accepts `.sql` only. A Python
migration class is **not adopted**. The one gap no SQL migration can
close — `transactions.amount` never renamed to `amount_cents` — is
handled by **refusing to serve** a database in that shape at startup,
naming the by-hand conversion above.

**Why a Python class would have worked, and was still refused.** The
conversion must be a no-op where `amount_cents` already exists, and
SQLite has no conditional DDL: a reference to `amount` fails at prepare
time whether or not a row would be touched. Against a database stamped to
version 6 with no `amount` column, the SQL form raises `no such column: amount`, and
`apply_startup_migrations` is deliberately fatal, so it would have
stopped the API from starting. Python expresses it easily.

**The cost it would carry, measured by building it (#143, closed
unmerged).** Three findings, each of which is the mechanism failing on
first contact with the safeguards:

1. **Every migration guard in this repo parses SQL.** The
   `BASELINE_COLUMNS` baseline globbed `*.sql` and could not see the new
   `0008.py` at all — so the first Python migration would land precisely
   in the blind spot those guards exist to cover. This was the third
   "the excluded category is where the hole is" failure of that run, and
   it occurred *inside the guard built to fix the previous one*.
2. **`_tables_created_by` was about to make a false promise.** It tells
   `init_db` whether migrating will make an incomplete database whole.
   `0008.py` contained a full `CREATE TABLE transactions` but rebuilt
   only when `amount` was present — statically counting that would let
   `init_db` tell a database *missing* that table that migrating restores
   it.
3. **Every SQL-parsing guard would need a second parser**, kept in step
   with the first, forever.

**The trigger for revisiting.** A genuine **data** migration with a
**real beneficiary** — an existing database, holding real data, that
cannot be brought forward any other way. This was not that: zero known
databases are in the stuck shape. If that case arrives, the guards must
learn to read both file types **in the same change**, never afterwards.

**What the refusal must keep doing.** Both directions are load-bearing
and both are tested. It refuses the stuck shape (`amount` present,
`amount_cents` absent), naming the column and the conversion. It does
**not** refuse a database with `amount_cents` and no `amount` — that is
every database created since the rename, and
refusing it would stop the API from starting.
## API error codes remain visible through MCP (2026-09-22)

The owner selected the structured MCP error-code ticket for implementation. API
error envelopes now render as `code: message`, followed by existing field
details and recovery instructions. The code is preserved from the envelope,
not inferred from HTTP status: `not_found`, `invalid_reference`, `conflict`
and `validation_error` remain distinguishable to callers. This is the text
contract of an MCP tool error, not a new MCP result schema; the SDK may
prepend its tool-name context. SDK validation and local tool refusals do not
necessarily carry API codes. Transport errors
and responses without an API code retain their existing fallback messages.
Production-style lazy-client tests exercise the real API's error envelopes.


## 2026-09-22: mobile dictation through authenticated SSH

The owner requested phone dictation using a resident whisper.cpp model on their Mac,
with q-core as the API. This is the concrete mobile need anticipated by the
original local-only decision. Keep both HTTP servers bound to loopback. The mobile client
uses its authenticated SSH channel to reach only the transcription endpoint,
with a dedicated bearer token that cannot authorize other q-core routes.
Tailscale is configured externally for SSH reachability, not q-core exposure.
No webhook, public bind or general tailnet API proxy is introduced. The SSH
account retains its ordinary machine privileges; this design does not sandbox
that account. Normal SSH/terminal operation does not depend on q-core/Whisper.

Select small.en after synthetic coding/noise comparison on the target laptop. The owner
agreed a warm two-second target for typical recordings up to thirty seconds.
Use a persistent per-user native server, not a model-loading CLI per request.
The API normalizes bounded audio, enforces one active request and cleans private
temporary files. Cancellation preserves the busy slot until inference finishes.
No Claude transcription/cleanup or API subscription is involved. The measured
limits and remaining real microphone/phone acceptance live in
[the dictation runbook](transcription.md).


## Private briefing artifacts and conversational inbox (2026-09-23)

The owner selected on-demand Claude daily and Monday-to-now weekly briefs, saved in
q-core with versions, and a browser collection ordered by creation descending.
Inbox actions remain conversational; documents are dated snapshots. No Codex
briefing connector integration or automatic generation is included.

The initial portable format is static HTML without attributes, scripts or
external resources; text diagrams use pre/code. The API validates and screens
content, and the read-only local viewer isolates it in a sandboxed frame with
restrictive CSP. SQLite stores complete immutable revisions alongside current
metadata; existing WAL-safe backups cover all content. Inbox revisions are
separate from artifacts and from Jyra's development tasks. New source reads
expose existing completion records and dated current-plan expectations; they
do not add bank matching, historical plan reconstruction or two-way Calendar
sync. Connector pre-model privacy capability and live Claude acceptance remain
explicit validation gates rather than assumptions from local tests.

### Shared dark web shell (September 23, 2026)

All q-core web destinations use `api/ui.py` for the navigation registry and
server-rendered shell, with shared `api/static/shell.css` and `shell.js`.
Spending, Cash runway, Financial evaluations and Daily briefs (including saved
versions) share a dark palette, content spacing and an accessible modal left
navigation drawer. Dark is fixed for now; no per-page or OS-dependent light mode.
Local filters and revision links remain page-specific. This introduces no build
pipeline, external asset fetch, credentials in HTML or new public API route.

The briefing outer page allows only the fixed shell script by CSP hash and a
data favicon. Its document iframe retains an empty sandbox and its own
no-script/no-fetch CSP. Generated artifact content never enters the shell script.
Browser validation uses synthetic records on an isolated server.

## Briefs read Gmail and Calendar content directly (2026-09-24)

Under the original design, briefs marked Gmail and Calendar unavailable. The pre-model
privacy gate required scrubbed content, and neither Claude connector can
provide it: Gmail's default view returns subjects and snippets, its metadata
view still returns unscrubbed sender identities, and Calendar returns full
event details. The owner decided that a brief must read mail to find what needs their
attention. Banks, lenders and insurers don't send full account numbers or SINs
by email, so ordinary message text is an accepted exposure.

The briefing skill now reads subjects, snippets, thread bodies and event
details read-only. Attachments stay behind server-side scrubbed document
intake. A leaked identifier is never repeated, and stored text remains minimal
summaries under server screening. This supersedes the connector pre-model gate
in "Private briefing artifacts and conversational inbox (2026-09-23)". It does
not change the statement/document redaction boundary.

## Spreadsheet and headerless-CSV layouts join the scrubber

Two reviewed layouts, described by structure only, were added along with a
narrow `.xlsx` reader:

- `.xlsx` is read with the standard library only (no new dependency): one
  sheet, stored values only, refusals for formulas, merged cells, unexpected
  cell types, DTD/entity declarations, non-UTF-8 parts and oversized archives.
  It becomes CSV text and passes through the same tabular scrubber.
- One spreadsheet export header is a reviewed layout. `Tag` is withheld whole
  (account label and number); descriptions go through personal and numeric
  scrubbing; dates, Debit/Credit and numbers are validated per row.
- A headerless five-column CSV shape is accepted only when every row is five columns with
  one date format, exactly one of debit/credit and a numeric balance; a header
  is prepended. A real header with the same names is still unreviewed.
- Any other suffix, or a `.pdf` that is not a PDF, is a named 422
  `unsupported_document_format` instead of a 500.

Unknown layouts still refuse; the general allowed-column set did not widen.

## Notes: title is the first line; API and MCP wired (2026-09-24)

`notes`/`note_links` existed since the first schema with no reader or
writer. `api/notes.py` and seven MCP tools now cover create, get, list
(filter by linked target, substring search, paginated), update, delete,
link and unlink (ticket T-23).

- **No title column.** A note's title is derived from its first non-blank
  line with leading `#`s removed. A stored title would be a second copy of
  the name that drifts as a draft is revised, and a heading line is how a
  Markdown note already names itself. No migration.
- **Revised in place.** PATCH replaces the whole body and stamps
  `updated_at`; there is no revision history. Iterating on a draft is the
  use case, and history can be added later if it proves needed.
- **Lists return previews, not bodies.** `list_notes` gives title, a
  240-character preview and `link_count`; `get_note` returns the full text,
  so listing long plans doesn't flood a model's context.
- **Link targets must exist and be active.** `note_links` has no FK, so the
  API checks the target: entities, documents, reminders and tickets by id;
  transactions and statements through the `active_*` views, since an
  archived row is an undone import. Links are idempotent per (note,
  target). Deleting an entity with linked notes was already refused.
- **Link filters are independent.** `list_notes` takes `target_type`,
  `target_id` or both. An id alone must name something of some linkable
  type or it is a 400, per the id-filter convention (ticket T-08).
- **Known gap:** deleting a document, reminder or ticket does not yet check
  `note_links`, so it can leave a dangling link (entities already refuse).
  Filed as a follow-up bug rather than widened here.

## Provenance SHAs are exempt from the digit screen (2026-09-26)

Canvas documents record where they came from as `provenance: [{repo, sha,
paths}]`, with the full 40-hex SHA from `git rev-parse HEAD` (Jyra
ticket T-58). The privacy screen refuses any run of seven or more consecutive
digits as a possible account number, and most full SHAs contain one, so a
provenance entry would usually be refused. Shortening doesn't help: a random
12-hex prefix still holds such a run about one time in ten.

The owner decided the exemption, narrowly. `is_provenance_sha(path, value)` in
`api/artifact_kinds.py` is true only when the value sits at exactly
`document.provenance[<i>].sha` and fully matches `^[0-9a-f]{40}$`. Such a value
skips `refuse_sensitive_numbers` and `scrub_personal`. Everything else is
screened as before: a `sha` key anywhere else (for example inside a brief's
`sources`), a malformed or uppercase SHA, and the same digits in a title.
`screen_payload` and `screen_diagram` both call the helper, so there is
one definition of the exemption. The key is `provenance`, not `sources`,
because brief documents already use `sources` for `{provider, reference,
summary}`.

The same change made artifacts general: `kind` is `daily`, `weekly`, `page`
or `diagram` (migration 0015), and `api/artifact_kinds.KINDS` holds each
kind's document model, screen and viewer route. A valid kind that is not
registered is refused with a 422. Widening the CHECK rebuilt both
`artifacts` and `artifact_revisions`, because renaming `artifacts` aside
silently re-points the revisions' foreign key at the renamed table.

## Artifact links: entity delete refuses, ticket delete unlinks (2026-09-26)

`artifact_links` connects an artifact to a Jyra ticket or an entity (Jyra
ticket T-59). Like `note_links` it is polymorphic, with no FK on `target_id`, so
the API checks the target exists when linking. The owner decided the delete
behaviour:

- **Entity delete is refused while linked.** The table is registered in
  `ENTITY_REFERENCES`/`_POLYMORPHIC` (only `target_type='entity'` rows
  count), and the refusal names `artifact_links (n)` and says to unlink first
  or archive. This matches notes.
- **Ticket delete removes the ticket's links in the same transaction**, like
  its attachments and transitions. The linked artifacts always remain, with
  their other links. A ticket is work tracking, and a design doc outlives it.

Artifacts still have no delete endpoint, so nothing yet removes links from
the artifact side except `unlink_artifact`. `get_ticket` embeds linked
artifacts capped at 100, with `artifact_count` as the true total, following
the attachments pattern.

## Artifact revisions: stale first, then content (2026-09-28)

`PUT /artifacts/{id}` (revise_artifact), `PATCH /artifacts/{id}`
(edit_artifact) and `PATCH /artifacts/{id}/diagram` (edit_diagram) all write
through `api.artifacts.write_revision` (ticket T-03). Before this, each
screened the new content before checking `expected_revision`. A stale edit
could therefore answer 422, for content or for a server without a privacy
profile, and callers couldn't rely on 409 meaning "reread".

The order, inside one `BEGIN IMMEDIATE`:

1. **The artifact itself.** Missing → 404. A kind the route doesn't edit
   (edit_artifact on a diagram, edit_diagram on anything else, a kind that
   isn't enabled) is refused whatever the revision.
2. **A future `expected_revision` → 409**, on all three routes, for consistency.
3. **Current:** compute and screen the result, and let its refusals (400,
   409, 422, a missing profile) propagate. Then write the revision.
4. **Stale:** the only success is an exact retry, a result equal to
   revision `expected_revision + 1` (payload, actor, note). A result that
   can't be computed or screened now can't be one, so any refusal becomes
   409 `Artifact changed; reread before editing`.

**Request shape always wins.** FastAPI validates the body model before the
handler runs. That covers StoredText's digit guard on actor and note, the
field validators inside diagram operations, and a missing removal
policy, so a malformed request is 422 whatever its revision. That's the
right boundary: such a request is wrong regardless of revision. "409 when
stale" covers everything the handler screens: profile redaction, the html
screen, `screen_diagram`, replacement matching and operation references.

**An exact retry needs the profile.** Stored revisions hold the screened
payload, so recognising a retry means screening the retried request. If the
profile has gone missing since the original write, the retry can't be
verified and is answered as stale (409). Rereading shows the applied edit.

## Jyra ticket keys: KA-12 is an alias for the UUID (2026-09-26)

Tickets carry a human-readable key such as `KA-12` so they can be typed or
said (ticket T-19). The key is the board's `key_prefix`, a hyphen and
the ticket's per-board `number`, composed on read.

- **Alias, not identity.** The UUID stays the primary id. Every stored
  reference -- `parent_id`, history, attachments and their directory,
  note links -- holds the UUID; a key is resolved to it once, at the edge.
  Every route and MCP tool taking a ticket id, and every `parent_id`,
  accepts either form, a key in any case. Unknown keys are `not_found` on
  a path and `invalid_reference` in a body or filter (ticket T-08).
- **Prefixes.** A board derives one from its entity name: initials of two or more words,
  otherwise the first three characters, numbered when taken (`CAN2`).
  `create_board` takes an optional `key_prefix` (`^[A-Z][A-Z0-9]{1,5}$`,
  upper-cased, unique); `update_board` changes it only while the board has
  never had a ticket (`next_ticket_number = 1`), since it is part of every
  key on the board.
- **A key never comes to name a different ticket** (The owner writes keys in
  notes and commits). Numbers are never reused, a prefix changes only
  before a board's first ticket (`next_ticket_number = 1`), and a prefix
  that ever issued a key goes into `retired_key_prefixes` when its board is
  deleted and is never issued again: explicit prefixes naming one are
  refused (409) and derivation, in SQL and Python alike, treats them as
  taken. A prefix that never issued a key protects nothing and is simply
  freed, so a typo'd prefix on a new board can be corrected and reused.
  Found in review of #21: without it a deleted board's `CAN` was derived
  again for the next Canvas board, and its `CAN-1` meant a different
  ticket.
- **Numbers.** `boards.next_ticket_number` is bumped with
  `UPDATE ... RETURNING` inside the create's `BEGIN IMMEDIATE`
  transaction, so concurrent creates serialize and never share a number;
  a unique index on `(board_id, number)` is the backstop. The counter
  never goes down: a deleted ticket leaves a gap and its number is never
  reused. A failed create rolls the counter back with it.
- **Backfill.** Existing tickets are numbered per board by
  `(created_at, id)`. The migration is SQL-only, per "Migrations stay
  SQL-only", so the name derivation exists twice -- a recursive CTE in the
  migration and `derive_key_prefix` for new boards -- and
  `test_the_migration_derives_prefixes_exactly_as_create_board_does` pins
  them equal.
- **Boards cannot move tickets.** That is what makes a key permanent. If
  moving is ever added, the old key must stay resolvable.

# api/

Python (FastAPI) API server. It owns the SQLite database and private files
under `data/`; the mounted MCP server and static read-only pages are clients
of this API. Nothing else talks to the database directly. It binds to
`127.0.0.1` only.

## Status

The implemented domains are:

- entities and relationships, with closed per-type attribute schemas;
- generic documents, server-side extraction and fail-closed personal/account
  redaction;
- financial accounts, statements, transactions, category and merchant-rule
  management, audited corrections, aggregates and source-tracked imports;
- Jyra boards, typed tickets, transitions, claims and attachments;
- reminders, occurrence completion/snooze, `/due`, and one-way Google Calendar
  projection;
- monthly/weekly spending reports, published evaluation summaries, and
  versioned cash-runway/property-cost forecasts; and
- three static read-only pages at `/ui/spending`, `/ui/evals` and
  `/ui/forecast`.

The write API and `/mcp/` require the shared bearer token. A small pinned set
of reporting routes is exempt so the loopback-only pages can load without
putting a token in HTML or browser storage. Those exemptions are read-only.

Statement intake uses `POST /source_imports/preview` and
`POST /source_imports/commit`, with stable source receipts, stale-preview
checks and explicit overlap decisions. The former `POST /import_statement` is
removed (404), not a fallback. See
[source-tracked imports](../runbooks/source-tracked-imports.md).

`GET /due` unions forward-dated entity attributes, relationship `end_date`s
and RRULE-expanded reminders. There is deliberately no
`GET /reminders/due`: `/due` answers “what is coming up,” while
`GET /reminders` lists each defined reminder series once. Reminder creation
validates RRULEs and projects to Google Calendar after committing locally;
provider failures never erase the q-core reminder. See
[Google Calendar reminders](../runbooks/google-calendar.md).

The exact route, tool, schema and migration inventory is generated in
[`docs/SYSTEM.md`](../docs/SYSTEM.md) and guarded against source drift. Generic notes remain designed but unbuilt; documents and
reminders do not.

Three Jyra behaviours are load-bearing and easy to undo by accident:

- **Status moves only through `POST /tickets/{id}/transition`.** It is not
  a patchable column — `TicketUpdate` has no `status` field and forbids
  extras, so trying returns 422. The endpoint writes the
  `ticket_transitions` row in the same transaction as the update, which is
  what makes the audit trail complete by construction rather than by
  everyone remembering. Ticket creation and the claim write their own rows
  too, so a ticket's history has no gaps at either end.
- **`POST /tickets/claim` is the agent loop's entry point**, and its
  conditional `UPDATE ... WHERE status = 'agent_ready'` is the whole
  concurrency guarantee — rowcount decides the winner, never a read
  followed by a write. Returns 204 when the column is drained.
- **`blocked` is deliberately outside the claim query.** A failed ticket
  routed back to an agent-visible column would be retried and fail
  identically, forever.

Attachment bytes live under `data/jyra/<ticket_id>/`, named by attachment UUID.
Models read attachments through `read_attachment` by id; no filesystem path is
part of the MCP response contract. The client's filename is display metadata,
never a path component.

Two behaviours here are deliberate and easy to mistake for bugs:

- The aggregates sum **signed** amounts rather than filtering to debits.
  That is what makes `runbooks/category-taxonomy.md`'s `Transfers`
  category work — a credit-card payment from checking nets to ~0 across
  its two legs instead of double-counting as spending on both.
- After source commit, read back returned transaction IDs. A null category
  means "still needs a category decision", not necessarily "no rule matched". A rule carrying only an `entity_id`
  legitimately leaves `category_id` NULL, and such a transaction still
  needs surfacing or it accumulates silently as permanently
  uncategorized.

Also worth knowing before building on `cost_of_ownership`: it attributes
the **whole** cost of any entity that finances or insures the subject,
not a share of it. One policy account covering two vehicles therefore
contributes its full premium to each, so per-entity figures are sound
but summing them across entities double-counts.

## Notes worth knowing before extending this

- `get_connection`'s `sqlite3.connect(...)` uses `check_same_thread=False`
  — required because FastAPI's threadpool execution can hand a
  connection's open/use/close across different OS threads within a
  single request. Verified necessary via a concurrent-load test during
  Phase 1 review, and re-verified under real concurrency in Phase 2 (see
  below); don't remove it.
- Any new route needing both auth and a DB connection should apply
  `require_token` via `dependencies=[Depends(require_token)]` at the
  router/route level, not as a named parameter dependency placed after
  `Depends(get_connection)` — declaration order matters, and the wrong
  order opens a DB connection before rejecting an unauthenticated request.
- Listings that `ORDER BY` a human-readable text column need
  `COLLATE NOCASE` plus a unique tiebreak column (see `list_entities`);
  ordering by an already-unique key like `id` needs neither.
- `created_at` and `updated_at` both come from SQLite's
  `CURRENT_TIMESTAMP` (`YYYY-MM-DD HH:MM:SS`, UTC, one-second
  resolution). Don't generate one of them Python-side — the formats
  diverge and nothing in the response models catches it.

## Concurrency smoke test

Phase 1 left a note that Phase 2's first real endpoint should carry a
concurrency smoke test, since `TestClient` is single-threaded and can't
exercise `check_same_thread=False`. Done in Phase 2, against a real
`uvicorn` server rather than `TestClient`:

```bash
TOKEN=$(grep Q_CORE_API_TOKEN .env | cut -d= -f2)
seq 1 20 | xargs -P 10 -I{} curl -s -o /dev/null -w "%{http_code}\n" \
  -X POST http://127.0.0.1:8420/entities \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"type": "pet", "name": "Concurrent {}"}'
```

Result: 20/20 `200`, 20 distinct rows persisted, no tracebacks. Repeated
at 60 requests / 25-way parallelism with the same outcome. Re-run this
after any change to connection handling.

**Scope of that result:** it holds for an already-bootstrapped database.
The run above was sequenced after other requests had created the schema,
so it exercised the warm path only.

Cold-start concurrency was a separate problem, and is now fixed. `init_db`
used to gate bootstrap on `if db_file.exists()`, which produced four
distinct failure modes under concurrent first-requests — three loud, one
silent:

- `table entities already exists` — two callers both passed the check and
  both ran `executescript`.
- `no such table: entities` — a caller saw another's 0-byte file
  (`sqlite3.connect()` creates one *before* any schema is written),
  returned early from `init_db`, and queried an empty database.
- a **partial seed** — a caller read between the schema and seed scripts
  and got a complete-looking database holding only part of the seeded categories.
  No exception, no 500, nothing to alert on. Measured at 11 of 300
  concurrent cold-start requests.
- `attempt to write a readonly database` — a caller's connection was still
  open on a file whose inode had been swapped out from under it.

All four are now unreachable on the normal path, by construction rather
than by narrowing. `init_db` assembles the database — schema, seed,
commit — in a private temp file, then installs it with `os.link()`, which
is atomic create-if-absent: it *fails* rather than overwriting, so two
racers can never both install regardless of how they interleave, and no
caller can observe a half-built database because a half-built one never
exists at the real path. See `_install`'s docstring for the two invariants
that have to keep holding; breaking either reopens all four modes.

Verified by `test_concurrent_cold_start_requests_all_succeed` (threads)
and `test_concurrent_cold_start_across_processes` (real OS processes,
since `os.link`'s guarantee is an operating-system one and Jyra's poller
is a separate process). Both assert that every committed row survives, not
merely that no exception was raised — a read-only assertion cannot detect
a clobbered write, and the partial-seed mode above is invisible to any
check weaker than an exact count.

A file that is already at the path and *isn't* a usable database — empty,
truncated, not SQLite, or valid-but-tableless — is never repaired
automatically. It raises a `CorruptDatabaseError` naming the path and
saying to remove it. That is deliberate and is what makes the guarantee
above architectural rather than probabilistic: repairing in place requires
an `os.replace`, which can swap an inode out from under a live connection
(`attempt to write a readonly database`, measured at ~10% of trials before
this). It is also the better default for a system whose job is being a
trustworthy record — such a file is evidence that something went wrong,
and silently replacing it destroys that evidence along with any data it
holds. A 0-byte `data/q-core.db` is exactly what bit this repo once; it
now fails loudly and tells you what to do instead of failing every request
forever with `no such table: entities`.

## Running it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r api/requirements-dev.txt
cp .env.example .env   # then fill in a real Q_CORE_API_TOKEN
uvicorn api.main:app --port 8420
```

To exercise the server by hand without writing to your real
`data/q-core.db`, point it at a scratch database for the run:

```bash
Q_CORE_DB_PATH=/tmp/e2e/q-core.db Q_CORE_DOCUMENTS_DIR=/tmp/e2e/documents \
  uvicorn api.main:app --port 8420
```

## Testing

```bash
pip install -r api/requirements-dev.txt   # once, from the repo root
pytest
```

`pytest` runs `api/tests/` and `mcp/tests/` together, so the dev
requirements now include `mcp/requirements.txt`. Installing only
`api/requirements.txt` leaves the MCP SDK missing and the whole suite
fails to collect.

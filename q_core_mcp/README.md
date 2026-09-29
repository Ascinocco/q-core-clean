# q_core_mcp/

MCP server for clients configured on the q-core Mac. It is a thin client of
`api/` — every tool is one HTTP call to `127.0.0.1`, with no database access
or independent state.

## Tool domains

The registry is split by domain under `q_core_mcp/tools/`:

| Module | Surface |
|---|---|
| `entities.py` | entity and relationship reads/writes |
| `documents.py` | scrubbed extraction, document registration and reads |
| `financial.py` | ledger reads/corrections, merchant rules and spending reports |
| `source_imports.py` | preview/commit for source-tracked statement imports |
| `forecast.py` | versioned forecast plan and read-only scenarios |
| `due.py` | statement coverage and the cross-domain due list |
| `reminders.py` | reminder CRUD, completion and snooze |
| `google_calendar.py` | connection health, OAuth start and full reminder sync |
| `jyra.py` | boards, tickets, transitions, claims and attachments |

Do not maintain a second exhaustive tool-name list here. The exact registry is
generated into [`docs/SYSTEM.md`](../docs/SYSTEM.md), while
`q_core_mcp/tests/test_tool_surface.py` pins every registered tool and its MCP
risk annotations. Tool descriptions carry the parameter-level contract a
model needs at call time.

### The intake loop

Use server-scrubbed source text and stable source locators. Preview first,
resolve overlaps with evidence, then commit with the review token. Read back
returned IDs through paginated `list_transactions`; propose categories and
rules for null-category rows. Obtain approval before learning rules.
See [the intake contract](../runbooks/source-tracked-imports.md).

`import_statement` is retired. Reconnect clients with cached tool lists; stale
calls cannot write. After uncertain commits, preview again to recognize source
occurrences safely rather than blindly retrying.

### Behaviours the descriptions have to carry

These produce a wrong answer rather than an error, so every one is stated
in the tool description *and* pinned by a test, several against the real
API rather than the prose:

- `update_entity` **replaces** the whole `attributes` object rather than
  merging. Read first, send the full set back.
- `create_relationship` **returns the existing row** for a duplicate, so
  success does not always mean something changed (a todo).
- `update_transaction` is **correction-only**: date, description and
  amount come from the imported statement and cannot be changed.
- `apply_transaction_as_rule` learns the transaction's **exact
  description**, matched as a substring — a rule from `ACME FUEL 41` does
  not cover `ACME FUEL 99`. It handles that string, not the merchant.
- `spending_summary` totals are **signed** sums, so a card payment nets
  to ~0 across its Transfers legs; uncategorized rows group under a
  **null** key rather than being omitted.
- `trend` needs **exactly one** of `category_id`/`entity_id`.
- `cost_of_ownership` is **whole-cost, not pro-rata** — one policy over
  two cars contributes its full premium to each, so these must not be
  summed across entities.

### Boundaries

The MCP server is intentionally a client of the public API, even though it is
mounted in the same process. It has no SQLite access. Category-tree mutation
is not exposed as a routine model tool; statement creation is owned by
source-tracked commit; and there is no generic “run SQL” escape hatch.

Wrong merchant rules can be corrected with `update_merchant_rule`, whose audit
history is visible through `merchant_rule_history`. Imported rows are changed
only through explicit correction/reapply tools, with reapply refusing to
overwrite an existing classification.

## Layout

```
q_core_mcp/
├── requirements.txt
├── client.py    — async httpx wrapper over api/, error mapping
├── tools/       — domain modules registered by tools/__init__.py
├── server.py    — build_server(): the MCPServer and its tool registry
└── tests/
```

It sits beside `api/` rather than under an `mcp/` directory. That is
load-bearing: a directory named `mcp` on `sys.path` shadows the SDK's own
import name, which is why the package used to be nested one level down
with `mcp/` deliberately kept free of `__init__.py`. With nothing named
`mcp` on the path, the hazard is gone by construction instead of by a
comment asking people not to trip it.

There is no entry point here. `api/main.py` builds the server and mounts
it; `server.py` only assembles it.

## How it runs

**Inside the API process**, mounted on the same FastAPI app at
`127.0.0.1:8420/mcp/`. One launchd job, one checkout, one version, one
restart — see `runbooks/decisions-log.md` for why this replaced a separate
stdio server.

The tools still reach the API over loopback HTTP rather than calling its
functions directly. That keeps `q_core_mcp` a client of the API's public
surface rather than of its internals, and keeps every round-trip test
proving what it proved before.

## Setup

```bash
pip install -r api/requirements-dev.txt
```

That pulls in `q_core_mcp/requirements.txt` (the MCP SDK and `httpx`) as
well as the API's own dependencies. The SDK is also in
`api/requirements.txt`, because the API process imports it at runtime now.

Register the endpoint with the Claude client — `.mcp.json` in this repo
already does it:

```json
{
  "mcpServers": {
    "q-core": {
      "type": "http",
      "url": "http://127.0.0.1:8420/mcp",
      "headers": { "Authorization": "Bearer ${Q_CORE_API_TOKEN}" }
    }
  }
}
```

Three details that are not arbitrary:

- **`127.0.0.1`, never `localhost`.** The transport has DNS-rebinding
  protection and answers **421** to any non-loopback `Host` header. That
  is a second lock on the local-only rule, and it will reject `localhost`.
- **Either spelling works, with no redirect.** Starlette's `Mount` matches
  `/mcp/...` but not the bare prefix, so `/mcp` would fall through to the
  router's redirect — *before* the auth check, meaning an unauthenticated
  request would be redirected rather than refused. A middleware rewrites
  the bare path ahead of routing so `/mcp` and `/mcp/` behave identically.
- **The token is not committed.** `${Q_CORE_API_TOKEN}` is expanded by
  the client from the environment. The endpoint requires the same bearer
  token as every other route: without it, `/mcp/` returns the API's
  ordinary 401 envelope.

## Running it

```bash
launchctl kickstart -k gui/$(id -u)/tech.q-core.api   # restart
launchctl print gui/$(id -u)/tech.q-core.api          # status
```

Or by hand:

```bash
uvicorn api.main:app --host 127.0.0.1 --port 8420
```

If **every** tool fails at once the API is down — and now that the MCP
endpoint is part of it, "the API is down" and "the tools are gone" are the
same event rather than two processes disagreeing about which is running.
A single tool failing is an ordinary API error (a 404, a validation
failure) and its message says which.

Logs go to `data/logs/api.log` alongside the API's own — one process, one
log, with the logger name distinguishing them. There is no separate
`mcp.log` any more.

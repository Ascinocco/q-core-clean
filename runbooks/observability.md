# Observability: logs, request ids, launchd

Where the two servers write their logs, what the format is, how to read
them, and how the API server gets started and kept running.

The point of all of this: when the owner reports a problem, the next session
should be able to debug it by reading files off disk — not by asking them
to restart anything, reproduce it with a terminal open, or paste output.

## Where the logs are

| File | Written by | Contains |
|---|---|---|
| `data/logs/api.log` | the API server | one JSON object per line: every request, every unhandled exception, plus uvicorn's own startup/shutdown/error lines |
| `data/logs/api.launchd.out` / `.err` | launchd | raw stdout/stderr — **fallback only** |

`data/` is gitignored in full (`.gitignore` line 2), so `data/logs/` is
already covered — no separate rule, and nothing here is ever committed.

The two `.launchd.*` files exist for failures that happen *before* the
app's own logging is configured: an import error, a missing
`Q_CORE_API_TOKEN`, a broken venv. Those would otherwise leave no trace
anywhere. They're kept separate from `api.log` on purpose — a Python
traceback landing mid-stream in a JSON-lines file would break every parser
pointed at it. **If `api.log` looks empty or stops, read these next.**

## The format

One JSON object per line. Always present:

| Field | Example |
|---|---|
| `timestamp` | `2026-09-19T01:36:13.454766+00:00` (UTC, ISO 8601) |
| `level` | `INFO`, `ERROR` |
| `logger` | `api.request`, `uvicorn.error`, `q_core_mcp` |
| `message` | `GET /health 200` |

Present when applicable:

| Field | When |
|---|---|
| `request_id` | any line logged while handling a request |
| `method`, `path`, `status`, `duration_ms` | `api.request` lines |
| `traceback` | any record logged with an exception attached |

A traceback's newlines are JSON-escaped, so **one record is always exactly
one line** — `grep` and `jq` stay reliable even around a crash.

### Reading them

```bash
# Follow along live, formatted.
tail -f data/logs/api.log | jq .

# Everything that failed.
jq 'select(.level == "ERROR")' data/logs/api.log

# The slowest requests.
jq -s 'sort_by(-.duration_ms) | .[0:20] | .[] | {path, duration_ms, status}' data/logs/api.log

# Everything belonging to one request — see "Request ids" below.
jq 'select(.request_id == "0123456789ab")' data/logs/api.log

# Read the actual traceback of the most recent error.
jq -r 'select(.traceback) | .traceback' data/logs/api.log | tail -40
```

`jq` treats the file as a stream of objects, so none of these need the
file to be valid JSON as a whole.

## Request ids

Every response carries an `X-Request-ID` header, and every log line emitted
while handling that request carries the same value as `request_id`. So a
report of "this call failed" becomes a single `jq` filter, as long as the
id gets quoted back.

- An **inbound** `X-Request-ID` is reused rather than replaced, so one id
  are all in `api.log`, since the MCP endpoint runs in the API process.
- **A 500 from an unhandled exception carries no `X-Request-ID` header.**
  Starlette's `ServerErrorMiddleware` sits outside this app's middleware
  and sends its own response, so the header never gets attached. The ERROR
  line in `api.log` *does* carry the id — correlate by `path` and
  timestamp instead. Everything else (including 4xx and handled 5xx)
  has the header.

## Log level

`Q_CORE_LOG_LEVEL` in `.env`, default `INFO`. Applies to both servers.

```bash
echo 'Q_CORE_LOG_LEVEL=DEBUG' >> .env
launchctl kickstart -k gui/$(id -u)/tech.q-core.api   # API: restart to apply
```

The MCP server picks it up when its client (Claude Desktop/Code) next
starts it.

Rotation is fixed in code: 10 MB per file, 5 backups, per server. Worst
case on disk is about 120 MB.

## What is deliberately *not* logged

Both of these are load-bearing against CLAUDE.md's rule that SSNs and full
account/routing numbers never reach an LLM or get stored:

- **No request bodies.** `api/privacy.py` refuses account-shaped numbers in
  model-visible persisted text, but it is not a general log sanitizer and does
  not cover every request field. Bodies are therefore not logged at all.
- **No query strings.** Only `path` is recorded. A query string can carry
  a person's name or an account number as a search term.

This is also why **uvicorn's access logger is switched off** rather than
routed into the file. Uvicorn renders its access line through
`get_path_with_query_string`, so every one of its lines contains the query
string by construction — measured, not assumed. The middleware's own
`api.request` line already carries more (duration, request id) and
without the query string, so nothing is lost. httpx and httpcore are held at
WARNING for the same reason: httpx logs every request at INFO as
`HTTP Request: GET <full URL>`, and the MCP tools' loopback calls carry search
terms in the query (`list_notes`' `q=`). `uvicorn.error` — startup,
shutdown, bind failures, tracebacks — *is* routed in, which is the part
with real diagnostic value.

Note that `configure_logging` attaches to the **root** logger, so any
third-party library's logging also lands in these files. If a future
dependency logs URLs or payloads, it will land here too — worth a thought
when adding one.

## Traces and OTLP export (the server)

On the server the API also sends **traces and logs** to the shared
observability stack (the host's own documentation): Tempo for traces,
Loki for logs, and Grafana at `https://<server>.<tailnet>.ts.net:8443`.
`api/telemetry.py` does this.

- **Off unless configured.** It's enabled only when
  `OTEL_EXPORTER_OTLP_ENDPOINT` is set. The NixOS module sets it from
  `services.q-core.telemetry.otlpEndpoint`, which is `http://127.0.0.1:4318`
  on the server. Tests, and the Mac under launchd, run with no telemetry at all.
- **What's traced:**
  - every HTTP request, named by its route template (`GET /entities/{entity_id}`);
  - each MCP tool call (`mcp.tool <name>`, with `mcp.outcome` ok or error), linked
    through httpx to the API request it makes;
  - the intake stages (`intake.extract`, `intake.preview`, `intake.commit`);
  - transcription (`transcription.normalize`, `transcription.infer`).

  Tempo derives rate, error and latency metrics from these spans.
- **Correlation:** a line in `api.log` written inside a traced request carries
  `trace_id` and `span_id`. In Loki, `{service_name="q-core"} | trace_id="<id>"`
  finds them.
- **Privacy: an allowlist, not a hope.** Instrumentation libraries record
  full URLs (query strings), exception messages and stack traces. So both
  exporters are wrapped:
  - span and event attributes not in `SPAN_ATTRIBUTES` / `EVENT_ATTRIBUTES`
    are dropped;
  - status descriptions are dropped;
  - the MCP SDK's own span loses the client-supplied tool name;
  - log records are exported only from q-core's own reviewed loggers
    (`LOG_LOGGERS`: `api`, `api.request`, `api.error`, `api.documents`,
    `uvicorn.error`), keep only `LOG_ATTRIBUTES`, and their message goes
    through the account-number scrubber.

  What's left is methods, status codes, routes, tool names, outcomes, and
  counts, kinds and sizes. `api/tests/test_telemetry.py` pins the lists and
  sends invented card numbers and names through every path. **Adding a key
  to an allowlist is a privacy decision**; record why in the PR.

To debug on the server, see the observability stack's query recipes. For example:

- recent traces: `{resource.service.name="q-core"}` in Tempo;
- failing tool calls: `{resource.service.name="q-core" && name=~"mcp.tool.*" && status=error}`.

## launchd: running the API server

The plist lives at `ops/launchd/tech.q-core.api.plist`. It is **not
installed automatically** — installing it is the owner's step:

```bash
# Install (symlink, so a git pull picks up changes to the plist).
ln -sfn ~/Projects/q-core/ops/launchd/tech.q-core.api.plist \
        ~/Library/LaunchAgents/tech.q-core.api.plist

launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/tech.q-core.api.plist
```

Day to day:

```bash
launchctl kickstart -k gui/$(id -u)/tech.q-core.api   # restart
launchctl print gui/$(id -u)/tech.q-core.api          # status, exit codes
launchctl kill SIGTERM gui/$(id -u)/tech.q-core.api   # stop (stays loaded)
launchctl bootout gui/$(id -u)/tech.q-core.api        # uninstall
```

`launchctl load`/`unload` are the older spelling of `bootstrap`/`bootout`
and still work, but `print` gives far more useful output than `list` when
something is wrong — it shows the last exit status and the resolved paths.

What the plist does and why:

- Runs `python -m api.run`, not the `uvicorn` CLI. Uvicorn's
  `--log-config` takes a *file* path, so using the CLI would mean keeping
  a second hand-maintained copy of the logging config on disk. `api/run.py`
  passes the dict directly — one source of truth.
- `127.0.0.1` is explicit inside `api/run.py` (`listening_sockets`) rather
  than left to a default. The loopback bind keeps the LAN out. Since the
  split, the tailnet reaches only the private Serve socket, and every
  request on either listener passes `api/serve_gate.py` (decisions-log entry
  "q-core split"). A default is a weaker guarantee than an
  argument.
- `KeepAlive` with `SuccessfulExit false` restarts on a crash but not
  after a deliberate stop.
- Uvicorn's `--reload` is deliberately off: under `KeepAlive` it would be
  a second supervisor for the same process.
- **Paths in the plist are absolute and hardcoded** to
  `/path/to/q-core`. launchd has no notion of a working
  directory to resolve against at load time. If the repo ever moves, this
  file needs editing.

## Verifying it actually works

```bash
# Every route needs a token since the q-core split; read it from .env without echoing it.
TOKEN="$(sed -n 's/^Q_CORE_API_TOKEN=//p' .env)"
curl -si -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8420/health | grep -i x-request-id
tail -1 data/logs/api.log | jq .
```

If the header is present and the log line's `request_id` matches it, the
whole chain is working.

## Why two logging modules instead of one

The MCP server has no logging module of its own any more. It runs inside
the API process (mounted at `/mcp`), so `api/logging_config.py` configures
both and every line lands in `data/logs/api.log`, distinguished by logger
name. The old MCP-package `logging_config.py` (from before the q-core rename) existed to guarantee nothing
was ever written to stdout — the stdio transport carried JSON-RPC there,
so one stray `print` corrupted the stream. Over HTTP stdout is just
stdout, so that constraint and the subprocess test that enforced it are
both gone. See `runbooks/decisions-log.md`.

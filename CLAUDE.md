# q-core

**q-core** is a personal-services backend: the API, the MCP server, the
SQLite database, the financial pipeline, Jyra (boards and tickets), Whisper
transcription and the life skills. Two modes of work happen here, and
it matters which one a request implies:

- **Operating it** — "add the new car," "what's due this month," "log this
  receipt," "how much did the Toyota cost me this year." These are
  read/write requests against the running system via its MCP tools, not
  invitations to change code or schema. Default to this interpretation
  when a request is ambiguous.
- **Building q-core** — schema changes, API server work, MCP server work,
  skills. Normal engineering practice applies here. Check
  `runbooks/decisions-log.md` before re-litigating an architecture call
  that's already been made — either follow it or raise why it needs
  revisiting, don't silently drift from it.

## Orientation

- `runbooks/INDEX.md` — knowledge base index (conventions, schemas, prior
  decisions). Check this before assuming an answer isn't written down
  somewhere; it's the stopgap between ad hoc skills/MCP tools and having
  actual institutional memory.
- `db/schema.sql` — current schema (source of truth, versioned).
- **Development work can be tracked on a Jyra board, reached over MCP.** Read
  it with `get_board` / `list_tickets`; file findings with `create_ticket`
  (`bug` for a defect in shipped behaviour, `task` for work or a decision;
  markdown description that says what done looks like); move status only
  with `transition_ticket`, whose note must say why.
- `api/` — Python (FastAPI) API server. Owns the SQLite DB. Nothing else
  talks to the DB directly.
- `q_core_mcp/` — MCP server. Thin client of `api/` — no DB access of
  its own. Sits beside `api/` rather than under `mcp/` so both import from
  the repo root with no path configuration, and so no directory named
  `mcp` can shadow the SDK's own import name.
- `plugin/` — the **q-core plugin**, installed user-wide on every machine
  (runbooks/plugin.md). `plugin/skills/` holds the life skills
  (statement-intake, document-intake, daily-brief, reminder-digest,
  email-triage, financial-evals) and `plugin/runbooks/` the runbooks they read
  at run time; `runbooks/` links to those. Only files inside `plugin/` reach
  an installed copy, so a skill refers to them as `${CLAUDE_PLUGIN_ROOT}/...`.
  Don't put skill content anywhere else. To try an edit before it is pushed,
  start Claude with `--plugin-dir plugin`.
- `data/` — gitignored. The actual SQLite DB file and documents (plaintext
  — see runbooks/decisions-log.md's "Document storage" entry for why) live
  here at runtime. Never committed.

## Financial workflow routing

- Cash runway, recurring bills, property costs or snowball payment scenarios
  → read `runbooks/cash-forecast.md`, then use `get_forecast_plan` and
  `get_cash_forecast`. Persist changes only when requested; distinguish
  projected balances, verified snapshots, estimated payments and unknowns.

- “Process/import the new folder in intake through our financial audit system”
  → the plugin's `statement-intake` skill (`plugin/skills/statement-intake/SKILL.md`). Limit discovery to the named
  folder; filenames only, then server-side scrubbed extraction. Preview and
  review source overlaps before commit. “Audit/preview only” means no writes.
- “Run financial evals”, “test classification/deduplication”, “compare eval runs”
  → the plugin's `financial-evals` skill (`plugin/skills/financial-evals/SKILL.md`). This is offline measurement,
  not permission to import or change the live ledger. Results: `/ui/evals`.
- See `runbooks/eval-workflow.md` for commands. The skills reach other
  machines through the plugin, not through MCP.

## Daily and weekly briefs

Create/revise a daily brief or week-to-date report, or work through the current
review inbox → the plugin's `daily-brief` skill (`plugin/skills/daily-brief/SKILL.md`) and
`runbooks/briefings.md`. Claude-only, on demand. Artifacts are private,
versioned snapshots; current inbox state is separate. The read-only collection
is `/ui/briefs`. Claude may read Gmail and Calendar content directly for briefs
(read-only, no attachments, never repeat a leaked identifier); see the
runbook's privacy section.

## Reminder and Calendar routing

- Create, list, complete and snooze reminders through the reminder MCP tools;
  use the plugin's `reminder-digest` skill for “what is due” workflows
  across reminders, entity dates and relationship dates.
- Google Calendar is a one-way notification projection. For OAuth setup,
  credential storage, sync semantics or recovery, read
  `runbooks/google-calendar.md`. q-core remains authoritative; do not treat a
  Google edit or deletion as a q-core update.

## Safety and operation

- Nothing here talks to a Claude API key for routine, on-demand use — this
  runs on the interactive subscription (Claude Code/Desktop/mobile), one
  human present per action. A Claude API key is reserved narrowly for
  genuine one-off batch jobs (e.g. importing a multi-year receipt
  backlog), run deliberately, not as a standing service.
- SSNs and full account/routing numbers are never sent to any LLM and
  never stored in the DB. Redact **before any model reads the document**:
  extraction and scrubbing both run server-side (`extract_document_text`),
  and the intake skill consumes only scrubbed output. The older wording
  put redaction "at the intake-skill level, before extraction output is
  persisted", which cannot satisfy the rule when the intake skill *is* a
  model reading the PDF — by then the number has already been sent.
- Personal names/address variants are also scrubbed server-side using a
  locally reviewed private profile. See `runbooks/document-privacy.md`.
  Never read that profile or raw documents into model context. Missing
  profiles and ambiguous personal sections are refusals, not permission
  to bypass extraction. This is not universal name recognition.
- The same boundary applies to development artifacts: never put raw statement
  text, the private redaction profile, email bodies, account/card/SIN values,
  or copies of private logs into prompts, transcripts, Jyra tickets, commits,
  PR descriptions, screenshots or test snapshots. Committed regression
  fixtures must be invented. Scrubbed financial text is safer, not anonymous;
  keep it local/private unless the owner explicitly approves a specific disclosure.
- Stored documents are read through `extract_registered_document_text(id)`.
  Document and attachment metadata deliberately withhold raw filesystem paths;
  never recover a path from the database or use shell/file tools as a bypass.
- The API binds to loopback only; it is published to the tailnet solely
  through Tailscale Serve, under the access rules and authentication recorded
  in the decisions log's "q-core split" entry. Concretely:
  - Serve proxies to a private Unix socket (`serve_socket`, directory 0700)
    that only tailscaled and the service user can reach. There, an
    allowlisted Tailscale identity opens only the browser pages, and
    anything else needs a token: a named, scoped client token, or the
    server's own service token (configuration, retired by rotation).
  - On the TCP service port every request needs a token; the only
    exception is the state-authenticated OAuth callback.
    `api/serve_gate.py` refuses everything else before routing, on both
    listeners.
  - Tokens are created and revoked with `python -m api.tokens`, or at
    `/ui/tokens` by a person through Serve only (never by another token).
    Only hashes are stored, and a token is never logged or echoed.
  - Nothing binds to the LAN or a public address. Whisper stays
    loopback-only and is never published.
  - Do not add another listener, a Funnel, a webhook or a broad proxy. Any
    change to this exposure is a new decisions-log entry, not a config edit.
- Remote shells and intake copies use OpenSSH over the tailnet, key-only.
  Tailscale SSH is not enabled on q-core hosts (same entry).
- The mobile-client SSH exception still stands until ticket T-02 moves
  dictation to the Serve URL with a `transcription` token. Until then its
  authenticated SSH connection may request `/transcriptions` with the scoped
  phone token, and nothing else. See `runbooks/transcription.md`.

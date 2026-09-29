# q-core

q-core (split from an earlier repository on 2026-09-25) is a personal, single-user system for entities (properties, vehicles,
people, pets and accounts), documents, reminders and financial tracking. The
FastAPI application owns the SQLite database and private files; a mounted MCP
server is the primary interface for Claude and other repo-aware agents.

It is deliberately single-user. The server binds only to loopback and runs on
one machine: under launchd on the Mac today (`ops/launchd/`), moving to the
NixOS home server through this flake's module
([`runbooks/deploy-nixos.md`](runbooks/deploy-nixos.md)).
The tailnet reaches it only through Tailscale Serve, and every request needs a
credential (decisions log, "q-core split"). Google Calendar is the one intentional external projection: q-core
reminders are copied to a dedicated calendar so phone notifications work,
while q-core remains authoritative.

## Start here

- Agent operating/building rules: [`CLAUDE.md`](CLAUDE.md)
- Maintained procedures and architecture: [`runbooks/INDEX.md`](runbooks/INDEX.md)
- Generated implementation inventory: [`docs/SYSTEM.md`](docs/SYSTEM.md)
- API setup and technical notes: [`api/README.md`](api/README.md)
- MCP architecture and tool domains: [`q_core_mcp/README.md`](q_core_mcp/README.md)
- launchd installation and restart commands: [`ops/launchd/README.md`](ops/launchd/README.md)

The live development roadmap is the Jyra board identified in `CLAUDE.md`.
There is intentionally no second `ROADMAP.md` to drift from it.

## Local pages

These pages open in a browser through the Tailscale Serve URL
(`https://<server>.<tailnet>.ts.net`) for an allowlisted Tailscale identity
(decisions log, "q-core split"). On the loopback service port
they need a `full` token like every route, so a browser there is refused.

- `/ui/spending`: monthly/weekly spend, coverage and merchant drill-down
- `/ui/evals`: published financial evaluation results
- `/ui/forecast`: cash runway and property-cost scenarios
- `/ui/briefs`: saved daily and weekly briefs
- `/ui/tokens`: create and revoke the per-machine API tokens

The write API and MCP endpoint remain bearer-authenticated. The pages are not
an administrative client; financial corrections, imports, plan changes and
reminder management continue through MCP/API workflows.

## Common workflows

- Put a new statement folder in `intake/` and ask to “process it through the
  financial audit system.” The statement-intake skill extracts and redacts on
  the server, previews overlaps, and requires review before commit.
- Ask to “run the financial evals” to use the offline regression corpus and
  publish a summary for `/ui/evals`. Evals never authorize live ledger writes.
- Ask for a reminder normally. Connected reminders project to the dedicated
  Google calendar with default notifications one day and one hour beforehand;
  a prompt can override those offsets.
- Ask about runway, recurring bills, property costs or snowball scenarios to
  use the versioned forecast plan. Read-only scenarios do not alter that plan.

Copy `.env.example` to `.env`, install `api/requirements-dev.txt`, and see the
API and launchd READMEs for foreground and long-running startup commands.

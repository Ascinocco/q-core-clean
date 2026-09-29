# Runbooks index

One line per file — the knowledge base that stands in for skills/MCP tools
that don't exist yet, or captures conventions those tools should follow.
Check here before assuming something isn't written down.

- [Deploy on NixOS](deploy-nixos.md) — the flake's package and `services.q-core` module: what it runs, paths, secrets, sudo CLI, backups, VM test, and changing dependencies.
- [q-core plugin](plugin.md) — how the life skills, their runbooks and the MCP connection reach every machine, install and token setup, and how the plugin keeps itself current.
- [Decisions log](decisions-log.md) — architecture decisions made and why, so they don't get relitigated by accident.
- [Entity attribute schemas](entity-attribute-schemas.md) — per-type shape of the `attributes` JSON blob, which date attributes point forward (what `/due` reads) versus record history, and the `entity_relationships` vocabulary (owns, leases, finances, insures, ...).
- [Category taxonomy](category-taxonomy.md) — fixed, two-level transaction category tree; category describes kind of spending, not which asset.
- [System document](../docs/SYSTEM.md) — what exists and how it fits together, and an inventory generated from the tree (routes, tools, schema, migrations, skills, tests) that `tests/test_system_doc.py` keeps honest — regenerate it before opening a PR that adds any of those.
- [Merchant rules conventions](merchant-rules-conventions.md) — match precedence (longest pattern wins); rule-vs-override behavior still open.
- [Skill conventions](skill-conventions.md) — the shared fences every SKILL.md carries verbatim (the pagination drain, NEVER-lists ordered by reversibility, stop-and-report, redaction tokens) and why they are copied rather than referenced.
- [Statement intake](statement-intake.md) — per-source document quirks and the normalization contract the intake harness must satisfy before source-tracked preview/commit.
- [Source-tracked imports](source-tracked-imports.md) — source occurrence receipts, read-only preview and atomic commit with explicit cross-export overlap decisions; the only production statement-import path.
- [Financial evaluations](financial-evaluations.md) — frozen reviewed labels, isolated classifier/import replay, agent extraction trials, private artifacts and version comparisons.
- [Eval workflow](eval-workflow.md) — natural-language skill routing, routine regression commands, read-only results page and publication privacy boundary.
- [Cash forecast](cash-forecast.md) — dated balance snapshots, recurring assumptions, property costs, read-only scenarios and versioned MCP plan updates.
- [Google Calendar reminders](google-calendar.md) — one-way reminder projection, Cloud/OAuth setup, Keychain storage, sync semantics and recovery.
- [End-to-end intake evaluations](intake-flow-evaluations.md) — fresh source-aware submissions, locator/label scoring, real disposable import sequences, repeat safety and unresolved overlap refusals.
- [Financial corrections](../docs/financial-corrections.md) — audited MCP repairs for statement periods and transaction membership, with expected-value checks and safe retries.
- [Document privacy](document-privacy.md) — local names/address profile, server-side redaction, fail-closed handling and safe setup.
- [Observability](observability.md) — where the logs are (`data/logs/`), the JSON-lines format, request ids, what is deliberately not logged, and the launchd commands.

- [Local dictation](transcription.md) — resident Whisper setup, scoped audio endpoint, SSH mobile boundary, latency and recovery.

- [Daily and weekly briefs](briefings.md) — Claude-only on-demand reports, private versioned artifacts, conversational review inbox, source coverage and local viewer.


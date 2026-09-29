# q-core agent entry point

Read `CLAUDE.md` before operating or building q-core. It is the shared source
for mode boundaries, privacy rules, backlog and workflow routing. Read
`runbooks/INDEX.md` to locate the relevant maintained procedures.

Skills are in `plugin/skills/*/SKILL.md`, shipped to every machine as the
q-core plugin (runbooks/plugin.md). Read the complete applicable skill before
acting; these same files serve Claude and other repo-based agents.

- New statement or intake folder; “process through the financial audit system”:
  `plugin/skills/statement-intake/SKILL.md`.
- Financial regression evals, extraction trials, comparisons or publishing eval
  results: `plugin/skills/financial-evals/SKILL.md`.
- Other workflows: use the matching skill under `plugin/skills/`.

Evals are offline and do not authorize live financial changes. Statement
processing uses server-side scrubbed extraction, never raw document reads into
model context. Preview-only requests authorize no live writes.

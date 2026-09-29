# The q-core plugin: life skills on every machine

The life skills (statement and document intake, daily and weekly briefs,
reminders, email triage, financial evals), the runbooks they read and the
q-core MCP connection ship as one Claude Code plugin from this repository
(`plugin/`), listed by the repository's own marketplace
(`.claude-plugin/marketplace.json`). It is installed user-wide on every machine
(split part 3; decisions log, "q-core split", decision 2).

## Install on a machine

```sh
claude plugin marketplace add Ascinocco/q-core-clean
claude plugin install q-core@q-core \
  --config server_url=https://q-core.<tailnet>.ts.net \
  --config server_ssh=<user>@<server> \
  --config server_checkout=~/q-core
```

Then set the token in a session with `/plugin configure q-core`. The prompt is
masked, and the value goes to Claude Code's secure storage, never
`settings.json` or your shell history. That's the system keychain on macOS,
and Claude Code's private credentials file (`~/.claude/.credentials.json`) on
Linux, which has no keychain. Use this machine's own `full` token from `/ui/tokens`
(through Serve) or `python -m api.tokens create <machine>` on the server.

- On the server itself: `server_url=http://127.0.0.1:8420`, and leave out
  `--config server_ssh=` entirely. An empty value is refused, and unset means
  "run server steps locally".
- Also turn on the marketplace's auto-update (`/plugin`, then Marketplaces). It
  is a second path; the plugin does not depend on it.

Check the result: `claude plugin list --json` shows `q-core@q-core` enabled
with the commit-derived version, and `claude mcp list` shows the plugin's
`q-core` server connected.

## How it stays current

- **No `version` field anywhere.** Claude Code then versions the plugin by the
  commit it was installed from, so every push is a new version. A pinned
  version would freeze every machine until someone bumped it.
- **The plugin updates itself.** Its SessionStart hook
  (`plugin/scripts/self-update.sh`) runs `claude plugin marketplace update q-core`
  and `claude plugin update q-core@q-core`:
  - detached, so a session never waits;
  - at most every 15 minutes across sessions, with a lock;
  - bounded by a timeout;
  - quiet while it works, and loud when it doesn't (below).

  An update fetched during one session loads at the next session start, or
  after `/reload-plugins`. The log is in the plugin's data directory
  (`~/.claude/plugins/data/q-core-q-core/self-update.log`).
- **A failure is never silent.** Every session start shows one warning, to you
  and to Claude, until the cause is fixed. The cases are:
  - the last attempt failed (with git's reason, or its exit code);
  - an attempt never finished;
  - no update has succeeded for a day;
  - `claude` isn't on the hook's PATH, so no update can run at all.

  The hook still exits 0, so a broken network never blocks a session.
- **Why not only Claude Code's auto-update:** for third-party marketplaces it is
  off by default, it runs only in interactive sessions, and it fails silently
  (ticket T-60).

## What an installed copy contains

Claude Code copies `plugin/` into its cache, and nothing outside travels: a
symlink out of the plugin is refused. So:

- The runbooks a skill reads at run time live in `plugin/runbooks/`.
  `runbooks/` in this repository links to them, for everything else here.
- Skills refer to them as `${CLAUDE_PLUGIN_ROOT}/runbooks/...`, which Claude Code
  replaces with the installed path when it loads the skill. A plain
  `runbooks/...` path would not exist on a machine without this checkout, and
  `tests/test_plugin.py` refuses one.
- Server-side steps go through `plugin/scripts/q-core-server.sh`, which runs
  them locally on the server and over OpenSSH elsewhere (never Tailscale SSH):
  - `backup` runs `python -m api.backup` before a statement import.
  - `push --intake-dir DIR NAME` sends a statement folder from this machine's
    intake folder (`intake_dir`, default `~/Q/intake`) to the server's
    `intake/NAME`. It streams a tar archive the helper never reads, and the
    server (`python -m api.intake_receive`) checks every entry before writing:
    regular files under `NAME/` only, size caps, and no overwriting an
    existing folder. Files land 0600 in 0700 folders, and the only output is
    the file names and counts. On the server itself `push` reports `skipped`.

  Each prints one JSON line on success and never any file content. The
  server's SSH key setup is ticket T-76.

## Changing a skill

1. Edit it under `plugin/skills/`.
2. Try it with `claude --plugin-dir plugin` from this checkout. That session-only
   copy takes precedence over the installed one.
3. Run `tests/test_plugin.py`, `tests/test_skill_conventions.py` and
   `tests/test_skill_vocabularies.py`.
4. Once merged to `main`, every machine picks it up at its next session start.

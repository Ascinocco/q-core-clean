# Runbooks shipped with the q-core plugin

These are the runbooks the life skills read at run time, so they travel with
the plugin (an installed plugin is a copy of `plugin/` only). Their home is
the q-core repository, whose `runbooks/` links here.

Paths in them such as `api/...`, `db/...`, `financial_evals/...`, `scripts/...`
or `runbooks/<name>.md` for a runbook not in this directory refer to the q-core
repository. They explain where behaviour is implemented; nothing in a skill
needs them at run time. A skill reads only files under
`${CLAUDE_PLUGIN_ROOT}`, which `tests/test_plugin.py` enforces.

# ops/launchd/

`tech.q-core.api.plist` — the launchd agent that runs the q-core API
server and keeps it running.

**Nothing here is installed automatically.** Installing a launch agent
changes what runs on the owner's machine at login, so it is a deliberate manual
step. The commands are below; full context (log locations, format, what
the plist does and why) is in `runbooks/observability.md`.

## Install

```bash
ln -sfn ~/Projects/q-core/ops/launchd/tech.q-core.api.plist \
        ~/Library/LaunchAgents/tech.q-core.api.plist

launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/tech.q-core.api.plist
```

A symlink rather than a copy, so editing the plist in the repo and
reloading is enough — no second copy to forget about.

Then check it came up:

```bash
# Every route needs a token since the q-core split; read it from .env without echoing it.
TOKEN="$(sed -n 's/^Q_CORE_API_TOKEN=//p' .env)"
curl -si -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8420/health | grep -i x-request-id
```

## Day to day

```bash
launchctl kickstart -k gui/$(id -u)/tech.q-core.api   # restart (after .env changes)
launchctl print gui/$(id -u)/tech.q-core.api          # status + last exit code
launchctl kill SIGTERM gui/$(id -u)/tech.q-core.api   # stop, stays installed
launchctl bootout gui/$(id -u)/tech.q-core.api        # uninstall
```

## If it will not start

1. `launchctl print gui/$(id -u)/tech.q-core.api` — look at the last exit
   status.
2. `cat data/logs/api.launchd.err` — anything that failed *before* the
   app's own logging was set up (missing `Q_CORE_API_TOKEN`, an import
   error, a venv that has moved) lands here and nowhere else.
3. `tail data/logs/api.log | jq .` — if the app got far enough to log, the
   real error is here.

## Before editing this plist

The paths inside are absolute and hardcoded to
`/path/to/q-core` — launchd has no working directory to
resolve relative paths against at load time. If the repo moves, edit the
plist and re-bootstrap.

Validate any edit before reloading; launchd's own error for a malformed
plist is not helpful:

```bash
plutil -lint ops/launchd/tech.q-core.api.plist
```

#!/usr/bin/env bash
# q-core plugin self-update, run by the plugin's SessionStart hook.
#
# Epic 3 requires the life skills to stay current with no manual step, and
# Claude Code's own marketplace auto-update is off by default for
# third-party marketplaces, runs only interactively and fails silently
# (ticket T-60). So the plugin refreshes itself:
#
#   claude plugin marketplace update q-core
#   claude plugin update q-core@q-core
#
# detached, so session start never waits on the network; the new version
# loads at the next session start (or /reload-plugins). At most one run per
# THROTTLE_SECONDS across all sessions, guarded by a lock so concurrent
# sessions do not race. The update's output goes to a log in the plugin's
# data directory. Always exits 0.
#
# A failing update must not be silent (decisions log, "q-core
# split", decision 2): a lapsed key would otherwise leave the machine stale
# forever. So the run records `last-success` and `last-failure`, and the
# NEXT session start prints one line when the last attempt failed or the last
# success is older than STALE_SECONDS.
set -u

THROTTLE_SECONDS="${Q_CORE_UPDATE_THROTTLE_SECONDS:-900}"
TIMEOUT_SECONDS="${Q_CORE_UPDATE_TIMEOUT_SECONDS:-120}"
STALE_SECONDS="${Q_CORE_UPDATE_STALE_SECONDS:-86400}"
data="${CLAUDE_PLUGIN_DATA:-$HOME/.claude/plugins/data/q-core-q-core}"
now="$(date +%s)"

number() { local v; v="$(cat "$1" 2>/dev/null || echo 0)"; case "$v" in *[!0-9]*|'') echo 0 ;; *) echo "$v" ;; esac; }
# One warning, shown to the user (systemMessage) and to Claude (additionalContext).
warn() {
  local m; m="$(printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | tr -d '\000-\037')"
  printf '{"systemMessage":"%s","hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}\n' "$m" "$m"
}

# Report the previous run's outcome first: this is the only line a session sees.
if [ -f "$data/last-attempt" ]; then
  success="$(number "$data/last-success")"; failure="$(number "$data/last-failure")"
  attempt="$(number "$data/last-attempt")"
  if [ "$attempt" -gt "$success" ] && [ "$attempt" -gt "$failure" ] \
      && [ $((now - attempt)) -gt $((TIMEOUT_SECONDS * 3)) ]; then
    warn "q-core plugin: the last self-update never finished (killed or hung); skills may be stale. See $data/self-update.log"
  elif [ "$failure" -gt "$success" ]; then
    reason="$(head -c 200 "$data/last-failure-reason" 2>/dev/null | tr -d '\n')"
    warn "q-core plugin: the last self-update failed${reason:+ ($reason)}; skills may be stale. Run 'claude plugin update q-core@q-core' or see $data/self-update.log"
  elif [ "$success" -gt 0 ] && [ $((now - success)) -gt "$STALE_SECONDS" ]; then
    warn "q-core plugin: no successful self-update since $(date -u -r "$success" +%Y-%m-%d 2>/dev/null || date -u -d "@$success" +%Y-%m-%d); skills may be stale. See $data/self-update.log"
  fi
fi

if ! command -v claude >/dev/null 2>&1; then
  # Without the CLI on the hook's PATH the plugin can never update itself.
  warn "q-core plugin: 'claude' is not on the SessionStart hook's PATH, so the plugin cannot update itself; skills may be stale. Add Claude Code's install directory to PATH in your login shell."
  exit 0
fi
mkdir -p "$data" 2>/dev/null || exit 0
stamp="$data/last-update"
lock="$data/update.lock"

if [ -f "$stamp" ]; then
  last="$(cat "$stamp" 2>/dev/null || echo 0)"
  case "$last" in *[!0-9]*|'') last=0 ;; esac
  [ $((now - last)) -lt "$THROTTLE_SECONDS" ] && exit 0
fi
# A stale lock (a run killed mid-way) is broken after the timeout has passed.
if ! mkdir "$lock" 2>/dev/null; then
  locked_at="$(cat "$lock/at" 2>/dev/null || echo 0)"
  case "$locked_at" in *[!0-9]*|'') locked_at=0 ;; esac
  [ $((now - locked_at)) -lt $((TIMEOUT_SECONDS * 2)) ] && exit 0
  rm -rf "$lock" && mkdir "$lock" 2>/dev/null || exit 0
fi
echo "$now" > "$lock/at"
echo "$now" > "$stamp"
echo "$now" > "$data/last-attempt"

(
  run() {  # a bounded command, killed if it outlives TIMEOUT_SECONDS
    "$@" &
    local pid=$!
    ( sleep "$TIMEOUT_SECONDS"; kill "$pid" 2>/dev/null ) &
    local watchdog=$!
    wait "$pid"; local status=$?
    kill "$watchdog" 2>/dev/null
    return $status
  }
  output="$data/last-run.out"
  { run claude plugin marketplace update q-core && run claude plugin update q-core@q-core; } > "$output" 2>&1
  status=$?
  # URL credentials are stripped from the log as well as from the warning.
  { echo "--- $(date -u +%Y-%m-%dT%H:%M:%SZ)"; sed -E 's#://[^/@[:space:]]*@#://#g' "$output"; echo "exit $status"; } >> "$data/self-update.log"
  # Keep the log bounded: the last 400 lines.
  tail -n 400 "$data/self-update.log" > "$data/self-update.log.tmp" 2>/dev/null && mv "$data/self-update.log.tmp" "$data/self-update.log"
  if [ "$status" -eq 0 ]; then
    date +%s > "$data/last-success"
  else
    date +%s > "$data/last-failure"
    # The first line that names a failure, trimmed and without URL credentials:
    # shown at every session start until an update succeeds.
    reason="$(grep -m1 -i -E 'error|fail|denied|timed out|could not|unable' "$output" \
      | sed -E 's#://[^/@[:space:]]*@#://#g' | cut -c1-160)"
    echo "${reason:-exit $status}" > "$data/last-failure-reason"
  fi
  rm -f "$output"
  rm -rf "$lock"
) </dev/null >/dev/null 2>&1 &
disown 2>/dev/null || true
exit 0

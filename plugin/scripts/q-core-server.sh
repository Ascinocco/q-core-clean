#!/usr/bin/env bash
# Run a q-core server-side command from any machine, for the life skills.
#
#   q-core-server.sh --ssh TARGET --checkout PATH backup
#   q-core-server.sh --ssh TARGET --checkout PATH push --intake-dir DIR NAME
#
# push copies DIR/NAME (this machine's intake folder) to the server's
# intake/NAME as a tar stream over OpenSSH; the server side
# (`python -m api.intake_receive NAME`) checks every entry before writing,
# refuses an existing folder, checks the file count and byte total this
# script sends against what arrived, and prints file names and counts only.
# The folder must be a real directory directly inside DIR, with only readable
# regular files and folders in it (no symlinks). On the server itself (no --ssh) there is nothing to copy: statements
# are already in intake/, so push reports that it skipped.
#
# On a NixOS server with the q-core module (runbooks/deploy-nixos.md) the
# command runs as the service user through `sudo -n -u q-core q-core-cli`,
# which carries the service's environment and secrets; the login user must be
# in services.q-core.cliUsers. Elsewhere it runs in the checkout's .venv.
#
# With --ssh empty (on the server itself) the command runs locally; otherwise over OpenSSH (never Tailscale SSH; decisions log,
# "q-core split", decision 7). On success it prints the
# command's one JSON line; the command's own errors are one JSON line on
# stderr, and an SSH failure is ssh's own message with exit 255. Never file
# contents or the environment.
set -u

ssh_target="" checkout="~/q-core" intake_dir="" name=""
usage() { echo '{"error":"usage","message":"q-core-server.sh --ssh TARGET --checkout PATH backup | push --intake-dir DIR NAME"}' >&2; exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --ssh) [ $# -ge 2 ] || usage; ssh_target=$2; shift 2 ;;
    --checkout) [ $# -ge 2 ] || usage; checkout=$2; shift 2 ;;
    *) break ;;
  esac
done
command=${1-}
[ $# -gt 0 ] && shift
if [ "$command" = push ]; then
  while [ $# -gt 0 ]; do
    case "$1" in
      --intake-dir) [ $# -ge 2 ] || usage; intake_dir=$2; shift 2 ;;
      *) [ -z "$name" ] || usage; name=$1; shift ;;
    esac
  done
elif [ $# -gt 0 ]; then
  usage
fi
# An unset userConfig option can reach us as its literal placeholder.
case "$ssh_target" in '${user_config.'*) ssh_target="" ;; esac
case "$checkout" in '${user_config.'*) checkout="~/q-core" ;; esac
case "$intake_dir" in '${user_config.'*) intake_dir="~/Q/intake" ;; esac
# The remote command: q-core-cli as the service user when the NixOS module
# installed it, otherwise the checkout's .venv. $checkout is validated below and
# every argument here is fixed or validated, so no shell metacharacters.
installed_or_checkout() {
  printf '%s' "if command -v q-core-cli >/dev/null 2>&1; then exec sudo -n -u q-core q-core-cli $1; else cd $checkout && exec .venv/bin/python -m $2; fi"
}
case "$command" in
  backup) remote=$(installed_or_checkout backup 'api.backup') ;;
  push)
    printf '%s' "$name" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$' \
      || { echo '{"error":"usage","message":"push needs one folder name of letters, digits, . _ or -"}' >&2; exit 2; } ;;
  *) usage ;;
esac
case "$checkout" in
  *[!A-Za-z0-9_./~-]*) echo '{"error":"usage","message":"the checkout path may contain only letters, digits and . _ / ~ -"}' >&2; exit 2 ;;
esac

if [ "$command" = push ]; then
  if [ -z "$ssh_target" ]; then
    echo '{"skipped":"on the server: the folder is already in intake/","folder":"'"$name"'"}'
    exit 0
  fi
  case "$intake_dir" in "~"|"~/"*) intake_dir="$HOME${intake_dir#\~}" ;; esac
  source_dir="$intake_dir/$name"
  if [ -z "$intake_dir" ] || [ ! -d "$intake_dir" ] || [ -L "$source_dir" ] || [ ! -d "$source_dir" ]; then
    echo '{"error":"not_found","message":"no such folder directly inside the local intake folder"}' >&2; exit 4
  fi
  if [ -n "$(find "$source_dir" -type l -print -quit)" ]; then
    echo '{"error":"refused","message":"the folder contains a symlink; move real files in instead"}' >&2; exit 3
  fi
  # Every file must be readable: tar would skip an unreadable one and still
  # produce a valid archive. The server also checks the count and size below.
  files=0 bytes=0
  while IFS= read -r -d '' file; do
    [ -r "$file" ] || { echo '{"error":"refused","message":"a file in the folder is not readable"}' >&2; exit 3; }
    files=$((files + 1)); bytes=$((bytes + $(wc -c < "$file")))
  done < <(find "$source_dir" -type f -print0)
  if [ -n "$(find "$source_dir" ! -type f ! -type d -print -quit)" ]; then
    echo '{"error":"refused","message":"the folder may contain only regular files and folders"}' >&2; exit 3
  fi
  remote=$(installed_or_checkout "intake-receive $name --expect-files $files --expect-bytes $bytes" \
    "api.intake_receive $name --expect-files $files --expect-bytes $bytes")
fi

if [ -z "$ssh_target" ]; then
  eval "$remote"   # checkout is validated above: no shell metacharacters
elif [ "$command" = push ]; then
  case "$ssh_target" in
    -*|*[!A-Za-z0-9_.@-]*) echo '{"error":"usage","message":"the SSH target may contain only letters, digits and . _ @ -"}' >&2; exit 2 ;;
  esac
  # A tar stream, never read here: file contents go straight to the server.
  set -o pipefail
  COPYFILE_DISABLE=1 tar -C "$intake_dir" -cf - -- "$name" \
    | ssh -o BatchMode=yes -o ConnectTimeout=10 -- "$ssh_target" "$remote"
  exit $?
else
  case "$ssh_target" in
    -*|*[!A-Za-z0-9_.@-]*) echo '{"error":"usage","message":"the SSH target may contain only letters, digits and . _ @ -"}' >&2; exit 2 ;;
  esac
  exec ssh -o BatchMode=yes -o ConnectTimeout=10 -- "$ssh_target" "$remote"
fi

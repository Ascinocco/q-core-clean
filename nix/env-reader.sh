# read_env_file FILE: export its KEY=value lines, read literally (q-core-cli).
#
# Never sourced, so a value is never shell code. What it matches of systemd's
# EnvironmentFile is what a sops-rendered file contains: blank lines and lines
# starting with # or ; are skipped, leading whitespace is ignored, and one
# pair of matching outer quotes is removed. Anything else (no "=", or a name
# that isn't [A-Za-z_][A-Za-z0-9_]*) is skipped with a warning that never
# repeats the line, which may hold a secret.
read_env_file() {
  while IFS= read -r line || [ -n "$line" ]; do
    line=${line#"${line%%[![:space:]]*}"}
    case "$line" in ""|"#"*|";"*) continue ;; esac
    case "$line" in
      *=*) ;;
      *) echo "q-core-cli: skipped an environment-file line with no '='" >&2; continue ;;
    esac
    key=${line%%=*}
    value=${line#*=}
    case "$key" in
      ""|[0-9]*|*[!A-Za-z0-9_]*)
        echo "q-core-cli: skipped an environment-file line whose name isn't valid" >&2; continue ;;
    esac
    case "$value" in
      \"*\") value=${value#\"}; value=${value%\"} ;;
      \'*\') value=${value#\'}; value=${value%\'} ;;
    esac
    export "$key=$value"
  done < "$1"
}

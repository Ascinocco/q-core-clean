#!/bin/sh
set -eu

# Audit the environment launchd actually runs, not whichever Python happens to
# be first on PATH. Override only when intentionally auditing another checkout.
scan_python=${Q_CORE_SCAN_PYTHON:-.venv/bin/python}
if [ ! -x "$scan_python" ]; then
  echo "No executable at $scan_python; set Q_CORE_SCAN_PYTHON to the app venv Python." >&2
  exit 2
fi

site_packages=$(
  "$scan_python" -c 'import site; print(next(path for path in site.getsitepackages() if path.endswith("site-packages")))'
)

echo "Dependency vulnerabilities (installed app environment)"
uvx --from pip-audit==2.9.0 pip-audit --path "$site_packages"

echo "Tracked-file secret candidates (hashes and locations, never values)"
git ls-files -z | xargs -0 uvx --from detect-secrets==1.5.0 detect-secrets scan

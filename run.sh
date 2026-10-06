#!/usr/bin/env bash
# Start FixPilot's phone-facing server.
#
#   ./run.sh                     # serve the repository this script lives in
#   ./run.sh /path/to/repo       # serve another repository
#   FIXPILOT_PORT=9000 ./run.sh  # serve on another port
#
# Everything after the optional repo argument is passed to `fixpilot serve`
# (e.g. `--verbose`, `--no-auth` for local development).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARGS=("$@")

REPO="${FIXPILOT_REPO:-$HERE}"
if [[ ${#ARGS[@]} -gt 0 && ${ARGS[0]} != -* && -d ${ARGS[0]} ]]; then
  REPO="$(cd "${ARGS[0]}" && pwd)"
  ARGS=("${ARGS[@]:1}")
fi

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "FixPilot needs python3 on PATH (set PYTHON=/path/to/python3 to override)." >&2
  exit 127
fi

if ! "$PYTHON" - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
then
  echo "FixPilot needs Python 3.11+ (found: $("$PYTHON" --version 2>&1))." >&2
  exit 1
fi

# Keep the checkout importable without an install step, and run unbuffered so the
# banner (URL + phone token) appears immediately even when output is piped or logged.
export PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1

echo "[fixpilot] repo: $REPO"
exec "$PYTHON" -u -m fixpilot --repo "$REPO" serve ${ARGS+"${ARGS[@]}"}

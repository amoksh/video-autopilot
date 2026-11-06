#!/usr/bin/env bash
# Launch the private Codespaces-forwardable web dashboard on port 8000.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
[ -d .venv ] && source .venv/bin/activate
exec uvicorn webapp.server:app --host 0.0.0.0 --port "${PORT:-8000}" --reload

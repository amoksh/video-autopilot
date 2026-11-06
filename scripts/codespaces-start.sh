#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
mkdir -p .codespaces
log_file=".codespaces/server.log"
exec >>"$log_file" 2>&1

printf '\n[%s] Checking Video Autopilot on port 8000.\n' "$(date --iso-8601=seconds)"

if ! command -v curl >/dev/null 2>&1; then
  echo "ERROR: curl is required to check whether the dashboard is already running."
  exit 1
fi

if curl --silent --show-error --output /dev/null --max-time 2 http://127.0.0.1:8000/; then
  echo "Dashboard already responds on port 8000; not starting another server."
  exit 0
fi

echo "Starting dashboard with scripts/serve.sh."
nohup env PORT=8000 bash scripts/serve.sh </dev/null >>"$log_file" 2>&1 &
echo "Dashboard startup command launched (pid $!)."

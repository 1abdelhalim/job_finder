#!/usr/bin/env bash
# Run the web UI using the project venv (no need to `source activate`).
# Default 5055 — avoids macOS AirPlay on 5000 and common conflicts on 5001.
# Override: PORT=8080 ./run.sh
#
# If the port is still in use (e.g. previous ./run.sh left running), we stop
# whatever is LISTENing on that port so this script always comes up cleanly.
set -e
cd "$(dirname "$0")"
PORT="${PORT:-5055}"

_free_port() {
  local p="$1"
  if ! command -v lsof >/dev/null 2>&1; then
    return 0
  fi
  local pids
  pids=$(lsof -ti ":$p" -sTCP:LISTEN 2>/dev/null || true)
  if [ -z "$pids" ]; then
    return 0
  fi
  echo "Port $p is in use — stopping previous process(es): $pids"
  # shellcheck disable=SC2086
  kill $pids 2>/dev/null || true
  sleep 0.4
  pids=$(lsof -ti ":$p" -sTCP:LISTEN 2>/dev/null || true)
  if [ -n "$pids" ]; then
    # shellcheck disable=SC2086
    kill -9 $pids 2>/dev/null || true
    sleep 0.2
  fi
}

_free_port "$PORT"

exec ./venv/bin/python main.py ui --port "$PORT"

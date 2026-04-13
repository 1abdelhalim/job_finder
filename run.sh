#!/usr/bin/env bash
# Run the web UI using the project venv (no need to `source activate`).
# Default 5055 — avoids macOS AirPlay on 5000 and common conflicts on 5001.
# Override: PORT=8080 ./run.sh
cd "$(dirname "$0")"
PORT="${PORT:-5055}"
exec ./venv/bin/python main.py ui --port "$PORT"

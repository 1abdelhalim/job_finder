#!/usr/bin/env bash
# Run the web UI using the project venv (no need to `source activate`).
# Default 5001: macOS often reserves 5000 for AirPlay Receiver (System Settings → AirDrop & Handoff).
cd "$(dirname "$0")"
PORT="${PORT:-5001}"
exec ./venv/bin/python main.py ui --port "$PORT"

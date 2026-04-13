#!/usr/bin/env bash
# Run the web UI using the project venv (no need to `source activate`).
cd "$(dirname "$0")"
exec ./venv/bin/python main.py ui --port 5000

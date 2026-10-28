#!/bin/bash
# One-click start: sets up everything on first run, then opens the control panel.
cd "$(dirname "$0")"
clear
echo "=== Kite Bot ==="
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is not installed. Install it from https://www.python.org/downloads/ and run this again."
  read -n 1 -s -r -p "Press any key to close."; exit 1
fi
if [ ! -x .venv/bin/python ]; then
  echo "First run: setting up (about 1-2 minutes)..."
  python3 -m venv .venv || { echo "Could not create environment."; read -n 1; exit 1; }
fi
if [ ! -f .venv/.deps_ok ] || [ requirements.txt -nt .venv/.deps_ok ]; then
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q -r requirements.txt && touch .venv/.deps_ok || { echo "Install failed - check internet."; read -n 1; exit 1; }
fi
# free the port if an old copy is still running
lsof -ti tcp:8765 2>/dev/null | xargs kill 2>/dev/null
echo "Opening control panel at http://127.0.0.1:8765"
echo "Keep this window open while the bot runs. Close it to shut down."
./.venv/bin/python app.py

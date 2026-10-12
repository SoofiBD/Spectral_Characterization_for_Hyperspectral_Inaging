#!/usr/bin/env bash
# Spectral Characterization Suite launcher: creates a virtual environment, installs the
# dependencies and starts the app. Works on macOS/Linux and on Windows (Git Bash).
#   ./run.sh              -> hardware mode
#   ./run.sh --simulate   -> simulation mode
set -e
cd "$(dirname "$0")"

# Find Python 3.11+
PY=""
for c in python3.12 python3.11 python3.13 python3 python py; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        PY="$c"; break
    fi
done
if [ -z "$PY" ]; then
    echo "ERROR: Python 3.11 or newer is required: https://www.python.org/downloads/"
    exit 1
fi

if [ ! -d .venv ]; then
    echo "Creating virtual environment with $PY ..."
    "$PY" -m venv .venv
fi

if [ -f .venv/Scripts/python.exe ]; then VPY=.venv/Scripts/python.exe; else VPY=.venv/bin/python; fi

if [ ! -f .venv/.deps_installed ] || [ requirements.txt -nt .venv/.deps_installed ]; then
    echo "Installing dependencies ..."
    "$VPY" -m pip install --upgrade pip
    "$VPY" -m pip install -r requirements.txt
    touch .venv/.deps_installed
fi

exec "$VPY" main.py "$@"

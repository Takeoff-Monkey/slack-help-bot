#!/usr/bin/env bash
# One-time setup: build the run_code sandbox venv. Model-written code runs with this
# venv's python (PyMuPDF/pandas/Pillow available); the bot's own env stays clean.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

if [ ! -d .venv ]; then
  echo "Creating sandbox venv at $(pwd)/.venv ..."
  "$PYTHON" -m venv .venv
fi

./.venv/bin/python -m pip install --upgrade pip >/dev/null
./.venv/bin/python -m pip install -r requirements.txt

echo "Done. Sandbox venv ready at $(pwd)/.venv"
./.venv/bin/python -c "import fitz, pandas, PIL; print('  sandbox deps import OK')"

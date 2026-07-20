#!/usr/bin/env bash
# One-time setup: build this tool's isolated venv from requirements.txt.
# The bot's local backend runs run.py with this venv's python, so the PDF deps
# (PyMuPDF, pandas, xlsxwriter) never touch the bot's own environment.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

if [ ! -d .venv ]; then
  echo "Creating venv at $(pwd)/.venv ..."
  "$PYTHON" -m venv .venv
fi

./.venv/bin/python -m pip install --upgrade pip >/dev/null
./.venv/bin/python -m pip install -r requirements.txt

echo "Done. Tool venv ready at $(pwd)/.venv"
echo "Quick check:"
./.venv/bin/python -c "import fitz, pandas, xlsxwriter; print('  tool deps import OK')"

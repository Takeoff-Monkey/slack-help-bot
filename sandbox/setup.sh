#!/usr/bin/env bash
# One-time setup: build the run_code sandbox venvs. Model-written code runs with one of these
# venvs' python; the bot's own env stays clean. Two environments are built:
#
#   .venv      "default"    — extended toolkit (requirements.txt): Tesseract OCR + opencv +
#                             PDF/data/doc libraries. Fast; used first.
#   .venv-ocr  "neural_ocr" — everything in .venv PLUS a heavier neural OCR engine
#                             (requirements.txt + requirements-ocr.txt). Slower; the bot
#                             escalates here when default Tesseract OCR looks poor.
#
# System binaries: OCR needs `tesseract`; pdf2image needs poppler's `pdftoppm`. We check for
# them and print an install hint if missing (we don't sudo-install for you).
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

# --- system binary checks (non-fatal, but OCR / pdf2image won't work without them) ---
missing=""
command -v tesseract >/dev/null 2>&1 || missing="${missing} tesseract-ocr"
command -v pdftoppm  >/dev/null 2>&1 || missing="${missing} poppler-utils"
if [ -n "${missing}" ]; then
  echo "WARNING: missing system binaries:${missing}"
  echo "  Debian/Ubuntu/Mint:  sudo apt-get install -y${missing}"
  echo "  (Tesseract also needs a language pack, e.g. tesseract-ocr-eng — usually bundled.)"
  echo "  OCR via pytesseract / pdf2image will fail until these are installed."
else
  echo "System binaries OK: $(tesseract --version 2>&1 | head -1), poppler $(pdftoppm -v 2>&1 | head -1 | awk '{print $NF}')"
fi

ensure_venv() {
  local dir="$1"
  if [ ! -d "${dir}" ]; then
    echo "Creating sandbox venv at $(pwd)/${dir} ..."
    "$PYTHON" -m venv "${dir}"
  fi
  "./${dir}/bin/python" -m pip install --upgrade pip >/dev/null
}

# Default (extended) environment.
ensure_venv .venv
echo "  installing requirements.txt into .venv ..."
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python -c "import fitz, pandas, PIL, openpyxl, pytesseract, cv2, pdfplumber, pdf2image, docx, pptx, reportlab, xlsxwriter, tabulate; print('  .venv (default) deps import OK')"

# Neural-OCR environment: extended toolkit + the neural engine, so it is a strict superset.
# The neural tier is installed with --no-deps (requirements-ocr.txt lists every runtime dep
# explicitly) so rapidocr-onnxruntime does NOT drag in the full opencv-python (libGL) — we reuse
# the opencv-python-headless from requirements.txt. This is the SAME sequence Dockerfile.ocr uses.
ensure_venv .venv-ocr
echo "  installing requirements.txt into .venv-ocr ..."
./.venv-ocr/bin/python -m pip install -r requirements.txt
echo "  installing requirements-ocr.txt (--no-deps) into .venv-ocr ..."
./.venv-ocr/bin/python -m pip install --no-deps -r requirements-ocr.txt
./.venv-ocr/bin/python -c "import cv2, pytesseract; from rapidocr_onnxruntime import RapidOCR; RapidOCR(); print('  .venv-ocr (neural_ocr) deps import OK (bundled models load offline)')"

echo "Done. Sandbox venvs ready: .venv (default), .venv-ocr (neural_ocr)."

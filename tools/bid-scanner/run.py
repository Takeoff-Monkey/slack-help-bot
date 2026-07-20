#!/usr/bin/env python
"""AI-callable single-file entrypoint for the bid-scanner tool.

The Slack bot's *local* tool-runner backend invokes this as a subprocess from this
directory, using this directory's own venv (so the bot never imports PyMuPDF/pandas).
The AWS Lambda backend uses lambda/handler.py instead, but both share the same
scan/highlight core in scanner.py (scan_pdf + save_excel).

Contract
--------
stdin (JSON):
  {
    "input":      {"input_file": "file_1", "keywords": ["fence", "gate"], "skip_pages": []},
    "input_path": "/abs/path/to/staged.pdf",   # the resolved attachment on disk
    "work_dir":   "/abs/path/to/work-<uuid>",   # where to write the outputs + result.json
    "backend":    "local"
  }

result.json (written into work_dir; also echoed to stdout):
  {
    "status":    "ok" | "error",
    "summary":   "Scanned site.pdf for 15 keywords; 42 matches across 8 keywords.",
    "artifacts": [
      {"kind": "pdf",  "ref": "<abs path>", "filename": "site-highlighted.pdf", "title": "..."},
      {"kind": "xlsx", "ref": "<abs path>", "filename": "site-keywords.xlsx",   "title": "..."}
    ],
    "error":     null | "<message>"
  }

`keywords` is optional: omit it (or pass an empty list) and the tool falls back to
scanner.DEFAULT_KEYWORDS — the standard bid-scope set. The bot strips "ref" before
showing the result to the model; it keeps the full record to upload the artifacts.
"""

import json
import os
import sys
import traceback

import scanner


def _write_result(work_dir: str, result: dict) -> None:
    with open(os.path.join(work_dir, "result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f)


def run(contract: dict) -> dict:
    tool_input = contract.get("input") or {}
    input_path = contract.get("input_path")
    work_dir = contract.get("work_dir") or "."

    if not input_path or not os.path.exists(input_path):
        return {
            "status": "error",
            "summary": "",
            "artifacts": [],
            "error": f"Input PDF not found at {input_path!r}.",
        }

    keywords = scanner.normalize_keywords(tool_input.get("keywords"))
    skip_pages = tool_input.get("skip_pages") or []

    with open(input_path, "rb") as f:
        pdf_bytes = f.read()

    highlighted, rows = scanner.scan_pdf(pdf_bytes, keywords=keywords, skip_pages=skip_pages)

    base = os.path.splitext(os.path.basename(input_path))[0] or "document"

    if not rows:
        return {
            "status": "ok",
            "summary": (
                f"Scanned {base} for {len(keywords)} keyword(s) but found no matches, "
                f"so there was nothing to highlight."
            ),
            "artifacts": [],
            "error": None,
        }

    pdf_name = f"{base}-highlighted.pdf"
    xlsx_name = f"{base}-keywords.xlsx"
    pdf_path = os.path.join(work_dir, pdf_name)
    xlsx_path = os.path.join(work_dir, xlsx_name)

    with open(pdf_path, "wb") as f:
        f.write(highlighted)
    scanner.save_excel(rows, xlsx_path)

    total = sum(r[1] for r in rows)
    summary = (
        f"Scanned {base} for {len(keywords)} keyword(s): {total} match(es) across "
        f"{len(rows)} keyword(s). Highlighted the PDF and wrote a keyword tally."
    )
    return {
        "status": "ok",
        "summary": summary,
        "artifacts": [
            {"kind": "pdf", "ref": pdf_path, "filename": pdf_name, "title": "Highlighted PDF"},
            {"kind": "xlsx", "ref": xlsx_path, "filename": xlsx_name, "title": "Keyword tally"},
        ],
        "error": None,
    }


def main() -> None:
    work_dir = "."
    try:
        contract = json.load(sys.stdin)
        work_dir = contract.get("work_dir") or "."
        result = run(contract)
    except Exception as err:
        traceback.print_exc(file=sys.stderr)
        result = {
            "status": "error",
            "summary": "",
            "artifacts": [],
            "error": f"{type(err).__name__}: {err}",
        }

    # result.json is the source of truth (stdout may carry stray prints from deps);
    # stdout is a convenience for humans / quick debugging.
    try:
        _write_result(work_dir, result)
    except Exception:
        traceback.print_exc(file=sys.stderr)
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()

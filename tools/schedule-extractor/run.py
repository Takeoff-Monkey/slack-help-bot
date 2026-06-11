#!/usr/bin/env python
"""AI-callable single-file entrypoint for the schedule-extractor tool.

The Slack bot's *local* tool-runner backend invokes this as a subprocess from this
directory, using this directory's own venv (so the bot never imports the heavy PDF
deps). The AWS Lambda backend uses lambda/handler.py instead, but both share the
same extraction core in main2.py (process_pdf + save_excel).

Contract
--------
stdin (JSON):
  {
    "input":      {"input_file": "file_1", "ignore_first_column": true, "skip_pages": []},
    "input_path": "/abs/path/to/staged.pdf",   # the resolved attachment on disk
    "work_dir":   "/abs/path/to/run-<uuid>",    # where to write the .xlsx + result.json
    "backend":    "local"
  }

result.json (written into work_dir; also echoed to stdout):
  {
    "status":    "ok" | "error",
    "summary":   "Extracted 3 schedules across 2 pages into site.xlsx.",
    "artifacts": [{"kind": "xlsx", "ref": "<abs path>", "filename": "site.xlsx", "title": "..."}],
    "error":     null | "<message>"
  }

The bot strips "ref" before showing the result to the model (the model never needs a
path); it keeps the full record so it can upload the artifact back to Slack.
"""

import json
import os
import sys
import traceback

import main2


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

    skip_pages = tool_input.get("skip_pages") or []
    ignore_first_column = tool_input.get("ignore_first_column", True)

    with open(input_path, "rb") as f:
        pdf_bytes = f.read()

    tables = main2.process_pdf(pdf_bytes, skip_pages=skip_pages)

    if not tables:
        return {
            "status": "ok",
            "summary": "No plant legends or material schedules were detected in this PDF.",
            "artifacts": [],
            "error": None,
        }

    n_pages = len(tables)
    n_scheds = sum(len(v) for v in tables.values())

    base = os.path.splitext(os.path.basename(input_path))[0] or "schedules"
    out_name = f"{base}.xlsx"
    out_path = os.path.join(work_dir, out_name)
    main2.save_excel(tables, out_path, ignore_first_column=ignore_first_column)

    summary = (
        f"Extracted {n_scheds} schedule{'s' if n_scheds != 1 else ''} "
        f"across {n_pages} page{'s' if n_pages != 1 else ''} into {out_name}."
    )
    return {
        "status": "ok",
        "summary": summary,
        "artifacts": [
            {"kind": "xlsx", "ref": out_path, "filename": out_name, "title": "Extracted schedules"}
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

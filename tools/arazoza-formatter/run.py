#!/usr/bin/env python
"""AI-callable single-file entrypoint for the arazoza-formatter tool.

The Slack bot's *local* tool-runner backend invokes this as a subprocess from this directory,
using this directory's own venv (so the bot never imports openpyxl). The AWS Lambda backend
uses lambda/handler.py instead; both share the formatting core in formatter.py.

Contract
--------
stdin (JSON):
  {
    "input":      {"input_file": "file_1", "sheet_name": null, "force": false},
    "input_path": "/abs/path/to/staged.xlsm",   # the resolved attachment on disk
    "work_dir":   "/abs/path/to/work-<uuid>",    # where to write the output + result.json
    "backend":    "local"
  }

result.json (written into work_dir; also echoed to stdout):
  {
    "status":    "ok" | "error",
    "summary":   "Formatted sheet 'Project Totals' of X.xlsm → X - formatted.xlsm. ...",
    "artifacts": [{"kind": "xlsm", "ref": "<abs path>", "filename": "X - formatted.xlsm", "title": "..."}],
    "error":     null | "<message>"
  }

The original file is never modified — the output is a new copy. The bot strips "ref" before
showing the result to the model; it keeps the full record to upload the artifact.
"""

import json
import os
import sys
import traceback

import formatter


def _write_result(work_dir: str, result: dict) -> None:
    with open(os.path.join(work_dir, "result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f)


def _err(message: str) -> dict:
    return {"status": "error", "summary": "", "artifacts": [], "error": message}


def run(contract: dict) -> dict:
    tool_input = contract.get("input") or {}
    input_path = contract.get("input_path")
    work_dir = contract.get("work_dir") or "."

    if not input_path or not os.path.exists(input_path):
        return _err(f"Input worksheet not found at {input_path!r}.")

    sheet_name = tool_input.get("sheet_name") or None
    force = bool(tool_input.get("force"))

    # The staged file is "<handle>-<original name>" (e.g. "file_1-Arazoza - X.xlsm"); the
    # output should carry the user's own filename, so strip that handle prefix back off.
    original_name = _original_name(os.path.basename(input_path))
    out_name = formatter.output_name_for(original_name)
    out_path = os.path.join(work_dir, out_name)

    try:
        report = formatter.format_file(input_path, out_path, sheet_name=sheet_name, force=force)
    except formatter.FormatterError as err:
        return _err(str(err))

    kind = out_name.rsplit(".", 1)[-1].lower()
    return {
        "status": "ok",
        "summary": report.summary(original_name, out_name),
        "artifacts": [
            {"kind": kind, "ref": out_path, "filename": out_name, "title": "Formatted Arazoza worksheet"}
        ],
        "error": None,
    }


def _original_name(staged_basename: str) -> str:
    """'file_1-Arazoza - X.xlsm' -> 'Arazoza - X.xlsm' (no-op if there's no handle prefix)."""
    import re
    return re.sub(r"^file_\d+-", "", staged_basename) or staged_basename


def main() -> None:
    work_dir = None
    try:
        contract = json.load(sys.stdin)
        work_dir = contract.get("work_dir") or "."
        result = run(contract)
    except Exception as err:
        traceback.print_exc(file=sys.stderr)
        result = _err(f"{type(err).__name__}: {err}")

    # result.json is the source of truth (stdout may carry stray prints from deps);
    # stdout is a convenience for humans / quick debugging. With no readable contract there is
    # no work_dir to write into — stdout is all the caller gets (tool_runner falls back to it).
    if work_dir is not None:
        try:
            _write_result(work_dir, result)
        except Exception:
            traceback.print_exc(file=sys.stderr)
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()

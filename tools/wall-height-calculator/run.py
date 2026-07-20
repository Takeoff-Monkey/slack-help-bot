#!/usr/bin/env python
"""AI-callable single-file entrypoint for the wall-height-calculator tool.

The Slack bot's *local* tool-runner backend invokes this as a subprocess from this
directory, using this directory's own venv (so the bot never imports PyMuPDF/pandas).
The AWS Lambda backend uses lambda/handler.py instead, but both share the same
detection + output core in wall_heights.py (process_pdf + save_excel).

Contract
--------
stdin (JSON):
  {
    "input":      {"input_file": "file_1", "project_type": "Single-Family", "skip_pages": []},
    "input_path": "/abs/path/to/staged.pdf",   # the resolved attachment on disk
    "work_dir":   "/abs/path/to/work-<uuid>",   # where to write the .xlsx + result.json
    "backend":    "local"
  }

result.json (written into work_dir; also echoed to stdout):
  {
    "status":    "ok" | "error",
    "summary":   "Calculated heights for 12 wall(s) (of 14 detected) using Single-Family rounding into site-wall-heights.xlsx.",
    "artifacts": [{"kind": "xlsx", "ref": "<abs path>", "filename": "site-wall-heights.xlsx", "title": "..."}],
    "error":     null | "<message>"
  }

`project_type` is optional: omit it and the tool defaults to Single-Family rounding. The
bot strips "ref" before showing the result to the model; it keeps the full record to
upload the artifact.
"""

import json
import os
import sys
import traceback

import wall_heights


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

    project_type = wall_heights.normalize_project_type(tool_input.get("project_type"))
    skip_pages = tool_input.get("skip_pages") or []

    with open(input_path, "rb") as f:
        pdf_bytes = f.read()

    walls = wall_heights.process_pdf(pdf_bytes, project_type=project_type, skip_pages=skip_pages)

    if not walls:
        return {
            "status": "ok",
            "summary": (
                "No wall elevation (TW/BW) markups were detected in this PDF, so there were "
                "no wall heights to calculate."
            ),
            "artifacts": [],
            "error": None,
        }

    base = os.path.splitext(os.path.basename(input_path))[0] or "walls"
    out_name = f"{base}-wall-heights.xlsx"
    out_path = os.path.join(work_dir, out_name)
    wall_heights.save_excel(walls, out_path)

    n_walls = len(walls)
    n_with_height = sum(1 for w in walls.values() if w.get("ht_round") is not None)
    summary = (
        f"Calculated heights for {n_with_height} wall(s) (of {n_walls} detected) using "
        f"{project_type} rounding into {out_name}."
    )
    return {
        "status": "ok",
        "summary": summary,
        "artifacts": [
            {"kind": "xlsx", "ref": out_path, "filename": out_name, "title": "Wall heights"}
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

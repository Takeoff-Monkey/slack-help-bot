#!/usr/bin/env python
"""AI-callable entrypoint for the schedule-extractor tool.

The Slack bot's *local* tool-runner backend invokes this as a subprocess from this
directory, using this directory's own venv (so the bot never imports the heavy PDF
deps). The AWS Lambda backend uses lambda/handler.py instead, but both share the
same extraction core in main2.py (process_inputs + save_excel).

Contract
--------
stdin (JSON):
  {
    "input":       {"input_files": ["file_1", "file_2"], "ignore_first_column": true, "skip_pages": []},
    "input_path":  "/abs/path/to/staged.pdf",             # first file (single-file shorthand)
    "input_paths": ["/abs/path/a.jpg", "/abs/path/b.jpg"],# every file the call resolved
    "work_dir":    "/abs/path/to/run-<uuid>",             # where to write the .xlsx + result.json
    "backend":     "local"
  }

Accepts one PDF or up to ten images (photos/screenshots of sheets, or crops of just the
table) in any mix; everything found lands in ONE workbook.

result.json (written into work_dir; also echoed to stdout):
  {
    "status":    "ok" | "error",
    "summary":   "Extracted 3 schedules from 2 images into schedules.xlsx.",
    "artifacts": [{"kind": "xlsx", "ref": "<abs path>", "filename": "...", "title": "..."}],
    "error":     null | "<message>"
  }

The bot strips "ref" before showing the result to the model (the model never needs a
path); it keeps the full record so it can upload the artifact back to Slack.
"""

import json
import os
import re
import sys
import traceback

import main2


def _write_result(work_dir: str, result: dict) -> None:
    with open(os.path.join(work_dir, "result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f)


def _err(message: str) -> dict:
    return {"status": "error", "summary": "", "artifacts": [], "error": message}


def _original_name(staged_basename: str) -> str:
    """'file_2-plant schedule.jpg' -> 'plant schedule.jpg' (no-op without a handle prefix).
    The original name is what sheets get named after, so the user can tell which photo a
    sheet came from."""
    return re.sub(r"^file_\d+-", "", staged_basename) or staged_basename


def _input_paths(contract: dict) -> list:
    """Every file this call was given. input_paths is the multi-file form; input_path is the
    single-file shorthand every other tool uses, so accept both."""
    paths = contract.get("input_paths") or []
    if isinstance(paths, str):
        paths = [paths]
    single = contract.get("input_path")
    if single and single not in paths:
        paths = [single, *paths]
    return [p for p in paths if p]


def run(contract: dict) -> dict:
    tool_input = contract.get("input") or {}
    work_dir = contract.get("work_dir") or "."
    paths = _input_paths(contract)

    if not paths:
        return _err("No input file was provided.")
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        return _err(f"Input file(s) not found: {missing!r}.")

    skip_pages = tool_input.get("skip_pages") or []
    ignore_first_column = tool_input.get("ignore_first_column", True)

    sources = []
    for path in paths:
        with open(path, "rb") as f:
            sources.append((_original_name(os.path.basename(path)), f.read()))

    tables, labels, errors = main2.process_inputs(sources, skip_pages=skip_pages)

    if not tables:
        # Every source failing is a real failure; finding nothing in readable files is not.
        if errors and len(errors) == len(sources):
            return _err("Couldn't read any of the attached files: " + "; ".join(errors))
        summary = "No plant legends or material schedules were detected in " + _describe_sources(sources) + "."
        if errors:
            summary += " Couldn't read: " + "; ".join(errors)
        return {"status": "ok", "summary": summary, "artifacts": [], "error": None}

    n_scheds = sum(len(v) for v in tables.values())
    n_entries = len(tables)

    base = os.path.splitext(sources[0][0])[0] if len(sources) == 1 else "schedules"
    out_name = f"{base or 'schedules'}.xlsx"
    out_path = os.path.join(work_dir, out_name)
    main2.save_excel(tables, out_path, ignore_first_column=ignore_first_column, labels=labels)

    # Entries are images or PDF pages depending on what came in; only say which when it's
    # all one kind.
    kinds = {main2.is_image_name(n) for n, _ in sources}
    unit = "image" if kinds == {True} else ("page" if kinds == {False} else "source")
    summary = (
        f"Extracted {n_scheds} schedule{'s' if n_scheds != 1 else ''} "
        f"from {n_entries} {unit}{'s' if n_entries != 1 else ''} into {out_name}."
    )
    if errors:
        summary += " Couldn't read: " + "; ".join(errors)
    return {
        "status": "ok",
        "summary": summary,
        "artifacts": [
            {"kind": "xlsx", "ref": out_path, "filename": out_name, "title": "Extracted schedules"}
        ],
        "error": None,
    }


def _describe_sources(sources: list) -> str:
    if len(sources) == 1:
        return f"{sources[0][0]}"
    images = sum(1 for n, _ in sources if main2.is_image_name(n))
    if images == len(sources):
        return f"the {images} images"
    return f"the {len(sources)} attached files"


def main() -> None:
    work_dir = "."
    try:
        contract = json.load(sys.stdin)
        work_dir = contract.get("work_dir") or "."
        result = run(contract)
    except Exception as err:
        traceback.print_exc(file=sys.stderr)
        result = _err(f"{type(err).__name__}: {err}")

    # result.json is the source of truth (stdout may carry stray prints from deps);
    # stdout is a convenience for humans / quick debugging.
    try:
        _write_result(work_dir, result)
    except Exception:
        traceback.print_exc(file=sys.stderr)
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()

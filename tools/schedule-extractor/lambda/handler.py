"""AWS Lambda handler for the schedule-extractor tool.

Same JSON contract as run.py, but files move through S3 instead of the local disk (sync
Lambda payloads are capped at 6 MB, so the bot always passes attachments by S3 key).
Extraction logic is the shared core in main2.py — unchanged.

Event (from the bot's tool_runner LambdaBackend):
  { "input": {"input_files": ["file_1", "file_2"], "ignore_first_column": true, "skip_pages": []},
    "input_path":  "runs/<id>/input/file_1-site.pdf",      # first file (single-file shorthand)
    "input_paths": ["runs/<id>/input/file_1-a.jpg", ...],  # every file the call resolved
    "work_dir":    "runs/<id>/work-XXXX/output",           # S3 prefix for outputs
    "bucket":      "<scratch bucket>",
    "backend":     "lambda" }

Takes one PDF or up to ten images (photos/screenshots of sheets, or crops of just the
table) in any mix; everything found lands in ONE workbook.

Returns the same result dict run.py writes, with artifact refs as S3 keys.

Credentials: Textract + S3 come from the Lambda execution role (no static keys). Only
ANTHROPIC_API_KEY is needed as an env var, and only if AI_CLEANUP is enabled.
"""

import os
import re
import traceback

import boto3

import main2

s3 = boto3.client("s3")


def handler(event, context):
    try:
        bucket = event.get("bucket") or os.environ.get("SCRATCH_S3_BUCKET")
        if not bucket:
            return _err("No S3 bucket provided (event.bucket / SCRATCH_S3_BUCKET).")
        keys = _input_keys(event)
        if not keys:
            return _err("No input_path/input_paths (S3 key) provided.")

        tool_input = event.get("input") or {}
        out_prefix = (event.get("work_dir") or "output").rstrip("/")
        skip_pages = tool_input.get("skip_pages") or []
        ignore_first_column = tool_input.get("ignore_first_column", True)

        sources = [
            (_original_name(os.path.basename(key)), s3.get_object(Bucket=bucket, Key=key)["Body"].read())
            for key in keys
        ]

        tables, labels, errors = main2.process_inputs(sources, skip_pages=skip_pages)

        if not tables:
            # Every source failing is a real failure; finding nothing in readable files is not.
            if errors and len(errors) == len(sources):
                return _err("Couldn't read any of the attached files: " + "; ".join(errors))
            summary = "No plant legends or material schedules were detected in " + _describe_sources(sources) + "."
            if errors:
                summary += " Couldn't read: " + "; ".join(errors)
            return {"status": "ok", "summary": summary, "artifacts": [], "error": None}

        base = os.path.splitext(sources[0][0])[0] if len(sources) == 1 else "schedules"
        out_name = f"{base or 'schedules'}.xlsx"
        local_path = f"/tmp/{out_name}"
        main2.save_excel(tables, local_path, ignore_first_column=ignore_first_column, labels=labels)

        out_key = f"{out_prefix}/{out_name}"
        with open(local_path, "rb") as f:
            s3.put_object(Bucket=bucket, Key=out_key, Body=f.read())
        os.remove(local_path)

        n_scheds = sum(len(v) for v in tables.values())
        n_entries = len(tables)
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
                {"kind": "xlsx", "ref": out_key, "filename": out_name, "title": "Extracted schedules"}
            ],
            "error": None,
        }
    except Exception as err:
        traceback.print_exc()
        return _err(f"{type(err).__name__}: {err}")


def _input_keys(event) -> list:
    """Every S3 key this call was given. input_paths is the multi-file form; input_path is the
    single-file shorthand every other tool uses, so accept both."""
    keys = event.get("input_paths") or []
    if isinstance(keys, str):
        keys = [keys]
    single = event.get("input_path")
    if single and single not in keys:
        keys = [single, *keys]
    return [k for k in keys if k]


def _original_name(staged_basename: str) -> str:
    """'file_2-plant schedule.jpg' -> 'plant schedule.jpg' (no-op without a handle prefix).
    The original name is what sheets get named after, so the user can tell which photo a
    sheet came from."""
    return re.sub(r"^file_\d+-", "", staged_basename) or staged_basename


def _describe_sources(sources: list) -> str:
    if len(sources) == 1:
        return f"{sources[0][0]}"
    images = sum(1 for n, _ in sources if main2.is_image_name(n))
    if images == len(sources):
        return f"the {images} images"
    return f"the {len(sources)} attached files"


def _err(message: str) -> dict:
    return {"status": "error", "summary": "", "artifacts": [], "error": message}

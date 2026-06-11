"""AWS Lambda handler for the schedule-extractor tool.

Same JSON contract as run.py, but files move through S3 instead of the local disk (sync
Lambda payloads are capped at 6 MB, so the bot always passes the PDF by S3 key). Extraction
logic is the shared core in main2.py — unchanged.

Event (from the bot's tool_runner LambdaBackend):
  { "input": {"ignore_first_column": true, "skip_pages": []},
    "input_path": "runs/<id>/input/file_1-site.pdf",   # S3 key of the staged PDF
    "work_dir":   "runs/<id>/work-XXXX/output",         # S3 prefix for outputs
    "bucket":     "<scratch bucket>",
    "backend":    "lambda" }

Returns the same result dict run.py writes, with artifact refs as S3 keys.

Credentials: Textract + S3 come from the Lambda execution role (no static keys). Only
OPENAI_API_KEY is needed as an env var, and only if GPT_CLEANUP is enabled.
"""

import os
import traceback

import boto3

import main2

s3 = boto3.client("s3")


def handler(event, context):
    try:
        bucket = event.get("bucket") or os.environ.get("SCRATCH_S3_BUCKET")
        if not bucket:
            return _err("No S3 bucket provided (event.bucket / SCRATCH_S3_BUCKET).")
        input_key = event.get("input_path")
        if not input_key:
            return _err("No input_path (S3 key) provided.")

        tool_input = event.get("input") or {}
        out_prefix = (event.get("work_dir") or "output").rstrip("/")
        skip_pages = tool_input.get("skip_pages") or []
        ignore_first_column = tool_input.get("ignore_first_column", True)

        pdf_bytes = s3.get_object(Bucket=bucket, Key=input_key)["Body"].read()
        tables = main2.process_pdf(pdf_bytes, skip_pages=skip_pages)

        if not tables:
            return {
                "status": "ok",
                "summary": "No plant legends or material schedules were detected in this PDF.",
                "artifacts": [],
                "error": None,
            }

        base = os.path.splitext(os.path.basename(input_key))[0] or "schedules"
        out_name = f"{base}.xlsx"
        local_path = f"/tmp/{out_name}"
        main2.save_excel(tables, local_path, ignore_first_column=ignore_first_column)

        out_key = f"{out_prefix}/{out_name}"
        with open(local_path, "rb") as f:
            s3.put_object(Bucket=bucket, Key=out_key, Body=f.read())
        os.remove(local_path)

        n_pages = len(tables)
        n_scheds = sum(len(v) for v in tables.values())
        summary = (
            f"Extracted {n_scheds} schedule{'s' if n_scheds != 1 else ''} "
            f"across {n_pages} page{'s' if n_pages != 1 else ''} into {out_name}."
        )
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


def _err(message: str) -> dict:
    return {"status": "error", "summary": "", "artifacts": [], "error": message}

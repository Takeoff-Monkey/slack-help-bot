"""AWS Lambda handler for the arazoza-formatter tool.

Same JSON contract as run.py, but files move through S3 instead of the local disk. The
formatting logic is the shared core in formatter.py — unchanged.

Event (from the bot's tool_runner LambdaBackend):
  { "input": {"sheet_name": null, "force": false},
    "input_path": "runs/<id>/input/file_1-Arazoza - X.xlsm",   # S3 key of the staged workbook
    "work_dir":   "runs/<id>/work-XXXX/output",                  # S3 prefix for outputs
    "bucket":     "<scratch bucket>",
    "backend":    "lambda" }

Returns the same result dict run.py writes, with the artifact ref as an S3 key.

Credentials: only the scratch S3 bucket is touched, via the Lambda execution role — no
static keys, no other AWS services.
"""

import os
import re
import shutil
import traceback

import boto3

import formatter

s3 = boto3.client("s3")

INPUT_DIR = "/tmp/input"
OUTPUT_DIR = "/tmp/output"


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
        sheet_name = tool_input.get("sheet_name") or None
        force = bool(tool_input.get("force"))

        # /tmp persists across warm invocations — start clean each time. The input keeps its
        # original name (extension included: formatter.format_file keys off it, and the output
        # is named after it).
        for d in (INPUT_DIR, OUTPUT_DIR):
            shutil.rmtree(d, ignore_errors=True)
            os.makedirs(d, exist_ok=True)
        original_name = re.sub(r"^file_\d+-", "", os.path.basename(input_key)) or "worksheet.xlsx"
        local_in = os.path.join(INPUT_DIR, original_name)
        s3.download_file(bucket, input_key, local_in)

        out_name = formatter.output_name_for(original_name)
        local_out = os.path.join(OUTPUT_DIR, out_name)
        try:
            report = formatter.format_file(local_in, local_out, sheet_name=sheet_name, force=force)
        except formatter.FormatterError as err:
            return _err(str(err))

        out_key = f"{out_prefix}/{out_name}"
        with open(local_out, "rb") as f:
            s3.put_object(Bucket=bucket, Key=out_key, Body=f.read())

        kind = out_name.rsplit(".", 1)[-1].lower()
        return {
            "status": "ok",
            "summary": report.summary(original_name, out_name),
            "artifacts": [
                {"kind": kind, "ref": out_key, "filename": out_name, "title": "Formatted Arazoza worksheet"}
            ],
            "error": None,
        }
    except Exception as err:
        traceback.print_exc()
        return _err(f"{type(err).__name__}: {err}")


def _err(message: str) -> dict:
    return {"status": "error", "summary": "", "artifacts": [], "error": message}

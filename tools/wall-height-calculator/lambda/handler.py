"""AWS Lambda handler for the wall-height-calculator tool.

Same JSON contract as run.py, but files move through S3 instead of the local disk (sync
Lambda payloads are capped at 6 MB, so the bot always passes the PDF by S3 key). The
detection + output logic is the shared core in wall_heights.py — unchanged.

Event (from the bot's tool_runner LambdaBackend):
  { "input": {"project_type": "Single-Family", "skip_pages": []},
    "input_path": "runs/<id>/input/file_1-site.pdf",   # S3 key of the staged PDF
    "work_dir":   "runs/<id>/work-XXXX/output",         # S3 prefix for outputs
    "bucket":     "<scratch bucket>",
    "backend":    "lambda" }

Returns the same result dict run.py writes, with the artifact ref as an S3 key.

Credentials: only the scratch S3 bucket is touched, via the Lambda execution role — no
static keys, no other AWS services.
"""

import os
import traceback

import boto3

import wall_heights

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
        project_type = wall_heights.normalize_project_type(tool_input.get("project_type"))
        skip_pages = tool_input.get("skip_pages") or []

        pdf_bytes = s3.get_object(Bucket=bucket, Key=input_key)["Body"].read()
        walls = wall_heights.process_pdf(pdf_bytes, project_type=project_type, skip_pages=skip_pages)

        if not walls:
            return {
                "status": "ok",
                "summary": (
                    "No wall elevation (TW/BW) markups were detected in this PDF, so there "
                    "were no wall heights to calculate."
                ),
                "artifacts": [],
                "error": None,
            }

        base = os.path.splitext(os.path.basename(input_key))[0] or "walls"
        out_name = f"{base}-wall-heights.xlsx"
        local_path = f"/tmp/{out_name}"
        wall_heights.save_excel(walls, local_path)

        out_key = f"{out_prefix}/{out_name}"
        with open(local_path, "rb") as f:
            s3.put_object(Bucket=bucket, Key=out_key, Body=f.read())
        os.remove(local_path)

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
                {"kind": "xlsx", "ref": out_key, "filename": out_name, "title": "Wall heights"}
            ],
            "error": None,
        }
    except Exception as err:
        traceback.print_exc()
        return _err(f"{type(err).__name__}: {err}")


def _err(message: str) -> dict:
    return {"status": "error", "summary": "", "artifacts": [], "error": message}

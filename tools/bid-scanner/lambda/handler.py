"""AWS Lambda handler for the bid-scanner tool.

Same JSON contract as run.py, but files move through S3 instead of the local disk (sync
Lambda payloads are capped at 6 MB, so the bot always passes the PDF by S3 key). The
scan/highlight logic is the shared core in scanner.py — unchanged.

Event (from the bot's tool_runner LambdaBackend):
  { "input": {"keywords": ["fence", "gate"], "skip_pages": []},
    "input_path": "runs/<id>/input/file_1-site.pdf",   # S3 key of the staged PDF
    "work_dir":   "runs/<id>/work-XXXX/output",         # S3 prefix for outputs
    "bucket":     "<scratch bucket>",
    "backend":    "lambda" }

Returns the same result dict run.py writes, with artifact refs as S3 keys.

Credentials: only the scratch S3 bucket is touched, via the Lambda execution role — no
static keys, no other AWS services.
"""

import os
import traceback

import boto3

import scanner

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
        keywords = scanner.normalize_keywords(tool_input.get("keywords"))
        skip_pages = tool_input.get("skip_pages") or []

        pdf_bytes = s3.get_object(Bucket=bucket, Key=input_key)["Body"].read()
        highlighted, rows = scanner.scan_pdf(pdf_bytes, keywords=keywords, skip_pages=skip_pages)

        base = os.path.splitext(os.path.basename(input_key))[0] or "document"

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
        local_xlsx = f"/tmp/{xlsx_name}"
        scanner.save_excel(rows, local_xlsx)

        pdf_key = f"{out_prefix}/{pdf_name}"
        xlsx_key = f"{out_prefix}/{xlsx_name}"
        s3.put_object(Bucket=bucket, Key=pdf_key, Body=highlighted)
        with open(local_xlsx, "rb") as f:
            s3.put_object(Bucket=bucket, Key=xlsx_key, Body=f.read())
        os.remove(local_xlsx)

        total = sum(r[1] for r in rows)
        summary = (
            f"Scanned {base} for {len(keywords)} keyword(s): {total} match(es) across "
            f"{len(rows)} keyword(s). Highlighted the PDF and wrote a keyword tally."
        )
        return {
            "status": "ok",
            "summary": summary,
            "artifacts": [
                {"kind": "pdf", "ref": pdf_key, "filename": pdf_name, "title": "Highlighted PDF"},
                {"kind": "xlsx", "ref": xlsx_key, "filename": xlsx_name, "title": "Keyword tally"},
            ],
            "error": None,
        }
    except Exception as err:
        traceback.print_exc()
        return _err(f"{type(err).__name__}: {err}")


def _err(message: str) -> dict:
    return {"status": "error", "summary": "", "artifacts": [], "error": message}

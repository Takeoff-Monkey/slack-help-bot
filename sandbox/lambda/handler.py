"""AWS Lambda handler for the run_code sandbox.

The Lambda is the isolation boundary: it has a least-privilege role (scratch bucket only,
no Textract/other AWS), and should be deployed with no network egress. Within it, the
model's code runs as a SEPARATE subprocess with a scrubbed environment, so even the Lambda's
own role credentials (AWS_* env vars) are not visible to the model code — it can only read
INPUT_FILE and write OUTPUT_DIR.

Event (from sandbox._run_lambda):
  { "code": "<python>", "input_path": "<s3 key|null>",
    "work_dir": "runs/<id>/sandbox-XXXX/output", "bucket": "<scratch>", "backend": "lambda" }
"""

import json
import os
import shutil
import subprocess
import sys
import traceback

import boto3

s3 = boto3.client("s3")

TIMEOUT = int(os.environ.get("SANDBOX_TIMEOUT_SECONDS", "120"))
MAX_OUTPUT_CHARS = 8_000
OUTPUT_DIR = "/tmp/output"
SNIPPET = "/tmp/snippet.py"
INPUT_PATH = "/tmp/input_file"


def handler(event, context):
    out_prefix = (event.get("work_dir") or "output").rstrip("/")
    bucket = event.get("bucket") or os.environ.get("SCRATCH_S3_BUCKET")
    try:
        code = event.get("code") or ""
        if not code.strip():
            return _err("No code provided.")
        if not bucket:
            return _err("No S3 bucket provided (event.bucket / SCRATCH_S3_BUCKET).")

        # /tmp persists across warm invocations — start clean each time.
        shutil.rmtree(OUTPUT_DIR, ignore_errors=True)
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        for stale in (SNIPPET, INPUT_PATH):
            if os.path.exists(stale):
                os.remove(stale)

        input_key = event.get("input_path")
        if input_key:
            s3.download_file(bucket, input_key, INPUT_PATH)

        with open(SNIPPET, "w", encoding="utf-8") as f:
            f.write(code)

        # Scrubbed env: PATH + PYTHONPATH (to reach the image's site-packages) + the two
        # contract vars. NO AWS_*/secret vars — model code cannot touch the role creds.
        # TESSDATA_PREFIX points pytesseract at the language data installed in the image;
        # OMP_NUM_THREADS keeps onnxruntime/opencv from oversubscribing the function's vCPUs.
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONPATH": os.environ.get("LAMBDA_TASK_ROOT", "/var/task"),
            "HOME": "/tmp",
            "LANG": "C.UTF-8",
            "PYTHONUNBUFFERED": "1",
            "OUTPUT_DIR": OUTPUT_DIR,
            "OMP_NUM_THREADS": os.environ.get("SANDBOX_OMP_NUM_THREADS", "4"),
        }
        if os.environ.get("TESSDATA_PREFIX"):
            env["TESSDATA_PREFIX"] = os.environ["TESSDATA_PREFIX"]
        if input_key:
            env["INPUT_FILE"] = INPUT_PATH

        proc = subprocess.run(
            [sys.executable, SNIPPET],
            cwd="/tmp",
            env=env,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )

        artifacts = []
        for name in sorted(os.listdir(OUTPUT_DIR)):
            p = os.path.join(OUTPUT_DIR, name)
            if not os.path.isfile(p):
                continue
            key = f"{out_prefix}/{name}"
            with open(p, "rb") as fh:
                s3.put_object(Bucket=bucket, Key=key, Body=fh.read())
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else "bin"
            artifacts.append({"kind": ext, "ref": key, "filename": name, "title": name})

        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-MAX_OUTPUT_CHARS:]
            return {"status": "error", "summary": "", "artifacts": artifacts,
                    "error": f"Script exited {proc.returncode}: {tail}"}

        summary = ""
        out = (proc.stdout or "").strip()
        if out:
            try:
                summary = (json.loads(out.splitlines()[-1]) or {}).get("summary", "")
            except Exception:
                summary = ""
        if not summary:
            summary = f"Ran custom code; produced {len(artifacts)} file(s)." if artifacts else "Ran custom code."
        return {"status": "ok", "summary": summary, "artifacts": artifacts, "error": None}

    except subprocess.TimeoutExpired:
        return _err(f"Code timed out after {TIMEOUT}s.")
    except Exception as err:
        traceback.print_exc()
        return _err(f"{type(err).__name__}: {err}")


def _err(message: str) -> dict:
    return {"status": "error", "summary": "", "artifacts": [], "error": message}

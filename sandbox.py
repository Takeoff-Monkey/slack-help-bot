"""The `run_code` fallback — sandboxed execution of model-written Python.

This is what lets the bot "attempt anything left over itself" after the specialized tools
have done their part (e.g. highlight a word across a PDF with PyMuPDF). It is deliberately
the *last resort*: ACTION_SYSTEM tells the model to prefer a registered tool whenever one
fits, and reach for run_code only when none does.

Backends mirror tool_runner:
  - local  → a SEPARATE subprocess with a throwaway cwd, a SCRUBBED environment (the bot's
             Slack/Anthropic/AWS/OpenAI secrets are never exposed to model code), and a hard
             timeout. The bot never exec/eval's model code in-process.
  - lambda → a dedicated least-privilege Lambda (no egress, scratch-bucket-only IAM). Built
             in sandbox/lambda/, wired in Phase 4.

Known local limitation: a subprocess can't cheaply block network egress, so locally treat
run_code as trusted-internal-but-observable. The Lambda is the real isolation boundary.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path

import tool_runner
from tool_runner import ToolInvocationResult

SANDBOX_DIR = Path(__file__).parent / "sandbox"
MAX_CODE_CHARS = 60_000
MAX_OUTPUT_CHARS = 8_000
SANDBOX_TIMEOUT = int(os.environ.get("SANDBOX_TIMEOUT_SECONDS", "120"))
SANDBOX_BACKEND = os.environ.get("TOOL_BACKEND", "local")  # follows the tool backend


def run_code_tool_def() -> dict:
    return {
        "name": "run_code",
        "description": (
            "Execute a short Python 3 script in a sandbox to perform an operation no other "
            "tool covers (e.g. highlight a word in a PDF, split/merge pages, convert a file). "
            "Use this only when no registered tool fits.\n\n"
            "Environment available to your script:\n"
            "- `fitz` (PyMuPDF), `pandas`, `PIL` (Pillow), and the Python standard library.\n"
            "- env var `INPUT_FILE`: absolute path to the attached file you named in `input_file` "
            "(absent if you didn't name one).\n"
            "- env var `OUTPUT_DIR`: write every file you want returned to the user into this "
            "directory. Anything written there is uploaded back to Slack automatically.\n"
            "- No network access. One shot per call (no state persists between calls).\n"
            "Optionally print a single final line of JSON like {\"summary\": \"...\"} to describe "
            "what you did; otherwise a generic summary is used."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "The Python 3 script to run."},
                "input_file": {
                    "type": "string",
                    "description": "Optional handle (e.g. file_1) of an attached file to expose as INPUT_FILE.",
                },
            },
            "required": ["code"],
            "additionalProperties": False,
        },
    }


def _sandbox_python() -> str:
    venv = SANDBOX_DIR / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else "python3"


def _collect_artifacts(output_dir: str) -> list[dict]:
    arts = []
    for name in sorted(os.listdir(output_dir)):
        p = os.path.join(output_dir, name)
        if not os.path.isfile(p):
            continue
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else "bin"
        arts.append({"kind": ext, "ref": p, "filename": name, "title": name})
    return arts


def _run_local(code: str, input_path: str | None, staging, logger) -> ToolInvocationResult:
    run_dir = os.path.join(staging.root, f"sandbox-{uuid.uuid4().hex[:8]}")
    output_dir = os.path.join(run_dir, "output")
    os.makedirs(output_dir, exist_ok=True)
    snippet = os.path.join(run_dir, "snippet.py")
    with open(snippet, "w", encoding="utf-8") as f:
        f.write(code)

    # Scrubbed env — explicitly allowlisted, so process-env secrets never reach model code.
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": run_dir,
        "LANG": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "OUTPUT_DIR": output_dir,
    }
    if input_path:
        env["INPUT_FILE"] = input_path

    try:
        proc = subprocess.run(
            [_sandbox_python(), snippet],
            cwd=run_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=SANDBOX_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return ToolInvocationResult.err(f"Code timed out after {SANDBOX_TIMEOUT}s.", work_dir=run_dir)

    artifacts = _collect_artifacts(output_dir)
    stdout = (proc.stdout or "").strip()

    if proc.returncode != 0:
        tail = (proc.stderr or stdout or "").strip()[-MAX_OUTPUT_CHARS:]
        return ToolInvocationResult(
            status="error",
            summary="",
            artifacts=artifacts,  # surface partial outputs if any
            error=f"Script exited {proc.returncode}: {tail}",
            work_dir=run_dir,
        )

    summary = ""
    if stdout:
        try:
            summary = (json.loads(stdout.splitlines()[-1]) or {}).get("summary", "")
        except Exception:
            summary = ""
    if not summary:
        summary = f"Ran custom code; produced {len(artifacts)} file(s)." if artifacts else "Ran custom code."
    return ToolInvocationResult(status="ok", summary=summary, artifacts=artifacts, work_dir=run_dir)


def _run_lambda(code: str, input_key: str | None, staging, logger) -> ToolInvocationResult:
    fn = os.environ.get("SANDBOX_LAMBDA_NAME", "tm-sandbox-runcode")
    out_prefix = f"{staging.root}/sandbox-{uuid.uuid4().hex[:8]}/output"
    payload = {"code": code, "input_path": input_key, "work_dir": out_prefix,
               "bucket": staging.bucket, "backend": "lambda"}
    try:
        # read_timeout must exceed the sandbox function's own timeout (130s) so we wait for
        # its real result; retries OFF so a hanging run isn't re-invoked into a multi-minute
        # stall (the bug that previously blocked the whole turn). See tool_runner.lambda_client.
        client = tool_runner.lambda_client(SANDBOX_TIMEOUT + 40)
        resp = client.invoke(FunctionName=fn, InvocationType="RequestResponse",
                             Payload=json.dumps(payload).encode("utf-8"))
    except Exception as err:
        logger.exception("Sandbox Lambda invoke failed")
        return ToolInvocationResult.err(f"Sandbox invoke failed: {err}", work_dir=out_prefix)
    body = resp["Payload"].read().decode("utf-8", errors="replace")
    try:
        raw = json.loads(body)
    except Exception:
        return ToolInvocationResult.err(f"Unparseable sandbox response: {body[:500]}", work_dir=out_prefix)
    if resp.get("FunctionError"):
        return ToolInvocationResult.err(f"Sandbox raised: {raw}", work_dir=out_prefix)
    return ToolInvocationResult(status=raw.get("status", "error"), summary=raw.get("summary", ""),
                                artifacts=raw.get("artifacts") or [], error=raw.get("error"), work_dir=out_prefix)


def run_code(tool_input: dict, staging, logger) -> ToolInvocationResult:
    """Execute model-written code. Always returns a ToolInvocationResult (never raises)."""
    code = tool_input.get("code") or ""
    if not code.strip():
        return ToolInvocationResult.err("No code provided.")
    if len(code) > MAX_CODE_CHARS:
        return ToolInvocationResult.err(f"Code too long ({len(code)} chars, max {MAX_CODE_CHARS}).")

    input_path = None
    handle = tool_input.get("input_file")
    if handle:
        staged = staging.by_handle().get(handle)
        if staged is None:
            return ToolInvocationResult.err(
                f"No attached file with handle {handle!r}. Available: {sorted(staging.by_handle()) or 'none'}."
            )
        input_path = staged.ref

    try:
        if SANDBOX_BACKEND == "lambda":
            return _run_lambda(code, input_path, staging, logger)
        return _run_local(code, input_path, staging, logger)
    except Exception as err:
        logger.exception("Sandbox crashed")
        return ToolInvocationResult.err(f"{type(err).__name__}: {err}")

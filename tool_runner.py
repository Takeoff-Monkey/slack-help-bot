"""Run a registered tool through a pluggable backend.

One interface, two backends, chosen by TOOL_BACKEND:
  - local  → LocalSubprocessBackend: run the tool's run.py as a child process in the
             tool's own venv (contract JSON on stdin, result from work_dir/result.json).
             Proves the whole flow end-to-end with no cloud.
  - lambda → LambdaBackend: invoke the tool's AWS Lambda; files pass by S3 key. Flip with
             TOOL_BACKEND=lambda once deployed — nothing else in the bot changes.

run_tool() validates the call and NEVER raises into the agent loop: every failure becomes a
status="error" result the model can read and react to.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from dataclasses import dataclass, field

import slack_files

TOOL_BACKEND = os.environ.get("TOOL_BACKEND", "local")


@dataclass
class ToolInvocationResult:
    status: str                       # "ok" | "error"
    summary: str
    artifacts: list = field(default_factory=list)   # [{kind, ref, filename, title}]
    error: str | None = None
    work_dir: str | None = None       # local dir | S3 prefix — for cleanup, never shown to model

    def model_view(self) -> dict:
        """What the model sees in the tool_result — strips filesystem/S3 refs."""
        return {
            "status": self.status,
            "summary": self.summary,
            "artifacts": [{k: v for k, v in a.items() if k != "ref"} for a in self.artifacts],
            "error": self.error,
        }

    @classmethod
    def err(cls, message: str, work_dir: str | None = None) -> "ToolInvocationResult":
        return cls(status="error", summary="", artifacts=[], error=message, work_dir=work_dir)


def _resolve_input(spec, tool_input: dict, by_handle: dict):
    """Resolve the input_file handle to a StagedFile and validate the accepted type.
    Returns (staged, error_message)."""
    handle = tool_input.get("input_file")
    if handle is None:
        # Tool declared input_file required (manifest) but model omitted it.
        if "input_file" in (spec.input_schema.get("required") or []):
            return None, "Missing required 'input_file' handle."
        return None, None
    staged = by_handle.get(handle)
    if staged is None:
        return None, f"No attached file with handle {handle!r}. Available: {sorted(by_handle) or 'none'}."
    allowed = [t.lower() for t in (spec.accepts or {}).get("file_types", [])]
    if allowed:
        ext = staged.filename.rsplit(".", 1)[-1].lower() if "." in staged.filename else ""
        if ext not in allowed:
            return None, f"{staged.filename!r} is not an accepted type for {spec.name} (needs {allowed})."
    return staged, None


def _local_work_dir(staging) -> str:
    d = os.path.join(staging.root, f"work-{uuid.uuid4().hex[:8]}")
    os.makedirs(d, exist_ok=True)
    return d


def _s3_work_prefix(staging) -> str:
    return f"{staging.root}/work-{uuid.uuid4().hex[:8]}/output"


class LocalSubprocessBackend:
    def invoke(self, spec, tool_input, staged, work_dir, logger) -> ToolInvocationResult:
        venv_python = spec.dir / ".venv" / "bin" / "python"
        if not venv_python.exists():
            return ToolInvocationResult.err(
                f"Tool {spec.name!r} isn't set up yet (missing .venv). Run tools/{spec.name}/setup.sh.",
                work_dir=work_dir,
            )
        cmd = (spec.entrypoint.get("local") or {}).get("cmd") or ["run.py"]
        contract = {
            "input": tool_input,
            "input_path": staged.ref if staged else None,
            "work_dir": work_dir,
            "backend": "local",
        }
        try:
            proc = subprocess.run(
                [str(venv_python), *cmd],
                cwd=str(spec.dir),
                input=json.dumps(contract),
                capture_output=True,
                text=True,
                timeout=spec.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return ToolInvocationResult.err(
                f"{spec.name} timed out after {spec.timeout_seconds}s.", work_dir=work_dir
            )
        return _result_from_local(work_dir, proc, logger)


class LambdaBackend:
    def invoke(self, spec, tool_input, staged, work_dir, logger) -> ToolInvocationResult:
        fn = (spec.entrypoint.get("lambda") or {}).get("function_name")
        if not fn:
            return ToolInvocationResult.err(f"{spec.name} has no lambda.function_name in its manifest.", work_dir=work_dir)
        payload = {
            "input": tool_input,
            "input_path": staged.ref if staged else None,   # S3 key
            "work_dir": work_dir,                            # S3 output prefix
            "bucket": slack_files.SCRATCH_S3_BUCKET,         # shared scratch bucket
            "backend": "lambda",
        }
        try:
            # Wait a bit longer than the function's own timeout so we receive its real
            # result instead of timing out while it finishes (and writes to S3).
            client = lambda_client(spec.timeout_seconds + 30)
            resp = client.invoke(
                FunctionName=fn,
                InvocationType="RequestResponse",
                Payload=json.dumps(payload).encode("utf-8"),
            )
        except Exception as err:
            logger.exception("Lambda invoke failed for %s", fn)
            return ToolInvocationResult.err(f"Lambda invoke failed: {err}", work_dir=work_dir)

        body = resp["Payload"].read().decode("utf-8", errors="replace")
        try:
            raw = json.loads(body)
            if isinstance(raw, dict) and "status" not in raw and "body" in raw:  # API-Gateway-style wrap
                raw = json.loads(raw["body"]) if isinstance(raw["body"], str) else raw["body"]
        except Exception:
            return ToolInvocationResult.err(f"Unparseable Lambda response: {body[:500]}", work_dir=work_dir)
        if resp.get("FunctionError"):
            return ToolInvocationResult.err(f"Lambda raised: {raw}", work_dir=work_dir)
        return ToolInvocationResult(
            status=raw.get("status", "error"),
            summary=raw.get("summary", ""),
            artifacts=raw.get("artifacts") or [],
            error=raw.get("error"),
            work_dir=work_dir,
        )


def _result_from_local(work_dir, proc, logger) -> ToolInvocationResult:
    """Prefer work_dir/result.json (source of truth); fall back to the last stdout line;
    else synthesize an error from stderr."""
    raw = None
    rp = os.path.join(work_dir, "result.json")
    if os.path.exists(rp):
        try:
            with open(rp, encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            logger.exception("Failed to parse %s", rp)
    if raw is None and proc.stdout:
        try:
            raw = json.loads(proc.stdout.strip().splitlines()[-1])
        except Exception:
            pass
    if raw is None:
        tail = (proc.stderr or "").strip()[-600:]
        return ToolInvocationResult.err(
            f"Tool produced no parseable result (exit {proc.returncode}). {tail}", work_dir=work_dir
        )
    return ToolInvocationResult(
        status=raw.get("status", "error"),
        summary=raw.get("summary", ""),
        artifacts=raw.get("artifacts") or [],
        error=raw.get("error"),
        work_dir=work_dir,
    )


def lambda_client(read_timeout: int):
    """A boto3 Lambda client tuned for synchronous (RequestResponse) invokes of
    long-running functions: read_timeout must exceed the function's own timeout, and
    retries are OFF — botocore's default 60s read_timeout + auto-retries would (a) give up
    while the function is still running (the result lands in S3 but the bot never sees it)
    and (b) re-invoke the still-running function, multiplying the wait into minutes."""
    import boto3
    from botocore.config import Config

    cfg = Config(
        connect_timeout=15,
        read_timeout=read_timeout,
        retries={"total_max_attempts": 1},
        tcp_keepalive=True,
    )
    return boto3.client("lambda", config=cfg)


def get_backend():
    return LambdaBackend() if TOOL_BACKEND == "lambda" else LocalSubprocessBackend()


def run_tool(spec, tool_input: dict, staging: "slack_files.Staging", logger) -> ToolInvocationResult:
    """Validate, pick a work dir, and dispatch to the active backend. Always returns a
    ToolInvocationResult (never raises)."""
    by_handle = staging.by_handle()
    staged, err = _resolve_input(spec, tool_input, by_handle)
    if err:
        return ToolInvocationResult.err(err)
    work_dir = _s3_work_prefix(staging) if staging.backend == "lambda" else _local_work_dir(staging)
    try:
        return get_backend().invoke(spec, tool_input, staged, work_dir, logger)
    except Exception as err:
        logger.exception("Backend crashed running %s", spec.name)
        return ToolInvocationResult.err(f"{type(err).__name__}: {err}", work_dir=work_dir)

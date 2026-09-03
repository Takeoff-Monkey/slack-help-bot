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
import time
import uuid
from dataclasses import dataclass, field

import slack_files

TOOL_BACKEND = os.environ.get("TOOL_BACKEND", "local")

# Cold-start headroom. Every tool and the sandbox are *container-image* Lambdas (1–3GB), so a
# function that has gone cold needs tens of seconds to pull its image and boot. Two knobs:
#   GRACE     — extra read-timeout on top of the function's own timeout, because that boot
#               happens INSIDE the invoke: the client must be willing to wait for boot + run.
#   MAX_WAIT  — total time we'll spend waiting *between* attempts when Lambda answers "not
#               ready / throttled" instead of running (see invoke_lambda).
LAMBDA_COLD_START_GRACE = int(os.environ.get("LAMBDA_COLD_START_GRACE_SECONDS", "90"))
LAMBDA_COLD_START_MAX_WAIT = int(os.environ.get("LAMBDA_COLD_START_MAX_WAIT_SECONDS", "65"))

# Escalating waits between attempts, trimmed to MAX_WAIT. Deliberately starts at 5s, not ~1s:
# a booting container gains nothing from being asked again immediately.
_COLD_START_STEPS = (5, 10, 20, 30)

# Lambda error codes that mean "not ready yet, ask again" rather than "this call is wrong".
# Anything else (bad payload, AccessDenied, ResourceNotFound) fails immediately — no amount
# of waiting fixes those.
_NOT_READY_CODES = {
    "TooManyRequestsException",     # throttled — typical when a cold start bursts concurrency
    "ResourceNotReadyException",    # function/ENI still initializing after idle
    "ResourceConflictException",    # function still updating (e.g. just after a deploy)
    "ServiceException",             # transient Lambda-side 500
    "ServiceUnavailableException",
    "EC2ThrottledException",
    "RequestTimeoutException",
}


class ColdStartTimeout(RuntimeError):
    """The function was still coming up after we'd waited our whole budget."""


def _not_ready(err: Exception) -> bool:
    from botocore.exceptions import (
        ClientError,
        ConnectionClosedError,
        ConnectTimeoutError,
        EndpointConnectionError,
    )

    # These never reached the service (or died before a response), so a retry is safe.
    # NOTE: ReadTimeoutError is deliberately absent — the function is probably still running,
    # and re-invoking would double the wait instead of shortening it.
    if isinstance(err, (ConnectTimeoutError, ConnectionClosedError, EndpointConnectionError)):
        return True
    if isinstance(err, ClientError):
        return err.response.get("Error", {}).get("Code") in _NOT_READY_CODES
    return False


def _cold_start_waits() -> list[int]:
    waits, spent = [], 0
    for step in _COLD_START_STEPS:
        if spent >= LAMBDA_COLD_START_MAX_WAIT:
            break
        step = min(step, LAMBDA_COLD_START_MAX_WAIT - spent)
        waits.append(step)
        spent += step
    return waits


def invoke_lambda(function_name: str, payload: dict, read_timeout: int, logger,
                  notify=None, label: str | None = None):
    """Invoke a Lambda synchronously, waiting *patiently* through a cold start.

    Lambda answers a not-ready/throttled invoke in milliseconds. Handing that straight back
    to the model made it re-run the tool about a second later — hammering a function that
    only needed time to boot. So the waiting happens here, in escalating steps, and the model
    only ever sees the result of an attempt that actually ran.

    `notify(msg)` (optional) surfaces the wait to the user instead of leaving them staring at
    a silent placeholder. Raises ColdStartTimeout if the budget runs out; every other
    exception (a real failure) is raised on the first attempt.
    """
    client = lambda_client(read_timeout)
    label = label or function_name
    last_err = None
    waited = 0
    for wait in [0, *_cold_start_waits()]:
        if wait:
            waited += wait
            logger.info("Lambda %s not ready (%s); waiting %ds before retrying (total %ds)",
                        function_name, last_err, wait, waited)
            if notify:
                try:
                    notify(f"{label} is still booting up — giving it {wait} more seconds…")
                except Exception:
                    logger.exception("cold-start notify failed (continuing)")
            time.sleep(wait)
        try:
            return client.invoke(
                FunctionName=function_name,
                InvocationType="RequestResponse",
                Payload=json.dumps(payload).encode("utf-8"),
            )
        except Exception as err:
            if not _not_ready(err):
                raise
            last_err = err
    raise ColdStartTimeout(
        f"{label} was still starting up after {waited}s of waiting ({last_err})."
    ) from last_err


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
    def invoke(self, spec, tool_input, staged, work_dir, logger, notify=None) -> ToolInvocationResult:
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
    def invoke(self, spec, tool_input, staged, work_dir, logger, notify=None) -> ToolInvocationResult:
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
            # Read timeout = the function's own timeout + cold-start grace, so we receive its
            # real result instead of giving up while it boots or finishes (and writes to S3).
            resp = invoke_lambda(
                fn, payload,
                read_timeout=spec.timeout_seconds + LAMBDA_COLD_START_GRACE,
                logger=logger, notify=notify, label=spec.name,
            )
        except ColdStartTimeout as err:
            logger.warning("Cold-start budget exhausted for %s: %s", fn, err)
            return ToolInvocationResult.err(
                f"{err} Tell the user it needs another moment and let them ask again — "
                f"do NOT immediately re-run this tool.",
                work_dir=work_dir,
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


def run_tool(spec, tool_input: dict, staging: "slack_files.Staging", logger, notify=None) -> ToolInvocationResult:
    """Validate, pick a work dir, and dispatch to the active backend. Always returns a
    ToolInvocationResult (never raises)."""
    by_handle = staging.by_handle()
    staged, err = _resolve_input(spec, tool_input, by_handle)
    if err:
        return ToolInvocationResult.err(err)
    work_dir = _s3_work_prefix(staging) if staging.backend == "lambda" else _local_work_dir(staging)
    try:
        return get_backend().invoke(spec, tool_input, staged, work_dir, logger, notify)
    except Exception as err:
        logger.exception("Backend crashed running %s", spec.name)
        return ToolInvocationResult.err(f"{type(err).__name__}: {err}", work_dir=work_dir)

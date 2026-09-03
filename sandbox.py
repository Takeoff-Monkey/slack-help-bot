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

import concurrent.futures
import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

import tool_runner
from tool_runner import ToolInvocationResult

SANDBOX_DIR = Path(__file__).parent / "sandbox"
MAX_CODE_CHARS = 60_000
MAX_OUTPUT_CHARS = 8_000
SANDBOX_TIMEOUT = int(os.environ.get("SANDBOX_TIMEOUT_SECONDS", "120"))
SANDBOX_BACKEND = os.environ.get("TOOL_BACKEND", "local")  # follows the tool backend

# Boot the sandbox ahead of time (see prewarm). On by default: a warm ping costs a few
# milliseconds of Lambda time and buys back the whole cold start.
SANDBOX_PREWARM = os.environ.get("SANDBOX_PREWARM", "1").lower() not in ("0", "false", "no")
# Don't re-ping a function we already warmed this recently — Lambda keeps an idle execution
# environment around far longer than this.
PREWARM_TTL_SECONDS = int(os.environ.get("SANDBOX_PREWARM_TTL_SECONDS", "240"))


def run_code_tool_def() -> dict:
    return {
        "name": "run_code",
        "description": (
            "Execute a short Python 3 script in a sandbox to perform an operation no registered "
            "tool covers — e.g. OCR a scanned/raster image or a text-less PDF, highlight a word "
            "in a PDF, split/merge/convert files, or build a spreadsheet/Word/PDF. Use this only "
            "when no registered tool fits.\n\n"
            "Two environments are available via the `environment` field:\n"
            "- \"default\" (use this FIRST): Tesseract OCR (`pytesseract` + the `tesseract` "
            "binary), `cv2` (OpenCV, image preprocessing), `fitz` (PyMuPDF), `pdfplumber`, "
            "`pdf2image`, `pandas`, `numpy`, `PIL` (Pillow), `openpyxl`/`xlsxwriter`, "
            "`docx` (python-docx), `pptx` (python-pptx), `reportlab`, `tabulate`, and the "
            "standard library.\n"
            "- \"neural_ocr\": everything in default PLUS `rapidocr_onnxruntime` (RapidOCR), a "
            "neural OCR engine that is far more accurate on messy, rotated, low-quality, or "
            "photographed scans but is slower to start. Escalate to it ONLY if you already ran "
            "OCR in the default environment and the text came back garbled, empty, or "
            "low-confidence.\n\n"
            "OCR tips: for raster images, preprocess with cv2 (grayscale, ~2x upscale, Otsu "
            "threshold, deskew) before Tesseract — it substantially improves accuracy. Inspect "
            "`pytesseract.image_to_data(..., output_type=Output.DICT)` word confidences to judge "
            "whether the result is good enough or you should escalate to \"neural_ocr\".\n\n"
            "Environment available to your script:\n"
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
                "environment": {
                    "type": "string",
                    "enum": ["default", "neural_ocr"],
                    "default": "default",
                    "description": (
                        "Which sandbox environment to run in. Use \"default\" first. Escalate to "
                        "\"neural_ocr\" only when default-environment OCR produced poor, garbled, "
                        "or empty text."
                    ),
                },
            },
            "required": ["code"],
            "additionalProperties": False,
        },
    }


# Local venv directory per environment; the neural venv is a superset of the default one.
_VENV_BY_ENV = {"default": ".venv", "neural_ocr": ".venv-ocr"}


def _sandbox_python(environment: str = "default") -> Path:
    """Path to the venv interpreter for an environment. May not exist yet (setup.sh not run for
    this tier) — the caller checks and returns a clear error rather than silently degrading."""
    return SANDBOX_DIR / _VENV_BY_ENV.get(environment, ".venv") / "bin" / "python"


def _collect_artifacts(output_dir: str) -> list[dict]:
    arts = []
    for name in sorted(os.listdir(output_dir)):
        p = os.path.join(output_dir, name)
        if not os.path.isfile(p):
            continue
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else "bin"
        arts.append({"kind": ext, "ref": p, "filename": name, "title": name})
    return arts


def _run_local(code: str, input_path: str | None, staging, logger, environment: str = "default") -> ToolInvocationResult:
    # Fail clearly if this environment's venv isn't built (e.g. setup.sh not re-run after the
    # neural tier was added), instead of silently running under the system python (which lacks
    # every sandbox dep and would surface an opaque ModuleNotFoundError). Mirrors tool_runner.
    venv_python = _sandbox_python(environment)
    if not venv_python.exists():
        return ToolInvocationResult.err(
            f"The {environment!r} sandbox isn't set up yet (missing sandbox/{_VENV_BY_ENV[environment]}). "
            f"Run sandbox/setup.sh to build it."
        )

    run_dir = os.path.join(staging.root, f"sandbox-{uuid.uuid4().hex[:8]}")
    output_dir = os.path.join(run_dir, "output")
    os.makedirs(output_dir, exist_ok=True)
    snippet = os.path.join(run_dir, "snippet.py")
    with open(snippet, "w", encoding="utf-8") as f:
        f.write(code)

    # Scrubbed env — explicitly allowlisted, so process-env secrets never reach model code.
    # OMP/onnxruntime thread caps keep a neural-OCR run from oversubscribing every core.
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": run_dir,
        "LANG": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "OUTPUT_DIR": output_dir,
        "OMP_NUM_THREADS": os.environ.get("SANDBOX_OMP_NUM_THREADS", "4"),
    }
    if input_path:
        env["INPUT_FILE"] = input_path

    try:
        proc = subprocess.run(
            [str(venv_python), snippet],
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


def _lambda_function_name(environment: str) -> str:
    """The default and neural-OCR sandboxes are two separate Lambdas (different images)."""
    if environment == "neural_ocr":
        return os.environ.get("SANDBOX_LAMBDA_NAME_OCR", "tm-sandbox-runcode-ocr")
    return os.environ.get("SANDBOX_LAMBDA_NAME", "tm-sandbox-runcode")


# One background worker whose only job is holding a warm-up invoke open while it boots.
_PREWARM_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="prewarm")
_prewarm_lock = threading.Lock()
_prewarm: dict[str, tuple[float, concurrent.futures.Future]] = {}   # env -> (started_at, future)


def _ping(fn: str, logger) -> None:
    """A do-nothing invoke whose only purpose is to make Lambda boot the container."""
    # `warmup` is understood by the current handler; older deployed handlers stop just as
    # early on the empty `code`, so this is a fast no-op either way.
    client = tool_runner.lambda_client(tool_runner.LAMBDA_COLD_START_GRACE + 30)
    started = time.monotonic()
    client.invoke(FunctionName=fn, InvocationType="RequestResponse",
                  Payload=json.dumps({"warmup": True, "code": ""}).encode("utf-8"))
    logger.info("sandbox prewarm: %s ready after %.1fs", fn, time.monotonic() - started)


def prewarm(logger, environment: str = "default") -> None:
    """Start booting the sandbox now, in the background, so it's up by the time it's needed.

    A cold container-image sandbox takes tens of seconds to pull and boot. That used to land
    entirely on the user: the model wrote code, called run_code, and everyone waited (or the
    invoke came back not-ready and the model impatiently re-ran it). Firing the boot at the
    *start* of an action turn overlaps it with the model's own thinking time, and _run_lambda
    waits for this ping before invoking for real — so the real run lands on a warm, idle
    environment instead of racing the boot and starting a second cold one.

    Fire-and-forget and best-effort: any failure here is ignored, the real invoke still runs.
    """
    if not SANDBOX_PREWARM or SANDBOX_BACKEND != "lambda":
        return
    fn = _lambda_function_name(environment)
    with _prewarm_lock:
        started_at, _ = _prewarm.get(environment, (0.0, None))
        if time.monotonic() - started_at < PREWARM_TTL_SECONDS:
            return          # already warm (or warming) — don't pay for a second ping
        logger.info("sandbox prewarm: pinging %s", fn)
        future = _PREWARM_POOL.submit(_ping, fn, logger)
        _prewarm[environment] = (time.monotonic(), future)


def _await_prewarm(environment: str, logger, notify=None) -> None:
    """Block until an in-flight warm-up finishes, so we don't invoke into a booting function
    (Lambda would spin up a SECOND cold environment rather than queue behind the first)."""
    with _prewarm_lock:
        entry = _prewarm.get(environment)
    if not entry:
        return
    started_at, future = entry
    if future.done():
        return
    budget = max(0, tool_runner.LAMBDA_COLD_START_GRACE - int(time.monotonic() - started_at))
    if not budget:
        return
    logger.info("sandbox: waiting up to %ds for the %s environment to finish booting",
                budget, environment)
    if notify:
        try:
            notify("The code sandbox is booting up — waiting for it before I run anything…")
        except Exception:
            logger.exception("prewarm notify failed (continuing)")
    try:
        future.result(timeout=budget)
    except Exception:
        logger.info("sandbox prewarm didn't complete; invoking anyway", exc_info=True)


def _run_lambda(code: str, input_key: str | None, staging, logger, environment: str = "default",
                notify=None) -> ToolInvocationResult:
    fn = _lambda_function_name(environment)
    out_prefix = f"{staging.root}/sandbox-{uuid.uuid4().hex[:8]}/output"
    payload = {"code": code, "input_path": input_key, "work_dir": out_prefix,
               "bucket": staging.bucket, "backend": "lambda"}
    # If a warm-up ping is still booting this environment, wait for it rather than invoking
    # into the boot (which would start a second cold environment and wait twice).
    _await_prewarm(environment, logger, notify)
    try:
        # read_timeout must exceed the sandbox function's own timeout (130s) PLUS a cold boot,
        # which happens inside the invoke; retries OFF so a hanging run isn't re-invoked into a
        # multi-minute stall (the bug that previously blocked the whole turn). invoke_lambda
        # adds its own patient waits for not-ready/throttled responses.
        resp = tool_runner.invoke_lambda(
            fn, payload,
            read_timeout=SANDBOX_TIMEOUT + tool_runner.LAMBDA_COLD_START_GRACE,
            logger=logger, notify=notify, label="The code sandbox",
        )
    except tool_runner.ColdStartTimeout as err:
        logger.warning("Sandbox cold-start budget exhausted: %s", err)
        return ToolInvocationResult.err(
            f"{err} This is a startup delay, not a problem with the code — tell the user the "
            f"sandbox is still warming up and to ask again in a moment. Do NOT immediately "
            f"re-run run_code.",
            work_dir=out_prefix,
        )
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


def run_code(tool_input: dict, staging, logger, notify=None) -> ToolInvocationResult:
    """Execute model-written code. Always returns a ToolInvocationResult (never raises).

    `notify(msg)` (optional) lets the caller surface waiting-on-boot status to the user."""
    code = tool_input.get("code") or ""
    if not code.strip():
        return ToolInvocationResult.err("No code provided.")
    if len(code) > MAX_CODE_CHARS:
        return ToolInvocationResult.err(f"Code too long ({len(code)} chars, max {MAX_CODE_CHARS}).")

    # Which environment to run in. Anything unrecognized falls back to the lean default.
    environment = tool_input.get("environment") or "default"
    if environment not in _VENV_BY_ENV:
        environment = "default"

    input_path = None
    handle = tool_input.get("input_file")
    if handle:
        staged = staging.by_handle().get(handle)
        if staged is None:
            return ToolInvocationResult.err(
                f"No attached file with handle {handle!r}. Available: {sorted(staging.by_handle()) or 'none'}."
            )
        input_path = staged.ref

    logger.info("run_code: backend=%s environment=%s input=%s code_chars=%d",
                SANDBOX_BACKEND, environment, bool(input_path), len(code))
    try:
        if SANDBOX_BACKEND == "lambda":
            return _run_lambda(code, input_path, staging, logger, environment, notify)
        return _run_local(code, input_path, staging, logger, environment)
    except Exception as err:
        logger.exception("Sandbox crashed")
        return ToolInvocationResult.err(f"{type(err).__name__}: {err}")

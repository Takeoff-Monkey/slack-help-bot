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

import ast
import concurrent.futures
import json
import os
import re
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


# --- what the sandbox can and cannot open -----------------------------------------------
# Formats no library in the toolkit can read, mapped to what to ask the teammate for instead.
#
# Why this exists: a legacy .xls attachment once cost an entire turn. The model tried openpyxl,
# then pandas, then probed for xlrd / xlwt / xlutils / pyexcel / olefile / win32com one by one —
# six steps discovering that none was installed and that there is no network to install any of
# them — and the turn ended by asking the teammate to re-upload as .xlsx. That question was
# answerable in the first second, before any work started. (.xls itself is now readable: xlrd
# is in requirements.txt. This table is for the formats that genuinely remain out of reach.)
#
# Keep this HONEST — an entry here makes the bot refuse to try. Anything the toolkit can
# actually open (.xlsx/.xlsm, .xls, .csv, .pdf, .docx, .pptx, images, .zip, text/json/xml)
# must NOT be listed.
UNREADABLE_FORMATS = {
    # Legacy Microsoft binaries — the modern OOXML twin is readable, the old one isn't.
    "doc":     ("a Word 97–2003 binary document", "re-save it as .docx"),
    "ppt":     ("a PowerPoint 97–2003 binary deck", "re-save it as .pptx"),
    "xlsb":    ("a binary Excel workbook", "re-save it as .xlsx"),
    "msg":     ("an Outlook message", "forward it as .eml, or paste the text"),
    "wpd":     ("a WordPerfect document", "re-save it as .docx or PDF"),
    # OpenDocument — no odfpy in the toolkit.
    "odt":     ("an OpenDocument text file", "export it as .docx or PDF"),
    "ods":     ("an OpenDocument spreadsheet", "export it as .xlsx"),
    "odp":     ("an OpenDocument presentation", "export it as .pptx or PDF"),
    # Apple iWork — bundles, not documents.
    "pages":   ("an Apple Pages document", "export it as .docx or PDF"),
    "numbers": ("an Apple Numbers spreadsheet", "export it as .xlsx"),
    "key":     ("an Apple Keynote deck", "export it as .pptx or PDF"),
    # Rich text / images without a decoder here.
    "rtf":     ("a rich-text document", "re-save it as .docx or PDF"),
    "heic":    ("an Apple HEIC image", "export it as .jpg or .png"),
    "heif":    ("a HEIF image", "export it as .jpg or .png"),
    # CAD / BIM — plausible on takeoff work, and nothing here can parse them.
    "dwg":     ("an AutoCAD drawing", "export the sheets as PDF"),
    "dxf":     ("a CAD exchange drawing", "export the sheets as PDF"),
    "rvt":     ("a Revit model", "export the sheets as PDF"),
    "skp":     ("a SketchUp model", "export what you need as PDF or an image"),
    "ifc":     ("an IFC/BIM model", "export a PDF or a schedule as .xlsx"),
    # Archives the stdlib can't open (.zip it can).
    "rar":     ("a RAR archive", "re-compress it as .zip"),
    "7z":      ("a 7-Zip archive", "re-compress it as .zip"),
}


def _ext(filename: str) -> str:
    return (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""


def unreadable_attachments(files) -> list:
    """[(StagedFile, what it is, what to ask for)] for attachments nothing here can open."""
    out = []
    for f in files or []:
        hit = UNREADABLE_FORMATS.get(_ext(f.filename))
        if hit:
            out.append((f, hit[0], hit[1]))
    return out


def capability_note(files) -> str:
    """A note for the model's context when an attachment is in a format the sandbox cannot
    read — so it says so in its first sentence instead of finding out over five failed calls.
    Returns '' when every attachment is fine (the common case)."""
    bad = unreadable_attachments(files)
    if not bad:
        return ""
    lines = []
    for f, what, ask in bad:
        lines.append(f"- `{f.handle}` ({f.filename}) is {what}. No library in the sandbox can "
                     f"open it, and there is no network to install one. Ask them to {ask}.")
    return ("Capability note — an attachment is in a format you CANNOT read:\n"
            + "\n".join(lines)
            + "\nDo not try anyway: there is no import, engine, or workaround that will open it, "
              "and attempting one just spends the turn. If the task needs what is inside this "
              "file, use `ask_user` NOW — in your first move, before any other tool call — to "
              "tell them plainly that this format can't be read and what to send instead. If "
              "other attachments cover the request on their own, carry on with those.")


def clip_output(text: str | None, limit: int = MAX_OUTPUT_CHARS) -> str | None:
    """Trim printed output for the model, keeping BOTH ends.

    The old error path kept only the tail, which is right for a traceback but wrong for an
    inspection dump: the header row and first data rows — the part the model actually needs to
    work out a sheet's layout — live at the top."""
    if not text:
        return None
    text = text.strip()
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    return f"{text[:head]}\n\n…[{len(text) - limit} characters trimmed from the middle]…\n\n{text[-tail:]}"


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
            "`pdf2image`, `pandas`, `numpy`, `PIL` (Pillow), `openpyxl`/`xlsxwriter` "
            "(.xlsx/.xlsm) and `xlrd` (legacy .xls — pandas.read_excel picks it up "
            "automatically, so read a .xls exactly as you would a .xlsx), "
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
            "- env var `OUTPUT_DIR`: write every file you want returned to the USER into this "
            "directory. Anything written there is uploaded to Slack automatically, so it is for "
            "finished deliverables ONLY — never write scratch or debug files there.\n"
            "- No network access. One shot per call (no state persists between calls).\n"
            "**Anything your script prints to stdout is returned to you in the `stdout` field of "
            "the result.** That is how you inspect a file: `print()` the header row, the column "
            "names, the row count, a sample of the data — whatever you need to see — and read it "
            "back in the tool result. Do NOT write debug files to OUTPUT_DIR to inspect them; you "
            "cannot read files back, only stdout. Keep prints purposeful (output is trimmed at "
            f"~{MAX_OUTPUT_CHARS} characters).\n"
            "Because you can inspect and act in the same script, prefer ONE call that inspects, "
            "decides, writes the output file, and prints what it did — rather than a separate "
            "look-first call. When a file's layout is genuinely unknown, one inspection call "
            "followed by one call that does the work is the right shape.\n"
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
                "user_confirmed": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Set true ONLY after the user has explicitly approved this specific "
                        "action in the conversation. Required for code that deletes or "
                        "overwrites the attached file or shells out; if you haven't asked yet, "
                        "use the `ask_user` tool first and leave this false."
                    ),
                },
            },
            "required": ["code"],
            "additionalProperties": False,
        },
    }


# Local venv directory per environment; the neural venv is a superset of the default one.
_VENV_BY_ENV = {"default": ".venv", "neural_ocr": ".venv-ocr"}


# --- "should I ask first?" ---------------------------------------------------------------
# The sandbox is already ephemeral and network-less, so almost nothing model-written code does
# outlives the run: files land in OUTPUT_DIR and go to Slack, everything else is thrown away.
# That makes this list deliberately SHORT — only operations that reach outside the sandbox or
# destroy the user's own attachment. A gate that cried wolf on ordinary file writes would just
# train everyone to wave it through.
#
# This reads the parse tree rather than the source text, because model-written Python is
# formatted however the model felt: a line-based scan missed `Path(INPUT_FILE).unlink()`, any
# call split across lines, and every case where the path was in a variable.

# Things that reach outside the sandbox no matter what they're pointed at.
_ESCAPES_SANDBOX = {
    "shutil.rmtree": "delete a whole directory tree",
    "os.removedirs": "delete directories",
    "os.system": "run a shell command",
    "os.popen": "run a shell command",
}
_ESCAPE_PREFIXES = (("subprocess.", "run another program"), ("os.exec", "run another program"),
                    ("os.spawn", "run another program"))

# Things that are fine on scratch files but not on the file the user attached. Value is
# (which argument must be the attached file, what it would do to it).
_DESTROYS_INPUT = {
    "os.remove": (0, "delete the file you attached"),
    "os.unlink": (0, "delete the file you attached"),
    "os.rename": (0, "move or rename the file you attached"),
    "os.replace": (0, "move or rename the file you attached"),
    "os.truncate": (0, "truncate the file you attached"),
    "shutil.move": (0, "move the file you attached"),
    "shutil.copy": (1, "overwrite the file you attached"),
    "shutil.copy2": (1, "overwrite the file you attached"),
    "shutil.copyfile": (1, "overwrite the file you attached"),
}
# pathlib equivalents, as methods called ON the attached file's path.
_DESTRUCTIVE_METHODS = {
    "unlink": "delete the file you attached",
    "rmdir": "delete the file you attached",
    "write_text": "overwrite the file you attached",
    "write_bytes": "overwrite the file you attached",
    "rename": "move or rename the file you attached",
    "replace": "move or rename the file you attached",
}
_INPUT_KEYS = ("INPUT_FILE", "input_file")
# Wrappers that keep a value a *path*. Deliberately excludes .read()/.read_text() and friends:
# the CONTENTS of the attached file are not the attached file, and treating them as tainted
# made ordinary `text.replace(...)` look like a rename.
_PATH_WRAPPERS = {"Path", "pathlib.Path", "str", "os.fspath",
                  "os.path.abspath", "os.path.realpath", "os.path.normpath"}


def _dotted(node) -> str:
    """'shutil.rmtree' for an Attribute/Name chain; '' for anything else."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _is_input_key(node) -> bool:
    return isinstance(node, ast.Constant) and node.value in _INPUT_KEYS


def _is_input_path(node, tainted: set) -> bool:
    """Is this expression the path of the file the user attached?"""
    if isinstance(node, ast.Name):
        return node.id in tainted or node.id in _INPUT_KEYS
    if isinstance(node, ast.Subscript):                     # os.environ["INPUT_FILE"]
        return _is_input_key(node.slice)
    if isinstance(node, ast.Call):
        name = _dotted(node.func)
        if name in ("os.environ.get", "os.getenv"):
            return bool(node.args) and _is_input_key(node.args[0])
        if name in _PATH_WRAPPERS:
            return any(_is_input_path(a, tainted) for a in node.args)
    return False


def _writes(call: ast.Call) -> bool:
    """Is this open() call opening for write/append?"""
    mode = None
    if len(call.args) > 1:
        mode = call.args[1]
    for kw in call.keywords:
        if kw.arg == "mode":
            mode = kw.value
    if mode is None:
        return False        # default "r"
    return isinstance(mode, ast.Constant) and any(c in str(mode.value) for c in "wax+")


def risky_operations(code: str) -> list[str]:
    """Plain-English list of things this script would do that the user should approve first.

    Static analysis, not a security control — the sandbox's isolation is that. This exists so
    the bot stops and asks before the handful of actions a teammate would be annoyed to find
    out about afterwards."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []       # it can't run anyway; let the sandbox report the real error

    risks: set[str] = set()
    tainted: set[str] = set()

    # Two passes: bind path variables first, so order of definition doesn't matter.
    for _ in range(2):
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                value = node.value
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if value is not None and _is_input_path(value, tainted):
                    for t in targets:
                        for n in ast.walk(t):
                            if isinstance(n, ast.Name):
                                tainted.add(n.id)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _dotted(node.func)
        if name in _ESCAPES_SANDBOX:
            risks.add(_ESCAPES_SANDBOX[name])
            continue
        for prefix, what in _ESCAPE_PREFIXES:
            if name.startswith(prefix) or name == "Popen":
                risks.add(what)
        if name in _DESTROYS_INPUT:
            idx, what = _DESTROYS_INPUT[name]
            if len(node.args) > idx and _is_input_path(node.args[idx], tainted):
                risks.add(what)
        elif name in ("open", "io.open") and node.args and _writes(node):
            if _is_input_path(node.args[0], tainted):
                risks.add("overwrite the file you attached")
        elif isinstance(node.func, ast.Attribute) and node.func.attr in _DESTRUCTIVE_METHODS:
            if _is_input_path(node.func.value, tainted):
                risks.add(_DESTRUCTIVE_METHODS[node.func.attr])

    return sorted(risks)


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
            # Whatever printed before the crash is often the whole diagnosis.
            stdout=clip_output(stdout),
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
    return ToolInvocationResult(status="ok", summary=summary, artifacts=artifacts,
                                stdout=clip_output(stdout), work_dir=run_dir)


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
    # `stdout` is absent on a handler older than 2026-09-08; clip_output(None) -> None, so the
    # model simply gets no stdout key rather than an error.
    return ToolInvocationResult(status=raw.get("status", "error"), summary=raw.get("summary", ""),
                                artifacts=raw.get("artifacts") or [], error=raw.get("error"),
                                stdout=clip_output(raw.get("stdout")), work_dir=out_prefix)


# --- the attached file must keep its extension ------------------------------------------
# The deployed Lambda handler stages every input at /tmp/input_file — no extension — and the
# libraries the model reaches for first refuse that: openpyxl checks the suffix before it will
# open a workbook, pandas.read_excel can't pick an engine, and so on. In production this made
# every spreadsheet request fail six times in a row and then give up; locally it never showed,
# because the local path keeps the filename. handler.py is fixed too, but that needs a Lambda
# redeploy — this prelude makes the bot correct against the handler that's live right now, and
# is a no-op once INPUT_FILE already carries the right suffix.
_EXT_RE = re.compile(r"^\.[a-z0-9]{1,8}$")


def _input_extension_prelude(filename: str) -> str:
    """One line of Python (so tracebacks shift by exactly one) that, if INPUT_FILE lacks the
    attachment's extension, copies it to a sibling path that has one and re-points the env var.
    Runs inside exec() with its own namespace, so nothing leaks into the model's code."""
    ext = os.path.splitext(filename or "")[1].lower()
    if not _EXT_RE.match(ext):
        return ""
    body = (
        "import os, shutil\n"
        "p = os.environ.get('INPUT_FILE')\n"
        f"if p and not p.lower().endswith({ext!r}):\n"
        "    d = os.path.join(os.path.dirname(p) or '.', 'input_named')\n"
        "    shutil.rmtree(d, ignore_errors=True); os.makedirs(d, exist_ok=True)\n"
        f"    n = os.path.join(d, 'input{ext}'); shutil.copyfile(p, n); os.environ['INPUT_FILE'] = n\n"
    )
    return f"exec({body!r}, {{}})  # bot prelude: keep the attachment's extension\n"


def _unshift_traceback_lines(text: str | None, lines: int) -> str | None:
    """Tracebacks from the sandbox point at snippet.py line numbers; take the prelude back out so
    the model can find the line it actually wrote."""
    if not text or not lines:
        return text
    return re.sub(r'(snippet\.py", line )(\d+)',
                  lambda m: f"{m.group(1)}{max(1, int(m.group(2)) - lines)}", text)


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

    # Stop and make the bot ask before the few things a teammate wouldn't want done on a guess.
    risks = risky_operations(code)
    if risks and not tool_input.get("user_confirmed"):
        logger.info("run_code: refusing unconfirmed risky code %s", risks)
        return ToolInvocationResult.err(
            "This code would " + ", and ".join(risks) + ". Don't run it yet: use the `ask_user` "
            "tool to describe exactly what you're about to do and ask the user to confirm. If "
            "they say yes, call run_code again with user_confirmed=true. If they'd rather you "
            "didn't, find another way — writing new files into OUTPUT_DIR never needs approval."
        )

    input_path = None
    prelude = ""
    handle = tool_input.get("input_file")
    if handle:
        staged = staging.by_handle().get(handle)
        if staged is None:
            return ToolInvocationResult.err(
                f"No attached file with handle {handle!r}. Available: {sorted(staging.by_handle()) or 'none'}."
            )
        # Backstop for the capability note above: if the model reaches for a format nothing here
        # can open, say so on the FIRST call with the answer already in hand, rather than letting
        # it probe for a missing engine one import at a time. Costs one step instead of a turn.
        unreadable = UNREADABLE_FORMATS.get(_ext(staged.filename))
        if unreadable:
            what, ask = unreadable
            logger.info("run_code: refusing unreadable format %s (%s)", _ext(staged.filename), handle)
            return ToolInvocationResult.err(
                f"{staged.filename!r} is {what}, and NOTHING in this sandbox can read it — not "
                f"openpyxl, not pandas, not any engine you might import. There is no network, so "
                f"nothing can be installed either. Do not try another library or another approach: "
                f"the answer will not change. Use the `ask_user` tool now to tell the teammate this "
                f"format can't be read and to ask them to {ask}."
            )
        input_path = staged.ref
        prelude = _input_extension_prelude(staged.filename)

    logger.info("run_code: backend=%s environment=%s input=%s code_chars=%d prelude=%s",
                SANDBOX_BACKEND, environment, bool(input_path), len(code), bool(prelude))
    try:
        if SANDBOX_BACKEND == "lambda":
            res = _run_lambda(prelude + code, input_path, staging, logger, environment, notify)
        else:
            res = _run_local(prelude + code, input_path, staging, logger, environment)
    except Exception as err:
        logger.exception("Sandbox crashed")
        return ToolInvocationResult.err(f"{type(err).__name__}: {err}")
    if prelude:
        res.error = _unshift_traceback_lines(res.error, prelude.count("\n"))
    return res

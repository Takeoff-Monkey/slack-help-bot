"""The agentic tool-use loop — the bot's "do things" path.

When a teammate asks the bot to *act* (usually with a file attached), this runs the standard
Anthropic tool-use loop: the model emits tool_use blocks, the bot executes them (a registered
tool via tool_runner, or the run_code sandbox), feeds the results back as tool_result blocks,
and repeats until the model returns a final text answer. Specialized tools are preferred;
run_code is the catch-all for anything no tool covers.

The pure-Q&A retrieval path in app2.py is untouched — this loop is only entered for actions.
Tool_use/tool_result blocks live only within a single turn; they are never written back into
Slack thread history (which stays plain text), so we never have to reconstruct partial tool
blocks from text — the #1 cause of malformed-history 400s.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
from dataclasses import dataclass, field

import sandbox
import slack_files
import tool_registry
import tool_runner

ACTION_MODEL = os.environ.get("ACTION_MODEL", "claude-sonnet-5")
MAX_TOOL_ITERATIONS = int(os.environ.get("MAX_TOOL_ITERATIONS", "6"))
MAX_TOKENS = 2048

# Tools run in a worker thread so the loop can keep polling the cancel signal and abandon a
# long-running tool the moment the user says "stop" (the Lambda finishes in the background;
# we just stop waiting on it and discard its result).
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="tool")


class _NeverCancel:
    """Default no-op cancel signal for callers that don't pass one."""
    @staticmethod
    def is_set() -> bool:
        return False

# The Slack-mrkdwn rules are copied from app2.ANSWER_SYSTEM so action replies render the
# same way as Q&A replies.
ACTION_SYSTEM = """You are an AI assistant for Takeoff Monkey that can *perform operations* for teammates, not just answer questions. You have specialized tools plus a `run_code` sandbox.

Tell the user what you're doing:
- Before you call ANY tool, first write ONE short, friendly sentence saying what you're about to do and on which file — e.g. "On it — running the Schedule Extractor on that PDF now." Then make the tool call in the SAME turn. This lets the user see what's happening and stop you if it's not what they wanted.

Core rule — do exactly what was asked, then stop:
- Most requests are satisfied by ONE tool call. Pick the single registered tool that matches the request, call it once, and when it returns status "ok" you are DONE.
- Do NOT call more tools to double-check, re-run, reformat, validate, or "improve" a result that already succeeded. A successful tool result IS the finished work. (The one exception is escalating poor-quality OCR — see below.)
- Only take an additional step if the user EXPLICITLY asked for a separate operation that the tool did not perform (e.g. "extract the schedules AND highlight every 'landscape'"). If they didn't ask for it, don't do it.
- Use `run_code` ONLY when no registered tool covers what the user explicitly asked for. Never use it to post-process a tool's output unless the user requested that post-processing.

OCR & scanned images:
- If a user attaches an image (PNG/JPG) or a scanned / text-less PDF and wants its text or tables, a registered tool may not fit — use `run_code`. Start in the "default" environment: preprocess with `cv2` (grayscale, upscale, threshold) and read with `pytesseract` (Tesseract).
- Judge the result. If the extracted text comes back garbled, mostly empty, or low-confidence (e.g. low mean word confidence from `pytesseract.image_to_data`), you MAY escalate: tell the user in one short sentence that the quick OCR looked rough and you're trying a more powerful engine, then call `run_code` again with `environment` set to "neural_ocr" (RapidOCR). This is an explicitly allowed second step — the one exception to "don't re-run a succeeded result." Escalate at most once; do not loop.
- If neural OCR still looks poor, stop and say so plainly — and suggest the original higher-quality source (e.g. the vector PDF instead of a photo) — rather than retrying further.

Files & output:
- Attached files are listed with handles like `file_1`. Pass those handles to a tool's `input_file` field. In `run_code`, the file you name is at env `INPUT_FILE`, and anything you write to env `OUTPUT_DIR` is uploaded to the thread automatically.
- Every file a tool or `run_code` produces is uploaded to the Slack thread for the user automatically. Never re-create, re-deliver, or tell the user where to find a file.
- When finished, reply with one or two plain sentences summarizing what you did. Do NOT paste raw tool JSON.

Errors:
- If a tool returns an error, you may retry ONCE with corrected inputs, or explain the problem plainly. Do not keep retrying or switch to `run_code` to brute-force around a failure.
- Startup delays are NOT errors to retry around. The sandbox and the tools run on infrastructure that can take up to a minute to boot when it has gone cold, and the bot already waits for it patiently before every call. If a result says something is still booting or starting up, do NOT immediately call it again — that only makes it slower. Tell the user it needs another moment and stop.

Formatting — your output is rendered in Slack mrkdwn (NOT standard Markdown). Use only:
- Inline code/identifiers: backticks, like `file_1`
- Bold: single asterisks, like *important* (NOT **double asterisks**)
- Italics: single underscores, like _example_
- Bullets: start each line with "- "
- Links: plain URLs (https://…); do not use angle brackets or [text](url).
Do not use double-asterisk bold or # / ## headings — Slack shows them literally."""


@dataclass
class AgentResult:
    text: str
    artifacts: list = field(default_factory=list)
    work_dirs: list = field(default_factory=list)  # for cleanup (paths/prefixes), never shown
    trace: list = field(default_factory=list)       # human-readable step-by-step log
    cancelled: bool = False                          # user stopped it mid-task


def _run_interruptible(fn, cancel_event):
    """Run fn() in a worker thread while polling cancel_event. Returns (result, cancelled).
    On cancel the worker is abandoned (it finishes in the background; we ignore its result)."""
    future = _EXECUTOR.submit(fn)
    while True:
        if cancel_event.is_set():
            return None, True
        try:
            return future.result(timeout=0.4), False
        except concurrent.futures.TimeoutError:
            continue


def _normalize(messages: list[dict]) -> list[dict]:
    """Drop leading assistant turns and merge consecutive same-role *string* turns. Only
    used on the plain-text history + initial user turn; tool blocks are added afterward."""
    while messages and messages[0]["role"] != "user":
        messages = messages[1:]
    merged: list[dict] = []
    for m in messages:
        if merged and merged[-1]["role"] == m["role"] and isinstance(merged[-1]["content"], str) and isinstance(m["content"], str):
            merged[-1]["content"] += "\n\n" + m["content"]
        else:
            merged.append({"role": m["role"], "content": m["content"]})
    return merged


def _text_of(message) -> str:
    return "".join(b.text for b in message.content if getattr(b, "type", None) == "text").strip()


def _short(s, n: int = 300) -> str:
    s = str(s).replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


def run_agent(client, question, history, staging, tool_specs, progress, on_artifacts, cancel_event, logger) -> AgentResult:
    """Drive the tool-use loop.

    - progress(msg): updates the Slack 'Thinking…' placeholder with a status line.
    - on_artifacts(list): uploads files the moment a tool produces them (output survives a
      later failure or the step cap).
    - cancel_event: a threading.Event-like; if it trips, the loop stops ASAP — including
      abandoning a tool mid-run — and returns a cancelled AgentResult.

    Every step is logged at INFO and appended to a human-readable `trace` (returned on the
    AgentResult) so the run isn't a black box.
    """
    cancel_event = cancel_event or _NeverCancel()
    tool_defs = tool_registry.anthropic_tool_defs(tool_specs) + [sandbox.run_code_tool_def()]

    # Start booting the sandbox now (no-op unless it's the Lambda backend and it's gone cold).
    # It takes tens of seconds to come up, and the model spends at least one turn writing code
    # before it calls run_code — so overlap the two instead of making the user wait for both.
    sandbox.prewarm(logger)

    attach_note = slack_files.attachments_for_prompt(staging)
    user_turn = f"{question}\n\n{attach_note}" if attach_note else question
    messages = _normalize(list(history) + [{"role": "user", "content": user_turn}])

    artifacts: list = []
    work_dirs: list = []
    trace: list = [f"USER: {question!r} | files={[f.handle for f in staging.files]}"]

    def emit(arts):
        """Accumulate + immediately upload artifacts a tool just produced."""
        if not arts:
            return
        artifacts.extend(arts)
        if on_artifacts:
            try:
                on_artifacts(arts)
            except Exception:
                logger.exception("on_artifacts callback failed (continuing)")

    def cancelled_result():
        logger.info("agent: cancelled by user")
        trace.append("CANCELLED by user")
        return AgentResult(
            text=":octagonal_sign: _Cancelled._",
            artifacts=artifacts, work_dirs=work_dirs, trace=trace, cancelled=True,
        )

    logger.info("agent: start | %d tool(s) | %d file(s) | q=%r",
                len(tool_defs), len(staging.files), _short(question, 120))

    for step in range(MAX_TOOL_ITERATIONS):
        if cancel_event.is_set():
            return cancelled_result()

        msg = client.messages.create(
            model=ACTION_MODEL,
            max_tokens=MAX_TOKENS,
            thinking={"type": "disabled"},
            system=ACTION_SYSTEM,
            tools=tool_defs,
            messages=messages,
        )
        if cancel_event.is_set():
            return cancelled_result()

        # The assistant turn (text + tool_use blocks) must be appended verbatim before the
        # matching tool_result turn.
        messages.append({"role": "assistant", "content": msg.content})

        say = _text_of(msg)
        tool_calls = [b for b in msg.content if getattr(b, "type", None) == "tool_use"]
        logger.info("agent: step %d/%d | stop=%s | says=%r | calls=%s",
                    step + 1, MAX_TOOL_ITERATIONS, msg.stop_reason, _short(say, 160),
                    [b.name for b in tool_calls])
        if say:
            trace.append(f"ASSISTANT[{step + 1}]: {say}")

        if msg.stop_reason != "tool_use":
            trace.append(f"DONE: {say}")
            logger.info("agent: done after %d step(s)", step + 1)
            return AgentResult(text=say or "Done.", artifacts=artifacts, work_dirs=work_dirs, trace=trace)

        # Surface the model's friendly preamble ("running the Schedule Extractor now…") so the
        # user can see what's happening and stop it if it's wrong.
        if say:
            progress(say)

        tool_results = []
        for block in tool_calls:
            if cancel_event.is_set():
                return cancelled_result()
            name = block.name
            if not say:
                progress(f"Running {name}…")
            trace.append(f"CALL[{step + 1}] {name}({_short(json.dumps(block.input), 300)})")

            def _do(block=block, name=name):
                if name == "run_code":
                    return sandbox.run_code(block.input, staging, logger, notify=progress)
                if name in tool_specs:
                    return tool_runner.run_tool(tool_specs[name], block.input, staging, logger,
                                                notify=progress)
                return tool_runner.ToolInvocationResult.err(f"Unknown tool {name!r}.")

            # Run in a worker thread so a cancel mid-tool abandons it instead of blocking.
            res, was_cancelled = _run_interruptible(_do, cancel_event)
            if was_cancelled:
                return cancelled_result()

            logger.info("agent: tool %s -> %s | %s | artifacts=%s",
                        name, res.status, _short(res.error or res.summary, 160),
                        [a.get("filename") for a in res.artifacts])
            trace.append(f"RESULT {name}: status={res.status} | {res.summary or res.error} "
                         f"| artifacts={[a.get('filename') for a in res.artifacts]}")
            emit(res.artifacts)   # upload now — survives any later failure/timeout
            if res.work_dir:
                work_dirs.append(res.work_dir)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(res.model_view()),
                "is_error": res.status == "error",
            })
        messages.append({"role": "user", "content": tool_results})

    # Hit the iteration cap — one no-tools wrap-up turn for a coherent reply.
    if cancel_event.is_set():
        return cancelled_result()
    progress("Wrapping up…")
    logger.info("agent: hit step cap (%d); forcing finalize", MAX_TOOL_ITERATIONS)
    delivered = " The files I completed are already attached." if artifacts else ""
    try:
        final = client.messages.create(
            model=ACTION_MODEL,
            max_tokens=MAX_TOKENS,
            thinking={"type": "disabled"},
            system=ACTION_SYSTEM + "\n\nYou have reached the step limit. Briefly summarize what you completed and stop calling tools.",
            messages=messages,
        )
        text = _text_of(final) or f"I hit my step limit before fully finishing.{delivered}"
    except Exception:
        logger.exception("force-finalize call failed")
        text = f"I hit my step limit before fully finishing.{delivered}"
    trace.append(f"FINALIZE: {text}")
    return AgentResult(text=text, artifacts=artifacts, work_dirs=work_dirs, trace=trace)

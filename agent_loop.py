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

import json
import os
from dataclasses import dataclass, field

import sandbox
import slack_files
import tool_registry
import tool_runner

ACTION_MODEL = os.environ.get("ACTION_MODEL", "claude-sonnet-4-6")
MAX_TOOL_ITERATIONS = int(os.environ.get("MAX_TOOL_ITERATIONS", "6"))
MAX_TOKENS = 2048

# The Slack-mrkdwn rules are copied from app2.ANSWER_SYSTEM so action replies render the
# same way as Q&A replies.
ACTION_SYSTEM = """You are an AI assistant for Takeoff Monkey that can *perform operations* for teammates, not just answer questions. You have specialized tools plus a `run_code` sandbox.

Core rule — do exactly what was asked, then stop:
- Most requests are satisfied by ONE tool call. Pick the single registered tool that matches the request, call it once, and when it returns status "ok" you are DONE.
- Do NOT call more tools to double-check, re-run, reformat, validate, or "improve" a result that already succeeded. A successful tool result IS the finished work.
- Only take an additional step if the user EXPLICITLY asked for a separate operation that the tool did not perform (e.g. "extract the schedules AND highlight every 'landscape'"). If they didn't ask for it, don't do it.
- Use `run_code` ONLY when no registered tool covers what the user explicitly asked for. Never use it to post-process a tool's output unless the user requested that post-processing.

Files & output:
- Attached files are listed with handles like `file_1`. Pass those handles to a tool's `input_file` field. In `run_code`, the file you name is at env `INPUT_FILE`, and anything you write to env `OUTPUT_DIR` is uploaded to the thread automatically.
- Every file a tool or `run_code` produces is uploaded to the Slack thread for the user automatically. Never re-create, re-deliver, or tell the user where to find a file.
- When finished, reply with one or two plain sentences summarizing what you did. Do NOT paste raw tool JSON.

Errors:
- If a tool returns an error, you may retry ONCE with corrected inputs, or explain the problem plainly. Do not keep retrying or switch to `run_code` to brute-force around a failure.

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


def run_agent(client, question, history, staging, tool_specs, progress, on_artifacts, logger) -> AgentResult:
    """Drive the tool-use loop.

    - progress(msg): updates the Slack 'Thinking…' placeholder with a status line.
    - on_artifacts(list): called the MOMENT a tool produces files, so they're uploaded to the
      thread immediately. This guarantees the user gets output even if a later step errors or
      the loop hits its cap — the file is already delivered.

    Every step is logged at INFO and appended to a human-readable `trace` (returned on the
    AgentResult) so the run isn't a black box.
    """
    tool_defs = tool_registry.anthropic_tool_defs(tool_specs) + [sandbox.run_code_tool_def()]

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

    logger.info("agent: start | %d tool(s) | %d file(s) | q=%r",
                len(tool_defs), len(staging.files), _short(question, 120))

    for step in range(MAX_TOOL_ITERATIONS):
        msg = client.messages.create(
            model=ACTION_MODEL,
            max_tokens=MAX_TOKENS,
            thinking={"type": "disabled"},
            system=ACTION_SYSTEM,
            tools=tool_defs,
            messages=messages,
        )
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

        tool_results = []
        for block in tool_calls:
            name = block.name
            trace.append(f"CALL[{step + 1}] {name}({_short(json.dumps(block.input), 300)})")
            if name == "run_code":
                progress("Running a custom step…")
                res = sandbox.run_code(block.input, staging, logger)
            elif name in tool_specs:
                progress(f"Running {name}…")
                res = tool_runner.run_tool(tool_specs[name], block.input, staging, logger)
            else:
                res = tool_runner.ToolInvocationResult.err(f"Unknown tool {name!r}.")
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

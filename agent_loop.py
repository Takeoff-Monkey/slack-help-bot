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

How to work:
- Prefer a registered tool whenever one matches the request or is named explicitly — they are specialized and reliable. Use `run_code` only for steps no registered tool covers.
- A request can need several steps (e.g. run a tool on a file, then do something extra with `run_code`). Do the tool steps first, then the leftover work.
- Files the user attached are listed with handles like `file_1`. Pass those handles to tools' `input_file` fields. In `run_code`, the attached file you name is available at the path in env `INPUT_FILE`, and anything you write to env `OUTPUT_DIR` is uploaded back to the user automatically.
- When you're done, write a short summary of what you did. Do NOT paste raw tool JSON, and do NOT tell the user to look for files on disk — produced files are uploaded to the thread for them.
- If a tool returns an error, read it, and either fix the inputs and retry, try `run_code`, or explain plainly what went wrong.

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


def run_agent(client, question, history, staging, tool_specs, progress, logger) -> AgentResult:
    """Drive the tool-use loop. `progress(msg)` updates the Slack placeholder; `client` is
    the shared anthropic client; `staging` carries the attached files."""
    tool_defs = tool_registry.anthropic_tool_defs(tool_specs) + [sandbox.run_code_tool_def()]

    attach_note = slack_files.attachments_for_prompt(staging)
    user_turn = f"{question}\n\n{attach_note}" if attach_note else question
    messages = _normalize(list(history) + [{"role": "user", "content": user_turn}])

    artifacts: list = []
    work_dirs: list = []

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

        if msg.stop_reason != "tool_use":
            return AgentResult(text=_text_of(msg) or "Done.", artifacts=artifacts, work_dirs=work_dirs)

        tool_results = []
        for block in msg.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            name = block.name
            if name == "run_code":
                progress("Running a custom step…")
                res = sandbox.run_code(block.input, staging, logger)
            elif name in tool_specs:
                progress(f"Running {name}…")
                res = tool_runner.run_tool(tool_specs[name], block.input, staging, logger)
            else:
                res = tool_runner.ToolInvocationResult.err(f"Unknown tool {name!r}.")
            artifacts.extend(res.artifacts)
            if res.work_dir:
                work_dirs.append(res.work_dir)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(res.model_view()),
                "is_error": res.status == "error",
            })
        messages.append({"role": "user", "content": tool_results})

    # Hit the iteration cap — one no-tools wrap-up turn so the user still gets a coherent reply.
    progress("Wrapping up…")
    try:
        final = client.messages.create(
            model=ACTION_MODEL,
            max_tokens=MAX_TOKENS,
            thinking={"type": "disabled"},
            system=ACTION_SYSTEM + "\n\nYou have reached the step limit. Summarize what you accomplished and stop calling tools.",
            messages=messages,
        )
        text = _text_of(final) or "I ran out of steps before fully finishing, but I've done what I could."
    except Exception:
        logger.exception("force-finalize call failed")
        text = "I ran several steps but hit my limit before finishing. The files I produced are attached."
    return AgentResult(text=text, artifacts=artifacts, work_dirs=work_dirs)

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

import anthropic

import sandbox
import slack_files
import tool_registry
import tool_runner

ACTION_MODEL = os.environ.get("ACTION_MODEL", "claude-sonnet-5")
MAX_TOOL_ITERATIONS = int(os.environ.get("MAX_TOOL_ITERATIONS", "6"))
MAX_TOKENS = 2048
# Per-request cap on a model call. The SDK's default read timeout is 600s, so a wedged
# request used to mean ten minutes of an unchanging "Thinking…" placeholder (times the
# SDK's own retries). Fail fast enough that we can tell the user something instead.
MODEL_TIMEOUT = float(os.environ.get("ACTION_MODEL_TIMEOUT_SECONDS", "120"))

# Tools run in a worker thread so the loop can keep polling the cancel signal and abandon a
# long-running tool the moment the user says "stop" (the Lambda finishes in the background;
# we just stop waiting on it and discard its result).
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="tool")


class _NeverCancel:
    """Default no-op cancel signal for callers that don't pass one."""
    @staticmethod
    def is_set() -> bool:
        return False


# The bot's way of stopping to ask instead of guessing. Calling it ends the turn: the question
# becomes the reply, and the teammate's answer arrives as the next message in the thread (which
# is already how history works, so no extra state to keep).
ASK_USER_TOOL = {
    "name": "ask_user",
    "description": (
        "Ask the teammate a question and STOP, handing the turn back to them. Use this when a "
        "decision is genuinely theirs to make:\n"
        "- before anything irreversible or destructive: deleting or overwriting a file, "
        "replacing their original, running shell commands, or any change that can't be undone\n"
        "- when the request is ambiguous and guessing wrong would waste their time — which "
        "file, which sheet, which pages, what output format\n"
        "- when the work would go well beyond what they actually asked for\n"
        "- when you've hit a wall and only they can unblock it\n"
        "Ask ONE clear question and stop. Do NOT use this to narrate progress, and do not ask "
        "permission for the ordinary work they already asked you to do — just do that."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question, in one or two plain sentences.",
            },
            "why": {
                "type": "string",
                "description": "Optional: one short sentence on why you stopped to ask.",
            },
        },
        "required": ["question"],
        "additionalProperties": False,
    },
}

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

Never go quiet — this matters as much as getting the work right:
- ALWAYS end your turn with words. Never stop after a tool result without writing a reply; an empty answer leaves the user staring at a spinner with no idea whether you're still working.
- The moment something goes wrong, say so in that same turn: one short sentence naming what failed and what you're doing about it. Don't discover an obstacle and quietly move on.
- If you genuinely cannot do what was asked, say plainly what stopped you and what would unblock it (a different file, a tool that doesn't exist, information only they have). "I can't do this because X" is a good answer. Silence is not.
- Never say something is done when it isn't, and never describe a file as delivered unless a tool actually produced it.

Ask before you act — use the `ask_user` tool:
- STOP and ask before anything irreversible or destructive: deleting or overwriting a file, replacing the user's original, running shell commands, or any change that can't be taken back.
- STOP and ask when the request is genuinely ambiguous and guessing wrong would waste their time — which of several attached files, which sheet, what output format.
- STOP and ask before doing substantially more than they asked for.
- Do NOT ask permission for the ordinary work they already requested — that's just delay. Ask when the answer is theirs to give, not when you're merely unsure of yourself.

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
    failed: bool = False                             # ended on an error we had to surface
    awaiting_user: bool = False                      # stopped to ask a question (ask_user)


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


def _append_user_text(messages: list, text: str) -> None:
    """Put an instruction into the conversation as the final user content. Folds into the last
    user turn when there is one (tool_results are a list of blocks; a nudge is a string), so we
    never produce two consecutive user messages or orphan a tool_use block."""
    if messages and messages[-1]["role"] == "user":
        content = messages[-1]["content"]
        if isinstance(content, list):
            content.append({"type": "text", "text": text})
        else:
            messages[-1]["content"] = f"{content}\n\n{text}"
    else:
        messages.append({"role": "user", "content": text})


def _model_failure_text(err: Exception, artifacts: list) -> str:
    """An honest, plain reply when the model call itself falls over. Previously this bubbled
    out of the loop and lost the trace and any files already produced."""
    if isinstance(err, anthropic.RateLimitError):
        what = "I'm being rate-limited by the model API"
    elif isinstance(err, anthropic.APITimeoutError):
        what = "the model API stopped responding"
    elif isinstance(err, anthropic.APIError):
        what = f"the model API errored ({getattr(err, 'message', None) or err})"
    else:
        what = f"something broke while I was working ({type(err).__name__}: {err})"
    done = " The file(s) I'd already finished are attached above." if artifacts else ""
    return (f":x: I've stopped — {what}. Nothing is still running in the background."
            f"{done} Ask me again and I'll pick it back up.")


def _wrap_up_text(say: str, stop_reason, artifacts: list, last_error: str | None) -> str:
    """What to tell the user when the model stops calling tools.

    This used to be `say or "Done."` — so a run that ended right after a failed tool, with the
    model saying nothing, reported *success*. Never claim an outcome we can't back up."""
    if say:
        if stop_reason == "max_tokens":
            say += "\n\n_(I ran out of room mid-answer — say 'continue' if that got cut off.)_"
        if last_error:
            # The model may well have written "let me try X" — a header makes the state
            # unmistakable whatever it wrote: the last thing that happened was a failure and
            # nothing more is running.
            return f":warning: I hit a wall on this one and have stopped.\n\n{say}"
        return say
    if last_error:
        done = " The file(s) I did finish are attached above." if artifacts else ""
        return (f":warning: I couldn't finish this one. The last thing I tried failed: "
                f"{_short(last_error, 400)}{done}\n\nTell me how you'd like me to proceed "
                f"and I'll take another run at it.")
    if artifacts:
        return "Done — the file(s) I produced are attached above."
    return (":warning: I stopped without producing anything, and without an error to explain "
            "why — that's a fault on my side, not something you did. Ask me again, or rephrase "
            "what you need, and I'll take another run at it.")


def run_agent(client, question, history, staging, tool_specs, reporter, on_artifacts, cancel_event, logger) -> AgentResult:
    """Drive the tool-use loop.

    - reporter: the turn's voice (status.Reporter). `.say()` posts a new thought the user
      should see now, `.waiting()` restates the one we're already on (it replaces rather than
      stacks), `.doing()` sets what the 30-second watchdog narrates when the bot goes quiet,
      `.snag()` the moment something goes wrong.
    - on_artifacts(list): uploads files the moment a tool produces them (output survives a
      later failure or the step cap).
    - cancel_event: a threading.Event-like; if it trips, the loop stops ASAP — including
      abandoning a tool mid-run — and returns a cancelled AgentResult.

    The loop's contract with the user: it ALWAYS returns an AgentResult carrying text worth
    reading. Every exit — success, model error, tool wall, cancel, step cap, a question back
    to the user — comes out of here as words, never as an exception the caller has to guess at.

    Every step is logged at INFO and appended to a human-readable `trace` (returned on the
    AgentResult) so the run isn't a black box.
    """
    cancel_event = cancel_event or _NeverCancel()
    tool_defs = (tool_registry.anthropic_tool_defs(tool_specs)
                 + [sandbox.run_code_tool_def(), ASK_USER_TOOL])

    # Start booting the sandbox now (no-op unless it's the Lambda backend and it's gone cold).
    # It takes tens of seconds to come up, and the model spends at least one turn writing code
    # before it calls run_code — so overlap the two instead of making the user wait for both.
    sandbox.prewarm(logger)

    attach_note = slack_files.attachments_for_prompt(staging)
    user_turn = f"{question}\n\n{attach_note}" if attach_note else question
    messages = _normalize(list(history) + [{"role": "user", "content": user_turn}])

    artifacts: list = []
    work_dirs: list = []
    last_error: str | None = None       # so a silent wrap-up can still explain what went wrong
    used_a_tool = False                 # did anything actually happen this turn?
    nudged = False                      # we only ever poke the model once (see below)
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

        reporter.doing("reading your request" if step == 0 else "working out what to do next")
        try:
            msg = client.messages.create(
                model=ACTION_MODEL,
                max_tokens=MAX_TOKENS,
                thinking={"type": "disabled"},
                system=ACTION_SYSTEM,
                tools=tool_defs,
                messages=messages,
                timeout=MODEL_TIMEOUT,
            )
        except Exception as err:
            # Don't let this escape as a bare exception: the user would get a generic
            # "something went wrong" and we'd lose the trace and any files already made.
            logger.exception("agent: model call failed on step %d", step + 1)
            trace.append(f"MODEL ERROR[{step + 1}]: {type(err).__name__}: {err}")
            return AgentResult(text=_model_failure_text(err, artifacts), artifacts=artifacts,
                               work_dirs=work_dirs, trace=trace, failed=True)
        if cancel_event.is_set():
            return cancelled_result()

        # The assistant turn (text + tool_use blocks) must be appended verbatim before the
        # matching tool_result turn. An empty turn can't go back into history at all — the API
        # rejects empty content — and there'd be nothing to answer anyway.
        if msg.content:
            messages.append({"role": "assistant", "content": msg.content})

        say = _text_of(msg)
        tool_calls = [b for b in msg.content if getattr(b, "type", None) == "tool_use"]
        logger.info("agent: step %d/%d | stop=%s | says=%r | calls=%s",
                    step + 1, MAX_TOOL_ITERATIONS, msg.stop_reason, _short(say, 160),
                    [b.name for b in tool_calls])
        if say:
            trace.append(f"ASSISTANT[{step + 1}]: {say}")

        if msg.stop_reason != "tool_use":
            # The "silently drops off" case, in both its forms. (a) A file is sitting there,
            # the model says "on it — running the extractor now", and just… stops. (b) Its last
            # tool call FAILED and it stops with "let me retry that" — the exact message seen in
            # production, with nothing after it. Either way a promise would become the final
            # answer and the user would wait forever. Poke it exactly once to act or to admit.
            # `not tool_calls` matters: a max_tokens/refusal turn can carry a half-written
            # tool_use block, and a plain user message after that would leave a tool_use with no
            # matching tool_result, which the API rejects outright.
            if not used_a_tool and staging is not None and staging.files:
                nudge = ("You stopped without calling any tool, so nothing has actually happened "
                         "yet and the user is still waiting. Either do the work now with the right "
                         "tool, or tell them plainly what is stopping you and what you need from "
                         "them. Do not answer with a description of what you are about to do.")
            elif last_error:
                nudge = ("Your last tool call failed and you have stopped without calling another "
                         "tool, so nothing further will run. If you are giving up, tell the user "
                         "plainly: what you were trying to do, what went wrong, and what they could "
                         "do about it. If you meant to keep going, make the tool call NOW. Never "
                         "describe an action you are not actually taking.")
            else:
                nudge = None
            if (nudge and msg.content and not tool_calls and not nudged
                    and step + 1 < MAX_TOOL_ITERATIONS):
                nudged = True
                trace.append(f"NUDGE[{step + 1}]: stopped without a tool call (last_error={bool(last_error)})")
                logger.info("agent: model stopped without a tool call (last_error=%s); nudging once",
                            bool(last_error))
                messages.append({"role": "user", "content": nudge})
                continue

            text = _wrap_up_text(say, msg.stop_reason, artifacts, last_error)
            trace.append(f"DONE: {text}")
            logger.info("agent: done after %d step(s) | stop=%s | said=%s",
                        step + 1, msg.stop_reason, bool(say))
            return AgentResult(text=text, artifacts=artifacts, work_dirs=work_dirs, trace=trace,
                               failed=not say and bool(last_error))

        # Surface the model's friendly preamble ("running the Schedule Extractor now…") so the
        # user can see what's happening and stop it if it's wrong.
        if say:
            reporter.say(say)

        tool_results = []
        for block in tool_calls:
            if cancel_event.is_set():
                return cancelled_result()
            name = block.name

            # The model wants a decision from the user. Hand the turn back with its question —
            # the answer arrives as the next message in the thread, which is already how
            # history works, so there's no pending state to keep anywhere.
            if name == "ask_user":
                asked = (block.input or {}).get("question") or "Could you tell me a bit more about what you'd like?"
                why = (block.input or {}).get("why")
                text = f":raising_hand: {asked}" + (f"\n\n_{why}_" if why else "")
                trace.append(f"ASK_USER[{step + 1}]: {asked}")
                logger.info("agent: stopping to ask the user | %r", _short(asked, 160))
                return AgentResult(text=text, artifacts=artifacts, work_dirs=work_dirs,
                                   trace=trace, awaiting_user=True)

            if not say:
                reporter.say(f"Running {name}…")
            reporter.doing(f"running `{name}`",
                           "Big files can take a couple of minutes." if staging.files else None)
            trace.append(f"CALL[{step + 1}] {name}({_short(json.dumps(block.input), 300)})")
            used_a_tool = True

            # notify= is the "still booting up, giving it 10 more seconds" channel: the same
            # thought restated with a new number, so it uses waiting() to replace its previous
            # line rather than posting a fresh message per retry.
            def _do(block=block, name=name):
                if name == "run_code":
                    return sandbox.run_code(block.input, staging, logger, notify=reporter.waiting)
                if name in tool_specs:
                    return tool_runner.run_tool(tool_specs[name], block.input, staging, logger,
                                                notify=reporter.waiting)
                return tool_runner.ToolInvocationResult.err(
                    f"Unknown tool {name!r}. Available: {sorted(tool_specs) + ['run_code', 'ask_user']}."
                )

            # Run in a worker thread so a cancel mid-tool abandons it instead of blocking.
            # run_tool/run_code promise not to raise, but a crash here must still become a
            # readable tool_result rather than killing the whole turn.
            try:
                res, was_cancelled = _run_interruptible(_do, cancel_event)
            except Exception as err:
                logger.exception("agent: tool %s crashed outright", name)
                res, was_cancelled = tool_runner.ToolInvocationResult.err(
                    f"{name} crashed: {type(err).__name__}: {err}"), False
            if was_cancelled:
                return cancelled_result()

            logger.info("agent: tool %s -> %s | %s | artifacts=%s",
                        name, res.status, _short(res.error or res.summary, 160),
                        [a.get("filename") for a in res.artifacts])
            trace.append(f"RESULT {name}: status={res.status} | {res.summary or res.error} "
                         f"| artifacts={[a.get('filename') for a in res.artifacts]}")
            # Tell the user about the obstacle NOW, rather than letting it surface only if the
            # model happens to mention it (or vanish entirely if the model just stops).
            if res.status == "error":
                last_error = res.error or "unknown error"
                reporter.snag(f"`{name}` hit a problem: {_short(last_error, 180)}")
                reporter.doing("working out how to get around that")
            else:
                last_error = None      # a later success clears an earlier failure
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
    reporter.say("Wrapping up…")
    reporter.doing("writing up what I managed to do")
    logger.info("agent: hit step cap (%d); forcing finalize", MAX_TOOL_ITERATIONS)
    delivered = " The files I completed are attached above." if artifacts else ""
    snag = f" The last thing that went wrong: {_short(last_error, 300)}." if last_error else ""
    # A system-prompt suffix was not enough: mid-debugging, the model answered "Let me retry
    # loading it now." and that became the last thing the user ever saw. The instruction now
    # goes into the conversation itself, right where the model is looking, and the header
    # below is fixed text so the state is unmistakable whatever it writes.
    _append_user_text(messages, (
        "You have used every step you had. Nothing further will run this turn, so do NOT "
        "describe what you will do next — there is no next. Report to the user, briefly and "
        "plainly: what they asked for, what you tried, what went wrong each time, and what "
        "they could do now."
    ))
    header = f":warning: I ran out of steps before I could finish this.{delivered}"
    try:
        # `tools` stays because the history holds tool_use/tool_result blocks; tool_choice
        # "none" is what actually keeps the model from calling anything.
        final = client.messages.create(
            model=ACTION_MODEL,
            max_tokens=MAX_TOKENS,
            thinking={"type": "disabled"},
            system=ACTION_SYSTEM,
            messages=messages,
            tools=tool_defs,
            tool_choice={"type": "none"},
            timeout=MODEL_TIMEOUT,
        )
        summary = _text_of(final)
        text = f"{header}\n\n{summary}" if summary else f"{header}{snag}"
    except Exception:
        logger.exception("force-finalize call failed")
        text = f"{header}{snag}"
    trace.append(f"FINALIZE: {text}")
    return AgentResult(text=text, artifacts=artifacts, work_dirs=work_dirs, trace=trace)

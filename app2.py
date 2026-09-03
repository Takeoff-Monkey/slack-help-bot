"""
Takeoff Monkey AI Slack bot.

Answers questions about the company's internal tech stack (automations, apps,
widgets) by retrieving from a library of HTML skill files in docs/skills/.

Pipeline per question:
  1. Selector call — send the user question + the skills manifest to Claude;
     get back a small JSON array of relevant skill IDs.
  2. Answer call  — load those HTML files (body content only), send them with
     the question to Claude; stream the final answer back to Slack.

The manifest in the selector's system prompt is cached (prompt caching) so
repeated calls only pay full price for the first one in the 5-minute window.

Triggers:
  - @-mention of the bot in any channel it's a member of
  - any message in a DM with the bot
"""

import concurrent.futures
import json
import logging
import os
import re
import ssl
from pathlib import Path

import anthropic
import certifi
import urllib.request
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import canvas_knowledge

# Agentic tool-use: the bot can run specialized tools (tools/<name>/) and a sandboxed
# code fallback on user-attached files, in addition to answering questions.
import agent_loop
import slack_files
import status
import tasks
import tool_registry


ssl_context = ssl.create_default_context(cafile=certifi.where())
urllib.request.urlopen("https://slack.com", context=ssl_context)

load_dotenv()

# Configure logging BEFORE the Bolt App is built, not in __main__ as before. Bolt copies the
# root logger's level into its own loggers at construction time, so the old late basicConfig()
# left every listener logger stuck at WARNING — which is why not one of the bot's INFO lines
# ("agent: step 2/6 …", "run_code: backend=lambda …") ever reached the Heroku logs, and a
# failing turn showed up only as a row of httpx 200s.
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

# Comma-separated Slack user IDs allowed to talk to the bot. If unset or empty,
# the bot responds to everyone (production mode). Use during testing to keep
# the bot invisible to the rest of the team — set ALLOWED_USERS=U01XXXXXXXX.
ALLOWED_USERS = {
    uid.strip()
    for uid in os.environ.get("ALLOWED_USERS", "").split(",")
    if uid.strip()
}

# Base URL where the skill HTML files are publicly served (e.g. GitHub Pages).
# If set, the bot's "Sources" footer links to the actual HTML docs; if unset,
# sources are shown as plain backticked skill IDs.
# Example: https://takeoff-monkey.github.io/slack-help-bot/skills
SKILL_DOCS_BASE_URL = os.environ.get("SKILL_DOCS_BASE_URL", "").rstrip("/")

# Which backend runs the tools: "local" (subprocess in each tool's venv — proves the flow
# with no cloud) or "lambda" (invoke each tool's AWS Lambda). The bot behaves identically
# either way; only file staging and dispatch differ. See tool_runner.py / slack_files.py.
TOOL_BACKEND = os.environ.get("TOOL_BACKEND", "local")

# Comma-separated Slack channel IDs whose canvas tabs the bot reads as live
# knowledge (Slack as the single source of truth — see canvas_knowledge.py).
# Unset = feature off; the bot behaves exactly as it did before. The bot must
# be a member of each channel, and the app needs files:read + groups:read.
KNOWLEDGE_CHANNELS = [
    c.strip() for c in os.environ.get("KNOWLEDGE_CHANNELS", "").split(",") if c.strip()
]
# How often (seconds) to re-sync canvases. Unchanged canvases are skipped via
# their 'updated' timestamp, so this is cheap.
CANVAS_SYNC_INTERVAL = int(os.environ.get("CANVAS_SYNC_INTERVAL_SECONDS", "600"))

# Two-tier model setup: Haiku for the cheap selector (structured JSON pick),
# Sonnet for the answer (reading + summarizing skill docs in Slack-friendly prose).
# Opus would be overkill for both — this is grounded retrieval, not deep reasoning.
# Model IDs are env-overridable so they can be bumped without a code change.
SELECTOR_MODEL = os.environ.get("SELECTOR_MODEL", "claude-haiku-4-5")
ANSWER_MODEL = os.environ.get("ANSWER_MODEL", "claude-sonnet-5")
MAX_SKILLS_PER_QUESTION = 5
MAX_SLACK_MESSAGE_CHARS = 3800

SKILLS_DIR = Path(__file__).parent / "docs" / "skills"
MANIFEST_PATH = SKILLS_DIR / "_manifest.json"

# The SDK defaults to a 600s read timeout with 2 retries — worst case, half an hour of a
# frozen "Thinking…" message while the user assumes work is happening. Cap it so a wedged
# request becomes an error the bot can actually tell them about.
ANTHROPIC_TIMEOUT = float(os.environ.get("ANTHROPIC_TIMEOUT_SECONDS", "120"))
anthropic_client = anthropic.Anthropic(
    api_key=ANTHROPIC_API_KEY,
    timeout=ANTHROPIC_TIMEOUT,
    max_retries=int(os.environ.get("ANTHROPIC_MAX_RETRIES", "2")),
)

# Bolt runs every listener on a 5-thread pool by default. A turn can hold its thread for
# minutes (a tool Lambda, a cold sandbox), so the 6th concurrent message would sit in a queue
# showing the user *nothing at all* — not even the placeholder — and a "stop" would queue
# behind the very task it was meant to cancel. These threads are almost always blocked on
# network I/O, so a bigger pool costs little.
app = App(
    token=SLACK_BOT_TOKEN,
    listener_executor=concurrent.futures.ThreadPoolExecutor(
        max_workers=int(os.environ.get("BOLT_LISTENER_THREADS", "16")),
        thread_name_prefix="bolt",
    ),
)

# Fetch the bot's own user ID at startup so we can identify our own messages
# when pulling conversation history.
try:
    BOT_USER_ID = app.client.auth_test()["user_id"]
    print(f"Bot user ID: {BOT_USER_ID}")
except Exception as e:
    print(f"Warning: could not fetch bot user ID at startup ({e})")
    BOT_USER_ID = None

MAX_HISTORY_MESSAGES = 10
# How many thread messages to pull to find those 10 turns. A turn now leaves its progress
# lines in the thread as well as its answer, so fetching MAX_HISTORY_MESSAGES + 1 would come
# back as mostly scaffolding and the real conversation would be filtered away to nothing.
HISTORY_FETCH_LIMIT = 100

SLACK_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
STYLE_BLOCK_RE = re.compile(r"<style[^>]*>.*?</style>", re.IGNORECASE | re.DOTALL)
SCRIPT_BLOCK_RE = re.compile(r"<script[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
BODY_RE = re.compile(r"<body[^>]*>(.*?)</body>", re.IGNORECASE | re.DOTALL)


def load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


MANIFEST = load_manifest()
INDEX_FOR_LLM = [
    {
        "id": s["id"],
        "name": s["name"],
        "platform": s["platform"],
        "status": s["status"],
        "summary": s["summary"],
        "tags": s["tags"],
    }
    for s in MANIFEST["skills"]
]
INDEX_JSON = json.dumps(INDEX_FOR_LLM, indent=2)
VALID_SKILL_IDS = {s["id"] for s in MANIFEST["skills"]}
SKILL_FILE_BY_ID = {s["id"]: s["file"] for s in MANIFEST["skills"]}

# Tools the bot can run on demand (tools/<name>/tool.json). A malformed manifest is logged
# and skipped, so a broken tool never stops the Q&A bot from starting.
TOOLS = tool_registry.discover_tools()


# Compact tool summary used by the selector only to classify intent (not for skill picking).
TOOLS_FOR_SELECTOR = "\n".join(
    f"- {name}: {spec.when_to_use}" for name, spec in TOOLS.items()
) or "(none registered)"


SELECTOR_SYSTEM = f"""You help an AI Slack bot triage a teammate's LATEST message in two ways.

1) INTENT — classify what they want:
   - "action": they want the bot to DO something to a file or produce an output — e.g. extract tables/schedules from an attached PDF, highlight text, convert/split/merge a file — or they explicitly name one of the runnable tools below. If a file is attached and they ask for anything to be done with it, that is "action".
   - "qa": anything else (asking a question, chatting, troubleshooting).

   Runnable tools (for intent classification only — NEVER put these in skill_ids):
{TOOLS_FOR_SELECTOR}

2) SKILLS — pick at most {MAX_SKILLS_PER_QUESTION} skill IDs from the list below that are directly relevant to the LATEST message (used when intent is "qa"; for "action" an empty list is fine). Each skill describes a system, automation, lambda, board automation, or tool, and the bot reads the full HTML for whichever you pick. Use prior turns as context for follow-ups like "what's wrong with it?". If the message is conversational, generic, or unrelated to the tech stack (e.g. "hi", "what can you do"), return an empty skill list.

Available skills:
{INDEX_JSON}"""


ANSWER_SYSTEM = """You are an AI assistant for Takeoff Monkey, a takeoff/estimating services company. You answer teammates' questions about the company's internal tech stack — Heroku bots, Zapier automations, AWS Lambdas, Google Cloud apps, Monday board automations, Chrome extensions, etc.

Ground every answer in the skill documentation provided in the user message. Be concise (Slack-appropriate length — typically 2–8 sentences, or a short bulleted list). When you reference a specific system, name it. If the provided skills don't cover the question, say so directly — don't guess or pad.

Formatting — your output is rendered in Slack, which uses mrkdwn (not standard Markdown). Use only these conventions:
- Inline code or identifiers: backticks, like `job-number` or `#12345!`
- Bold: single asterisks, like *important* (NOT **double asterisks**)
- Italics: single underscores, like _example_
- Bullets: start each line with "- "
- Links: write plain URLs prefaced with `http://` or `https://` (e.g. https://github.com/foo/bar). Slack auto-detects and linkifies them. Do NOT wrap URLs in angle brackets like <https://...> and do NOT use [text](url) Markdown syntax — both render as literal characters.
Do not use double-asterisk bold or # / ## headings — Slack will display them as literal characters instead of rendering them.

Escalation pointer: most systems are owned by Tommy Lather; mention escalating to Tommy if the user is reporting a break and the skill doesn't name a different owner."""


def clean_skill_for_llm(html: str) -> str:
    """Strip <head>, <style>, <script> blocks before sending to Claude.

    The CSS is identical across all skill files — sending it on every call
    wastes tokens. Body content is what carries the actual knowledge.
    """
    html = STYLE_BLOCK_RE.sub("", html)
    html = SCRIPT_BLOCK_RE.sub("", html)
    body_match = BODY_RE.search(html)
    if body_match:
        return body_match.group(1).strip()
    return html.strip()


def load_skill_body(skill_id: str) -> str | None:
    file_name = SKILL_FILE_BY_ID.get(skill_id)
    if not file_name:
        return None
    path = SKILLS_DIR / file_name
    try:
        html = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return clean_skill_for_llm(html)


SOURCES_FOOTER_RE = re.compile(r"\n\n_Sources:.*$", re.DOTALL)


def get_conversation_history(channel: str, thread_ts: str, current_ts: str, logger) -> list[dict]:
    """Fetch prior turns from this Slack *thread*, oldest first, formatted as
    Claude messages. Excludes the current message (caller appends it). History
    is always scoped to the thread, so each thread is an independent
    conversation — no context bleeds across threads or across separate DMs.
    Returns [] on any failure — the bot still works without history."""
    try:
        resp = app.client.conversations_replies(
            channel=channel, ts=thread_ts, limit=HISTORY_FETCH_LIMIT
        )
        raw = resp.get("messages", [])
    except Exception:
        logger.exception("Failed to fetch thread history (continuing without context)")
        return []

    history = []
    for msg in raw:
        if msg.get("ts") == current_ts:
            continue
        text = (msg.get("text") or "").strip()
        # Skip the progress lines every turn leaves behind — the placeholder, "Running X…",
        # "Still on it (42s)". They're the bot thinking out loud, not part of the
        # conversation. Snags and final answers are real statements and stay.
        if status.is_progress_line(text):
            continue
        is_bot = msg.get("user") == BOT_USER_ID or bool(msg.get("bot_id"))
        if is_bot:
            text = SOURCES_FOOTER_RE.sub("", text)
        else:
            text = strip_mention(text)
        if not text:
            continue
        history.append({"role": "assistant" if is_bot else "user", "content": text})

    return normalize_message_history(history)[-MAX_HISTORY_MESSAGES:]


def normalize_message_history(messages: list[dict]) -> list[dict]:
    """Claude requires messages to alternate user/assistant and start with
    user. Drop leading assistant messages; merge consecutive same-role."""
    while messages and messages[0]["role"] != "user":
        messages = messages[1:]
    merged: list[dict] = []
    for msg in messages:
        if merged and merged[-1]["role"] == msg["role"]:
            merged[-1]["content"] += "\n\n" + msg["content"]
        else:
            merged.append({"role": msg["role"], "content": msg["content"]})
    return merged


def selector_system_blocks() -> list[dict]:
    """System prompt for the selector. The static skill index is its own cached
    block (stable → permanent cache hits). Live canvases, which change as the
    team edits them, go in a second cached block so a canvas edit only busts
    that small block, not the 100-skill one."""
    blocks = [
        {
            "type": "text",
            "text": SELECTOR_SYSTEM,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    canvas_entries = canvas_knowledge.index_entries()
    if canvas_entries:
        blocks.append(
            {
                "type": "text",
                "text": (
                    "Additional live knowledge sources — Slack canvases the team "
                    "maintains directly. Treat these exactly like skills and pick "
                    "their IDs when relevant:\n"
                    + json.dumps(canvas_entries, indent=2)
                ),
                "cache_control": {"type": "ephemeral"},
            }
        )
    return blocks


def _run_selector(question: str, history: list[dict]) -> tuple[str, list[str]]:
    """One Haiku call that both classifies intent and picks skills. Returns
    (intent, skill_ids) where intent is "qa" or "action". The bot reuses this single call
    for routing (action vs Q&A) and for retrieval, so a normal Q&A turn still costs one
    selector call."""
    messages = normalize_message_history(history + [{"role": "user", "content": question}])
    response = anthropic_client.messages.create(
        model=SELECTOR_MODEL,
        max_tokens=512,
        system=selector_system_blocks(),
        messages=messages,
        output_config={
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "intent": {"type": "string", "enum": ["qa", "action"]},
                        "skill_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["intent", "skill_ids"],
                    "additionalProperties": False,
                },
            }
        },
    )
    text = next(b.text for b in response.content if b.type == "text")
    payload = json.loads(text)
    intent = payload.get("intent", "qa")
    picked = payload.get("skill_ids", [])
    allowed = VALID_SKILL_IDS | canvas_knowledge.valid_ids()
    valid = [sid for sid in picked if sid in allowed][:MAX_SKILLS_PER_QUESTION]
    return intent, valid


def select_skills(question: str, history: list[dict]) -> list[str]:
    return _run_selector(question, history)[1]


def answer_question(question: str, skill_ids: list[str], history: list[dict]) -> str:
    if skill_ids:
        bodies = []
        for sid in skill_ids:
            # Canvas IDs (canvas-…) resolve from the in-memory cache; everything
            # else is a static HTML skill loaded from disk.
            body = canvas_knowledge.get_body(sid)
            if body is None:
                body = load_skill_body(sid)
            if body is not None:
                bodies.append(f'<skill id="{sid}">\n{body}\n</skill>')
        skill_context = "\n\n".join(bodies) if bodies else "(no skills loaded)"
    else:
        skill_context = "(no skills selected — the question may be general or unrelated to the tech stack)"

    current_user_message = (
        f"Teammate's question:\n{question}\n\n"
        f"Relevant skill documentation:\n{skill_context}"
    )
    messages = normalize_message_history(
        history + [{"role": "user", "content": current_user_message}]
    )

    with anthropic_client.messages.stream(
        model=ANSWER_MODEL,
        max_tokens=2048,
        thinking={"type": "disabled"},
        output_config={"effort": "medium"},
        system=ANSWER_SYSTEM,
        messages=messages,
    ) as stream:
        final = stream.get_final_message()

    for block in final.content:
        if block.type == "text":
            # A cut-off answer that looks complete is its own kind of silent failure.
            if getattr(final, "stop_reason", None) == "max_tokens":
                return block.text + "\n\n_(That got cut off — ask me to continue.)_"
            return block.text
    return (":warning: The model came back with nothing for that one — no answer and no error. "
            "Try rephrasing it and I'll have another go.")


def _looks_like_action(question: str) -> bool:
    """Phase-1 routing heuristic: treat a message as an *action* (→ tool-use loop) when it
    explicitly names a registered tool. Attachments also force the action path (the caller
    handles that). Smarter text-only intent detection is folded into the selector later."""
    q = question.lower()
    return any(name in q or name.replace("-", " ") in q for name in TOOLS)


# Only used when the Haiku cancel-intent call fails. Deliberately narrow: "don't" used to be
# in here, which turned any sentence containing it ("don't forget the sheet index") into a
# cancellation of whatever was running.
_CANCEL_WORDS = ("stop", "cancel", "abort", "nevermind", "never mind", "wrong tool",
                 "wrong file", "halt")


def _is_cancel_request(question: str, logger) -> bool:
    """Decide whether a message sent WHILE a task is running is asking to stop it (e.g. "no
    that's the wrong tool", "actually I uploaded the wrong file", "stop"). Only called when a
    task is active, so the extra Haiku call is rare. Falls back to a keyword check on error."""
    q = (question or "").strip()
    if not q:
        return False
    try:
        resp = anthropic_client.messages.create(
            model=SELECTOR_MODEL,
            max_tokens=10,
            system=(
                "A task the assistant started is currently still running for the user. "
                "Decide if the user's new message is telling the assistant to STOP, cancel, "
                "abort, or undo that running task (e.g. 'no wrong tool', 'stop', 'actually "
                "that's the wrong file', 'never mind'). A brand-new unrelated request is NOT "
                "a cancellation. Answer with the JSON boolean."
            ),
            messages=[{"role": "user", "content": q}],
            output_config={
                "format": {
                    "type": "json_schema",
                    "schema": {
                        "type": "object",
                        "properties": {"cancel": {"type": "boolean"}},
                        "required": ["cancel"],
                        "additionalProperties": False,
                    },
                }
            },
        )
        text = next(b.text for b in resp.content if b.type == "text")
        return bool(json.loads(text).get("cancel"))
    except Exception:
        logger.exception("cancel-intent check failed; falling back to keywords")
        ql = q.lower()
        return any(w in ql for w in _CANCEL_WORDS)


def respond_to_question(question, history, staging, prefetched_skill_ids, reporter, on_artifacts, cancel_event, logger) -> "agent_loop.AgentResult":
    """Route a turn. When `staging` is not None we're on the *action* path (a file was
    attached, a tool was named, or the selector classified intent as "action") → run the
    tool-use loop. Otherwise it's the unchanged retrieval Q&A path, reusing the skills the
    caller already fetched (`prefetched_skill_ids`) so the selector isn't called twice.
    `on_artifacts` is the callback the loop fires to upload files the moment they're produced.
    Always returns an AgentResult so the caller treats both paths uniformly."""
    question = question.strip()
    if not question and not (staging and staging.files):
        return agent_loop.AgentResult(
            text=":wave: Ask me anything about Takeoff Monkey's internal tech stack — or attach a file and tell me what to do with it."
        )

    try:
        if staging is not None:
            return agent_loop.run_agent(
                anthropic_client, question, history, staging, TOOLS, reporter, on_artifacts, cancel_event, logger
            )

        if prefetched_skill_ids is None:
            reporter.doing("working out which docs cover this")
            skill_ids = select_skills(question, history)
        else:
            skill_ids = prefetched_skill_ids
        logger.info(f"Selected skills: {skill_ids} (history turns: {len(history)})")
        reporter.doing("reading the docs and writing your answer")
        answer = answer_question(question, skill_ids, history)
        if skill_ids:
            source_fmts = []
            for sid in skill_ids:
                canvas_source = canvas_knowledge.get_source(sid)
                if canvas_source:
                    name, permalink = canvas_source
                    source_fmts.append(f"<{permalink}|{name}>" if permalink else f"`{name}`")
                elif SKILL_DOCS_BASE_URL and sid in SKILL_FILE_BY_ID:
                    source_fmts.append(f"<{SKILL_DOCS_BASE_URL}/{SKILL_FILE_BY_ID[sid]}|{sid}>")
                else:
                    source_fmts.append(f"`{sid}`")
            answer += "\n\n_Sources: " + ", ".join(source_fmts) + "_"
        if len(answer) > MAX_SLACK_MESSAGE_CHARS:
            answer = answer[: MAX_SLACK_MESSAGE_CHARS - 20] + "… _(truncated)_"
        return agent_loop.AgentResult(text=answer)
    except anthropic.RateLimitError:
        logger.exception("Anthropic rate limit hit")
        # Not the hourglass, however apt: that's the progress-line prefix, and a line starting
        # with it is treated as scaffolding and dropped from thread history (status.is_progress_line).
        return agent_loop.AgentResult(
            text=":warning: I'm getting rate-limited by the model API and have "
                 "stopped. Nothing is still running — try me again in a minute.", failed=True)
    except anthropic.APIError as e:
        logger.exception("Anthropic API error")
        return agent_loop.AgentResult(
            text=f":x: I hit an API error and stopped: {e.message if hasattr(e, 'message') else e}",
            failed=True)
    except Exception as e:
        logger.exception("Unexpected error in respond_to_question")
        return agent_loop.AgentResult(
            text=f":x: Something went wrong on my side and I've stopped: `{type(e).__name__}: {e}`. "
                 f"Nothing is still running in the background.", failed=True)


def strip_mention(text: str) -> str:
    return SLACK_MENTION_RE.sub("", text).strip()


def is_allowed(user_id: str | None) -> bool:
    if not ALLOWED_USERS:
        return True
    return user_id in ALLOWED_USERS


def recent_thread_files(channel: str, thread_ts: str, logger) -> list[dict]:
    """Files the user attached earlier in this thread, so an action follow-up that doesn't
    re-attach ("now highlight 'landscape' in it") can still operate on the original file.
    Returns the `files` of the most recent NON-bot message that had attachments (newest
    first), or []. The bot's own uploaded results are skipped so we never feed an output
    back in as an input."""
    try:
        resp = app.client.conversations_replies(channel=channel, ts=thread_ts, limit=100)
        msgs = resp.get("messages", [])
    except Exception:
        logger.exception("Failed to fetch thread history for file carry-over")
        return []
    for msg in reversed(msgs):
        if msg.get("user") == BOT_USER_ID or msg.get("bot_id"):
            continue
        files = msg.get("files") or []
        if files:
            return files
    return []


def reply_with_thinking_indicator(question, channel, thread_ts, files, history, say, client, logger):
    """Post a 'Thinking…' placeholder, generate the reply, and narrate the work in between.
    On the action path that means short progress lines ("Running schedule-extractor…") and
    uploading produced files into the thread as they appear.

    The first real line replaces the placeholder; each new thought after that is its own
    message, so the thread reads back as a record of what the bot did rather than a single
    line that overwrites itself. A status.Reporter owns all of it — it speaks up on its own
    after 30 seconds of silence, and, because every exit below runs through finish()/fail(),
    the turn is never left reading "Thinking…" forever. That last part is the bug this fixes:
    any exception outside the one try/except used to leave the bot looking busy for good.
    """
    with status.turn(channel, thread_ts, say, client, logger) as reporter:
        _run_turn(question, channel, thread_ts, files, history, say, client, logger, reporter)


def _run_turn(question, channel, thread_ts, files, history, say, client, logger, reporter):
    """The turn itself. Anything that escapes here is caught and reported by status.turn(),
    so this body is free to `return` early without stranding the user."""
    # Register this run so a follow-up message on another thread can cancel it mid-task.
    task_key = tasks.key(channel, thread_ts)
    cancel_event = tasks.register(task_key)

    staging = None
    uploaded_refs: set = set()
    undelivered: list = []

    def emit_artifacts(arts):
        """Upload produced files the moment a tool emits them (deduped by storage ref), so the
        user always gets output even if a later step errors or the loop hits its cap."""
        if staging is None or not arts:
            return
        undelivered.extend(
            slack_files.upload_artifacts(client, channel, thread_ts, arts, staging, logger,
                                         seen=uploaded_refs)
        )

    try:
        # Routing. A file or an explicitly-named tool always means "action". Otherwise ask the
        # selector (the one call the Q&A path needs anyway) to classify intent, and reuse its
        # skill picks so it isn't called twice. staging stays None on the pure-Q&A path.
        prefetched_skills = None
        action = bool(files) or _looks_like_action(question)
        if not action and question.strip():
            reporter.doing("working out what you're asking for")
            try:
                intent, prefetched_skills = _run_selector(question, history)
                action = intent == "action"
            except Exception:
                logger.exception("selector intent classification failed; defaulting to Q&A")

        # On an action turn with no fresh attachment, reuse the file from earlier in the thread
        # so follow-ups ("now highlight 'landscape' in it") don't make the user re-upload.
        effective_files = files or []
        if action and not effective_files:
            effective_files = recent_thread_files(channel, thread_ts, logger)
            if effective_files:
                logger.info("Carrying %d file(s) forward from earlier in this thread", len(effective_files))

        if action:
            if effective_files:
                reporter.doing("downloading the file(s) from Slack")
            staging = slack_files.stage_attachments(effective_files, SLACK_BOT_TOKEN, TOOL_BACKEND, logger)
            staging.carried_forward = bool(effective_files) and not files
            # Staging skips anything it can't fetch. If that swallowed everything the user
            # attached to THIS message, say so now rather than letting the model flounder over
            # an empty attachment list. Files merely carried over from earlier in the thread
            # are a different matter — the user didn't ask for them this turn, so a failure
            # there just goes into the prompt (staging.skipped) and the turn carries on.
            if files and not staging.files:
                why = ("\n" + "\n".join(f"- {r}" for r in staging.skipped)) if staging.skipped else ""
                reporter.fail(
                    f"I couldn't get hold of the file(s) for this:{why}\n\nTry re-uploading, "
                    f"or tell me what you'd like me to do without them."
                )
                return

        result = respond_to_question(
            question, history, staging, prefetched_skills, reporter, emit_artifacts, cancel_event, logger
        )
        # The Q&A path has no way to abandon a call mid-flight, so a "stop" during one used to
        # be acknowledged and then contradicted a second later by the answer landing anyway.
        if cancel_event.is_set() and not result.cancelled:
            logger.info("Turn completed after a cancel request — discarding the answer")
            result = agent_loop.AgentResult(
                text=":octagonal_sign: _Cancelled._", artifacts=result.artifacts,
                work_dirs=result.work_dirs, trace=result.trace, cancelled=True,
            )
        text = result.text or "Done."
        if not reporter.finish(text):
            # finish() already retries as a new message if editing fails, so False means Slack
            # refused both. The answer still has to reach the user — try once more ourselves.
            logger.warning("Could not deliver the answer via the reporter; retrying directly")
            try:
                say(text=text, thread_ts=thread_ts)
            except Exception:
                logger.exception("Could not post the answer at all")

        # Final sweep — uploads anything not already delivered incrementally (no-op if all
        # were). Skipped when cancelled so we don't push out the very output the user rejected.
        if staging is not None and result.artifacts and not result.cancelled:
            try:
                undelivered.extend(slack_files.upload_artifacts(
                    client, channel, thread_ts, result.artifacts, staging, logger, seen=uploaded_refs
                ))
            except Exception:
                logger.exception("Final artifact upload failed")
                undelivered.extend(result.artifacts)

        # Optional: persist the step-by-step trace for debugging (off unless TRACE_TO_S3 set).
        if staging is not None and result.trace and os.environ.get("TRACE_TO_S3"):
            slack_files.write_trace(
                staging, "\n".join(result.trace), f"{thread_ts}.txt", logger
            )
    finally:
        # Always tell the user about a file that never made it, whichever way we got here —
        # the answer above may well have promised it. reporter.note() posts a new message, so
        # it works even once the answer has sealed the status message.
        # Reconcile before crying wolf: an upload that failed incrementally is deliberately
        # left out of uploaded_refs, so the final sweep retries it and often succeeds. Only
        # something STILL missing is worth a warning — otherwise we'd post "couldn't attach
        # it" directly underneath the file we just attached.
        missing = list(dict.fromkeys(
            art.get("filename") or "a file" for art in undelivered
            if art.get("ref") not in uploaded_refs
        ))
        if missing:
            reporter.note(
                ":warning: I produced " + ", ".join(f"`{f}`" for f in missing)
                + " but couldn't attach " + ("it" if len(missing) == 1 else "them")
                + " to this thread. Ask me to try again and I'll re-run it."
            )
        tasks.deregister(task_key, cancel_event)
        if staging is not None:
            slack_files.cleanup([staging], logger)


def _maybe_cancel(question, channel, thread_ts, say, logger) -> bool:
    """If a task is running on this thread and this message asks to stop it, cancel it and
    return True (the caller should stop processing). Cheap: the cancel-intent model call only
    runs when something is actually in flight on this thread."""
    key = tasks.key(channel, thread_ts)
    if not tasks.has_active(key) or not _is_cancel_request(question, logger):
        return False
    n = tasks.cancel(key)
    logger.info("Cancel requested — tripped %d running task(s) on %s", n, key)
    say(
        text=":octagonal_sign: Okay, I've stopped that — let me know what you'd like instead.",
        thread_ts=thread_ts,
    )
    return True


@app.event("app_mention")
def handle_app_mention(event, say, client, logger):
    if event.get("bot_id"):
        return
    # A DM that @-mentions the bot arrives as BOTH app_mention and message — handling both
    # ran the whole turn twice, with two placeholders and the tool billed twice. DM channel
    # IDs start with "D"; the message handler already covers them.
    if str(event.get("channel", "")).startswith("D"):
        logger.info("Ignoring app_mention in a DM — the message handler owns this turn")
        return
    user_id = event.get("user")
    if not is_allowed(user_id):
        logger.info(f"Ignoring app_mention from non-allowlisted user {user_id}")
        return
    question = strip_mention(event.get("text", ""))
    channel = event["channel"]
    current_ts = event["ts"]
    thread_ts = event.get("thread_ts") or current_ts
    files = event.get("files") or []
    logger.info(f"app_mention question: {question!r} (files: {len(files)})")
    if _maybe_cancel(question, channel, thread_ts, say, logger):
        return
    history = get_conversation_history(channel, thread_ts, current_ts, logger)
    reply_with_thinking_indicator(
        question, channel, thread_ts, files, history, say, client, logger
    )


@app.event("message")
def handle_message(event, say, client, logger):
    if event.get("channel_type") != "im":
        return
    # An edit or deletion arrives as message_changed/message_deleted with the original nested
    # under event["message"] — so the bot_id check below misses it and the bot would re-run
    # the whole turn (a second placeholder, the tool billed twice) on any edit.
    if event.get("subtype") in ("bot_message", "message_changed", "message_deleted"):
        return
    if event.get("bot_id"):
        return
    user_id = event.get("user")
    if not is_allowed(user_id):
        logger.info(f"Ignoring DM from non-allowlisted user {user_id}")
        return
    question = strip_mention(event.get("text", ""))
    channel = event["channel"]
    current_ts = event["ts"]
    # Thread the reply instead of posting a flat DM. A brand-new top-level DM
    # starts its own thread (rooted at this message); a message inside an
    # existing thread continues it. Either way history is scoped to the thread,
    # so a fresh top-level DM is a fresh conversation — the bot no longer drags
    # every past DM into context.
    thread_ts = event.get("thread_ts") or current_ts
    files = event.get("files") or []
    logger.info(f"DM question: {question!r} (files: {len(files)})")
    if _maybe_cancel(question, channel, thread_ts, say, logger):
        return
    history = get_conversation_history(channel, thread_ts, current_ts, logger)
    reply_with_thinking_indicator(
        question, channel, thread_ts, files, history, say, client, logger
    )


@app.error
def handle_unexpected_listener_error(error, body, logger):
    """Bolt's default handler writes to the log and nothing else, so anything that escapes a
    listener is, from the user's side, the bot simply never answering. Try to say so."""
    logger.exception("Unhandled listener error: %s", error)
    try:
        event = (body or {}).get("event") or {}
        channel = event.get("channel")
        thread_ts = event.get("thread_ts") or event.get("ts")
        if channel and thread_ts:
            app.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=f":x: I hit an unexpected error and couldn't process that "
                     f"(`{type(error).__name__}`). Nothing is running — please try again.",
            )
    except Exception:
        logger.exception("Could not even report the listener error to Slack")


if __name__ == "__main__":
    mode = (
        f"private mode (allowlist: {sorted(ALLOWED_USERS)})"
        if ALLOWED_USERS
        else "open mode (responds to everyone)"
    )
    print(f"Bot starting — {len(MANIFEST['skills'])} skills loaded, {mode}")

    # Live canvas knowledge: pull configured channels' canvas tabs in the
    # background so Slack stays the single source of truth. Inert unless
    # KNOWLEDGE_CHANNELS is set.
    if KNOWLEDGE_CHANNELS:
        canvas_knowledge.start(
            app.client, SLACK_BOT_TOKEN, KNOWLEDGE_CHANNELS, CANVAS_SYNC_INTERVAL
        )
        print(
            f"Canvas knowledge sync started — channels={KNOWLEDGE_CHANNELS}, "
            f"every {CANVAS_SYNC_INTERVAL}s"
        )
    else:
        print("KNOWLEDGE_CHANNELS unset — canvas knowledge disabled")

    SocketModeHandler(app, SLACK_APP_TOKEN).start()

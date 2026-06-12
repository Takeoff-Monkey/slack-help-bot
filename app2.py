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
import tool_registry


ssl_context = ssl.create_default_context(cafile=certifi.where())
urllib.request.urlopen("https://slack.com", context=ssl_context)

load_dotenv()

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
SELECTOR_MODEL = "claude-haiku-4-5"
ANSWER_MODEL = "claude-sonnet-4-6"
MAX_SKILLS_PER_QUESTION = 5
MAX_SLACK_MESSAGE_CHARS = 3800

SKILLS_DIR = Path(__file__).parent / "docs" / "skills"
MANIFEST_PATH = SKILLS_DIR / "_manifest.json"

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
app = App(token=SLACK_BOT_TOKEN)

# Fetch the bot's own user ID at startup so we can identify our own messages
# when pulling conversation history.
try:
    BOT_USER_ID = app.client.auth_test()["user_id"]
    print(f"Bot user ID: {BOT_USER_ID}")
except Exception as e:
    print(f"Warning: could not fetch bot user ID at startup ({e})")
    BOT_USER_ID = None

MAX_HISTORY_MESSAGES = 10

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
            channel=channel, ts=thread_ts, limit=MAX_HISTORY_MESSAGES + 1
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
        if not text or text == THINKING_PLACEHOLDER:
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
            return block.text
    return "Sorry, I couldn't generate a response."


def _looks_like_action(question: str) -> bool:
    """Phase-1 routing heuristic: treat a message as an *action* (→ tool-use loop) when it
    explicitly names a registered tool. Attachments also force the action path (the caller
    handles that). Smarter text-only intent detection is folded into the selector later."""
    q = question.lower()
    return any(name in q or name.replace("-", " ") in q for name in TOOLS)


def respond_to_question(question, history, staging, prefetched_skill_ids, progress, on_artifacts, logger) -> "agent_loop.AgentResult":
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
                anthropic_client, question, history, staging, TOOLS, progress, on_artifacts, logger
            )

        skill_ids = prefetched_skill_ids if prefetched_skill_ids is not None else select_skills(question, history)
        logger.info(f"Selected skills: {skill_ids} (history turns: {len(history)})")
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
        return agent_loop.AgentResult(text=":hourglass_flowing_sand: I'm getting rate-limited. Try again in a minute.")
    except anthropic.APIError as e:
        logger.exception("Anthropic API error")
        return agent_loop.AgentResult(text=f":warning: I hit an API error: {e.message if hasattr(e, 'message') else e}")
    except Exception as e:
        logger.exception("Unexpected error in respond_to_question")
        return agent_loop.AgentResult(text=f":x: Something went wrong: {e}")


def strip_mention(text: str) -> str:
    return SLACK_MENTION_RE.sub("", text).strip()


def is_allowed(user_id: str | None) -> bool:
    if not ALLOWED_USERS:
        return True
    return user_id in ALLOWED_USERS


THINKING_PLACEHOLDER = ":hourglass_flowing_sand: _Thinking…_"


def reply_with_thinking_indicator(question, channel, thread_ts, files, history, say, client, logger):
    """Post a 'Thinking…' placeholder, generate the reply, then edit the placeholder to the
    real answer. On the action path it also streams short progress updates ("Running
    schedule-extractor…") and uploads any produced files into the thread afterward. Falls
    back to a fresh message if chat.update fails, and always cleans up staged temp files."""
    placeholder = say(text=THINKING_PLACEHOLDER, thread_ts=thread_ts)
    placeholder_ts = placeholder.get("ts") if placeholder else None

    # Throttled progress: only edits the placeholder when the message actually changes.
    last_progress = {"text": None}

    def progress(msg):
        if not placeholder_ts or msg == last_progress["text"]:
            return
        last_progress["text"] = msg
        try:
            client.chat_update(
                channel=channel, ts=placeholder_ts, text=f":hourglass_flowing_sand: _{msg}_"
            )
        except Exception:
            logger.exception("progress chat_update failed (continuing)")

    # Routing. A file or an explicitly-named tool always means "action". Otherwise ask the
    # selector (the one call the Q&A path needs anyway) to classify intent, and reuse its
    # skill picks so it isn't called twice. staging stays None on the pure-Q&A path.
    prefetched_skills = None
    action = bool(files) or _looks_like_action(question)
    if not action and question.strip():
        try:
            intent, prefetched_skills = _run_selector(question, history)
            action = intent == "action"
        except Exception:
            logger.exception("selector intent classification failed; defaulting to Q&A")
    staging = (
        slack_files.stage_attachments(files or [], SLACK_BOT_TOKEN, TOOL_BACKEND, logger)
        if action else None
    )

    # Upload produced files the moment a tool emits them (deduped by storage ref), so the
    # user always gets output even if a later step errors or the loop hits its cap.
    uploaded_refs: set = set()

    def emit_artifacts(arts):
        if staging is None or not arts:
            return
        slack_files.upload_artifacts(client, channel, thread_ts, arts, staging, logger, seen=uploaded_refs)

    try:
        result = respond_to_question(
            question, history, staging, prefetched_skills, progress, emit_artifacts, logger
        )
        text = result.text or "Done."
        if placeholder_ts:
            try:
                client.chat_update(channel=channel, ts=placeholder_ts, text=text)
            except Exception:
                logger.exception("chat_update failed; posting answer as a new message")
                say(text=text, thread_ts=thread_ts)
        else:
            say(text=text, thread_ts=thread_ts)

        # Final sweep — uploads anything not already delivered incrementally (no-op if all were).
        if staging is not None and result.artifacts:
            slack_files.upload_artifacts(
                client, channel, thread_ts, result.artifacts, staging, logger, seen=uploaded_refs
            )

        # Optional: persist the step-by-step trace for debugging (off unless TRACE_TO_S3 set).
        if staging is not None and result.trace and os.environ.get("TRACE_TO_S3"):
            slack_files.write_trace(
                staging, "\n".join(result.trace), f"{thread_ts}.txt", logger
            )
    finally:
        if staging is not None:
            slack_files.cleanup([staging], logger)


@app.event("app_mention")
def handle_app_mention(event, say, client, logger):
    if event.get("bot_id"):
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
    history = get_conversation_history(channel, thread_ts, current_ts, logger)
    reply_with_thinking_indicator(
        question, channel, thread_ts, files, history, say, client, logger
    )


@app.event("message")
def handle_message(event, say, client, logger):
    if event.get("channel_type") != "im":
        return
    if event.get("bot_id") or event.get("subtype") == "bot_message":
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
    history = get_conversation_history(channel, thread_ts, current_ts, logger)
    reply_with_thinking_indicator(
        question, channel, thread_ts, files, history, say, client, logger
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
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

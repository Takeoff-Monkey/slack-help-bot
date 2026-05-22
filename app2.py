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


SELECTOR_SYSTEM = f"""You help an AI Slack bot pick which of Takeoff Monkey's internal-systems documentation files (called "skills") to load when answering a teammate's question.

Each skill is one entry below describing a system, automation, lambda, board automation, or tool. The bot will read the full HTML for whichever skills you pick.

Pick at most {MAX_SKILLS_PER_QUESTION} skill IDs that look directly relevant to the LATEST user message. If the conversation contains prior turns, use them as context for what the user is asking about — follow-ups like "what's wrong with it?" or "what about the other one?" only make sense given prior turns. If the question is conversational, generic, or unrelated to the tech stack (e.g. "hi", "what can you do"), return an empty list — the bot has a fallback for that case.

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


def get_conversation_history(channel: str, thread_ts: str | None, current_ts: str, logger) -> list[dict]:
    """Fetch prior turns from this Slack conversation, oldest first, formatted
    as Claude messages. Excludes the current message (caller appends it).
    Returns [] on any failure — the bot still works without history."""
    try:
        if thread_ts:
            resp = app.client.conversations_replies(
                channel=channel, ts=thread_ts, limit=MAX_HISTORY_MESSAGES + 1
            )
            raw = resp.get("messages", [])
        else:
            resp = app.client.conversations_history(
                channel=channel, limit=MAX_HISTORY_MESSAGES + 1
            )
            raw = list(reversed(resp.get("messages", [])))
    except Exception:
        logger.exception("Failed to fetch conversation history (continuing without context)")
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


def select_skills(question: str, history: list[dict]) -> list[str]:
    messages = normalize_message_history(history + [{"role": "user", "content": question}])
    response = anthropic_client.messages.create(
        model=SELECTOR_MODEL,
        max_tokens=512,
        system=[
            {
                "type": "text",
                "text": SELECTOR_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=messages,
        output_config={
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "skill_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        }
                    },
                    "required": ["skill_ids"],
                    "additionalProperties": False,
                },
            }
        },
    )
    text = next(b.text for b in response.content if b.type == "text")
    payload = json.loads(text)
    picked = payload.get("skill_ids", [])
    valid = [sid for sid in picked if sid in VALID_SKILL_IDS]
    return valid[:MAX_SKILLS_PER_QUESTION]


def answer_question(question: str, skill_ids: list[str], history: list[dict]) -> str:
    if skill_ids:
        bodies = []
        for sid in skill_ids:
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


def respond_to_question(question: str, history: list[dict], logger) -> str:
    question = question.strip()
    if not question:
        return ":wave: Ask me anything about Takeoff Monkey's internal tech stack — Heroku bots, Zapier flows, AWS lambdas, Monday automations, etc."

    try:
        skill_ids = select_skills(question, history)
        logger.info(f"Selected skills: {skill_ids} (history turns: {len(history)})")
        answer = answer_question(question, skill_ids, history)
        if skill_ids:
            if SKILL_DOCS_BASE_URL:
                source_fmts = [
                    f"<{SKILL_DOCS_BASE_URL}/{SKILL_FILE_BY_ID[sid]}|{sid}>"
                    for sid in skill_ids
                ]
            else:
                source_fmts = [f"`{sid}`" for sid in skill_ids]
            answer += "\n\n_Sources: " + ", ".join(source_fmts) + "_"
        if len(answer) > MAX_SLACK_MESSAGE_CHARS:
            answer = answer[: MAX_SLACK_MESSAGE_CHARS - 20] + "… _(truncated)_"
        return answer
    except anthropic.RateLimitError:
        logger.exception("Anthropic rate limit hit")
        return ":hourglass_flowing_sand: I'm getting rate-limited. Try again in a minute."
    except anthropic.APIError as e:
        logger.exception("Anthropic API error")
        return f":warning: I hit an API error: {e.message if hasattr(e, 'message') else e}"
    except Exception as e:
        logger.exception("Unexpected error in respond_to_question")
        return f":x: Something went wrong: {e}"


def strip_mention(text: str) -> str:
    return SLACK_MENTION_RE.sub("", text).strip()


def is_allowed(user_id: str | None) -> bool:
    if not ALLOWED_USERS:
        return True
    return user_id in ALLOWED_USERS


THINKING_PLACEHOLDER = ":hourglass_flowing_sand: _Thinking…_"


def reply_with_thinking_indicator(question, channel, thread_ts, history, say, client, logger):
    """Post a 'Thinking…' placeholder, generate the answer, then edit the
    placeholder to contain the real answer. Falls back to a fresh message
    if chat.update fails for any reason."""
    placeholder = say(text=THINKING_PLACEHOLDER, thread_ts=thread_ts)
    placeholder_ts = placeholder.get("ts") if placeholder else None
    answer = respond_to_question(question, history, logger)
    if not placeholder_ts:
        say(text=answer, thread_ts=thread_ts)
        return
    try:
        client.chat_update(channel=channel, ts=placeholder_ts, text=answer)
    except Exception:
        logger.exception("chat_update failed; posting answer as a new message")
        say(text=answer, thread_ts=thread_ts)


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
    logger.info(f"app_mention question: {question!r}")
    history = get_conversation_history(channel, thread_ts, current_ts, logger)
    reply_with_thinking_indicator(
        question, channel, thread_ts, history, say, client, logger
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
    logger.info(f"DM question: {question!r}")
    history = get_conversation_history(channel, None, current_ts, logger)
    reply_with_thinking_indicator(
        question, channel, None, history, say, client, logger
    )


if __name__ == "__main__":
    mode = (
        f"private mode (allowlist: {sorted(ALLOWED_USERS)})"
        if ALLOWED_USERS
        else "open mode (responds to everyone)"
    )
    print(f"Bot starting — {len(MANIFEST['skills'])} skills loaded, {mode}")
    SocketModeHandler(app, SLACK_APP_TOKEN).start()

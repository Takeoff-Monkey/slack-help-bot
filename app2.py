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

MODEL = "claude-opus-4-7"
MAX_SKILLS_PER_QUESTION = 5
MAX_SLACK_MESSAGE_CHARS = 3800

SKILLS_DIR = Path(__file__).parent / "docs" / "skills"
MANIFEST_PATH = SKILLS_DIR / "_manifest.json"

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
app = App(token=SLACK_BOT_TOKEN)

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

Pick at most {MAX_SKILLS_PER_QUESTION} skill IDs that look directly relevant to the question. Prefer fewer, more-relevant skills over more, weakly-related ones. If the question is conversational, generic, or unrelated to the tech stack (e.g. "hi", "what can you do"), return an empty list — the bot has a fallback for that case.

Available skills:
{INDEX_JSON}"""


ANSWER_SYSTEM = """You are an AI assistant for Takeoff Monkey, a takeoff/estimating services company. You answer teammates' questions about the company's internal tech stack — Heroku bots, Zapier automations, AWS Lambdas, Google Cloud apps, Monday board automations, Chrome extensions, etc.

Ground every answer in the skill documentation provided in the user message. Be concise (Slack-appropriate length — typically 2–8 sentences, or a short bulleted list). When you reference a specific system, name it. If the provided skills don't cover the question, say so directly — don't guess or pad.

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


def select_skills(question: str) -> list[str]:
    response = anthropic_client.messages.create(
        model=MODEL,
        max_tokens=512,
        system=[
            {
                "type": "text",
                "text": SELECTOR_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": question}],
        output_config={
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "skill_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": MAX_SKILLS_PER_QUESTION,
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
    return [sid for sid in picked if sid in VALID_SKILL_IDS]


def answer_question(question: str, skill_ids: list[str]) -> str:
    if skill_ids:
        bodies = []
        for sid in skill_ids:
            body = load_skill_body(sid)
            if body is not None:
                bodies.append(f'<skill id="{sid}">\n{body}\n</skill>')
        skill_context = "\n\n".join(bodies) if bodies else "(no skills loaded)"
    else:
        skill_context = "(no skills selected — the question may be general or unrelated to the tech stack)"

    user_message = (
        f"Teammate's question:\n{question}\n\n"
        f"Relevant skill documentation:\n{skill_context}"
    )

    with anthropic_client.messages.stream(
        model=MODEL,
        max_tokens=2048,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        system=ANSWER_SYSTEM,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        final = stream.get_final_message()

    for block in final.content:
        if block.type == "text":
            return block.text
    return "Sorry, I couldn't generate a response."


def respond_to_question(question: str, logger) -> str:
    question = question.strip()
    if not question:
        return ":wave: Ask me anything about Takeoff Monkey's internal tech stack — Heroku bots, Zapier flows, AWS lambdas, Monday automations, etc."

    try:
        skill_ids = select_skills(question)
        logger.info(f"Selected skills: {skill_ids}")
        answer = answer_question(question, skill_ids)
        if skill_ids:
            footer = "\n\n_Sources: " + ", ".join(f"`{sid}`" for sid in skill_ids) + "_"
            answer += footer
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


@app.event("app_mention")
def handle_app_mention(event, say, logger):
    if event.get("bot_id"):
        return
    user_id = event.get("user")
    if not is_allowed(user_id):
        logger.info(f"Ignoring app_mention from non-allowlisted user {user_id}")
        return
    question = strip_mention(event.get("text", ""))
    thread_ts = event.get("thread_ts") or event.get("ts")
    logger.info(f"app_mention question: {question!r}")
    answer = respond_to_question(question, logger)
    say(text=answer, thread_ts=thread_ts)


@app.event("message")
def handle_message(event, say, logger):
    if event.get("channel_type") != "im":
        return
    if event.get("bot_id") or event.get("subtype") == "bot_message":
        return
    user_id = event.get("user")
    if not is_allowed(user_id):
        logger.info(f"Ignoring DM from non-allowlisted user {user_id}")
        return
    question = strip_mention(event.get("text", ""))
    logger.info(f"DM question: {question!r}")
    answer = respond_to_question(question, logger)
    say(text=answer)


if __name__ == "__main__":
    mode = (
        f"private mode (allowlist: {sorted(ALLOWED_USERS)})"
        if ALLOWED_USERS
        else "open mode (responds to everyone)"
    )
    print(f"Bot starting — {len(MANIFEST['skills'])} skills loaded, {mode}")
    SocketModeHandler(app, SLACK_APP_TOKEN).start()

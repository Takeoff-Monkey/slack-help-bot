"""Slack MCP connector — lets the bot look things up in Slack itself.

Slack hosts an MCP server at https://mcp.slack.com/mcp. We don't speak MCP ourselves:
we hand the server to Anthropic's MCP connector, so Anthropic opens the connection,
lists the tools, and runs the calls server-side inside the same model call we were
already making. That means a question like "when did I send the Arazoza login?" or
"where's the doc for the wall-height tool?" is answered by searching the workspace,
with no client-side tool loop to write and nothing new to deploy.

Two halves are required and the API rejects either one alone (see build()):
  - `mcp_servers=[{type: "url", url, name, authorization_token}]`
  - `tools=[{type: "mcp_toolset", mcp_server_name: <same name>}]`
plus the beta flag `mcp-client-2025-11-20`.

AUTHENTICATION — the part that needs a human.
Slack's MCP server acts *on behalf of a user*, so it wants a user token (`xoxp-…`),
not the bot token. A bot token will be rejected. Getting one means installing the
Slack app with user scopes; see DEPLOY.md → "Slack MCP" for the scope list and the
install steps. Two ways to configure it here:

  SLACK_MCP_USER_TOKENS  JSON map of Slack user ID → that person's own user token,
                         e.g. {"U01ABC":"xoxp-…","U02DEF":"xoxp-…"}. Preferred: each
                         teammate's lookups then see exactly what they can see in
                         Slack, and no more.
  SLACK_MCP_USER_TOKEN   A single fallback token used for anyone not in that map.
                         Simplest to set up, but every search runs as whoever owns
                         it — so the bot can surface a private channel or DM the
                         asker has no access to. Fine for a small team that already
                         shares everything; not fine as a default for a big one.

Set neither and the feature is simply off: nothing else in the bot changes.
"""

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field

# The beta flag the MCP connector rides on. Pinned deliberately — a newer one is a
# deliberate upgrade, not something to pick up silently.
BETA = "mcp-client-2025-11-20"

SERVER_URL = os.environ.get("SLACK_MCP_URL", "https://mcp.slack.com/mcp")
# The name ties the two halves of the request together; it is also what the model sees
# prefixed onto every tool name, so keep it short and obvious.
SERVER_NAME = os.environ.get("SLACK_MCP_SERVER_NAME", "slack")

# Optional belt-and-braces allowlist of MCP tool names. The real enforcement boundary is
# the token's OAuth scopes — Slack refuses anything they don't cover, whatever we ask for
# — so this is off by default. Slack doesn't publish the tool names, but the bot logs
# every one it calls ("slack-mcp: called …"), so an allowlist can be built from real logs
# once you've watched it work.
ALLOWED_TOOLS = [t.strip() for t in os.environ.get("SLACK_MCP_ALLOWED_TOOLS", "").split(",") if t.strip()]

_ENABLED_ENV = os.environ.get("SLACK_MCP_ENABLED", "").strip().lower()

# A token Slack won't accept doesn't degrade gracefully: Anthropic can't complete the
# handshake, so the WHOLE model call comes back 400 — the answer dies with it, not just the
# search. A revoked or expired token would otherwise burn a failed call on every question
# that wanted Slack, forever. So the first such failure parks the connector for a while;
# the bot answers from the docs meanwhile and tries again once the cooldown lapses.
COOLDOWN_SECONDS = int(os.environ.get("SLACK_MCP_COOLDOWN_SECONDS", "600"))

_log = logging.getLogger(__name__)
_lock = threading.Lock()
_paused_until = 0.0


def _load_user_tokens() -> dict[str, str]:
    """Per-user tokens, keyed by Slack user ID. Bad JSON is a config mistake worth
    shouting about, but not worth taking the bot down for — log it and carry on with
    whatever shared token exists."""
    raw = os.environ.get("SLACK_MCP_USER_TOKENS", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        _log.error("SLACK_MCP_USER_TOKENS is not valid JSON — ignoring it. "
                   "Expected a map like {\"U01ABC\": \"xoxp-…\"}.")
        return {}
    if not isinstance(parsed, dict):
        _log.error("SLACK_MCP_USER_TOKENS must be a JSON object of user_id → token; got %s.",
                   type(parsed).__name__)
        return {}
    return {str(k): str(v) for k, v in parsed.items() if v}


USER_TOKENS = _load_user_tokens()
SHARED_TOKEN = os.environ.get("SLACK_MCP_USER_TOKEN", "").strip()


def available() -> bool:
    """Is the connector configured at all? SLACK_MCP_ENABLED=0 forces it off even when
    tokens are present (a quick kill switch that doesn't mean unsetting secrets)."""
    if _ENABLED_ENV in ("0", "false", "no", "off"):
        return False
    return bool(USER_TOKENS or SHARED_TOKEN)


def is_connection_error(err: Exception) -> bool:
    """Did this model call fail because of the MCP server rather than the request itself?

    Anthropic reports a rejected token as a plain 400 invalid_request_error whose message
    names the MCP server ("Authentication error while communicating with MCP server…").
    There's no dedicated error type to match on, so match the message — and err towards
    yes, since every MCP-shaped failure has the same right answer: drop the connector and
    still give the user their answer.
    """
    return "mcp" in str(err).lower()


def pause(reason: str) -> None:
    """Stop attaching the connector for a while after it broke a call."""
    global _paused_until
    with _lock:
        first = time.monotonic() >= _paused_until
        _paused_until = time.monotonic() + COOLDOWN_SECONDS
    if first:
        _log.error("slack-mcp: disabling Slack search for %ds — %s. Questions will be "
                   "answered from the docs alone until then. If this repeats, the user "
                   "token is likely expired or revoked; reinstall the app to reissue it.",
                   COOLDOWN_SECONDS, reason)


def paused() -> bool:
    with _lock:
        return time.monotonic() < _paused_until


def token_for(user_id: str | None) -> tuple[str | None, str]:
    """Resolve the token to search as. Returns (token, source) where source is "user",
    "shared", or "none" — the caller logs it, because "whose eyes is this searching
    with" is the one thing worth being able to tell from a log line."""
    if not available():
        return None, "none"
    if user_id and user_id in USER_TOKENS:
        return USER_TOKENS[user_id], "user"
    if SHARED_TOKEN:
        return SHARED_TOKEN, "shared"
    return None, "none"


@dataclass
class Connector:
    """The request fragments to splice into a model call, plus who they search as."""
    mcp_servers: list = field(default_factory=list)
    tools: list = field(default_factory=list)
    betas: list = field(default_factory=lambda: [BETA])
    token_source: str = "none"

    @property
    def own_token(self) -> bool:
        """True when we're searching as the asker themselves, so results are scoped to
        what they can already see. False means a shared token is standing in for them."""
        return self.token_source == "user"


def build(user_id: str | None) -> Connector | None:
    """The connector for this user, or None if it isn't configured for them.

    Both halves are returned together on purpose: `mcp_servers` without a matching
    `mcp_toolset` in `tools` is a validation error from the API, not a silent no-op.
    """
    if paused():
        return None
    token, source = token_for(user_id)
    if not token:
        return None

    toolset: dict = {"type": "mcp_toolset", "mcp_server_name": SERVER_NAME}
    if ALLOWED_TOOLS:
        # Allowlist mode: everything off by default, named tools switched back on.
        toolset["default_config"] = {"enabled": False}
        toolset["configs"] = {name: {"enabled": True} for name in ALLOWED_TOOLS}

    return Connector(
        mcp_servers=[{
            "type": "url",
            "url": SERVER_URL,
            "name": SERVER_NAME,
            "authorization_token": token,
        }],
        tools=[toolset],
        token_source=source,
    )


def tools_used(message) -> list[str]:
    """Names of the Slack MCP tools the model actually invoked in a response.

    Used for two things: the "Sources: Slack search" footer, and a log line naming each
    call — which is the only way to learn the tool names, since Slack doesn't publish
    them and the connector runs the calls server-side where we never see the request.
    """
    names = []
    for block in getattr(message, "content", None) or []:
        if getattr(block, "type", None) == "mcp_tool_use":
            names.append(getattr(block, "name", "?"))
    return names


def guidance(*, in_dm: bool, own_token: bool, for_action: bool = False) -> str:
    """The system-prompt paragraph that goes with the toolset. Only ever added to a call
    that actually carries the toolset — telling a model about a tool it hasn't got is how
    you get an answer that describes a search it never ran.

    `for_action` swaps the framing for the tool-use loop, where there is no skill
    documentation in the prompt and the point of a search is to find the context needed to
    get the job done, not to write an answer.
    """
    lines = [
        "",
        f"Searching Slack — you have tools (prefixed `{SERVER_NAME}`) that search this Slack "
        "workspace directly: messages, files, channels, canvases and other docs saved in Slack.",
    ]
    if for_action:
        lines += [
            "- Use them to find context the request assumes you already have — which job a "
            "number belongs to, what was agreed in a thread, where a spec or checklist lives.",
            "- They are a lookup, not the work. Search only when you're actually blocked "
            "without it, and get straight back to the job; never let searching replace "
            "producing the file the user asked for.",
            "- You cannot open a file through Slack search. If the file you need is one the "
            "user hasn't attached, use `ask_user` to ask them to attach it.",
        ]
    else:
        lines += [
            "Use them when the answer lives in the workspace rather than in the documentation "
            "above:",
            "- Anything time-bound or conversational: \"when did I send…\", \"who set this up\", "
            "\"what did we decide about…\", \"did anyone report this before\".",
            "- Anything the skill docs don't cover, or cover thinly — a newer internal tool, a "
            "process written up in a canvas, a credential or link someone shared in a thread.",
            "- Read the documentation above FIRST. It is curated and current; Slack search is "
            "for what it doesn't answer. Don't search when the docs already answered it.",
        ]
    lines += [
        "- Search with a couple of well-chosen queries, not a dozen. If two or three come back "
        "empty, say you couldn't find it — a failed search is a real answer, and a much better "
        "one than a guess dressed up as a finding.",
        "- Cite what you find with its Slack permalink so the teammate can open the original. "
        "Say when it was posted and by whom — for \"when did I…\" questions that IS the answer.",
        "- Never present something you found in Slack as documented fact: a teammate's message "
        "from a year ago may be out of date. Attribute it (\"Tommy said in #dev on May 3…\").",
    ]
    if not own_token:
        # A shared token can reach conversations the asker cannot. Re-posting from one into
        # their thread is how a private channel leaks, so make the model check first.
        lines.append(
            "- IMPORTANT: your Slack search runs under a shared account, not the asker's own, "
            "so it can reach private channels and DMs they may have no access to. Before you "
            "quote something, check it is somewhere they can plainly get to (a public channel, "
            "or a conversation they are in). If it isn't, tell them it exists and who to ask "
            "instead of pasting the contents."
        )
    if not in_dm:
        # The original may have been a DM; this thread may be a public channel.
        lines.append(
            "- This thread is not a DM. If what you found is a password, API key, token or "
            "other credential, do NOT repeat it here — link to the original message and say "
            "what it is. Offer to DM it if they want the value itself."
        )
    return "\n".join(lines)


def status_line() -> str:
    """One line for the startup log, so which mode is live is never a mystery."""
    if not available():
        return "Slack MCP: off (no SLACK_MCP_USER_TOKEN / SLACK_MCP_USER_TOKENS set)"
    parts = [f"Slack MCP: on ({SERVER_URL})"]
    if USER_TOKENS:
        parts.append(f"{len(USER_TOKENS)} per-user token(s)")
    parts.append("shared fallback token" if SHARED_TOKEN else "no shared fallback — "
                 "teammates without their own token get no Slack search")
    if ALLOWED_TOOLS:
        parts.append(f"tools limited to {', '.join(ALLOWED_TOOLS)}")
    return " | ".join(parts)

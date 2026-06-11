"""
Live canvas knowledge — pull Slack channel canvases into the bot's knowledge base.

The "tabs" at the top of a Slack channel (e.g. *Automation Guides*, *Misc. Notes*)
are Slack **canvases** — rich docs the team edits in place. They're a living source
of truth, so instead of mirroring them into static HTML skills (which would need
manual upkeep), we read them from Slack directly and treat each canvas as one more
"skill" in the existing selector → answer pipeline.

How it works
------------
1. Discover canvas files in each configured channel via ``files.list(types="canvas",
   channel=…)`` plus the channel's default canvas at ``conversations.info`` →
   ``properties.canvas`` (and any ``properties.tabs`` canvas entries). Both documented
   discovery paths are combined so custom canvas tabs *and* the default channel canvas
   are caught.
2. No Slack Web API method returns a canvas body as JSON, so we download each canvas
   file's ``url_private`` with the bot token (``files:read``) and strip it to text.
3. Cache per ``file_id`` and only re-download when the file's ``updated`` timestamp
   advances — refreshes are cheap. Answering never calls Slack; it reads this
   in-memory cache, so it stays fast.

The cache is rebuilt by a daemon thread every ``interval`` seconds and swapped in
atomically (a single reference assignment), so reader threads never see a half-built
cache and never need a lock.

Setup (must be done in Slack — not in code)
-------------------------------------------
- Bot scopes: ``files:read`` and ``groups:read`` (private channels). Reinstall the app.
- Invite the bot to each channel you want read (``/invite @yourbot``).
- Set ``KNOWLEDGE_CHANNELS=C0XXXX,C0YYYY`` (comma-separated channel IDs).
- Optional: ``CANVAS_SYNC_INTERVAL_SECONDS`` (default 600).
"""

from __future__ import annotations

import html
import logging
import re
import ssl
import threading
import time
import urllib.request

import certifi

log = logging.getLogger("canvas_knowledge")

# Canvas IDs are namespaced with this prefix so they never collide with the
# static HTML skill IDs and are easy to recognise in logs / sources footers.
ID_PREFIX = "canvas-"

# Guardrail: a single canvas should not be allowed to dominate the answer
# prompt. Anything past this many characters is truncated with a marker.
MAX_CANVAS_CHARS = 20000

_SSL = ssl.create_default_context(cafile=certifi.where())

# Atomically-swapped snapshot: {file_id: entry}. Never mutated in place — sync
# builds a fresh dict and rebinds this name (assignment is atomic in CPython).
_CACHE: dict[str, dict] = {}

_STYLE_RE = re.compile(r"<style[^>]*>.*?</style>", re.IGNORECASE | re.DOTALL)
_SCRIPT_RE = re.compile(r"<script[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
_BODY_RE = re.compile(r"<body[^>]*>(.*?)</body>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_BLANKS_RE = re.compile(r"\n{3,}")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


# --------------------------------------------------------------------------- #
# Discovery + download
# --------------------------------------------------------------------------- #
def _discover_canvas_files(client, channel: str) -> dict[str, dict]:
    """Return {file_id: file_object} for every canvas reachable in ``channel``.

    Combines the two documented discovery paths so nothing is missed:
      - files.list(types="canvas", channel=…)  → custom canvas tabs shared in the channel
      - conversations.info → properties.canvas  → the channel's single default canvas
        (and any canvas entries in properties.tabs)
    """
    found: dict[str, dict] = {}

    try:
        resp = client.files_list(channel=channel, types="canvas", count=100)
        for f in resp.get("files", []):
            if f.get("id"):
                found[f["id"]] = f
    except Exception:
        log.exception("canvas sync: files.list failed for channel %s", channel)

    try:
        info = client.conversations_info(channel=channel)
        props = (info.get("channel") or {}).get("properties", {}) or {}
        extra_ids: set[str] = set()

        canvas = props.get("canvas") or {}
        if canvas.get("file_id") and not canvas.get("is_empty"):
            extra_ids.add(canvas["file_id"])

        for tab in props.get("tabs", []) or []:
            if tab.get("type") in ("canvas", "channel_canvas"):
                fid = (tab.get("data") or {}).get("file_id") or tab.get("file_id")
                if fid:
                    extra_ids.add(fid)

        for fid in extra_ids:
            if fid in found:
                continue
            try:
                fobj = client.files_info(file=fid).get("file")
                if fobj:
                    found[fid] = fobj
            except Exception:
                log.exception("canvas sync: files.info failed for %s", fid)
    except Exception:
        log.exception("canvas sync: conversations.info failed for channel %s", channel)

    return found


def _download_canvas_text(token: str, f: dict) -> str:
    """Authenticated download of a canvas body. No JSON method returns canvas
    content, so we GET url_private with the bot token (requires files:read)."""
    url = f.get("url_private_download") or f.get("url_private")
    if not url:
        return ""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, context=_SSL, timeout=30) as r:
        raw = r.read().decode("utf-8", errors="replace")
    return _to_text(raw)


def _to_text(raw: str) -> str:
    """Canvas downloads come back as Slack-flavoured markdown or HTML depending
    on the canvas. Normalise either into clean plain text for the LLM prompt."""
    s = raw.strip()
    if "<" in s and ">" in s:  # looks like HTML — strip it down to text
        s = _STYLE_RE.sub("", s)
        s = _SCRIPT_RE.sub("", s)
        body = _BODY_RE.search(s)
        if body:
            s = body.group(1)
        s = _TAG_RE.sub(" ", s)
        s = html.unescape(s)
    s = _BLANKS_RE.sub("\n\n", s).strip()
    if len(s) > MAX_CANVAS_CHARS:
        s = s[:MAX_CANVAS_CHARS].rstrip() + "\n\n…(canvas truncated)"
    return s


# --------------------------------------------------------------------------- #
# Entry building
# --------------------------------------------------------------------------- #
def _slugify(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-")


def _summarize(text: str) -> str:
    """A short summary for the selector index: first non-empty line as a lead,
    then enough words to convey scope (the selector only needs the gist)."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return "Slack canvas (empty)."
    lead = lines[0]
    rest = " ".join(lines[1:])
    blurb = (lead + " — " + rest) if rest else lead
    words = blurb.split()
    if len(words) > 50:
        blurb = " ".join(words[:50]) + "…"
    return blurb


def _build_entry(f: dict, channel: str, text: str, updated: int) -> dict:
    title = (f.get("title") or "Canvas").strip()
    slug = _slugify(title) or f["id"].lower()
    return {
        "id": ID_PREFIX + slug,
        "file_id": f["id"],
        "channel": channel,
        "name": title,
        "summary": _summarize(text),
        "platform": "Slack Canvas",
        "status": "Live",
        "tags": ["canvas", channel],
        "permalink": f.get("permalink", ""),
        "updated": updated,
        "text": text,
    }


# --------------------------------------------------------------------------- #
# Sync
# --------------------------------------------------------------------------- #
def sync_once(client, token: str, channels: list[str]) -> None:
    """Rebuild the cache from Slack. Unchanged canvases (same ``updated``) reuse
    their cached text — only changed/new canvases are downloaded. Transient
    per-channel or per-file failures keep the prior cached entry rather than
    dropping knowledge."""
    global _CACHE
    old = _CACHE
    new: dict[str, dict] = {}

    for channel in channels:
        try:
            files = _discover_canvas_files(client, channel)
        except Exception:
            log.exception("canvas sync: discovery failed for %s", channel)
            files = {}

        if not files:
            # Discovery turned up nothing (often a transient error) — retain any
            # previously-cached canvases for this channel so we don't go blank.
            for e in old.values():
                if e["channel"] == channel:
                    new[e["file_id"]] = e
            continue

        for fid, f in files.items():
            updated = int(f.get("updated") or f.get("timestamp") or 0)
            prev = old.get(fid)
            if prev and prev["updated"] == updated and prev.get("text"):
                new[fid] = prev  # unchanged — skip the expensive download
                continue
            try:
                text = _download_canvas_text(token, f)
            except Exception:
                log.exception("canvas sync: download failed for %s", fid)
                if prev:
                    new[fid] = prev  # keep last-known-good text
                continue
            if not text:
                continue
            new[fid] = _build_entry(f, channel, text, updated)

    _CACHE = new
    log.info(
        "canvas sync: %d canvas(es) cached across %d channel(s)",
        len(new),
        len(channels),
    )


def _run(client, token: str, channels: list[str], interval: int) -> None:
    while True:
        try:
            sync_once(client, token, channels)
        except Exception:
            log.exception("canvas sync loop error")
        time.sleep(interval)


def start(client, token: str, channels: list[str], interval: int = 600) -> None:
    """Start the background sync thread. Returns immediately; the first sync
    runs a moment later so it never blocks the bot from connecting."""
    t = threading.Thread(
        target=_run,
        args=(client, token, channels, interval),
        daemon=True,
        name="canvas-sync",
    )
    t.start()


# --------------------------------------------------------------------------- #
# Accessors used by the bot (read the atomically-swapped snapshot)
# --------------------------------------------------------------------------- #
def index_entries() -> list[dict]:
    """Selector-facing index rows, same shape as app2's INDEX_FOR_LLM items."""
    return [
        {
            "id": e["id"],
            "name": e["name"],
            "platform": e["platform"],
            "status": e["status"],
            "summary": e["summary"],
            "tags": e["tags"],
        }
        for e in _CACHE.values()
    ]


def valid_ids() -> set[str]:
    return {e["id"] for e in _CACHE.values()}


def get_body(canvas_id: str) -> str | None:
    for e in _CACHE.values():
        if e["id"] == canvas_id:
            return e["text"]
    return None


def get_source(canvas_id: str) -> tuple[str, str] | None:
    """(display name, permalink) for the Sources footer, or None if not a canvas."""
    for e in _CACHE.values():
        if e["id"] == canvas_id:
            return e["name"], e.get("permalink", "")
    return None

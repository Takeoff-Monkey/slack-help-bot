"""Discover and validate the bot's callable tools.

A tool is a directory under tools/ holding:
  - tool.json  — the machine contract (validated against tools/_schema.json's spirit here)
  - README.md  — human/AI prose

At startup the bot scans tools/*/tool.json, validates each, and turns the survivors into
Anthropic tool-use definitions. A malformed manifest is logged and skipped — a broken tool
must never take the Q&A bot down.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("tool_registry")

TOOLS_DIR = Path(__file__).parent / "tools"
_NAME_RE = re.compile(r"^[a-z0-9-]+$")
_REQUIRED = ("name", "description", "when_to_use", "input_schema", "output", "entrypoint")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    when_to_use: str
    input_schema: dict
    accepts: dict
    output: str
    entrypoint: dict
    timeout_seconds: int
    dir: Path
    confirm: bool = False     # tool.json "confirm": true -> never run without the user's OK

    def model_description(self) -> str:
        """The description string handed to Anthropic (when_to_use + output folded in,
        since the tool-use API only has name/description/input_schema)."""
        return f"{self.description}\n\nWhen to use: {self.when_to_use}\n\nOutput: {self.output}"


def _parse(manifest: Path) -> ToolSpec | None:
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:
        log.exception("Tool manifest %s is not valid JSON — skipping", manifest)
        return None

    missing = [k for k in _REQUIRED if k not in data]
    if missing:
        log.error("Tool manifest %s missing keys %s — skipping", manifest, missing)
        return None

    name = data["name"]
    if not isinstance(name, str) or not _NAME_RE.match(name):
        log.error("Tool %s has invalid name %r — skipping", manifest, name)
        return None
    if name != manifest.parent.name:
        log.error("Tool name %r != directory %r — skipping", name, manifest.parent.name)
        return None
    if not isinstance(data.get("input_schema"), dict) or "properties" not in data["input_schema"]:
        log.error("Tool %r has an invalid input_schema — skipping", name)
        return None

    return ToolSpec(
        name=name,
        description=data["description"],
        when_to_use=data["when_to_use"],
        input_schema=data["input_schema"],
        accepts=data.get("accepts") or {},
        output=data["output"],
        entrypoint=data.get("entrypoint") or {},
        timeout_seconds=int(data.get("timeout_seconds") or 300),
        dir=manifest.parent,
        confirm=bool(data.get("confirm")),
    )


def discover_tools(tools_dir: Path = TOOLS_DIR) -> dict[str, ToolSpec]:
    specs: dict[str, ToolSpec] = {}
    if not tools_dir.is_dir():
        log.warning("tools dir %s not found — no tools loaded", tools_dir)
        return specs
    for manifest in sorted(tools_dir.glob("*/tool.json")):
        spec = _parse(manifest)
        if spec is None:
            continue
        if spec.name in specs:
            log.error("Duplicate tool name %r (%s) — skipping", spec.name, manifest)
            continue
        specs[spec.name] = spec
        log.info("Discovered tool %r", spec.name)
    return specs


def _confirmable(schema: dict) -> dict:
    """Add the `user_confirmed` flag to a tool that declares confirm:true, so the model has a
    way to say "the user approved this" — see tool_runner.run_tool, which refuses without it."""
    props = dict(schema.get("properties") or {})
    props["user_confirmed"] = {
        "type": "boolean",
        "default": False,
        "description": (
            "Set true ONLY after the user has explicitly approved this specific action in the "
            "conversation. If you haven't asked yet, use the `ask_user` tool first."
        ),
    }
    return {**schema, "properties": props}


def anthropic_tool_defs(specs: dict[str, ToolSpec]) -> list[dict]:
    """Anthropic tool-use defs for the registered tools. (The run_code sandbox tool is
    appended separately by agent_loop.)"""
    return [
        {
            "name": s.name,
            "description": s.model_description(),
            "input_schema": _confirmable(s.input_schema) if s.confirm else s.input_schema,
        }
        for s in specs.values()
    ]

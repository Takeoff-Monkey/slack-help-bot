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
from dataclasses import dataclass, field
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
    # tool.json "triggers": {"keywords": [...], "action_words": [...], "filename_contains": [...]}
    # -> deterministic routing on top of the model's judgement. A keyword AND (where the tool
    # declares them) an action word make the turn an action; a keyword alone, or a matching
    # attachment name, becomes a routing note in the model's context. See routing_note().
    triggers: dict = field(default_factory=dict)

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
        triggers=_parse_triggers(name, data.get("triggers")),
    )


def _parse_triggers(name: str, raw) -> dict:
    """Validate the optional `triggers` block. `keywords` and `action_words` are case-insensitive
    regexes (a plain phrase is a fine regex); `filename_contains` are lower-cased substrings.
    A malformed entry is logged and dropped — it must never keep the tool itself from loading."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        log.error("Tool %r has a non-object 'triggers' block — ignoring it", name)
        return {}
    out: dict = {}
    for key in ("keywords", "action_words"):
        values = raw.get(key) or []
        if not isinstance(values, list):
            log.error("Tool %r triggers.%s must be a list — ignoring it", name, key)
            continue
        compiled = []
        for v in values:
            if not isinstance(v, str) or not v.strip():
                continue
            try:
                compiled.append(re.compile(v, re.IGNORECASE))
            except re.error as err:
                log.error("Tool %r triggers.%s pattern %r is not a valid regex (%s) — ignoring it", name, key, v, err)
        # Record the key whenever it was DECLARED, even if every pattern was unusable. Dropping
        # it would read as "this tool declares no action words", which means the opposite: a
        # bare keyword would start forcing the action path. Broken patterns must fail closed.
        if compiled or raw.get(key) is not None:
            out[key] = compiled
    values = raw.get("filename_contains") or []
    if not isinstance(values, list):
        log.error("Tool %r triggers.filename_contains must be a list — ignoring it", name)
    else:
        cleaned = [v.strip().lower() for v in values if isinstance(v, str) and v.strip()]
        if cleaned:
            out["filename_contains"] = cleaned
    return out


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


# --- deterministic routing from tool-declared triggers -----------------------------------

def _accepts_file(spec: ToolSpec, f) -> bool:
    """Does this attachment have an extension the tool declares it accepts? (No declaration =
    accepts anything.) Mirrors tool_runner._resolve_input's check, so a routing note never
    points the model at a file the runner would then reject."""
    allowed = [t.lower() for t in (spec.accepts or {}).get("file_types", [])]
    if not allowed:
        return True
    name = getattr(f, "filename", "") or ""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return ext in allowed


def keyword_hits(specs: dict[str, ToolSpec], text: str) -> list[tuple[ToolSpec, str, bool]]:
    """(spec, matched text, with_action) for every tool whose trigger keywords appear in `text`.
    `with_action` is True when the tool declares no action_words, or one of them appears too —
    "format this Arazoza worksheet" as opposed to "what does the Arazoza db worker do?"."""
    t = text or ""
    hits = []
    for spec in specs.values():
        match = None
        for pattern in spec.triggers.get("keywords", []):
            match = pattern.search(t)
            if match:
                break
        if not match:
            continue
        # Key present (even as an empty list, which is what a tool with only unusable patterns
        # leaves behind) means "this tool wants a verb too" — absent means the keyword is enough.
        verbs = spec.triggers.get("action_words")
        with_action = True if verbs is None else any(p.search(t) for p in verbs)
        hits.append((spec, match.group(0), with_action))
    return hits


def action_triggered(specs: dict[str, ToolSpec], text: str) -> bool:
    """Should this message take the action path on its text alone? Only when a tool's keyword
    AND (if it declares any) one of its action words are present — the bare noun is not enough,
    or every question *about* Arazoza would be answered with a demand for a worksheet."""
    return any(with_action for _, _, with_action in keyword_hits(specs, text))


def filename_hits(specs: dict[str, ToolSpec], files) -> list[tuple[ToolSpec, str, list, list]]:
    """(spec, token, matching files the tool accepts, matching files of the wrong type) for every
    tool whose filename trigger matches an attachment. `files` are slack_files.StagedFile-like
    (need .handle and .filename)."""
    hits = []
    for spec in specs.values():
        for token in spec.triggers.get("filename_contains", []):
            matched = [f for f in files if token in (getattr(f, "filename", "") or "").lower()]
            if matched:
                ok = [f for f in matched if _accepts_file(spec, f)]
                bad = [f for f in matched if not _accepts_file(spec, f)]
                hits.append((spec, token, ok, bad))
                break
    return hits


def _names(files) -> str:
    return ", ".join(f"`{f.handle}` ({f.filename})" for f in files)


def routing_note(specs: dict[str, ToolSpec], question: str, files) -> str:
    """A short note for the model's context when a tool's own trigger rules fire this turn.

    The model already sees every tool's when_to_use; this exists for the cases where the tool
    has a hard rule the model kept getting wrong on its own — the file name says which tool
    applies, or the tool needs a file and none of the right kind was attached (so the right
    move is to stop and ask, not to improvise with run_code or call the tool on a PNG). A hit
    with an action word is phrased as an instruction; a bare mention is phrased as a hint the
    model may set aside if the user is plainly asking something else. Returns '' when nothing
    fires."""
    files = list(files or [])
    by_kw = {spec.name: (spec, text, act) for spec, text, act in keyword_hits(specs, question)}
    by_fn = {spec.name: (spec, token, ok, bad) for spec, token, ok, bad in filename_hits(specs, files)}
    lines = []
    for name in dict.fromkeys([*by_fn, *by_kw]):
        spec = (by_fn.get(name) or by_kw.get(name))[0]
        types = ", ".join(f".{t}" for t in (spec.accepts or {}).get("file_types", [])) or "a file"
        needs_file = "input_file" in (spec.input_schema.get("required") or [])
        accepted = [f for f in files if _accepts_file(spec, f)]
        ok = by_fn[name][2] if name in by_fn else []
        firm = name in by_kw and by_kw[name][2]
        why = []
        if name in by_fn:
            why.append(f"an attachment's name matches its filename rule ('{by_fn[name][1]}')")
        if name in by_kw:
            why.append(f"the request says '{by_kw[name][1]}'")
        why_txt = " and ".join(why)
        # One call per file is the contract (accepts.max_files); with several candidates the
        # model must not silently pick one.
        one_only = (spec.accepts or {}).get("max_files") == 1
        pick = (f" There is more than one, and `{spec.name}` takes a single file per call: ask "
                f"with `ask_user` which one they mean." if one_only and len(ok) > 1 else "")
        if ok:
            lines.append(
                f"Routing note: {_names(ok)} is what the `{spec.name}` tool is for ({why_txt}). "
                + (f"Call `{spec.name}` on it — never run_code for that." if firm else
                   f"If the user wants it processed the way that tool does, call `{spec.name}` on it — never "
                   f"run_code for that. If they are clearly asking for something else, ignore this note.")
                + pick
            )
        elif needs_file and not accepted:
            wrong = (f" The attached file(s) — {_names(files)} — are not {types}, so the tool cannot take them."
                     if files else "")
            lines.append(
                f"Routing note: {why_txt}, which is what the `{spec.name}` tool is for, but no {types} file is "
                f"attached to this message or earlier in the thread.{wrong} If that work is what they want, do "
                f"not start anything and do not use run_code: use `ask_user` to ask them to upload the "
                f"{types} worksheet first."
            )
        elif accepted:
            lines.append(
                f"Routing note: {why_txt}, which is what the `{spec.name}` tool is for. "
                + (f"Use it on the attached file ({_names(accepted)}) rather than run_code." if firm else
                   f"If that is what they want, use it on the attached file ({_names(accepted)}) rather than "
                   f"run_code; if they are asking something else, ignore this note.")
            )
        else:
            # The tool takes no file (or none of its own), so there is nothing to ask for.
            lines.append(
                f"Routing note: {why_txt}, which is what the `{spec.name}` tool is for. "
                f"If that is what they want, use `{spec.name}` rather than run_code."
            )
    return "\n".join(lines)

"""Spell-check the botanical (Latin) names in item descriptions — and nothing else.

Estimators type plant names fast, so "Quercus virginana" and "Sabal palmeto" turn up in OST
output. Those are worth fixing: the Latin name is how the item gets matched downstream.
Everything else on the sheet — group names, common names, cultivar names, sizes,
abbreviations — stays exactly as the estimator wrote it (TODO #2: the earlier passes were "too
generous").

Claude (Haiku 5.5) proposes the corrections; every proposal is then checked here and dropped
unless it is plainly a spelling fix of a Latin word:
  - same number of words, and at most two of them changed;
  - every changed word is alphabetic, at least 4 letters, and close to the original
    (difflib ratio >= 0.75), and not inside quotes (cultivars like 'Savannah' are left alone);
  - the estimator's capitalisation is re-applied, so the fix never re-cases anything.

No API key (local tests, a Lambda deployed without one) or any API failure → no changes,
and the formatter says the check was skipped.
"""

from __future__ import annotations

import difflib
import json
import os
import re

MODEL = os.getenv("LATIN_MODEL", "claude-haiku-5-5")
EFFORT = os.getenv("LATIN_EFFORT", "medium")
MAX_CHANGED_WORDS = 2
MIN_RATIO = 0.75
MIN_WORD_LEN = 4

SYSTEM = """You proofread botanical (Latin) plant names in a landscape takeoff list.

Each item is one line from an estimator's worksheet. Some items are botanical names (genus,
species, maybe a variety or a cultivar in quotes), some are common names, some are materials
(mulch, sod, soil, edging, rock).

Return a correction ONLY when a genus or species epithet is misspelled — e.g. "Quercus
virginana" -> "Quercus virginiana", "Podocarpus macrophylus" -> "Podocarpus macrophyllus".

Never change:
- common (English) names, material names, or group names, even if they look misspelled;
- cultivar or trade names (usually in quotes), abbreviations, sizes, numbers, punctuation;
- capitalisation or spacing;
- a name that is already a valid spelling, including accepted synonyms and older names.
If you are not sure a word is a misspelling, leave it alone. Most items need no change.

Return the full corrected item text for each item you change, and omit every other item."""

SCHEMA = {
    "type": "object",
    "properties": {
        "corrections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "corrected": {"type": "string"},
                },
                "required": ["id", "corrected"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["corrections"],
    "additionalProperties": False,
}

_WORD = re.compile(r"^(\W*)(.*?)(\W*)$", re.S)
_QUOTED = re.compile(r"(['\"‘’“”]).*?\1|['\"‘’“”][^'\"‘’“”]*$")


def available() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def _match_case(new: str, old: str) -> str:
    if old.isupper():
        return new.upper()
    if old.islower():
        return new.lower()
    if old[:1].isupper() and old[1:].islower():
        return new[:1].upper() + new[1:].lower()
    return new


def _quoted_spans(text: str) -> list[tuple[int, int]]:
    return [m.span() for m in _QUOTED.finditer(text)]


def accept(original: str, proposed: str) -> str | None:
    """The proposal with the original's casing restored, if it is only a Latin spelling fix;
    otherwise None."""
    if not proposed or proposed == original:
        return None
    old_words = list(re.finditer(r"\S+", original))
    new_words = proposed.split()
    if len(old_words) != len(new_words):
        return None
    quoted = _quoted_spans(original)
    out, changed = [], 0
    for m, new in zip(old_words, new_words):
        old = m.group(0)
        if old.lower() == new.lower():
            out.append(old)
            continue
        changed += 1
        if any(a <= m.start() < b for a, b in quoted):
            return None
        (lead_o, core_old, tail_o), (lead_n, core_new, tail_n) = _WORD.match(old).groups(), _WORD.match(new).groups()
        if (lead_o, tail_o) != (lead_n, tail_n):
            return None                        # punctuation moved
        if not (core_old.isalpha() and core_new.isalpha()) or len(core_old) < MIN_WORD_LEN:
            return None
        if difflib.SequenceMatcher(None, core_old.lower(), core_new.lower()).ratio() < MIN_RATIO:
            return None
        out.append(lead_o + _match_case(core_new, core_old) + tail_o)
    if not changed or changed > MAX_CHANGED_WORDS:
        return None
    # Rebuild with the original spacing between words.
    result, last = [], 0
    for m, word in zip(old_words, out):
        result.append(original[last:m.start()])
        result.append(word)
        last = m.end()
    result.append(original[last:])
    fixed = "".join(result)
    return fixed if fixed != original else None


def _ask_claude(items: list[str]) -> list[dict]:
    import anthropic                      # only needed when the check actually runs

    client = anthropic.Anthropic()
    listing = "\n".join(f"{i}: {t}" for i, t in enumerate(items))
    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": SCHEMA}},
        system=SYSTEM,
        messages=[{"role": "user", "content": f"Items (id: text):\n{listing}"}],
    )
    if response.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(f"the spell check stopped early ({response.stop_reason})")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text).get("corrections", [])


def fix_latin_names(texts: list[str], ask=_ask_claude) -> dict[str, str]:
    """original text -> corrected text, for the texts whose Latin name was misspelled.
    `ask` is injectable for tests. Raises if the model call fails (the caller reports it)."""
    unique = sorted({t for t in texts if t and re.search(r"[A-Za-z]{4}", t)})
    if not unique:
        return {}
    fixes: dict[str, str] = {}
    for c in ask(unique):
        i = c.get("id")
        if not isinstance(i, int) or not 0 <= i < len(unique):
            continue
        fixed = accept(unique[i], str(c.get("corrected", "")))
        if fixed:
            fixes[unique[i]] = fixed
    return fixes

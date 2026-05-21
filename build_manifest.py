"""Build _manifest.json by parsing meta tags from every skill HTML file.

Run this whenever you add, remove, or rename skills in docs/skills/.
The bot (app2.py) reads _manifest.json at startup as its index for retrieval.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

SKILLS_DIR = Path(__file__).parent / "docs" / "skills"
MANIFEST_PATH = SKILLS_DIR / "_manifest.json"

META_RE = re.compile(
    r'<meta\s+name=["\']([^"\']+)["\']\s+content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)
SUMMARY_RE = re.compile(
    r'<p\s+class=["\']summary["\']>(.*?)</p>', re.IGNORECASE | re.DOTALL
)
H1_RE = re.compile(r"<h1>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
TAG_STRIP_RE = re.compile(r"<[^>]+>")


def clean(text: str) -> str:
    return TAG_STRIP_RE.sub("", text).strip()


def parse_skill(path: Path) -> dict | None:
    html = path.read_text(encoding="utf-8")

    metas = {name.lower(): value for name, value in META_RE.findall(html)}
    skill_id = metas.get("skill-id")
    if not skill_id:
        return None

    title_match = TITLE_RE.search(html)
    title = clean(title_match.group(1)) if title_match else skill_id
    title = title.replace(" — Takeoff Monkey Skill", "").strip()

    h1_match = H1_RE.search(html)
    name = clean(h1_match.group(1)) if h1_match else title

    summary_match = SUMMARY_RE.search(html)
    summary = clean(summary_match.group(1)) if summary_match else ""

    tags_raw = metas.get("skill-tags", "")
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()]

    entry = {
        "id": skill_id,
        "file": path.name,
        "name": name,
        "title": title,
        "summary": summary,
        "platform": metas.get("skill-platform", ""),
        "status": metas.get("skill-status", ""),
        "owner": metas.get("skill-owner", ""),
        "tags": tags,
    }
    if "automation-id" in metas:
        entry["automation_id"] = metas["automation-id"]
    if "last-updated" in metas:
        entry["last_updated"] = metas["last-updated"]
    return entry


def main() -> None:
    skill_files = sorted(
        p for p in SKILLS_DIR.glob("*.html")
        if not p.name.startswith("_") and p.name != "index.html"
    )

    skills = []
    skipped = []
    for path in skill_files:
        entry = parse_skill(path)
        if entry:
            skills.append(entry)
        else:
            skipped.append(path.name)

    by_platform: dict[str, int] = {}
    for s in skills:
        by_platform[s["platform"]] = by_platform.get(s["platform"], 0) + 1

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(skills),
        "by_platform": dict(sorted(by_platform.items())),
        "skills": skills,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"Wrote {MANIFEST_PATH} — {len(skills)} skills indexed.")
    for platform, n in sorted(by_platform.items()):
        print(f"  {platform}: {n}")
    if skipped:
        print(f"Skipped (no skill-id meta): {skipped}")


if __name__ == "__main__":
    main()

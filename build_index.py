"""Render docs/skills/index.html — a human-browsable landing page.

Reads _manifest.json (must be built first with build_manifest.py) and
groups all skills by platform with clickable links.
"""

import json
import html
from pathlib import Path

SKILLS_DIR = Path(__file__).parent / "docs" / "skills"
MANIFEST_PATH = SKILLS_DIR / "_manifest.json"
INDEX_PATH = SKILLS_DIR / "index.html"


STATUS_CLASS = {
    "Live": "status-live",
    "In Progress": "status-progress",
}


def render() -> str:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    groups: dict[str, list[dict]] = {}
    for skill in manifest["skills"]:
        groups.setdefault(skill["platform"], []).append(skill)
    for skills in groups.values():
        skills.sort(key=lambda s: s["name"].lower())

    rows = []
    for platform in sorted(groups.keys()):
        rows.append(f'  <section><h2>{html.escape(platform)} <span class="count">({len(groups[platform])})</span></h2>')
        rows.append('  <ul class="skills">')
        for s in groups[platform]:
            status_cls = STATUS_CLASS.get(s["status"], "status-unknown")
            rows.append(
                f'    <li><a href="{html.escape(s["file"])}">{html.escape(s["name"])}</a>'
                f' <span class="{status_cls}">{html.escape(s["status"] or "—")}</span>'
                f'<div class="summary">{html.escape(s["summary"])}</div></li>'
            )
        rows.append("  </ul></section>")
    sections = "\n".join(rows)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Takeoff Monkey — Skills Index</title>
  <style>
    body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 960px; margin: 2rem auto; padding: 0 1rem; color: #1f2328; line-height: 1.5; }}
    h1 {{ border-bottom: 1px solid #d0d7de; padding-bottom: .3rem; }}
    h2 {{ margin-top: 2rem; border-bottom: 1px solid #eaecef; padding-bottom: .2rem; }}
    .count {{ color: #656d76; font-weight: 400; font-size: .85em; }}
    .skills {{ list-style: none; padding: 0; }}
    .skills li {{ background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 6px; padding: .6rem .9rem; margin-bottom: .5rem; }}
    .skills a {{ color: #0969da; font-weight: 600; text-decoration: none; }}
    .skills a:hover {{ text-decoration: underline; }}
    .summary {{ color: #424a53; font-size: .9rem; margin-top: .2rem; }}
    .status-live {{ color: #1a7f37; font-weight: 600; font-size: .8em; margin-left: .5rem; }}
    .status-progress {{ color: #9a6700; font-weight: 600; font-size: .8em; margin-left: .5rem; }}
    .status-unknown {{ color: #656d76; font-weight: 600; font-size: .8em; margin-left: .5rem; }}
    .stats {{ background: #ddf4ff; border: 1px solid #54aeff; border-radius: 6px; padding: .8rem 1rem; margin: 1rem 0; }}
  </style>
</head>
<body>
  <h1>Takeoff Monkey — Skills Index</h1>
  <p>This is the human-browsable index of every system, automation, and tool documented for the AI Slack bot. Each entry is also machine-readable in <code>_manifest.json</code>.</p>
  <div class="stats"><strong>{manifest["count"]} skills</strong> indexed across {len(groups)} platforms. Generated: {manifest["generated_at"]}.</div>
{sections}
</body>
</html>
"""


def main() -> None:
    INDEX_PATH.write_text(render(), encoding="utf-8")
    print(f"Wrote {INDEX_PATH}")


if __name__ == "__main__":
    main()

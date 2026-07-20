# wall-height-calculator

Calculates **retaining-wall heights and areas** from the TW/BW (top-of-wall /
bottom-of-wall) elevation markup **annotations** on a plan/site PDF, and writes them to an
Excel workbook — one row per wall.

This is a **bot-callable tool**: the Takeoff Monkey Slack bot ([app2.py](../../app2.py))
discovers it via [`tool.json`](tool.json) and runs it when a teammate attaches a marked-up
PDF and asks to calculate wall heights / areas. It is the same tool as the Streamlit "Wall
Height Calculator" — the original UI script is kept alongside as
[`wall-height-calculator.py`](wall-height-calculator.py) purely for reference; all of its
logic (minus Streamlit/auth) lives in [`wall_heights.py`](wall_heights.py), where the
wall-detection algorithm is a **verbatim port** so results are identical.

---

## What it does

1. Flattens every annotation's text `content` across the PDF (skipping any `skip_pages`).
2. Walks that list using wall-ID markups as anchors, reading the neighbouring TW/BW
   elevation callouts and the wall length.
3. For each wall computes:
   - `First Input Top` / `First Input Bottom` — the two paired top−bottom differences,
   - `Wall Height in Feet (Calculated)` — their average,
   - `Wall Height (Roundup)` — the calculated height rounded per `project_type`,
   - `Wall Area (SF)` — rounded height × length.

Output: one `.xlsx` workbook (sheet `Wall Data`).

### Project type (rounding)

`project_type` controls how the calculated height is rounded up:

| project_type | rounds up to |
|---|---|
| `Single-Family` (default) | nearest **0.1 ft** |
| `Multi-Family/Commercial` | nearest **0.5 ft** |

If the user doesn't specify, the bot omits the field and the tool defaults to
`Single-Family` (matching the Streamlit radio's default). The value is normalized leniently
(`normalize_project_type`), so `"multifamily"`, `"commercial"`, `"single family"`, etc. all
map correctly.

## When the bot should use it

> Attach a marked-up plan/site PDF whose annotations carry wall IDs with TW/BW elevation
> callouts and ask to calculate wall heights, wall areas, or build a wall takeoff.

It is **not** for PDFs without wall elevation markups, **not** for extracting
schedule/legend tables (that's [`schedule-extractor`](../schedule-extractor/)), and **not**
for keyword highlighting (that's [`bid-scanner`](../bid-scanner/)).

---

## How the bot calls it (contract)

The bot resolves the user's attachment to a file handle (e.g. `file_1`) and invokes the
tool via the backend selected by `TOOL_BACKEND`:

- **local** → runs [`run.py`](run.py) as a subprocess in this directory's venv.
- **lambda** → invokes the `tm-tool-wall-height-calculator` Lambda (see [`lambda/`](lambda/)).

Both honor the same JSON contract:

**Input (stdin / event):**
```json
{
  "input":      { "input_file": "file_1", "project_type": "Single-Family", "skip_pages": [] },
  "input_path": "/abs/path/to/staged.pdf",
  "work_dir":   "/abs/path/to/work-<uuid>",
  "backend":    "local"
}
```

**Output (`work_dir/result.json`):**
```json
{
  "status": "ok",
  "summary": "Calculated heights for 12 wall(s) (of 14 detected) using Single-Family rounding into site-wall-heights.xlsx.",
  "artifacts": [
    { "kind": "xlsx", "ref": "/abs/.../site-wall-heights.xlsx", "filename": "site-wall-heights.xlsx", "title": "Wall heights" }
  ],
  "error": null
}
```

If no wall elevation markups are found, `artifacts` is empty and the summary says so.

### Configurable inputs (from `tool.json`)
| field | type | default | meaning |
|---|---|---|---|
| `input_file` | string (handle) | — (required) | which attached PDF to process |
| `project_type` | enum | `Single-Family` | rounding mode (see table above); omit to default |
| `skip_pages` | integer[] | `[]` | zero-based page indices to skip (e.g. a cover sheet) |

The bot processes **one** PDF per call (`accepts.max_files: 1`), matching the other tools.
If several PDFs are attached, the model calls the tool once per file.

---

## Run it standalone (terminal)

```bash
cd tools/wall-height-calculator
./setup.sh                       # one-time: builds .venv from requirements.txt
# Default (Single-Family) rounding:
echo '{"input":{"skip_pages":[]},"input_path":"/path/to/in.pdf","work_dir":"."}' \
  | .venv/bin/python run.py
# Multi-Family/Commercial rounding:
echo '{"input":{"project_type":"Multi-Family/Commercial"},"input_path":"/path/to/in.pdf","work_dir":"."}' \
  | .venv/bin/python run.py
```

## Secrets & deployment

This tool needs **no secrets** — it's pure PyMuPDF + pandas, no cloud services for the
calculation itself. Under the `lambda` backend the function only needs read/write on the
scratch S3 bucket (files round-trip through S3); it uses its execution role, no static
keys. See [`lambda/`](lambda/) and the deploy steps in [`../README.md`](../README.md).

Region convention: `us-east-1`. Owner: Konur Papageorgiou. General escalation: Tommy Lather.

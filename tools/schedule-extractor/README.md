# schedule-extractor

Extracts **plant legends and material schedules** from a construction/landscape PDF and
writes them to an Excel workbook — one sheet per detected schedule.

This is a **bot-callable tool**: the Takeoff Monkey Slack bot ([app2.py](../../app2.py))
discovers it via [`tool.json`](tool.json) and runs it when a teammate attaches a PDF and
asks to pull schedules/legends out of it. It can also be run standalone from the terminal.

---

## What it does

For each page of the PDF it:
1. Finds schedule/legend anchors (`SCHED_KEYWORDS = ["drawing title", "legend"]`) paired
   with a nearby table header (`HEAD_KEYWORDS = ["qty", "quantity", "symbol", "key"]`).
2. Computes a bounding box around each schedule (handles rotated/landscape title blocks).
3. Renders that region at 300 DPI and sends it to **AWS Textract** (TABLES) for OCR/table
   recovery.
4. Cleans the table into a pandas DataFrame and writes it to a sheet named
   `Page N, Schedule M`.

Output: one `.xlsx` workbook. Optionally (`GPT_CLEANUP`) an OpenAI pass tidies typos and
splits common/botanical plant names — off by default.

## When the bot should use it

> Attach a site/landscape/construction PDF and ask to extract the plant legends, material
> schedules, or planting tables into a spreadsheet.

It is **not** for answering questions and **not** for PDFs with no schedule/legend tables.
Anything outside this scope (e.g. "highlight every 'landscape'") is handled by the bot's
sandboxed code fallback, not this tool.

---

## How the bot calls it (contract)

The bot resolves the user's attachment to a file handle (e.g. `file_1`) and invokes the
tool via the backend selected by `TOOL_BACKEND`:

- **local** → runs [`run.py`](run.py) as a subprocess in this directory's venv.
- **lambda** → invokes the `tm-tool-schedule-extractor` Lambda (see [`lambda/`](lambda/)).

Both honor the same JSON contract:

**Input (stdin / event):**
```json
{
  "input":      { "input_file": "file_1", "ignore_first_column": true, "skip_pages": [] },
  "input_path": "/abs/path/to/staged.pdf",
  "work_dir":   "/abs/path/to/run-<uuid>",
  "backend":    "local"
}
```

**Output (`work_dir/result.json`):**
```json
{
  "status": "ok",
  "summary": "Extracted 3 schedules across 2 pages into site.xlsx.",
  "artifacts": [
    { "kind": "xlsx", "ref": "/abs/.../site.xlsx", "filename": "site.xlsx", "title": "Extracted schedules" }
  ],
  "error": null
}
```

### Configurable inputs (from `tool.json`)
| field | type | default | meaning |
|---|---|---|---|
| `input_file` | string (handle) | — (required) | which attached PDF to process |
| `ignore_first_column` | boolean | `true` | drop the leading symbol/code column |
| `skip_pages` | integer[] | `[]` | zero-based page indices to skip (e.g. cover sheets) |

Other knobs (`GPT_CLEANUP`, `SCHED_KEYWORDS`, `MIN_SCHED_SIZE`, …) live as module constants
in [`main2.py`](main2.py) and are not exposed to the bot.

---

## Run it standalone (terminal)

```bash
cd tools/schedule-extractor
./setup.sh                       # one-time: builds .venv from requirements.txt
# Single file (the bot's path):
echo '{"input":{"ignore_first_column":true,"skip_pages":[]},"input_path":"/path/to/in.pdf","work_dir":"."}' \
  | .venv/bin/python run.py
# S3 batch (legacy): processes every PDF under s3://<BUCKET>/input/, writes to /output/
.venv/bin/python main2.py
```

## Secrets & deployment

`.env` (gitignored — never commit it) provides:
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION` (for Textract/S3)
- `OPENAI_API_KEY` (only used when `GPT_CLEANUP=True`)

> ⚠️ Rotate any keys that have been shared or committed. In the Lambda, Textract/S3 should
> use the **execution IAM role** (no static keys); only `OPENAI_API_KEY` needs to be set as
> an encrypted Lambda env var or pulled from Secrets Manager.

Region convention: `us-east-1` (matches the other Takeoff Monkey PDF pipelines).
Owner: Konur Papageorgiou. General escalation: Tommy Lather.

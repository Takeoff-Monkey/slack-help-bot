# schedule-extractor

Extracts **plant legends and material schedules** from construction/landscape drawings and
writes them to an Excel workbook — one sheet per detected schedule.

Takes a **PDF**, or **images** of a drawing sheet (photos, screenshots) — up to ten at a
time, all landing in one workbook.

This is a **bot-callable tool**: the Takeoff Monkey Slack bot ([app2.py](../../app2.py))
discovers it via [`tool.json`](tool.json) and runs it when a teammate attaches drawings and
asks to pull schedules/legends out of them. It can also be run standalone from the terminal.

---

## What it does

For each page (a PDF page, or one image) it:
1. Finds schedule/legend anchors (`SCHED_KEYWORDS = ["schedule", "legend"]`) paired with a
   nearby table header (`HEAD_KEYWORDS = ["qty", "quantity", "symbol", "key"]`) — from the
   PDF's text layer where there is one, otherwise from a whole-page **AWS Textract** OCR pass.
2. Computes a bounding box around each schedule (handles rotated/landscape title blocks).
3. Renders that region and sends it to **AWS Textract** (TABLES) for table recovery.
4. Cleans the table into a pandas DataFrame and writes it to its own sheet.

Output: one `.xlsx` workbook. Optionally (`AI_CLEANUP`) a Claude pass tidies typos and splits
common/botanical plant names — off by default.

### How images are handled

An image is wrapped in a one-page PDF at **1 point = 1 pixel** (`image_page_pdf`) and then
runs the *same* path as a PDF page, so both kinds of attachment behave the same way:

- **A photo or screenshot of a whole sheet** — the schedule is found by OCR and **cropped out**
  of the frame before table recovery, exactly as it is on a drawing page. The plan, title
  block and notes around it are left behind.
- **An image of just the schedule** — a user-made crop has no "PLANT SCHEDULE" title left to
  anchor on, so when nothing is detected the whole frame is taken as the table. This fallback
  is for images only: doing it per page on a 60-sheet PDF would return junk for most of them.
- **Several images at once** — each becomes its own sheet, named after the file it came from
  (`north beds, Schedule 1`), so a teammate can tell which photo a sheet is from. One
  unreadable attachment is reported in the summary and does not lose the others' schedules.

Photos get an EXIF-orientation fix (a sideways phone photo would otherwise defeat the
row/column geometry), and everything on its way to Textract is fitted to its size ceiling
(`MAX_OCR_SIDE`, `MAX_OCR_BYTES`) — a 12 MP photo is otherwise rejected outright. Image pages
render at their true resolution (`NATIVE_DPI`); upscaling a raster recovers no detail.

HEIC/HEIF are **not** accepted (no decoder here) — ask for a JPEG or PNG.

## When the bot should use it

> Attach a site/landscape/construction PDF, or images of a sheet or a schedule table, and ask
> to extract the plant legends, material schedules, or planting tables into a spreadsheet.

It is **not** for answering questions and **not** for drawings with no schedule/legend tables.
Anything outside this scope (e.g. "highlight every 'landscape'", or OCR of some *other* kind
of document) is handled by the bot's sandboxed code fallback, not this tool.

---

## How the bot calls it (contract)

The bot resolves the user's attachments to file handles (e.g. `file_1`) and invokes the
tool via the backend selected by `TOOL_BACKEND`:

- **local** → runs [`run.py`](run.py) as a subprocess in this directory's venv.
- **lambda** → invokes the `tm-tool-schedule-extractor` Lambda (see [`lambda/`](lambda/)).

Both honor the same JSON contract:

**Input (stdin / event):**
```json
{
  "input":       { "input_files": ["file_1", "file_2"], "ignore_first_column": true, "skip_pages": [] },
  "input_path":  "/abs/path/to/staged.jpg",
  "input_paths": ["/abs/path/to/file_1-north beds.jpg", "/abs/path/to/file_2-south beds.jpg"],
  "work_dir":    "/abs/path/to/run-<uuid>",
  "backend":     "local"
}
```
`input_paths` is every file the call resolved; `input_path` is the first of them (the
single-file shorthand every other tool uses). Either is accepted.

**Output (`work_dir/result.json`):**
```json
{
  "status": "ok",
  "summary": "Extracted 3 schedules from 3 images into schedules.xlsx.",
  "artifacts": [
    { "kind": "xlsx", "ref": "/abs/.../schedules.xlsx", "filename": "schedules.xlsx", "title": "Extracted schedules" }
  ],
  "error": null
}
```

### Configurable inputs (from `tool.json`)
| field | type | default | meaning |
|---|---|---|---|
| `input_files` | string[] (handles) | — (required) | which attached files to process: one PDF, or up to 10 images |
| `input_file` | string (handle) | — | single-file shorthand for `input_files` |
| `ignore_first_column` | boolean | `true` | drop the leading symbol/code column |
| `skip_pages` | integer[] | `[]` | zero-based PDF page indices to skip (e.g. cover sheets); ignored for images |

Other knobs (`AI_CLEANUP`, `SCHED_KEYWORDS`, `MIN_SCHED_SIZE`, `CROP_DPI`, `MAX_OCR_SIDE`, …)
live as module constants in [`main2.py`](main2.py) and are not exposed to the bot.

The workbook is named after the file when there's one, `schedules.xlsx` when there are
several. Sheets are `Page N, Schedule M` for a lone PDF (unchanged), and named after the
source image or page otherwise.

---

## Run it standalone (terminal)

```bash
cd tools/schedule-extractor
./setup.sh                       # one-time: builds .venv from requirements.txt
# One PDF (the bot's original path):
echo '{"input":{"ignore_first_column":true,"skip_pages":[]},"input_path":"/path/to/in.pdf","work_dir":"."}' \
  | .venv/bin/python run.py
# Several schedule photos:
echo '{"input":{},"input_paths":["/path/a.jpg","/path/b.jpg"],"work_dir":"."}' \
  | .venv/bin/python run.py
# S3 batch (legacy): processes every PDF under s3://<BUCKET>/input/, writes to /output/
.venv/bin/python main2.py
```

## Secrets & deployment

`.env` (gitignored — never commit it) provides:
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION` (for Textract/S3)
- `ANTHROPIC_API_KEY` (only used when `AI_CLEANUP=True`)

> ⚠️ Rotate any keys that have been shared or committed. In the Lambda, Textract/S3 should
> use the **execution IAM role** (no static keys); only `ANTHROPIC_API_KEY` needs to be set as
> an encrypted Lambda env var or pulled from Secrets Manager.

> ⚠️ The deployed `tm-tool-schedule-extractor` Lambda predates image support. Until it is
> redeployed (`cd lambda && SCRATCH_BUCKET=... ./deploy.sh`), images only work on
> `TOOL_BACKEND=local`. See the drift note in [DEPLOY.md](../../DEPLOY.md).

Region convention: `us-east-1` (matches the other Takeoff Monkey PDF pipelines).
Owner: Konur Papageorgiou. General escalation: Tommy Lather.

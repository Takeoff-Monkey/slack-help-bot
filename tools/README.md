# Bot tools & the `run_code` sandbox

This directory holds the **tools** the Slack bot ([../app2.py](../app2.py)) can run on demand.
When a teammate attaches a file and asks for something, the bot reaches for the most fitting
specialized tool first; anything no tool covers, it attempts itself with the sandboxed
`run_code` fallback. See the design notes below before adding a tool.

### The `run_code` sandbox environments
The `run_code` fallback ([../sandbox.py](../sandbox.py), deps in [../sandbox/](../sandbox/)) runs
model-written Python in one of two environments, chosen by the model via the tool's `environment`
field:
- **`default`** (extended toolkit) — Tesseract OCR (`pytesseract` + the `tesseract` binary),
  `cv2` (opencv-headless, image preprocessing), `fitz` (PyMuPDF), `pdfplumber`, `pdf2image`,
  `pandas`/`numpy`, `PIL`, `openpyxl`/`xlsxwriter`, `python-docx`, `python-pptx`, `reportlab`,
  `tabulate`. Fast; the model uses it first. Needs the `tesseract` and poppler `pdftoppm` system
  binaries (already present locally; installed via SPAL in the Lambda image).
- **`neural_ocr`** — everything in `default` PLUS RapidOCR (`rapidocr_onnxruntime`), a neural OCR
  engine with **bundled, offline** ONNX models that is far more accurate on messy/rotated/scanned
  images. Heavier + slower cold start, so the bot escalates to it only when `default` Tesseract
  output looks poor. Local = `sandbox/.venv-ocr`; Lambda = `tm-sandbox-runcode-ocr`.

## Anatomy of a tool

```
tools/<name>/
  tool.json          ← machine contract: name, description, when_to_use, input_schema, entrypoint,
                       and optionally "confirm": true (see below) and "triggers" (see below)
  README.md          ← human/AI prose
  run.py             ← local entrypoint (stdin JSON in → result.json out)
  main*.py           ← the actual logic
  requirements.txt   ← the tool's own deps (installed into .venv, NOT the bot's env)
  setup.sh           ← builds .venv from requirements.txt
  .venv/             ← the tool's isolated interpreter (gitignored)
  .env               ← the tool's secrets (gitignored)
  lambda/            ← Dockerfile + handler.py + template.yaml + deploy.sh (AWS Lambda)
```

### Tools that need the user's OK

A tool whose manifest sets `"confirm": true` is never run on the model's own judgement.
`tool_runner.run_tool` refuses it unless the call carries `user_confirmed: true`, and tells the
model to ask first with the `ask_user` tool — which ends the turn, puts the question in the
thread, and waits for a human answer. Set it for anything that deletes, overwrites, posts
outward, or spends money. The three analysis tools here are read-only, so none set it; the gate
exists so the *next* tool can't quietly acquire blast radius. `run_code` has the same gate built
in for the handful of operations that reach outside its scratch dir (see `sandbox.risky_operations`).
(`arazoza-formatter` writes a *new* copy of the workbook and never touches the original, so it
doesn't need the gate either.)

### Tools with hard routing rules (`triggers`)

A manifest may declare `"triggers": {"keywords": [...], "action_words": [...], "filename_contains": [...]}`
(`keywords`/`action_words` are case-insensitive regexes; a plain phrase is a fine regex):
- a **keyword** in the user's message ("arazoza") *together with* an **action word** ("format",
  "clean up", "worksheet"…) makes the turn an *action* deterministically
  (`app2._looks_like_action` → `tool_registry.action_triggered`), the same as naming the tool.
  The bare keyword is left to the selector, so a question *about* Arazoza is still answered;
- a **filename** substring on an attachment, or a keyword hit, adds a *routing note* to the
  model's context (`tool_registry.routing_note`) naming the tool. The note respects
  `accepts.file_types`: when the tool needs a file and no acceptable one is attached (nothing,
  or only a schedule PNG / an old `.xls`), it tells the model to stop and ask for it with
  `ask_user` rather than improvise with `run_code` or call the tool on the wrong file.

Use it for rules the model kept getting wrong on its own; the ordinary case is still
`when_to_use`. `arazoza-formatter` is the first tool to use it.

Every `tool.json` is validated at startup against [`_schema.json`](_schema.json). A malformed
manifest is logged and skipped — a broken tool never stops the bot from starting.

## The invocation contract (both backends honor it)

**Input** (stdin for local, event for lambda):
```json
{ "input": { "input_file": "file_1", "...": "..." },
  "input_path": "<local path | S3 key>",
  "work_dir":   "<local dir | S3 output prefix>",
  "backend":    "local | lambda" }
```
**Output** (`work_dir/result.json` for local; returned payload for lambda):
```json
{ "status": "ok|error", "summary": "...",
  "artifacts": [ { "kind": "xlsx", "ref": "<path|key>", "filename": "x.xlsx", "title": "..." } ],
  "error": null }
```
Files are passed **by handle** (`file_1`) so the model never sees real paths/keys. The bot
strips `ref` before showing a result to the model; it keeps the full record to upload the
artifact back into the Slack thread.

## Backends

Selected by `TOOL_BACKEND`:
- **`local`** (default) — the bot runs `run.py` as a subprocess in the tool's `.venv`. Proves
  the whole flow with no cloud. Run `tools/<name>/setup.sh` and `sandbox/setup.sh` once.
- **`lambda`** — the bot invokes each tool's AWS Lambda (`tm-tool-<name>`) and the two sandbox
  Lambdas (`tm-sandbox-runcode`, `tm-sandbox-runcode-ocr`); files round-trip through
  `SCRATCH_S3_BUCKET`. Nothing else in the bot changes when you flip the switch.

### Bot environment variables
| var | default | purpose |
|---|---|---|
| `TOOL_BACKEND` | `local` | `local` or `lambda` |
| `SCRATCH_S3_BUCKET` | — | scratch bucket for staging files (lambda only) |
| `MAX_FILE_BYTES` | `26214400` | per-attachment download cap (25 MB) |
| `ACTION_MODEL` | `claude-sonnet-5` | model that drives the tool-use loop |
| `MAX_TOOL_ITERATIONS` | `6` | tool-use loop cap |
| `SANDBOX_TIMEOUT_SECONDS` | `120` | hard cap on a single `run_code` run |
| `SANDBOX_LAMBDA_NAME` | `tm-sandbox-runcode` | default sandbox Lambda name (lambda only) |
| `SANDBOX_LAMBDA_NAME_OCR` | `tm-sandbox-runcode-ocr` | neural-OCR sandbox Lambda name (lambda only) |
| `SANDBOX_OMP_NUM_THREADS` | `4` | thread cap for OCR/onnxruntime in `run_code` |

### New Slack scope
Uploading result files needs **`files:write`** (in addition to the existing `files:read`).
Add it in the Slack app's *OAuth & Permissions* and **reinstall the app**.

## Adding a new tool
1. `mkdir tools/<name>/`, add `tool.json` (name must equal the dir), `README.md`, your logic,
   `requirements.txt`, and a `run.py` honoring the contract above.
2. `tools/<name>/setup.sh` to build the venv; the bot auto-discovers it on next start.
3. For production, add a `lambda/` package (copy this tool's as a template) and deploy.

## Deploying the Lambdas (when you're ready to flip to `lambda`)
1. Create a scratch S3 bucket (suggest a lifecycle rule to expire `runs/` after a day).
2. `cd tools/schedule-extractor/lambda && SCRATCH_BUCKET=<bucket> ./deploy.sh`
3. `cd tools/bid-scanner/lambda && SCRATCH_BUCKET=<bucket> ./deploy.sh`
4. `cd tools/wall-height-calculator/lambda && SCRATCH_BUCKET=<bucket> ./deploy.sh`
5. `cd tools/arazoza-formatter/lambda && SCRATCH_BUCKET=<bucket> ./deploy.sh`
6. `cd sandbox/lambda && SCRATCH_BUCKET=<bucket> ./deploy.sh` (builds BOTH sandbox images:
   `tm-sandbox-runcode` and `tm-sandbox-runcode-ocr`)
7. Set the bot's config: `TOOL_BACKEND=lambda`, `SCRATCH_S3_BUCKET=<bucket>`, and AWS creds.

### Bot IAM policy (Heroku access keys)
The bot only needs to invoke the six functions and read/write the scratch bucket:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow",
      "Action": "lambda:InvokeFunction",
      "Resource": [
        "arn:aws:lambda:us-east-1:<ACCOUNT_ID>:function:tm-tool-schedule-extractor",
        "arn:aws:lambda:us-east-1:<ACCOUNT_ID>:function:tm-tool-bid-scanner",
        "arn:aws:lambda:us-east-1:<ACCOUNT_ID>:function:tm-tool-wall-height-calculator",
        "arn:aws:lambda:us-east-1:<ACCOUNT_ID>:function:tm-tool-arazoza-formatter",
        "arn:aws:lambda:us-east-1:<ACCOUNT_ID>:function:tm-sandbox-runcode",
        "arn:aws:lambda:us-east-1:<ACCOUNT_ID>:function:tm-sandbox-runcode-ocr"
      ] },
    { "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject"],
      "Resource": "arn:aws:s3:::<SCRATCH_BUCKET>/*" }
  ]
}
```
The Lambdas use their **own** execution roles (schedule-extractor: Textract + scratch
bucket; bid-scanner, wall-height-calculator, arazoza-formatter, and both sandbox functions:
scratch bucket only)
— no static keys inside them.

> Region convention: `us-east-1`. Owner: Konur Papageorgiou; general escalation: Tommy Lather.

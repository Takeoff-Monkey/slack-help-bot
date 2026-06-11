# Bot tools & the `run_code` sandbox

This directory holds the **tools** the Slack bot ([../app2.py](../app2.py)) can run on demand.
When a teammate attaches a file and asks for something, the bot reaches for the most fitting
specialized tool first; anything no tool covers, it attempts itself with the sandboxed
`run_code` fallback. See the design notes below before adding a tool.

## Anatomy of a tool

```
tools/<name>/
  tool.json          ← machine contract: name, description, when_to_use, input_schema, entrypoint
  README.md          ← human/AI prose
  run.py             ← local entrypoint (stdin JSON in → result.json out)
  main*.py           ← the actual logic
  requirements.txt   ← the tool's own deps (installed into .venv, NOT the bot's env)
  setup.sh           ← builds .venv from requirements.txt
  .venv/             ← the tool's isolated interpreter (gitignored)
  .env               ← the tool's secrets (gitignored)
  lambda/            ← Dockerfile + handler.py + template.yaml + deploy.sh (AWS Lambda)
```

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
- **`lambda`** — the bot invokes each tool's AWS Lambda (`tm-tool-<name>`) and the sandbox
  Lambda (`tm-sandbox-runcode`); files round-trip through `SCRATCH_S3_BUCKET`. Nothing else
  in the bot changes when you flip the switch.

### Bot environment variables
| var | default | purpose |
|---|---|---|
| `TOOL_BACKEND` | `local` | `local` or `lambda` |
| `SCRATCH_S3_BUCKET` | — | scratch bucket for staging files (lambda only) |
| `MAX_FILE_BYTES` | `26214400` | per-attachment download cap (25 MB) |
| `ACTION_MODEL` | `claude-sonnet-4-6` | model that drives the tool-use loop |
| `MAX_TOOL_ITERATIONS` | `6` | tool-use loop cap |
| `SANDBOX_TIMEOUT_SECONDS` | `120` | hard cap on a single `run_code` run |
| `SANDBOX_LAMBDA_NAME` | `tm-sandbox-runcode` | sandbox Lambda name (lambda only) |

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
3. `cd sandbox/lambda && SCRATCH_BUCKET=<bucket> ./deploy.sh`
4. Set the bot's config: `TOOL_BACKEND=lambda`, `SCRATCH_S3_BUCKET=<bucket>`, and AWS creds.

### Bot IAM policy (Heroku access keys)
The bot only needs to invoke the two functions and read/write the scratch bucket:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow",
      "Action": "lambda:InvokeFunction",
      "Resource": [
        "arn:aws:lambda:us-east-1:<ACCOUNT_ID>:function:tm-tool-schedule-extractor",
        "arn:aws:lambda:us-east-1:<ACCOUNT_ID>:function:tm-sandbox-runcode"
      ] },
    { "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject"],
      "Resource": "arn:aws:s3:::<SCRATCH_BUCKET>/*" }
  ]
}
```
The Lambdas use their **own** execution roles (Textract + scratch bucket for the tool;
scratch bucket only for the sandbox) — no static keys inside them.

> Region convention: `us-east-1`. Owner: Konur Papageorgiou; general escalation: Tommy Lather.

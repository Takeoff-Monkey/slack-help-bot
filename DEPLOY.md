# Deploying the bot's tools (install + push)

There are **four Python environments** and they never mix:

| Environment | Where | Built by | Holds |
|---|---|---|---|
| **Bot** | repo root (`./.venv` locally, the Heroku dyno in prod) | `requirements.txt` | anthropic, slack-bolt, **boto3** — light |
| **Tool** | `tools/schedule-extractor/.venv` | `tools/schedule-extractor/setup.sh` | PyMuPDF, pandas, textract — heavy |
| **Sandbox (default)** | `sandbox/.venv` | `sandbox/setup.sh` | Tesseract OCR + opencv + PDF/data/doc libs (extended toolkit) |
| **Sandbox (neural OCR)** | `sandbox/.venv-ocr` | `sandbox/setup.sh` | above **+** RapidOCR (`rapidocr-onnxruntime` + onnxruntime, offline models) |

> `sandbox/setup.sh` builds **both** sandbox venvs and warns if the `tesseract`/`pdftoppm` system
> binaries are missing (OCR needs them — `sudo apt-get install -y tesseract-ocr poppler-utils`).

> **Key fact:** the tool/sandbox `.venv`s are gitignored, so they do **not** exist on Heroku.
> That's why **Heroku must run `TOOL_BACKEND=lambda`** (the tools live in AWS Lambda there).
> The `local` backend is for proving things on *your machine* only.

Account/resources for this project: AWS `191219945009`, region `us-east-1`,
scratch bucket `help-bot-code-scratchpad`, Heroku app `slack-help-bot`.

---

## Part 0 — Slack: add `files:write` (do this first; needed to upload result files)

1. api.slack.com/apps → your app → **OAuth & Permissions** → *Scopes* → *Bot Token Scopes* →
   **Add `files:write`** (you already have `files:read`, `chat:write`, `groups:read`).
2. Scroll up → **Reinstall to Workspace** → Allow.
3. That's it — no event-subscription or socket changes. The bot token (`xoxb-…`) stays the
   same; it just gains the new scope. (Attached files already arrive on the existing
   `message`/`app_mention` events.)

---

## Part 1 — Prove it locally (`TOOL_BACKEND=local`)

```bash
cd /home/konur/Documents/Takeoff_Monkey/slack-help-bot

# 1) Bot venv  ← this is where `pip install` goes
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2) Tool + sandbox venvs (already built once; re-run after a fresh clone)
./tools/schedule-extractor/setup.sh
./sandbox/setup.sh

# 3) Make sure your local .env (repo root) has these, and you're allowlisted:
#    SLACK_BOT_TOKEN=xoxb-...   SLACK_APP_TOKEN=xapp-...   ANTHROPIC_API_KEY=sk-ant-...
#    ALLOWED_USERS=<your-slack-user-id>     TOOL_BACKEND=local
#    (the TOOL's own AWS/Textract creds live in tools/schedule-extractor/.env)

# 4) Run it (socket mode — no public URL needed)
python app2.py
```
Then DM the bot a construction PDF and ask it to "extract the schedules". You should see
"Running schedule-extractor…", then a summary + an `.xlsx` uploaded into the thread.

> Real extraction calls AWS Textract using the creds in `tools/schedule-extractor/.env`.
> If you rotated those keys (you should — they were exposed), update that file.

---

## Part 2 — Deploy the two Lambdas (one-time, from your machine → AWS)

### 2a. Install the missing prerequisite: AWS SAM CLI
```bash
curl -Lo /tmp/aws-sam-cli.zip "https://github.com/aws/aws-sam-cli/releases/latest/download/aws-sam-cli-linux-x86_64.zip"
unzip -o /tmp/aws-sam-cli.zip -d /tmp/sam-installation
sudo /tmp/sam-installation/install      # or: sudo /tmp/sam-installation/install --update
sam --version
```
(`aws` and `docker` are already installed and Docker is running — good.)

### 2b. (recommended) Auto-expire scratch files
Add an S3 **Lifecycle rule** on `help-bot-code-scratchpad` to expire objects under prefix
`runs/` after ~1 day (console → bucket → Management → Lifecycle rules).

### 2c. Deploy
The scripts are at `tools/schedule-extractor/lambda/deploy.sh` and `sandbox/lambda/deploy.sh`.
Run each from its own folder; first run is interactive (`--guided`) and saves a
`samconfig.toml` so later runs are one command.

```bash
# Tool Lambda → creates function: tm-tool-schedule-extractor
cd tools/schedule-extractor/lambda
SCRATCH_BUCKET=help-bot-code-scratchpad ./deploy.sh
#   guided prompts: stack name (accept default), region us-east-1, confirm changeset = Y,
#   "allow SAM to create IAM roles" = Y, ScratchBucket = help-bot-code-scratchpad,
#   OpenAIApiKey = (leave blank unless you turn GPT_CLEANUP on), save args = Y.

# Sandbox Lambdas → creates BOTH functions: tm-sandbox-runcode + tm-sandbox-runcode-ocr
cd ../../../sandbox/lambda
SCRATCH_BUCKET=help-bot-code-scratchpad ./deploy.sh
```
SAM builds the Docker images, pushes them to an auto-created ECR repo, and creates each
function **with its own least-privilege execution role** (Textract + scratch-bucket for the
tool; scratch-bucket only for the sandboxes). The sandbox stack now builds **two** images —
the default extended toolkit and the heavier neural-OCR image (`Dockerfile.ocr`, ~300MB larger,
so a slower first build). Your `KonurPapa` user needs rights to create CloudFormation/ECR/Lambda/IAM
(account owner is fine).

---

## Part 3 — Give the bot AWS credentials (to invoke the Lambdas)

The Lambdas have their own roles; the **bot** just needs to invoke them + use the bucket.
Create a dedicated least-privilege IAM user (don't reuse your personal keys):

`bot-invoke-policy.json`:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow", "Action": "lambda:InvokeFunction",
      "Resource": [
        "arn:aws:lambda:us-east-1:191219945009:function:tm-tool-schedule-extractor",
        "arn:aws:lambda:us-east-1:191219945009:function:tm-sandbox-runcode",
        "arn:aws:lambda:us-east-1:191219945009:function:tm-sandbox-runcode-ocr"
      ] },
    { "Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::help-bot-code-scratchpad/*" },
    { "Effect": "Allow", "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::help-bot-code-scratchpad" }
  ]
}
```

> The bot deletes each run's scratch objects after replying (output is delivered to Slack,
> so nothing needs to persist) — hence `s3:DeleteObject` + `s3:ListBucket`. If you used your
> admin keys for the bot, this is already covered. If you used a scoped `tm-help-bot` user,
> re-run the `put-user-policy` command with the updated document above. As a backstop, you
> can also add an S3 lifecycle rule expiring `runs/` after 1 day.
```bash
aws iam create-user --user-name tm-help-bot
aws iam put-user-policy --user-name tm-help-bot --policy-name invoke-tools \
  --policy-document file://bot-invoke-policy.json
aws iam create-access-key --user-name tm-help-bot      # copy the AccessKeyId + SecretAccessKey
```

---

## Part 4 — Push to Heroku (production = `lambda` backend)

```bash
cd /home/konur/Documents/Takeoff_Monkey/slack-help-bot

# Config (the bot reads these as env vars on the dyno)
heroku config:set -a slack-help-bot \
  TOOL_BACKEND=lambda \
  SCRATCH_S3_BUCKET=help-bot-code-scratchpad \
  AWS_DEFAULT_REGION=us-east-1 \
  AWS_ACCESS_KEY_ID=<from step 3> \
  AWS_SECRET_ACCESS_KEY=<from step 3>

# Ship the code
git add -A
git commit -m "Add agentic tool-use: schedule-extractor tool + run_code sandbox"
git push heroku main          # deploy
git push origin main          # mirror to GitHub

# Watch it come up
heroku logs --tail -a slack-help-bot     # look for: Discovered tool 'schedule-extractor'
```

> **Do not** leave `TOOL_BACKEND` unset on Heroku — it defaults to `local`, and the tool
> venvs don't exist on the dyno, so the tool would return "isn't set up yet". `lambda` is
> required in prod.

---

## Quick reference

| Thing | Value |
|---|---|
| Scratch bucket | `help-bot-code-scratchpad` (us-east-1) |
| Tool Lambda | `tm-tool-schedule-extractor` |
| Sandbox Lambdas | `tm-sandbox-runcode` (default), `tm-sandbox-runcode-ocr` (neural OCR) |
| Heroku app | `slack-help-bot` |
| New Slack scope | `files:write` (reinstall) |
| Prod env vars | `TOOL_BACKEND=lambda`, `SCRATCH_S3_BUCKET`, `AWS_*` |

### Cold-start tuning (optional — sane defaults, only set to override)

The tool/sandbox Lambdas are container images, so one that has gone cold needs tens of
seconds to pull and boot. The bot handles that itself: it pings the sandbox awake at the
start of an action turn, waits for that boot before invoking for real, and if Lambda answers
"not ready / throttled" it backs off in escalating steps (5s → 10s → 20s → 30s) instead of
retrying instantly.

| Var | Default | What it does |
|---|---|---|
| `LAMBDA_COLD_START_GRACE_SECONDS` | `90` | Read-timeout headroom on top of each function's own timeout (the boot happens *inside* the invoke). Also caps how long the bot waits on a warm-up ping. |
| `LAMBDA_COLD_START_MAX_WAIT_SECONDS` | `65` | Total time spent waiting *between* attempts before giving up with "still starting up". |
| `SANDBOX_PREWARM` | `1` | Set `0` to stop pinging the sandbox awake at the start of an action turn. |
| `SANDBOX_PREWARM_TTL_SECONDS` | `240` | Don't re-ping a sandbox warmed this recently. |

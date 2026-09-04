# slack-help-bot

AI Slack bot that answers teammates' questions about Takeoff Monkey's internal tech stack (Heroku bots, Zapier flows, AWS Lambdas, Monday automations, etc). Backed by a library of HTML "skill" files in [`docs/skills/`](docs/skills/). Hosted on Heroku.

The bot ([app2.py](app2.py)) is separate from the Ewing note updater ([app.py](app.py)) — both share this repo but each runs as its own Heroku app/process.

## Project structure

```
app2.py                  ← the AI help bot (this project)
app.py                   ← legacy: Ewing note updater
docs/skills/             ← knowledge base — one HTML file per system
  ├── _manifest.json     ← machine-readable index (regenerated)
  ├── index.html         ← human-browsable index (regenerated)
  └── *.html             ← individual skills
build_manifest.py        ← regenerates _manifest.json
build_index.py           ← regenerates index.html
Procfile                 ← Heroku processes: worker (note updater), ai_bot (help bot)
requirements.txt         ← Python deps
.python-version          ← Python 3.13
```

## Deploy a code change

What to run depends on whether you touched a `tools/<name>/` tool's own logic, or just the bot.

### Bot-only change (app2.py, agent_loop.py, docs/skills, …)

```bash
git add <files>
git commit -m "message"
git push heroku main         # deploy to Heroku
git push origin main         # push to GitHub
```

Heroku rebuilds and auto-restarts after each push. Watch logs to confirm:

```bash
heroku logs --tail -a slack-help-bot
```

### A tool was added, or its logic changed (anything under `tools/<name>/`)

Production runs `TOOL_BACKEND=lambda` — the tool's own `.venv` is gitignored, so it never
reaches the Heroku dyno. Pushing the code makes the bot *discover* the tool, but running it
still needs that tool's **AWS Lambda** built or updated, which only someone with AWS
credentials can do (this repo's assistant does not deploy live AWS on its own):

```bash
# 1. Ship the code, same as above
git add <files> && git commit -m "message"
git push heroku main && git push origin main

# 2. Deploy/update that tool's Lambda (from your machine — needs Docker + AWS SAM,
#    see DEPLOY.md Part 2a if they aren't installed yet)
cd tools/<name>/lambda
SCRATCH_BUCKET=help-bot-code-scratchpad ./deploy.sh

# 3. First time only for a brand-new tool: add its function's ARN to the bot's IAM
#    policy — see DEPLOY.md Part 3 for the policy JSON and the put-user-policy command.
```

Until step 2 is done, calling that tool in Slack fails cleanly (a "Lambda invoke failed"
error) rather than silently — the bot never falls back to the missing local venv in
production.

[DEPLOY.md](DEPLOY.md) has the full one-time setup this quick version assumes is already
done: the scratch S3 bucket, the `files:write` Slack scope, the bot's IAM user, and the
cold-start/status-message tuning knobs.

## Watch / debug

```bash
heroku logs --tail -a slack-help-bot           # live tail (Ctrl+C to stop)
heroku logs -n 200 -a slack-help-bot           # last 200 lines
heroku ps -a slack-help-bot                    # process status
heroku restart -a slack-help-bot               # force restart
heroku run -a slack-help-bot python            # interactive Python REPL on the dyno
heroku run -a slack-help-bot bash              # interactive shell on the dyno
```

## Manage env vars

```bash
heroku config -a slack-help-bot                # list all
heroku config:set KEY=value -a slack-help-bot  # set one (auto-restarts dyno)
heroku config:unset KEY -a slack-help-bot      # remove one
```

Bot expects these env vars on Heroku:

| Var | Purpose |
|---|---|
| `SLACK_BOT_TOKEN` | `xoxb-…` — from Slack app's OAuth & Permissions |
| `SLACK_APP_TOKEN` | `xapp-…` — from Slack app's Basic Information |
| `ANTHROPIC_API_KEY` | `sk-ant-…` |
| `ALLOWED_USERS` | Comma-separated Slack user IDs (e.g. `U01XXX,U02YYY`). If unset, bot responds to everyone (production). If set, only those users get answers — use for testing. |
| `SKILL_DOCS_BASE_URL` | e.g. `https://takeoff-monkey.github.io/slack-help-bot/skills` — if set, Sources footer links to the HTML docs. If unset, sources shown as plain text. |
| `SELECTOR_MODEL` | Claude model for the cheap routing/skill-picker call. Default `claude-haiku-4-5`. |
| `ANSWER_MODEL` | Claude model for Q&A answers. Default `claude-sonnet-5`. |
| `ACTION_MODEL` | Claude model for the tool-use loop. Default `claude-sonnet-5`. |

## Edit the knowledge base

Skills are HTML files in `docs/skills/`. To add or modify one:

1. Copy [`_template.html`](docs/skills/_template.html) or an existing skill as a starting point. Fill in the `<meta>` tags in the `<head>` — those drive retrieval.
2. Regenerate the manifest + index:
   ```bash
   python3 build_manifest.py
   python3 build_index.py
   ```
3. Commit and deploy as normal.

The bot reads `_manifest.json` at startup, so it picks up new skills after the next deploy.

## Toggle private ↔ live

```bash
heroku config:set ALLOWED_USERS=U01YOURID -a slack-help-bot   # only you can use it
heroku config:unset ALLOWED_USERS -a slack-help-bot           # open to whole workspace
```

The bot logs `private mode` or `open mode` on startup so you can confirm in the logs.

## Scale Heroku processes

The Procfile defines two process types. This Heroku app should only run `ai_bot`:

```bash
heroku ps:scale ai_bot=1 worker=0 -a slack-help-bot
```

(The Ewing note updater's separate Heroku app does the opposite.)

## Inspect what the bot sees

Quick way to verify the bot's identity / tokens / Slack connection from the dyno:

```bash
heroku run -a slack-help-bot python
```

then in the REPL:

```python
from slack_sdk import WebClient
import os
print(WebClient(token=os.environ['SLACK_BOT_TOKEN']).auth_test().data)
```

Returns the workspace, bot username, user ID, and app ID — useful when DMs aren't reaching the bot.

## Resources
- [Slack App](https://api.slack.com/apps/A0B54VC7CG3/install-on-team?success=1)
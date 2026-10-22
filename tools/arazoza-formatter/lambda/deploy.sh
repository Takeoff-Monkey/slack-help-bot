#!/usr/bin/env bash
# Deploy the arazoza-formatter Lambda (container image) with AWS SAM.
# Prereqs: awscli configured, docker running, sam CLI installed.
#
# Usage:
#   SCRATCH_BUCKET=help-bot-code-scratchpad ANTHROPIC_API_KEY=sk-ant-... ./deploy.sh
#
# ANTHROPIC_API_KEY lets Claude spell-check the botanical names (latin.py). Omit it on a
# redeploy and SAM keeps the key the stack already has; with none at all the step is skipped.
#
# First run uses --guided to create the stack config; later runs reuse it.
set -euo pipefail
cd "$(dirname "$0")"

: "${SCRATCH_BUCKET:?Set SCRATCH_BUCKET to the scratch S3 bucket name}"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
STACK="${STACK:-tm-tool-arazoza-formatter}"

sam build

PARAMS="ScratchBucket=${SCRATCH_BUCKET}"
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  PARAMS="${PARAMS} AnthropicApiKey=${ANTHROPIC_API_KEY}"
fi

if [ -f samconfig.toml ]; then
  sam deploy --region "${REGION}" --parameter-overrides ${PARAMS}
else
  sam deploy --guided --stack-name "${STACK}" --region "${REGION}" \
    --capabilities CAPABILITY_IAM --resolve-image-repos \
    --parameter-overrides ${PARAMS}
fi

echo "Deployed. Point the bot at it: set TOOL_BACKEND=lambda and SCRATCH_S3_BUCKET=${SCRATCH_BUCKET}."

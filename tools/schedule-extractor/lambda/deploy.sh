#!/usr/bin/env bash
# Deploy the schedule-extractor Lambda (container image) with AWS SAM.
# Prereqs: awscli configured, docker running, sam CLI installed.
#
# Usage:
#   SCRATCH_BUCKET=prod-s3-tm-bot-scratch-YYYY-MM-DD ./deploy.sh
#   ANTHROPIC_API_KEY=sk-ant-...  needed for images to be read by Claude (without one they fall
#     back to Textract). Only needed once: a deploy that leaves out a parameter keeps the value
#     the stack already has, which also means a changed template Default doesn't apply to an
#     existing stack until you pass the new value.
#   (optional) VISION_MODEL=claude-haiku-5-5   to override the image-reading model
#   (optional) CLEANUP_MODEL=claude-haiku-5-5  to override the AI cleanup model (AI_CLEANUP)
#
# First run uses --guided to create the stack config; later runs reuse it.
set -euo pipefail
cd "$(dirname "$0")"

: "${SCRATCH_BUCKET:?Set SCRATCH_BUCKET to the scratch S3 bucket name}"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
STACK="${STACK:-tm-tool-schedule-extractor}"
ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}"
CLEANUP_MODEL="${CLEANUP_MODEL:-}"
VISION_MODEL="${VISION_MODEL:-}"

sam build

PARAMS="ScratchBucket=${SCRATCH_BUCKET}"
if [ -n "${ANTHROPIC_API_KEY}" ]; then
  PARAMS="${PARAMS} AnthropicApiKey=${ANTHROPIC_API_KEY}"
fi
if [ -n "${VISION_MODEL}" ]; then
  PARAMS="${PARAMS} VisionModel=${VISION_MODEL}"
fi
if [ -n "${CLEANUP_MODEL}" ]; then
  PARAMS="${PARAMS} CleanupModel=${CLEANUP_MODEL}"
fi

if [ -f samconfig.toml ]; then
  sam deploy --region "${REGION}" --parameter-overrides ${PARAMS}
else
  sam deploy --guided --stack-name "${STACK}" --region "${REGION}" \
    --capabilities CAPABILITY_IAM --resolve-image-repos \
    --parameter-overrides ${PARAMS}
fi

echo "Deployed. Point the bot at it: set TOOL_BACKEND=lambda and SCRATCH_S3_BUCKET=${SCRATCH_BUCKET}."

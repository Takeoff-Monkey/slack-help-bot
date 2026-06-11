#!/usr/bin/env bash
# Deploy the run_code sandbox Lambda (container image) with AWS SAM.
# Prereqs: awscli configured, docker running, sam CLI installed.
#
# Usage:
#   SCRATCH_BUCKET=prod-s3-tm-bot-scratch-YYYY-MM-DD ./deploy.sh
set -euo pipefail
cd "$(dirname "$0")"

: "${SCRATCH_BUCKET:?Set SCRATCH_BUCKET to the scratch S3 bucket name}"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
STACK="${STACK:-tm-sandbox-runcode}"

sam build

if [ -f samconfig.toml ]; then
  sam deploy --region "${REGION}" --parameter-overrides "ScratchBucket=${SCRATCH_BUCKET}"
else
  sam deploy --guided --stack-name "${STACK}" --region "${REGION}" \
    --capabilities CAPABILITY_IAM --resolve-image-repos \
    --parameter-overrides "ScratchBucket=${SCRATCH_BUCKET}"
fi

echo "Deployed tm-sandbox-runcode. The bot uses it automatically when TOOL_BACKEND=lambda."

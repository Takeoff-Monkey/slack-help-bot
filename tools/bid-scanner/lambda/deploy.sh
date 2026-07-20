#!/usr/bin/env bash
# Deploy the bid-scanner Lambda (container image) with AWS SAM.
# Prereqs: awscli configured, docker running, sam CLI installed.
#
# Usage:
#   SCRATCH_BUCKET=prod-s3-tm-bot-scratch-YYYY-MM-DD ./deploy.sh
#
# First run uses --guided to create the stack config; later runs reuse it.
set -euo pipefail
cd "$(dirname "$0")"

: "${SCRATCH_BUCKET:?Set SCRATCH_BUCKET to the scratch S3 bucket name}"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
STACK="${STACK:-tm-tool-bid-scanner}"

sam build

PARAMS="ScratchBucket=${SCRATCH_BUCKET}"

if [ -f samconfig.toml ]; then
  sam deploy --region "${REGION}" --parameter-overrides ${PARAMS}
else
  sam deploy --guided --stack-name "${STACK}" --region "${REGION}" \
    --capabilities CAPABILITY_IAM --resolve-image-repos \
    --parameter-overrides ${PARAMS}
fi

echo "Deployed. Point the bot at it: set TOOL_BACKEND=lambda and SCRATCH_S3_BUCKET=${SCRATCH_BUCKET}."

#!/usr/bin/env bash
# Deploy the run_code sandbox Lambdas (container images) with AWS SAM. The stack defines TWO
# functions and `sam build` builds BOTH images: tm-sandbox-runcode (default extended toolkit)
# and tm-sandbox-runcode-ocr (adds the RapidOCR neural engine).
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

echo "Deployed tm-sandbox-runcode + tm-sandbox-runcode-ocr."
echo "The bot uses them automatically when TOOL_BACKEND=lambda (defaults match SANDBOX_LAMBDA_NAME"
echo "and SANDBOX_LAMBDA_NAME_OCR; override those env vars only if you renamed the functions)."

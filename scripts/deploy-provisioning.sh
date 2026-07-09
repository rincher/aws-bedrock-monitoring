#!/usr/bin/env bash
# Push local provisioning/ to the S3 bucket that EC2 syncs from on boot.
#
# Usage:
#   AWS_PROFILE=<profile> PROVISIONING_BUCKET=<bucket> ./scripts/deploy-provisioning.sh
#
# Env:
#   AWS_PROFILE             AWS CLI profile (required)
#   AWS_REGION              default: ap-northeast-2
#   PROVISIONING_BUCKET     S3 bucket name (required)
set -euo pipefail

: "${AWS_PROFILE:?AWS_PROFILE is required}"
: "${PROVISIONING_BUCKET:?PROVISIONING_BUCKET is required}"
AWS_REGION="${AWS_REGION:-ap-northeast-2}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$REPO_ROOT/provisioning"

[ -d "$SRC" ] || { echo "missing $SRC" >&2; exit 1; }

echo "→ syncing $SRC → s3://$PROVISIONING_BUCKET/provisioning/"
aws s3 sync "$SRC" "s3://$PROVISIONING_BUCKET/provisioning/" \
  --delete \
  --profile "$AWS_PROFILE" \
  --region "$AWS_REGION"

echo "✓ done. EC2 will pick this up on next reboot or by running scripts/bootstrap-ec2.sh."

#!/usr/bin/env bash
# Build the React frontend, upload it to the private site bucket, invalidate CloudFront.
#
#   scripts/deploy_frontend.sh [--skip-build] [--wait]
#
#   --skip-build  upload what is already in the dist directory (frontend/dist, or $FRONTEND_DIST)
#   --wait        block until the CloudFront invalidation has completed (about a minute)
#
# Bucket name and distribution id come from infra/outputs.json (written by infra/deploy.sh).
# Every AWS call goes through scripts/with_aws_env.sh; credentials are never printed.
# Cache policy: hashed assets are immutable for a year, everything else (index.html) is revalidated.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HELPER="$ROOT/scripts/with_aws_env.sh"
OUTPUTS="$ROOT/infra/outputs.json"
FRONTEND="$ROOT/frontend"
DIST_DIR="${FRONTEND_DIST:-$FRONTEND/dist}"

SKIP_BUILD=false
WAIT=false
for arg in "$@"; do
  case "$arg" in
    --skip-build) SKIP_BUILD=true ;;
    --wait) WAIT=true ;;
    -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "deploy_frontend: unknown argument: $arg" >&2; exit 2 ;;
  esac
done

[ -r "$OUTPUTS" ] || { echo "deploy_frontend: $OUTPUTS not found. Deploy the stack first: infra/deploy.sh" >&2; exit 1; }

read_output() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["AldoReturnsDemo"][sys.argv[2]])' "$OUTPUTS" "$1"
}
BUCKET="$(read_output BucketName)"
DISTRIBUTION_ID="$(read_output DistributionId)"
SITE_URL="$(read_output CloudFrontUrl)"

if [ "$SKIP_BUILD" = false ]; then
  [ -f "$FRONTEND/package.json" ] || { echo "deploy_frontend: $FRONTEND/package.json not found" >&2; exit 1; }
  if [ ! -d "$FRONTEND/node_modules" ]; then
    echo "deploy_frontend: installing frontend dependencies (npm ci)"
    (cd "$FRONTEND" && npm ci --no-audit --no-fund)
  fi
  echo "deploy_frontend: building $FRONTEND"
  (cd "$FRONTEND" && npm run build)
fi

[ -f "$DIST_DIR/index.html" ] || { echo "deploy_frontend: $DIST_DIR/index.html not found (build first)" >&2; exit 1; }

echo "deploy_frontend: uploading $DIST_DIR to s3://$BUCKET"
# 1) everything except hashed assets and index.html, short cache
"$HELPER" aws s3 sync "$DIST_DIR/" "s3://$BUCKET/" --delete --only-show-errors \
  --exclude 'assets/*' --exclude 'index.html' --cache-control 'public, max-age=300'
# 2) hashed assets, immutable
if [ -d "$DIST_DIR/assets" ]; then
  "$HELPER" aws s3 sync "$DIST_DIR/assets/" "s3://$BUCKET/assets/" --delete --only-show-errors \
    --cache-control 'public, max-age=31536000, immutable'
fi
# 3) index.html last, always revalidated
"$HELPER" aws s3 cp "$DIST_DIR/index.html" "s3://$BUCKET/index.html" --only-show-errors \
  --cache-control 'no-cache' --content-type 'text/html; charset=utf-8'

INVALIDATION_ID="$("$HELPER" aws cloudfront create-invalidation --distribution-id "$DISTRIBUTION_ID" \
  --paths '/*' --query 'Invalidation.Id' --output text)"
echo "deploy_frontend: invalidation $INVALIDATION_ID created for distribution $DISTRIBUTION_ID"
if [ "$WAIT" = true ]; then
  "$HELPER" aws cloudfront wait invalidation-completed --distribution-id "$DISTRIBUTION_ID" --id "$INVALIDATION_ID"
  echo "deploy_frontend: invalidation completed"
fi
echo "deploy_frontend: done -> $SITE_URL"

#!/usr/bin/env bash
# Deploy the AldoReturnsDemo stack with the pinned CDK CLI (infra/node_modules). All AWS access goes through
# scripts/with_aws_env.sh, credentials are never printed.
#
#   infra/deploy.sh                       Lambda code = infra/stubs (default, works before backend/api exists)
#   infra/deploy.sh --real-code           build the linux arm64 Lambda bundle from backend/ and deploy it
#                                         (scripts/bundle_backend.sh --lambda, then cdk deploy -c useRealCode=true)
#   infra/deploy.sh --real-code --allow-stubs
#                                         same, but use the stub for a handler missing in backend/ (testing only)
#   infra/deploy.sh --real-code --skip-bundle
#                                         reuse the existing infra/build/lambda
#   infra/deploy.sh --fast                add --hotswap-fallback: Lambda code/env changes land in seconds
#                                         (development only; falls back to CloudFormation when it cannot hotswap)
#   infra/deploy.sh --diff                cdk diff only, no deploy
#   infra/deploy.sh --runtime-arn ARN     override the AgentCore runtime ARN (default: SSM /aldo-demo/runtime-arn)
#
# The runtime ARN is only used when it looks like arn:aws:bedrock-agentcore:...; otherwise the stack keeps a
# placeholder ARN and the worker cannot invoke anything yet.
# After a deploy the non-secret stack outputs are written to infra/outputs.json:
#   {"AldoReturnsDemo": {"CloudFrontUrl": "...", "ApiUrl": "...", "TableName": "...", "BucketName": "...", ...}}
set -euo pipefail

INFRA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$INFRA")"
HELPER="$ROOT/scripts/with_aws_env.sh"
STACK="AldoReturnsDemo"
REGION="us-east-1"

REAL_CODE=false
ALLOW_STUBS=false
SKIP_BUNDLE=false
FAST=false
DIFF_ONLY=false
RUNTIME_ARN=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --real-code) REAL_CODE=true ;;
    --allow-stubs) ALLOW_STUBS=true ;;
    --skip-bundle) SKIP_BUNDLE=true ;;
    --fast) FAST=true ;;
    --diff) DIFF_ONLY=true ;;
    --runtime-arn) shift; RUNTIME_ARN="${1:-}" ;;
    -h|--help) sed -n '2,22p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "deploy: unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

CONTEXT_ARGS=()

if [ "$REAL_CODE" = true ]; then
  if [ "$SKIP_BUNDLE" = false ]; then
    if [ "$ALLOW_STUBS" = true ]; then
      "$ROOT/scripts/bundle_backend.sh" --lambda --allow-stubs
    else
      "$ROOT/scripts/bundle_backend.sh" --lambda
    fi
  fi
  CONTEXT_ARGS+=(-c useRealCode=true)
else
  # Keep the fixtures bundle fresh even in stub mode (cheap, and the agent deploy needs it).
  "$ROOT/scripts/bundle_backend.sh"
fi

if [ -z "$RUNTIME_ARN" ]; then
  RUNTIME_ARN="$("$HELPER" aws ssm get-parameter --region "$REGION" --name /aldo-demo/runtime-arn \
    --query Parameter.Value --output text 2>/dev/null || true)"
fi
case "$RUNTIME_ARN" in
  arn:aws:bedrock-agentcore:*)
    CONTEXT_ARGS+=(-c "runtimeArn=$RUNTIME_ARN")
    echo "deploy: using AgentCore runtime ARN from SSM/arg"
    ;;
  *)
    echo "deploy: no AgentCore runtime ARN yet; the stack keeps its placeholder"
    ;;
esac

cd "$INFRA"
if [ "$DIFF_ONLY" = true ]; then
  exec "$HELPER" npx cdk diff "$STACK" --no-color --no-notices ${CONTEXT_ARGS[@]+"${CONTEXT_ARGS[@]}"}
fi

DEPLOY_ARGS=(--require-approval never --no-color --no-notices)
if [ "$FAST" = true ]; then
  DEPLOY_ARGS+=(--hotswap-fallback)
fi
"$HELPER" npx cdk deploy "$STACK" "${DEPLOY_ARGS[@]}" ${CONTEXT_ARGS[@]+"${CONTEXT_ARGS[@]}"}

# Refresh infra/outputs.json from the live stack (also correct after a hotswap deploy).
"$HELPER" aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query 'Stacks[0].Outputs' --output json | python3 -c '
import json, sys
outputs = json.load(sys.stdin)
data = {"AldoReturnsDemo": {o["OutputKey"]: o["OutputValue"] for o in outputs}}
with open(sys.argv[1], "w") as handle:
    json.dump(data, handle, indent=2, sort_keys=True)
    handle.write("\n")
' "$INFRA/outputs.json"
echo "deploy: wrote $INFRA/outputs.json"

#!/usr/bin/env bash
# Deploy (or diff) the AldoDataPlatform stack with the pinned CDK CLI. Separate, removable
# unit (docs/data-platform/DESIGN.md): it adds ONLY new resources and never touches the main
# AldoReturnsDemo stack or the AgentCore runtime.
#
#   infra/deploy_data_platform.sh --diff   cdk diff only, no deploy (run this FIRST; the diff
#                                          must contain ONLY additions)
#   infra/deploy_data_platform.sh          cdk deploy AldoDataPlatform
#
# All AWS access goes through scripts/with_aws_env.sh; credentials are never printed.
# The worker and API Lambda role ARNs are resolved READ-ONLY (infra/outputs.json, then
# `aws lambda get-function-configuration` as a fallback) and passed as CDK context so the
# stack imports them with iam.Role.fromRoleArn(mutable=True) and creates no new roles.
#
# CDK needs writable temp/home dirs under infra/build (the sandboxed HOME is not writable);
# this mirrors the env infra/deploy.sh relies on.
set -euo pipefail

INFRA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$INFRA")"
HELPER="$ROOT/scripts/with_aws_env.sh"
STACK="AldoDataPlatform"
REGION="us-east-1"

DIFF_ONLY=false
while [ "$#" -gt 0 ]; do
  case "$1" in
    --diff) DIFF_ONLY=true ;;
    -h|--help) sed -n '2,16p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "deploy_data_platform: unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

# --- Writable CDK temp/home under infra/build (mirror of what infra/deploy.sh depends on).
export TMPDIR="$INFRA/build/cdk-tmp"
export TMP="$TMPDIR"
export TEMP="$TMPDIR"
export CDK_HOME="$INFRA/build/cdk-home"
mkdir -p "$TMPDIR" "$CDK_HOME"

# --- Resolve the existing Lambda role ARNs READ-ONLY.
OUTPUTS="$INFRA/outputs.json"
WORKER_ROLE_ARN=""
API_ROLE_ARN=""
if [ -r "$OUTPUTS" ]; then
  WORKER_ROLE_ARN="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["AldoReturnsDemo"].get("WorkerRoleArn",""))' "$OUTPUTS" 2>/dev/null || true)"
fi
if [ -z "$WORKER_ROLE_ARN" ]; then
  WORKER_ROLE_ARN="$("$HELPER" aws lambda get-function-configuration --region "$REGION" \
    --function-name aldo-returns-demo-worker --query Role --output text 2>/dev/null || true)"
fi
# outputs.json has no API role ARN; read it from the live function configuration.
API_ROLE_ARN="$("$HELPER" aws lambda get-function-configuration --region "$REGION" \
  --function-name aldo-returns-demo-api --query Role --output text 2>/dev/null || true)"

if [ -z "$WORKER_ROLE_ARN" ] || [ -z "$API_ROLE_ARN" ]; then
  echo "deploy_data_platform: could not resolve both Lambda role ARNs" >&2
  echo "  worker=${WORKER_ROLE_ARN:-<empty>}  api=${API_ROLE_ARN:-<empty>}" >&2
  exit 1
fi
echo "deploy_data_platform: worker role resolved, api role resolved"

CONTEXT_ARGS=(
  -c dataPlatform=true
  -c "workerRoleArn=$WORKER_ROLE_ARN"
  -c "apiRoleArn=$API_ROLE_ARN"
)

cd "$INFRA"
if [ "$DIFF_ONLY" = true ]; then
  exec "$HELPER" node_modules/.bin/cdk diff "$STACK" --no-color --no-notices "${CONTEXT_ARGS[@]}"
fi

"$HELPER" node_modules/.bin/cdk deploy "$STACK" \
  --require-approval never --no-color --no-notices "${CONTEXT_ARGS[@]}"
echo "deploy_data_platform: $STACK deployed"

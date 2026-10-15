#!/usr/bin/env bash
# Deploy the ALDO returns agent (backend/agent_app.py) to Amazon Bedrock AgentCore Runtime.
#
#   scripts/deploy_agent.sh                 bundle, stage, package, create or update the runtime, verify, publish the ARN
#   scripts/deploy_agent.sh --skip-bundle   reuse backend/fixtures_bundle (do not run scripts/bundle_backend.sh)
#   scripts/deploy_agent.sh --env-only      replace only the runtime's environment variables (no package, no upload)
#   scripts/deploy_agent.sh --dry-run       print the plan and the exact commands, change nothing
#
# Direct code deploy with the pinned starter toolkit (backend/.venv/bin/agentcore): no Docker, CodeBuild or ECR.
# Idempotent: the first run creates the runtime, every later run updates it in place (same ARN, new version).
# Run it again after ANY change under backend/agent, backend/returns_domain, backend/agent_app.py, the fixtures
# or backend/requirements-agent.txt.
#
# Steps
#   1. scripts/bundle_backend.sh -> backend/fixtures_bundle (skipped with --skip-bundle).
#   2. Stage infra/build/agent/ (gitignored through infra/.gitignore): agent_app.py, agent/, returns_domain/,
#      fixtures_bundle/ without personal_agent/ (the owner's private demo memory never ships to the ALDO agent)
#      and requirements.txt generated from backend/requirements-agent.txt. No tests, __pycache__ or .venv. The
#      toolkit state (.bedrock_agentcore*) survives between runs: it holds the runtime id and the arm64 dependency zip.
#      The staged tree must import on its own (agent_app and every returns_domain module the agent can load).
#   3. Code bucket aldo-demo-agent-code-<account>-us-east-1 (created on first use, public access blocked).
#   4. agentcore configure, then agentcore deploy -auc with every --env flag. An update is a full replace in the
#      service, so every deploy repeats the whole environment.
#   5. Verify: runtime and DEFAULT endpoint READY, environment exactly as intended, and the uploaded deployment.zip
#      (size, dependencies present, source present, no tests/.venv/__pycache__, every .so is ELF aarch64).
#   6. SSM /aldo-demo/runtime-arn = runtime ARN (the worker reads it; no CDK redeploy needed).
#
# The dependency trap: `agentcore deploy` ignores `configure -rf`. It reads only a file literally named
# requirements.txt in the source directory; without it the zip silently has no dependencies (0 MB).
#
# Runtime environment (all non-secret): TABLE_NAME, AWS_REGION, DEMO_NOW (read from the deployed worker Lambda, else
# returns_domain.config), BEDROCK_MODEL_ID, BEDROCK_FALLBACK_MODEL_ID, FIXTURES_DIR, LOG_LEVEL, MAX_ROUNDS,
# GATEWAY_TRANSPORT. Deliberately NOT set: SECRETS_ARN, GATEWAY_BASE_URL or any key. The runtime role has no secret
# or SSM access; the API and the worker deliver the size write-back, the agent's own flush only logs a warning.
#
# Teardown: never run `agentcore destroy` here. It deletes the execution role given with -er, which is the CDK-owned
# AldoReturnsDemoAgentRuntimeRole. Delete the runtime with `aws bedrock-agentcore-control delete-agent-runtime` instead.
#
# AWS only through scripts/with_aws_env.sh. Credentials are never printed. Region us-east-1 only.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$ROOT/backend"
INFRA="$ROOT/infra"
STAGE="$INFRA/build/agent"
LOCK_DIR="$INFRA/build/.deploy_agent.lock"
HELPER="$ROOT/scripts/with_aws_env.sh"
OUTPUTS="$INFRA/outputs.json"
AGENTCORE_BIN="$BACKEND/.venv/bin/agentcore"
VENV_PYTHON="$BACKEND/.venv/bin/python"

REGION="us-east-1"
RUNTIME_NAME="aldo_returns_agent"            # the worker role only allows aldo_returns_agent-*
RUNTIME_ARN_PARAM="/aldo-demo/runtime-arn"
PACKAGE_ROOT="/var/task"                     # AgentCore unzips the package here and puts it first on sys.path
MODEL_ID="us.anthropic.claude-sonnet-5-5"
FALLBACK_MODEL_ID="us.anthropic.claude-opus-5-5"
DEMO_NOW_PATTERN='^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})$'

SKIP_BUNDLE=false
ENV_ONLY=false
DRY_RUN=false
for arg in "$@"; do
  case "$arg" in
    --skip-bundle) SKIP_BUNDLE=true ;;
    --env-only) ENV_ONLY=true ;;
    --dry-run) DRY_RUN=true ;;
    -h|--help) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "deploy_agent: unknown argument: $arg" >&2; exit 2 ;;
  esac
done

say() { printf 'deploy_agent: %s\n' "$*"; }
die() { printf 'deploy_agent: %s\n' "$*" >&2; exit 1; }
phase() { say "[${SECONDS}s] $*"; }

# ------------------------------------------------------------------------------------------ python tool
# One embedded helper for everything that needs boto3 (control plane calls and the zip check). It runs with the
# backend venv's python, through scripts/with_aws_env.sh.
read -r -d '' PY_TOOL <<'PY' || true
import json
import os
import sys
import time
import zipfile

REGION = os.environ["REGION"]
NAME = os.environ["RUNTIME_NAME"]


def control():
    import boto3

    return boto3.client("bedrock-agentcore-control", region_name=REGION)


def find_runtime(client):
    token = None
    while True:
        kwargs = {"maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        page = client.list_agent_runtimes(**kwargs)
        for item in page.get("agentRuntimes", []):
            if item.get("agentRuntimeName") == NAME:
                return item
        token = page.get("nextToken")
        if not token:
            return None


def wait_ready(client, runtime_id, min_version=0, timeout=480):
    """Wait for the runtime and its DEFAULT endpoint to be READY on a version >= min_version."""
    deadline = time.time() + timeout
    runtime, endpoint = {}, {}
    while time.time() < deadline:
        runtime = client.get_agent_runtime(agentRuntimeId=runtime_id)
        if runtime.get("status") in ("CREATE_FAILED", "UPDATE_FAILED"):
            sys.exit("runtime %s: %s" % (runtime["status"], runtime.get("failureReason", "no reason given")))
        if runtime.get("status") == "READY" and int(runtime.get("agentRuntimeVersion", 0)) >= int(min_version):
            try:
                endpoint = client.get_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName="DEFAULT")
            except client.exceptions.ResourceNotFoundException:
                endpoint = {}
            if endpoint.get("status") in ("CREATE_FAILED", "UPDATE_FAILED"):
                sys.exit("DEFAULT endpoint %s: %s" % (endpoint["status"], endpoint.get("failureReason", "no reason")))
            if endpoint.get("status") == "READY" and str(endpoint.get("liveVersion")) == str(runtime.get("agentRuntimeVersion")):
                return runtime, endpoint
        time.sleep(3)
    sys.exit("timed out waiting for READY: runtime %s, endpoint %s" % (runtime.get("status"), endpoint.get("status")))


def expected_env():
    return json.loads(os.environ["EXPECTED_ENV"])


def cmd_show():
    client = control()
    found = find_runtime(client)
    if not found:
        sys.exit("runtime %s not found" % NAME)
    runtime, endpoint = wait_ready(client, found["agentRuntimeId"])
    code = runtime["agentRuntimeArtifact"]["codeConfiguration"]
    s3 = code["code"]["s3"]
    print("runtime:    %s version %s, %s, DEFAULT endpoint %s" % (
        runtime["agentRuntimeId"], runtime["agentRuntimeVersion"], runtime["status"], endpoint["status"]))
    print("arn:        %s" % runtime["agentRuntimeArn"])
    print("artifact:   %s %s s3://%s/%s" % (code["runtime"], code["entryPoint"], s3["bucket"], s3["prefix"]))
    print("role:       %s" % runtime["roleArn"])
    print("log group:  /aws/bedrock-agentcore/runtimes/%s-DEFAULT" % runtime["agentRuntimeId"])
    want, got = expected_env(), runtime.get("environmentVariables") or {}
    wrong = sorted(k for k in want if got.get(k) != want[k])
    extra = sorted(k for k in got if k not in want)
    for key in sorted(got):
        print("  env       %s=%s" % (key, got[key]))
    if wrong or extra:
        sys.exit("environment differs from the intended one: wrong or missing %s, unexpected %s" % (wrong, extra))
    print("env:        %d variables, exactly as intended" % len(got))


def cmd_update_env():
    client = control()
    found = find_runtime(client)
    if not found:
        sys.exit("runtime %s does not exist yet; run scripts/deploy_agent.sh without --env-only first" % NAME)
    current = client.get_agent_runtime(agentRuntimeId=found["agentRuntimeId"])
    if current.get("status") != "READY":
        sys.exit("runtime is %s, not READY; try again in a minute" % current.get("status"))
    accepted = set(client.meta.service_model.operation_model("UpdateAgentRuntime").input_shape.members)
    params = {key: value for key, value in current.items() if key in accepted and key != "clientToken"}
    params["environmentVariables"] = expected_env()      # full replace: this is the whole environment
    response = client.update_agent_runtime(**params)
    print("update_agent_runtime accepted: same artifact, role and network, new environment (new version %s)"
          % response["agentRuntimeVersion"])
    wait_ready(client, found["agentRuntimeId"], min_version=response["agentRuntimeVersion"])


def check_zip(path):
    problems = []
    zipped_mb = os.path.getsize(path) / 1048576
    required_files = [
        "agent_app.py", "requirements.txt", "agent/__init__.py", "agent/runner.py", "agent/tools.py",
        "returns_domain/__init__.py", "returns_domain/config.py", "returns_domain/store.py",
        "fixtures_bundle/demo/policy.json", "fixtures_bundle/demo/logistics.json", "fixtures_bundle/derived/fit_insights.json",
    ]
    required_packages = ["strands", "bedrock_agentcore", "boto3", "botocore", "pydantic", "pydantic_core",
                         "aws_lambda_powertools", "tzdata", "starlette", "uvicorn"]
    ours = ("agent/", "returns_domain/", "fixtures_bundle/")
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        unzipped_mb = sum(info.file_size for info in infos) / 1048576
        top = {name.split("/", 1)[0] for name in names}
        missing_files = [f for f in required_files if f not in names]
        missing_packages = [p for p in required_packages if p not in top]
        bad = [n for n in names if "__pycache__" in n or n.endswith(".pyc")
               or n.startswith((".venv/", ".git/", ".bedrock_agentcore", "tests/", "personal_agent/"))]
        bad += [n for n in names if n.startswith(ours) and ("/tests/" in n or "/.pytest_cache/" in n or "/personal_agent/" in n)]
        libs, wrong_arch = [n for n in names if n.endswith(".so") or ".so." in n], []
        for lib in libs:
            with archive.open(lib) as handle:
                head = handle.read(20)
            machine = int.from_bytes(head[18:20], "little" if head[5:6] == b"\x01" else "big")
            if head[:4] != b"\x7fELF" or machine != 183:
                wrong_arch.append("%s (%s)" % (lib, "not ELF" if head[:4] != b"\x7fELF" else "e_machine %d" % machine))
    if zipped_mb < 5:
        problems.append("only %.2f MB zipped: the dependencies are missing (is requirements.txt in the source dir?)" % zipped_mb)
    if zipped_mb > 250 or unzipped_mb > 750:
        problems.append("over the limits (250 MB zipped, 750 MB unzipped)")
    if missing_files:
        problems.append("source files missing: %s" % missing_files)
    if missing_packages:
        problems.append("dependencies missing: %s" % missing_packages)
    if bad:
        problems.append("must not be in the package: %s" % bad[:10])
    if not libs:
        problems.append("no native libraries found: the wheels look wrong")
    if wrong_arch:
        problems.append("not linux arm64: %s" % wrong_arch[:10])
    print("package:    %.1f MB zipped, %.1f MB unzipped (limits 250 / 750), %d files" % (zipped_mb, unzipped_mb, len(names)))
    print("source:     %s" % ("agent_app.py, agent/, returns_domain/, fixtures_bundle/ present" if not missing_files else "INCOMPLETE"))
    print("deps:       %s" % (", ".join(required_packages) + " present" if not missing_packages else "INCOMPLETE"))
    print("native:     %d shared libraries, %s" % (len(libs), "all ELF aarch64" if not wrong_arch else "WRONG ARCHITECTURE"))
    print("clean:      %s" % ("no __pycache__, .venv, tests or personal_agent in our trees" if not bad else "NOT CLEAN"))
    for problem in problems:
        print("PROBLEM:    %s" % problem)
    sys.exit(1 if problems else 0)


COMMANDS = {"show": cmd_show, "update-env": cmd_update_env, "check-zip": lambda: check_zip(sys.argv[2])}
if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
    sys.exit("usage: show | update-env | check-zip <zip>")
COMMANDS[sys.argv[1]]()
PY

# ------------------------------------------------------------------------------------------ preflight
[ -x "$AGENTCORE_BIN" ] || die "$AGENTCORE_BIN not found. Install the backend environment: cd $BACKEND && uv sync"
[ -x "$VENV_PYTHON" ] || die "$VENV_PYTHON not found. Install the backend environment: cd $BACKEND && uv sync"
[ -r "$OUTPUTS" ] || die "$OUTPUTS not found. Deploy the stack first: infra/deploy.sh"
for tool in uv zip rsync python3; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is required (agentcore deploy builds the arm64 dependencies with uv and zips with zip)"
done
"$HELPER" aws sts get-caller-identity --region "$REGION" --query Account --output text >/dev/null 2>&1 \
  || die "AWS credentials are missing or expired. Refresh new.txt and run again (this script does not retry)."

read_output() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["AldoReturnsDemo"][sys.argv[2]])' "$OUTPUTS" "$1"
}
ROLE_ARN="$(read_output AgentRuntimeRoleArn)" || die "AgentRuntimeRoleArn is missing in $OUTPUTS"
WORKER_FN="$(read_output WorkerFunctionName)" || die "WorkerFunctionName is missing in $OUTPUTS"
TABLE_NAME="$(read_output TableName)" || die "TableName is missing in $OUTPUTS"
IFS=: read -r _ _ _ _ ACCOUNT _ <<<"$ROLE_ARN"
[[ "$ACCOUNT" =~ ^[0-9]{12}$ ]] || die "cannot read the account id from the runtime role ARN in $OUTPUTS"
BUCKET="aldo-demo-agent-code-${ACCOUNT}-${REGION}"
S3_KEY="$RUNTIME_NAME/deployment.zip"        # the toolkit uploads to <bucket>/<agent name>/deployment.zip

# DEMO_NOW: the value the deployed Lambdas run with, so the agent and the API agree on "today".
DEMO_NOW="$("$HELPER" aws lambda get-function-configuration --region "$REGION" --function-name "$WORKER_FN" \
  --query 'Environment.Variables.DEMO_NOW' --output text 2>/dev/null || true)"
if [[ "$DEMO_NOW" =~ $DEMO_NOW_PATTERN ]]; then
  DEMO_NOW_SOURCE="environment of the deployed Lambda $WORKER_FN"
else
  DEMO_NOW="$(cd "$BACKEND" && "$VENV_PYTHON" -c 'from returns_domain import config; print(config.DEFAULT_DEMO_NOW)')"
  DEMO_NOW_SOURCE="returns_domain.config.DEFAULT_DEMO_NOW (the Lambda environment could not be read)"
  [[ "$DEMO_NOW" =~ $DEMO_NOW_PATTERN ]] || die "DEMO_NOW has an unexpected format: $DEMO_NOW"
fi

# Runtime environment. `agentcore deploy --env` and update_agent_runtime replace the WHOLE environment.
ENV_VARS=(
  "TABLE_NAME=$TABLE_NAME"
  "AWS_REGION=$REGION"
  "DEMO_NOW=$DEMO_NOW"
  "BEDROCK_MODEL_ID=$MODEL_ID"
  "BEDROCK_FALLBACK_MODEL_ID=$FALLBACK_MODEL_ID"
  "FIXTURES_DIR=$PACKAGE_ROOT/fixtures_bundle"
  "LOG_LEVEL=INFO"
  "MAX_ROUNDS=8"
  "GATEWAY_TRANSPORT=http"
)
for kv in "${ENV_VARS[@]}"; do
  case "${kv%%=*}" in
    *SECRET*|*TOKEN*|*PASSWORD*|*API_KEY*|*ACCESS_KEY*|GATEWAY_BASE_URL)
      die "refusing to put ${kv%%=*} into the runtime environment (the runtime role has no secret access by design)" ;;
  esac
done
ENV_JSON="$(printf '%s\n' "${ENV_VARS[@]}" | python3 -c 'import json,sys; print(json.dumps(dict(l.rstrip("\n").split("=", 1) for l in sys.stdin if l.strip())))')"

aws_cli() { "$HELPER" aws --region "$REGION" "$@"; }
py_tool() { "$HELPER" env "REGION=$REGION" "RUNTIME_NAME=$RUNTIME_NAME" "EXPECTED_ENV=$ENV_JSON" "$VENV_PYTHON" -c "$PY_TOOL" "$@"; }

load_runtime() {   # sets RT_ID RT_ARN RT_VERSION RT_STATUS (empty when the runtime does not exist)
  local row
  row="$(aws_cli bedrock-agentcore-control list-agent-runtimes \
    --query "agentRuntimes[?agentRuntimeName=='$RUNTIME_NAME'].[agentRuntimeId,agentRuntimeArn,agentRuntimeVersion,status] | [0]" \
    --output text)"
  RT_ID=""; RT_ARN=""; RT_VERSION=""; RT_STATUS=""
  if [ -n "$row" ] && [ "$row" != "None" ]; then
    IFS=$'\t' read -r RT_ID RT_ARN RT_VERSION RT_STATUS <<<"$row"
  fi
}

bucket_exists() { aws_cli s3api head-bucket --bucket "$BUCKET" --expected-bucket-owner "$ACCOUNT" >/dev/null 2>&1; }

configure_args() {
  CONFIGURE_ARGS=(configure -e agent_app.py -n "$RUNTIME_NAME" -p HTTP -dt direct_code_deploy -rt PYTHON_3_12
    -r "$REGION" -er "$ROLE_ARN" -s3 "$BUCKET" -dm -do -ni)
  DEPLOY_ARGS=(deploy -a "$RUNTIME_NAME" -auc)
  local kv
  for kv in "${ENV_VARS[@]}"; do DEPLOY_ARGS+=(--env "$kv"); done
}
configure_args

# ------------------------------------------------------------------------------------------ dry run
if [ "$DRY_RUN" = true ]; then
  load_runtime
  say "DRY RUN: nothing is changed (read-only AWS calls only)"
  say "runtime name : $RUNTIME_NAME"
  if [ -n "$RT_ID" ]; then
    say "runtime      : exists ($RT_ID, version $RT_VERSION, $RT_STATUS) -> would be UPDATED in place"
  else
    say "runtime      : not found -> would be CREATED"
  fi
  say "role         : $ROLE_ARN"
  say "DEMO_NOW     : $DEMO_NOW (from the $DEMO_NOW_SOURCE)"
  say "environment  :"
  for kv in "${ENV_VARS[@]}"; do say "  $kv"; done
  if [ "$ENV_ONLY" = true ]; then
    say "mode         : --env-only: update_agent_runtime with the current artifact, role and network and the environment above"
    say "               (no bundling, no staging, no upload), then wait for READY and verify"
  else
    if bucket_exists; then say "code bucket  : $BUCKET (exists)"; else say "code bucket  : $BUCKET (would be created, public access blocked)"; fi
    if [ "$SKIP_BUNDLE" = true ]; then say "step 1       : skipped (--skip-bundle)"; else say "step 1       : $ROOT/scripts/bundle_backend.sh"; fi
    say "step 2       : stage $STAGE"
    say "               copy agent_app.py, agent/, returns_domain/, fixtures_bundle/ (without personal_agent/); no tests, __pycache__, .venv"
    say "               requirements.txt <- backend/requirements-agent.txt ($(grep -cvE '^[[:space:]]*(#|$)' "$BACKEND/requirements-agent.txt") pinned lines)"
    say "step 3       : cd $STAGE && agentcore ${CONFIGURE_ARGS[*]}"
    say "step 4       : cd $STAGE && agentcore ${DEPLOY_ARGS[*]}"
    say "step 5       : verify runtime, environment and s3://$BUCKET/$S3_KEY"
  fi
  say "step 6       : aws ssm put-parameter --name $RUNTIME_ARN_PARAM --type String --overwrite --value <runtime ARN>"
  exit 0
fi

# ------------------------------------------------------------------------------------------ lock and temp
mkdir -p "$INFRA/build"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  holder="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  if [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null; then
    die "another deploy_agent.sh is running (pid $holder); wait for it to finish"
  fi
  rm -rf "$LOCK_DIR"
  mkdir "$LOCK_DIR"
fi
echo "$$" > "$LOCK_DIR/pid"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/deploy_agent.XXXXXX")"
cleanup() { rm -rf "$LOCK_DIR" "$WORK"; }
trap cleanup EXIT

scrub() { grep -viE 'secret|token|access_?key|password' || true; }

run_toolkit() {    # run_toolkit <configure|deploy> <args...>: agentcore from the staging dir, output summarized
  local label="$1" log="$WORK/agentcore-$1.log"
  shift
  if ! (cd "$STAGE" && "$HELPER" env AGENTCORE_SUPPRESS_RECOMMENDATION=1 NO_COLOR=1 COLUMNS=200 "$AGENTCORE_BIN" "$label" "$@") >"$log" 2>&1; then
    echo "deploy_agent: agentcore $label failed. Last lines of its output:" >&2
    tail -n 40 "$log" | scrub >&2
    exit 1
  fi
  local keep
  keep="$(scrub <"$log" | grep -vE '^(╭|│|╰|├)' | grep -iE 'dependenc|package|upload|created|updat|cache|warn|error|fail|ready|success' | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//' | grep -v '^$' | uniq | head -n 24 || true)"
  [ -n "$keep" ] && printf '%s\n' "$keep" | sed 's/^/  agentcore: /'
  return 0
}

# ------------------------------------------------------------------------------------------ env only
load_runtime
if [ "$ENV_ONLY" = true ]; then
  [ -n "$RT_ID" ] || die "runtime $RUNTIME_NAME does not exist yet; run scripts/deploy_agent.sh without --env-only first"
  phase "replacing the environment of $RT_ID (version $RT_VERSION), package untouched"
  py_tool update-env
  phase "waiting for READY and checking the result"
  py_tool show
  load_runtime
else
  # ---------------------------------------------------------------------------------------- 1 bundle
  if [ "$SKIP_BUNDLE" = true ]; then
    [ -d "$BACKEND/fixtures_bundle/demo" ] || die "backend/fixtures_bundle is missing; run without --skip-bundle"
    phase "1/6 fixtures bundle: reusing $BACKEND/fixtures_bundle (--skip-bundle)"
  else
    phase "1/6 fixtures bundle"
    "$ROOT/scripts/bundle_backend.sh"
  fi

  # ---------------------------------------------------------------------------------------- 2 stage
  phase "2/6 staging $STAGE"
  case "$STAGE" in */infra/build/agent) ;; *) die "refusing to clean an unexpected path: $STAGE" ;; esac
  mkdir -p "$STAGE"
  for entry in "$STAGE"/* "$STAGE"/.[!.]*; do          # clean build: keep only the toolkit state
    [ -e "$entry" ] || continue
    case "${entry##*/}" in .bedrock_agentcore.yaml|.bedrock_agentcore) continue ;; esac
    rm -rf "$entry"
  done
  if [ -z "$RT_ID" ] && [ -f "$STAGE/.bedrock_agentcore.yaml" ]; then
    rm -f "$STAGE/.bedrock_agentcore.yaml"               # stale: it points at a runtime that no longer exists
  fi
  RSYNC=(rsync -a --exclude '__pycache__' --exclude '*.pyc' --exclude '.pytest_cache' --exclude 'tests' --exclude '.DS_Store')
  cp "$BACKEND/agent_app.py" "$STAGE/agent_app.py"
  "${RSYNC[@]}" "$BACKEND/agent" "$BACKEND/returns_domain" "$STAGE/"
  "${RSYNC[@]}" --exclude 'personal_agent' "$BACKEND/fixtures_bundle" "$STAGE/"
  {
    echo "# GENERATED by scripts/deploy_agent.sh from backend/requirements-agent.txt. Do not edit."
    echo "# agentcore deploy reads only a file named requirements.txt in the source directory (configure -rf is ignored)."
    grep -vE '^[[:space:]]*(#|$)' "$BACKEND/requirements-agent.txt" | sed -E 's/[[:space:]]+$//' || true
  } > "$STAGE/requirements.txt"
  pins="$(grep -cE '^[A-Za-z0-9_.-]+(\[[^]]*\])?==' "$STAGE/requirements.txt" || true)"
  [ "$pins" -ge 3 ] || die "requirements.txt has $pins pinned packages; check backend/requirements-agent.txt"
  for must in strands-agents== bedrock-agentcore== boto3==; do
    grep -q "^$must" "$STAGE/requirements.txt" || die "requirements.txt does not pin ${must%==}"
  done
  junk="$(find "$STAGE" -path "$STAGE/.bedrock_agentcore" -prune -o \
    \( -name '__pycache__' -o -name '*.pyc' -o -name 'tests' -o -name '.venv' -o -name '.pytest_cache' -o -name 'personal_agent' \) -print)"
  [ -z "$junk" ] || die "the staging directory is not clean: $junk"
  # The staged tree must import on its own (nothing the filters removed may be needed). Runs from the staging
  # directory, no AWS access, no bytecode written into it.
  if ! (cd "$STAGE" && PYTHONDONTWRITEBYTECODE=1 "$VENV_PYTHON" -c '
import importlib
import agent_app
from agent.services import SERVICE_NAMES
for name in sorted(SERVICE_NAMES):
    importlib.import_module("returns_domain." + name)
') >"$WORK/import-check.log" 2>&1; then
    tail -n 15 "$WORK/import-check.log" >&2
    die "the staged package does not import by itself (something it needs is missing from the staging copy)"
  fi
  say "  staged $(find "$STAGE" -path "$STAGE/.bedrock_agentcore" -prune -o -type f ! -name '.bedrock_agentcore.yaml' -print | wc -l | tr -d ' ') files, $pins pinned requirements (agent_app.py, agent/, returns_domain/, fixtures_bundle/)"

  # ---------------------------------------------------------------------------------------- 3 bucket
  phase "3/6 code bucket $BUCKET"
  if bucket_exists; then
    say "  exists"
  else
    aws_cli s3api create-bucket --bucket "$BUCKET" >/dev/null
    aws_cli s3api put-public-access-block --bucket "$BUCKET" \
      --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
    aws_cli s3api put-bucket-tagging --bucket "$BUCKET" --tagging 'TagSet=[{Key=project,Value=aldo-returns-demo}]'
    say "  created, public access blocked"
  fi

  # ---------------------------------------------------------------------------------------- 4 deploy
  if [ -n "$RT_ID" ]; then
    phase "4/6 agentcore configure + deploy (runtime exists: $RT_ID version $RT_VERSION -> update in place)"
  else
    phase "4/6 agentcore configure + deploy (runtime not found -> create)"
  fi
  run_toolkit configure "${CONFIGURE_ARGS[@]:1}"
  run_toolkit deploy "${DEPLOY_ARGS[@]:1}"

  # ---------------------------------------------------------------------------------------- 5 verify
  phase "5/6 verify the runtime and the uploaded package"
  py_tool show
  aws_cli s3api get-object --bucket "$BUCKET" --key "$S3_KEY" --expected-bucket-owner "$ACCOUNT" "$WORK/deployment.zip" >/dev/null
  py_tool check-zip "$WORK/deployment.zip"
  load_runtime
fi

# ------------------------------------------------------------------------------------------ 6 publish
[ -n "$RT_ARN" ] || die "the runtime ARN could not be read after the deploy"
phase "6/6 publish the ARN to SSM $RUNTIME_ARN_PARAM"
current="$(aws_cli ssm get-parameter --name "$RUNTIME_ARN_PARAM" --query Parameter.Value --output text 2>/dev/null || true)"
if [ "$current" = "$RT_ARN" ]; then
  say "  already holds this ARN"
else
  aws_cli ssm put-parameter --name "$RUNTIME_ARN_PARAM" --type String --overwrite --value "$RT_ARN" >/dev/null
  say "  written"
fi
say "done in ${SECONDS}s: $RUNTIME_NAME $RT_ID version $RT_VERSION ($RT_STATUS)"
say "runtime ARN: $RT_ARN"

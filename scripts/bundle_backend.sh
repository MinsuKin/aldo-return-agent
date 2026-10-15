#!/usr/bin/env bash
# Prepare backend deployment inputs. Run before `agentcore deploy` and before a real-code CDK deploy.
#
#   scripts/bundle_backend.sh
#       Copy fixtures/ and docs/impact/impact-model.json into backend/fixtures_bundle/ (gitignored):
#         fixtures_bundle/demo/*.json, derived/fit_insights.json, personal_agent/memory.json (if present),
#         fixtures_bundle/impact-model.json
#       A placeholder impact-model.json is written when the real one does not exist yet.
#       FIXTURES_DIR in the deployed Lambdas points at /var/task/fixtures_bundle.
#
#   scripts/bundle_backend.sh --lambda [--allow-stubs]
#       Also build infra/build/lambda (linux arm64, Python 3.12, no Docker): pinned dependencies from
#       backend/requirements-lambda.txt (else infra/requirements-lambda.txt), then backend/returns_domain,
#       backend/personal_agent, backend/api, backend/worker and fixtures_bundle.
#       Used by: cd infra && ../scripts/with_aws_env.sh npx cdk deploy -c useRealCode=true
#                (or simply: infra/deploy.sh --real-code)
#       --allow-stubs copies infra/stubs/{api,worker}/handler.py for a handler that does not exist in
#       backend/ yet (only for testing the bundle before the real handlers are written).
#
# dataset/ and docs/sources/ are never read. No AWS calls, no credentials.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$ROOT/backend"
BUNDLE="$BACKEND/fixtures_bundle"
LAMBDA_OUT="$ROOT/infra/build/lambda"

BUILD_LAMBDA=false
ALLOW_STUBS=false
for arg in "$@"; do
  case "$arg" in
    --lambda) BUILD_LAMBDA=true ;;
    --allow-stubs) ALLOW_STUBS=true ;;
    -h|--help) sed -n '2,22p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "bundle_backend: unknown argument: $arg" >&2; exit 2 ;;
  esac
done

copy_fixtures() {
  case "$BUNDLE" in
    */backend/fixtures_bundle) ;;
    *) echo "bundle_backend: refusing to clear unexpected path: $BUNDLE" >&2; exit 1 ;;
  esac
  rm -rf "$BUNDLE"
  mkdir -p "$BUNDLE"
  if [ -d "$ROOT/fixtures" ]; then
    rsync -a --exclude '.gitkeep' --exclude 'README.md' "$ROOT/fixtures/" "$BUNDLE/"
  else
    echo "bundle_backend: WARNING fixtures/ does not exist yet; bundle has no fixtures" >&2
  fi
  local impact="$ROOT/docs/impact/impact-model.json"
  local impact_state
  if [ -f "$impact" ]; then
    cp "$impact" "$BUNDLE/impact-model.json"
    impact_state="real docs/impact/impact-model.json"
  else
    printf '%s\n' '{"status":"placeholder","note":"docs/impact/impact-model.json did not exist when scripts/bundle_backend.sh ran. Run scripts/build_impact_model.py, then scripts/bundle_backend.sh and redeploy."}' > "$BUNDLE/impact-model.json"
    impact_state="PLACEHOLDER (real impact model not generated yet)"
  fi
  local count
  count="$(find "$BUNDLE" -type f | wc -l | tr -d ' ')"
  echo "fixtures_bundle: $count files in $BUNDLE; impact model: $impact_state"
}

build_lambda() {
  command -v uv >/dev/null 2>&1 || { echo "bundle_backend: uv is required" >&2; exit 1; }
  local req="$BACKEND/requirements-lambda.txt"
  [ -f "$req" ] || req="$ROOT/infra/requirements-lambda.txt"
  case "$LAMBDA_OUT" in
    */infra/build/lambda) ;;
    *) echo "bundle_backend: refusing to clear unexpected path: $LAMBDA_OUT" >&2; exit 1 ;;
  esac
  # Dependencies only change when the requirements file does: reuse them for fast code-only rebuilds.
  local want marker
  want="$(shasum -a 256 "$req" | cut -d' ' -f1)-aarch64-manylinux_2_28-cp312"
  marker="$LAMBDA_OUT/.deps-hash"
  if [ -f "$marker" ] && [ "$(cat "$marker")" = "$want" ]; then
    echo "lambda bundle: dependencies from $req unchanged, reusing them"
  else
    echo "lambda bundle: installing dependencies from $req (linux aarch64, cp312, wheels only)"
    rm -rf "$LAMBDA_OUT"
    mkdir -p "$LAMBDA_OUT"
    uv pip install --quiet --target "$LAMBDA_OUT" \
      --python-platform aarch64-manylinux_2_28 --python-version 3.12 --only-binary :all: \
      -r "$req"
    printf '%s' "$want" > "$marker"
  fi

  # Always refresh our own code from backend/ (drop the previous copies first).
  local pkg
  for pkg in returns_domain personal_agent api worker fixtures_bundle; do
    rm -rf "${LAMBDA_OUT:?}/$pkg"
  done
  for pkg in returns_domain personal_agent api worker; do
    if [ -d "$BACKEND/$pkg" ]; then
      rsync -a --exclude '__pycache__' --exclude '*.pyc' "$BACKEND/$pkg" "$LAMBDA_OUT/"
    fi
  done
  rsync -a "$BUNDLE" "$LAMBDA_OUT/"

  local handler_pkg
  for handler_pkg in api worker; do
    if [ ! -f "$LAMBDA_OUT/$handler_pkg/handler.py" ]; then
      if [ "$ALLOW_STUBS" = true ]; then
        mkdir -p "$LAMBDA_OUT/$handler_pkg"
        cp "$ROOT/infra/stubs/$handler_pkg/handler.py" "$LAMBDA_OUT/$handler_pkg/handler.py"
        echo "bundle_backend: WARNING backend/$handler_pkg/handler.py missing; bundled the STUB handler" >&2
      else
        echo "bundle_backend: backend/$handler_pkg/handler.py is missing (use --allow-stubs to test the bundle anyway)" >&2
        exit 1
      fi
    fi
  done

  # Prove the binary wheels are linux arm64 (Lambda arm64), not macOS.
  local so
  so="$(find "$LAMBDA_OUT/pydantic_core" -name '*.so' 2>/dev/null | head -1)"
  if [ -n "$so" ]; then
    if file "$so" | grep -q 'ARM aarch64'; then
      echo "lambda bundle: pydantic_core is ELF ARM aarch64 (ok)"
    else
      echo "bundle_backend: pydantic_core is NOT linux aarch64: $(file "$so")" >&2
      exit 1
    fi
  fi
  echo "lambda bundle: $(du -sh "$LAMBDA_OUT" | cut -f1) unzipped at $LAMBDA_OUT (limit 250 MB)"
}

copy_fixtures
if [ "$BUILD_LAMBDA" = true ]; then
  build_lambda
fi

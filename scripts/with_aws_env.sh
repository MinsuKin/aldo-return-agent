#!/usr/bin/env bash
# Run ONE command with AWS credentials from new.txt.
# Accepted line formats (the four AWS_* names only; other lines are ignored):
#   fish:   set -x AWS_ACCESS_KEY_ID "value"
#   bash:   export AWS_ACCESS_KEY_ID="value"   (also without export, with single quotes or no quotes)
# Usage: scripts/with_aws_env.sh aws sts get-caller-identity
# Re-reads new.txt on every call. Never prints values.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CRED="$ROOT/new.txt"
[ -r "$CRED" ] || { echo "with_aws_env: cannot read new.txt" >&2; exit 1; }
[ "$#" -gt 0 ] || { echo "usage: $0 command [args...]" >&2; exit 2; }
NAMES='AWS_DEFAULT_REGION|AWS_REGION|AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN'
CLEAN="$(tr -d '\r' < "$CRED")"
ENVSRC="$(
  printf '%s\n' "$CLEAN" | sed -n -E "s/^[[:space:]]*set[[:space:]]+-x[[:space:]]+(${NAMES})[[:space:]]+[\"']?([^\"']*)[\"']?[[:space:]]*\$/export \\1='\\2'/p"
  printf '%s\n' "$CLEAN" | sed -n -E "s/^[[:space:]]*(export[[:space:]]+)?(${NAMES})[[:space:]]*=[[:space:]]*[\"']?([^\"']*)[\"']?[[:space:]]*\$/export \\2='\\3'/p"
)"
[ -n "$ENVSRC" ] || { echo "with_aws_env: no AWS_* variables found in new.txt (expected export NAME=\"value\" or set -x NAME \"value\")" >&2; exit 1; }
eval "$ENVSRC"
unset ENVSRC CLEAN NAMES
exec "$@"

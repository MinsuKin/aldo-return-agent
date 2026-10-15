#!/usr/bin/env bash
# Run ONE command with AWS credentials from new.txt (fish `set -x NAME "value"` lines).
# Usage: scripts/with_aws_env.sh aws sts get-caller-identity
# Re-reads new.txt on every call. Never prints values.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CRED="$ROOT/new.txt"
[ -r "$CRED" ] || { echo "with_aws_env: cannot read new.txt" >&2; exit 1; }
[ "$#" -gt 0 ] || { echo "usage: $0 command [args...]" >&2; exit 2; }
ENVSRC="$(sed -n -E 's/^[[:space:]]*set[[:space:]]+-x[[:space:]]+(AWS_DEFAULT_REGION|AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN)[[:space:]]+"?([^"]*)"?[[:space:]]*$/export \1=\x27\2\x27/p' "$CRED")"
eval "$ENVSRC"
unset ENVSRC
exec "$@"

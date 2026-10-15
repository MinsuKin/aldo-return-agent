#!/usr/bin/env bash
# Build the linux arm64 / Python 3.12 Lambda bundle (infra/build/lambda). Same as: scripts/bundle_backend.sh --lambda
# Extra arguments are passed through (for example --allow-stubs).
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/bundle_backend.sh" --lambda "$@"

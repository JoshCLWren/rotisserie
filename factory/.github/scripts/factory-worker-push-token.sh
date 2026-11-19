#!/usr/bin/env bash
# Resolve factory credentials for push/PR creation and readable handoffs.
# Worker 48 uses its GitHub App installation token when configured.
# Every other worker prints PR_REBASE_TOKEN for resolve, and default for
# resolve-handoff. Never creates a GitHub App. Never posts trusted markers.
set -Eeuo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$here/factory_worker_github_app.py" "$@"

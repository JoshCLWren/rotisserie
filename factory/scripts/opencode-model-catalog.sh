#!/usr/bin/env bash
# Dump one OpenCode CLI provider catalog for factory discovery.
#
# This is the same command the factory runner and model scout use:
#   opencode models <provider> [--refresh] [--verbose]
# NVIDIA presence is the OpenCode nvidia list, not integrate.api.nvidia.com.

set -Eeuo pipefail

usage() {
  cat <<'USAGE'
Usage: opencode-model-catalog.sh PROVIDER [verbose|plain] [refresh|no-refresh]

PROVIDER is an OpenCode provider id (opencode, nvidia, openrouter).
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

provider="${1:?provider required}"
mode="${2:-verbose}"
refresh="${3:-refresh}"
binary="${OPENCODE_BIN:-opencode}"

command -v "$binary" >/dev/null 2>&1 || {
  printf 'ERROR: OpenCode CLI not found: %s\n' "$binary" >&2
  exit 1
}

# OpenCode 2 exposes an unfiltered model list and has no V1 --refresh or
# --verbose flags. Keep the provider argument for callers; the shared Python
# parser filters the returned provider/model selectors.
if "$binary" --version 2>/dev/null | grep -Eq '(^|[[:space:]])v?2[.]'; then
  exec "$binary" models --standalone
fi

# Legacy OpenCode 1 compatibility for local factory tools.
args=(models "$provider")
if [[ "$refresh" == "refresh" ]]; then
  args+=(--refresh)
fi
if [[ "$mode" == "verbose" ]]; then
  args+=(--verbose)
fi
exec "$binary" "${args[@]}"

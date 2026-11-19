#!/usr/bin/env bash
# OpenCode 2 refreshes its bundled catalog asynchronously on a cold runner.
# Poll for the exact selected model before inference; never change providers/models.
set -Eeuo pipefail
model="${1:?exact provider/model selector required}"
binary="${OPENCODE_BIN:-opencode}"
deadline=$((SECONDS + 45))
catalog_readable=0
while ((SECONDS < deadline)); do
  remaining=$((deadline - SECONDS))
  request_timeout=$((remaining < 10 ? remaining : 10))
  if catalog="$(timeout --signal=TERM --kill-after=1s "${request_timeout}s" \
    "$binary" models --standalone)"; then
    # A zero-byte successful response is not an authoritative model inventory.
    if [[ -n "${catalog//[[:space:]]/}" ]]; then
      catalog_readable=1
    fi
    if grep -Fxq -- "$model" <<<"$catalog"; then
      printf 'OpenCode catalog ready for %s\n' "$model"
      exit 0
    fi
  fi
  if ((SECONDS < deadline)); then
    echo "Waiting for OpenCode catalog to include $model" >&2
    sleep 1
  fi
done
printf 'OpenCode catalog did not expose %s within 45 seconds\n' "$model" >&2
if (( catalog_readable == 1 )); then
  # The runtime provided a readable model inventory, but the chosen route was
  # missing throughout the full refresh window. Do not retry this model as a
  # generic controller fault. The trusted caller records model_unavailable.
  exit 3
fi
# Every catalog command failed: this is a control-plane problem, not evidence
# that the selected model is retired. Preserve retry eligibility.
exit 1

#!/usr/bin/env bash
set -Eeuo pipefail

TIMEOUT_SECONDS="${1:?timeout seconds required}"
TITLE="${2:?title required}"
PROMPT="${3:?prompt required}"
LOG_PATH="${4:?log path required}"
RUNTIME_MODEL="${FACTORY_RUNTIME_MODEL:-kilo/kilo-auto/free}"

# This experiment must prove anonymous/free Kilo routing. Do not inherit an
# account token or local Kilo state that could make a paid route available.
unset KILO_API_KEY KILOCODE_API_KEY
runtime_root="$(mktemp -d "${RUNNER_TEMP:?RUNNER_TEMP is required}/kilo-runtime.XXXXXX")"
export XDG_CONFIG_HOME="$runtime_root/config"
export XDG_DATA_HOME="$runtime_root/data"
export XDG_CACHE_HOME="$runtime_root/cache"
mkdir -p "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_CACHE_HOME"

set +e
timeout --signal=TERM --kill-after=30s "${TIMEOUT_SECONDS}s" \
  kilo run -m "$RUNTIME_MODEL" --auto --format json --dir "${GITHUB_WORKSPACE:?GITHUB_WORKSPACE is required}" \
  --title "$TITLE" "$PROMPT" \
  2>&1 | tee "$LOG_PATH"
status=${PIPESTATUS[0]}
set -e

if (( status != 0 )); then
  exit "$status"
fi

# Normalize every JSON object we can recover from the mixed stdout/stderr log
# into JSONL. Kilo currently streams one JSON object per line, but using a
# streaming decoder also handles pretty-printed/multiline JSON without turning
# a healthy run into a false telemetry failure.
events_jsonl="$runtime_root/events.jsonl"
python3 - "$LOG_PATH" "$events_jsonl" <<'PY'
import json
import sys
from pathlib import Path

source = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
out = Path(sys.argv[2])
decoder = json.JSONDecoder()
objects = []
index = 0
while index < len(source):
    start = source.find("{", index)
    if start < 0:
        break
    try:
        value, consumed = decoder.raw_decode(source[start:])
    except json.JSONDecodeError:
        index = start + 1
        continue
    if isinstance(value, dict):
        objects.append(value)
    index = start + consumed
out.write_text("".join(json.dumps(obj, separators=(",", ":")) + "\n" for obj in objects), encoding="utf-8")
PY

step_count="$(jq -s '[.[] | select(.type == "step_finish")] | length' "$events_jsonl")"

# The smoke's job is only to prove that anonymous Kilo Auto Free returns the
# requested sentinel and leaves the worktree untouched. A text-only completion
# must not be rejected merely because a particular CLI build omits step_finish.
# Real factory invocations still require telemetry so experiments remain auditable.
telemetry_required=true
if [[ "$PROMPT" == *'KILO_AUTO_FREE_OK'* ]]; then
  telemetry_required=false
fi
if [[ "$telemetry_required" == true && "$step_count" == "0" ]]; then
  echo 'Kilo factory run returned success without observable step_finish telemetry' >&2
  exit 85
fi

# Paid execution is forbidden. If Kilo exposes any step cost, inspect it even
# during the relaxed smoke path and fail closed on a non-zero value.
if jq -e 'select(.type == "step_finish") | select((.part.cost // 0) > 0)' "$events_jsonl" >/dev/null; then
  echo 'Kilo Auto Free reported non-zero cost; refusing any paid execution path' >&2
  exit 86
fi

telemetry="$(jq -r '
  select(.type == "step_finish")
  | [
      (.part.model.providerID // "unreported"),
      (.part.model.modelID // "unreported"),
      (.part.generationID // "unreported"),
      ((.part.cost // "unreported") | tostring),
      ((.part.time.elapsed // "unreported") | tostring)
    ]
  | @tsv
' "$events_jsonl" | tail -n 1)"

if [[ -n "$telemetry" ]]; then
  IFS=$'\t' read -r provider model generation cost elapsed <<< "$telemetry"
  printf 'Kilo telemetry: requested_route=kilo-auto/free runtime_model=%s provider_id=%s resolved_model=%s generation_id=%s cost=%s elapsed_ms=%s\n' \
    "$RUNTIME_MODEL" "$provider" "$model" "$generation" "$cost" "$elapsed"
else
  echo 'Kilo telemetry: requested_route=kilo-auto/free resolved metadata unavailable'
fi

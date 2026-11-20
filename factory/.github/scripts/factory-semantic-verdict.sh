#!/usr/bin/env bash
set -Eeuo pipefail

FACTORY_SEMANTIC_STATUS_APPROVED='semantic_review_approved'
FACTORY_SEMANTIC_STATUS_BLOCKED='semantic_review_blocked'
FACTORY_SEMANTIC_STATUS_RECOVERED='semantic_review_verdict_recovered'
FACTORY_SEMANTIC_STATUS_MISSING='semantic_review_missing_verdict'
FACTORY_SEMANTIC_STATUS_CONFLICTING='semantic_review_conflicting_verdict'
FACTORY_SEMANTIC_STATUS_RECOVERY_FAILED='semantic_review_recovery_failed'
FACTORY_SEMANTIC_STATUS_TIMEOUT='semantic_review_timeout'
FACTORY_SEMANTIC_STATUS_MODEL_ERROR='semantic_review_model_error'
FACTORY_SEMANTIC_STATUS_HEAD_CHANGED='semantic_review_head_changed'

factory_strip_ansi() {
  sed -E $'s/\x1B\[[0-9;?]*[ -\/]*[@-~]//g'
}

factory_normalize_terminal_line() {
  local line="$1"
  line="$(printf '%s' "$line" | factory_strip_ansi)"
  line="$(sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//' <<< "$line")"
  line="$(sed -E 's/^(\*\*|__|`)//; s/(\*\*|__|`)$//' <<< "$line")"
  printf '%s\n' "$line"
}

factory_terminal_marker() {
  local log_file="$1"
  [[ -f "$log_file" ]] || return 1
  factory_strip_ansi < "$log_file" \
    | awk 'BEGIN { fenced=0 } /^[[:space:]]*(```|~~~)/ { fenced = !fenced; next } !fenced { print }' \
    | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//' \
    | sed -E 's/^(\*\*|__|`)//; s/(\*\*|__|`)$//' \
    | grep -E '^FACTORY_GATE_(READY|BLOCKED|NOT_READY|REJECT)$' \
    | tail -n 1
}

factory_verdict_from_marker() {
  local marker="$1"
  case "$marker" in
    FACTORY_GATE_READY) printf '%s\n' approve ;;
    FACTORY_GATE_BLOCKED|FACTORY_GATE_NOT_READY) printf '%s\n' repair ;;
    FACTORY_GATE_REJECT) printf '%s\n' reject ;;
    *) return 1 ;;
  esac
}

factory_extract_semantic_verdict() {
  local log_file="$1" marker
  marker="$(factory_terminal_marker "$log_file" || true)"
  [[ -n "$marker" ]] || return 1
  factory_verdict_from_marker "$marker"
}

factory_review_has_conflicting_terminal_markers() {
  local log_file="$1" markers
  [[ -f "$log_file" ]] || return 1
  markers="$(factory_strip_ansi < "$log_file" \
    | awk 'BEGIN { fenced=0 } /^[[:space:]]*(```|~~~)/ { fenced = !fenced; next } !fenced { print }' \
    | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//' \
    | sed -E 's/^(\*\*|__|`)//; s/(\*\*|__|`)$//' \
    | grep -E '^FACTORY_GATE_(READY|BLOCKED|NOT_READY|REJECT)$' \
    | sort -u || true)"
  [[ "$(wc -l <<< "$markers" | tr -d ' ')" -gt 1 ]]
}

factory_review_is_substantive() {
  local log_file="$1" bytes
  [[ -f "$log_file" ]] || return 1
  bytes="$(tr -d '[:space:]' < "$log_file" | wc -c | tr -d ' ')"
  (( bytes >= 80 ))
}

factory_primary_review_denies_ready_recovery() {
  local log_file="$1" marker
  [[ -f "$log_file" ]] || return 0
  marker="$(factory_terminal_marker "$log_file" || true)"
  case "$marker" in
    FACTORY_GATE_BLOCKED|FACTORY_GATE_NOT_READY|FACTORY_GATE_REJECT) return 0 ;;
    *) return 1 ;;
  esac
}

factory_sanitize_review_log() {
  local input="$1" output="$2"
  factory_strip_ansi < "$input" \
    | sed -E 's#https://x-access-token:[^@[:space:]]+@github\.com/#https://x-access-token:[REDACTED]@github.com/#g' \
    | sed -E 's/(github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9_]+)/[REDACTED_GITHUB_TOKEN]/g' \
    | sed -E 's/(Authorization:[[:space:]]*Bearer[[:space:]]+)[^[:space:]]+/\1[REDACTED]/Ig' \
    > "$output"
}

factory_has_acceptance_report() {
  local review_log="$1" acceptance_script="$2" mission_text="${3:-}"
  python3 - "$review_log" "$acceptance_script" "$mission_text" <<'PY'
import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("factory_issue_acceptance", sys.argv[2])
if spec is None or spec.loader is None:
    raise SystemExit(1)
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)
report = policy.report_from_text(Path(sys.argv[1]).read_text(encoding="utf-8"))
if report is None:
    raise SystemExit(1)
if sys.argv[3] and not policy.report_matches_mission(report, sys.argv[3]):
    raise SystemExit(1)
PY
}

factory_append_recovered_acceptance_report() {
  local primary_log="$1" recovery_log="$2" acceptance_script="$3" expected_head="$4"
  local mission_text="${5:-}"
  python3 - "$primary_log" "$recovery_log" "$acceptance_script" "$expected_head" "$mission_text" <<'PY'
import importlib.util
import json
import sys
from pathlib import Path

primary_path, recovery_path = Path(sys.argv[1]), Path(sys.argv[2])
spec = importlib.util.spec_from_file_location("factory_issue_acceptance", sys.argv[3])
if spec is None or spec.loader is None:
    raise SystemExit(1)
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)

primary = primary_path.read_text(encoding="utf-8")
existing = policy.report_from_text(primary)
if existing is not None:
    # A report already in the primary output must not be overwritten or
    # hidden by recovery, even if incomplete; fail closed instead.
    if sys.argv[5] and not policy.report_matches_mission(existing, sys.argv[5]):
        raise SystemExit(1)
    raise SystemExit(0)
if any(line.startswith(policy.PREFIX) for line in primary.splitlines()):
    # Malformed/multiple primary reports are not a blank slot for replacement.
    raise SystemExit(1)
recovered = policy.report_from_text(recovery_path.read_text(encoding="utf-8"))
if recovered is None or recovered.get("head") != sys.argv[4]:
    raise SystemExit(1)
if sys.argv[5] and not policy.report_matches_mission(recovered, sys.argv[5]):
    # A bare JSON object or an A1-only/foreign-revision report cannot count.
    raise SystemExit(1)
# This is only independent model evidence. The trusted controller still
# validates issue revision, criteria, changed files, checks, and provenance.
with primary_path.open("a", encoding="utf-8") as target:
    target.write("\n" + policy.PREFIX + json.dumps(recovered, separators=(",", ":")) + "\n")
PY
}

factory_recover_semantic_verdict() {
  local runtime_model="$1" workspace="$2" timeout_seconds="$3" recovery_log="$4"
  local pr="$5" head="$6" acceptance_mission="$7" status=0 prompt
  [[ "$pr" =~ ^[1-9][0-9]*$ && "$head" =~ ^[0-9a-f]{40}$ ]] || return 2
  prompt="Perform a fresh bounded, independent semantic review of ComicPile pull request #${pr} at exact head ${head}. Do not inspect or approve a different PR/head. Inspect the diff, issue, code and tests; do not edit code or push. Use gh pr view ${pr} --json ... for PR details, never gh pr status <number>. Review every Greptile (greptile-apps[bot]) finding, including unresolved review threads and the PR-body summary. Approve only if EVERY Greptile finding was either fixed in code or independently rebutted with concrete evidence; otherwise the verdict must be FACTORY_GATE_BLOCKED and never FACTORY_GATE_READY. PR-Agent advice is not independent approval. ${acceptance_mission} For READY, give the complete single-line FACTORY_ACCEPTANCE_V1 JSON record independently grounded in the changed files and observed checks, followed by the final bare line FACTORY_GATE_READY. If the report cannot be substantiated or any criterion is missing, do not invent evidence: finish with FACTORY_GATE_BLOCKED. OUTPUT EXACTLY ONE acceptance record as a single line beginning with the literal FACTORY_ACCEPTANCE_V1= at column zero. A bare JSON object, an A1-only report when the mission has more criteria, or a template containing unverified placeholders is NOT an acceptance report. Count every A1..An against the issued mission before returning READY. The final line must be exactly one bare verdict marker."
  # Never toggle errexit here: a trailing `set -e` re-enabled it inside the
  # caller's `set +e` window, so a timed-out recovery (124) killed the whole
  # worker before it could release the PR or record its outcome (run 44071823893).
  # OpenCode 2 rejects opencode run --dir; run from the workspace instead.
  ( cd "$workspace" && exec timeout --signal=TERM --kill-after=15s "${timeout_seconds}s" \
    opencode run -m "$runtime_model" --agent build --auto \
    --title "ComicPile PR ${pr} semantic verdict recovery" "$prompt"
  ) 2>&1 | tee "$recovery_log" || status=${PIPESTATUS[0]}
  return "$status"
}

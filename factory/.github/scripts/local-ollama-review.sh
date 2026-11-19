#!/usr/bin/env bash
set -euo pipefail

# Trusted provider-backed review. Inference may come from Ollama or another
# OpenAI-compatible provider, but only the controller's github-actions[bot]
# attestation can affect factory state. External comments never drive the queue.
PR="${PR_INPUT:-${PR_PAYLOAD:-}}"
[ -n "$PR" ] || { echo "no PR input" >&2; exit 1; }
: "${REVIEW_PROVIDER_URL:=${LOCAL_OLLAMA_URL:-}}"
: "${REVIEW_PROVIDER_KEY:=${LOCAL_OLLAMA_KEY:-ollama}}"
: "${REVIEW_PROVIDER_MODEL:=qwen2.5-coder:7b}"
[ -n "${REVIEW_PROVIDER_URL:-}" ] || { echo "REVIEW_PROVIDER_URL secret missing" >&2; exit 1; }

gh_retry() {
  for attempt in 1 2 3; do
    "$@" && return 0
    echo "gh call failed (attempt $attempt), retrying in 10s" >&2
    sleep 10
  done
  return 1
}

# Resolve the current target and linked issue through the canonical policy.
# Do not infer a fallback issue, consume inference on a merged PR, or steal leases.
gh_retry gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR}" > /tmp/local-review-target.json
target="$(python3 .github/scripts/local_review_contract.py target /tmp/local-review-target.json)"
HEAD="$(jq -er .head <<< "$target")"
ISSUE="$(jq -er .issue <<< "$target")"
NON_CLOSING="$(jq -er '.non_closing | if type == "boolean" then tostring else error("non_closing must be boolean") end' <<< "$target")"
echo "Local review: open PR #${PR}, exact head ${HEAD}, issue #${ISSUE}"

gh_retry gh pr diff "$PR" > /tmp/p.patch
# Preserve the entire trusted diff; bound only the model-facing excerpt.

# Build the prompt from the authoritative issue rubric and exact changed paths.
# The model must return the complete structured report; this script never
# fabricates acceptance items, citations, evidence, or test names.
python3 - "$HEAD" "$ISSUE" "$PR" "$REVIEW_PROVIDER_MODEL" <<'PY'
import importlib.util
import json
import os
import subprocess
import sys

head, issue, pr, model = sys.argv[1:]
sys.path.insert(0, ".github/scripts")
import local_review_contract as contract
spec = importlib.util.spec_from_file_location("factory_issue_acceptance", ".github/scripts/factory_issue_acceptance.py")
assert spec and spec.loader
acceptance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acceptance)
repo = os.environ["GITHUB_REPOSITORY"]
body = json.loads(subprocess.check_output(["gh", "api", f"repos/{repo}/issues/{issue}"], text=True))["body"]
pr_data = json.loads(subprocess.check_output(["gh", "api", f"repos/{repo}/pulls/{pr}"], text=True))
changed = [path for path in subprocess.check_output(["gh", "pr", "diff", pr, "--name-only"], text=True).splitlines() if path]
checks = subprocess.run(
    ["gh", "pr", "checks", pr],
    text=True,
    capture_output=True,
    check=False,
).stdout
prompt = (
    "You are the independent semantic reviewer for a factory PR. Inspect the exact PR head and "
    "decide whether every issue criterion is actually satisfied by this diff. Do not infer or "
    "fabricate evidence. Return one complete JSON object, not Markdown. Set verdict "
    "to APPROVE only if every acceptance item is justified; otherwise use REJECT "
    "with actionable findings in reason. For BOTH APPROVE and REJECT, include a reason "
    "string of at least 30 characters explaining concrete, independently checked evidence; "
    "an empty or generic reason is invalid. An APPROVE must include acceptance with "
    "issue (integer), head (exact SHA), issue_digest (exact digest), and items "
    "for EVERY rubric criterion A1 through the last. Each item needs id, status=pass, "
    "file (one changed path), evidence (specific behavior, 30+ characters), and "
    "test (named check with observed result). Keep output concise but complete. "
    "Never invent file paths, test outcomes or acceptance evidence. "
    "Use the exact issue number, full head and digest below.\n\nIssue rubric:\n" +
    acceptance.rubric(body) + "\n\nChanged paths:\n" + "\n".join(changed) +
    "\n\nExact head: " + head + "\nIssue: " + issue +
    "\nIssue digest: " + acceptance.digest(body) +
    "\n\nPR title:\n" + pr_data.get("title", "") +
    "\n\nPR body (authoritative validation claims; verify them against the diff):\n" +
    pr_data.get("body", "")[:8000] +
    "\n\nCurrent CI checks for the exact PR head:\n" + checks[:6000] +
    "\n\nPatch evidence (full when short; across-file changed-line excerpts "
    "when oversized, with omissions marked):\n" +
    contract.review_patch_excerpt(open("/tmp/p.patch", encoding="utf-8").read())
)
json.dump(contract.make_request(model, prompt, len(acceptance.requirements(body))), open("/tmp/req.json", "w"))
PY

# Complete JSON responses only. Transport failures and incomplete model output
# are separate: retry incomplete output once, without inventing evidence.
# Allow a very slow local 7B model a full two hours per HTTP attempt while
# recording the actual network timing. A Cloudflare 524 may still terminate
# the connection first; the curl metrics distinguish that from client timeout.
review_valid=false
for semantic_attempt in 1 2; do
  delivered=false
  for transport_attempt in 1 2 3; do
    curl_exit=0
    if curl -fsS --connect-timeout 60 --max-time 7200 -X POST "${REVIEW_PROVIDER_URL%/}/v1/chat/completions" \
      -H "Authorization: Bearer ${REVIEW_PROVIDER_KEY}" -H "Content-Type: application/json" \
      --data-binary @/tmp/req.json \
      --write-out 'http=%{http_code} connect_s=%{time_connect} tls_s=%{time_appconnect} first_byte_s=%{time_starttransfer} total_s=%{time_total} bytes=%{size_download}\n' \
      --output /tmp/resp.json > /tmp/curl.metrics 2>/tmp/curl.err; then
      delivered=true
    else
      curl_exit=$?
    fi
    metrics="$(tr -d '\n' < /tmp/curl.metrics)"
    printf '[local-review-timing] semantic_attempt=%s transport_attempt=%s curl_exit=%s %s\n' \
      "$semantic_attempt" "$transport_attempt" "$curl_exit" "$metrics"
    if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
      printf -- '- Semantic attempt %s, HTTP attempt %s: curl exit %s, %s\n' \
        "$semantic_attempt" "$transport_attempt" "$curl_exit" "$metrics" >> "$GITHUB_STEP_SUMMARY"
    fi
    if [[ "$delivered" == true ]]; then
      break
    fi
    if [[ "$metrics" == http=524\ * ]]; then
      echo "::warning::HTTP 524 came from the upstream proxy before the 7200-second client timeout" >&2
    fi
    echo "Provider transport attempt $transport_attempt failed" >&2
    cat /tmp/curl.err >&2
    if [[ "$transport_attempt" != 3 ]]; then sleep 10; fi
  done
  if [[ "$delivered" != true ]]; then
    echo "::error::Review provider unavailable after three timed attempts" >&2
    exit 1
  fi

  if python3 .github/scripts/local_review_contract.py response /tmp/resp.json \
    > /tmp/local-review-payload.json &&
    python3 - "$HEAD" "$ISSUE" "$PR" "$NON_CLOSING" <<'PY'
import importlib.util
import json
import os
import subprocess
import sys

head, issue, pr, non_closing = sys.argv[1:]
spec = importlib.util.spec_from_file_location(
    "factory_issue_acceptance", ".github/scripts/factory_issue_acceptance.py"
)
assert spec and spec.loader
acceptance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acceptance)
repo = os.environ["GITHUB_REPOSITORY"]
body = json.loads(subprocess.check_output(
    ["gh", "api", f"repos/{repo}/issues/{issue}"], text=True
))["body"]
payload = json.load(open("/tmp/local-review-payload.json", encoding="utf-8"))
verdict = payload["verdict"]
reason = payload["reason"]
report = payload.get("acceptance")
if verdict == "APPROVE":
    changed = subprocess.check_output(
        ["gh", "pr", "diff", pr, "--name-only"], text=True
    ).splitlines()
    errors = acceptance.validate(
        report, int(issue), head, body, changed, non_closing=non_closing == "true"
    )
    if errors:
        raise SystemExit("invalid provider acceptance report: " + "; ".join(errors))
log = (
    f"### Factory semantic review · Factory 77 · {verdict}\n"
    f"Exact head {head}.\n{reason}\n"
    f"# comic-pile-factory-authoritative-diff-evidence\ngh pr diff {pr}\n"
)
if verdict == "APPROVE":
    log += "FACTORY_ACCEPTANCE_V1=" + json.dumps(report, separators=(",", ":")) + "\n"
open("/tmp/opencode-factory-77.log", "w", encoding="utf-8").write(log)
print(f"Validated {verdict} from local provider for issue #{issue}")
PY
  then
    review_valid=true
    break
  fi
  if [[ "$semantic_attempt" == 2 ]]; then
    echo "::error::Local reviewer did not produce complete valid JSON after two attempts" >&2
    exit 1
  fi
  echo "Incomplete semantic review; retrying once with a larger output budget" >&2
  jq '.max_tokens = 8192' /tmp/req.json > /tmp/local-review-retry.json
  mv /tmp/local-review-retry.json /tmp/req.json
done
[[ "$review_valid" == true ]] || exit 1

# Check the current GitHub head and ownership again after CPU inference.
gh_retry gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR}" > /tmp/local-review-current.json
python3 .github/scripts/local_review_contract.py target \
  /tmp/local-review-current.json --expected-head "$HEAD" > /dev/null

# A reject returns the PR to repair; it is not authority to close the PR.
controller_verdict=approve
if [[ "$(jq -er .verdict /tmp/local-review-payload.json)" == REJECT ]]; then
  controller_verdict=repair
fi

# Only the trusted controller posts the review attestation as github-actions[bot].
gh pr edit "$PR" --add-label "factory:review,factory:77" >/dev/null 2>&1 || true
epoch="$(date +%s)"
echo "<!-- comic-pile-factory-review-claim-v2:pr-${PR}:opencode-free-model-factory-77:${epoch} -->" > /tmp/claim.txt
gh issue comment "$PR" --body-file /tmp/claim.txt >/dev/null
# The controller alone authorizes readiness. A model verdict or an existing
# factory:approved label must never trigger a merge on its own.
review_result="$(python3 .github/scripts/factory-review-controller.py review --worker 77 --pr "$PR" \
  --reviewed-head "$HEAD" --verdict "$controller_verdict" --review-log /tmp/opencode-factory-77.log)"
printf '%s\n' "$review_result"

# Wake the existing canonical merge drain immediately after exact-head readiness.
# It rechecks authorization, required CI, dependencies, main health, and head SHA
# before merging, and retains sole ownership of post-merge closure/deployment.
if jq -e --arg head "$HEAD" '.status == "ready" and .head == $head' \
  <<< "$review_result" >/dev/null; then
  echo "PR #$PR approved at $HEAD; dispatching canonical merge drain"
  gh_retry gh workflow run factory-ready-merge-drain.yml --repo "$GITHUB_REPOSITORY" --ref main
fi

"""Log evidence patterns for the fixed-model run classifier."""

from __future__ import annotations

import re

CANONICAL_OUTCOME_CLASSES = frozenset(
    {
        "success",
        "no_work",
        "work_failure",
        "agent_timeout",
        "provider_failure",
        "provider_throttle",
        "provider_quota",
        "model_unavailable",
        "model_retired_410",
        "model_policy_violation",
        "environment_failure",
        "control_plane_failure",
        "unknown_failure",
    }
)

LOCK_RE = re.compile(
    r"Factory (?P<worker>\d+) locked: source=(?P<source>\S+) model=(?P<model>\S+) "
    r"runtime=(?P<runtime>\S+) minute=:(?P<minute>\d+) scheduler=(?P<scheduler>\S+) "
    r"configured=(?P<configured>true|false)",
    re.IGNORECASE,
)
TARGET_RE = re.compile(r"checked out (?P<kind>issue|pr) #(?P<number>\d+) on ")
# Account-level quota/budget exhaustion. Unlike a transient throttle this does
# not clear within minutes, so it gets its own class (and a quota pause).
QUOTA_RE = re.compile(
    r"budget[ _]exhausted|payment required|(?:http(?: status)?|status(?: code)?)[ :]+402\b|"
    r"(?:monthly|daily|weekly|free) usage limit(?: reached)?|"
    r"reached your [a-z ]*usage limit|"
    r"insufficient[ _](?:credits|balance|quota)|exceeded your current quota|out of credits|"
    r"quota[ _](?:exceeded|exhausted)|api key budget limit exceeded|"
    r"free-models-per-day-high-balance|"
    r"rate limit exceeded: free-models-per-day[^\\s]*",
    re.I,
)
RATE_LIMIT_RE = re.compile(
    r"(?:http(?: status)?|status(?: code)?|error(?: code)?)[\s:=]+429\b|"
    r"\b429 too many requests\b|too many requests|rate.?limit|\bquota\b|throttl|"
    r"(?:at|out of|no|insufficient|exceeded)\s+capacity|"
    r"capacity\s+(?:exceeded|exhausted|unavailable)|"
    r"(?:http(?: status)?|status(?: code)?|error(?: code)?)[\s:=]+413\b|"
    r"\b413 request (?:too )?large\b|"
    r"request too large|tokens per minute|\btpm\b",
    re.I,
)
MODEL_MISSING_RE = re.compile(
    r"pinned .*model is not currently (?:exposed|invokable)|unknown model|model .*not found|"
    r"model .*not available|"
    r"model .*does not exist|(?:http(?: status)?|status(?: code)?)[ :]+(?:404|410)\b|"
    r"\b(?:404 not found|410 gone)\b",
    re.I,
)
NATIVE_INTENT_RE = re.compile(r"^auto/", re.I)
BACKING_UNAVAILABLE_RE = re.compile(
    r"model\s+'([^']+)'\s+is not available|"
    r"([\w./:-]+):\s*model\s+[—\-]\s*.*not available",
    re.I,
)
TIMEOUT_RE = re.compile(r"timed? out|timeout|exit status 124|process completed with exit code 124", re.I)
SESSION_TIMEOUT_RE = re.compile(
    r"exit status 124|process completed with exit code 124",
    re.I,
)
# Trust the actual work-session exit as the model proof. No separate prompt.
WORK_SESSION_OK_RE = re.compile(
    r"\[factory:(?P<worker>\d+)\](?:\[[^\]\n]+\])*\s+"
    r"agent exit status 0 for (?P<kind>issue|pr) #(?P<number>\d+)\b", re.I,
)
NO_ASSIGNMENT_RE = re.compile(
    r"\[factory:\d+\].*no control-plane assignment is leased to this worker", re.I,
)
AGENT_TIMEOUT_RE = re.compile(
    r"\[factory:\d+\](?:\[[^\]\n]+\])*\s+agent exit status 124 for (?:issue|pr) #\d+\b",
    re.I,
)
NVIDIA_PROBE_FAILED_RE = re.compile(r"pinned nvidia model probe failed", re.I)
CANCEL_RE = re.compile(
    r"operation was canceled|operation was cancelled|cancellation requested|job was canceled|job was cancelled",
    re.I,
)
PROCESS_FAILURE_RE = re.compile(
    r"process completed with exit code [1-9][0-9]*|command failed with exit code [1-9][0-9]*",
    re.I,
)
PROVIDER_RE = re.compile(
    r"provider error|service unavailable|bad gateway|gateway timeout|"
    r"(?:http(?: status)?|status(?: code)?)[\s:=]+(?:502|503|504)\b|"
    r"\b(?:502 bad gateway|503 service unavailable|504 gateway timeout)\b|"
    r"econnreset|connection reset|model probe failed|failed the opencode compatibility probe",
    re.I,
)
MODEL_INTERRUPTION_RE = re.compile(
    r"stream (?:interrupted|aborted|closed)|inference abort(?:ed)?|"
    r"unexpected end of (?:stream|input)|incomplete (?:chunked read|response)",
    re.I,
)
MODEL_POLICY_RE = re.compile(
    r"content policy|model policy|safety policy|policy[_ -]?violation|"
    r"(?:model|provider) .* rejected .* policy|not allowed .* policy",
    re.I,
)
TRUSTED_GUARD_RE = re.compile(
    r"(?:trusted )?(?:policy|guard) (?:blocked|rejected|denied)|"
    r"(?:blocked|rejected|denied) by (?:trusted )?(?:policy|guard)",
    re.I,
)
CONTROL_PLANE_RE = re.compile(
    r"factory[-_ ](?:work|review|completion)[-_ ]controller|"
    r"controller-assignment-read-failed|"
    r"(?:lease|state[- ]machine|lifecycle|dispatch(?:er)?) (?:error|failed|failure|exception)",
    re.I,
)
WORK_FAILURE_RE = re.compile(
    r"(?:tests?|checks?|lint|coverage|mechanical (?:merge )?gate) (?:failed|failing|failure)|"
    r"actionable (?:review )?findings|semantic blockers remain|"
    r'^\d{4}-\d{2}-\d{2}T\S+Z \{"head": "[a-f0-9]{40}", '
    r'"producer": "\d+", "status": "implementation-evidence-required"\}',
    re.I | re.M,
)
ENVIRONMENT_RE = re.compile(
    r"checkout failed|failed to (?:checkout|install)|no space left on device|"
    r"docker: error response|(?:git|runner|tool installation) (?:failed|failure)",
    re.I,
)
NO_CHANGE_RE = re.compile(
    r"no (?:useful |persisted )?(?:changes?|diff)|nothing to commit|"
    r"no-change repair|repair attempt produced no",
    re.I,
)

PRODUCTIVE_SIGNALS = (
    "opened/updated PR #",
    "pushed repairs to PR #",
    "all exact-head gates passed for PR #",
)

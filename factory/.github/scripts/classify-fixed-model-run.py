#!/usr/bin/env python3
"""Classify one completed fixed-model factory run from its Actions job log."""

from __future__ import annotations

import argparse
import json
import re
import sys
import unittest
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from classify_fixed_model_patterns import (  # noqa: E402
    AGENT_TIMEOUT_RE,
    BACKING_UNAVAILABLE_RE,
    CANCEL_RE,
    CANONICAL_OUTCOME_CLASSES,
    CONTROL_PLANE_RE,
    ENVIRONMENT_RE,
    LOCK_RE,
    MODEL_INTERRUPTION_RE,
    MODEL_MISSING_RE,
    MODEL_POLICY_RE,
    NATIVE_INTENT_RE,
    NO_ASSIGNMENT_RE,
    NO_CHANGE_RE,
    NVIDIA_PROBE_FAILED_RE,
    PROCESS_FAILURE_RE,
    PRODUCTIVE_SIGNALS,
    PROVIDER_RE,
    QUOTA_RE,
    RATE_LIMIT_RE,
    SESSION_TIMEOUT_RE,
    TARGET_RE,
    TIMEOUT_RE,
    TRUSTED_GUARD_RE,
    WORK_FAILURE_RE,
    WORK_SESSION_OK_RE,
    provider_retry_outcome,
)

__all__ = ["CANONICAL_OUTCOME_CLASSES", "Result", "classify", "provider_retry_outcome"]
SELF_TEST_MODULES = ("test_classify_fixed_model_run", "test_classify_fixed_model_run_provider")


def _delivered_pr(log: str, worker: str, target: dict[str, str]) -> str:
    """Find an actual wrapper PR delivery followed by the lease handoff."""
    number = target["number"]
    if not worker or not number:
        return ""
    prefix = rf"\[factory:{re.escape(worker)}\](?:\[[^\]\n]+\])*\s+"
    if target["kind"] == "issue":
        pattern = prefix + r"opened/updated PR #(?P<pr>\d+) for issue #(?P<issue>\d+)"
        for match in re.finditer(pattern, log):
            if match["issue"] != number:
                continue
            tail = log[match.end():]
            released_issue = re.search(
                prefix + rf"released issue #{number} to factory:unowned\b", tail
            )
            released_pr = re.search(
                prefix + rf"released pr #{match['pr']} to factory:unowned\b", tail
            )
            if released_issue and released_pr and released_issue.start() < released_pr.start():
                return match["pr"]
    elif target["kind"] == "pr":
        pattern = prefix + r"pushed repairs to PR #(?P<pr>\d+); handing it to the merge controller"
        for match in re.finditer(pattern, log):
            if match["pr"] != number:
                continue
            tail = log[match.end():]
            if re.search(prefix + rf"released pr #{number} to factory:unowned\b", tail):
                return number
    return ""

@dataclass(frozen=True)
class Result:
    """Structured classification of a fixed-model factory run."""

    worker: str = ""
    source: str = ""
    model: str = ""
    runtime_model: str = ""
    minute: str = ""
    configured: bool = False
    exact_model_proven: bool = False
    selected_kind: str = ""
    selected_number: str = ""
    persisted_work: bool = False
    delivery_outcome: str = "none"
    delivery_pr: str = ""
    outcome: str = "NOT YET PROVEN"
    outcome_class: str = "unknown_failure"
    detail: str = "factory job did not expose enough runtime evidence"


def _is_native_intent(model: str) -> bool:
    """Return True when the selected model is an OmniRoute intent selector."""
    return bool(NATIVE_INTENT_RE.match(model or ""))


def _backing_unavailable_for_intent(log: str, selected_model: str) -> bool:
    """True when a backing model (not the intent) is reported unavailable."""
    if not _is_native_intent(selected_model):
        return False
    for match in BACKING_UNAVAILABLE_RE.finditer(log):
        named = next((g for g in match.groups() if g), "")
        if not named:
            continue
        # Ignore when the unavailable name is the selected intent itself.
        if named == selected_model or named.endswith("/" + selected_model):
            continue
        if selected_model not in named:
            return True
    # Fallback: generic "not available" alongside a native intent lock, but
    # without naming the intent — treat as backing-route exhaustion.
    if MODEL_MISSING_RE.search(log) and selected_model not in log.split("not available", 1)[0][-80:]:
        # Prefer explicit backing-name matches above; this is a soft signal only
        # when the unavailable phrase does not quote the intent id nearby.
        return "not available in the active live catalog" in log.lower()
    return False


def _runtime_evidence(log: str) -> str:
    """Keep executed runner diagnostics, not echoed commands or agent telemetry.

    Opaque IDs in OpenCode structured events can contain `413` or `429`,
    while tool output can quote unrelated quota documentation. Neither proves
    the model provider returned a rate limit. Only a structured error's
    message (not its metadata) counts as error evidence.
    """
    evidence = []
    for line in log.splitlines():
        if "\x1b[36;1m" in line:
            continue
        payload = re.sub(r"^\d{4}-\d{2}-\d{2}T\S+Z\s+", "", line).lstrip()
        if payload.startswith(("* [new branch]", "remotes/origin/")):
            continue
        if payload.startswith("{"):
            try:
                event = json.loads(payload)
            except ValueError:
                # A truncated OpenCode trace is untrusted, not a provider
                # response. Real sessions can emit enormous partial tool JSON.
                continue
            else:
                if isinstance(event, dict) and isinstance(event.get("type"), str):
                    if event["type"] == "error":
                        detail = event.get("error") or event.get("message") or ""
                        if isinstance(detail, dict):
                            detail = detail.get("message") or ""
                        if isinstance(detail, str) and detail.strip():
                            evidence.append(detail)
                    # The remainder is agent text, tool reads, timing and
                    # session metadata, not provider response evidence.
                    continue
        evidence.append(line)
    return "\n".join(evidence)

def classify(log: str) -> Result:
    """Classify a fixed-model factory Actions log into a structured outcome.

    Args:
        log: Full text of the factory job log.

    Returns:
        Result describing worker, model, and terminal outcome.

    """
    log = _runtime_evidence(log)
    lock = LOCK_RE.search(log)
    if not lock:
        return Result()

    values = lock.groupdict()
    configured = values["configured"].lower() == "true"
    target_matches = list(TARGET_RE.finditer(log))
    target = target_matches[-1].groupdict() if target_matches else {"kind": "", "number": ""}
    work_session = WORK_SESSION_OK_RE.search(log)
    exact_proven = (
        "FIXED_MODEL_OPENCODE_OK" in log  # historical jobs
        or bool(
            work_session and work_session["worker"] == values["worker"]
            and work_session["kind"] == target["kind"]
            and work_session["number"] == target["number"]
        )
    )
    persisted = any(signal in log for signal in PRODUCTIVE_SIGNALS)
    delivered_pr = _delivered_pr(log, values['worker'], target) if persisted else ''

    common = {
        "worker": values["worker"],
        "source": values["source"],
        "model": values["model"],
        "runtime_model": values["runtime"],
        "minute": values["minute"],
        "configured": configured,
        "exact_model_proven": exact_proven,
        "selected_kind": target["kind"],
        "selected_number": target["number"],
        "persisted_work": persisted,
        "delivery_outcome": "persisted" if delivered_pr else "none",
        "delivery_pr": delivered_pr,
    }

    if not configured:
        return Result(
            **common,
            outcome="NOT YET PROVEN",
            detail="lane was not configured, so the pinned model was never invoked",
        )

    if NO_ASSIGNMENT_RE.search(log):
        return Result(
            **common,
            outcome="HEALTHY / IDLE",
            outcome_class="no_work",
            detail="no assignment was leased; model inference was skipped",
        )

    # An actual assigned NVIDIA invocation returned HTTP 410. No extra probe.
    if values["source"] == "nvidia" and re.search(
        r"(?m)^(?:\d{4}-\d{2}-\d{2}T\S+Z\s+)?NVIDIA permanently retired model: "
        + re.escape(values["model"]) + r"\s*$",
        log,
    ):
        return Result(
            **common, outcome="MODEL RETIRED", outcome_class="model_retired_410",
            detail="trusted assigned NVIDIA worker received permanent HTTP 410",
        )
    # TPM / request-too-large must win over backing-model "not available" noise
    # that OmniRoute emits after a 413 cascade for native intents.
    if QUOTA_RE.search(log):
        return Result(
            **common,
            outcome="PROVIDER QUOTA",
            outcome_class="provider_quota",
            detail="runtime evidence shows the provider account quota or budget is exhausted",
        )

    # A trusted local deadline is not a provider rate limit, even when
    # review prose or source code mentions quotas.
    if AGENT_TIMEOUT_RE.search(log):
        return Result(
            **common,
            outcome="AGENT DEADLINE",
            outcome_class="agent_timeout",
            detail="the trusted worker's agent deadline expired; delivery classified separately",
        )

    if RATE_LIMIT_RE.search(log):
        return Result(
            **common,
            outcome="PROVIDER THROTTLE",
            outcome_class="provider_throttle",
            detail="runtime evidence contains a provider rate-limit, quota, TPM, or capacity response",
        )

    if MODEL_MISSING_RE.search(log):
        if _backing_unavailable_for_intent(log, values["model"]):
            return Result(
                **common,
                outcome="PROVIDER FAILURE",
                outcome_class="provider_failure",
                detail="native intent remaining backing routes were unavailable; the intent itself is not missing",
            )
        return Result(
            **common,
            outcome="MODEL MISSING",
            outcome_class="model_unavailable",
            detail="the configured provider did not expose the pinned model",
        )

    # Useful persistence wins even if a later cleanup/provider call failed. The
    # run did useful work with the exact model, which is the fleet fact we care about.
    if persisted and exact_proven:
        return Result(
            **common,
            outcome="HEALTHY / PRODUCTIVE",
            outcome_class="success",
            detail="exact OpenCode model proof succeeded and useful work was persisted",
        )

    if MODEL_POLICY_RE.search(log):
        return Result(
            **common,
            outcome="MODEL POLICY VIOLATION",
            outcome_class="model_policy_violation",
            detail="explicit provider/model policy evidence rejected this model invocation",
        )

    if CONTROL_PLANE_RE.search(log):
        return Result(
            **common,
            outcome="CONTROL PLANE FAILURE",
            outcome_class="control_plane_failure",
            detail="factory controller, dispatcher, lease, or lifecycle execution failed",
        )

    if WORK_FAILURE_RE.search(log) or TRUSTED_GUARD_RE.search(log):
        return Result(
            **common,
            outcome="WORK FAILURE",
            outcome_class="work_failure",
            detail="code validation, review findings, or a trusted work guard requires additional work",
        )

    if NO_CHANGE_RE.search(log) and target["number"]:
        return Result(
            **common,
            outcome="NO WORK",
            outcome_class="no_work",
            detail="the selected target received no useful persisted change",
        )

    if ENVIRONMENT_RE.search(log):
        return Result(
            **common,
            outcome="NOT YET PROVEN" if not exact_proven else "ENVIRONMENT FAILURE",
            outcome_class="environment_failure",
            detail="worker checkout, tooling, runner, or execution environment failed",
        )

    if CANCEL_RE.search(log):
        return Result(
            **common,
            outcome="CONTROL PLANE FAILURE",
            outcome_class="control_plane_failure",
            detail="the factory job was cancelled before useful persistence",
        )

    if NVIDIA_PROBE_FAILED_RE.search(log):
        return Result(
            **common,
            outcome="PROVIDER FAILURE",
            outcome_class="provider_failure",
            detail="NVIDIA probe did not receive a successful HTTP response",
        )

    if MODEL_INTERRUPTION_RE.search(log) or (SESSION_TIMEOUT_RE.search(log) and exact_proven):
        return Result(
            **common,
            outcome="PROVIDER FAILURE",
            outcome_class="provider_failure",
            detail="the model invocation started but its provider response was interrupted or timed out",
        )

    if TIMEOUT_RE.search(log) and not exact_proven:
        return Result(
            **common,
            outcome="NOT YET PROVEN",
            outcome_class="unknown_failure",
            detail="execution timed out before exact model proof",
        )

    if exact_proven:
        if PROVIDER_RE.search(log):
            return Result(
                **common,
                outcome="PROVIDER FAILURE",
                outcome_class="provider_failure",
                detail="the model proved itself, then provider/runtime evidence failed before useful persistence",
            )
        if PROCESS_FAILURE_RE.search(log):
            return Result(
                **common,
                outcome="UNKNOWN FAILURE",
                outcome_class="unknown_failure",
                detail="the model proved itself, then the worker exited without causal failure evidence",
            )
        return Result(
            **common,
            outcome="HEALTHY / IDLE",
            outcome_class="no_work",
            detail="exact OpenCode model proof succeeded but no useful work was persisted",
        )

    # A smoke invocation reached OpenCode but did not produce the proof token.
    if "fixed-model smoke" in log or "Smoke exact pinned model through OpenCode" in log:
        return Result(
            **common,
            outcome="PROVIDER FAILURE",
            outcome_class="provider_failure",
            detail="the exact OpenCode model invocation ran but did not return the proof token",
        )

    # OmniRoute can prove provider/model exposure before OpenCode is reached. Keep
    # model-exposure failures above distinct, while setup failures remain unproven.
    if values["source"] == "omniroute-opencode":
        return Result(
            **common,
            outcome="NOT YET PROVEN",
            detail="OmniRoute setup ended before exact OpenCode model proof",
        )

    return Result(
        **common,
        outcome="NOT YET PROVEN",
        detail="execution ended before exact OpenCode model proof",
    )


def main() -> int:
    """CLI entrypoint for classification or self-tests.

    Returns:
        Process exit code (0 on success).

    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        loader = unittest.defaultTestLoader
        suite = unittest.TestSuite(loader.loadTestsFromName(name) for name in SELF_TEST_MODULES)
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
    if not args.log:
        parser.error("--log is required unless --self-test is used")
    print(
        json.dumps(
            asdict(classify(args.log.read_text(encoding="utf-8", errors="replace"))),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

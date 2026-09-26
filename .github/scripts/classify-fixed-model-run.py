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

CANONICAL_OUTCOME_CLASSES = frozenset(
    {
        "success",
        "no_work",
        "work_failure",
        "provider_failure",
        "provider_throttle",
        "model_unavailable",
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
RATE_LIMIT_RE = re.compile(
    r"(?:^|[^0-9.])429(?!\d)|too many requests|rate.?limit|\bquota\b|throttl|"
    r"(?:at|out of|no|insufficient|exceeded)\s+capacity|"
    r"capacity\s+(?:exceeded|exhausted|unavailable)|"
    r"(?:http(?: status)?|status(?: code)?)[ :]+413\b|(?:^|[^0-9.])413(?!\d)|"
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
    r"provider error|service unavailable|bad gateway|gateway timeout|502|503|504|"
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
    r"actionable (?:review )?findings|semantic blockers remain",
    re.I,
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


def classify(log: str) -> Result:
    """Classify a fixed-model factory Actions log into a structured outcome.

    Args:
        log: Full text of the factory job log.

    Returns:
        Result describing worker, model, and terminal outcome.

    """
    lock = LOCK_RE.search(log)
    if not lock:
        return Result()

    values = lock.groupdict()
    configured = values["configured"].lower() == "true"
    exact_proven = "FIXED_MODEL_OPENCODE_OK" in log
    target_matches = list(TARGET_RE.finditer(log))
    target = target_matches[-1].groupdict() if target_matches else {"kind": "", "number": ""}
    persisted = any(signal in log for signal in PRODUCTIVE_SIGNALS)

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
    }

    if not configured:
        return Result(
            **common,
            outcome="NOT YET PROVEN",
            detail="lane was not configured, so the pinned model was never invoked",
        )

    # TPM / request-too-large must win over backing-model "not available" noise
    # that OmniRoute emits after a 413 cascade for native intents.
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
    if "ComicPile fixed-model smoke" in log or "Smoke exact pinned model through OpenCode" in log:
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


class ClassifierTests(unittest.TestCase):
    """Unit tests for fixed-model log classification."""

    BASE = (
        "Factory 32 locked: source=omniroute-opencode model=oc/big-pickle "
        "runtime=omniroute/oc/big-pickle minute=:15 scheduler=slot-15 configured=true\n"
    )

    def assert_canonical(self, result: Result) -> None:
        """Assert one emitted class belongs to the durable factory contract."""
        self.assertIn(result.outcome_class, CANONICAL_OUTCOME_CLASSES)

    def test_productive(self) -> None:
        """Productive runs with proof token and persisted work are healthy."""
        result = classify(
            self.BASE
            + "FIXED_MODEL_OPENCODE_OK\n[factory:32] checked out issue #928 on x\n"
            + "opened/updated PR #1220 for issue #928\n"
        )
        self.assertEqual(result.outcome, "HEALTHY / PRODUCTIVE")
        self.assertEqual(result.outcome_class, "success")
        self.assertTrue(result.exact_model_proven)
        self.assertTrue(result.persisted_work)
        self.assertEqual((result.selected_kind, result.selected_number), ("issue", "928"))

    def test_idle_is_no_work(self) -> None:
        """Proof without selected or persisted work is canonical no_work."""
        result = classify(self.BASE + "FIXED_MODEL_OPENCODE_OK\nno selectable ordinary target\n")
        self.assertEqual(result.outcome, "HEALTHY / IDLE")
        self.assertEqual(result.outcome_class, "no_work")

    def test_rate_limited_is_provider_throttle(self) -> None:
        """Provider rate-limit responses map to canonical provider_throttle."""
        result = classify(self.BASE + "ComicPile fixed-model smoke\nError: Too Many Requests 429\n")
        self.assertEqual(result.outcome, "PROVIDER THROTTLE")
        self.assertEqual(result.outcome_class, "provider_throttle")

    def test_tpm_413_is_provider_throttle(self) -> None:
        """HTTP 413 TPM / request-too-large maps to provider_throttle."""
        result = classify(
            self.BASE
            + "ComicPile fixed-model smoke\n"
            + "Error: groq/openai/gpt-oss-20b: model — [413]: Request too large for model "
            + "`openai/gpt-oss-20b` on tokens per minute (TPM): Limit 8000, Requested 12027 "
            + "(HTTP 413)\n"
        )
        self.assertEqual(result.outcome, "PROVIDER THROTTLE")
        self.assertEqual(result.outcome_class, "provider_throttle")

    def test_native_intent_backing_unavailable_is_not_model_missing(self) -> None:
        """Backing catalog misses must not mark auto/coding:free as MODEL MISSING."""
        lock = (
            "Factory 68 locked: source=omniroute-free model=auto/coding:free "
            "runtime=omniroute/auto/coding:free minute=:30 scheduler=dispatcher configured=true\n"
        )
        result = classify(
            lock
            + "ComicPile fixed-model smoke\n"
            + "Error: groq/openai/gpt-oss-20b: model — [413]: Request too large (HTTP 413); "
            + "oc/north-mini-code-free: model — Model 'north-mini-code-free' is not available "
            + "in the active live catalog for provider 'opencode'. (HTTP 400)\n"
        )
        self.assertEqual(result.outcome_class, "provider_throttle")
        self.assertNotEqual(result.outcome, "MODEL MISSING")

    def test_native_intent_backing_only_unavailable_is_provider_failure(self) -> None:
        """Intent stays selectable when only a backing model is catalog-missing."""
        lock = (
            "Factory 68 locked: source=omniroute-free model=auto/coding:free "
            "runtime=omniroute/auto/coding:free minute=:30 scheduler=dispatcher configured=true\n"
        )
        result = classify(
            lock
            + "ComicPile fixed-model smoke\n"
            + "Model 'north-mini-code-free' is not available in the active live catalog "
            + "for provider 'opencode'. (HTTP 400)\n"
        )
        self.assertEqual(result.outcome_class, "provider_failure")
        self.assertNotEqual(result.outcome, "MODEL MISSING")

    def test_model_missing(self) -> None:
        """Missing pinned model exposure maps to MODEL MISSING."""
        self.assertEqual(
            classify(
                self.BASE + "Pinned OmniRoute model is not currently exposed: oc/big-pickle\n"
            ).outcome,
            "MODEL MISSING",
        )

    def test_nvidia_model_not_invokable_is_missing(self) -> None:
        """NVIDIA catalog entries that return 404 on invoke map to MODEL MISSING."""
        self.assertEqual(
            classify(
                self.BASE
                + "Pinned NVIDIA model is not currently invokable for this account: moonshotai/kimi-k2.6\n"
            ).outcome,
            "MODEL MISSING",
        )

    def test_explicit_http_model_retirement_is_unavailable(self) -> None:
        """HTTP 404 and 410 permanently identify an unavailable model only."""
        for response in (
            "HTTP 404",
            "HTTP status: 410",
            "410 Gone",
            "404 Not Found",
            "Model 'north-mini-code-free' is not available in the active live catalog",
        ):
            with self.subTest(response=response):
                result = classify(self.BASE + f"model invocation failed: {response}\n")
                self.assertEqual(result.outcome, "MODEL MISSING")
                self.assertEqual(result.outcome_class, "model_unavailable")

    def test_model_stream_interruption_is_provider_failure(self) -> None:
        """Interrupted provider responses use the canonical transient provider failure."""
        result = classify(self.BASE + "FIXED_MODEL_OPENCODE_OK\nstream interrupted\n")
        self.assertEqual(result.outcome_class, "provider_failure")

    def test_control_plane_failure_is_distinct(self) -> None:
        """Controller lifecycle exceptions are not attributed to the model."""
        result = classify(self.BASE + "FIXED_MODEL_OPENCODE_OK\nlifecycle exception\n")
        self.assertEqual(result.outcome_class, "control_plane_failure")

    def test_assignment_read_failure_is_control_plane(self) -> None:
        """Explicit controller assignment failures do not poison model health."""
        result = classify(
            self.BASE
            + "FIXED_MODEL_OPENCODE_OK\n"
            + "released pr #1897 (controller-assignment-read-failed)\n"
        )
        self.assertEqual(result.outcome_class, "control_plane_failure")

    def test_work_failure_is_distinct(self) -> None:
        """Genuine failing tests remain repair work."""
        result = classify(self.BASE + "FIXED_MODEL_OPENCODE_OK\ntests failed\n")
        self.assertEqual(result.outcome_class, "work_failure")

    def test_no_change_repair_is_no_work(self) -> None:
        """Selected repairs without a diff use canonical no_work."""
        result = classify(
            self.BASE
            + "FIXED_MODEL_OPENCODE_OK\nchecked out pr #123 on branch\nno changes\n"
        )
        self.assertEqual(result.outcome_class, "no_work")

    def test_unknown_failure_fails_closed(self) -> None:
        """Unexplained process failures never become provider failures."""
        result = classify(
            self.BASE + "FIXED_MODEL_OPENCODE_OK\nProcess completed with exit code 1\n"
        )
        self.assertEqual(result.outcome_class, "unknown_failure")

    def test_environment_failure_is_distinct(self) -> None:
        """Runner and checkout failures do not poison model health."""
        result = classify(self.BASE + "checkout failed\n")
        self.assertEqual(result.outcome_class, "environment_failure")

    def test_trusted_guard_is_work_scoped(self) -> None:
        """Internal work guards do not permanently poison a model."""
        result = classify(self.BASE + "trusted guard rejected unsafe repair\n")
        self.assertEqual(result.outcome_class, "work_failure")

    def test_explicit_model_policy_violation_is_model_scoped(self) -> None:
        """Explicit provider/model policy evidence uses the model-scoped class."""
        result = classify(self.BASE + "provider rejected request: content policy violation\n")
        self.assertEqual(result.outcome_class, "model_policy_violation")

    def test_timeout_after_proof_is_provider_failure(self) -> None:
        """A provider timeout after proof is a canonical provider failure."""
        result = classify(
            self.BASE + "FIXED_MODEL_OPENCODE_OK\nProcess completed with exit code 124\n"
        )
        self.assertEqual(result.outcome_class, "provider_failure")

    def test_cancellation_is_control_plane_failure(self) -> None:
        """Job cancellation is control-plane evidence, not provider health evidence."""
        result = classify(self.BASE + "FIXED_MODEL_OPENCODE_OK\nThe operation was canceled.\n")
        self.assertEqual(result.outcome_class, "control_plane_failure")

    def test_unconfigured(self) -> None:
        """Unconfigured lanes never invoke the model and stay unproven."""
        log = (
            "Factory 6 locked: source=nvidia model=z-ai/glm-5.2 "
            "runtime=nvidia/z-ai/glm-5.2 minute=:00 scheduler=slot-00 configured=false\n"
        )
        result = classify(log)
        self.assertEqual(result.outcome, "NOT YET PROVEN")
        self.assert_canonical(result)

    def test_omniroute_setup_failure_is_unproven(self) -> None:
        """OmniRoute setup failures before proof remain NOT YET PROVEN."""
        result = classify(self.BASE + "docker: Error response from daemon\n")
        self.assertEqual(result.outcome, "NOT YET PROVEN")
        self.assertEqual(result.outcome_class, "environment_failure")

    def test_failed_exact_smoke_is_provider_failure(self) -> None:
        """Smoke that reaches the provider without the proof token is a provider failure."""
        result = classify(
            self.BASE
            + "Smoke exact pinned model through OpenCode\nError: unexpected provider response\n"
        )
        self.assertEqual(result.outcome, "PROVIDER FAILURE")
        self.assertEqual(result.outcome_class, "provider_failure")

    def test_gha_timestamp_milliseconds_are_not_rate_limits(self) -> None:
        """Bare 429 inside GitHub Actions timestamps must not throttle a model."""
        nvidia = (
            "Factory 21 locked: source=nvidia model=google/gemma-4-31b-it "
            "runtime=nvidia/google/gemma-4-31b-it minute=:50 scheduler=dispatcher "
            "configured=true\n"
        )
        result = classify(
            nvidia
            + "2026-09-14T04:55:12.4291234Z Probe pinned NVIDIA model before OpenCode smoke\n"
            + "Pinned NVIDIA model probe failed HTTP 401\n"
        )
        self.assertNotEqual(result.outcome_class, "provider_throttle")
        self.assertEqual(result.outcome_class, "provider_failure")

    def test_capacity_script_echo_is_not_rate_limit(self) -> None:
        """Factory capacity wording in runner logs is not provider throttle evidence."""
        result = classify(
            self.BASE
            + "Configure selected OmniRoute capacity\n"
            + 'OmniRoute free-entry capacity: {"remaining": 2}\n'
            + "Smoke exact pinned model through OpenCode\n"
            + "Error: unexpected provider response\n"
        )
        self.assertNotEqual(result.outcome_class, "provider_throttle")
        self.assertEqual(result.outcome_class, "provider_failure")

    def test_model_at_capacity_is_provider_throttle(self) -> None:
        """Explicit provider capacity exhaustion remains provider_throttle."""
        result = classify(self.BASE + "ComicPile fixed-model smoke\nThe model is at capacity\n")
        self.assertEqual(result.outcome, "PROVIDER THROTTLE")
        self.assertEqual(result.outcome_class, "provider_throttle")

    def test_nvidia_probe_hard_fail_is_provider_failure(self) -> None:
        """Non-transient NVIDIA probe hard-fail is opaque without this class."""
        nvidia = (
            "Factory 21 locked: source=nvidia model=google/gemma-4-31b-it "
            "runtime=nvidia/google/gemma-4-31b-it minute=:50 scheduler=dispatcher "
            "configured=true\n"
        )
        result = classify(nvidia + "Pinned NVIDIA model probe failed HTTP 401\n")
        self.assertEqual(result.outcome, "PROVIDER FAILURE")
        self.assertEqual(result.outcome_class, "provider_failure")

    def test_legacy_http_000_probe_hard_fail_is_provider_failure(self) -> None:
        """HTTP 000 hard-fail plus curl timeout must not stay unknown_failure."""
        nvidia = (
            "Factory 21 locked: source=nvidia model=google/gemma-4-31b-it "
            "runtime=nvidia/google/gemma-4-31b-it minute=:50 scheduler=dispatcher "
            "configured=true\n"
        )
        result = classify(
            nvidia
            + "curl: (28) Operation timed out after 120000 ms with 0 bytes received\n"
            + "Pinned NVIDIA model probe failed HTTP 000\n"
        )
        self.assertEqual(result.outcome_class, "provider_failure")
        self.assertNotEqual(result.outcome_class, "unknown_failure")

    def test_probe_curl_timeout_then_smoke_proof_is_idle(self) -> None:
        """Retried HTTP 000 probe timeouts must not poison a later proof token."""
        nvidia = (
            "Factory 21 locked: source=nvidia model=google/gemma-4-31b-it "
            "runtime=nvidia/google/gemma-4-31b-it minute=:50 scheduler=dispatcher "
            "configured=true\n"
        )
        result = classify(
            nvidia
            + "curl: (28) Operation timed out after 45000 ms with 0 bytes received\n"
            + "NVIDIA probe transient HTTP 000 curl_exit 28 (attempt 1/3); retrying after 5s\n"
            + "NVIDIA probe succeeded for google/gemma-4-31b-it\n"
            + "FIXED_MODEL_OPENCODE_OK\n"
        )
        self.assertEqual(result.outcome, "HEALTHY / IDLE")
        self.assertEqual(result.outcome_class, "no_work")

    def test_all_representative_results_use_canonical_taxonomy(self) -> None:
        """Regression guard forbids legacy runtime outcome classes."""
        logs = (
            self.BASE + "FIXED_MODEL_OPENCODE_OK\nno selectable ordinary target\n",
            self.BASE + "FIXED_MODEL_OPENCODE_OK\ntests failed\n",
            self.BASE + "FIXED_MODEL_OPENCODE_OK\nprovider error 503\n",
            self.BASE + "Error 429 Too Many Requests\n",
            self.BASE + "Request too large tokens per minute (TPM) HTTP 413\n",
            self.BASE + "model invocation failed: HTTP 404\n",
            self.BASE + "content policy violation\n",
            self.BASE + "checkout failed\n",
            self.BASE + "FIXED_MODEL_OPENCODE_OK\nlifecycle failure\n",
            self.BASE + "FIXED_MODEL_OPENCODE_OK\nProcess completed with exit code 1\n",
        )
        for log in logs:
            with self.subTest(log=log):
                self.assert_canonical(classify(log))


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
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(ClassifierTests)
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

"""The worker's own terminal decision outranks phrases in agent tool output."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".github/scripts"
spec = importlib.util.spec_from_file_location(
    "factory_dynamic_outcome_terminal", SCRIPTS / "factory_dynamic_outcome.py"
)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

WORKER = "10001"
TAG = f"[factory:{WORKER}][source:opencode-free][model:a/b] "
TOOL_OUTPUT = (
    '{"number":5745,"title":"t","body":"Actionable review findings block progress"}\n'
    "error: test failed, to rerun pass `-p roll-service`\n"
    "e06ec6a03 Fix linting issues and test failures\n"
)


def session(*worker_lines: str, number: int = 5745) -> str:
    """Worker lines get this worker's tag; a leading "~" marks an untagged line."""
    head = (
        f"Factory {WORKER} assigned dynamic provider slot: source=opencode-free configured=true\n"
        f"{TAG}checked out pr #{number} on factory/work at {'a' * 40}\n"
        f"{TAG}FACTORY_EXECUTION_CANDIDATE: source=opencode-free model=a/b "
        "runtime=opencode-go/a/b\n" + TOOL_OUTPUT
    )
    return head + "".join(
        f"{line[1:]}\n" if line.startswith("~") else f"{TAG}{line}\n" for line in worker_lines
    )


def test_submitted_review_verdict_is_success_not_work_failure() -> None:
    """Run 45419093957: an approved review was counted as a model failure."""
    result = module.classify(
        session(
            "agent exit status 0 for pr #5745",
            "agent terminal token for PR #5745: FACTORY_GATE_READY",
            "semantic_review_approved: submitting approve semantic verdict for PR #5745",
            "assignment complete; remaining budget 4929s",
        )
    )
    assert result["outcome_class"] == "success"
    assert result["detail"] == "semantic review submitted its approve verdict"


def test_review_verdict_without_completion_is_not_promoted() -> None:
    result = module.classify(
        session(
            "agent exit status 0 for pr #5745",
            "semantic_review_blocked: submitting repair semantic verdict for PR #5745",
        )
    )
    assert result["outcome_class"] == "work_failure"


def test_repair_without_change_is_idle_even_when_tool_output_says_tests_failed() -> None:
    """Run 45412858234: a no-change repair was counted as a model failure."""
    result = module.classify(
        session(
            "agent exit status 0 for pr #5745",
            "repair attempt for PR #5745 found no persisted change and reports ready",
            "assignment complete; remaining budget 5078s",
        )
    )
    assert result["outcome_class"] == "no_work"


def test_scope_rejected_diff_is_a_work_failure_that_names_the_guard() -> None:
    """Run 45543140046: a merge-conflict diff was reset, then reported as no change."""
    result = module.classify(
        session(
            "agent exit status 0 for pr #5681",
            "rejecting out-of-scope pr #5681 diff before push (factory-control-out-of-scope)",
            "repair attempt for PR #5681 produced no persisted change; preserving",
            "assignment complete; remaining budget 5562s",
            number=5681,
        )
    )
    assert result["outcome_class"] == "work_failure"
    assert result["detail"] == (
        "the model's diff was rejected before push (factory-control-out-of-scope)"
    )


def test_fast_check_failure_names_the_failed_checks() -> None:
    """Run 45585302957: the detail now says which validation failed."""
    result = module.classify(
        session(
            "agent exit status 0 for pr #5777",
            "~factory fast checks failed: OpenAPI schema freshness (log: /tmp/x.log)",
            "fast checks still failed for pr #5777; retaining persisted edits (log: /tmp/x.log)",
            number=5777,
        )
    )
    assert result["outcome_class"] == "work_failure"
    assert result["detail"] == "validation failed: OpenAPI schema freshness"


def test_missing_verdict_detail_names_the_recovery_failure() -> None:
    result = module.classify(
        session(
            "agent exit status 0 for pr #5756",
            "semantic_review_recovery_failed: same-session recovery exited 124",
            "semantic_review_recovery_failed: review model did not produce a verdict",
            number=5756,
        )
    )
    assert result["outcome_class"] == "work_failure"
    assert result["detail"].endswith("; recovery: same-session recovery exited 124")


def test_explicit_terminal_line_wins_over_legacy_decision_lines() -> None:
    result = module.classify(
        session(
            "agent exit status 0 for pr #5745",
            "repair attempt for PR #5745 produced no persisted change; preserving",
            "FACTORY_TERMINAL_OUTCOME: class=work_failure detail=PR #5745 failed trusted checks",
        )
    )
    assert result["outcome_class"] == "work_failure"
    assert result["detail"] == "PR #5745 failed trusted checks"


def test_other_workers_decision_lines_are_ignored() -> None:
    other = "[factory:20002][source:opencode-free][model:a/b] "
    result = module.classify(
        session(
            "agent exit status 0 for pr #5745",
            f"~{other}semantic_review_approved: submitting approve semantic verdict for PR #5745",
            f"~{other}assignment complete; remaining budget 1s",
        )
    )
    assert result["outcome_class"] == "work_failure"


def _record(outcome: str, detail: str, env: str, tmp_path: Path) -> tuple[str, str]:
    script = (
        f'set -euo pipefail\nTERMINAL_OUTCOME_FILE="{tmp_path}/outcome"\n'
        f"log() {{ printf '[factory:1] %s\\n' \"$*\"; }}\n{env}\n"
        f'source "{SCRIPTS}/factory-worker-outcome.sh"\n'
        f'record_terminal_outcome {outcome} "{detail}"\n'
    )
    run = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
    return (tmp_path / "outcome").read_text(), run.stderr


def test_recorder_turns_a_scope_rejected_no_change_into_work_failure(tmp_path: Path) -> None:
    saved, logged = _record(
        "no_work",
        "repair produced no persisted change",
        "FACTORY_SCOPE_REJECTED_REASON=factory-control-out-of-scope",
        tmp_path,
    )
    assert saved.startswith("work_failure\t")
    assert "(factory-control-out-of-scope)" in saved
    assert "FACTORY_TERMINAL_OUTCOME: class=work_failure detail=" in logged


def test_recorder_appends_failed_fast_check_names(tmp_path: Path) -> None:
    saved, logged = _record(
        "work_failure",
        "PR #1 failed trusted checks; work retained",
        "FACTORY_FAST_CHECK_FAILURES='frontend typecheck;Rust format/Clippy/native build'",
        tmp_path,
    )
    assert saved.rstrip("\n").endswith(
        "; validation failed: frontend typecheck;Rust format/Clippy/native build"
    )
    assert module.worker_terminal(logged, "1") == (
        "work_failure",
        "PR #1 failed trusted checks; work retained; validation failed: "
        "frontend typecheck;Rust format/Clippy/native build",
    )


def test_entry_failure_message_carries_the_detail() -> None:
    entry = (ROOT.parent / ".github/workflows/free-model-factory-entry.yml").read_text()
    assert 'message="$message; what happened: $failure_detail"' in entry

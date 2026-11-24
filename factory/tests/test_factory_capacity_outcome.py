"""Capacity exits and provider 429 retries get their real outcome class."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".github/scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location(
    "factory_dynamic_outcome_capacity", SCRIPTS / "factory_dynamic_outcome.py"
)
assert spec and spec.loader
dynamic = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = dynamic
spec.loader.exec_module(dynamic)

import factory_state_ledger as ledger  # noqa: E402
from factory_model_attempt_events import model_attempt_events  # noqa: E402

WORKER = "1327811785102"
SLOT = f"2026-11-24T11:42:30.0000000Z Factory {WORKER} assigned dynamic provider slot: source=opencode-free configured=true\n"
# The echoed run: script carries the same phrases, indented and colored.
ECHO = (
    "2026-11-24T11:42:39.7334880Z \x1b[36;1m      echo '::error::Every available model "
    "authored this PR; the review stays queued for another model'\x1b[0m\n"
    "2026-11-24T11:42:39.7334881Z \x1b[36;1m    printf 'factory failover exhausted: no eligible "
    "provider/model combination remains'\x1b[0m\n"
)
REVIEW_EXIT = (
    "2026-11-24T11:42:40.2907273Z OpenCode Go unavailable: No zero-cost Go coding models outside cooldown\n"
    f"2026-11-24T11:42:40.8702688Z Factory {WORKER} lane opencode-free: Go key or free catalog "
    "unavailable; selecting across the eligible fleet\n"
    f"2026-11-24T11:42:41.2925320Z Factory {WORKER}: nvidia/moonshotai/kimi-k3.1-pro authored this PR "
    "(as nvidia+moonshotai+kimi-k3.1-pro); selecting a different reviewer model\n"
    "2026-11-24T11:42:41.4023921Z factory failover exhausted: no eligible provider/model combination remains\n"
)
CAPACITY_LINES = [
    "2026-11-24T11:42:41Z Kilo catalog: Kilo CLI reported no eligible catalog candidates",
    "2026-11-24T11:42:41Z OpenCode Go unavailable: No zero-cost Go coding models outside cooldown",
    "2026-11-24T11:42:41Z factory catalog: openrouter-free: no eligible catalog candidates available",
    "2026-11-24T11:42:41Z factory failover exhausted: no eligible provider/model combination remains",
    "2026-11-24T11:42:41Z ##[error]Every available model authored this PR; the review stays queued for another model",
    "2026-11-24T11:42:41Z ##[error]No reviewer model: 1 model(s) outside exclusions are eligible and all 1 "
    "authored this PR (2 author model(s) recorded); the review waits for a non-author model",
]


@pytest.mark.parametrize("line", CAPACITY_LINES)
def test_each_capacity_exit_is_no_capacity_with_its_reason(line: str) -> None:
    result = dynamic.classify(
        SLOT + ECHO + line + "\n" + "##[error]Process completed with exit code 1.\n"
    )
    assert result["outcome_class"] == "no_capacity"
    assert result["outcome"] == "NO CAPACITY"
    assert result["detail"] == re.sub(r"^\S+ (?:##\[error\])?", "", line)


def test_reviewer_exit_after_go_lane_fallback_is_no_capacity() -> None:
    log = (
        SLOT
        + ECHO
        + REVIEW_EXIT
        + (
            "2026-11-24T11:42:41.4216927Z ##[error]Every available model authored this PR; "
            "the review stays queued for another model\n"
        )
    )
    assert dynamic.classify(log)["outcome_class"] == "no_capacity"


def test_capacity_wins_over_a_cooling_lane_catalog() -> None:
    log = (
        SLOT
        + (
            "2026-11-24T11:42:40Z factory catalog: ollama-cloud: catalog contained no eligible "
            "candidates outside cooldown\n"
        )
        + REVIEW_EXIT
    )
    assert dynamic.classify(log)["outcome_class"] == "no_capacity"


def test_cooling_lane_alone_stays_no_work() -> None:
    log = SLOT + (
        "2026-11-24T11:42:40Z factory catalog: ollama-cloud: catalog contained no eligible "
        "candidates outside cooldown\n"
    )
    assert dynamic.classify(log)["outcome_class"] == "no_work"


def test_lane_capacity_line_then_successful_fleet_fallback_is_not_capacity() -> None:
    log = (
        SLOT
        + ECHO
        + (
            "2026-11-24T11:42:40Z OpenCode Go unavailable: No zero-cost Go coding models outside cooldown\n"
            f"2026-11-24T11:42:41Z Factory {WORKER} lane opencode-free: Go key or free catalog "
            "unavailable; selecting across the eligible fleet\n"
            "2026-11-24T11:42:42Z ##[error]Selected model differs from explicit requested model\n"
        )
    )
    assert dynamic.classify(log)["outcome_class"] == "control_plane_failure"


def test_echoed_script_text_alone_is_not_capacity() -> None:
    assert dynamic.classify(SLOT + ECHO)["outcome_class"] == "control_plane_failure"


def test_no_capacity_is_an_idle_worker_event(monkeypatch: pytest.MonkeyPatch) -> None:
    import factory_state_operations

    written: list[dict[str, object]] = []
    monkeypatch.setattr(ledger, "record_events", lambda _repo, entries: written.extend(entries))
    monkeypatch.setattr(factory_state_operations, "enabled", lambda: False)
    outcome = dynamic.classify(SLOT + REVIEW_EXIT)
    outcome["worker"] = WORKER
    ledger.record_runtime_outcome("o/r", "45386686089", outcome)
    assert [entry["event_type"] for entry in written] == ["worker_idle"]
    assert written[0]["outcome_class"] == "no_capacity"


def _reviewer_guard_block() -> str:
    workflow = (ROOT.parent / ".github/workflows/free-model-factory-run.yml").read_text(
        encoding="utf-8"
    )
    block = workflow.split("picked=$((", 1)[1].split('echo "::error::$reason"', 1)[0]
    return "picked=$((" + block + 'echo "::error::$reason"\n'


@pytest.mark.parametrize(
    ("picks", "expected"),
    [
        (
            1,
            "No reviewer model: 1 model(s) outside exclusions are eligible and all 1 authored "
            "the current head (2 head author model(s) recorded); the review waits for a "
            "non-author model",
        ),
        (
            17,
            "No reviewer model: at least 17 model(s) outside exclusions are eligible and all 17 "
            "authored the current head (2 head author model(s) recorded); the review waits for "
            "a non-author model",
        ),
    ],
)
def test_reviewer_guard_error_states_the_counts(tmp_path: Path, picks: int, expected: str) -> None:
    script = (
        "set -Eeuo pipefail\n"
        f"discovery_outcome={tmp_path}/outcome\n"
        'FACTORY_REVIEW_AUTHORS=\'["a/x","b/y"]\'\n'
        "avoided=()\n"
        f'for i in $(seq {picks}); do avoided+=(--attempted-pair "[\\"s\\",\\"m$i\\"]"); done\n'
        + _reviewer_guard_block()
    )
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout
    assert out.strip() == f"::error::{expected}"
    assert (tmp_path / "outcome").read_text() == f"no_capacity\t{expected}\n"
    log = SLOT + f"2026-11-24T11:42:41Z ##[error]{expected}\n"
    assert dynamic.classify(log)["outcome_class"] == "no_capacity"


NVIDIA = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
RETRY_429 = (
    "\x1b[93m\x1b[1m!\x1b[0m retrying (attempt {n}): Provider request failed with HTTP 429: "
    '{{"status":429,"title":"Too Many Requests"}}'
)


def _prefixed(stamp: str, message: str) -> str:
    return f"{stamp} [factory:{WORKER}][source:nvidia][model:{NVIDIA}] {message}\n"


def _nvidia_session(exit_status: int, *, before: str = "", after: str = "") -> str:
    return (
        f"Factory {WORKER} assigned dynamic provider slot: source=nvidia configured=true\n"
        + _prefixed("2026-11-24T12:40:20Z", "checked out pr #5671 on factory/work")
        + _prefixed(
            "2026-11-24T12:40:21Z",
            f"FACTORY_EXECUTION_CANDIDATE: source=nvidia model={NVIDIA} runtime=nvidia/{NVIDIA}",
        )
        + "2026-11-24T12:40:36Z > build · nvidia/nemotron-3-nano-omni-30b-a3b-reasoning\n"
        + before
        + "2026-11-24T12:41:08Z "
        + RETRY_429.format(n=2)
        + "\n"
        + "2026-11-24T12:41:09Z "
        + RETRY_429.format(n=3)
        + "\n"
        + after
        + _prefixed("2026-11-24T12:41:30Z", f"agent exit status {exit_status} for pr #5671")
    )


# Run 45395552539: OpenCode recovered from the 429s and the model kept working.
RECOVERED = (
    "2026-11-24T12:41:15Z $ git checkout pr-5671\n"
    "2026-11-24T12:41:15Z Switched to branch 'pr-5671'\n"
    "2026-11-24T12:41:16Z > build · nvidia/nemotron-3-nano-omni-30b-a3b-reasoning\n"
)


def test_session_ending_on_429_after_exit_1_is_throttle() -> None:
    assert dynamic.classify(_nvidia_session(1))["outcome_class"] == "provider_throttle"


def test_session_ending_on_429_is_throttle_not_work_failure() -> None:
    result = dynamic.classify(_nvidia_session(0, before="2026-11-24T12:41:00Z tests failed\n"))
    assert result["outcome_class"] == "provider_throttle"
    assert result["detail"] == "the session's last provider retry was an HTTP 429 response"


def test_recovery_session_ending_on_429_with_missing_verdict_is_throttle() -> None:
    log = _nvidia_session(0) + _prefixed(
        "2026-11-24T12:41:40Z",
        "semantic_review_recovery_failed: review model did not produce an authoritative "
        "terminal verdict for PR #5671",
    )
    assert dynamic.classify(log)["outcome_class"] == "provider_throttle"


def test_recovered_429_then_test_failure_stays_work_failure() -> None:
    log = _nvidia_session(0, after=RECOVERED + "2026-11-24T12:41:20Z tests failed\n")
    assert dynamic.classify(log)["outcome_class"] == "work_failure"


def test_recovered_429_in_an_approved_review_stays_no_work() -> None:
    log = _nvidia_session(0, after="2026-11-24T12:41:20Z FACTORY_GATE_READY\n")
    assert dynamic.classify(log)["outcome_class"] == "no_work"


def test_idle_session_ending_on_429_stays_no_work() -> None:
    assert dynamic.classify(_nvidia_session(0))["outcome_class"] == "no_work"


def test_429_quoted_by_the_agent_is_not_a_provider_response() -> None:
    log = (
        f"Factory {WORKER} assigned dynamic provider slot: source=nvidia configured=true\n"
        + _prefixed("2026-11-24T12:40:20Z", "checked out pr #5671 on factory/work")
        + _prefixed(
            "2026-11-24T12:40:21Z",
            f"FACTORY_EXECUTION_CANDIDATE: source=nvidia model={NVIDIA} runtime=nvidia/{NVIDIA}",
        )
        + '2026-11-24T12:37:43Z     r"Too Many Requests|status.?429|HTTP.?429|rate.?limit",\n'
        + '2026-11-24T12:37:57Z     /// parallel tests race into 429 "Too many requests" flakes\n'
        + "2026-11-24T12:41:20Z tests failed\n"
        + _prefixed("2026-11-24T12:41:30Z", "agent exit status 0 for pr #5671")
    )
    assert dynamic.classify(log)["outcome_class"] == "work_failure"


def test_daily_limit_429_is_quota() -> None:
    after = (
        "2026-11-24T12:41:12Z ! retrying (attempt 5): HTTP 429: Rate limit exceeded: "
        "free-models-per-day-high-balance.\n"
    )
    log = _nvidia_session(0, before="2026-11-24T12:41:00Z tests failed\n", after=after)
    assert dynamic.classify(log)["outcome_class"] == "provider_quota"


def _attempt(log: str, outcome: dict[str, object]) -> list[dict[str, object]]:
    stamped = "".join(
        line if re.match(r"^\d{4}-", line) else f"2026-11-24T12:40:00Z {line}"
        for line in log.splitlines(keepends=True)
    )
    return model_attempt_events("45395552539", {"worker": WORKER, **outcome}, stamped)


def test_unproven_final_attempt_takes_the_terminal_provider_class() -> None:
    log = _nvidia_session(1, after=RECOVERED).replace(
        "2026-11-24T12:40:21Z", "2026-11-24T12:40:21.0000000Z"
    )
    events = _attempt(
        log,
        {
            "source": "nvidia",
            "model": NVIDIA,
            "outcome_class": "provider_failure",
            "exact_model_proven": False,
        },
    )
    assert [event["outcome_class"] for event in events] == ["provider_failure"]


def test_no_change_attempt_with_429_retries_is_throttle() -> None:
    no_change = _prefixed(
        "2026-11-24T12:41:31.0000000Z",
        f"FACTORY_NO_CHANGE_FAILOVER: source=nvidia model={NVIDIA} made no change to pr #5671",
    )
    log = _nvidia_session(0).replace("2026-11-24T12:40:21Z", "2026-11-24T12:40:21.0000000Z")
    events = _attempt(
        log + no_change,
        {
            "source": "other",
            "model": "m",
            "outcome_class": "success",
            "exact_model_proven": True,
        },
    )
    assert events[0]["outcome_class"] == "provider_throttle"
    assert events[0]["evidence"] == "provider_retry"


def test_recorded_control_plane_failure_reaches_the_classified_log(tmp_path: Path) -> None:
    script = (
        "set -euo pipefail\n"
        f"WORKER={WORKER} SOURCE=nvidia MODEL={NVIDIA}\n"
        f"TERMINAL_OUTCOME_FILE={tmp_path}/outcome\n"
        "log() { printf '[factory:%s][source:%s][model:%s] %s\\n' "
        '"$WORKER" "$SOURCE" "$MODEL" "$*"; }\n'
        f"source {SCRIPTS}/factory-worker-outcome.sh\n"
        "record_terminal_outcome control_plane_failure "
        "'trusted review controller failed for PR #5671 with exit status 1'\n"
        "record_terminal_outcome work_failure 'tests failed'\n"
    )
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout
    assert out.splitlines() == [
        f"[factory:{WORKER}][source:nvidia][model:{NVIDIA}] control_plane_failure: "
        "trusted review controller failed for PR #5671 with exit status 1"
    ]
    log = (
        _nvidia_session(0, after=RECOVERED).replace(
            _prefixed("2026-11-24T12:41:30Z", "agent exit status 0 for pr #5671"), ""
        )
        + _prefixed("2026-11-24T12:41:30Z", "agent exit status 0 for pr #5671")
        + "2026-11-24T12:41:40Z "
        + out
    )
    assert dynamic.classify(log)["outcome_class"] == "control_plane_failure"

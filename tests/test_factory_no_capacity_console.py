from __future__ import annotations

from pathlib import Path

from rotisserie.factory_console.commands._shared import OUTCOME_WORDS
from rotisserie.factory_console.commands.report import plain_failure
from rotisserie.factory_console.tracker import EXIT_CODES, Result, parse_outcome
from rotisserie.factory_console.verdicts import outcome_class

ROOT = Path(__file__).resolve().parents[1]
REASON = (
    "No reviewer model: 1 model(s) outside exclusions are eligible and all 1 authored this PR "
    "(2 author model(s) recorded); the review waits for a non-author model"
)
PERSIST_LOG = (
    f"2026-11-24T11:44:02.1Z [factory-outcome] no_capacity: No capacity: {REASON}\n"
    '2026-11-24T11:44:02.2Z [factory-outcome-class] {"class":"no_capacity","source":"nvidia",'
    '"model":"","target":"pr:5671"}\n'
)


def test_persist_step_prints_the_no_capacity_verdict_the_console_parses() -> None:
    entry = (ROOT / ".github/workflows/free-model-factory-entry.yml").read_text(encoding="utf-8")
    assert 'elif [[ "$classification" == no_capacity ]]; then' in entry
    assert 'verdict="no_capacity"' in entry
    assert 'message="No capacity: ' in entry
    assert 'echo "[factory-outcome] $verdict: $message"' in entry
    assert '"[factory-outcome-class] "' in entry


def test_no_capacity_verdict_is_its_own_result() -> None:
    outcome = parse_outcome(PERSIST_LOG)
    assert outcome is not None
    assert outcome.result is Result.NO_CAPACITY
    assert outcome.message == f"No capacity: {REASON}"
    assert outcome.pr is None
    assert EXIT_CODES[Result.NO_CAPACITY] == 3


def test_report_names_no_capacity_with_its_reason() -> None:
    assert plain_failure(f"No capacity: {REASON}") == f"no capacity: {REASON}"[:133]
    assert OUTCOME_WORDS["no_capacity"] == "no capacity"
    assert OUTCOME_WORDS["control_plane_failure"] == "factory setup problem"


def test_outcome_class_line_gives_class_source_model_and_target() -> None:
    assert outcome_class(PERSIST_LOG) == {
        "class": "no_capacity",
        "source": "nvidia",
        "model": "",
        "target": "pr:5671",
    }


def test_runs_before_the_class_line_still_give_a_class() -> None:
    failed = (
        "[factory-outcome] failed: No verified saved code or PR delivery; "
        "worker failed: control_plane_failure\n"
    )
    assert outcome_class(failed) == {"class": "control_plane_failure"}
    assert outcome_class("[factory-outcome] idle: No code delivered\n") == {"class": "no_work"}
    assert outcome_class("no outcome here") == {}

"""A review-stage PR whose own checks are red goes to CI repair, not to a reviewer.

Comic Pile #5669 and #5674 kept drawing reviewers: their one required
aggregate check passed while a path-filtered check (rust-roll) failed on the
PR's own change. Failures main caused keep today's path.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from factory_work_policy import Candidate, own_check_failures

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_PATH = REPO_ROOT / ".github/scripts/factory-work-controller.py"
HEAD = "a" * 40
RUN_URL = "https://github.com/JoshCLWren/comic-pile/actions/runs/{run}/job/{job}"


@pytest.fixture
def controller(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("factory_work_controller_red_review", CONTROLLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    monkeypatch.delenv("FACTORY_ADVISORY_CHECKS", raising=False)
    return module


def review_pr(*labels: str) -> dict[str, Any]:
    return {"labels": [{"name": name} for name in ("factory", "factory:review", *labels)]}


def test_policy_counts_only_failures_main_did_not_cause() -> None:
    mains = {"fmt": "FAILURE", "clippy": "success", "openapi": "", "flaky": "timed_out"}
    failing = ["clippy", "fmt", "openapi", "new-check", "flaky", "clippy"]
    assert own_check_failures(failing, mains) == ["clippy", "new-check", "openapi"]
    assert own_check_failures([], mains) == []


def test_red_review_pr_is_rerouted_and_not_reviewed(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = Candidate("pr", 5669, 3, 2, "2026-11-22T12:00:00Z", stage="factory:review")
    monkeypatch.setattr(controller, "target_json", lambda _n: review_pr("factory:unowned"))
    monkeypatch.setattr(controller, "required_checks_outcome", lambda _n: "passed")
    monkeypatch.setattr(controller, "own_red_checks", lambda _n: ["rust-roll"])
    moved: list[tuple[int, list[str]]] = []
    monkeypatch.setattr(
        controller, "reroute_red_review", lambda n, _labels, own: moved.append((n, own)) or True
    )
    assert controller.candidate_is_live_executable(candidate) is False
    assert moved == [(5669, ["rust-roll"])]


def test_inherited_red_keeps_review_and_owned_or_conflicted_prs_are_not_read(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(controller, "required_checks_outcome", lambda _n: "failed")
    reads: list[int] = []

    def own(number: int) -> list[str]:
        reads.append(number)
        return []  # main fails the same check: inherited

    monkeypatch.setattr(controller, "own_red_checks", own)
    monkeypatch.setattr(controller, "target_json", lambda _n: review_pr("factory:unowned"))
    assert controller.candidate_is_live_executable(Candidate("pr", 1, 3, 2, "")) is True
    monkeypatch.setattr(controller, "target_json", lambda _n: review_pr("factory:12"))
    assert controller.candidate_is_live_executable(Candidate("pr", 2, 3, 2, "")) is True
    monkeypatch.setattr(controller, "target_json", lambda _n: review_pr("factory:unowned"))
    conflicted = Candidate("pr", 3, 3, 2, "", conflicted=True)
    assert controller.candidate_is_live_executable(conflicted) is True
    assert reads == [1]


def routed(controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch, routes: dict[str, object]) -> list[str]:
    calls: list[str] = []

    def gh_json(args: list[str], **_kwargs: object) -> object:
        path = args[-1]
        calls.append(path)
        for suffix, value in routes.items():
            if path.endswith(suffix):
                if isinstance(value, Exception):
                    raise value
                return value
        return {}

    monkeypatch.setattr(controller, "gh_json", gh_json)
    monkeypatch.setattr(controller, "live_pr_state", lambda _n: {"head": {"sha": HEAD}})
    return calls


def failing_run(name: str, run: int, conclusion: str = "failure") -> dict[str, object]:
    return {"name": name, "conclusion": conclusion, "html_url": RUN_URL.format(run=run, job=run + 1)}


def main_routes(run: int, conclusion: str, *, name: str = "rust-roll", workflow: int = 77) -> dict[str, object]:
    return {
        f"actions/runs/{run}": {"workflow_id": workflow},
        f"actions/workflows/{workflow}/runs?branch=main&status=completed&per_page=10": {
            "workflow_runs": [
                {"id": 8, "event": "pull_request"},
                {"id": 9, "event": "push"},
                {"id": 10, "event": "push"},
            ]
        },
        "actions/runs/9/jobs?per_page=100": {"jobs": [{"name": "other", "conclusion": "success"}]},
        "actions/runs/10/jobs?per_page=100": {"jobs": [{"name": name, "conclusion": conclusion}]},
    }


def test_own_red_checks_compares_each_failure_with_main(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FACTORY_ADVISORY_CHECKS", "PR-Agent advisory review, ")
    routes: dict[str, object] = {
        "repos/JoshCLWren/comic-pile": {"default_branch": "main"},
        f"commits/{HEAD}/check-runs?per_page=100": {
            "check_runs": [
                failing_run("rust-roll", 100),
                failing_run("rust-roll", 100),  # a re-run reports the same check twice
                failing_run("fmt", 200),
                failing_run("PR-Agent advisory review", 300),
                failing_run("CI Summary", 400, conclusion="success"),
                failing_run("no-url", 500) | {"html_url": "https://example.com/x"},
                "not a run",
            ]
        },
        **main_routes(100, "success"),
    }
    routes["actions/runs/200"] = {"workflow_id": 88}
    routes["actions/workflows/88/runs?branch=main&status=completed&per_page=10"] = {
        "workflow_runs": [{"id": 11, "event": "push"}]
    }
    routes["actions/runs/11/jobs?per_page=100"] = {"jobs": [{"name": "fmt", "conclusion": "failure"}]}
    calls = routed(controller, monkeypatch, routes)
    assert controller.own_red_checks(5669) == ["no-url", "rust-roll"]
    # Main's verdict for a run is read once per process.
    before = len(calls)
    assert controller.main_check_conclusion("rust-roll", RUN_URL.format(run=100, job=101)) == "success"
    assert len(calls) == before


def test_missing_main_evidence_counts_as_the_prs_own(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    routes: dict[str, object] = {
        "repos/JoshCLWren/comic-pile": ["not", "a", "repo"],
        "actions/runs/1": {"workflow_id": "x"},
        "actions/runs/2": {"workflow_id": 5},
        "actions/workflows/5/runs?branch=main&status=completed&per_page=10": {"workflow_runs": None},
        "actions/runs/3": {"workflow_id": 6},
        "actions/workflows/6/runs?branch=main&status=completed&per_page=10": {
            "workflow_runs": [{"id": 12, "event": "push"}, "bad"]
        },
        "actions/runs/12/jobs?per_page=100": {"jobs": None},
    }
    routed(controller, monkeypatch, routes)
    assert controller.default_branch() == "main"
    assert controller.main_check_conclusion("a", RUN_URL.format(run=1, job=1)) == ""
    assert controller.main_check_conclusion("a", RUN_URL.format(run=2, job=1)) == ""
    assert controller.main_check_conclusion("a", RUN_URL.format(run=3, job=1)) == ""
    assert controller.main_check_conclusion("a", "") == ""


@pytest.mark.parametrize(
    ("live", "runs"),
    [
        ({"head": {"sha": "short"}}, {}),
        ({"head": {"sha": HEAD}}, {"check_runs": None}),
        ({"head": {"sha": HEAD}}, ["not", "an", "object"]),
    ],
)
def test_unreadable_heads_and_check_runs_have_no_failures(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch, live: dict[str, object], runs: object
) -> None:
    monkeypatch.setattr(controller, "live_pr_state", lambda _n: live)
    monkeypatch.setattr(controller, "gh_json", lambda _args, **_kw: runs)
    assert controller.head_failing_check_runs(1) == []


def test_read_errors_keep_todays_review_path(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_n: int) -> dict[str, object]:
        raise RuntimeError("gh failed")

    monkeypatch.setattr(controller, "live_pr_state", broken)
    assert controller.own_red_checks(1) == []
    monkeypatch.setattr(controller, "live_pr_state", lambda _n: {"head": {"sha": HEAD}})

    def bad_json(_args: list[str], **_kw: object) -> object:
        raise json.JSONDecodeError("x", "", 0)

    monkeypatch.setattr(controller, "gh_json", bad_json)
    assert controller.own_red_checks(1) == []


def test_reroute_writes_ci_stage_under_an_owner_compare_and_swap(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    writes: list[tuple[int, str, str | None, str | None]] = []
    result = {"value": True}

    def replace(number: int, owner: str, stage: str | None = None, **kwargs: Any) -> bool:
        writes.append((number, owner, stage, kwargs.get("expected_owner")))
        return result["value"]

    monkeypatch.setattr(controller, "replace_factory_labels", replace)
    unowned = {"factory", "factory:review", "factory:unowned"}
    assert controller.reroute_red_review(5669, unowned, ["rust-roll"]) is True
    assert writes == [(5669, "factory:unowned", "factory:ci", "factory:unowned")]
    assert "its own checks fail (rust-roll); moved from review to CI repair" in capsys.readouterr().err
    # No owner label to compare against: leave the PR alone.
    assert controller.reroute_red_review(5669, {"factory", "factory:review"}, ["x"]) is False
    assert len(writes) == 1
    # The compare-and-swap lost (a worker claimed it): reported as not moved.
    result["value"] = False
    assert controller.reroute_red_review(5669, unowned, ["x"]) is False

    def refused(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError("dispatcher authorization failed")

    monkeypatch.setattr(controller, "replace_factory_labels", refused)
    assert controller.reroute_red_review(5669, unowned, ["x"]) is False
    assert "could not reroute PR #5669" in capsys.readouterr().err

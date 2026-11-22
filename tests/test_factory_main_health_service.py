"""The console's main-verification service and its doctor check."""

from __future__ import annotations

import json
from collections.abc import Sequence

import pytest

from rotisserie.factory_console.github import GitHub, GitHubError
from rotisserie.factory_console.service import doctor, main_health

REPO = "owner/repo"
SHA = "d" * 40


def run(conclusion: str | None = "success", *, status: str = "completed") -> dict[str, object]:
    return {
        "id": 7,
        "path": ".github/workflows/ci.yml",
        "event": "workflow_dispatch",
        "head_sha": SHA,
        "status": status,
        "conclusion": conclusion,
    }


class Fake:
    def __init__(self, runs: list[dict[str, object]]) -> None:
        self.runs = runs
        self.calls: list[list[str]] = []
        self.dispatched: list[str] = []

    def __call__(self, args: Sequence[str]) -> str:
        argv = list(args)
        self.calls.append(argv)
        if argv[:2] == ["variable", "list"]:
            return json.dumps([{"name": "FACTORY_MAIN_CI_WORKFLOWS", "value": "ci.yml"}])
        if argv[0] == "workflow":
            self.dispatched.append(argv[2])
            # A dispatched run shows up right away as queued.
            self.runs = [run(None, status="queued")]
            return ""
        path = next(item for item in argv[1:] if item.startswith("repos/"))
        if path.endswith("commits/HEAD"):
            return json.dumps({"sha": SHA})
        if "/actions/workflows/ci.yml/runs" in path:
            return json.dumps({"workflow_runs": self.runs})
        raise AssertionError(argv)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Fake:
    state = Fake([])
    monkeypatch.setattr(GitHub, "_run", lambda self, args, stdin=None: state(args))
    return state


def test_prefetched_data_makes_no_github_reads(fake: Fake) -> None:
    health = main_health.assess(
        GitHub(REPO), sha=SHA, runs=[{"workflow_runs": [run()]}], variables={}
    )
    assert health.state == "green" and health.merges_allowed
    assert health.runs[0].workflow == "ci.yml"
    assert fake.calls == []


def test_no_run_is_unverified_and_doctor_offers_the_fix(fake: Fake) -> None:
    finding = doctor.main_verification(GitHub(REPO))
    assert finding.status == "warn"
    assert finding.fix == "verify-main"
    assert "unverified" in finding.text
    applied = doctor.apply_fixes(GitHub(REPO), [finding])
    assert fake.dispatched == ["ci.yml"]
    assert applied[0].fix == "verify-main"
    # Deduplicated: the commit now has a queued run, so a second fix does nothing.
    assert doctor.apply_fixes(GitHub(REPO), [finding]) == []
    assert fake.dispatched == ["ci.yml"]


@pytest.mark.parametrize(
    ("runs", "status", "state"),
    [
        ([run()], "ok", "green"),
        ([run(None, status="in_progress")], "ok", "pending"),
        ([run("failure")], "fail", "red"),
        ([run("cancelled")], "fail", "red"),
    ],
)
def test_doctor_reports_each_state(
    fake: Fake, runs: list[dict[str, object]], status: str, state: str
) -> None:
    fake.runs = runs
    health = main_health.assess(GitHub(REPO))
    assert health.state == state
    assert doctor.main_verification(GitHub(REPO), health).status == status
    assert main_health.dispatch_verification(GitHub(REPO), health) == []


def test_malformed_runs_surface_as_a_github_error(fake: Fake) -> None:
    with pytest.raises(GitHubError):
        main_health.assess(GitHub(REPO), sha=SHA, runs=[{"nope": 1}], variables={})

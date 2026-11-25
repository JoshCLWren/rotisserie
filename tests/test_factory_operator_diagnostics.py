"""`baste health reset MODEL`, `baste outcome RUN` and the factory App budget in `limits`."""

from __future__ import annotations

import argparse
import io
import json
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from rotisserie import cli
from rotisserie import control_contract as contract
from rotisserie.factory_console import commands
from rotisserie.factory_console.commands.app_budget import show_app_budget
from rotisserie.factory_console.commands.outcome import cmd_outcome, run_id
from rotisserie.factory_console.context import Context, UsageError
from rotisserie.factory_console.github import JSON, GitHub, GitHubAuthError, GitHubError
from rotisserie.factory_console.service import app_budget, backoffs, bridge, run_outcome
from rotisserie.factory_console.tracker import Clock, Result
from rotisserie.factory_console.ui import Console, Prompt

NOW = datetime(2026, 11, 25, 18, 0, tzinfo=UTC)
CLASS_LINE = (
    '2026-11-25T15:01:02.1Z [factory-outcome-class] {"class":"work_failure","source":"nvidia",'
    '"model":"moonshotai/kimi-k3.1-pro","target":"pr:5772","kind":"review",'
    '"selection":"nvidia / moonshotai/kimi-k3.1-pro: requested model"}\n'
)


class Out(io.StringIO):
    def isatty(self) -> bool:
        return False


def _reset(argv: list[str], monkeypatch: pytest.MonkeyPatch, seen: list[Any], data: Any) -> Any:
    monkeypatch.setattr(GitHub, "_run", lambda self, args, stdin=None, allow_exit_one=False: "[]")

    def fake_run(github, clock, workflow, inputs, **kwargs):  # type: ignore[no-untyped-def]
        seen.append((workflow, inputs))
        if isinstance(data, Exception):
            raise data
        return bridge.BridgeRun(workflow, 9, "https://example.invalid/run/9", "success", data)

    monkeypatch.setattr(bridge, "run", fake_run)
    arguments = cli.parser().parse_args(["factory", "--repo", "acme/widgets", "--json", *argv])
    out = Out()
    code = commands.main(arguments, env={"TERM": "xterm"}, stdout=out, stdin=Out(), executable="sh")
    return code, json.loads(out.getvalue())


def test_health_reset_clears_one_model_through_the_trusted_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []
    cleared = {
        "model": "m",
        "model_health_rows": [{"source": "nvidia", "model": "m", "status": "cooling"}],
        "backoff_rows": [{"source": "nvidia", "model": "m", "streak": 4, "outcome": "x"}],
        "worker_backoffs_kept": 1,
    }
    argv = ["health", "reset", "m", "--source", "nvidia", "--yes", "--via", "github"]
    code, data = _reset(argv, monkeypatch, seen, cleared)
    assert code == 0
    assert seen == [(backoffs.RESET_WORKFLOW, {"model": "m", "source": "nvidia"})]
    assert data["backoff_rows"][0]["streak"] == 4 and data["bridge"]["run_id"] == 9


def test_an_old_caller_without_the_model_input_says_to_reinstall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refused = bridge.BridgeError("HTTP 422: Unexpected inputs provided: [model]")
    code, data = _reset(
        ["health", "reset", "m", "--yes", "--via", "github"], monkeypatch, [], refused
    )
    assert code == 2
    assert "Re-run the Rotisserie installer" in data["error"]


def test_health_reset_asks_first(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []
    code, _ = _reset(["health", "reset", "m", "--via", "github"], monkeypatch, seen, {})
    assert code == 3 and seen == []


class RunGitHub(GitHub):
    def __init__(self, *, flaky: int = 0, status: str = "completed") -> None:
        super().__init__("acme/widgets")
        self.flaky = flaky
        self.status = status
        self.reads = 0

    def repo_api(self, path: str, **kwargs: object) -> JSON:
        if path == "actions/runs/77":
            self.reads += 1
            if self.flaky:
                self.flaky -= 1
                raise GitHubError("GitHub request failed: unexpected end of JSON input")
            status, self.status = self.status, "completed"
            return {
                "id": 77, "html_url": "https://x/runs/77", "status": status,
                "conclusion": "failure" if status == "completed" else None,
                "display_title": "Factory 42 · nvidia/m · fixed-model entry", "name": "entry",
            }  # fmt: skip
        if path.startswith("actions/runs/77/jobs"):
            steps: list[JSON] = [{"name": "Classify and persist", "conclusion": "failure"}]
            return {
                "jobs": [
                    {"id": 1, "name": "rotisserie / run / factory", "status": "completed",
                     "conclusion": "success", "html_url": "https://x/job/1", "steps": []},
                    {"id": 2, "name": "rotisserie / Persist trusted worker outcome",
                     "status": "completed", "conclusion": "failure", "html_url": "https://x/job/2",
                     "steps": steps},
                ]
            }  # fmt: skip
        if path == "check-runs/2/annotations":
            return [{"annotation_level": "failure", "message": "worker failed: work_failure"}]
        raise AssertionError(path)

    def text(self, path: str, *, final: str = "") -> str:
        if path == "actions/jobs/1/logs":
            return "2026-11-25T15:00:00Z ##[notice]Model nvidia/m picked by track record\n"
        return CLASS_LINE + "[factory-outcome] failed: worker failed: work_failure\n"


def _outcome(github: RunGitHub, **options: Any) -> tuple[Any, str, list[float]]:
    stream = io.StringIO()
    console = Console(stream, env={})
    slept: list[float] = []
    clock = Clock(now=lambda: NOW, sleep=slept.append)
    context = Context(cast(GitHub, github), console, Prompt(console), clock, False)
    values = {"run": "77", "follow": False, "interval": 30, "timeout": 600, **options}
    return cmd_outcome(context, argparse.Namespace(**values)), stream.getvalue(), slept


def test_outcome_shows_the_class_notices_error_and_failed_step() -> None:
    outcome, text, _ = _outcome(RunGitHub())
    assert outcome.result == Result.COMPLETED
    assert outcome.data["outcome"]["class"] == "work_failure"
    assert outcome.data["outcome"]["target"] == "pr:5772"
    assert "selection nvidia / moonshotai/kimi-k3.1-pro: requested model" in text
    assert "notice: Model nvidia/m picked by track record" in text
    assert "Error: worker failed: work_failure" in text
    assert "Failed in: Persist trusted worker outcome / Classify and persist" in text
    assert "https://x/job/2" in text


def test_follow_waits_and_retries_a_garbled_github_answer() -> None:
    github = RunGitHub(flaky=2, status="in_progress")
    outcome, _, slept = _outcome(github, follow=True)
    assert outcome.data["retried_reads"] == 2
    assert outcome.data["status"] == "completed"
    assert slept == [5.0, 10.0, 30.0]


def test_follow_does_not_retry_an_auth_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    github = RunGitHub()

    def refuse(path: str, **kwargs: object) -> JSON:
        raise GitHubAuthError("not logged in")

    monkeypatch.setattr(github, "repo_api", refuse)
    with pytest.raises(GitHubAuthError):
        run_outcome.wait_until_done(cast(GitHub, github), Clock(sleep=lambda s: None), 77)


def test_run_ids_come_from_ids_or_urls() -> None:
    assert run_id("https://github.com/a/b/actions/runs/45635205302/job/1") == 45635205302
    assert run_id("77") == 77
    with pytest.raises(UsageError):
        run_id("not-a-run")


GATE = "Factory installation REST budget: {n}/5000 remaining (reset epoch {reset})"


def _budget(remaining: int, reset: int) -> tuple[Any, str]:
    log = "2026-11-25T17:50:00Z " + GATE.format(n=remaining, reset=reset) + "\n"

    class Dispatcher(GitHub):
        def repo_api(self, path: str, **kwargs: object) -> JSON:
            if path.startswith("actions/workflows/fixed-model-factory-dispatch.yml/runs"):
                assert "status=success" in path
                return {"workflow_runs": [{"id": 5, "html_url": "https://x/runs/5"}]}
            if path.startswith("actions/runs/5/jobs"):
                gate: JSON = {"name": app_budget.GATE_STEP, "conclusion": "success",
                        "completed_at": "2026-11-25T17:50:00Z"}  # fmt: skip
                return {"jobs": [{"id": 8, "status": "completed", "steps": [gate]}]}
            raise AssertionError(path)

        def text(self, path: str, *, final: str = "") -> str:
            return log

    stream = io.StringIO()
    console = Console(stream, env={})
    clock = Clock(now=lambda: NOW)
    context = Context(Dispatcher("acme/widgets"), console, Prompt(console), clock, False)
    found = show_app_budget(context, {"core": {"remaining": 4000}})
    return found, stream.getvalue()


def test_limits_reads_the_app_budget_from_the_dispatcher_gate() -> None:
    later = int(NOW.timestamp()) + 1800
    found, text = _budget(3371, later)
    assert found["remaining"] == 3371 and found["limit"] == 5000 and found["state"] == "ok"
    assert "core: 3,371 / 5,000 remaining, read 10m ago" in text
    near, text = _budget(1500, later)
    assert near["state"] == "near_reserve" and "Near the dispatcher's 1,200 reserve" in text
    below, text = _budget(900, later)
    assert below["state"] == "below_reserve" and "it skips dispatch" in text
    stale, text = _budget(900, int(NOW.timestamp()) - 60)
    assert stale["state"] == "stale" and "refilled since" in text


def test_the_app_budget_is_not_read_when_the_own_quota_is_spent() -> None:
    stream = io.StringIO()
    console = Console(stream, env={})
    context = Context(GitHub("acme/widgets"), console, Prompt(console), Clock(), False)
    assert show_app_budget(context, {"core": {"remaining": 0}}) is None
    assert "not read" in stream.getvalue()


def test_the_control_rooms_send_health_reset_and_read_outcome() -> None:
    assert contract.write_command(
        {"operation": "health_reset", "model": "nvidia/m", "source": "nvidia"}
    ) == ["health", "reset", "nvidia/m", "--yes", "--via", "github", "--source", "nvidia"]
    assert contract.read_command("outcome", {"run": "77"}) == ["outcome", "77"]
    for bad in ({"operation": "health_reset"}, {"operation": "health_reset", "model": "a b"}):
        with pytest.raises(contract.InvalidRequest):
            contract.write_command(bad)
    with pytest.raises(contract.InvalidRequest):
        contract.read_command("outcome", {"run": "x"})

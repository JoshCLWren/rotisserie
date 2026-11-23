"""Contract tests for ``rotisserie factory`` against a scripted GitHub.

These prove the console's behavior deterministically. They are not end-to-end
verification: that requires a real authenticated dispatch (see the PR notes).
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from rotisserie import cli
from rotisserie.factory_console import commands, runtime
from rotisserie.factory_console.github import (
    JSON,
    GitHub,
    GitHubAuthError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    Obj,
    _classify,
)
from rotisserie.factory_console.tracker import (
    Clock,
    Result,
    Stage,
    failure_reasons,
    parse_outcome,
    parse_result,
    read_dispatcher,
    read_worker,
)

REPO = "acme/widgets"
START = datetime(2026, 11, 21, 23, 0, tzinfo=UTC)
WORKER = runtime.module("factory_worker_slots").worker_id("opencode-free", 1)


class FakeClock(Clock):
    def __init__(self) -> None:
        self.current = START
        self.sleeps = 0
        self.on_sleep: Callable[[int], None] | None = None
        super().__init__(now=lambda: self.current, sleep=self._sleep)

    def _sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.current += timedelta(seconds=seconds)
        if self.on_sleep is not None:
            self.on_sleep(self.sleeps)


def step(name: str, status: str = "completed", conclusion: str = "success", at: int = 0) -> Obj:
    stamp = (START + timedelta(seconds=at)).isoformat().replace("+00:00", "Z")
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
        "started_at": stamp,
        "completed_at": stamp,
    }


def job(
    name: str,
    steps: list[Obj],
    status: str = "completed",
    conclusion: str = "success",
    job_id: int = 1,
) -> Obj:
    listed: list[JSON] = list(steps)
    return {
        "id": job_id,
        "name": name,
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
        "steps": listed,
        "started_at": "2026-11-21T23:00:10Z",
        "completed_at": "2026-11-21T23:01:00Z",
    }


DISPATCH_DONE = [
    job(
        "dispatch",
        [
            step("Set up job"),
            step("Capture scheduling inventory once"),
            step("Advance selected issue or pull request, or pick next work for a model"),
            step(
                "Report quota-deferred targeted dispatch without failing a worker",
                conclusion="skipped",
            ),
        ],
        job_id=11,
    )
]


def dispatched_log(**overrides: object) -> str:
    result = {
        "status": "dispatched",
        "kind": "issue",
        "number": 3953,
        "action": "implement",
        "worker": WORKER,
        "provider": "opencode-free",
        "model": "opencode-go/hy4-flash-free",
        "forced": True,
        "slot_index": None,
        "dispatched_at": int(START.timestamp()),
    }
    result.update(overrides)
    return "dispatch\tAdvance\t2026-11-21T23:00:30Z " + json.dumps(result) + "\n"


@dataclass
class FakeGitHub:
    """Routes ``gh`` invocations to scripted responses and records them."""

    routes: dict[str, JSON | Callable[[], JSON]] = field(default_factory=dict)
    texts: dict[str, str | Callable[[], str]] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)
    dispatch_output: str = f"https://github.com/{REPO}/actions/runs/900\n"
    error: Exception | None = None

    def __call__(self, github: GitHub, args: Sequence[str], *, stdin: str | None = None) -> str:
        argv = list(args)
        self.calls.append(argv)
        if self.error is not None:
            raise self.error
        if argv[0] == "workflow":
            return self.dispatch_output
        if argv[0] == "api":
            path = next(
                item
                for index, item in enumerate(argv[1:], start=1)
                if not item.startswith("-")
                and argv[index - 1] not in ("-f", "-F", "--method", "-X")
                and item not in ("GET", "POST")
            )
            if "--allow-escape-sequences" in argv:
                for prefix, text in self.texts.items():
                    if path.endswith(prefix):
                        return text() if callable(text) else text
                return ""
            for prefix, value in self.routes.items():
                if re.search(prefix, path):
                    found = value() if callable(value) else value
                    return json.dumps(found)
            return json.dumps({})
        if argv[:2] == ["run", "cancel"]:
            return ""
        if argv[:2] == ["variable", "list"]:
            return "[]"
        if argv[:2] == ["variable", "set"]:
            return ""
        if argv[:2] == ["pr", "view"]:
            return json.dumps(self.routes.get("pr-view", {}))
        if argv[:2] == ["issue", "list"]:
            return json.dumps(self.routes.get("issue-list", []))
        if argv[:2] == ["pr", "list"]:
            return json.dumps(self.routes.get("pr-list", []))
        if argv[:2] == ["secret", "list"]:
            return json.dumps(self.routes.get("secret-list", []))
        return "[]"

    def dispatch_inputs(self) -> dict[str, str]:
        call = next(call for call in self.calls if call[0] == "workflow")
        return dict(
            call[index + 1].split("=", 1) for index, item in enumerate(call) if item == "-f"
        )


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    gh = FakeGitHub()
    monkeypatch.setattr(GitHub, "_run", lambda self, args, stdin=None: gh(self, args, stdin=stdin))
    return gh


class TTY(io.StringIO):
    def __init__(self, text: str = "", tty: bool = False) -> None:
        super().__init__(text)
        self._tty = tty

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return "utf-8"

    def isatty(self) -> bool:
        return self._tty


def invoke(
    argv: list[str],
    *,
    clock: FakeClock | None = None,
    stdin: str = "",
    tty: bool = False,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    parser = cli.parser()
    arguments = parser.parse_args(["factory", "--repo", REPO, *argv])
    out = TTY(tty=tty)
    code = commands.main(
        arguments,
        env=env or {"TERM": "xterm"},
        stdout=out,
        stdin=TTY(stdin, tty=tty),
        clock=clock or FakeClock(),
        executable="sh",
    )
    return code, out.getvalue()


def scheduler_routes(
    fake: FakeGitHub, *, log: str, jobs: list[Obj] | None = None, worker: list[Obj] | None = None
) -> None:
    fake.routes["actions/runs/900/jobs"] = {"jobs": list[JSON](jobs or DISPATCH_DONE)}
    fake.routes["actions/runs/900$"] = {
        "id": 900,
        "status": "completed",
        "conclusion": "success",
        "created_at": "2026-11-21T23:00:00Z",
    }
    fake.texts["actions/jobs/11/logs"] = log
    fake.routes["issues/3953$"] = {
        "number": 3953,
        "title": "New releases: Fix missing publisher and volume titles",
        "labels": [],
        "state": "open",
    }
    fake.routes["free-model-factory-entry.yml/runs"] = {
        "workflow_runs": [
            {
                "id": 950,
                "display_title": f"Factory {WORKER} · fixed-model entry",
                "created_at": "2026-11-21T23:00:31Z",
                "status": "in_progress",
            }
        ]
    }
    fake.routes["actions/runs/950/jobs"] = {
        "jobs": list[JSON](
            worker
            or [
                job(
                    "run / Validate assignment under worker lock (trusted)",
                    [step("Verify original active claim and live target")],
                    job_id=21,
                ),
                job(
                    "run / factory",
                    [
                        step("Select execution candidate at dispatch time"),
                        step(
                            "Run continuous fixed-model factory session",
                            status="in_progress",
                            at=60,
                        ),
                    ],
                    status="in_progress",
                    job_id=22,
                ),
            ]
        )
    }
    fake.routes["actions/runs/950$"] = {
        "id": 950,
        "status": "in_progress",
        "created_at": "2026-11-21T23:00:31Z",
        "run_started_at": "2026-11-21T23:00:35Z",
    }


# Interpretation of real signals ----------------------------------------------------


def test_scheduler_states_distinguish_queue_machine_and_selection() -> None:
    assert read_dispatcher({"status": "queued"}, []).stage is Stage.QUEUED
    waiting = read_dispatcher({"status": "in_progress"}, [job("dispatch", [], status="queued")])
    assert waiting.stage is Stage.WAITING_MACHINE
    reading = read_dispatcher(
        {"status": "in_progress"},
        [
            job(
                "dispatch",
                [step("Capture scheduling inventory once", status="in_progress")],
                status="in_progress",
            )
        ],
    )
    assert (reading.stage, reading.text) == (
        Stage.READING_WORK,
        "Reading open issues and pull requests",
    )
    claiming = read_dispatcher(
        {"status": "in_progress"},
        [
            job(
                "dispatch",
                [
                    step("Capture scheduling inventory once"),
                    step(
                        "Advance selected issue or pull request, or pick next work for a model",
                        status="in_progress",
                    ),
                ],
                status="in_progress",
            )
        ],
    )
    assert claiming.stage is Stage.CLAIMING
    assert read_dispatcher({"status": "completed", "conclusion": "success"}, DISPATCH_DONE).finished


def test_worker_assignment_is_not_reported_as_execution() -> None:
    assigned = read_worker({"status": "queued"}, [job("run / factory", [], status="queued")], 3953)
    assert assigned.stage is Stage.ASSIGNED
    choosing = read_worker(
        {"status": "in_progress"},
        [
            job(
                "run / factory",
                [step("Select execution candidate at dispatch time", status="in_progress")],
                status="in_progress",
            )
        ],
    )
    assert choosing.stage is Stage.WORKER_STARTING
    working = read_worker(
        {"status": "in_progress"},
        [
            job(
                "run / factory",
                [step("Run continuous fixed-model factory session", status="in_progress")],
                status="in_progress",
            )
        ],
        7,
    )
    assert (working.stage, working.text) == (Stage.WORKING, "Model is working on #7")
    # Real run 44962194616: candidate selection failed, the session was skipped.
    never = read_worker(
        {"status": "completed", "conclusion": "failure"},
        [
            job(
                "run / factory",
                [
                    step("Select execution candidate at dispatch time", conclusion="failure"),
                    step("Run continuous fixed-model factory session", conclusion="skipped"),
                ],
                conclusion="failure",
            )
        ],
    )
    assert never.finished and never.failed
    assert never.text == "Worker stopped before the model started"


@pytest.mark.parametrize(
    "line",
    [
        'factory log: {"kind":"issue","number":5123,"status":"dispatched","worker":"123","model":"hy4","action":"implement"}',
        '{"status":"dispatched","kind":"issue","number":5123,"worker":"123","model":"hy4","action":"implement"}',
    ],
)
def test_result_parsing_accepts_any_key_order_and_prefix(line: str) -> None:
    result = parse_result(line)
    assert result is not None and result.assignment is not None
    assert (result.assignment.number, result.assignment.worker) == (5123, "123")


def test_result_parsing_deferred_busy_and_malformed() -> None:
    deferred = parse_result(
        'x {"reason":"Every eligible task is claimed","status":"deferred","details":["a"]}'
    )
    assert deferred is not None and (deferred.status, deferred.details) == ("deferred", ["a"])
    busy = parse_result('{"status":"busy","number":3953,"owner_worker":"77"}')
    assert busy is not None and (busy.busy_number, busy.owner_worker) == (3953, "77")
    assert parse_result("irrelevant workflow log {not json") is None
    broken = parse_result('{"status":"dispatched","number":"abc"}')
    assert broken is not None and broken.status == "unknown"


def test_failure_reasons_drop_generic_exit_lines_and_read_tracebacks() -> None:
    notes: list[dict[str, JSON]] = [
        {"annotation_level": "failure", "message": "Process completed with exit code 1."},
        {
            "annotation_level": "failure",
            "message": "Exact requested model could not be selected; no provider fallback",
        },
        {"annotation_level": "warning", "message": "ignored"},
    ]
    assert failure_reasons(notes) == [
        "Exact requested model could not be selected; no provider fallback"
    ]
    log = "Traceback...\nValueError: Unsupported factory provider: bogus\n"
    assert failure_reasons([], log) == ["Unsupported factory provider: bogus"]


def test_failed_outcome_still_links_the_work_pr_with_saved_code() -> None:
    # Verbatim from live run 44968096160 (issue #5592).
    outcome = parse_outcome(
        "[factory-outcome] failed: Code saved on the work PR; validation incomplete; "
        "worker failed: work_failure (work PR: 5660)"
    )
    assert outcome is not None and (outcome.result, outcome.pr) == (Result.FAILED, 5660)


def test_worker_outcome_verdicts() -> None:
    delivered = parse_outcome(
        "t [factory-outcome] code_delivered: Verified PR #5701 was handed off; agent outcome: success"
    )
    assert delivered is not None and (delivered.result, delivered.pr) == (Result.DELIVERED, 5701)
    idle = parse_outcome(
        "[factory-outcome] idle: No code delivered; worker had no useful work to persist"
    )
    assert idle is not None and idle.result is Result.IDLE


def test_github_errors_are_plain() -> None:
    assert isinstance(
        _classify("gh: To get started with GitHub CLI, please run:  gh auth login"), GitHubAuthError
    )
    assert isinstance(_classify("HTTP 403: API rate limit exceeded for user"), GitHubRateLimitError)


# run ------------------------------------------------------------------------------


def test_explicit_selection_forwards_exactly_and_never_prompts(fake: FakeGitHub) -> None:
    code, out = invoke(
        [
            "run",
            "opencode-go",
            "opencode-go/hy4-flash-free",
            "--force",
            "--slot",
            "2",
            "--target",
            "issue:5123",
            "--no-wait",
        ],
        stdin="",
        tty=True,
    )
    assert code == 0
    assert fake.dispatch_inputs() == {
        "provider": "opencode-go",
        "model": "opencode-go/hy4-flash-free",
        "mode": "smoke",
        "action": "advance",
        "force_slot": "true",
        "target": "issue:5123",
        "slot_index": "2",
    }
    assert "Choose" not in out and "Start this?" not in out
    assert "Request sent" in out and "rotisserie factory watch 900" in out


def test_unforced_next_task_request(fake: FakeGitHub) -> None:
    code, _ = invoke(["run", "nvidia", "auto", "--no-wait"])
    assert code == 0
    inputs = fake.dispatch_inputs()
    assert inputs["force_slot"] == "false" and "target" not in inputs and "slot_index" not in inputs


@pytest.mark.parametrize(
    "argv, message",
    [
        (["run", "not-a-provider", "auto"], "Unknown provider"),
        (["run", "opencode-go", "openrouter/some-model"], "does not belong"),
        (["run"], "Name a provider"),
        (["run", "opencode-go", "auto", "--target", "issue:abc"], "--target must look like"),
    ],
)
def test_invalid_requests_exit_2_without_dispatching(
    fake: FakeGitHub, argv: list[str], message: str
) -> None:
    code, out = invoke(argv)
    assert code == 2 and message in out
    assert not any(call[0] == "workflow" for call in fake.calls)


def test_default_run_stops_once_the_model_actually_started(fake: FakeGitHub) -> None:
    scheduler_routes(fake, log=dispatched_log())
    code, out = invoke(["run", "opencode-go", "opencode-go/hy4-flash-free", "--force"])
    assert code == 0, out
    assert "✓ Claimed issue #3953" in out
    assert "New releases: Fix missing publisher and volume titles" in out
    assert "✓ Assigned to OpenCode Go: opencode-go/hy4-flash-free" in out
    assert "✓ Model started working on #3953" in out
    assert out.index("Assigned to") < out.index("Model started working")
    assert "Refreshing run status" not in out and "JOBS" not in out


def test_assigned_worker_that_never_starts_is_a_failure(fake: FakeGitHub) -> None:
    scheduler_routes(
        fake,
        log=dispatched_log(),
        worker=[
            job(
                "run / factory",
                [
                    step("Select execution candidate at dispatch time", conclusion="failure"),
                    step("Run continuous fixed-model factory session", conclusion="skipped"),
                ],
                conclusion="failure",
                job_id=22,
            ),
        ],
    )
    fake.routes["actions/runs/950$"] = {"id": 950, "status": "completed", "conclusion": "failure"}
    fake.routes["check-runs/22/annotations"] = [
        {
            "annotation_level": "failure",
            "message": "Exact requested model could not be selected; no provider fallback",
        }
    ]
    code, out = invoke(["run", "opencode-go", "opencode-go/hy4-flash-free", "--force"])
    assert code == 4
    assert "Model started working" not in out
    assert "✗ The model never started" in out
    assert "Exact requested model could not be selected" in out


def test_follow_reports_delivered_pr_with_checks_not_as_finished(fake: FakeGitHub) -> None:
    scheduler_routes(
        fake,
        log=dispatched_log(),
        worker=[
            job(
                "run / factory",
                [step("Run continuous fixed-model factory session", at=60)],
                job_id=22,
            ),
            job(
                "Persist trusted worker outcome in Neon",
                [step("Classify and persist this completed worker")],
                job_id=23,
            ),
        ],
    )
    fake.routes["actions/runs/950$"] = {
        "id": 950,
        "status": "completed",
        "conclusion": "success",
        "updated_at": "2026-11-21T23:30:00Z",
    }
    fake.texts["actions/jobs/23/logs"] = (
        "[factory-outcome] code_delivered: Verified PR #5701 was handed off; agent outcome: success\n"
    )
    fake.routes["pr-view"] = {
        "state": "OPEN",
        "reviewDecision": "REVIEW_REQUIRED",
        "title": "Fix titles",
        "url": f"https://github.com/{REPO}/pull/5701",
        "statusCheckRollup": [{"status": "IN_PROGRESS"}, {"conclusion": "SUCCESS"}],
    }
    code, out = invoke(["run", "opencode-go", "auto", "--force", "--follow"])
    assert code == 0, out
    assert "✓ Pull request #5701 delivered" in out
    assert "1 check(s) still running" in out and "Not merged yet" in out


def test_deferred_explains_the_real_reason(fake: FakeGitHub) -> None:
    scheduler_routes(
        fake,
        log='{"status":"deferred","reason":"Nothing is waiting: every open item is finished, claimed or blocked.","details":[]}\n',
    )
    code, out = invoke(["run", "opencode-go", "auto", "--force"])
    assert code == 3
    assert "✗ No work assigned" in out
    assert "Reason: Nothing is waiting" in out
    assert "See why: rotisserie factory why\n" in out


def test_deferred_target_points_at_why_for_that_issue(fake: FakeGitHub) -> None:
    scheduler_routes(
        fake,
        log='{"status":"deferred","reason":"Nothing is waiting: every open item is finished, claimed or blocked.","details":[]}\n',
    )
    code, out = invoke(["run", "opencode-go", "auto", "--force", "--target", "issue:3953"])
    assert code == 3
    assert "See why: rotisserie factory why issue:3953" in out


def test_busy_target_offers_take_over_and_cancel(fake: FakeGitHub) -> None:
    scheduler_routes(
        fake, log='{"status":"busy","number":3953,"owner":"factory:77","owner_worker":"77"}\n'
    )
    code, out = invoke(["run", "opencode-go", "auto", "--force", "--target", "issue:3953"])
    assert code == 3
    assert "#3953 is already being worked on" in out
    assert "--take-over" in out and "rotisserie factory cancel issue:3953" in out


def test_quota_deferred_scheduler_is_explained(fake: FakeGitHub) -> None:
    jobs = [
        job(
            "dispatch",
            [step("Report quota-deferred targeted dispatch without failing a worker")],
            job_id=11,
        )
    ]
    scheduler_routes(fake, log="", jobs=jobs)
    code, out = invoke(["run", "opencode-go", "auto", "--force"])
    assert code == 3 and "API allowance" in out


def test_scheduler_failure_shows_the_error(fake: FakeGitHub) -> None:
    jobs = [
        job(
            "dispatch",
            [step("Advance selected issue or pull request", conclusion="failure")],
            conclusion="failure",
            job_id=11,
        )
    ]
    scheduler_routes(
        fake, log="Traceback\nValueError: Unsupported factory provider: bogus\n", jobs=jobs
    )
    code, out = invoke(["run", "opencode-go", "auto"])
    assert code == 4 and "Unsupported factory provider: bogus" in out


def test_unreadable_decision_is_not_reported_as_success(fake: FakeGitHub) -> None:
    scheduler_routes(fake, log="nothing useful\n")
    code, out = invoke(["run", "opencode-go", "auto"])
    assert code == 6 and "could not be read" in out


def test_github_login_and_rate_limit_errors_exit_5(fake: FakeGitHub) -> None:
    fake.error = GitHubAuthError("GitHub CLI is not logged in. Run: gh auth login")
    code, out = invoke(["run", "opencode-go", "auto"])
    assert code == 5 and "gh auth login" in out
    fake.error = GitHubRateLimitError("GitHub's API limit is used up for now.")
    code, out = invoke(["status"])
    assert code == 5


def test_json_output_is_one_document(fake: FakeGitHub) -> None:
    scheduler_routes(fake, log=dispatched_log())
    code, out = invoke(["run", "opencode-go", "auto", "--force", "--json"])
    document = json.loads(out)
    assert code == 0 == document["exit_code"]
    assert document["result"] == "started"
    assert document["assignment"]["number"] == 3953
    assert document["worker_run_id"] == 950


def test_ctrl_c_stops_watching_without_cancelling(fake: FakeGitHub) -> None:
    scheduler_routes(fake, log=dispatched_log())
    fake.routes["actions/runs/900$"] = {"id": 900, "status": "in_progress"}
    fake.routes["actions/runs/900/jobs"] = {"jobs": [job("dispatch", [], status="queued")]}  # noqa: E501
    clock = FakeClock()

    def interrupt(count: int) -> None:
        if count >= 2:
            raise KeyboardInterrupt

    clock.on_sleep = interrupt
    code, out = invoke(["run", "opencode-go", "auto", "--force"], clock=clock)
    assert code == 130
    assert "Nothing was cancelled" in out and "rotisserie factory watch 900" in out
    assert not any(call[:2] == ["run", "cancel"] for call in fake.calls)


def test_interactive_menus_choose_provider_model_and_work(fake: FakeGitHub) -> None:
    providers = runtime.providers()
    go = next(index for index, item in enumerate(providers) if item.key == "opencode-go") + 1
    # provider, model (auto), work (next task), force (default yes), confirm.
    answers = f"{go}\n1\n1\n\ny\n"
    code, out = invoke(["run", "--no-wait"], stdin=answers, tty=True)
    assert code == 0, out
    inputs = fake.dispatch_inputs()
    assert (inputs["provider"], inputs["model"], inputs["force_slot"]) == (
        "opencode-go",
        "auto",
        "true",
    )


def test_non_interactive_without_model_uses_auto(fake: FakeGitHub) -> None:
    code, _ = invoke(["run", "opencode-go", "--no-wait"])
    assert code == 0 and fake.dispatch_inputs()["model"] == "auto"


def test_take_over_stops_the_holder_first(fake: FakeGitHub) -> None:
    holder = runtime.module("factory_worker_slots").worker_id("z-ai", 0)
    released = {"done": False}

    def issue() -> JSON:
        labels: list[JSON] = [] if released["done"] else [{"name": f"factory:{holder}"}]
        return {"number": 3953, "title": "t", "labels": labels, "state": "open"}

    scheduler_routes(fake, log=dispatched_log())
    fake.routes["issues/3953$"] = issue
    fake.routes = {
        "status=in_progress": {
            "workflow_runs": [
                {
                    "id": 777,
                    "display_title": f"Factory {holder} · fixed-model entry",
                    "status": "in_progress",
                    "run_started_at": "2026-11-21T22:00:00Z",
                }
            ]
        },
        **fake.routes,
    }
    clock = FakeClock()

    def release(count: int) -> None:
        released["done"] = True

    clock.on_sleep = release
    code, out = invoke(
        ["run", "opencode-go", "auto", "--target", "issue:3953", "--take-over", "--no-wait"],
        clock=clock,
    )
    assert code == 0, out
    cancel = [call for call in fake.calls if call[:2] == ["run", "cancel"]]
    assert cancel and cancel[0][2] == "777"
    workflow = next(index for index, call in enumerate(fake.calls) if call[0] == "workflow")
    assert fake.calls.index(cancel[0]) < workflow
    assert fake.dispatch_inputs()["force_slot"] == "true"
    assert "#3953 is free" in out


def test_take_over_removes_an_outside_agents_claim_first(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rotisserie.factory_console.service import external

    claim = "11111111-1111-4111-8111-111111111111"
    revoked: list[dict[str, str]] = []
    monkeypatch.setattr(
        external,
        "holder",
        lambda github, number: (
            None if revoked else {"claim_id": claim, "client": "codebuff-phone", "actor": "josh"}
        ),
    )

    def revoke(github: object, **kwargs: str) -> dict[str, object]:
        revoked.append(kwargs)
        return {"status": "revoked", "claim_id": kwargs["claim_id"]}

    monkeypatch.setattr(external, "revoke", revoke)
    scheduler_routes(fake, log=dispatched_log())
    fake.routes["issues/3953$"] = {"number": 3953, "title": "t", "labels": [], "state": "open"}
    code, out = invoke(
        [
            "--json",
            "run",
            "opencode-go",
            "auto",
            "--target",
            "issue:3953",
            "--take-over",
            "--no-wait",
        ],
    )
    assert code == 0, out
    assert revoked and revoked[0]["claim_id"] == claim
    assert revoked[0]["reason"] == "operator take-over of #3953"
    assert json.loads(out)["revoked_external_claim"]["claim_id"] == claim
    assert fake.dispatch_inputs()["force_slot"] == "true"


def test_take_over_stops_when_the_outside_claim_cannot_be_removed(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rotisserie.factory_console.service import external

    monkeypatch.setattr(
        external,
        "holder",
        lambda github, number: {
            "claim_id": "11111111-1111-4111-8111-111111111111",
            "client": "codebuff-phone",
            "actor": "josh",
        },
    )
    monkeypatch.setattr(
        external, "revoke", lambda github, **kwargs: {"status": "conflict", "reason": "stale_claim"}
    )
    scheduler_routes(fake, log=dispatched_log())
    fake.routes["issues/3953$"] = {"number": 3953, "title": "t", "labels": [], "state": "open"}
    code, out = invoke(
        ["run", "opencode-go", "auto", "--target", "issue:3953", "--take-over", "--no-wait"]
    )
    assert code != 0
    assert "stale_claim" in out
    assert not any(call[0] == "workflow" for call in fake.calls)


def test_take_over_without_neon_explains_and_continues(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rotisserie.factory_console.service import external

    def unavailable(github: object, number: int) -> None:
        raise external.ClaimStoreUnavailable("FACTORY_STATE_DSN_FILE is not set")

    monkeypatch.setattr(external, "holder", unavailable)
    scheduler_routes(fake, log=dispatched_log())
    fake.routes["issues/3953$"] = {"number": 3953, "title": "t", "labels": [], "state": "open"}
    code, out = invoke(
        ["run", "opencode-go", "auto", "--target", "issue:3953", "--take-over", "--no-wait"]
    )
    assert code == 0, out
    assert "rotisserie factory revoke" in out


# other commands ---------------------------------------------------------------------


def test_settings_validate_before_writing(fake: FakeGitHub) -> None:
    code, out = invoke(["settings", "pr-wip-limit", "15"])
    assert code == 0 and "Open pull request limit set to 15" in out
    assert ["variable", "set", "FACTORY_PR_WIP_LIMIT", "--repo", REPO, "--body", "15"] in fake.calls
    code, out = invoke(["settings", "FACTORY_PR_WIP_LIMIT", "lots"])
    assert code == 2 and "whole number" in out
    code, out = invoke(["settings", "priority-order", "e2e,bogus"])
    assert code == 2 and "unknown priority" in out


def test_settings_list_comes_from_the_factory_registry(fake: FakeGitHub) -> None:
    code, out = invoke(["settings", "--json"])
    names = {row["name"] for row in json.loads(out)["settings"]}
    assert {"FACTORY_SLOTS_PER_PROVIDER", "FACTORY_PR_WIP_LIMIT", "FACTORY_PRIORITY_ORDER"} <= names
    assert code == 0


def test_providers_are_discovered_from_the_factory_runtime() -> None:
    keys = {provider.key for provider in runtime.providers()}
    sources = {str(item) for item in runtime.module("factory_worker_slots").sources()}
    assert len(keys) == len(sources)
    assert "opencode-go" in keys and "opencode-free" not in keys
    go = runtime.resolve_provider("opencode-go")
    assert go.model_prefix == "opencode-go/" and go.secrets == ("OPENCODE_GO_CLIENT_ID",)
    assert runtime.resolve_provider("openrouter-free").model_prefix == "openrouter/"


def test_graph_commands_still_require_config(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["inspect"])
    assert raised.value.code == 2
    assert "--config is required" in capsys.readouterr().err


def test_factory_help_lists_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["factory", "--help"])
    text = capsys.readouterr().out
    for command in ("run", "status", "report", "stats", "settings", "cancel", "watch"):
        assert command in text


def test_wheel_ships_every_runtime_module_the_console_loads() -> None:
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / "pyproject.toml").read_text())
    included = config["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert set(runtime.RUNTIME_MODULES) <= set(runtime.RUNTIME_SOURCES)
    for name, source in runtime.RUNTIME_SOURCES.items():
        assert included[source] == f"rotisserie/factory_console/factory_runtime/{name}.py"
        assert (root / source).is_file()


# why ---------------------------------------------------------------------------------

HEAD_SHA = "c" * 40


def open_issue(number: int, *labels: str, body: str = "", title: str = "") -> Obj:
    return {
        "number": number,
        "title": title or f"Issue {number}",
        "body": body,
        "labels": [{"name": label} for label in labels],
        "createdAt": "2026-11-01T00:00:00Z",
        "updatedAt": "2026-11-20T00:00:00Z",
    }


def factory_pr(number: int, issue: int, *labels: str) -> Obj:
    return {
        "number": number,
        "title": f"Fix #{issue}",
        "body": "",
        "labels": [{"name": label} for label in labels],
        "headRefName": f"factory/12-{issue}-change",
        "headRefOid": "d" * 40,
        "createdAt": "2026-11-10T00:00:00Z",
        "updatedAt": "2026-11-20T00:00:00Z",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "isCrossRepository": False,
    }


def why_routes(
    fake: FakeGitHub,
    issues: list[Obj],
    prs: list[Obj] | None = None,
    *,
    stage: str | None = "legacy",
    blockers: dict[int, list[JSON]] | None = None,
) -> None:
    import base64

    fake.routes["issue-list"] = list[JSON](issues)
    fake.routes["pr-list"] = list[JSON](prs or [])
    fake.routes["commits/HEAD$"] = {"sha": HEAD_SHA}
    if stage is not None:
        control = {
            "schema_version": 1,
            "stage": stage,
            "lane": {"name": "issue-intake", "subjects": ["label:factory:unowned"]},
        }
        fake.routes["contents/.github/factory-rotisserie-control.json"] = {
            "content": base64.b64encode(json.dumps(control).encode()).decode()
        }
    for number, rows in (blockers or {}).items():
        fake.routes[f"issues/{number}/dependencies/blocked_by"] = rows


def test_why_says_an_unblocked_issue_can_start_now(fake: FakeGitHub) -> None:
    why_routes(fake, [open_issue(41, "factory:unowned", title="Fix the importer")])
    code, out = invoke(["why", "issue:41"])
    assert code == 0
    assert "#41  Fix the importer" in out
    assert "Eligible now" in out
    assert "prioritize issue:41" in out


def test_why_names_every_reason_an_issue_is_skipped(fake: FakeGitHub) -> None:
    issues = [
        open_issue(7, "factory:12", "factory:blocked", body="Depends on #8"),
        open_issue(8),
    ]
    why_routes(fake, issues, [factory_pr(300, 7, "factory:review")])
    code, out = invoke(["why", "7"])
    assert code == 0
    assert "Not picked up automatically" in out
    assert "It is labeled factory:blocked." in out
    assert "Worker 12" in out and "is already on it." in out
    assert "It depends on #8, still open." in out
    assert "Pull request #300 already implements it" in out


def test_why_reports_a_live_github_dependency_blocker(fake: FakeGitHub) -> None:
    why_routes(
        fake,
        [open_issue(41, "factory:unowned")],
        blockers={41: [{"number": 99, "state": "open"}, {"number": 98, "state": "closed"}]},
    )
    code, out = invoke(["why", "issue:41"])
    assert code == 0
    assert "GitHub lists open blocking issues: #99." in out
    assert "#98" not in out


def test_why_explains_review_backlog_pressure_with_numbers(fake: FakeGitHub) -> None:
    prs = [factory_pr(300 + n, 900 + n, "factory:review") for n in range(6)]
    why_routes(fake, [open_issue(41, "factory:unowned")], prs)
    code, out = invoke(["--json", "why", "issue:41"])
    data = json.loads(out)
    assert code == 0
    assert data["status"] == "waiting"
    assert data["explanation"]["review_backlog"] == 6
    assert data["explanation"]["review_backlog_limit"] == 6
    assert "FACTORY_REVIEW_BACKLOG_LIMIT" in data["reasons"][0]


def test_why_main_breakage_bypasses_review_backlog_pressure(fake: FakeGitHub) -> None:
    prs = [factory_pr(300 + n, 900 + n, "factory:review") for n in range(6)]
    why_routes(fake, [open_issue(41, "factory:unowned", "main-breakage")], prs)
    code, out = invoke(["--json", "why", "issue:41"])
    assert code == 0
    assert json.loads(out)["status"] == "ready"


def test_why_runs_the_real_rotisserie_intake_decision(fake: FakeGitHub) -> None:
    prs = [factory_pr(300 + n, 900 + n, "factory:review") for n in range(6)]
    issues = [open_issue(41, "factory:unowned"), open_issue(42, "factory:unowned")]
    why_routes(fake, issues, prs, stage="expanded")
    code, out = invoke(["why", "issue:41"])
    assert code == 0
    assert "held back by the factory's limits" in out
    assert "Raise the limit:  rotisserie factory settings FACTORY_REVIEW_BACKLOG_LIMIT N" in out
    code, out = invoke(["--verbose", "why", "issue:41"])
    assert "intake not-authorized" in out

    why_routes(fake, issues, [], stage="expanded")
    code, out = invoke(["--json", "why", "issue:41"])
    data = json.loads(out)
    assert data["status"] == "ready"
    assert data["explanation"]["intake"] == "authorized"


def test_why_missing_intake_control_admits_nothing_like_the_controller(
    fake: FakeGitHub,
) -> None:
    why_routes(fake, [open_issue(41, "factory:unowned")], stage=None)
    fake.routes["contents/.github/factory-rotisserie-control.json"] = lambda: (_ for _ in ()).throw(
        GitHubNotFoundError("missing")
    )
    code, out = invoke(["why", "issue:41"])
    assert code == 0
    assert "Rotisserie intake admits nothing right now" in out
    assert "factory-rotisserie-control.json is missing" in out


def test_why_without_a_target_summarizes_the_whole_queue(fake: FakeGitHub) -> None:
    issues = [
        open_issue(1, "factory:unowned", title="Ready one"),
        open_issue(2, "factory:blocked"),
        open_issue(3, "factory:blocked"),
        open_issue(4, "epic"),
        open_issue(5, body="<!-- factory-execution:manual-only -->"),
    ]
    why_routes(fake, issues)
    code, out = invoke(["why"])
    assert code == 0
    assert "Open issues (5)" in out
    assert "1 can start now." in out
    assert "#1" in out and "Ready one" in out
    assert re.search(r"2\s+are labeled blocked", out)
    assert re.search(r"1\s+are epics or parent work", out)
    code, out = invoke(["--json", "why"])
    data = json.loads(out)
    assert data["ready"] == [1]
    assert data["not_starting"] == {"blocked": 2, "parent": 1, "manual-only": 1}


def test_why_refuses_pull_requests(fake: FakeGitHub) -> None:
    why_routes(fake, [])
    code, out = invoke(["why", "pr:5"])
    assert code == 2
    assert "'why' explains issues" in out
    fake.routes["issues/77$"] = {"number": 77, "pull_request": {}, "title": "A PR"}
    code, out = invoke(["why", "77"])
    assert code == 2
    assert "#77 is a pull request" in out


def test_why_explains_a_closed_issue(fake: FakeGitHub) -> None:
    why_routes(fake, [])
    fake.routes["issues/77$"] = {"number": 77, "title": "Old", "state": "closed", "labels": []}
    code, out = invoke(["why", "issue:77"])
    assert code == 0
    assert "It is closed." in out
    assert "--force" not in out


# unstick -------------------------------------------------------------------------------


def stats_snapshot(*models: tuple[str, str, int, int, Obj]) -> Obj:
    import base64

    entries: list[JSON] = [
        {
            "source": source,
            "model": model,
            "health": health,
            "windows": {
                "7d": {"runs": runs, "delivered": delivered, "failed": runs - delivered},
                "24h": {"runs": 0, "delivered": 0, "failed": 0},
            },
        }
        for source, model, runs, delivered, health in models
    ]
    payload = {"generated_at": "2026-11-21T22:50:00Z", "models": entries, "providers": []}
    return {"content": base64.b64encode(json.dumps(payload).encode()).decode()}


HEALTHY: Obj = {"status": "healthy"}


LIVE_MODELS: dict[str, frozenset[str] | None] = {}


@pytest.fixture(autouse=True)
def live_catalog(monkeypatch: pytest.MonkeyPatch) -> dict[str, frozenset[str] | None]:
    """Never read this machine's OpenCode CLI or the network in tests."""
    from rotisserie.factory_console.service import catalog

    LIVE_MODELS.clear()
    monkeypatch.setattr(catalog, "live_models", lambda provider: LIVE_MODELS.get(provider.key))
    monkeypatch.setattr(catalog, "opencode_catalog", lambda: [])
    return LIVE_MODELS


def unstick_routes(fake: FakeGitHub, prs: list[Obj], issues: list[Obj] | None = None) -> None:
    why_routes(fake, issues or [open_issue(41, "factory:unowned")], prs)
    fake.routes["secret-list"] = [{"name": "OPENCODE_GO_CLIENT_ID"}, {"name": "NVIDIA_API_KEY"}]
    fake.routes["contents/factory-stats.json"] = stats_snapshot(
        ("opencode-free", "hy4-flash-free", 10, 2, HEALTHY),
        ("opencode-free", "nemotron-3.5-lightning-free", 10, 5, HEALTHY),
        # Best rate, but its provider is not set up here.
        ("z-ai", "glm-9", 4, 4, HEALTHY),
        # Paused right now.
        (
            "nvidia",
            "moonshotai/kimi-k3.1-pro",
            10,
            9,
            {"status": "cooling", "cooldown_until": "2026-11-22T05:00:00Z"},
        ),
    )


def backlog(*specs: tuple[int, str, str]) -> list[Obj]:
    prs = []
    for number, stage, owner in specs:
        pr = factory_pr(number, 900 + number, stage, owner)
        if stage == "factory:conflict":
            pr["labels"] = [{"name": "factory:review"}, {"name": owner}]
            pr["mergeable"] = "CONFLICTING"
        prs.append(pr)
    return prs


FULL_BACKLOG = backlog(
    (301, "factory:review", "factory:unowned"),
    (302, "factory:ci", "factory:unowned"),
    (303, "factory:changes-requested", "factory:unowned"),
    (304, "factory:conflict", "factory:unowned"),
    (305, "factory:review", f"factory:{WORKER}"),
    (306, "factory:review", "factory:unowned"),
    (307, "factory:review", "factory:unowned"),
)


def test_unstick_plans_backlog_work_with_the_best_set_up_model(fake: FakeGitHub) -> None:
    unstick_routes(fake, FULL_BACKLOG)
    code, out = invoke(["--json", "unstick", "--dry-run"])
    data = json.loads(out)
    plan = data["plan"]
    assert code == 0
    assert plan["needed_to_reopen_intake"] == 2  # 7 in the backlog, limit 6
    work = [(item["task"]["target"], item["task"]["action"]) for item in plan["assignments"]]
    # Reviews first (they can leave the backlog directly), then repairs, then conflicts.
    assert work == [
        ("pr:301", "review"),
        ("pr:306", "review"),
        ("pr:307", "review"),
        ("pr:302", "ci-repair"),
        ("pr:303", "review-findings"),
        ("pr:304", "merge-conflict"),
    ]
    assert {item["model"] for item in plan["assignments"]} == {
        "opencode-go/nemotron-3.5-lightning-free"
    }
    assert "5 of 10 runs delivered" in plan["model_choice"]
    assert [reason["prs"] for reason in plan["held"]] == [[305]]
    assert not any(call[0] == "workflow" for call in fake.calls)


def test_unstick_starts_one_forced_targeted_worker_per_task(fake: FakeGitHub) -> None:
    unstick_routes(fake, FULL_BACKLOG)
    code, out = invoke(["unstick", "--max", "2", "--yes"])
    assert code == 0
    assert "Plan: 2 workers" in out
    assert "4 more could be started (raise --max)" in out
    sent = [
        dict(call[i + 1].split("=", 1) for i, item in enumerate(call) if item == "-f")
        for call in fake.calls
        if call[0] == "workflow"
    ]
    assert [(item["target"], item["action"], item["force_slot"]) for item in sent] == [
        ("pr:301", "review", "true"),
        ("pr:306", "review", "true"),
    ]
    assert all(item["provider"] == "opencode-go" for item in sent)
    assert "pr:301: request sent" in out


def test_unstick_needs_confirmation_outside_a_terminal(fake: FakeGitHub) -> None:
    unstick_routes(fake, FULL_BACKLOG)
    code, out = invoke(["unstick"])
    assert code == 3
    assert "Add --yes to start these workers." in out
    assert not any(call[0] == "workflow" for call in fake.calls)


def test_unstick_asks_before_starting_in_a_terminal(fake: FakeGitHub) -> None:
    unstick_routes(fake, FULL_BACKLOG)
    code, out = invoke(["unstick"], stdin="n\n", tty=True)
    assert code == 3
    assert "Nothing was started." in out
    assert not any(call[0] == "workflow" for call in fake.calls)


def test_unstick_without_backpressure_starts_issues_that_can_start(fake: FakeGitHub) -> None:
    unstick_routes(fake, [], issues=[open_issue(41, "factory:unowned"), open_issue(42, "epic")])
    code, out = invoke(["--json", "unstick", "--dry-run"])
    plan = json.loads(out)["plan"]
    assert [item["task"]["target"] for item in plan["assignments"]] == ["issue:41"]
    assert plan["assignments"][0]["task"]["action"] == "advance"
    assert plan["diagnosis"][0]["code"] == "ready"


def test_unstick_honors_an_explicit_provider_and_model(fake: FakeGitHub) -> None:
    unstick_routes(fake, FULL_BACKLOG)
    code, out = invoke(
        ["--json", "unstick", "opencode-go", "opencode-go/hy4-flash-free", "--dry-run"]
    )
    plan = json.loads(out)["plan"]
    assert {item["model"] for item in plan["assignments"]} == {"opencode-go/hy4-flash-free"}
    assert plan["model_choice"] == "you chose it"


def test_unstick_reports_when_no_model_has_a_track_record(fake: FakeGitHub) -> None:
    unstick_routes(fake, FULL_BACKLOG)
    fake.routes["secret-list"] = []
    code, out = invoke(["unstick", "--dry-run"])
    assert code == 3
    assert "No set-up, unpaused model that the provider still offers has a track record" in out


def test_paused_models_count_as_paused_only_until_the_cooldown_ends() -> None:
    from rotisserie.factory_console.service import reliability

    past: Obj = {"status": "cooling", "cooldown_until": "2026-11-21T20:00:00Z"}
    future: Obj = {"status": "cooling", "cooldown_until": "2026-11-22T05:00:00Z"}
    assert reliability.health_status(past, START) == "healthy"
    assert reliability.health_status(future, START) == "paused"
    assert reliability.health_status({"status": "retired"}, START) == "retired"


def test_stats_does_not_show_an_expired_pause(fake: FakeGitHub) -> None:
    fake.routes["contents/factory-stats.json"] = stats_snapshot(
        (
            "opencode-free",
            "hy4-flash-free",
            5,
            1,
            {"status": "cooling", "cooldown_until": "2026-11-21T20:00:00Z"},
        ),
    )
    code, out = invoke(["stats"])
    assert code == 0
    assert "paused until" not in out


def test_service_layer_stays_front_end_neutral() -> None:
    """Web, TUI and MCP front ends reuse the service; it must never print or prompt."""
    import ast
    from pathlib import Path

    service = Path(commands.__file__).with_name("service")
    for path in sorted(service.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module not in (
                    "rotisserie.factory_console.ui",
                    "rotisserie.factory_console.commands",
                    "argparse",
                ), f"{path.name} imports {node.module}"
            if isinstance(node, ast.Import):
                assert all(alias.name != "argparse" for alias in node.names), path.name
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in ("print", "input"), f"{path.name} calls {node.func.id}"


def test_status_shows_each_workers_model_from_its_run_name(fake: FakeGitHub) -> None:
    other = runtime.module("factory_worker_slots").worker_id("opencode-free", 2)
    third = runtime.module("factory_worker_slots").worker_id("opencode-free", 3)
    fake.routes[r"issues\?state=open&per_page=100&page=1&"] = [
        {"number": 11, "title": "Exact model", "labels": [{"name": f"factory:{WORKER}"}]},
        {"number": 12, "title": "Auto model", "labels": [{"name": f"factory:{other}"}]},
        {"number": 13, "title": "Old run", "labels": [{"name": f"factory:{third}"}]},
    ]
    fake.routes[r"free-model-factory-entry.yml/runs\?status=in_progress"] = {
        "workflow_runs": [
            {
                "id": 1,
                "display_title": f"Factory {WORKER} · opencode-go/hy4-flash-free · fixed-model entry",
                "status": "in_progress",
            },
            {"id": 2, "display_title": f"Factory {other} · auto · fixed-model entry"},
            {"id": 3, "display_title": f"Factory {third} · fixed-model entry"},
        ]
    }
    code, out = invoke(["status"])
    assert code == 0
    assert "Model" in out
    assert "opencode-go/hy4-flash-free" in out
    assert "picked by the worker" in out
    assert "not recorded (older run)" in out
    code, out = invoke(["--json", "status"])
    models = {item["ref"]: item["model"] for item in json.loads(out)["working"]}
    assert models == {
        "issue:11": "opencode-go/hy4-flash-free",
        "issue:12": "auto",
        "issue:13": None,
    }


def test_unstick_skips_models_the_provider_no_longer_offers(fake: FakeGitHub) -> None:
    unstick_routes(fake, FULL_BACKLOG)
    # nemotron has the best record but OpenCode Go no longer lists it; the worker
    # would refuse it ("not a current zero-price tool-capable Go model").
    LIVE_MODELS["opencode-go"] = frozenset({"opencode-go/hy4-flash-free"})
    code, out = invoke(["--json", "unstick", "--dry-run"])
    plan = json.loads(out)["plan"]
    assert {item["model"] for item in plan["assignments"]} == {"opencode-go/hy4-flash-free"}
    assert "could not check" not in plan["model_choice"]


def test_unstick_ranks_unverifiable_models_after_checked_ones(fake: FakeGitHub) -> None:
    unstick_routes(fake, FULL_BACKLOG)
    LIVE_MODELS["opencode-go"] = None  # no readable live list on this machine
    code, out = invoke(["--json", "unstick", "--dry-run"])
    plan = json.loads(out)["plan"]
    assert "could not check that the provider still offers it" in plan["model_choice"]


def test_models_flags_track_record_models_the_provider_dropped(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rotisserie.factory_console.service import catalog

    fake.routes["contents/factory-stats.json"] = stats_snapshot(
        ("opencode-free", "hy4-flash-free", 10, 2, HEALTHY),
        ("opencode-free", "deepseek-v4.1-flash-free", 29, 4, HEALTHY),
    )
    monkeypatch.setattr(catalog, "opencode_catalog", lambda: ["opencode-go/hy4-flash-free"])
    code, out = invoke(["--json", "models", "opencode-go"])
    rows = {row["model"]: row for row in json.loads(out)["models"]}
    assert code == 0
    assert rows["opencode-go/hy4-flash-free"]["offered"] is True
    assert rows["opencode-go/deepseek-v4.1-flash-free"]["offered"] is False
    assert "no longer offered" in rows["opencode-go/deepseek-v4.1-flash-free"]["source"]


# outcomes ------------------------------------------------------------------------------


def test_outcome_goals_follow_the_target_state_not_the_workers_word() -> None:
    from rotisserie.factory_console.service.outcomes import judge
    from rotisserie.factory_console.tracker import WorkerOutcome

    idle = WorkerOutcome(Result.IDLE, "nothing to do")
    conflicted: Obj = {"mergeable": False, "mergeable_state": "dirty", "state": "open"}
    clean: Obj = {"mergeable": True, "mergeable_state": "clean", "state": "open"}
    computing: Obj = {"mergeable": None, "mergeable_state": "unknown", "state": "open"}
    assert judge("merge-conflict", "pr", conflicted, idle)[0] == "not-met"
    assert judge("merge-conflict", "pr", clean, idle)[0] == "met"
    assert judge("merge-conflict", "pr", computing, idle)[0] == "pending"
    stuck: Obj = {"state": "open", "labels": [{"name": "factory:changes-requested"}]}
    moved: Obj = {"state": "open", "labels": [{"name": "factory:review"}]}
    assert judge("review-findings", "pr", stuck, idle) == (
        "not-met",
        "it is still changes-requested",
    )
    assert judge("review-findings", "pr", moved, idle)[0] == "met"
    assert judge("review", "pr", moved, idle)[0] == "not-met"
    assert judge("ci-repair", "pr", {"merged": True}, idle)[0] == "met"
    delivered = WorkerOutcome(Result.DELIVERED, "Verified PR #9 was handed off", 9)
    assert judge("implement", "issue", {}, delivered) == ("met", "pull request #9 was delivered")


def test_latest_misses_ignore_older_requests_and_unstarted_ones() -> None:
    from rotisserie.factory_console.service.outcomes import RequestOutcome, latest_misses

    def outcome(run: int, target: str, goal: str) -> RequestOutcome:
        return RequestOutcome(
            run, START, target, "dispatched", "", "review", "m", "w", None, "", "", goal, ""
        )

    found = [
        outcome(4, "pr:1", "not-started"),  # deferred: does not hide the real result
        outcome(3, "pr:1", "met"),
        outcome(2, "pr:1", "not-met"),  # older miss, since fixed
        outcome(1, "pr:2", "not-met"),
    ]
    assert [item.scheduler_run for item in latest_misses(found)] == [1]


def test_outcomes_flags_an_idle_worker_that_left_conflicts(fake: FakeGitHub) -> None:
    fake.routes["fixed-model-factory-dispatch.yml/runs"] = {
        "workflow_runs": [
            {
                "id": 900,
                "display_title": "Factory dispatch · pr:3953",
                "status": "completed",
                "created_at": "2026-11-21T22:30:00Z",
            },
            {"id": 901, "display_title": "Factory dispatch · opencode-go", "status": "completed"},
        ]
    }
    fake.routes["actions/runs/900/jobs"] = {"jobs": list[JSON](DISPATCH_DONE)}
    fake.texts["actions/jobs/11/logs"] = dispatched_log(
        kind="pr", number=3953, action="merge-conflict", dispatched_at=int(START.timestamp())
    )
    fake.routes["free-model-factory-entry.yml/runs"] = {
        "workflow_runs": [
            {
                "id": 950,
                "display_title": f"Factory {WORKER} · opencode-go/hy4-flash-free · fixed-model entry",
                "created_at": "2026-11-21T23:00:31Z",
            }
        ]
    }
    fake.routes["actions/runs/950$"] = {"id": 950, "status": "completed"}
    fake.routes["actions/runs/950/jobs"] = {
        "jobs": [job("Persist trusted worker outcome in Neon", [step("Classify")], job_id=31)]
    }
    fake.texts["actions/jobs/31/logs"] = "[factory-outcome] idle: The model found nothing to do\n"
    fake.routes["pulls/3953$"] = {"mergeable": False, "mergeable_state": "dirty", "state": "open"}
    code, out = invoke(["outcomes", "--since", "2h"])
    assert code == 0
    assert "Requests since" in out and "(1)" in out  # the routine pass is left out
    assert "pr:3953: it still has merge conflicts" in out
    assert "the worker reported nothing to do, but the goal does not hold" in out
    code, out = invoke(["--json", "outcomes", "--all"])
    data = json.loads(out)
    assert data["goals"]["not-met"] == 1
    assert data["requests"][0]["worker_result"] == "idle"


def test_unstick_does_not_repeat_a_model_that_just_missed_on_the_item() -> None:
    from rotisserie.factory_console.service.unstick import Task, _assign

    ranked = [("opencode-go", "opencode-go/a", "best"), ("opencode-go", "opencode-go/b", "next")]
    task = Task("pr:5575", "merge-conflict", "t", "w")
    fresh = _assign(task, ranked, {})
    assert fresh.model == "opencode-go/a" and "missed" not in fresh.why
    moved = _assign(task, ranked, {"pr:5575": {"opencode-go/a"}})
    assert moved.model == "opencode-go/b"
    assert "opencode-go/a just missed on it" in moved.why
    exhausted = _assign(task, ranked, {"pr:5575": {"opencode-go/a", "opencode-go/b"}})
    assert exhausted.model == "opencode-go/a"
    assert "every offered model already missed on it recently" in exhausted.why


def _missing() -> JSON:
    raise GitHubNotFoundError("gone")


def test_a_pr_whose_files_main_deleted_is_probably_obsolete(fake: FakeGitHub) -> None:
    from rotisserie.factory_console.service.obsolete import pr_staleness

    fake.routes[r"pulls/304/files"] = [
        {"filename": "tests/test_factory_state_cache.py", "status": "modified"},
        {"filename": "app/kept.py", "status": "modified"},
        {"filename": "app/new.py", "status": "added"},  # added files are not checked
        {"filename": "docs/gone.md", "status": "modified"},
    ]
    fake.routes[r"contents/tests/test_factory_state_cache.py"] = _missing
    fake.routes[r"contents/docs/gone.md"] = _missing
    fake.routes[r"contents/app/kept.py"] = {"sha": "x"}
    fake.routes[r"commits\?path=tests/test_factory_state_cache.py"] = [
        {"sha": "11627036aa", "commit": {"message": "chore(factory): move it (#5531)\n\nbody"}}
    ]
    found = pr_staleness(GitHub(REPO, executable="sh"), 304)
    assert found.checked == 3
    assert found.deleted == ("tests/test_factory_state_cache.py", "docs/gone.md")
    assert found.obsolete
    assert "main deleted 2 of the 3 files" in found.text
    assert "removed by 11627036 chore(factory): move it (#5531)" in found.text


def test_unstick_sends_no_worker_to_an_obsolete_pr(fake: FakeGitHub) -> None:
    unstick_routes(fake, FULL_BACKLOG)
    fake.routes[r"pulls/304/files"] = [{"filename": "gone.py", "status": "modified"}]
    fake.routes[r"contents/gone.py"] = _missing
    code, out = invoke(["--json", "unstick", "--dry-run"])
    plan = json.loads(out)["plan"]
    targets = [item["task"]["target"] for item in plan["assignments"]]
    assert "pr:304" not in targets and "pr:301" in targets
    assert [reason["prs"] for reason in plan["obsolete"]] == [[304]]
    code, out = invoke(["unstick", "--dry-run"])
    assert "Needs you: probably obsolete (1)" in out


def test_close_closes_the_pr_and_its_issue_as_not_planned(fake: FakeGitHub) -> None:
    fake.routes[r"issues/5575$"] = {"number": 5575, "title": "SQL budgets", "labels": []}
    fake.routes[r"pulls/5575$"] = {
        "title": "Rust API: SQL budgets",
        "body": "Closes #4737.",
        "head": {"ref": "factory/work-checkpoints/issue-4737/beam-free/1"},
        "state": "open",
        "labels": [],
    }
    fake.routes[r"issues/4737$"] = {"number": 4737, "state": "open"}
    code, out = invoke(["close", "pr:5575", "--reason", "Retired in #5239."])
    assert code == 3 and "Nothing was closed. Add --yes" in out
    assert not any(call[:2] == ["pr", "close"] for call in fake.calls)
    code, out = invoke(["close", "pr:5575", "--reason", "Retired in #5239.", "--yes"])
    assert code == 0
    assert "Closed PR #5575 and issue #4737" in out
    pr_close = next(call for call in fake.calls if call[:2] == ["pr", "close"])
    assert pr_close[pr_close.index("--comment") + 1] == "Retired in #5239."
    issue_close = next(call for call in fake.calls if call[:2] == ["issue", "close"])
    assert issue_close[2] == "4737"
    assert issue_close[issue_close.index("--reason") + 1] == "not planned"


def test_close_refuses_issues_and_an_empty_reason(fake: FakeGitHub) -> None:
    code, out = invoke(["close", "issue:5", "--reason", "x", "--yes"])
    assert code == 2 and "close takes pull requests" in out


def test_unstick_starts_no_more_issues_than_the_limits_leave_room_for(fake: FakeGitHub) -> None:
    issues = [open_issue(n, "factory:unowned") for n in range(41, 51)]
    # 4 PRs in the backlog (limit 6): room for 2 more.
    unstick_routes(fake, FULL_BACKLOG[:4], issues=issues)
    code, out = invoke(["--json", "unstick", "--dry-run"])
    plan = json.loads(out)["plan"]
    issue_tasks = [
        item["task"]["target"]
        for item in plan["assignments"]
        if item["task"]["target"].startswith("issue:")
    ]
    assert issue_tasks == ["issue:41", "issue:42"]
    assert "adds at most 2" in plan["diagnosis"][0]["text"]


def test_report_and_status_count_a_worker_on_an_issue_and_its_pr_once(fake: FakeGitHub) -> None:
    fake.routes[r"issues\?state=open&per_page=100&page=1&"] = [
        {"number": 11, "title": "The issue", "labels": [{"name": f"factory:{WORKER}"}]},
        {
            "number": 12,
            "title": "Its pull request",
            "pull_request": {},
            "labels": [{"name": f"factory:{WORKER}"}],
        },
    ]
    code, out = invoke(["--json", "status"])
    assert code == 0
    code, report = invoke(["report", "--since", "1h"])
    assert "1 item(s) being worked on now" in report
    code, out = invoke(["status"])
    assert "Working now (1)" in out


def test_routine_scheduler_passes_list_every_worker_they_started() -> None:
    from rotisserie.factory_console.service.outcomes import routine_assignments

    log = (
        '2026-11-22T04:09:01Z Factory 1236998703300 assignment: {"kind": "pr", "number": 5662, '
        '"worker": "1236998703300", "stage": "factory:review"}\n'
        "2026-11-22T04:09:02Z Starting Factory 1236998703300 (dispatch attempt 1/3)\n"
        '2026-11-22T04:09:03Z Factory 1078053396700 assignment: {"kind": "issue", "number": 3483, '
        '"worker": "1078053396700", "stage": "factory:building"}\n'
        '    echo "Factory ${worker} assignment: ${assignment}"\n'
    )
    found = routine_assignments(log, START)
    assert [(item.kind, item.number, item.worker, item.action) for item in found] == [
        ("pr", 5662, "1236998703300", "review"),
        ("issue", 3483, "1078053396700", "implement"),
    ]


def test_outcomes_counts_a_closed_pr_as_overtaken_not_missed(fake: FakeGitHub) -> None:
    fake.routes["fixed-model-factory-dispatch.yml/runs"] = {
        "workflow_runs": [
            {
                "id": 900,
                "display_title": "Factory dispatch · pr:3953",
                "status": "completed",
                "created_at": "2026-11-21T22:30:00Z",
            }
        ]
    }
    fake.routes["actions/runs/900/jobs"] = {"jobs": list[JSON](DISPATCH_DONE)}
    fake.texts["actions/jobs/11/logs"] = dispatched_log(
        kind="pr", number=3953, action="review", dispatched_at=int(START.timestamp())
    )
    fake.routes["free-model-factory-entry.yml/runs"] = {
        "workflow_runs": [{"id": 950, "display_title": f"Factory {WORKER} · fixed-model entry"}]
    }
    fake.routes["actions/runs/950$"] = {"id": 950, "status": "completed"}
    fake.routes["actions/runs/950/jobs"] = {
        "jobs": [job("Persist trusted worker outcome in Neon", [step("Classify")], job_id=31)]
    }
    fake.texts["actions/jobs/31/logs"] = (
        "[factory-outcome] failed: Assignment target is no longer owned\n"
    )
    fake.routes["pulls/3953$"] = {"state": "closed", "merged": False}
    code, out = invoke(["--json", "outcomes"])
    data = json.loads(out)
    assert data["requests"][0]["goal"] == "closed"
    code, out = invoke(["outcomes"])
    assert "Not done" not in out


# checks ---------------------------------------------------------------------------------


def test_check_logs_are_summarized_by_their_first_real_failure() -> None:
    from rotisserie.factory_console.service.checks import summarize_log

    fmt = (
        "2026-11-20T17:20:00Z Diff in /home/runner/work/comic-pile/comic-pile/native/roll-service/src/a.rs:162:\n"
        "2026-11-20T17:20:00Z Diff in /home/runner/work/comic-pile/comic-pile/native/roll-service/src/b.rs:9:\n"
        "##[error]Process completed with exit code 1.\n"
    )
    kind, summary, files = summarize_log(fmt)
    assert kind == "formatting"
    assert summary == "cargo fmt --check wants changes in a.rs, b.rs"
    assert files == ("native/roll-service/src/a.rs", "native/roll-service/src/b.rs")
    tests = (
        "test same_path_openapi::tests::checked_in_contract_is_current ... FAILED\n"
        "error: test failed, to rerun pass `-p comicpile-roll-service --lib`\n"
    )
    kind, summary, _ = summarize_log(tests)
    assert kind == "tests"
    assert "same_path_openapi::tests::checked_in_contract_is_current" in summary
    assert "regenerate it" in summary
    kind, summary, _ = summarize_log("error[E0308]: mismatched types\n")
    assert (kind, summary) == ("compile", "error[E0308]: mismatched types")
    kind, summary, _ = summarize_log("##[error]Process completed with exit code 1.\n")
    assert kind == "other"


def test_checks_command_reports_failing_checks_on_the_head(fake: FakeGitHub) -> None:
    fake.routes[r"pulls/5629$"] = {"head": {"sha": "6dcf97e6" + "0" * 32}}
    fake.routes[r"commits/6dcf97e6\w+/check-runs"] = {
        "check_runs": [
            {"id": 71, "name": "rust-roll", "conclusion": "failure", "html_url": "u1"},
            {"id": 72, "name": "lint", "conclusion": "success", "html_url": "u2"},
        ]
    }
    fake.texts["actions/jobs/71/logs"] = "Diff in /home/runner/work/w/w/src/a.rs:1:\n"
    code, out = invoke(["checks", "pr:5629"])
    assert code == 0
    assert "rust-roll (formatting): cargo fmt --check wants changes in a.rs" in out
    assert "--action ci-repair" in out and "lint" not in out


def test_unstick_sends_ci_repair_not_review_when_checks_are_red(fake: FakeGitHub) -> None:
    unstick_routes(fake, FULL_BACKLOG)
    fake.routes[r"pulls/303$"] = {"head": {"sha": "a" * 40}}
    fake.routes[r"commits/a{40}/check-runs"] = {
        "check_runs": [{"id": 81, "name": "rust-roll", "conclusion": "failure", "html_url": "u"}]
    }
    fake.texts["actions/jobs/81/logs"] = "test x::y ... FAILED\n"
    code, out = invoke(["--json", "unstick", "--dry-run"])
    plan = json.loads(out)["plan"]
    work = {item["task"]["target"]: item["task"] for item in plan["assignments"]}
    assert work["pr:303"]["action"] == "ci-repair"  # was changes-requested
    assert "checks fail: rust-roll: failing tests: x::y" in work["pr:303"]["why"]


def test_by_item_flags_items_going_in_circles() -> None:
    from rotisserie.factory_console.service.outcomes import RequestOutcome, by_item

    def outcome(run: int, target: str, goal: str, result: str = "failed") -> RequestOutcome:
        return RequestOutcome(
            run, START, target, "dispatched", "", "review-findings", "m", "w", None,
            result, "", goal, "",
        )  # fmt: skip

    histories = by_item(
        [
            outcome(5, "pr:1", "pending", "running"),
            outcome(4, "pr:1", "not-met"),
            outcome(3, "pr:1", "not-met"),
            outcome(2, "pr:2", "met", "delivered"),
            outcome(1, "pr:2", "not-met"),
        ]
    )
    assert [(item.target, item.rounds, item.missed, item.churning) for item in histories] == [
        ("pr:1", 3, 2, True),
        ("pr:2", 2, 1, False),
    ]


def test_outcomes_names_the_factory_gate_that_rejected_a_change(fake: FakeGitHub) -> None:
    from rotisserie.factory_console.service.outcomes import validation_failure
    from rotisserie.factory_console.tracker import Tracker

    fake.texts["actions/jobs/41/logs"] = (
        "x factory fast checks failed: native ingress/deployment/write guard tests "
        "(log: /tmp/factory-fast-checks-1.log)\n"
    )
    jobs: list[Obj] = [job("run / factory", [], job_id=41)]
    tracker = Tracker(GitHub(REPO, executable="sh"))
    assert validation_failure(tracker, jobs) == (
        "factory fast checks failed: native ingress/deployment/write guard tests"
    )
    assert validation_failure(tracker, [job("other", [], job_id=42)]) == ""


def test_a_ci_repair_goal_is_green_checks_not_a_stage_label(fake: FakeGitHub) -> None:
    fake.routes["fixed-model-factory-dispatch.yml/runs"] = {
        "workflow_runs": [
            {
                "id": 900,
                "display_title": "Factory dispatch · pr:3953",
                "status": "completed",
                "created_at": "2026-11-21T22:30:00Z",
            }
        ]
    }
    fake.routes["actions/runs/900/jobs"] = {"jobs": list[JSON](DISPATCH_DONE)}
    fake.texts["actions/jobs/11/logs"] = dispatched_log(
        kind="pr", number=3953, action="ci-repair", dispatched_at=int(START.timestamp())
    )
    fake.routes["free-model-factory-entry.yml/runs"] = {
        "workflow_runs": [{"id": 950, "display_title": f"Factory {WORKER} · fixed-model entry"}]
    }
    fake.routes["actions/runs/950$"] = {"id": 950, "status": "completed"}
    fake.routes["actions/runs/950/jobs"] = {
        "jobs": [job("Persist trusted worker outcome in Neon", [step("Classify")], job_id=31)]
    }
    fake.texts["actions/jobs/31/logs"] = "[factory-outcome] work_completed: Repaired\n"
    fake.routes["pulls/3953$"] = {
        "state": "open",
        "labels": [{"name": "factory:review"}],
        "head": {"sha": "b" * 40},
    }
    fake.routes[r"commits/b{40}/check-runs"] = {
        "check_runs": [{"id": 5, "name": "rust-roll", "conclusion": "failure", "html_url": "u"}]
    }
    code, out = invoke(["--json", "outcomes"])
    request = json.loads(out)["requests"][0]
    assert request["goal"] == "not-met"
    assert request["goal_text"] == "its checks still fail: rust-roll"


def test_outcomes_shows_the_first_error_from_the_published_excerpt(fake: FakeGitHub) -> None:
    from rotisserie.factory_console.service.outcomes import validation_failure
    from rotisserie.factory_console.tracker import Tracker

    fake.texts["actions/jobs/41/logs"] = (
        "x factory fast checks failed: frontend typecheck (log: /tmp/f.log)\n"
        "=== factory fast check failure output ===\n"
        "### frontend typecheck\n"
        "src/api.ts(3,1): error TS2322: Type 'string' is not assignable to type 'number'.\n"
        "[exit 2]\n"
        "=== end factory fast check failure output ===\n"
    )
    tracker = Tracker(GitHub(REPO, executable="sh"))
    assert validation_failure(tracker, [job("run / factory", [], job_id=41)]) == (
        "factory fast checks failed: frontend typecheck "
        "(src/api.ts(3,1): error TS2322: Type 'string' is not assignable to type 'number'.)"
    )


def test_unstick_skip_and_only_scope_the_plan(fake: FakeGitHub) -> None:
    unstick_routes(fake, FULL_BACKLOG)
    code, out = invoke(["--json", "unstick", "--dry-run", "--skip", "pr:301", "--skip", "pr:306"])
    targets = [item["task"]["target"] for item in json.loads(out)["plan"]["assignments"]]
    assert "pr:301" not in targets and "pr:306" not in targets and "pr:307" in targets
    code, out = invoke(["--json", "unstick", "--dry-run", "--only", "pr:302"])
    targets = [item["task"]["target"] for item in json.loads(out)["plan"]["assignments"]]
    assert targets == ["pr:302"]
    code, out = invoke(["unstick", "--dry-run", "--skip", "5656"])
    assert code == 2 and "--skip/--only take pr:N or issue:N" in out


# doctor ---------------------------------------------------------------------------------


def _finished(run: int, message: str, result: str = "failed") -> object:
    from rotisserie.factory_console.service.outcomes import RequestOutcome

    return RequestOutcome(
        run, START, f"pr:{run}", "dispatched", "", "ci-repair", "m", "w", run, result,
        message, "not-met", "",
    )  # fmt: skip


def test_doctor_flags_one_gate_rejecting_most_workers() -> None:
    """The fast checks ran tests main had deleted: every Rust worker 'failed'."""
    from rotisserie.factory_console.service.doctor import systemic_rejection

    gate = (
        "Code saved; validation incomplete; factory fast checks failed: "
        "native ingress/deployment/write guard tests"
    )
    found = [_finished(n, gate) for n in range(4)] + [_finished(9, "model gave up")]
    finding = systemic_rejection(found)  # type: ignore[arg-type]
    assert finding.status == "fail"
    assert "4 of 5 recent workers were rejected by the same factory gate" in finding.text
    assert "native ingress/deployment/write guard tests" in finding.text
    healthy = systemic_rejection([_finished(1, "model gave up")])  # type: ignore[list-item]
    assert healthy.status == "ok"


def test_doctor_reports_a_missing_stats_snapshot_and_a_failed_publisher(
    fake: FakeGitHub,
) -> None:
    fake.routes["contents/factory-stats.json"] = lambda: (_ for _ in ()).throw(
        GitHubNotFoundError("missing")
    )
    fake.routes["factory-status-pages.yml/runs"] = {
        "workflow_runs": [{"id": 77, "status": "completed", "conclusion": "failure"}]
    }
    fake.routes[r"actions/runs/77/jobs"] = {
        "jobs": [
            job(
                "publish-live-dashboard",
                [step("Generate factory dashboard", conclusion="failure")],
                conclusion="failure",
                job_id=78,
            )
        ]
    }
    fake.routes[r"check-runs/78/annotations"] = [
        {"annotation_level": "failure", "message": "Process completed with exit code 1."}
    ]
    fake.texts["actions/jobs/78/logs"] = (
        "Traceback (most recent call last):\n"
        "ValueError: factory slot capacity must be a positive integer\n"
    )
    fake.routes["fixed-model-factory-dispatch.yml/runs"] = {
        "workflow_runs": [{"id": 1, "created_at": "2026-11-21T22:59:00Z"}]
    }
    why_routes(fake, [])
    code, out = invoke(["doctor"])
    assert code == 4
    assert "reliability snapshot: factory-stats.json has never been published" in out
    assert "dashboard publisher: last run failure at step 'Generate factory dashboard'" in out
    assert "factory slot capacity must be a positive integer" in out
    assert "✓ scheduler: running" in out


def test_doctor_fix_reruns_the_publisher_for_a_stale_snapshot(fake: FakeGitHub) -> None:
    import base64

    stale = {"generated_at": "2026-11-21T20:00:00Z", "models": [], "providers": []}
    fake.routes["contents/factory-stats.json"] = {
        "content": base64.b64encode(json.dumps(stale).encode()).decode()
    }
    fake.routes["factory-status-pages.yml/runs"] = {
        "workflow_runs": [{"id": 77, "status": "completed", "conclusion": "success"}]
    }
    fake.routes["fixed-model-factory-dispatch.yml/runs"] = {
        "workflow_runs": [{"id": 1, "created_at": "2026-11-21T22:59:00Z"}]
    }
    why_routes(fake, [])
    code, out = invoke(["doctor"])
    assert "factory-stats.json is stale" in out
    assert "Safe repairs are available: rotisserie factory doctor --fix" in out
    assert not any(call[0] == "workflow" for call in fake.calls)
    code, out = invoke(["doctor", "--fix"])
    run = next(call for call in fake.calls if call[0] == "workflow")
    assert run[:3] == ["workflow", "run", "factory-status-pages.yml"]
    assert "started factory-status-pages.yml" in out


def test_take_over_retries_when_a_routine_pass_reclaims_the_item(fake: FakeGitHub) -> None:
    """Stopping the holder frees the item; a routine pass can grab it first."""
    slots = runtime.module("factory_worker_slots")
    first, second = str(slots.worker_id("z-ai", 0)), str(slots.worker_id("kilo-auto", 0))
    phase = {"n": 0}  # 0: first holds it, 1: second re-claimed it, 2: free

    def issue() -> JSON:
        owner = {0: first, 1: second}.get(phase["n"])
        labels: list[JSON] = [{"name": f"factory:{owner}"}] if owner else []
        return {"number": 3953, "title": "t", "labels": labels, "state": "open"}

    def running() -> JSON:
        owner = {0: first, 1: second}.get(phase["n"])
        rows: list[JSON] = []
        if owner:
            rows.append(
                {
                    "id": 777 + phase["n"],
                    "display_title": f"Factory {owner} · fixed-model entry",
                    "status": "in_progress",
                    "run_started_at": "2026-11-21T22:00:00Z",
                }
            )
        return {"workflow_runs": rows}

    def decision() -> str:
        sent = sum(1 for call in fake.calls if call[0] == "workflow")
        if sent == 1:
            return (
                '{"status":"busy","number":3953,"owner":"factory:'
                + second
                + '","owner_worker":"'
                + second
                + '"}\n'
            )
        return dispatched_log()

    scheduler_routes(fake, log="")
    fake.texts["actions/jobs/11/logs"] = decision
    fake.routes["issues/3953$"] = issue
    fake.routes = {"status=in_progress": running, **fake.routes}
    clock = FakeClock()

    def release(count: int) -> None:
        cancels = sum(1 for call in fake.calls if call[:2] == ["run", "cancel"])
        phase["n"] = 1 if cancels == 1 else 2

    clock.on_sleep = release
    code, out = invoke(
        ["--json", "run", "opencode-go", "auto", "--target", "issue:3953", "--take-over",
         "--no-wait"],
        clock=clock,
    )  # fmt: skip
    data = json.loads(out)
    assert code == 0, out
    assert data["take_over_attempts"] == 2
    assert data["stopped_workers"] == [first, second]
    cancels = [call[2] for call in fake.calls if call[:2] == ["run", "cancel"]]
    assert cancels == ["777", "778"]
    assert sum(1 for call in fake.calls if call[0] == "workflow") == 2


# conflicts ------------------------------------------------------------------------------


def conflict_routes(fake: FakeGitHub) -> None:
    fake.routes[r"pulls/5660$"] = {"head": {"ref": "feature"}, "base": {"ref": "main"}}
    fake.routes[r"compare/main\.\.\.feature"] = {
        "merge_base_commit": {"sha": "base1"},
        "behind_by": 14,
    }
    fake.routes[r"pulls/5660/files"] = [
        {"filename": "src/shared.rs", "status": "modified"},
        {"filename": "src/untouched.rs", "status": "modified"},
        {"filename": "src/new.rs", "status": "added"},
    ]
    fake.routes[r"contents/src/shared\.rs\?ref=base1"] = {"sha": "old"}
    fake.routes[r"contents/src/shared\.rs\?ref=main"] = {"sha": "new"}
    fake.routes[r"contents/src/untouched\.rs\?ref="] = {"sha": "same"}
    fake.routes[r"commits\?path=src/shared\.rs"] = [
        {"sha": "6dcf97e6aa", "commit": {"message": "checkpoint(factory): issue #5624"}}
    ]
    fake.routes[r"commits/6dcf97e6aa/pulls"] = [{"number": 5629, "merged_at": "2026-11-22"}]


def test_conflicts_names_files_main_also_changed_and_the_merged_pr(fake: FakeGitHub) -> None:
    conflict_routes(fake)
    code, out = invoke(["--json", "conflicts", "pr:5660"])
    data = json.loads(out)
    assert code == 0
    assert data["checked"] == 2  # added files cannot conflict
    assert [item["path"] for item in data["overlaps"]] == ["src/shared.rs"]
    assert data["prs"] == [5629]
    assert data["text"] == "PR #5660: main also changed shared.rs (in #5629)"


def test_unstick_hands_repeated_conflict_failures_to_a_person(fake: FakeGitHub) -> None:
    from rotisserie.factory_console.service import outcomes

    unstick_routes(fake, FULL_BACKLOG)

    def missed(_github: GitHub, *, since: object, **_: object) -> list[object]:
        return [
            outcomes.RequestOutcome(
                n,
                START,
                "pr:304",
                "dispatched",
                "",
                "merge-conflict",
                model,
                "w",
                n,
                "failed",
                "",
                "not-met",
                "",
            )  # fmt: skip
            for n, model in enumerate(["a/x", "b/y", "opencode-go/hy4-flash-free"])
        ]

    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as patch:
        patch.setattr(outcomes, "request_outcomes", missed)
        code, out = invoke(["--json", "unstick", "--dry-run"])
    plan = json.loads(out)["plan"]
    assert "pr:304" not in [item["task"]["target"] for item in plan["assignments"]]
    assert plan["needs_person"][0]["prs"] == [304]
    assert "3 models failed to resolve its conflicts" in plan["needs_person"][0]["text"]


# shared failures (baseline) -----------------------------------------------------------

MAIN_FILE = "native/svc/src/shared.rs"
FMT_LOG = (
    f"Diff in /home/runner/work/w/w/{MAIN_FILE}:12:\n##[error]Process completed with exit code 1.\n"
)


def failing_check(
    name: str = "rust-roll",
    *,
    files: tuple[str, ...] = (MAIN_FILE,),
    summary: str = "cargo fmt --check wants changes in shared.rs",
    completed: str = "2026-11-22T12:00:00Z",
    run: int = 7000,
) -> Any:
    from rotisserie.factory_console.service.checks import FailingCheck

    return FailingCheck(
        name, f"https://github.com/{REPO}/actions/runs/{run}/job/{run + 1}", "formatting",
        summary, files, completed,
    )  # fmt: skip


def main_check(conclusion: str, ran_at: str, files: tuple[str, ...] = (MAIN_FILE,)) -> Any:
    from rotisserie.factory_console.service.baseline import MainCheck

    return MainCheck("rust-roll", conclusion, "e" * 40, "u", ran_at, "s", files)


def change(when: str, pr: int | None = 5629) -> Any:
    from rotisserie.factory_console.service.baseline import Change

    return Change(MAIN_FILE, "f" * 40, when, "add shared code", pr)


def test_a_failure_in_files_the_pr_does_not_change_is_inherited() -> None:
    from rotisserie.factory_console.service import baseline

    verdict = baseline.judge(
        1,
        failing_check(),
        frozenset({"src/mine.rs"}),
        None,
        lambda _p: change("2026-11-22T05:00:00Z"),
    )
    assert verdict.origin == baseline.INHERITED
    assert "shared.rs, which the PR does not change" in verdict.reason
    assert verdict.not_the_prs and not verdict.main_fixed


def test_a_failure_in_the_prs_own_files_is_its_own() -> None:
    from rotisserie.factory_console.service import baseline

    verdict = baseline.judge(1, failing_check(), frozenset({MAIN_FILE}), None, lambda _p: None)
    assert verdict.origin == baseline.OWN


def test_a_failure_without_files_is_inherited_only_when_main_fails_the_same_way() -> None:
    from rotisserie.factory_console.service import baseline

    bare = failing_check(files=(), summary="error[E0308]: mismatched types")
    assert baseline.judge(1, bare, frozenset(), None, lambda _p: None).origin == baseline.OWN
    red_main = baseline.MainCheck(
        "rust-roll", "failure", "e" * 40, "u", "2026-11-22T11:00:00Z",
        "error[E0308]: mismatched types",
    )  # fmt: skip
    verdict = baseline.judge(1, bare, frozenset(), red_main, lambda _p: None)
    assert (verdict.origin, verdict.reason) == (baseline.INHERITED, "main fails the same way")


def test_mixed_failures_wait_on_main_until_main_is_fixed() -> None:
    from rotisserie.factory_console.service import baseline

    both = failing_check(files=(MAIN_FILE, "src/mine.rs"))
    files = frozenset({"src/mine.rs"})
    broken = baseline.judge(1, both, files, None, lambda _p: change("2026-11-22T05:00:00Z"))
    assert broken.origin == baseline.MIXED and not broken.main_fixed
    report = baseline.Report(verdicts=(broken,))
    assert report.waits_on_main(1) and not report.needs_sync(1)
    fixed = baseline.judge(1, both, files, None, lambda _p: change("2026-11-22T13:00:00Z"))
    assert fixed.main_fixed and "already fixed" in fixed.reason
    report = baseline.Report(verdicts=(fixed,))
    assert report.needs_sync(1) and not report.waits_on_main(1)
    # Own failures beside a fixed inherited one: sync first, repair after.
    own = baseline.judge(
        1, failing_check("lint", files=("src/mine.rs",)), files, None, lambda _p: None
    )
    report = baseline.Report(verdicts=(fixed, own))
    assert report.needs_sync(1) and report.own_failures(1, [both]) == [both]


def test_main_moving_past_an_inherited_failure_means_sync_not_repair() -> None:
    from rotisserie.factory_console.service import baseline

    touched = baseline.judge(
        1, failing_check(), frozenset(), None, lambda _p: change("2026-11-22T13:00:00Z")
    )
    assert touched.origin == baseline.FIXED_ON_MAIN
    assert "main changed shared.rs after this run" in touched.reason
    passed = baseline.judge(
        1, failing_check(), frozenset(), main_check("success", "2026-11-22T13:00:00Z"),
        lambda _p: None,
    )  # fmt: skip
    assert passed.origin == baseline.FIXED_ON_MAIN and "passed on main since" in passed.reason
    # A newer main run that still fails outweighs a newer change to the file.
    still = baseline.judge(
        1, failing_check(), frozenset(), main_check("failure", "2026-11-22T14:00:00Z"),
        lambda _p: change("2026-11-22T13:00:00Z"),
    )  # fmt: skip
    assert still.origin == baseline.INHERITED


def test_shared_failures_group_by_root_cause_across_checks_with_evidence() -> None:
    from rotisserie.factory_console.service import baseline

    stale_main = main_check("success", "2026-11-21T03:00:00Z")
    verdicts = [
        baseline.judge(pr, failing_check(name), frozenset(), None, lambda _p: None)
        for pr in (11, 12)
        for name in ("rust-roll", "Rust build")
    ]
    verdicts.append(
        baseline.judge(13, failing_check(), frozenset({MAIN_FILE}), None, lambda _p: None)
    )
    incidents = baseline.group(
        verdicts, {"rust-roll": stale_main}, lambda _p: change("2026-11-22T05:39:14Z")
    )
    assert len(incidents) == 1
    incident = incidents[0]
    assert incident.checks == ("rust-roll", "Rust build")
    assert incident.prs == (11, 12)  # #13 changes the file itself
    assert incident.state == "broken" and not incident.confirmed
    assert "(PR #5629) last changed shared.rs on main" in incident.cause
    assert any("none of them change shared.rs" in line for line in incident.evidence)
    assert any("green result is out of date" in line for line in incident.evidence)
    assert "Fix it once on main" in incident.text


def baseline_routes(fake: FakeGitHub, prs: dict[int, str], *, changed_at: str) -> None:
    """PRs whose rust-roll fails in MAIN_FILE; prs maps PR number to its own changed file."""
    fake.routes[f"repos/{REPO}$"] = {"default_branch": "main"}
    for number, own in prs.items():
        head = f"{number:040d}"
        fake.routes[f"pulls/{number}$"] = {"head": {"sha": head}}
        fake.routes[f"pulls/{number}/files"] = [{"filename": own, "status": "modified"}]
        fake.routes[f"commits/{head}/check-runs"] = {
            "check_runs": [
                {
                    "id": 10 + number, "name": "rust-roll", "conclusion": "failure",
                    "html_url": f"https://github.com/{REPO}/actions/runs/70{number}/job/{10 + number}",
                    "completed_at": "2026-11-22T12:00:00Z",
                }
            ]
        }  # fmt: skip
        fake.texts[f"actions/jobs/{10 + number}/logs"] = FMT_LOG
        fake.routes[f"actions/runs/70{number}$"] = {"workflow_id": 55}
    fake.routes[r"actions/workflows/55/runs"] = {"workflow_runs": []}
    fake.routes[r"commits\?sha=main&path="] = [
        {
            "sha": "f" * 40,
            "commit": {"message": "add shared code", "committer": {"date": changed_at}},
        }
    ]
    fake.routes[r"commits/f{40}/pulls"] = [{"number": 5629, "merged_at": changed_at}]


def test_unstick_sends_no_worker_to_prs_red_only_because_of_main(fake: FakeGitHub) -> None:
    unstick_routes(fake, FULL_BACKLOG)
    baseline_routes(fake, {301: "src/a.rs", 302: "src/b.rs"}, changed_at="2026-11-22T05:39:14Z")
    code, out = invoke(["--json", "unstick", "--dry-run"])
    plan = json.loads(out)["plan"]
    targets = {item["task"]["target"] for item in plan["assignments"]}
    assert "pr:301" not in targets and "pr:302" not in targets
    assert "pr:306" in targets  # unaffected PRs are still planned
    assert [item["prs"] for item in plan["waiting_on_main"]] == [[301], [302]]
    [incident] = plan["shared_failures"]
    assert incident["prs"] == [301, 302] and incident["state"] == "broken"
    assert "PR #5629" in incident["cause"]
    code, out = invoke(["unstick", "--dry-run"])
    assert "Failing because of main (1)" in out
    assert "No worker until main is fixed" in out


def test_unstick_syncs_free_prs_once_main_moved_past_the_failure(fake: FakeGitHub) -> None:
    unstick_routes(fake, FULL_BACKLOG)
    baseline_routes(fake, {301: "src/a.rs", 305: "src/e.rs"}, changed_at="2026-11-22T13:00:00Z")
    fake.routes[r"issues/301$"] = {"labels": [{"name": "factory:unowned"}]}
    code, out = invoke(["--json", "unstick", "--dry-run"])
    plan = json.loads(out)["plan"]
    assert [(item["pr"], item["head"]) for item in plan["syncs"]] == [(301, f"{301:040d}")]
    assert "pr:301" not in {item["task"]["target"] for item in plan["assignments"]}
    # #305 has a worker: it is left alone, with a note to sync later.
    assert any("sync it with main once its worker finishes" in r["text"] for r in plan["held"])
    assert not any("update-branch" in " ".join(call) for call in fake.calls)
    code, out = invoke(["unstick", "--yes", "--only", "pr:301"])
    puts = [call for call in fake.calls if any("update-branch" in part for part in call)]
    assert len(puts) == 1 and "PUT" in puts[0]
    assert f"expected_head_sha={301:040d}" in puts[0]
    assert "PR #301: merged main into it" in out


def test_sync_leaves_a_pr_a_worker_took_since_the_plan(fake: FakeGitHub) -> None:
    from rotisserie.factory_console.github import GitHub
    from rotisserie.factory_console.service import unstick

    fake.routes[r"pulls/301$"] = {
        "state": "open",
        "head": {"sha": "a" * 40},
        "base": {"ref": "main"},
        "labels": [{"name": f"factory:{WORKER}"}],
    }
    result = unstick.sync(GitHub(REPO), unstick.Sync(301, "a" * 40, "t", "w"))
    assert not result.ok and f"factory:{WORKER} holds it" in result.text
    assert "Neon leases were not consulted" in result.text
    assert not any("update-branch" in " ".join(call) for call in fake.calls)


def test_unknown_pr_files_never_make_a_failure_inherited(fake: FakeGitHub) -> None:
    from rotisserie.factory_console.github import GitHub
    from rotisserie.factory_console.service import baseline

    fake.routes[f"repos/{REPO}$"] = {"default_branch": "main"}
    fake.routes[r"pulls/9/files"] = []
    report = baseline.analyze(GitHub(REPO), {9: [failing_check()]})
    assert [item.origin for item in report.verdicts] == [baseline.OWN]
    assert not report.incidents and "unknown" in report.problems[0]


def test_checks_command_says_which_failures_main_caused(fake: FakeGitHub) -> None:
    baseline_routes(fake, {5670: "src/a.rs"}, changed_at="2026-11-22T05:39:14Z")
    code, out = invoke(["checks", "pr:5670"])
    assert code == 0
    assert "From main (inherited): fails in shared.rs, which the PR does not change" in out
    assert "Likely cause on main: ffffffff add shared code (PR #5629)" in out
    assert "Nothing here is the PR's to repair" in out and "--action ci-repair" not in out


def test_doctor_fails_when_main_breaks_backlog_prs(fake: FakeGitHub) -> None:
    from rotisserie.factory_console.github import GitHub
    from rotisserie.factory_console.service import doctor

    unstick_routes(fake, FULL_BACKLOG)
    baseline_routes(fake, {301: "src/a.rs", 302: "src/b.rs"}, changed_at="2026-11-22T05:39:14Z")
    finding = doctor.shared_failures(GitHub(REPO), START)
    assert finding.status == "fail"
    assert "#301, #302" in finding.text and "Likely cause" in finding.hint
    assert not finding.fix


def test_doctor_fix_syncs_free_prs_main_moved_past(fake: FakeGitHub) -> None:
    from rotisserie.factory_console.github import GitHub
    from rotisserie.factory_console.service import doctor

    unstick_routes(fake, FULL_BACKLOG)
    baseline_routes(fake, {301: "src/a.rs", 305: "src/e.rs"}, changed_at="2026-11-22T13:00:00Z")
    fake.routes[r"issues/301$"] = {"labels": [{"name": "factory:unowned"}]}
    finding = doctor.shared_failures(GitHub(REPO), START)
    assert (finding.status, finding.fix) == ("warn", "sync-main")
    assert finding.targets == (f"pr:301@{301:040d}",)  # #305 has a worker
    assert "#305 also need it once their workers finish" in finding.text
    applied = doctor.apply_fixes(GitHub(REPO), [finding])
    assert [item.fix for item in applied] == ["sync-main"]
    puts = [call for call in fake.calls if any("update-branch" in part for part in call)]
    assert len(puts) == 1 and f"expected_head_sha={301:040d}" in puts[0]


def test_a_conflicting_pr_gets_merge_conflict_work_not_a_sync(fake: FakeGitHub) -> None:
    unstick_routes(fake, FULL_BACKLOG)
    baseline_routes(fake, {304: "src/d.rs"}, changed_at="2026-11-22T13:00:00Z")
    code, out = invoke(["--json", "unstick", "--dry-run"])
    plan = json.loads(out)["plan"]
    assert not plan["syncs"]
    work = {item["task"]["target"]: item["task"]["action"] for item in plan["assignments"]}
    assert work["pr:304"] == "merge-conflict"


def test_sync_reports_a_conflict_as_merge_conflict_work(fake: FakeGitHub) -> None:
    from rotisserie.factory_console.github import GitHub, GitHubError
    from rotisserie.factory_console.service import unstick

    fake.routes[r"pulls/301$"] = {
        "state": "open",
        "head": {"sha": "a" * 40},
        "base": {"ref": "main"},
        "labels": [{"name": "factory:unowned"}],
    }

    def conflict() -> JSON:
        raise GitHubError("GitHub request failed: gh: merge conflict between base and head")

    fake.routes[r"pulls/301/update-branch"] = conflict
    result = unstick.sync(GitHub(REPO), unstick.Sync(301, "a" * 40, "t", "w"))
    assert not result.ok and "needs merge-conflict work" in result.text


# sync / leases -------------------------------------------------------------------------


def sync_rows(*specs: tuple[int, str], extra: str = "factory") -> list[JSON]:
    rows: list[JSON] = []
    for number, owner in specs:
        row = factory_pr(number, 900 + number, "factory:review", owner, extra)
        row["baseRefName"] = "main"
        rows.append(row)
    return rows


def label_posts(fake: FakeGitHub) -> list[list[str]]:
    return [call for call in fake.calls if any(part.endswith("/labels") for part in call)]


def test_sync_without_neon_queues_for_the_leased_scheduler_pass(fake: FakeGitHub) -> None:
    fake.routes["pr-list"] = sync_rows((301, "factory:unowned"))
    code, out = invoke(["--json", "sync", "pr:301", "--yes"])
    data = json.loads(out)
    assert code == 0 and data["neon"] is False
    assert [entry["pr"] for entry in data["queued"]] == [301]
    [post] = label_posts(fake)
    assert "POST" in post and "labels[]=factory-sync-main" in post
    assert not any("update-branch" in " ".join(call) for call in fake.calls)


def test_sync_does_not_requeue_an_already_queued_pr(fake: FakeGitHub) -> None:
    fake.routes["pr-list"] = sync_rows((301, "factory:unowned"), extra="factory-sync-main")
    code, out = invoke(["sync", "pr:301", "--yes"])
    assert "already queued" in out and not label_posts(fake)


def test_sync_now_uses_one_snapshot_and_compare_and_swap(fake: FakeGitHub) -> None:
    fake.routes["pr-list"] = sync_rows((301, "factory:unowned"))
    fake.routes[r"compare/main\.\.\."] = {"status": "behind"}
    code, out = invoke(["--json", "sync", "pr:301", "--now", "--yes"])
    data = json.loads(out)
    assert code == 0 and data["synced"][0]["status"] == "synced"
    assert data["synced"][0]["neon"] is False
    api_calls = [call for call in fake.calls if call[0] == "api"]
    # One list (gh pr list) + one compare + one update-branch; no per-PR re-reads.
    assert len(api_calls) == 2
    assert f"expected_head_sha={'d' * 40}" in api_calls[1]
    assert sum(1 for call in fake.calls if call[:2] == ["pr", "list"]) == 1


def test_sync_now_never_touches_a_pr_a_worker_holds(fake: FakeGitHub) -> None:
    fake.routes["pr-list"] = sync_rows((305, f"factory:{WORKER}"))
    code, out = invoke(["sync", "pr:305", "--now", "--yes"])
    assert f"owner label factory:{WORKER} holds it" in out
    assert "Neon was not consulted" in out
    assert not any("update-branch" in " ".join(call) for call in fake.calls)


def test_sync_refuses_without_targets_or_with_both(fake: FakeGitHub) -> None:
    assert invoke(["sync"])[0] == 2
    assert invoke(["sync", "pr:1", "--all-stale"])[0] == 2
    assert invoke(["sync", "issue:1"])[0] == 2


def test_sync_all_stale_plans_from_the_shared_evidence(fake: FakeGitHub) -> None:
    unstick_routes(fake, FULL_BACKLOG)
    baseline_routes(fake, {301: "src/a.rs", 305: "src/e.rs"}, changed_at="2026-11-22T13:00:00Z")
    code, out = invoke(["--json", "sync", "--all-stale", "--dry-run"])
    data = json.loads(out)
    assert [(item["pr"], item["head"]) for item in data["candidates"]] == [(301, "d" * 40)]
    assert [item["pr"] for item in data["held"]] == [305]
    assert not label_posts(fake)
    assert not any("update-branch" in " ".join(call) for call in fake.calls)


def test_leases_without_neon_explains_and_shows_owner_labels(fake: FakeGitHub) -> None:
    fake.routes["pr-list"] = sync_rows((301, "factory:unowned"), (305, f"factory:{WORKER}"))
    code, out = invoke(["--json", "leases"])
    data = json.loads(out)
    assert code == 0 and data["source"] == "labels"
    assert "visible only to the trusted factory runtime" in data["note"]
    assert [row["target"] for row in data["rows"]] == ["#305"]
    code, out = invoke(["--json", "leases", "pr:301"])
    assert json.loads(out)["rows"] == [
        {"target": "#301", "holder": "free", "active": False, "expires_at": ""}
    ]


def test_leases_release_expired_needs_neon(fake: FakeGitHub) -> None:
    code, out = invoke(["leases", "--release-expired"])
    assert code == 3 and "Cannot release leases" in out


def test_leases_read_neon_when_configured(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    import types

    from rotisserie.factory_console.service import sync as branch_sync

    def list_leases(repo: str) -> list[dict[str, object]]:
        return [
            {"key": f"factory-lease-v1:{repo}:worker:43", "worker": "43", "claim_id": "abc",
             "active": True, "expires_at": "2026-11-22T13:00:00+00:00"},
            {"key": f"factory-lease-v1:{repo}:target:301", "operation": "scheduler-9",
             "claim_id": "def", "active": False, "expires_at": "2026-11-22T11:00:00+00:00"},
        ]  # fmt: skip

    ledger = types.SimpleNamespace(list_leases=list_leases)
    monkeypatch.setattr(branch_sync, "load_ledger", lambda env=None: (ledger, ""))
    code, out = invoke(["--json", "leases"])
    data = json.loads(out)
    assert data["source"] == "neon"
    assert [(r["target"], r["active"]) for r in data["rows"]] == [
        ("worker:43", True),
        ("#301", False),
    ]
    assert data["rows"][1]["holder"].startswith("operation scheduler-9")


def test_revoke_command_removes_an_external_claim(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rotisserie.factory_console.service import external

    seen: list[dict[str, object]] = []

    def revoke(github: object, **kwargs: object) -> dict[str, object]:
        seen.append(kwargs)
        return {"status": "revoked", "claim_id": kwargs["claim_id"]}

    monkeypatch.setattr(external, "revoke", revoke)
    claim = "11111111-1111-4111-8111-111111111111"
    code, out = invoke(["--json", "revoke", claim, "--reason", "phone went offline"])
    assert code == 0, out
    assert json.loads(out)["status"] == "revoked"
    assert seen[0]["claim_id"] == claim and seen[0]["reason"] == "phone went offline"


def test_revoke_command_reports_a_refusal(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rotisserie.factory_console.service import external

    def revoke(github: object, **kwargs: object) -> dict[str, object]:
        raise ValueError("GitHub actor lacks verified write access to this repository")

    monkeypatch.setattr(external, "revoke", revoke)
    code, out = invoke(["revoke", "11111111-1111-4111-8111-111111111111", "--reason", "x"])
    assert code == 4
    assert "write access" in out


def test_claim_store_failure_is_a_redacted_json_hold(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rotisserie.factory_console.service import external

    def reserve(github: object, **kwargs: object) -> dict[str, object]:
        raise ConnectionRefusedError("postgresql://role:secret@host/db refused")

    monkeypatch.setattr(external, "reserve_next", reserve)
    code, out = invoke(["--json", "reserve-next", "--client", "chatgpt"])
    assert code == 4
    data = json.loads(out)
    assert data["status"] == "held"
    assert data["retry"] == "check_lease_before_retry"
    assert "ConnectionRefusedError" in data["reason"]
    assert "secret" not in out

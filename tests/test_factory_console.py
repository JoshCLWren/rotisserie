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
    texts: dict[str, str] = field(default_factory=dict)
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
                for item in argv[1:]
                if not item.startswith("-") and item not in ("GET", "POST")
            )
            if "--allow-escape-sequences" in argv:
                for prefix, text in self.texts.items():
                    if path.endswith(prefix):
                        return text
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

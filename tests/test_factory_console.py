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
    for name in runtime.RUNTIME_MODULES:
        source = f"factory/.github/scripts/{name}.py"
        assert included[source] == f"rotisserie/factory_console/factory_runtime/{name}.py"
        assert (root / source).is_file()

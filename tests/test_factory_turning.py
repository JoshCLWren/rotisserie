"""``baste turning``: each verdict and reason, ``--follow`` exits, doctor and recap lines."""

from __future__ import annotations

import base64
import json
from datetime import timedelta
from typing import Any

import pytest
from test_factory_console import (  # type: ignore[import-not-found]
    START,
    FakeClock,
    FakeGitHub,
    fake,
    invoke,
)

from rotisserie import control_contract
from rotisserie.factory_console.commands import turning as view
from rotisserie.factory_console.github import JSON
from rotisserie.factory_console.service import doctor, recap, turning
from rotisserie.factory_console.service import recap_build as build
from rotisserie.factory_console.service.turning_reads import (
    ControlFailure,
    DispatcherPass,
    WorkerRuns,
    parse_pass,
)

__all__ = ["fake"]

NOW = START
ECHOED = (
    '2026-11-24T21:25:45.2324094Z \x1b[36;1m        echo "Factory ${worker}: no executable '
    'control-plane assignment"\x1b[0m\n'
    '2026-11-24T21:25:45.2362534Z \x1b[36;1m  echo "Source-diverse dispatch: $(jq -r length '
    '<<< "$workers") eligible workers"\x1b[0m\n'
)


def stamp(delta: timedelta) -> str:
    return str((NOW - delta).isoformat()).replace("+00:00", "Z")


def dispatcher_log(eligible: int = 12, reason: str = "") -> str:
    lines = [ECHOED]
    lines.append("2026-11-24T21:25:45.3535157Z Factory 1149154685411: provider orcarouter is "
                 "turned off; no claim\n")  # fmt: skip
    lines.append("2026-11-24T21:25:45.6214676Z Factory 1381631231811 skipped: source "
                 "typhoon-free daily dispatch budget 1/1 reached (UTC day)\n")  # fmt: skip
    lines.append(f"2026-11-24T21:25:46.4328663Z Source-diverse dispatch: {eligible} eligible "
                 "workers\n")  # fmt: skip
    lines.append('2026-11-24T21:25:49.1147265Z OmniRoute free-entry capacity: {"enabled": 0, '
                 '"in_flight": 0, "cap": 12, "remaining": 12}\n')  # fmt: skip
    for index in range(eligible):
        lines.append(f"2026-11-24T21:26:0{index % 10}.9824482Z Factory 13989438916{index:02d}: "
                     f"no executable control-plane assignment{reason}\n")  # fmt: skip
    return "".join(lines)


def test_the_dispatcher_log_parser_ignores_the_echoed_script() -> None:
    found = parse_pass(dispatcher_log())
    assert (found.eligible, found.assigned, found.none) == (12, 0, 12)
    assert found.reasons == {"not logged": 12}
    assert not found.reason_logged
    assert found.capacity_remaining == 12
    assert found.filtered["orcarouter is turned off"] == 1
    assert found.filtered["typhoon-free daily dispatch budget 1/1"] == 1


def test_a_logged_controller_reason_is_read() -> None:
    found = parse_pass(dispatcher_log(2, " (reason=review_backlog_saturated)"))
    assert found.reasons == {"review_backlog_saturated": 2}
    assert found.reason_logged


def evidence(**changes: Any) -> turning.Evidence:
    base: dict[str, Any] = {
        "now": NOW,
        "workers": WorkerRuns(stamp(timedelta(minutes=10)), stamp(timedelta(minutes=5)), 1, []),
        "passes": [DispatcherPass(eligible=4, assigned=1, at=stamp(timedelta(minutes=2)))],
        "newest_pass": stamp(timedelta(minutes=2)),
        "backlog": {"eligible_issues": 3, "open_prs": 2, "limit": "", "limit_text": ""},
        "providers": {"read": True, "sources": 2, "cooling": [], "all_paused": False},
    }
    base.update(changes)
    return turning.Evidence(**base)


def idle(minutes: int = 120) -> WorkerRuns:
    return WorkerRuns(stamp(timedelta(minutes=minutes)), stamp(timedelta(minutes=30)), 0, [])


def none_pass(eligible: int = 12, **fields: Any) -> DispatcherPass:
    found = parse_pass(dispatcher_log(eligible))
    found.at = stamp(timedelta(minutes=3))
    for key, value in fields.items():
        setattr(found, key, value)
    return found


def test_a_recent_worker_start_is_turning() -> None:
    verdict = turning.assess(evidence())
    assert (verdict.verdict, verdict.reason, verdict.next) == ("turning", "moving", "")


@pytest.mark.parametrize(
    ("changes", "reason", "command"),
    [
        ({"passes": [none_pass(store="failure")]}, "store-unreachable", "baste store selftest"),
        ({"passes": [none_pass()]}, "dispatcher-none", "baste unstick --dry-run"),
        ({"passes": [none_pass(eligible=0)]}, "no-capacity", "baste backoffs --via github"),
        (
            {
                "passes": [none_pass(eligible=0)],
                "backlog": {
                    "eligible_issues": 0,
                    "open_prs": 6,
                    "limit": "review-limit",
                    "limit_text": "The review backlog is at its limit (6 of 6).",
                },
            },
            "review-limit",
            "baste unstick --dry-run",
        ),
        (
            {"passes": [DispatcherPass(eligible=3, assigned=2, broken=1)]},
            "dispatch-broken",
            "baste doctor",
        ),
        (
            {
                "passes": [DispatcherPass(eligible=3, assigned=1)],
                "newest_pass": stamp(timedelta(hours=1)),
            },
            "scheduler-stopped",
            "baste doctor",
        ),
        ({"passes": [DispatcherPass(eligible=3, assigned=1)]}, "unknown", "baste why"),
    ],
)
def test_each_burning_reason(changes: dict[str, Any], reason: str, command: str) -> None:
    verdict = turning.assess(evidence(workers=idle(), **changes))
    assert (verdict.verdict, verdict.reason, verdict.next) == ("burning", reason, command)
    assert verdict.text.startswith("No worker has started for 2h 00m.")


def test_todays_stall_is_burning_with_the_dispatcher_reason() -> None:
    passes = [none_pass() for _ in range(3)]
    backlog = {
        "eligible_issues": 0,
        "open_prs": 6,
        "limit": "review-limit",
        "limit_text": "The review backlog is at its limit (6 of 6), so new issue work waits.",
    }
    verdict = turning.assess(evidence(workers=idle(125), passes=passes, backlog=backlog))
    assert verdict.verdict == "burning"
    assert verdict.reason == "dispatcher-none"
    assert "3 dispatcher passes found 12 eligible workers and assigned none" in verdict.text
    assert "does not log the controller's reason" in verdict.text
    assert "review backlog is at its limit" in verdict.text


def test_no_work_and_no_workers_is_an_idle_turn() -> None:
    found = evidence(
        workers=idle(),
        passes=[DispatcherPass(eligible=0)],
        backlog={"eligible_issues": 0, "open_prs": 0, "limit": "", "limit_text": ""},
    )
    assert turning.assess(found).reason == "nothing-to-do"
    assert turning.assess(found).verdict == "turning"


def failure(workflow: str) -> ControlFailure:
    return ControlFailure(workflow, "https://github.com/acme/widgets/actions/runs/7", stamp(
        timedelta(minutes=5)))  # fmt: skip


def test_control_failures_over_the_threshold_are_slowing() -> None:
    found = evidence(
        failures=[failure("dispatcher"), failure("label claim"), failure("dispatcher")]
    )
    verdict = turning.assess(found)
    assert (verdict.verdict, verdict.reason, verdict.next) == (
        "slowing",
        "control-failures",
        "baste doctor",
    )
    assert "dispatcher 2, label claim 1" in verdict.text
    persist = evidence(failures=[failure("Entry persist")] * 3)
    assert turning.assess(persist).next == "baste failures"
    assert turning.assess(evidence(failures=[failure("dispatcher")] * 2)).verdict == "turning"


def test_an_idle_factory_between_watchdog_ticks_is_not_slowing() -> None:
    found = evidence(
        workers=idle(),
        passes=[DispatcherPass(eligible=0)],
        newest_pass=stamp(timedelta(minutes=40)),
        backlog={"eligible_issues": 0, "open_prs": 0, "limit": "", "limit_text": ""},
    )
    assert (turning.assess(found).verdict, turning.assess(found).reason) == (
        "turning",
        "nothing-to-do",
    )
    running = evidence(newest_pass=stamp(timedelta(minutes=40)))
    assert turning.assess(running).verdict == "turning"


def test_unread_worker_runs_give_no_verdict_and_no_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    found = evidence(workers=None, passes=[none_pass()], unread=["worker runs"])
    verdict = turning.assess(found)
    assert (verdict.verdict, verdict.reason) == ("unknown", "unread")
    seen = scripted(monkeypatch, [found, found, evidence(workers=idle(), passes=[none_pass()])])
    code, out = invoke(["turning", "--follow", "--every", "1m"])
    assert code == view.EXIT_BURNING and len(seen) == 3
    assert "❔ Unknown" in out


def entry_run(run_id: int) -> tuple[dict[str, JSON], dict[str, JSON]]:
    run: dict[str, JSON] = {
        "id": run_id,
        "status": "completed",
        "conclusion": "failure",
        "created_at": stamp(timedelta(minutes=50)),
        "updated_at": stamp(timedelta(minutes=5)),
        "html_url": f"https://github.com/acme/widgets/actions/runs/{run_id}",
    }
    jobs: dict[str, JSON] = {
        "jobs": [
            {"id": 1, "name": "run / factory", "conclusion": "success", "steps": []},
            {"id": run_id * 10, "name": "Persist trusted worker outcome in Neon",
             "conclusion": "failure", "steps": [
                 {"name": "Classify and persist this completed worker", "conclusion": "failure"}
             ]},
        ]
    }  # fmt: skip
    return run, jobs


def test_a_recorded_worker_outcome_is_not_a_control_failure(fake: FakeGitHub) -> None:
    from rotisserie.factory_console.github import GitHub
    from rotisserie.factory_console.service.turning_reads import control_failures
    from rotisserie.factory_console.tracker import Tracker

    recorded = '[factory-outcome-class] {"class":"work_failure","source":"nvidia"}\n'
    worked, worked_jobs = entry_run(41)
    lost, lost_jobs = entry_run(42)
    fake.routes[r"actions/runs/41/jobs"] = worked_jobs
    fake.routes[r"actions/runs/42/jobs"] = lost_jobs
    fake.texts["actions/jobs/410/logs"] = recorded
    fake.texts["actions/jobs/420/logs"] = "##[error]store unreachable\n"
    fake.routes[r"actions/runs\?status=failure"] = {"workflow_runs": []}
    github = GitHub("acme/widgets", executable="sh")
    found = control_failures(github, Tracker(github), NOW - timedelta(hours=1), [worked, lost])
    assert [row.url.rsplit("/", 1)[-1] for row in found] == ["42"]
    assert found[0].workflow == "Entry persist"


def stats_content() -> JSON:
    stats = {
        "models": [{"source": "nvidia", "model": "m", "health": {"status": "healthy"}}],
        "db_usage": {"status": "ok", "checked_at": stamp(timedelta(minutes=10))},
    }
    return {"content": base64.b64encode(json.dumps(stats).encode()).decode()}


def stalled_routes(fake: FakeGitHub) -> None:
    fake.routes[r"free-model-factory-entry\.yml/runs"] = {
        "workflow_runs": [
            {"id": 5, "status": "completed", "conclusion": "failure",
             "created_at": stamp(timedelta(hours=2)), "updated_at": stamp(timedelta(minutes=40)),
             "html_url": "https://github.com/acme/widgets/actions/runs/5"},
        ]
    }  # fmt: skip
    fake.routes[r"fixed-model-factory-dispatch\.yml/runs"] = {
        "workflow_runs": [
            {"id": 900 + index, "status": "completed", "conclusion": "success",
             "created_at": stamp(timedelta(minutes=3 + index)),
             "html_url": f"https://github.com/acme/widgets/actions/runs/{900 + index}"}
            for index in range(3)
        ]
    }  # fmt: skip
    for index in range(3):
        fake.routes[rf"actions/runs/{900 + index}/jobs"] = {
            "jobs": [
                {"id": 70 + index, "name": "rotisserie / dispatch", "steps": [
                    {"name": "Initialize the factory store", "conclusion": "success"},
                    {"name": "Resolve and dispatch fixed workers", "conclusion": "success"},
                ]}
            ]
        }  # fmt: skip
        fake.texts[f"actions/jobs/{70 + index}/logs"] = dispatcher_log()
    fake.routes[r"actions/runs/5/jobs"] = {
        "jobs": [{"id": 1, "name": "run / factory", "conclusion": "success", "steps": []}]
    }
    fake.routes[r"actions/runs\?status=failure"] = {"workflow_runs": []}
    fake.routes[r"contents/factory-stats\.json"] = stats_content()


def test_the_cli_reports_todays_stall(fake: FakeGitHub) -> None:
    stalled_routes(fake)
    code, out = invoke(["turning", "--json"])
    data = json.loads(out)
    assert code == 0
    assert (data["verdict"], data["reason"]) == ("burning", "dispatcher-none")
    passes = data["evidence"]["dispatcher_passes"]
    assert [(p["eligible"], p["assigned"], p["none"]) for p in passes] == [(12, 0, 12)] * 3
    assert data["evidence"]["starts_source"] == "github runs"

    def strings(value: object) -> list[str]:
        if isinstance(value, dict):
            return [s for key, item in value.items() for s in [str(key), *strings(item)]]
        if isinstance(value, list):
            return [s for item in value for s in strings(item)]
        return [value] if isinstance(value, str) else []

    assert all(text.isascii() for text in strings(data)), "JSON values stay free of emoji"

    code, out = invoke(["turning"])
    assert "🔥🔥 Burning" in out
    assert "👉 Next: baste unstick --dry-run" in out
    assert "🧑‍🍳 Last worker started" in out
    assert "12 eligible, 0 assigned, 12 none (reason not logged 12)" in out


def scripted(monkeypatch: pytest.MonkeyPatch, states: list[turning.Evidence]) -> list[int]:
    seen: list[int] = []

    def gather(*_: object, **__: object) -> turning.Evidence:
        seen.append(1)
        return states[min(len(seen), len(states)) - 1]

    monkeypatch.setattr(turning, "gather", gather)
    return seen


@pytest.mark.parametrize(
    ("bad", "code", "face"),
    [
        (evidence(workers=idle(), passes=[none_pass()]), view.EXIT_BURNING, "🔥🔥 Burning"),
        (evidence(failures=[failure("dispatcher")] * 3), view.EXIT_SLOWING, "🐢 Slowing"),
    ],
)
def test_follow_exits_with_a_distinct_code_on_a_transition(
    monkeypatch: pytest.MonkeyPatch, bad: turning.Evidence, code: int, face: str
) -> None:
    seen = scripted(monkeypatch, [evidence(), evidence(), bad])
    notes: list[list[str]] = []
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/osascript")
    monkeypatch.setattr("subprocess.run", lambda args, **_: notes.append(args))
    clock = FakeClock()
    exit_code, out = invoke(["turning", "--follow", "--every", "1m", "--notify"], clock=clock)
    assert exit_code == code
    assert len(seen) == 3 and clock.sleeps == 2
    assert out.count("🍗🔄 Turning") == 1, "an unchanged verdict is not printed again"
    assert "🚨🔥 ALERT" in out and face in out
    assert notes and notes[0][0] == "osascript"


def test_follow_alerts_at_once_on_a_burning_start_and_skips_notify_without_osascript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripted(monkeypatch, [evidence(workers=idle(), passes=[none_pass()])])
    monkeypatch.setattr("shutil.which", lambda _: None)
    code, out = invoke(["turning", "--follow", "--notify", "--json"])
    data = json.loads(out)
    assert code == view.EXIT_BURNING
    assert data["verdict"] == "burning" and data["alert"] is True


def test_doctor_runs_the_same_check(monkeypatch: pytest.MonkeyPatch) -> None:
    scripted(monkeypatch, [evidence(workers=idle(), passes=[none_pass()])])
    status, text, hint = turning.health(None, NOW)  # type: ignore[arg-type]
    assert (status, hint) == ("fail", "baste unstick --dry-run")
    assert text.startswith("Burning: No worker has started")
    assert view.decorate(text).startswith("🔥🔥 Burning: No worker")
    scripted(monkeypatch, [evidence(failures=[failure("dispatcher")] * 3)])
    assert turning.health(None, NOW)[0] == "warn"  # type: ignore[arg-type]
    scripted(monkeypatch, [evidence()])
    assert turning.health(None, NOW)[0] == "ok"  # type: ignore[arg-type]


def test_doctor_lists_the_turning_check(fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    scripted(monkeypatch, [evidence(workers=idle(), passes=[none_pass()])])
    names = {item.check: item for item in doctor.run_checks(fake_github(), now=NOW)}
    assert names[turning.CHECK].status == "fail"
    code, out = invoke(["doctor"])
    assert "work turning: 🔥🔥 Burning: No worker has started" in out
    assert "👉 baste unstick --dry-run" in out


def fake_github() -> Any:
    from rotisserie.factory_console.github import GitHub

    return GitHub("acme/widgets", executable="sh")


def test_recap_names_a_stretch_and_says_burning_only_while_doctor_fails() -> None:
    since = NOW - timedelta(hours=12)
    runs: list[dict[str, JSON]] = [{"created_at": stamp(timedelta(hours=h))} for h in (11, 10, 2)]
    sources = build.Sources(since=since, now=NOW, runs=runs)
    gap = recap.burning_stretch(sources)
    assert gap is not None
    assert gap["minutes"] == 480 and gap["ongoing"] is False and gap["burning"] is False
    assert gap["text"] == "No worker started for 8h 00m, then workers started again."
    sources.runs = [{"created_at": stamp(timedelta(hours=11))}]
    quiet = recap.burning_stretch(sources)
    assert quiet is not None and str(quiet["text"]).startswith("No worker started for 11h")
    sources.findings = [doctor.Finding(turning.CHECK, "fail", "Burning: ...")]
    stretch = recap.burning_stretch(sources)
    assert stretch is not None and stretch["burning"] is True
    assert stretch["text"] == "Burning: no worker started for 11h 00m, until now."
    sources.runs = [{"created_at": stamp(timedelta(minutes=m))} for m in range(0, 720, 30)]
    assert recap.burning_stretch(sources) is None
    sources.unread.append("worker runs")
    assert recap.burning_stretch(sources) is None


def test_the_contract_and_both_control_rooms_offer_turning() -> None:
    assert "turning" in control_contract.READ_ONLY
    assert "turning" in control_contract.LIVE_READS
    assert control_contract.read_command("turning", {}) == ["turning"]
    with pytest.raises(control_contract.InvalidRequest):
        control_contract.read_command("turning", {"idle": "1m"})
    from test_control_parity import UI, hosted_tables  # type: ignore[import-not-found]

    assert 'get("turning")' in UI and 'id="turning-refresh"' in UI
    assert "🔥🔥 Burning" in UI and "👉" in UI
    assert "turning" in hosted_tables()["live_reads"]

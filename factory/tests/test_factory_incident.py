"""Incident mode diverts free workers to one issue until it is fixed (#239)."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
sys.path.insert(0, str(SCRIPTS))

policy = importlib.import_module("factory_work_policy")
incident = importlib.import_module("factory_incident")
settings = importlib.import_module("factory_settings")
jev = importlib.import_module("factory_jev_shadow")

HEAD = "a" * 40
INCIDENT = "factory:incident"


def issue(number: int, *labels: str, state: str = "OPEN") -> dict[str, Any]:
    return {
        "number": number,
        "state": state,
        "title": f"Issue {number}",
        "labels": [{"name": name} for name in ("factory:unowned", *labels)],
        "createdAt": f"2026-08-{number % 28 + 1:02d}T00:00:00Z",
    }


def pr(number: int, linked: int, stage: str = "factory:review", **extra: Any) -> dict[str, Any]:
    return {
        "number": number,
        "state": "OPEN",
        "isDraft": False,
        "labels": [{"name": name} for name in ("factory:unowned", stage)],
        "headRefName": f"factory/18-{linked}-nvidia",
        "headRefOid": HEAD,
        "body": "Worker: opencode-free-model-factory-18",
        "createdAt": "2026-08-20T00:00:00Z",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        **extra,
    }


def numbers(candidates: list[Any]) -> list[tuple[str, int]]:
    return [(c.kind, c.number) for c in candidates]


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("", {}),
        ("239", {239: 3}),
        ("239:2, 240:6", {239: 2, 240: 6}),
        ('239:0,240:7,abc,-1,241:x,"},242:1', {242: 1}),
    ],
)
def test_only_well_formed_entries_count(raw: str, expected: dict[int, int]) -> None:
    assert incident.parse(raw) == expected
    assert incident.parse(incident.format_incidents(expected)) == expected


def test_the_variable_arrives_through_the_workflow_settings_snapshot() -> None:
    assert '"FACTORY_INCIDENTS": "${{ vars.FACTORY_INCIDENTS }}"' in settings.workflow_env_value()
    assert incident.NOTES_VAR not in settings.workflow_env_value()
    env = {settings.REPO_VARS_ENV: json.dumps({incident.INCIDENTS_VAR: "239:2"})}
    assert incident.declared(env) == {239: 2}


def test_an_incident_is_active_only_while_its_issue_is_open_and_labeled() -> None:
    issues = [issue(239, INCIDENT), issue(240, state="CLOSED")]
    assert incident.active(issues, {239: 3, 240: 3, 241: 3, 242: 3}) == {239: 3}


def test_incident_issue_ranks_first_and_other_new_intake_pauses() -> None:
    issues = [issue(1, "main-breakage"), issue(2, "factory:override"), issue(239, INCIDENT)]
    prs = [pr(50, 7)]
    candidates = policy.build_candidates(issues, prs, incidents={239: 3})
    assert numbers(candidates) == [("issue", 239), ("pr", 50)]
    assert candidates[0].incident is True
    assert candidates[1].incident is False


def test_without_an_incident_intake_is_unchanged() -> None:
    issues = [issue(1, "main-breakage"), issue(239, INCIDENT)]
    assert numbers(policy.build_candidates(issues, [], incidents={})) == [
        ("issue", 1),
        ("issue", 239),
    ]


def test_a_closed_incident_issue_resumes_normal_intake() -> None:
    issues = [issue(1), issue(2)]
    candidates = policy.build_candidates(issues, [], incidents={239: 3})
    assert numbers(candidates) == [("issue", 1), ("issue", 2)]


def test_the_incident_issue_bypasses_wip_and_review_backlog_limits() -> None:
    busy = [pr(100 + n, 300 + n) for n in range(3)]
    for row in busy:
        row["labels"] = [{"name": "factory:7"}, {"name": "factory:review"}]
    issues = [issue(5), issue(239, INCIDENT)]
    blocked = policy.build_candidates(issues, busy, pr_wip_limit=1, incidents={})
    assert [c.number for c in blocked if c.kind == "issue"] == []
    allowed = policy.build_candidates(issues, busy, pr_wip_limit=1, incidents={239: 3})
    assert [c.number for c in allowed if c.kind == "issue"] == [239]
    review_full = [pr(400 + n, 500 + n) for n in range(3)]
    saturated = policy.build_candidates(
        issues, review_full, review_backlog_limit=1, incidents={239: 3}
    )
    assert [c.number for c in saturated if c.kind == "issue"] == [239]


def test_operator_force_still_sees_paused_issues() -> None:
    issues = [issue(5), issue(239, INCIDENT)]
    forced = policy.build_candidates(
        issues, [], include_backpressured_issues=True, incidents={239: 3}
    )
    assert numbers(forced) == [("issue", 239), ("issue", 5)]


def test_incident_pr_work_jumps_owner_picks_and_main_breakage_for_every_worker() -> None:
    issues = [issue(1, "main-breakage"), issue(2, "factory:override"), issue(239, INCIDENT)]
    prs = [
        pr(60, 1),
        pr(61, 2, "factory:ci"),
        pr(62, 239, "factory:changes-requested"),
    ]
    candidates = policy.build_candidates(issues, prs, incidents={239: 3})
    assert candidates[0].number == 62
    for worker in [str(n) for n in range(1, 40)]:
        ordered = policy.order_candidates_for_worker(candidates, worker)
        assert ordered[0].number == 62, worker


def test_an_attempt_author_still_never_reviews_its_incident_pr() -> None:
    candidates = policy.build_candidates([issue(239, INCIDENT)], [pr(62, 239)], incidents={239: 3})
    ordered = policy.order_candidates_for_worker(candidates, "18")
    assert 62 not in [c.number for c in ordered]


def test_owner_pick_ranks_below_incident_but_above_lanes() -> None:
    incident_item = policy.Candidate("pr", 3, 5, 0, "", incident=True)
    pick = policy.Candidate("pr", 2, 5, 0, "", override=True)
    urgent = policy.Candidate("pr", 1, 0, 4, "")
    ordered = sorted([urgent, pick, incident_item], key=policy.Candidate.sort_key)
    assert [c.number for c in ordered] == [3, 2, 1]


def test_jev_completion_ranking_cannot_promote_past_incident_work() -> None:
    ordered = [
        policy.Candidate("pr", 62, 3, 0, "", stage="factory:review", incident=True),
        policy.Candidate("pr", 70, 3, 0, "", stage="factory:ci"),
    ]
    report = {
        "surface": "pr-completion",
        "status": "chosen",
        "chosen": "pr-70",
        "action_map": {"pr-70": {"pr": 70, "action": "ci-repair", "head": HEAD}},
    }
    result, changed, _reason = jev.apply_completion_ranking(ordered, report, {70: HEAD})
    assert changed is False
    assert [c.number for c in result] == [62, 70]


def test_explain_reports_the_incident_intake_pause() -> None:
    issues = [issue(5), issue(239, INCIDENT)]
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(incident.INCIDENTS_VAR, "239:3")
        report = policy.explain_issue_report(
            5, issues[0], issues=issues, prs=[], no_diff_attempts={},
            intake=None, open_blocker=None,
        )  # fmt: skip
    assert report["dynamic_gates"]["incident_intake_pause"] is True
    assert report["dynamic_gates"]["active_incidents"] == [239]


def test_superseded_prs_are_only_free_attempt_branches_for_that_issue() -> None:
    claimed = pr(67, 239)
    claimed["labels"] = [{"name": "factory:claimed"}]
    running = pr(68, 239)
    running["labels"] = [{"name": "factory:18"}, {"name": "factory:review"}]
    prs = [
        pr(62, 239),
        pr(63, 239, state="CLOSED"),
        pr(64, 240),
        {"number": 65, "state": "OPEN", "headRefName": "human/fix", "body": "Fixes #239",
         "labels": []},
        {"number": 66, "state": "OPEN", "headRefName": "local/fix", "body": "Fixes #239",
         "labels": [{"name": "factory"}]},
        claimed,
        running,
    ]  # fmt: skip
    assert incident.superseded_prs(prs, 239) == [62]
    assert incident.held_prs(prs, 239) == [67, 68]


def test_a_held_attempt_keeps_the_label_until_a_later_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    held = pr(68, 239)
    held["labels"] = [{"name": "factory:override"}]

    def fake_gh(*args: str) -> str:
        calls.append(args)
        if args[0] == "api" and len(args) == 2:
            return json.dumps({"state": "closed", "labels": [{"name": INCIDENT}]})
        return json.dumps([pr(62, 239), held])

    monkeypatch.setattr(incident, "_gh", fake_gh)
    ended = incident.reconcile("acme/widgets", {239: 3})
    assert ended == [{"issue": 239, "closed_prs": [62], "held_prs": [68]}]
    assert not any(call[:3] == ("api", "--method", "DELETE") for call in calls)


def test_an_ended_incident_costs_one_read_per_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_gh(*args: str) -> str:
        calls.append(args)
        return json.dumps({"state": "closed", "labels": []})

    monkeypatch.setattr(incident, "_gh", fake_gh)
    assert incident.reconcile("acme/widgets", {239: 3}) == []
    assert calls == [("api", "repos/acme/widgets/issues/239")]


def test_a_reopened_issue_without_the_label_does_not_restart_the_incident() -> None:
    issues = [issue(239), issue(5)]
    assert numbers(policy.build_candidates(issues, [], incidents={239: 3})) == [
        ("issue", 5),
        ("issue", 239),
    ]


def test_reconcile_ends_only_closed_incidents(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []
    states = {239: "closed", 240: "open"}

    def fake_gh(*args: str) -> str:
        calls.append(args)
        if args[0] == "api" and len(args) == 2:
            state = states[int(args[1].rsplit("/", 1)[1])]
            return json.dumps({"state": state, "labels": [{"name": INCIDENT}]})
        if args[:2] == ("pr", "list"):
            return json.dumps([pr(62, 239), pr(70, 240)])
        return ""

    monkeypatch.setattr(incident, "_gh", fake_gh)
    ended = incident.reconcile("acme/widgets", {239: 3, 240: 3})
    assert ended == [{"issue": 239, "closed_prs": [62], "held_prs": []}]
    assert ("pr", "close", "62", "--repo", "acme/widgets", "--comment",
            incident.superseded_comment(239)) in calls  # fmt: skip
    assert any(
        call[:3] == ("api", "--method", "DELETE") and call[3].endswith("factory%3Aincident")
        for call in calls
    )
    assert not any(call[:3] == ("pr", "close", "70") for call in calls)


def test_reconcile_dry_run_mutates_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_gh(*args: str) -> str:
        calls.append(args)
        if args[0] == "api":
            return json.dumps({"state": "closed", "labels": [{"name": INCIDENT}]})
        return json.dumps([pr(62, 239)])

    monkeypatch.setattr(incident, "_gh", fake_gh)
    assert incident.reconcile("acme/widgets", {239: 3}, dry_run=True)[0]["closed_prs"] == [62]
    assert all(
        call[:2] in (("api", "repos/acme/widgets/issues/239"), ("pr", "list")) for call in calls
    )


def test_dispatcher_runs_the_reconcile_on_scheduled_ticks() -> None:
    text = (SCRIPTS.parents[2] / ".github/workflows/fixed-model-factory-dispatch.yml").read_text()
    assert "factory_incident.py reconcile" in text


def test_cli_list_prints_declared_incidents() -> None:
    env = {"FACTORY_INCIDENTS": "239:2", "GITHUB_REPOSITORY": "acme/widgets", "PATH": "/usr/bin:/bin"}
    out = subprocess.run(
        [sys.executable, str(SCRIPTS / "factory_incident.py"), "list"],
        env=env, capture_output=True, text=True, check=True,
    )  # fmt: skip
    assert json.loads(out.stdout) == {"incidents": [{"issue": 239, "attempts": 2}]}

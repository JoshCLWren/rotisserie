"""A split plan: the model's record, validated, composed into child bodies, persisted once."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import factory_model_track_record as track
import factory_review_policy as policy
import factory_split_plan as plans
import pytest
from factory_adopter import MARKER_PREFIX
from factory_issue_intake_guard import split_reason
from factory_work_policy_base import eligibility

from tests import turso_support as support

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "factory" / ".github" / "scripts"
PARENT = 4350


def child(title: str, *blocked: int) -> dict[str, Any]:
    return {
        "title": title,
        "body": f"Change {title.lower()} so it does one thing well.",
        "acceptance": [f"{title} has a focused test", f"{title} passes the existing suite"],
        "files": [f"app/{title.lower().replace(' ', '_')}.py"],
        "blocked_by": list(blocked),
    }


def raw() -> dict[str, Any]:
    return {"children": [child("Schema", 2), child("Reader"), child("Writer", 1, 2)]}


def test_a_valid_plan_is_normalized_and_ordered_blockers_first() -> None:
    plan = plans.validate(raw(), PARENT)
    assert [c["title"] for c in plan["children"]] == ["Schema", "Reader", "Writer"]
    assert plan["order"] == [2, 1, 3]


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda r: r.update(children=r["children"][:1]), "2 to 8"),
        (lambda r: r.update(children=r["children"] * 3), "2 to 8"),
        (lambda r: r["children"][1].update(blocked_by=[1]), "cycle"),
        (lambda r: r["children"][0].update(blocked_by=[1]), "not a sibling"),
        (lambda r: r["children"][0].update(acceptance=["a", "b", "c", "d"]), "1 to 3"),
        (lambda r: r["children"][0].update(acceptance=[]), "1 to 3"),
        (lambda r: r["children"][0].update(files=[]), "files"),
        (lambda r: r["children"][0].update(files=["/etc/passwd"]), "file path"),
        (lambda r: r["children"][0].update(body="x <!-- factory --> y"), "HTML comment"),
        (lambda r: r["children"][0].update(body="Depends on #77"), "other than its siblings"),
        (lambda r: r["children"][0].update(body="x" * 15_990), "split"),
        (lambda r: r["children"][0].update(title=""), "title"),
    ],
)
def test_an_invalid_plan_is_rejected(change: Any, reason: str) -> None:
    record = copy.deepcopy(raw())
    change(record)
    with pytest.raises(plans.PlanError, match=reason):
        plans.validate(record, PARENT)


def test_a_composed_child_reads_as_one_plain_task_with_its_sibling_blockers() -> None:
    plan = plans.validate(raw(), PARENT)
    body = plans.compose(PARENT, 3, plan["children"][2], {1: 5001, 2: 5002})
    assert body.startswith(plans.child_marker(PARENT, 3)) and plans.is_child(body)
    assert f"Part of #{PARENT}." in body
    assert eligibility.parse_declared_dependencies(body) == {5001, 5002}
    issue = {"body": body, "labels": [{"name": "factory"}]}
    assert split_reason(issue) is None
    assert not eligibility.is_acceptance_parent(body)


def test_the_agent_log_plan_is_the_json_after_the_last_sentinel() -> None:
    log = (
        f'prompt mentions {plans.SENTINEL} and {{"children": []}}\n'
        f"\x1b[0mthinking...\n{plans.SENTINEL}\n```json\n{json.dumps(raw())}\n```\n"
    )
    assert plans.from_agent_log(log) == raw()
    with pytest.raises(plans.PlanError):
        plans.from_agent_log("no plan here")


def test_the_job_log_plan_is_the_wrappers_newest_logged_line() -> None:
    plan = plans.validate(raw(), PARENT)
    lines = [
        f'2026-11-25T18:00:00Z {plans.SENTINEL}: {{"children": []}}',
        f"2026-11-25T18:01:00Z [factory:7][source:s][model:m] {plans.SENTINEL}: "
        + json.dumps(plan),
    ]
    assert plans.from_job_log("\n".join(lines)) == plan


def test_a_split_assignment_is_its_own_track_record_kind() -> None:
    assert track.action_kind({"kind": "issue", "action": "split"}) == "split"
    assert track.action_kind({"kind": "issue"}) == "implement"


def claim(worker: str, action: str) -> dict[str, Any]:
    body = (
        f"<!-- {MARKER_PREFIX}-factory-controller-claim-v1:issue-{PARENT}:{worker}:1764100000"
        f":source=operator:action={action} -->"
    )
    return {"body": body, "user": {"login": policy.TRUSTED_COMMENT_LOGIN}}


@pytest.fixture(params=support.kinds())
def env(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with support.bound(request.param, tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        yield db, repo


def test_the_persister_records_one_plan_per_parent_only_for_a_split_lease(env) -> None:
    import factory_state_ledger as ledger

    _, repo = env
    plan = plans.validate(raw(), PARENT)
    log = f"x\n[factory:7] {plans.SENTINEL}: {json.dumps(plan)}\n"
    assignment = {"kind": "issue", "number": PARENT, "worker": "7", "action": "split"}

    def record(entries: list[dict[str, Any]]) -> int:
        return ledger.record_events(repo, entries)

    success = {"outcome_class": "success"}
    keep = [claim("7", "split")]
    assert (
        plans.persist({**assignment, "action": "implement"}, "7", success, log, keep, record, "1")
        == "not a split run"
    )
    assert plans.persist(
        assignment, "7", {"outcome_class": "work_failure"}, log, keep, record, "1"
    ).startswith("split run ended")
    assert (
        plans.persist(assignment, "7", success, log, [claim("7", "implement")], record, "1")
        == "no trusted split lease for this worker"
    )
    assert plans.persist(assignment, "7", success, log, keep, record, "1") == (
        "recorded a 3-child plan"
    )
    other = copy.deepcopy(plan)
    other["children"][0]["title"] = "Different"
    log2 = f"[factory:7] {plans.SENTINEL}: {json.dumps(other)}\n"
    assert plans.persist(assignment, "7", success, log2, keep, record, "2") == (
        "plan already recorded"
    )


def test_the_worker_runs_the_split_path_only_for_a_split_assignment() -> None:
    worker = (SCRIPTS / "free-model-factory-worker.sh").read_text()
    primitives = (SCRIPTS / "free-model-factory-worker-primitives.sh").read_text()
    helper = (SCRIPTS / "factory-split-worker.sh").read_text()
    assert 'FACTORY_SPLIT="$(jq -r \'if .action == "split" then 1 else 0 end\'' in worker
    assert re.search(r'start_work_checkpoint\(\) \{\n  \[\[ [^\n]*FACTORY_SPLIT:-0}" != 1', worker)
    assert re.search(r"no_change_failover_eligible\(\) \{\n  \[\[ [^\n]*FACTORY_SPLIT", worker)
    assert "factory_finish_split; exit 0; }" in worker
    assert "factory_split_plan.py mission" in primitives
    assert "release_target \"$NUMBER\" 'factory:building' 'split-plan-handoff'" in helper
    assert not policy_reason_matches("split-plan-handoff")
    assert not policy_reason_matches("split-plan-rejected")


def policy_reason_matches(reason: str) -> bool:
    from factory_work_policy_base import NO_DIFF_ATTEMPT_RE

    marker = (
        f"<!-- {MARKER_PREFIX}-factory-claim-released-v3:issue-{PARENT}:7:1764100000:{reason} -->"
    )
    return NO_DIFF_ATTEMPT_RE.search(marker) is not None


def test_the_dispatcher_turns_a_split_target_into_a_split_action() -> None:
    dispatch = (ROOT / ".github" / "workflows" / "fixed-model-factory-dispatch.yml").read_text()
    assert 'if [[ "$TARGET" =~ ^split:([1-9][0-9]*)$ ]]; then' in dispatch
    assert "ACTION=split" in dispatch
    entry = (ROOT / ".github" / "workflows" / "free-model-factory-entry.yml").read_text()
    assert "factory_split_plan.py persist" in entry
    controller = (SCRIPTS / "factory-work-controller.py").read_text()
    assert "'split' if requested_action == 'split' else 'implement'" in controller
    assert "({'action': 'split'} if force_slot and target_spec and action == 'split'" in controller

"""The owner's allow-self-review label lifts the head-author rule for one head only."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
sys.path.insert(0, str(SCRIPTS))
policy = importlib.import_module("factory_review_policy")
exclusion = importlib.import_module("factory_review_exclusion")
allowance = importlib.import_module("factory_self_review_allowance")
guard = importlib.import_module("factory_review_model_guard")

PR = 7
PARENT, HEAD, OTHER = "2" * 40, "3" * 40, "4" * 40
KIMI = "nvidia/moonshotai/kimi-k3.1-pro"
LABELED_AT = "2026-11-25T15:00:00Z"
EVENTS = f"issues/{PR}/events?per_page=100&page=1"


def load_controller() -> ModuleType:
    path = SCRIPTS / "factory-review-controller.py"
    spec = importlib.util.spec_from_file_location("factory_review_controller_allowance", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def labeled(login: str = "JoshCLWren", kind: str = "User", event_id: int = 900) -> dict:
    return {
        "id": event_id,
        "event": "labeled",
        "label": {"name": allowance.LABEL},
        "actor": {"login": login, "type": kind},
        "created_at": LABELED_AT,
    }


class World:
    def __init__(self, *, label: bool = True, actor: dict | None = None) -> None:
        labels = [{"name": "factory"}] + ([{"name": allowance.LABEL}] if label else [])
        self.reads: list[str] = []
        self.paths: dict[str, object] = {
            f"pulls/{PR}": {
                "base": {"ref": "main"},
                "head": {"sha": HEAD},
                "state": "open",
                "labels": labels,
            },
            f"commits/{HEAD}": {
                "sha": HEAD,
                "parents": [{"sha": PARENT}],
                "commit": {"message": "fix", "committer": {"date": "2026-11-25T14:00:00Z"}},
            },
            EVENTS: [{"id": 1, "event": "labeled", "label": {"name": "factory"}}]
            + ([actor or labeled()] if label else []),
        }

    def fetch(self, path: str) -> object:
        self.reads.append(path)
        return self.paths.get(path)


def comments(*extra: str) -> list[str]:
    author = policy.agent_model_author_marker(pr=PR, head=HEAD, model=KIMI, agent="42")
    review = policy.reviewer_model_marker(
        pr=PR, head=HEAD, reviewer_model=KIMI, reviewer_agent="42"
    )
    return [author, review, *extra]


def bound(head: str, event_id: int = 900) -> str:
    grant = allowance.Allowance(True, allowance.NOTE, "JoshCLWren", event_id)
    return allowance.announcement(PR, head, grant)


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch):
    controller = load_controller()
    worlds: list[World] = []

    def gh_json(args: list[str], **_kw: object) -> object:
        return worlds[-1].fetch(args[1].split("/", 3)[3])

    monkeypatch.setattr(controller, "gh_json", gh_json)

    def verdict(world: World, rows: list[str]) -> object:
        worlds.append(world)
        return controller.review_eligibility(PR, HEAD, approvers={"42"}, model_comments=rows)

    return verdict


def test_a_person_labelled_pr_lets_the_head_author_pass_the_gate(gate) -> None:
    result = gate(World(), comments())
    assert result.eligible
    assert "Self-review allowed by owner (label by JoshCLWren)" in result.reason


def test_a_person_labelled_pr_lets_the_head_author_pass_reviewer_selection() -> None:
    records = [{"user": {"login": "github-actions[bot]"}, "body": body} for body in comments()]
    unlabelled = exclusion.for_pull(World(label=False).fetch, PR, records=records)
    assert guard.self_review(KIMI, list(unlabelled.excluded))
    found = exclusion.for_pull(World().fetch, PR, records=records)
    assert found.excluded == ()
    assert found.allowed_by == "JoshCLWren"
    assert guard.self_review(KIMI, list(found.excluded)) == []
    assert "self-review allowed by owner (label by JoshCLWren)" in found.describe()


@pytest.mark.parametrize(
    "actor",
    [
        labeled("github-actions[bot]", "Bot"),
        labeled("github-actions", "User"),
        labeled("factory-worker[bot]", "Bot"),
        {**labeled(), "performed_via_github_app": {"slug": "some-app"}},
        {**labeled(), "actor": None},
    ],
)
def test_a_label_not_added_by_a_person_changes_nothing(gate, actor: dict) -> None:
    result = gate(World(actor=actor), comments())
    assert result.status == "self-review"


def test_a_force_push_after_the_label_drops_the_exception(gate) -> None:
    world = World()
    events = world.paths[EVENTS]
    assert isinstance(events, list)
    events.append({"id": 950, "event": "head_ref_force_pushed", "actor": {"login": "x"}})
    assert gate(world, comments()).status == "self-review"


def test_a_head_committed_after_the_label_drops_the_exception(gate) -> None:
    world = World()
    head = world.paths[f"commits/{HEAD}"]
    assert isinstance(head, dict)
    head["commit"]["committer"]["date"] = "2026-11-25T16:00:00Z"
    assert gate(world, comments()).status == "self-review"


def test_a_grant_bound_to_an_earlier_head_does_not_cover_a_new_one(gate) -> None:
    assert gate(World(), comments(bound(OTHER))).status == "self-review"
    assert gate(World(), comments(bound(HEAD))).eligible
    assert gate(World(), comments(bound(OTHER, event_id=1))).eligible


def test_unreadable_events_fail_closed(gate) -> None:
    world = World()
    world.paths[EVENTS] = None
    assert gate(world, comments()).status == "self-review"


def test_an_unlabelled_pr_is_unchanged_and_reads_no_events(gate) -> None:
    world = World(label=False)
    result = gate(world, comments())
    assert result.status == "self-review"
    assert not any("events" in path for path in world.reads)


def test_a_closed_pr_is_not_covered() -> None:
    world = World()
    pull = world.paths[f"pulls/{PR}"]
    assert isinstance(pull, dict)
    pull["state"] = "closed"
    grant = allowance.check(world.fetch, PR, pull=pull, head=HEAD, comments=[])
    assert not grant.allowed


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str) -> str:
        self.calls.append(args)
        return ""


def settle(world: World, *, action: str = "", rows: list[str] | None = None) -> Recorder:
    world.paths[f"issues/{PR}/comments?per_page=100&page=1"] = [
        {"user": {"login": "github-actions[bot]"}, "body": body} for body in rows or []
    ]
    gh = Recorder()
    allowance.settle("o/r", PR, fetch=world.fetch, gh=gh, action=action)
    return gh


def test_settle_announces_a_new_grant_once() -> None:
    gh = settle(World())
    assert len(gh.calls) == 1 and gh.calls[0][:2] == ("pr", "comment")
    body = gh.calls[0][-1]
    assert "Owner allowed self-review on this PR (label added by JoshCLWren)" in body
    assert settle(World(), rows=[bound(HEAD)]).calls == []


@pytest.mark.parametrize("change", ["closed", "synchronize", "rebound"])
def test_settle_removes_the_label_when_the_pr_closes_or_the_head_changes(change: str) -> None:
    world = World()
    pull = world.paths[f"pulls/{PR}"]
    assert isinstance(pull, dict)
    if change == "closed":
        pull["state"] = "closed"
    gh = settle(
        world,
        action="synchronize" if change == "synchronize" else "",
        rows=[bound(OTHER)] if change == "rebound" else [],
    )
    assert gh.calls == [
        ("api", "--method", "DELETE", f"repos/o/r/issues/{PR}/labels/{allowance.LABEL}")
    ]

"""Duplicate PRs for one issue resolve themselves: keep one, close the rest with context."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import factory_duplicate_context as duplicate_context
import factory_review_policy as policy
import factory_self_heal as heal
import factory_self_heal_duplicates as dups
import factory_self_heal_rules as rules
import factory_state_labels as state
import pytest

from tests import turso_support as support
from tests.test_factory_item_loop import heal_budget

ISSUE = 5695
OLD, NEW = 5768, 5775
SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"


@pytest.fixture(params=support.kinds())
def env(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with support.bound(request.param, tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        yield db, repo


def pull(number: int, created: str, *labels: str) -> dict[str, Any]:
    names = ["factory", "factory:unowned", "factory:review", *labels]
    return {
        "number": number,
        "title": f"Fix #{ISSUE}",
        "body": f"Closes #{ISSUE}",
        "created_at": created,
        "head": {"sha": str(number % 10) * 40, "ref": f"factory/7-{ISSUE}-try-{number}"},
        "labels": [{"name": name} for name in names],
    }


class Hub:
    def __init__(self, repo: str, *, green: set[int] = frozenset(),
                 threads: dict[int, int] | None = None) -> None:
        self.repo = repo
        self.green = green
        self.threads = threads or {}
        self.writes: list[tuple[str, Any]] = []
        self.files = {OLD: [f"app/f{i}.py" for i in range(17)],
                      NEW: [f"app/f{i}.py" for i in range(10)]}

    def api(self, path: str, *args: str, payload: object | None = None) -> Any:
        if path.startswith("commits/"):
            sha = path.split("/")[1]
            number = next(n for n in (OLD, NEW) if str(n % 10) * 40 == sha)
            conclusion = "success" if number in self.green else "failure"
            return {"check_runs": [{"status": "completed", "conclusion": conclusion}]}
        if path.endswith("/files?per_page=100"):
            return [{"filename": name} for name in self.files[int(path.split("/")[1])]]
        self.writes.append((path, payload))
        return {}

    def comments(self, number: int) -> list[dict[str, Any]]:
        return []

    def graphql(self, query: str, **variables: object) -> dict[str, Any]:
        open_count = self.threads.get(int(variables["pr"]), 0)  # type: ignore[arg-type]
        nodes = [{"isResolved": False, "comments": {"nodes": [{"author": {"login": "r"}}]}}]
        return {"data": {"repository": {"pullRequest": {"reviewThreads": {
            "nodes": nodes * open_count}}}}}


def sweep(repo: str, hub: Hub, *rows: dict[str, Any]) -> list[dict[str, Any]]:
    return dups.resolve_duplicates(hub, heal.LedgerStore(repo), list(rows), heal_budget())


def closed(hub: Hub) -> list[int]:
    return [int(path.split("/")[1]) for path, p in hub.writes
            if path.startswith("pulls/") and p == {"state": "closed"}]


def test_the_groups_name_every_open_pr_per_issue() -> None:
    rows = [{**pull(OLD, "a"), "headRefName": pull(OLD, "a")["head"]["ref"]},
            {**pull(NEW, "b"), "headRefName": pull(NEW, "b")["head"]["ref"]}]
    assert state.duplicate_groups(rows) == {ISSUE: [OLD, NEW]}
    assert state.duplicates(rows) == {OLD: False, NEW: True}


def test_green_ci_wins_over_age(env) -> None:
    _, repo = env
    hub = Hub(repo, green={NEW})
    found = sweep(repo, hub, pull(OLD, "2026-11-20"), pull(NEW, "2026-11-21"))
    assert closed(hub) == [OLD]
    assert found[0]["kept"] == NEW and len(found[0]["files"]) == 7


def test_without_green_ci_fewer_findings_win(env) -> None:
    _, repo = env
    hub = Hub(repo, threads={OLD: 3, NEW: 1})
    sweep(repo, hub, pull(OLD, "2026-11-20"), pull(NEW, "2026-11-21"))
    assert closed(hub) == [OLD]


def test_on_a_tie_the_older_pr_stays(env) -> None:
    _, repo = env
    tie = Hub(repo, threads={OLD: 1, NEW: 1})
    sweep(repo, tie, pull(OLD, "2026-11-20"), pull(NEW, "2026-11-21"))
    assert closed(tie) == [NEW]


def test_the_closing_comment_and_the_kept_pr_name_the_extra_files(env) -> None:
    _, repo = env
    hub = Hub(repo, green={NEW})
    sweep(repo, hub, pull(OLD, "2026-11-20"), pull(NEW, "2026-11-21"))
    bodies = {path: str(p["body"]) for path, p in hub.writes if path.endswith("/comments")}
    closing = bodies[f"issues/{OLD}/comments"]
    assert rules.parse_marker(closing)["kind"] == rules.DUPLICATE_CLOSED  # type: ignore[index]
    assert f"#{NEW} delivers the same issue #{ISSUE}" in closing and "`app/f16.py`" in closing
    assert "`app/f3.py`" not in closing
    kept = bodies[f"issues/{NEW}/comments"]
    line = duplicate_context.context([{"body": kept, "user": {"login": policy.TRUSTED_COMMENT_LOGIN}}], NEW)
    assert line.startswith("Context: #5768 was closed as a duplicate") and "app/f10.py" in line
    assert duplicate_context.context([{"body": kept, "user": {"login": "someone"}}], NEW) == ""
    assert not any("git/refs" in path for path, _ in hub.writes)


def test_a_reopened_duplicate_is_not_closed_again(env) -> None:
    _, repo = env
    sweep(repo, Hub(repo, green={NEW}), pull(OLD, "2026-11-20"), pull(NEW, "2026-11-21"))
    again = Hub(repo, green={NEW})
    found = sweep(repo, again, pull(OLD, "2026-11-20"), pull(NEW, "2026-11-21"))
    assert closed(again) == [] and found == [{"pr": OLD, "status": "already closed once"}]


@pytest.mark.parametrize("label", ["agent:codex", "factory:7", "factory:local"])
def test_a_held_pr_holds_its_whole_group(env, label: str) -> None:
    _, repo = env
    hub = Hub(repo, green={NEW})
    found = sweep(repo, hub, pull(OLD, "2026-11-20", label), pull(NEW, "2026-11-21"))
    assert found[0]["status"] == "waiting for release" and closed(hub) == []


def test_the_repair_mission_reads_the_duplicate_context() -> None:
    primitives = (SCRIPTS / "free-model-factory-worker-primitives.sh").read_text()
    assert 'factory_duplicate_context.py" "$number" --repo' in primitives
    worker = (SCRIPTS / "free-model-factory-worker.sh").read_text()
    assert ".github/scripts/factory_duplicate_context.py \"$trusted_dir/\"" in worker

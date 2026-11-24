"""history, labels --run and use: factory views that replace hand-built gh chains."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from rotisserie import web
from rotisserie.factory_console.commands.history import local_time
from rotisserie.factory_console.context import (
    UsageError,
    default_repository,
    find_repository,
    save_default_repository,
)
from rotisserie.factory_console.github import GitHub
from rotisserie.factory_console.service import bridge, claimops, history

REPO = "acme/widgets"


class Timeline:
    repo = REPO

    def __init__(self, nodes: list[dict[str, Any]], total: int) -> None:
        self.nodes = nodes
        self.total = total
        self.queries: list[tuple[str, ...]] = []

    def gh(self, *args: str, **kwargs: Any) -> str:
        self.queries.append(args)
        item = {"state": "OPEN", "timelineItems": {"totalCount": self.total, "nodes": self.nodes}}
        return json.dumps({"data": {"repository": {"issueOrPullRequest": item}}})

    def repo_api(self, route: str) -> Any:
        return {"workflow_runs": []}


NODES = [
    {
        "__typename": "LabeledEvent",
        "createdAt": "2026-11-24T01:32:19Z",
        "actor": {"login": "alice"},
        "label": {"name": "factory:claimed"},
    },
    {
        "__typename": "IssueComment",
        "createdAt": "2026-11-24T01:32:56Z",
        "author": {"login": "github-actions"},
        "body": "<!-- rotisserie-label-claim-status-v1 -->\n**Rotisserie claim status**",
    },
    {
        "__typename": "ClosedEvent",
        "createdAt": "2026-11-24T01:31:14Z",
        "actor": {"login": "github-actions"},
        "closer": {"__typename": "PullRequest", "number": 5696},
    },
    {"__typename": "ReopenedEvent", "createdAt": "2026-11-24T01:32:07Z", "actor": {"login": "a"}},
    {
        "__typename": "IssueComment",
        "createdAt": "2026-11-24T01:40:00Z",
        "author": {"login": "bob"},
        "body": "Still   broken\nafter the merge",
    },
]


def test_history_names_every_change_and_who_made_it() -> None:
    github = Timeline(NODES, total=12)
    data = history.history(cast(GitHub, github), "issue:5695")
    assert [event["detail"] for event in cast(list[dict[str, Any]], data["events"])] == [
        "+factory:claimed",
        "rotisserie-label-claim-status-v1",
        "closed by PR #5696",
        "reopened",
        "Still broken after the merge",
    ]
    assert data["notes"] == []
    assert "number=5695" in github.queries[0]


def test_history_warns_when_github_stops_recording_labels() -> None:
    data = history.history(cast(GitHub, Timeline([], total=310)), "issue:2407")
    assert "stops recording label changes" in str(data["notes"])


def test_markers_with_fields_keep_them() -> None:
    event = history._event(
        {
            "__typename": "IssueComment",
            "createdAt": "x",
            "author": {"login": "github-actions"},
            "body": "<!-- widgets-factory-merge-closure-v1:pr-5696:issue-5695 --> Closed",
        }
    )
    assert event is not None
    assert event["detail"] == "widgets-factory-merge-closure-v1 pr-5696:issue-5695"


def test_local_time_converts_from_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "America/Chicago")
    import time

    time.tzset()
    assert local_time("2026-11-24T01:32:19Z") == "Nov 23 7:32 PM CST"
    assert local_time("not a time") == "not a time"


def test_a_saved_default_beats_the_git_remote_but_not_flag_env_or_project(
    tmp_path: Path,
) -> None:
    env = {"XDG_CONFIG_HOME": str(tmp_path / "config")}
    assert default_repository(env) == ""
    save_default_repository(env, "acme/factory")
    elsewhere = tmp_path / "work"
    elsewhere.mkdir()
    assert find_repository(None, env, elsewhere) == "acme/factory"
    assert find_repository("x/flag", env, elsewhere) == "x/flag"
    assert find_repository(None, {**env, "FACTORY_REPO": "x/env"}, elsewhere) == "x/env"
    (elsewhere / ".rotisserie").mkdir()
    (elsewhere / ".rotisserie" / "config.toml").write_text(
        '[repository]\nowner = "x"\nname = "project"\n'
    )
    assert find_repository(None, env, elsewhere) == "x/project"
    with pytest.raises(UsageError):
        save_default_repository(env, "not a repo")


def test_labels_run_reads_an_earlier_doctor_report(monkeypatch: pytest.MonkeyPatch) -> None:
    class Runs:
        repo = REPO

        def repo_api(self, route: str) -> Any:
            assert route == "actions/runs/77"
            return {"html_url": "u", "status": "completed", "conclusion": "success"}

    monkeypatch.setattr(bridge, "_artifact", lambda github, run_id, name, url: {"mode": "repair"})
    data = claimops.run_report(cast(GitHub, Runs()), 77)
    assert data["mode"] == "repair"
    assert cast(dict[str, Any], data["bridge"])["run_id"] == 77


def test_the_web_ui_reads_history() -> None:
    assert web.read_command("history", {"target": "issue:5"}) == ["history", "issue:5"]
    with pytest.raises(web.InvalidRequest):
        web.read_command("history", {"target": "issue:5 --limit 9"})

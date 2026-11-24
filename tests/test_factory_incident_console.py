"""``rotisserie factory incident`` declares, lists and ends incidents (#239)."""

from __future__ import annotations

import io
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from rotisserie import cli, web
from rotisserie.factory_console import commands
from rotisserie.factory_console.github import GitHub, GitHubNotFoundError
from rotisserie.factory_console.service import queue
from rotisserie.factory_console.tracker import Clock

REPO = "acme/widgets"
NOW = datetime(2026, 11, 24, 9, 0, tzinfo=UTC)


class Repo(GitHub):
    """Variables, issues and PRs in memory."""

    def __init__(self) -> None:
        super().__init__(REPO)
        self.values: dict[str, str] = {}
        self.issues: dict[int, dict[str, Any]] = {
            239: {"number": 239, "state": "open", "labels": []},
            240: {"number": 240, "state": "closed", "labels": []},
            241: {"number": 241, "state": "open", "labels": [], "pull_request": {}},
        }
        self.prs: list[dict[str, Any]] = []
        self.closed: list[int] = []

    def _run(self, args: Sequence[str], **_: object) -> str:
        argv = list(args)
        if argv[:2] == ["variable", "list"]:
            return json.dumps([{"name": k, "value": v} for k, v in self.values.items()])
        if argv[:2] == ["variable", "set"]:
            self.values[argv[2]] = argv[argv.index("--body") + 1]
            return ""
        if argv[:2] == ["variable", "delete"]:
            self.values.pop(argv[2], None)
            return ""
        if argv[:2] == ["pr", "list"]:
            return json.dumps(self.prs)
        if argv[:2] == ["pr", "close"]:
            self.closed.append(int(argv[2]))
            return ""
        if argv[0] == "api" and "/issues/" in argv[1]:
            path = argv[1]
            number = int(path.split("/issues/")[1].split("/")[0])
            if number not in self.issues:
                raise GitHubNotFoundError("HTTP 404")
            issue = self.issues[number]
            if path.endswith("/labels") and "POST" in argv:
                names = [arg.split("=", 1)[1] for arg in argv if arg.startswith("labels[]=")]
                issue["labels"] += [{"name": name} for name in names]
                return "[]"
            if "/labels/" in path and "DELETE" in argv:
                issue["labels"] = [x for x in issue["labels"] if x["name"] != "factory:incident"]
                return "[]"
            return json.dumps(issue)
        return "[]"


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch) -> Repo:
    found = Repo()
    monkeypatch.setattr(GitHub, "_run", lambda self, args, **kw: Repo._run(found, args))
    monkeypatch.delenv("FACTORY_REPO_VARS", raising=False)
    return found


def run(argv: list[str]) -> dict[str, Any]:
    arguments = cli.parser().parse_args(["factory", "--repo", REPO, "--json", *argv])
    out = io.StringIO()
    code = commands.main(
        arguments,
        env={"TERM": "xterm"},
        stdout=out,
        stdin=io.StringIO(),
        clock=Clock(now=lambda: NOW, sleep=lambda _: None),
        executable="sh",
    )
    data: dict[str, Any] = json.loads(out.getvalue().strip().splitlines()[-1])
    data["exit_code"] = code
    return data


def labels(repo: Repo, number: int) -> list[str]:
    return [label["name"] for label in repo.issues[number]["labels"]]


def test_start_writes_the_delivered_variable_a_private_reason_and_the_label(repo: Repo) -> None:
    data = run(["incident", "start", "issue:239", "--attempts", "2", "--reason", "prod down"])
    assert data["exit_code"] == 0
    assert data["changed"] is True
    assert repo.values["FACTORY_INCIDENTS"] == "239:2"
    notes = json.loads(repo.values["FACTORY_INCIDENT_NOTES"])
    assert notes["239"]["reason"] == "prod down"
    assert labels(repo, 239) == ["factory:incident"]
    listing = run(["incident"])
    assert listing["incidents"] == [
        {
            "issue": 239,
            "attempts": 2,
            "reason": "prod down",
            "since": NOW.isoformat(timespec="seconds"),
            "issue_state": "open",
            "labeled": True,
            "active": True,
        }
    ]


def test_start_defaults_to_three_attempts_and_is_idempotent(repo: Repo) -> None:
    run(["incident", "start", "issue:239"])
    assert repo.values["FACTORY_INCIDENTS"] == "239:3"
    again = run(["incident", "start", "issue:239"])
    assert again["changed"] is False
    assert labels(repo, 239) == ["factory:incident"]


@pytest.mark.parametrize(
    "argv",
    [
        ["incident", "start", "issue:240"],
        ["incident", "start", "issue:999"],
        ["incident", "start", "pr:239"],
        ["incident", "start", "issue:241"],
        ["incident", "start", "issue:239", "--attempts", "7"],
        ["incident", "start", "issue:239", "--attempts", "0"],
        ["incident", "begin", "issue:239"],
        ["incident", "stop", "issue:239", "--attempts", "2"],
    ],
)
def test_bad_requests_change_nothing(repo: Repo, argv: list[str]) -> None:
    assert run(argv)["exit_code"] == 2
    assert repo.values == {}


def test_stop_on_an_open_issue_clears_state_and_label_but_keeps_its_prs(repo: Repo) -> None:
    run(["incident", "start", "issue:239", "--reason", "x"])
    repo.prs = [{"number": 62, "state": "OPEN", "headRefName": "factory/18-239-a", "labels": []}]
    data = run(["incident", "stop", "issue:239"])
    assert data["exit_code"] == 0
    assert data["changed"] is True
    assert "FACTORY_INCIDENTS" not in repo.values
    assert "FACTORY_INCIDENT_NOTES" not in repo.values
    assert labels(repo, 239) == []
    assert repo.closed == []
    assert run(["incident"])["incidents"] == []


def test_stop_after_the_fix_merged_closes_sibling_attempts_as_superseded(repo: Repo) -> None:
    run(["incident", "start", "issue:239"])
    repo.issues[239]["state"] = "closed"
    assert run(["incident"])["incidents"][0]["active"] is False
    repo.prs = [
        {"number": 62, "state": "OPEN", "headRefName": "factory/18-239-a", "labels": []},
        {"number": 63, "state": "OPEN", "headRefName": "factory/19-239-b", "labels": []},
        {"number": 64, "state": "OPEN", "headRefName": "factory/19-300-b", "labels": []},
        {"number": 65, "state": "OPEN", "headRefName": "human/x", "body": "Fixes #239"},
    ]
    data = run(["incident", "stop", "issue:239"])
    assert data["closed_prs"] == [62, 63]
    assert repo.closed == [62, 63]
    assert "FACTORY_INCIDENTS" not in repo.values


def test_two_incidents_are_kept_apart(repo: Repo) -> None:
    repo.issues[300] = {"number": 300, "state": "open", "labels": []}
    run(["incident", "start", "issue:239"])
    run(["incident", "start", "issue:300", "--attempts", "1"])
    assert repo.values["FACTORY_INCIDENTS"] == "239:3,300:1"
    run(["incident", "stop", "issue:239"])
    assert repo.values["FACTORY_INCIDENTS"] == "300:1"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            {"operation": "incident_start", "issue": "issue:239", "reason": "prod down"},
            ["incident", "start", "issue:239", "--reason=prod down"],
        ),
        (
            {"operation": "incident_start", "issue": "issue:239", "attempts": "2"},
            ["incident", "start", "issue:239", "--reason=", "--attempts=2"],
        ),
        (
            {"operation": "incident_stop", "issue": "issue:239"},
            ["incident", "stop", "issue:239"],
        ),
    ],
)
def test_web_incident_writes_map_to_cli_commands(
    body: dict[str, object], expected: list[str]
) -> None:
    args = web.write_command(body)
    assert args == expected
    cli.parser().parse_args(["factory", *args])


@pytest.mark.parametrize(
    "body",
    [
        {"operation": "incident_start", "issue": "pr:239"},
        {"operation": "incident_start", "issue": "--repo=x"},
        {"operation": "incident_start", "issue": "issue:239", "attempts": "9"},
        {"operation": "incident_start", "issue": "issue:239", "reason": "a\nb"},
        {"operation": "incident_stop"},
    ],
)
def test_web_incident_refuses_unsafe_input(body: dict[str, object]) -> None:
    with pytest.raises(web.InvalidRequest):
        web.write_command(body)


def test_web_lists_incidents_read_only() -> None:
    assert web.read_command("incident", {}) == ["incident"]
    with pytest.raises(web.InvalidRequest):
        web.read_command("incident", {"target": "issue:1"})


def test_why_names_the_incident_pause_from_repository_variables() -> None:
    def row(number: int, *labels: str) -> dict[str, Any]:
        names = ("factory:unowned", *labels)
        return {"number": number, "state": "OPEN", "title": "t", "body": "",
                "labels": [{"name": name} for name in names]}  # fmt: skip

    board = queue.Board([row(5), row(239, "factory:incident")], [], 8, 6, intake=None)
    board.incidents = {239: 3}
    other = queue.verdict(queue.explain(board, board.issues[0], blocker=None), board)
    assert other.status == "waiting"
    assert [reason.code for reason in other.reasons] == ["incident"]
    fix = queue.verdict(queue.explain(board, board.issues[1], blocker=None), board)
    assert fix.status == "ready"
    board.incidents = {}
    resumed = queue.verdict(queue.explain(board, board.issues[0], blocker=None), board)
    assert resumed.status == "ready"


def test_the_board_reads_incidents_from_repository_variables(repo: Repo) -> None:
    board = queue.read_board(repo, {"FACTORY_INCIDENTS": "239:2"}, 0)
    assert board.incidents == {239: 2}

"""Label claim operations shared by the CLI and the web UI (issue #191 follow-up)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote

import pytest

from rotisserie import web
from rotisserie.factory_console import github as gh
from rotisserie.factory_console.github import GitHub, GitHubNotFoundError, describe_request
from rotisserie.factory_console.service import bridge, claimops, external
from rotisserie.factory_console.tracker import Clock

REPO = "acme/widgets"
T0 = datetime(2026, 11, 23, 21, 0, tzinfo=UTC)


class Ticking(Clock):
    def __init__(self) -> None:
        self.at = T0
        super().__init__(now=lambda: self.at, sleep=self._sleep)

    def _sleep(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


class Fake:
    repo = REPO

    def __init__(self) -> None:
        self.labels: dict[int, set[str]] = {7: {"factory", "factory:review"}}
        self.runs: list[dict[str, Any]] = []
        self.artifacts: dict[int, dict[str, Any]] = {}
        self.comments: dict[int, str] = {}
        self.on_label: Any = None
        self.items: dict[int, dict[str, Any]] = {}
        self.pulls: dict[int, dict[str, Any]] = {}

    def repo_api(self, route: str) -> Any:
        if route.startswith(f"actions/workflows/{claimops.LABEL_WORKFLOW}/runs"):
            return {"workflow_runs": list(reversed(self.runs))}
        if route.startswith("pulls/"):
            return self.pulls[int(route.split("/")[1])]
        if route.startswith("issues/"):
            return self.items[int(route.split("/")[1])]
        raise AssertionError(route)

    def gh(self, *args: str, **kwargs: Any) -> str:
        if args[:3] == ("api", "--paginate", "--slurp") and "/comments" in args[3]:
            number = int(args[3].split("/")[4])
            body = self.comments.get(number)
            rows = (
                [
                    {
                        "user": {"login": "github-actions[bot]"},
                        "body": claimops.STATUS_MARKER + "\n" + body,
                    }
                ]
                if body
                else []
            )
            return json.dumps([rows])
        route = args[1]
        number = int(route.split("/")[4])
        if "POST" in args:
            self.labels[number].add("factory:claimed")
        else:
            name = unquote(route.rsplit("/", 1)[1])
            if name not in self.labels[number]:
                raise GitHubNotFoundError("GitHub says that does not exist or you cannot see it.")
            self.labels[number].discard(name)
        if self.on_label:
            self.on_label(number)
        return "{}"


def answer(fake: Fake, clock: Ticking, result: dict[str, Any], comment: str) -> None:
    def respond(number: int) -> None:
        run_id = 100 + len(fake.runs)
        fake.runs.append(
            {
                "id": run_id,
                "display_title": f"Label claim · #{number} · issues · alice",
                "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}",
                "event": "issues",
                "actor": {"login": "alice"},
                "status": "completed",
                "conclusion": "success",
                "created_at": (clock.at + timedelta(seconds=2)).isoformat(),
            }
        )
        fake.artifacts[run_id] = result
        fake.comments[number] = comment

    fake.on_label = respond


@pytest.fixture
def artifacts(monkeypatch: pytest.MonkeyPatch) -> dict[int, dict[str, Any]]:
    store: dict[int, dict[str, Any]] = {}
    monkeypatch.setattr(bridge, "_artifact", lambda github, run_id, name, url: store[run_id])
    return store


def test_claim_adds_the_label_and_returns_the_controller_answer(
    artifacts: dict[int, dict[str, Any]],
) -> None:
    fake, clock = Fake(), Ticking()
    answer(
        fake,
        clock,
        {"status": "reserved", "claim_id": "u-1", "expires_at": "soon"},
        "Accepted: @alice holds `review` on this item.",
    )
    fake.artifacts = artifacts
    data = claimops.claim(cast(GitHub, fake), clock, "pr:7")
    assert "factory:claimed" in fake.labels[7]
    assert data["status"] == "reserved" and data["claim_id"] == "u-1"
    assert "Accepted: @alice" in str(data["status_comment"])


def test_claim_ignores_controller_runs_from_before_the_request(
    artifacts: dict[int, dict[str, Any]],
) -> None:
    fake, clock = Fake(), Ticking()
    fake.runs.append(
        {
            "id": 1,
            "display_title": "Label claim · #7 · issues · bob",
            "status": "completed",
            "conclusion": "success",
            "created_at": (T0 - timedelta(minutes=5)).isoformat(),
        }
    )
    artifacts[1] = {"status": "conflict"}
    data = claimops.claim(cast(GitHub, fake), clock, "pr:7", timeout=30)
    assert data["result"] == {"result": "unknown", "reason": "no answer within 30s"}


def test_finish_without_a_claim_label_says_so(artifacts: dict[int, dict[str, Any]]) -> None:
    data = claimops.finish(cast(GitHub, Fake()), Ticking(), "pr:7")
    assert data["status"] == "not-claimed"


def test_finish_returns_the_hand_off(artifacts: dict[int, dict[str, Any]]) -> None:
    fake, clock = Fake(), Ticking()
    fake.labels[7].add("factory:claimed")
    answer(
        fake,
        clock,
        {
            "status": "released",
            "operation": "handoff",
            "evidence": f"https://github.com/{REPO}/pull/7",
        },
        "@alice finished `merge-conflict`.",
    )
    fake.artifacts = artifacts
    data = claimops.finish(cast(GitHub, fake), clock, "pr:7")
    assert data["operation"] == "handoff" and "factory:claimed" not in fake.labels[7]


def test_item_runs_match_the_exact_item_number() -> None:
    fake = Fake()
    fake.runs = [
        {"id": 1, "display_title": "Label claim · #70 · issues · a", "created_at": T0.isoformat()},
        {"id": 2, "display_title": "Label claim · #7 · issues · a", "created_at": T0.isoformat()},
    ]
    assert [row["run_id"] for row in claimops.item_runs(cast(GitHub, fake), 7)] == [2]


def _pr(merged: bool, state: str = "open") -> dict[str, Any]:
    return {
        "state": state,
        "merged": merged,
        "head": {"sha": "a" * 40, "ref": "work-9"},
        "body": "Closes #9",
        "title": "t",
    }


def test_follow_stops_when_the_pr_merged_and_its_issue_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(external, "_required_checks_outcome", lambda github, number: "passed")
    fake, clock = Fake(), Ticking()
    fake.items = {
        12: {"state": "open", "labels": [{"name": "factory:review"}]},
        9: {"state": "open", "labels": []},
    }
    fake.pulls = {12: _pr(False)}
    seen: list[str] = []

    def progress(view: dict[str, object], keys: list[str]) -> None:
        seen.append(str(view.get("stage")))
        if len(seen) == 1:
            fake.items[12] = {"state": "open", "labels": [{"name": "factory:ci"}]}
        elif len(seen) == 2:
            fake.items[12] = {"state": "closed", "labels": []}
            fake.pulls[12] = _pr(True, "closed")
        elif len(seen) == 3:
            fake.items[9] = {"state": "closed", "labels": [{"name": "ralph-status:done"}]}

    data = claimops.follow(cast(GitHub, fake), clock, "pr:12", interval=30, on_change=progress)
    assert data["status"] == "finished"
    assert data["reason"] == "merged and its issue is closed"
    assert seen == ["factory:review", "factory:ci", "None", "None"]


def test_follow_holds_on_a_blocked_item() -> None:
    fake = Fake()
    fake.items = {5: {"state": "open", "labels": [{"name": "factory:blocked"}]}}
    data = claimops.follow(cast(GitHub, fake), Ticking(), "issue:5", interval=30)
    assert (data["status"], data["reason"]) == ("finished", "blocked")


def test_own_review_offers_are_not_takeable() -> None:
    fake = Fake()
    fake.pulls = {7: {"user": {"login": "alice"}}}
    assert claimops.own_review(cast(GitHub, fake), {"action": "review", "target": "pr:7"}, "alice")
    assert not claimops.own_review(
        cast(GitHub, fake), {"action": "review", "target": "pr:7"}, "bob"
    )
    assert not claimops.own_review(
        cast(GitHub, fake), {"action": "ci-repair", "target": "pr:7"}, "alice"
    )


def test_failed_requests_are_described_without_field_values() -> None:
    assert (
        describe_request(
            ["api", "repos/a/b/issues/5/labels/factory%3Aavailable", "--method", "DELETE"]
        )
        == "DELETE repos/a/b/issues/5/labels/factory%3Aavailable"
    )
    assert (
        describe_request(
            ["api", "repos/a/b/issues/5/comments", "--method", "POST", "-f", "body=secret text"]
        )
        == "POST repos/a/b/issues/5/comments"
    )
    assert "secret" not in describe_request(["issue", "comment", "5", "--body", "secret"])


def test_github_errors_carry_the_failed_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = tmp_path / "gh"
    script.write_text("#!/bin/sh\necho 'HTTP 404: Not Found' >&2\nexit 1\n")
    script.chmod(0o755)
    client = gh.GitHub(REPO, executable=str(script))
    with pytest.raises(GitHubNotFoundError) as caught:
        client.gh("api", "repos/acme/widgets/issues/1/labels/x", "--method", "DELETE")
    assert caught.value.request == "DELETE repos/acme/widgets/issues/1/labels/x"


def test_parse_target_rejects_anything_else() -> None:
    for bad in ("7", "pr:0", "pull:7", "issue:7;rm"):
        with pytest.raises(ValueError):
            claimops.parse_target(bad)


@pytest.mark.parametrize(
    ("name", "query", "expected"),
    [
        ("runs", {"target": "pr:5"}, ["runs", "pr:5"]),
        ("follow", {"target": "issue:9"}, ["follow", "issue:9", "--once"]),
    ],
)
def test_web_reads_cover_the_new_commands(
    name: str, query: dict[str, str], expected: list[str]
) -> None:
    assert web.read_command(name, query) == expected


def test_web_writes_never_block_on_a_bridge_run() -> None:
    assert web.write_command({"operation": "claim", "target": "pr:5"}) == [
        "claim",
        "pr:5",
        "--no-wait",
    ]
    assert web.write_command({"operation": "finish", "target": "issue:5"}) == [
        "finish",
        "issue:5",
        "--no-wait",
    ]
    assert web.write_command({"operation": "labels", "mode": "repair"}) == [
        "labels",
        "--repair",
        "--via",
        "github",
        "--no-wait",
    ]
    with pytest.raises(web.InvalidRequest):
        web.write_command({"operation": "labels", "mode": "rm -rf"})
    with pytest.raises(web.InvalidRequest):
        web.write_command({"operation": "claim", "target": "pr:5 --force"})


def test_bridge_waits_while_a_new_run_still_reads_as_missing(
    monkeypatch: pytest.MonkeyPatch, artifacts: dict[int, dict[str, Any]]
) -> None:
    monkeypatch.setattr(bridge, "_start", lambda github, workflow, inputs: (55, "run-url"))
    artifacts[55] = {"applied": 0}
    reads = iter([GitHubNotFoundError("missing"), {"status": "completed", "conclusion": "success"}])

    class Runs:
        repo = REPO

        def repo_api(self, route: str) -> Any:
            assert route == "actions/runs/55"
            value = next(reads)
            if isinstance(value, Exception):
                raise value
            return value

    data = claimops.doctor(cast(GitHub, Runs()), Ticking(), "dry-run")
    assert data["applied"] == 0


def test_bridge_gives_up_on_a_missing_run_at_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge, "_start", lambda github, workflow, inputs: (55, "run-url"))

    class Missing:
        repo = REPO

        def repo_api(self, route: str) -> Any:
            raise GitHubNotFoundError("missing")

    with pytest.raises(bridge.BridgeError, match="could not be read"):
        claimops.doctor(cast(GitHub, Missing()), Ticking(), "dry-run")

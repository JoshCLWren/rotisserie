"""Owner rule (2026-11-24): only the current head's author model may not review it."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
sys.path.insert(0, str(SCRIPTS))
policy = importlib.import_module("factory_review_policy")
guard = importlib.import_module("factory_review_model_guard")

PR = 7
FIRST, SECOND, HEAD = "1" * 40, "2" * 40, "3" * 40
SYNC, MAIN = "4" * 40, "5" * 40
A, B, C = "zai/glm-4.6", "mistral/devstral-2", "opencode-go/hy4-flash-free"


def author(model: str, head: str, agent: str = "43") -> str:
    return policy.agent_model_author_marker(pr=PR, head=head, model=model, agent=agent)


def reviewed(model: str, head: str = HEAD, agent: str = "17") -> str:
    return policy.reviewer_model_marker(
        pr=PR, head=head, reviewer_model=model, reviewer_agent=agent
    )


def history() -> list[str]:
    return [author(A, FIRST, "40"), author(B, SECOND, "41"), author(C, HEAD, "42")]


@pytest.mark.parametrize("earlier", [A, B])
def test_gate_accepts_an_earlier_head_author(earlier: str) -> None:
    verdict = policy.evaluate_review_eligibility(
        [*history(), reviewed(earlier)], pr=PR, head=HEAD, approvers={"17"}
    )
    assert verdict.eligible and verdict.status == "eligible"
    assert verdict.author_models == ("opencode-go+hy4-flash-free",)


def test_gate_rejects_the_current_head_author() -> None:
    verdict = policy.evaluate_review_eligibility(
        [*history(), reviewed("zen/hy4-flash")], pr=PR, head=HEAD, approvers={"17"}
    )
    assert not verdict.eligible and verdict.status == "self-review"
    assert "authored the current head" in verdict.reason


def test_head_without_an_author_record_excludes_no_model() -> None:
    comments = [author(A, FIRST, "40"), author(B, SECOND, "41"), reviewed(A)]
    verdict = policy.evaluate_review_eligibility(comments, pr=PR, head=HEAD, approvers={"17"})
    assert verdict.eligible and verdict.author_models == ()


def test_human_head_excludes_no_model() -> None:
    human = policy.agent_model_author_marker(pr=PR, head=HEAD, model="human", agent="interactive-o")
    comments = [author(A, FIRST), human, reviewed(A)]
    assert policy.evaluate_review_eligibility(comments, pr=PR, head=HEAD).eligible


def test_conflicting_head_markers_exclude_every_named_model() -> None:
    comments = [author(A, HEAD, "40"), author(B, HEAD, "41")]
    assert policy.head_author_record(comments, pr=PR, heads=(HEAD,))[1] == (
        "mistral+devstral-2",
        "zai+glm-4.6",
    )
    verdict = policy.evaluate_review_eligibility([*comments, reviewed(B)], pr=PR, head=HEAD)
    assert verdict.status == "self-review"


def test_worker_push_without_a_model_excludes_every_recorded_author() -> None:
    pushed = policy.head_contributor_marker(pr=PR, head=HEAD, worker="12", epoch=1)
    comments = [author(A, FIRST), author(B, SECOND), pushed]
    assert policy.head_author_record(comments, pr=PR, heads=(HEAD,)) == (
        HEAD,
        ("mistral+devstral-2", "zai+glm-4.6"),
        "unrecorded-worker",
    )


class Hub:
    """Serves ``gh api`` reads from a table, as the guard's subprocess would."""

    def __init__(
        self, comments: list[str], commits: dict[str, dict[str, object]], head: str = HEAD
    ) -> None:
        self.head = head
        self.comments = [
            {"user": {"login": "github-actions[bot]"}, "body": body} for body in comments
        ]
        self.commits = commits
        self.compares: dict[str, object] = {}

    def route(self, path: str) -> object:
        if path == f"pulls/{PR}":
            return {"head": {"sha": self.head}, "base": {"ref": "main"}}
        if path.startswith(f"issues/{PR}/comments"):
            return self.comments
        if path.startswith("commits/"):
            return self.commits.get(path.removeprefix("commits/"), {})
        return self.compares.get(path)

    def run(self, args: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        path = next(arg for arg in args if arg.startswith("repos/")).split("/", 3)[3]
        payload = self.route(path)
        if "--slurp" in args:
            payload = [payload]
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")


def commit(sha: str, *parents: str, message: str = "work") -> dict[str, object]:
    return {"sha": sha, "parents": [{"sha": p} for p in parents], "commit": {"message": message}}


def guard_authors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], hub: Hub
) -> list[str]:
    monkeypatch.setattr(guard.subprocess, "run", hub.run)
    assert guard.main(["authors", "--repo", "acme/widgets", "--pr", str(PR)]) == 0
    return json.loads(capsys.readouterr().out)


def test_guard_excludes_only_the_head_author(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hub = Hub(history(), {HEAD: commit(HEAD, SECOND)})
    excluded = guard_authors(monkeypatch, capsys, hub)
    assert excluded == ["opencode-go+hy4-flash-free"]
    for earlier in (A, B):
        assert (
            guard.main(["check", "--authors-json", json.dumps(excluded), "--model", earlier]) == 1
        )
    assert guard.main(["check", "--authors-json", json.dumps(excluded), "--model", C]) == 0
    assert "authored the current head" in capsys.readouterr().out


def test_guard_head_without_record_excludes_nobody(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hub = Hub([author(A, FIRST), author(B, SECOND)], {HEAD: commit(HEAD, SECOND)})
    assert guard_authors(monkeypatch, capsys, hub) == []


def test_guard_ignores_author_records_outside_the_controller_stream(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hub = Hub([author("hy4-flash", HEAD)], {HEAD: commit(HEAD, SECOND)})
    hub.comments.append(
        {"user": {"login": "someone"}, "author_association": "OWNER", "body": author(A, HEAD)}
    )
    assert guard_authors(monkeypatch, capsys, hub) == ["hy4-flash"]


def test_guard_reads_the_head_commit_model_trailer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hub = Hub([author(A, FIRST)], {HEAD: commit(HEAD, FIRST, message="fix\n\nModel: gpt-5.2")})
    assert guard_authors(monkeypatch, capsys, hub) == ["gpt-5.2"]


def test_guard_carries_the_author_through_a_clean_sync(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    commits = {SYNC: commit(SYNC, SECOND, MAIN), SECOND: commit(SECOND, FIRST)}
    hub = Hub([author(A, FIRST), author(C, SECOND)], commits, head=SYNC)
    hub.compares = {
        f"compare/{MAIN}...main": {"status": "identical"},
        f"compare/{SECOND}...{SYNC}": {"files": [{"filename": "from_main.py"}]},
        f"compare/{MAIN}...{SYNC}": {"files": [{"filename": "pr_only.py"}]},
    }
    assert guard_authors(monkeypatch, capsys, hub) == ["opencode-go+hy4-flash-free"]


def test_guard_does_not_carry_through_a_conflict_resolving_merge(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hub = Hub([author(C, SECOND)], {SYNC: commit(SYNC, SECOND, MAIN)}, head=SYNC)
    hub.compares = {
        f"compare/{MAIN}...main": {"status": "identical"},
        f"compare/{SECOND}...{SYNC}": {"files": [{"filename": "shared.py"}]},
        f"compare/{MAIN}...{SYNC}": {"files": [{"filename": "shared.py"}]},
    }
    assert guard_authors(monkeypatch, capsys, hub) == []

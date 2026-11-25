"""Transient GitHub failures retry idempotent gh calls; writes and hard errors run once."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

import factory_crash
import factory_self_heal

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
BAD_GATEWAY = "gh: Server Error (HTTP 502)"


def _load(name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(f"gh_retry_{name}", SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def controller() -> types.ModuleType:
    return _load("factory-work-controller")


@pytest.fixture(scope="module")
def review() -> types.ModuleType:
    return _load("factory-review-controller")


class FakeGh:
    def __init__(self, *outcomes: str | BaseException) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(command))
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome.startswith("ok:"):
            return subprocess.CompletedProcess(command, 0, outcome[3:], "")
        return subprocess.CompletedProcess(command, 1, "", outcome)


def test_a_502_then_success_returns_the_result(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGh(BAD_GATEWAY, 'ok:{"number": 5678}')
    monkeypatch.setattr(controller.subprocess, "run", gh)
    assert controller.gh_json(["api", "repos/o/r/issues/5678"]) == {"number": 5678}
    assert len(gh.calls) == 2
    assert "retry 2/3" in capsys.readouterr().err


def test_three_502s_raise_github_call_error(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    gh = FakeGh(BAD_GATEWAY)
    monkeypatch.setattr(controller.subprocess, "run", gh)
    with pytest.raises(factory_crash.GitHubCallError, match="HTTP 502"):
        controller.run_gh(["api", "repos/o/r/issues/5678"])
    assert len(gh.calls) == 3


def test_a_post_is_not_retried(controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    gh = FakeGh(BAD_GATEWAY, "ok:{}")
    monkeypatch.setattr(controller.subprocess, "run", gh)
    with pytest.raises(factory_crash.GitHubCallError):
        controller.run_gh(["api", "-X", "POST", "repos/o/r/issues/5/comments", "-f", "body=hi"])
    assert len(gh.calls) == 1


@pytest.mark.parametrize("status", ["HTTP 404", "HTTP 422", "HTTP 401", "HTTP 403"])
def test_hard_errors_are_not_retried(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    gh = FakeGh(f"gh: Not Found ({status}) timeout", "ok:{}")
    monkeypatch.setattr(controller.subprocess, "run", gh)
    with pytest.raises(factory_crash.GitHubCallError):
        controller.run_gh(["api", "repos/o/r/issues/5"])
    assert len(gh.calls) == 1


def test_a_timeout_on_a_read_retries_and_a_timeout_on_a_post_does_not(
    controller: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    gh = FakeGh(subprocess.TimeoutExpired(["gh"], 120), "ok:[]")
    monkeypatch.setattr(controller.subprocess, "run", gh)
    assert controller.run_gh(["api", "repos/o/r/pulls"]) == "[]"
    post = FakeGh(subprocess.TimeoutExpired(["gh"], 120), "ok:{}")
    monkeypatch.setattr(controller.subprocess, "run", post)
    with pytest.raises(factory_crash.GitHubCallError, match="timed out"):
        controller.run_gh(["workflow", "run", "x.yml"])
    assert len(post.calls) == 1


def test_review_controller_and_self_heal_retry_reads(
    review: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    gh = FakeGh("unexpected end of JSON input", "ok:[]")
    monkeypatch.setattr(review.subprocess, "run", gh)
    assert review.run_gh(["pr", "view", "5", "--json", "labels"]).stdout == "[]"
    heal = FakeGh("gh: Bad Gateway (HTTP 502)", 'ok:{"ok": true}')
    monkeypatch.setattr(factory_self_heal.subprocess, "run", heal)
    assert factory_self_heal.GhCli("o/r").api("pulls/5") == {"ok": True}
    assert len(gh.calls) == len(heal.calls) == 2


@pytest.mark.parametrize(
    ("args", "stdin", "expected"),
    [
        (["api", "repos/o/r/issues/5"], None, True),
        (["api", "--paginate", "--jq", ".[]", "repos/o/r/issues/5/comments"], None, True),
        (["api", "--method=PUT", "repos/o/r/issues/5/labels", "--input", "-"], "{}", True),
        (["api", "-XPATCH", "repos/o/r/issues/5", "-f", "state=closed"], None, True),
        (["api", "-X", "DELETE", "repos/o/r/issues/5/labels/x"], None, True),
        (["api", "repos/o/r/issues/5/labels", "-f", "labels[]=x"], None, True),
        (["api", "repos/o/r/issues/5/comments", "-f", "body=x"], None, False),
        (["api", "repos/o/r/issues/5", "--input", "-"], "{}", False),
        (["api", "repos/o/r/pulls/5/merge", "-X", "PUT"], None, True),
        (["api", "graphql", "-f", "query=query { viewer { login } }"], None, True),
        (["api", "graphql", "-f", "query=mutation { resolveReviewThread }"], None, False),
        (["api", "graphql", "--input", "-"], json.dumps({"query": "mutation { x }"}), False),
        (["pr", "list", "--limit", "500"], None, True),
        (["pr", "merge", "5"], None, False),
        (["issue", "comment", "5", "--body", "x"], None, False),
        ([], None, False),
    ],
)
def test_only_idempotent_calls_are_retryable(
    args: list[str], stdin: str | None, expected: bool
) -> None:
    assert factory_crash.gh_idempotent(args, stdin) is expected


def test_secondary_rate_limit_honors_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACTORY_GH_RATE_LIMIT_DELAY", "60")
    monkeypatch.setenv("FACTORY_GH_RETRY_DELAYS", "2,5")
    limited = "HTTP 403: You have exceeded a secondary rate limit"
    assert factory_crash.gh_retry_delay(f"{limited}. Retry-After: 17", 0) == 17
    assert factory_crash.gh_retry_delay(limited, 0) == 60
    assert factory_crash.gh_retry_delay("HTTP 403: Resource not accessible", 0) is None
    assert 2 <= (factory_crash.gh_retry_delay(BAD_GATEWAY, 0) or 0) <= 2.5
    assert 5 <= (factory_crash.gh_retry_delay("HTTP 504", 1) or 0) <= 6.25
    waits: list[float] = []
    gh = FakeGh(f"{limited}. Retry-After: 17", "ok:{}")
    proc = factory_crash.gh_with_retry(["gh", "api", "rate_limit"], lambda: gh(["gh"]), sleep=waits.append)
    assert proc.returncode == 0 and waits == [17]

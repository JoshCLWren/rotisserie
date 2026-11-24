"""`baste unstick` never pins a model the worker would refuse.

The worker refuses an exact model that its exclusion export lists, and a
review on a model that authored the PR. The planner reads the same export from
the published stats and the same author markers, and skips with a reason.
"""

from __future__ import annotations

import base64
import io
import json
from typing import Any

import pytest
from test_factory_console import (  # type: ignore[import-not-found]
    FULL_BACKLOG,
    HEALTHY,
    REPO,
    TTY,
    FakeClock,
    FakeGitHub,
    fake,
    invoke,
    live_catalog,
    stats_snapshot,
    unstick_routes,
)

from rotisserie import cli
from rotisserie.factory_console import commands
from rotisserie.factory_console.service.unstick import Assignment, Task, _assign

__all__ = ["fake", "live_catalog"]

BEST = "opencode-go/nemotron-3.5-lightning-free"
NEXT = "opencode-go/hy4-flash-free"


def snapshot(exclusions: dict[str, list[str]] | None = None) -> dict[str, Any]:
    raw = stats_snapshot(
        ("opencode-free", "hy4-flash-free", 10, 2, HEALTHY),
        ("opencode-free", "nemotron-3.5-lightning-free", 10, 5, HEALTHY),
    )
    payload = json.loads(base64.b64decode(raw["content"]))
    payload["model_exclusions"]["sources"] = exclusions or {}
    return {"content": base64.b64encode(json.dumps(payload).encode()).decode()}


def plan(fake: FakeGitHub, *argv: str) -> tuple[int, dict[str, Any], str]:
    code, out = invoke(["--json", "unstick", *argv, "--dry-run"])
    _, text = invoke(["unstick", *argv, "--dry-run"])
    return code, json.loads(out)["plan"], text


def authored_by(monkeypatch: pytest.MonkeyPatch, pr: int, *models: str) -> None:
    """The shared rule names these models as the current head's authors of PR ``pr``."""
    from rotisserie.factory_console.service import review

    heads = {pr: models}

    def excluded(github: Any, number: int, **_: Any) -> review.Excluded:
        found = heads.get(number, ())
        return review.Excluded(number, "h", found, "", "test", "head author")

    monkeypatch.setattr(review, "excluded_models", excluded)


def test_a_healthy_model_the_worker_excludes_is_never_pinned(fake: FakeGitHub) -> None:
    unstick_routes(fake, FULL_BACKLOG)
    # Healthy in the reliability counts, but in a durable backoff: the worker's
    # export lists it (as it listed nvidia meta/muse-glimmer-30b on 2026-11-24).
    fake.routes["contents/factory-stats.json"] = snapshot(
        exclusions={"opencode-free": ["nemotron-3.5-lightning-free"]}
    )
    code, found, text = plan(fake)
    assert code == 0
    assert {item["model"] for item in found["assignments"]} == {NEXT}
    assert "the worker excludes it right now" in text


def test_every_excluded_model_means_no_worker_and_a_reason(fake: FakeGitHub) -> None:
    unstick_routes(fake, FULL_BACKLOG)
    fake.routes["contents/factory-stats.json"] = snapshot(
        exclusions={"opencode-free": ["nemotron-3.5-lightning-free", "hy4-flash-free"]}
    )
    code, found, text = plan(fake, "opencode-go")
    assert code == 3
    assert found["assignments"] == []
    assert "Every OpenCode Go model with a track record is skipped right now" in text
    assert BEST in text and NEXT in text and "the worker excludes it right now" in text


@pytest.mark.parametrize(
    "block",
    [
        None,
        {"generated_at": "2026-11-21T21:00:00Z", "sources": {}},  # two hours old
        {"generated_at": "2026-11-21T22:50:00Z", "sources": {"nvidia": "not a list"}},
    ],
)
def test_unreadable_exclusions_pin_the_provider_and_let_the_worker_pick(
    fake: FakeGitHub, block: dict[str, Any] | None
) -> None:
    unstick_routes(fake, FULL_BACKLOG)
    payload = json.loads(base64.b64decode(snapshot()["content"]))
    if block is None:
        del payload["model_exclusions"]
    else:
        payload["model_exclusions"] = block
    fake.routes["contents/factory-stats.json"] = {
        "content": base64.b64encode(json.dumps(payload).encode()).decode()
    }
    code, found, text = plan(fake)
    assert code == 0
    assert {(item["provider"], item["model"]) for item in found["assignments"]} == {
        ("opencode-go", "auto")
    }
    assert "the worker picks the model" in found["assignments"][0]["why"]
    assert "exclusions are not published" in text


def test_a_cooling_credit_pool_lets_the_worker_pick_its_free_model(fake: FakeGitHub) -> None:
    unstick_routes(fake, FULL_BACKLOG)
    fake.routes["contents/factory-stats.json"] = snapshot(
        exclusions={"@credit_pool": ["opencode-free"]}
    )
    _, found, _ = plan(fake)
    assert {item["model"] for item in found["assignments"]} == {"auto"}


def test_a_review_skips_models_that_authored_the_pr(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    unstick_routes(fake, FULL_BACKLOG)
    fake.routes["contents/factory-stats.json"] = snapshot()
    authored_by(monkeypatch, 301, BEST)
    _, found, text = plan(fake)
    models = {item["task"]["target"]: item["model"] for item in found["assignments"]}
    assert models["pr:301"] == NEXT
    assert models["pr:306"] == BEST
    assert f"skipped {BEST}: authored this PR" in text


def test_a_review_every_model_wrote_is_not_started(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    unstick_routes(fake, FULL_BACKLOG)
    fake.routes["contents/factory-stats.json"] = snapshot()
    authored_by(monkeypatch, 301, BEST, NEXT)
    _, found, text = plan(fake)
    assert "pr:301" not in [item["task"]["target"] for item in found["assignments"]]
    assert [reason["prs"] for reason in found["no_model"]] == [[301]]
    assert "Not started, and why (1)" in text
    assert "pr:301: not started: every available model authored this PR" in text


def test_a_repair_may_run_on_a_model_that_wrote_the_pr(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    unstick_routes(fake, FULL_BACKLOG)
    fake.routes["contents/factory-stats.json"] = snapshot()
    authored_by(monkeypatch, 302, BEST, NEXT)
    _, found, _ = plan(fake)
    models = {item["task"]["target"]: item["model"] for item in found["assignments"]}
    assert models["pr:302"] == BEST


def test_progress_names_each_assignments_model_and_the_skipped_ones(fake: FakeGitHub) -> None:
    unstick_routes(fake, FULL_BACKLOG)
    fake.routes["contents/factory-stats.json"] = snapshot(
        exclusions={"opencode-free": ["nemotron-3.5-lightning-free"]}
    )
    arguments = cli.parser().parse_args(
        ["factory", "--repo", REPO, "--json", "--progress", "unstick", "--dry-run"]
    )
    err = io.StringIO()
    commands.main(
        arguments,
        env={"TERM": "xterm"},
        stdout=TTY(),
        stdin=TTY(),
        stderr=err,
        clock=FakeClock(),
        executable="sh",
    )
    steps = [json.loads(line) for line in err.getvalue().splitlines()]
    texts = [step["step"] for step in steps]
    assert f"Skipped {BEST}: the worker excludes it right now (cooldown or backoff)" in texts
    assigned = [step for step in steps if step.get("phase") == "assign"]
    assert [step["target"] for step in assigned][:2] == ["pr:301", "pr:306"]
    assert assigned[0]["step"].startswith(f"pr:301: {NEXT} (2 of 10 runs delivered")


def test_unstick_does_not_repeat_a_model_that_just_missed_on_the_item() -> None:
    ranked = [("opencode-go", "opencode-go/a", "best"), ("opencode-go", "opencode-go/b", "next")]
    task = Task("pr:5575", "merge-conflict", "t", "w")
    fresh = _assign(task, ranked, {})
    assert isinstance(fresh, Assignment)
    assert fresh.model == "opencode-go/a" and "missed" not in fresh.why
    moved = _assign(task, ranked, {"pr:5575": {"opencode-go/a"}})
    assert isinstance(moved, Assignment)
    assert moved.model == "opencode-go/b"
    assert "opencode-go/a just missed on it" in moved.why
    exhausted = _assign(task, ranked, {"pr:5575": {"opencode-go/a", "opencode-go/b"}})
    assert isinstance(exhausted, Assignment)
    assert exhausted.model == "opencode-go/a"
    assert "every offered model already missed on it recently" in exhausted.why


def test_a_review_skips_the_model_whose_empty_verdict_the_factory_cleared(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rotisserie.factory_console import runtime

    unstick_routes(fake, FULL_BACKLOG)
    fake.routes["contents/factory-stats.json"] = snapshot()
    authored_by(monkeypatch, 301)
    heal = runtime.module("factory_self_heal_rules").comment_body(
        kind="empty-verdict", pr=301, head="b" * 40, ref="7", text="Moved back.", avoid=BEST
    )
    record = {"id": 9, "user": {"login": "github-actions[bot]", "type": "Bot"}, "body": heal}
    fake.routes = {r"issues/301/comments": [record], **fake.routes}
    _, found, _ = plan(fake)
    models = {item["task"]["target"]: item["model"] for item in found["assignments"]}
    assert models["pr:301"] == NEXT

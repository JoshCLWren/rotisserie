"""A long park gets one forced run on the best proven model, then a release or a closure."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import factory_model_track_record as track
import factory_review_policy as policy
import factory_self_heal as heal
import factory_self_heal_loops as loops
import factory_self_heal_park_retry as retry
import factory_self_heal_rules as rules
import factory_turso_events as events
import pytest
from tests.test_factory_item_loop import HEAD, heal_budget

from tests import turso_support as support

PR, ISSUE = 12, 40
BRANCH = f"factory/7-{ISSUE}-normalize"


@pytest.fixture(params=support.kinds())
def env(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(loops, "incident_pulls", lambda *_a: frozenset())
    monkeypatch.setenv(track.FILE_ENV, str(stats(tmp_path)))
    with support.bound(request.param, tmp_path / "state.sqlite", monkeypatch) as (db, repo):
        yield db, repo


def stats(tmp_path: Path) -> Path:
    def counts(completed: int, failed: int) -> dict[str, Any]:
        return {"completed": completed, "failed": failed, "delivered": completed}

    rows = {
        "strong-model": counts(18, 2),
        "lucky-model": counts(3, 0),
        "bad-model": counts(0, 12),
    }
    stamp = datetime.now(UTC).isoformat()
    path = tmp_path / "stats.json"
    payload = {
        "generated_at": stamp,
        "counts_generated_at": stamp,
        "models": [
            {"source": "opencode-free", "model": name, "windows": {"7d": window}}
            for name, window in rows.items()
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class Hub:
    def __init__(
        self,
        repo: str,
        issue_labels: tuple[str, ...] = ("factory", "factory:unowned", "factory:review"),
    ) -> None:
        self.repo = repo
        self.writes: list[tuple[str, tuple[str, ...], Any]] = []
        self.issue_labels = issue_labels

    def api(self, path: str, *args: str, payload: object | None = None) -> Any:
        if not args:
            return {
                "number": ISSUE,
                "state": "open",
                "labels": [{"name": name} for name in self.issue_labels],
            }
        self.writes.append((path, args, payload))
        return {}

    def comments(self, number: int) -> list[dict[str, Any]]:
        if number != PR:
            return []
        first = policy.review_marker(
            pr=PR, head=HEAD, reviewer="8", producer="7", verdict="repair"
        )
        verdict = f"{first}\nThe change misses the null case."
        return [{"body": verdict, "user": {"login": policy.TRUSTED_COMMENT_LOGIN}}]

    def graphql(self, query: str, **variables: object) -> dict[str, Any]:
        raise AssertionError(query)

    def paths(self) -> list[str]:
        return [path for path, _args, _payload in self.writes]


def pull(*labels: str) -> dict[str, Any]:
    names = ["factory", "factory:unowned", "factory:review", "factory:parked", *labels]
    return {
        "number": PR,
        "title": "Normalize the API key",
        "body": f"Closes #{ISSUE}",
        "html_url": f"https://github.com/o/r/pull/{PR}",
        "head": {"sha": HEAD, "ref": BRANCH},
        "labels": [{"name": name} for name in names],
    }


def park(db: Any, repo: str, hours: float) -> None:
    heal.LedgerStore(repo).record(
        [
            heal.event(
                kind=rules.LOOP_PARKED,
                pr=PR,
                head=HEAD,
                ref="review-x",
                text="4 of 4 worker runs changed nothing.",
                stage="factory:review",
            )
        ]
    )
    age(db, repo, rules.LOOP_PARKED, hours)


def age(db: Any, repo: str, kind: str, hours: float) -> None:
    db.batch(
        [
            (
                "UPDATE factory_state_events SET created_at = created_at - :s"
                " WHERE repo = :repo AND detail ->> 'kind' = :kind",
                {"s": int(hours * 3600), "repo": repo, "kind": kind},
            )
        ]
    )


def run(repo: str, outcome: str, n: int, *, delivered: bool = False) -> None:
    events.record_events(
        repo,
        [
            {
                "event_type": "worker_completed" if outcome == "success" else "worker_failed",
                "event_id": f"runtime:{900 + n}",
                "worker": support.uncapped_workers(1)[0],
                "run_id": str(900 + n),
                "source": "opencode-free",
                "model": "strong-model",
                "outcome_class": outcome,
                "selected_kind": "pr",
                "selected_number": str(PR),
                "action_kind": "review",
                "delivery_outcome": "persisted" if delivered else "none",
            }
        ],
    )


def sweep(repo: str, hub: Hub | None = None, *labels: str) -> tuple[Hub, list[dict[str, Any]]]:
    hub = hub or Hub(repo)
    found = retry.resolve_long_parks(hub, heal.LedgerStore(repo), [pull(*labels)], heal_budget())
    return hub, found


def dispatched(hub: Hub) -> list[dict[str, Any]]:
    return [
        payload["inputs"]  # type: ignore[index]
        for path, _args, payload in hub.writes
        if path.endswith("/dispatches")
    ]


def test_best_is_the_highest_success_floor_among_proven_models(env) -> None:
    assert track.best("review")[:2] == ("opencode-free", "strong-model")  # type: ignore[index]


def test_a_park_under_twelve_hours_is_left_alone(env) -> None:
    db, repo = env
    park(db, repo, 11)
    hub, found = sweep(repo)
    assert found == [] and hub.writes == []


def test_a_long_park_gets_one_forced_run_on_the_best_model(env) -> None:
    db, repo = env
    park(db, repo, 13)
    hub, found = sweep(repo)
    assert dispatched(hub) == [
        {
            "mode": "smoke",
            "action": "advance",
            "provider": "opencode-free",
            "model": "opencode-go/strong-model",
            "force_slot": "true",
            "target": f"pr:{PR}",
        }
    ]
    assert found[0]["kind"] == rules.PARK_RETRY
    assert hub.paths()[-1] == f"issues/{PR}/comments"
    again, found = sweep(repo)
    assert dispatched(again) == [] and found == []


def test_an_owner_override_exempts_the_park(env) -> None:
    db, repo = env
    park(db, repo, 13)
    hub, found = sweep(repo, None, "factory:override")
    assert dispatched(hub) == [] and found == [{"pr": PR, "status": "owner"}]


def test_a_successful_forced_run_ends_the_park(env) -> None:
    db, repo = env
    park(db, repo, 13)
    sweep(repo)
    run(repo, "success", 1)
    hub, found = sweep(repo)
    assert found[0]["kind"] == rules.LOOP_RELEASED
    assert (
        "factory_turso_loops"
        and retry.evidence(repo)["parks"][f"pr:{PR}"]["at_heads"] == set()
    )
    assert f"pulls/{PR}" not in hub.paths()


def test_a_forced_run_that_changed_nothing_closes_the_pr_and_requeues_the_issue(env) -> None:
    db, repo = env
    park(db, repo, 13)
    sweep(repo)
    run(repo, "work_failure", 1)
    hub, found = sweep(repo)
    assert found[0]["kind"] == rules.PARK_CLOSED and found[0]["issue_status"] == "requeued"
    paths = hub.paths()
    assert paths[-1] == f"pulls/{PR}"
    assert hub.writes[-1][2] == {"state": "closed"}
    assert not any("git/refs" in path for path in paths)
    closing = next(p for path, _a, p in hub.writes if path == f"issues/{PR}/comments")
    text = str(closing["body"])  # type: ignore[index]
    assert "4 of 4 worker runs changed nothing." in text and BRANCH in text
    assert "repair at `dddddddddddd`: The change misses the null case." in text
    note = next(p for path, _a, p in hub.writes if path == f"issues/{ISSUE}/comments")
    assert f"pull/{PR}" in str(note["body"])  # type: ignore[index]
    labels = next(p for path, _a, p in hub.writes if path == f"issues/{ISSUE}/labels")
    assert "factory:review" not in labels["labels"]  # type: ignore[index]
    reopened, found = sweep(repo)
    assert f"pulls/{PR}" not in reopened.paths()
    assert found == [{"pr": PR, "status": "reopened after closure"}]


def test_a_quota_failure_waits_and_each_lapse_dispatches_again_without_closing(env) -> None:
    db, repo = env
    park(db, repo, 13)
    sweep(repo)
    run(repo, "provider_quota", 1)
    hub, found = sweep(repo)
    assert found == [] and hub.writes == []
    for attempt in (2, 3):
        age(db, repo, rules.PARK_RETRY, 7)
        hub, found = sweep(repo)
        assert dispatched(hub) and found[0]["attempt"] == attempt
        assert f"pulls/{PR}" not in hub.paths()


def test_one_sweep_acts_on_at_most_three_parks(env) -> None:
    db, repo = env
    numbers = (12, 13, 14, 15)
    heal.LedgerStore(repo).record(
        [
            heal.event(kind=rules.LOOP_PARKED, pr=n, head=HEAD, ref="review-x", text="t",
                       stage="factory:review")
            for n in numbers
        ]
    )
    age(db, repo, rules.LOOP_PARKED, 13)
    hub = Hub(repo)
    pulls = [{**pull(), "number": n} for n in numbers]
    found = retry.resolve_long_parks(hub, heal.LedgerStore(repo), pulls, heal_budget())
    assert len(dispatched(hub)) == 3
    assert {"pr": 15, "status": "would-due"} in found


def test_the_judge_counts_only_runs_after_the_attempt() -> None:
    now = datetime(2026, 11, 25, 12, tzinfo=UTC)
    attempt = retry.Attempt(PR, 1, 1, now - timedelta(hours=1), "m")
    before = retry.loop.Run(now - timedelta(hours=2), "review", "m", "success")
    wasted = retry.loop.Run(now, "review", "m", "work_failure")
    assert retry.judge(attempt, [before], now) == "pending"
    assert retry.judge(attempt, [before, wasted], now) == "stuck"
    delivered = retry.loop.Run(now, "review", "m", "deadline", delivered=True)
    assert retry.judge(attempt, [wasted, delivered], now) == "progress"
    assert retry.judge(attempt, [], now + timedelta(hours=6)) == "lapsed"


def test_without_a_proven_model_the_forced_run_uses_normal_selection(
    env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, repo = env
    stamp = datetime.now(UTC).isoformat()
    windows = {"tiny-model": (2, 0), "bad-model": (0, 12)}
    path = tmp_path / "unproven.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": stamp,
                "counts_generated_at": stamp,
                "models": [
                    {
                        "source": "opencode-free",
                        "model": name,
                        "windows": {"7d": {"completed": ok, "failed": bad, "delivered": ok}},
                    }
                    for name, (ok, bad) in windows.items()
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(track.FILE_ENV, str(path))
    assert track.best("review") is None
    park(db, repo, 13)
    hub, _found = sweep(repo)
    assert dispatched(hub) == [{"mode": "smoke", "action": "advance", "target": f"pr:{PR}"}]

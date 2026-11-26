"""The console reads failed attempts and split status from GitHub, as the factory records them."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from rotisserie import control_contract
from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import GitHub, Obj
from rotisserie.factory_console.service import attempts, splits

BOT: Obj = {"login": "github-actions[bot]", "type": "Bot"}
SINCE = datetime(2026, 11, 26, 5, 0, tzinfo=UTC)
HEAD = "a" * 40


def pull(number: int, issue: int, *, closed: str, merged: str = "", factory: bool = True) -> Obj:
    labels: list[Any] = [{"name": "factory"}] if factory else []
    return {
        "number": number,
        "title": f"Work on {issue}",
        "body": f"Part of #{issue}.",
        "head": {"ref": f"factory/work-checkpoints/issue-{issue}/hy4-flash-free/1", "sha": HEAD},
        "labels": labels,
        "closed_at": closed,
        "merged_at": merged or None,
        "html_url": f"https://github.com/acme/widgets/pull/{number}",
    }


def park_closed(pr: int) -> Obj:
    rules = runtime.module("factory_self_heal_rules")
    text = f"Closed #{pr} as a failed attempt."
    body = rules.comment_body(kind=rules.PARK_CLOSED, pr=pr, head=HEAD, ref="closed-1", text=text)
    return {"user": BOT, "body": body, "created_at": "2026-11-26T05:40:00Z"}


class Hub:
    repo = "acme/widgets"

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def api(self, route: str) -> Any:
        self.calls.append(route)
        return self.routes.get(route.split("?", 1)[0], {"items": []})

    def repo_api(self, route: str) -> Any:
        self.calls.append(route)
        for prefix, answer in self.routes.items():
            if route.startswith(prefix):
                return answer
        return []


def test_each_failed_attempt_shows_what_its_issue_did_next() -> None:
    closed = [
        pull(5823, 5733, closed="2026-11-26T08:49:00Z", merged="2026-11-26T08:49:00Z"),
        pull(5808, 5733, closed="2026-11-26T05:40:48Z"),
        pull(5814, 4839, closed="2026-11-26T05:40:38Z"),
        pull(5770, 4000, closed="2026-11-26T06:00:00Z"),
        pull(5600, 3000, closed="2026-11-20T06:00:00Z"),
    ]
    issue = {"number": 4839, "state": "open", "labels": [{"name": "factory:unowned"}]}
    hub = Hub(
        {
            "pulls?state=closed": closed,
            "pulls?state=open": [],
            "issues/5808/comments": [park_closed(5808)],
            "issues/5814/comments": [park_closed(5814)],
            "issues/5770/comments": [],
            "issues/4839": issue,
        }
    )
    found = attempts.recent(cast(GitHub, hub), since=SINCE)
    assert [(row.pr, row.issue, row.next, row.next_pr) for row in found] == [
        (5808, 5733, "merged", 5823),
        (5814, 4839, "open", None),
    ]
    assert found[1].detail == "queued (factory:unowned)"
    assert not any(call.startswith("issues/5600") or call == "issues/5733" for call in hub.calls)


def test_a_split_issue_shows_its_runs_and_a_rejection_without_a_recorded_reason() -> None:
    plans = runtime.module("factory_split_plan")
    prefix = plans.MARKER_PREFIX
    claim = (
        f"<!-- {prefix}-factory-controller-claim-v1:issue-5743:1327811785100:1795695872"
        ":source=operator:action=split -->"
    )
    release = (
        f"<!-- {prefix}-factory-claim-released-v3:issue-5743:"
        "opencode-free-model-factory-1327811785100:1795696109:split-plan-rejected -->"
    )
    issue = {"number": 5743, "title": "Rename native_roll", "html_url": "https://x/5743"}
    hub = Hub(
        {
            "issues?labels=factory%3Aneeds-split": [issue],
            "issues?labels=umbrella": [issue],
            "issues/5743/comments": [
                {"user": BOT, "body": claim, "created_at": "2026-11-26T12:24:32Z"},
                {"user": BOT, "body": release, "created_at": "2026-11-26T12:28:31Z"},
            ],
        }
    )
    [found] = splits.recent(cast(GitHub, hub), since=SINCE)
    assert found.issue == 5743 and len(found.runs) == 1
    assert found.runs[0]["end"] == "rejected" and found.runs[0]["reason"] == ""
    assert found.state == (
        "plan rejected with no recorded reason; the next split run starts on the next sweep"
    )
    assert found.max_runs == plans.MAX_DISPATCHES
    assert sum(call.startswith("issues/5743/comments") for call in hub.calls) == 1


def test_three_rejected_plans_hold_the_issue() -> None:
    runs = [{"worker": str(n), "end": "rejected", "reason": "x"} for n in range(3)]
    record = {"runs": runs, "done_at": "", "children": 0}
    assert splits.state(record, 3) == "held: 3 split plans rejected, no fallback"
    assert splits.state({**record, "runs": runs[:1]}, 3).startswith("plan rejected; the next")
    assert splits.state({**record, "done_at": "t", "children": 4}, 3) == "done: 4 child issues"


def test_both_views_are_bounded_live_reads() -> None:
    assert control_contract.read_command("attempts", {}) == ["attempts", "--since", "24h"]
    assert control_contract.read_command("splits", {}) == ["splits", "--since", "3d"]
    assert {"attempts", "splits"} <= control_contract.LIVE_READS


def test_the_sdist_carries_every_runtime_source_the_wheel_force_includes() -> None:
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / "pyproject.toml").read_text())
    sdist = set(config["tool"]["hatch"]["build"]["targets"]["sdist"]["include"])
    assert {f"/{source}" for source in runtime.RUNTIME_SOURCES.values()} <= sdist

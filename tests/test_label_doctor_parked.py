"""The label doctor keeps ``factory:parked`` in line with the loop breaker's park at each head."""

from __future__ import annotations

from typing import Any, cast

from test_label_claims import NOW, FakeGitHub, snapshot  # type: ignore[import-not-found]

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import GitHub, GitHubError
from rotisserie.factory_console.service import labels

PARKED = "factory:parked"
HEAD = "d" * 40


class Hub(FakeGitHub):  # type: ignore[misc]
    pulls: list[dict[str, Any]] = []
    search_hits: list[dict[str, Any]] = []

    def repo_api(self, route: str) -> Any:
        if route.startswith("issues/") and "/comments" in route:
            return self.comments.get(int(route.split("/")[1]), [])
        if route.startswith("pulls?state=open"):
            return self.pulls
        return super().repo_api(route)

    def api(self, route: str, **kwargs: Any) -> Any:
        assert route.startswith("search/issues?"), route
        return {"items": self.search_hits}

    def gh(self, *args: str, **kwargs: Any) -> str:
        return "[]" if args[:2] == ("variable", "list") else super().gh(*args, **kwargs)

    bodies: dict[int, str] = {}

    def _row(self, number: int) -> dict[str, Any]:
        return {**super()._row(number), "body": self.bodies.get(number, "")}

    def _gh(self, *args: str) -> str:
        names = [a.removeprefix("name=") for a in args if a.startswith("name=")]
        if args[1].endswith("/labels") and names:
            self.writes.append(("define", 0, names[0]))
            return "{}"
        return str(super()._gh(*args))


def park_comment(number: int, head: str) -> dict[str, Any]:
    rules = runtime.module("factory_self_heal_rules")
    body = rules.comment_body(
        kind=rules.LOOP_PARKED, pr=number, head=head, ref=f"review-{head[:12]}", text="Parked."
    )
    return {"user": {"login": "github-actions[bot]", "type": "Bot"}, "body": body}


def hub(*names: str, head: str = HEAD, parked_head: str | None = HEAD) -> Hub:
    github = Hub({9: {"factory", "factory:unowned", "factory:review", *names}})
    github.kinds[9] = "pr"
    github.pulls = [
        {"number": 9, "title": "Cards", "head": {"sha": head}, "labels": [{"name": "factory"}]}
    ]
    if parked_head is not None:
        github.search_hits = [{"number": 9}]
        github.comments[9] = [park_comment(9, parked_head)]
    return github


def repair(github: Hub) -> Any:
    return labels.repair(cast(GitHub, github), now=NOW, snapshot=snapshot())


def test_the_doctor_adds_the_label_to_a_pr_parked_at_its_head() -> None:
    github = hub()
    repair(github)
    assert ("define", 0, PARKED) in github.writes
    assert ("add", 9, PARKED) in github.writes


def test_the_doctor_removes_the_label_once_the_head_moves() -> None:
    github = hub(PARKED, head="e" * 40)
    repair(github)
    assert ("remove", 9, PARKED) in github.writes


def test_the_doctor_removes_the_label_when_no_park_exists() -> None:
    github = hub(PARKED, parked_head=None)
    repair(github)
    assert ("remove", 9, PARKED) in github.writes


def test_the_doctor_removes_the_label_under_an_owner_override() -> None:
    github = hub(PARKED, "factory:override")
    github.pulls[0]["labels"] = [{"name": "factory:override"}]
    repair(github)
    assert ("remove", 9, PARKED) in github.writes


def test_a_failed_park_read_leaves_the_label_alone() -> None:
    class Down(Hub):
        def api(self, route: str, **kwargs: Any) -> Any:
            raise GitHubError("search is down")

    github = Down({9: {"factory", "factory:unowned", "factory:review", PARKED}})
    github.kinds[9] = "pr"
    repair(github)
    assert not any(name == PARKED for _, _, name in github.writes)


def test_an_issue_never_keeps_the_label() -> None:
    github = hub(parked_head=None)
    github.items[4] = {"factory", "factory:unowned", "factory:building", PARKED}
    repair(github)
    assert ("remove", 4, PARKED) in github.writes

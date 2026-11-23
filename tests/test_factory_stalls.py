"""Review deadlocks found by hand on ComicPile (#177): #5688 and #5684."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from rotisserie.factory_console.github import GitHub, Obj
from rotisserie.factory_console.service import doctor, stalls

REPO = "acme/widgets"
HEAD = "b" * 40
VERDICT = (
    "<!-- comic-pile-factory-semantic-review-v1:pr-44:head-"
    + HEAD
    + ":reviewer-123:producer-unknown:verdict-{verdict} -->\n\n"
    "### Factory semantic review · Factory 123 · REPAIR\n\n"
    "Semantic blockers remain.\n\n"
    "<details><summary>Review output</summary>\n\n```text\n{excerpt}\n```\n</details>"
)
NARRATION = (
    "I'll start by reading the AGENTS.md file and understanding the PR context.\n"
    "> build · poolside/laguna-xs-2.1\n"
    "$ git log -3\n"
    "(no output)\n"
    "    Model: z-ai/glm-5.3-flash\n"
    "native/roll-service/src/comicvine_v2.rs\n"
    "gh pr diff 44"
)
FINDING = "The retry loop never resets its counter, so the second failure aborts the job."


def comment(login: str, body: str, *, oid: str = HEAD, url: str = "u") -> Obj:
    return {"author": {"login": login}, "body": body, "commit": {"oid": oid}, "url": url}


def thread(
    *comments: Obj, resolved: bool = False, outdated: bool = False, more: bool = False
) -> Obj:
    return {
        "isResolved": resolved,
        "isOutdated": outdated,
        "path": "src/app.ts",
        "comments": {"pageInfo": {"hasNextPage": more}, "nodes": list(comments)},
    }


ANSWER = [
    comment("greptile-apps", "P0: the test never awaits the refresh"),
    comment("github-actions", "**Fixed** in `" + HEAD + "` by ComicPile Factory 100", url="r"),
]
ANSWERED = thread(*ANSWER)


class Hub:
    repo = REPO

    def __init__(self, threads: list[Obj], labels: list[str], records: list[Obj]) -> None:
        self.threads = threads
        self.labels = labels
        self.records = records
        self.more_threads = False

    def gh(self, *args: str) -> str:
        assert args[:2] == ("api", "graphql")
        assert "number=44" in args
        nodes: list[Any] = list(self.threads)
        listing = {"pageInfo": {"hasNextPage": self.more_threads}, "nodes": nodes}
        return json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": listing}}}})

    def repo_api(self, route: str) -> Any:
        if route.startswith("pulls/44"):
            return self.pull()
        if route.startswith("issues/44/comments"):
            return self.records
        if route.startswith("pulls?state=open"):
            return [self.pull()]
        raise AssertionError(route)

    def pull(self) -> Obj:
        return {
            "number": 44,
            "head": {"sha": HEAD},
            "labels": [{"name": name} for name in ["factory", *self.labels]],
        }


def record(body: str, login: str = "github-actions[bot]") -> Obj:
    return {"user": {"login": login}, "body": body, "html_url": "https://x/c1"}


def inspect(hub: Hub) -> list[stalls.Stall]:
    return stalls.inspect(cast(GitHub, hub), 44)


def test_an_answered_unresolved_head_thread_is_a_deadlock() -> None:
    [found] = inspect(Hub([ANSWERED], ["factory:review"], []))
    assert found.kind == "answered-thread"
    assert "greptile-apps's thread on src/app.ts" in found.text
    assert found.url == "r"


@pytest.mark.parametrize(
    "shape",
    [
        thread(*ANSWER, resolved=True),
        thread(*ANSWER, outdated=True),
        # Never answered: ordinary repair work, not a deadlock.
        thread(comment("greptile-apps", "P1: missing null check")),
        # The reviewer spoke last (it re-reviewed): it owns the thread now.
        thread(
            *ANSWER,
            comment("greptile-apps", "Still broken: the mock is never reset."),
        ),
        # The merge gate ignores the retired reviewer; so does this check.
        thread(
            comment("coderabbitai[bot]", "nit"),
            comment("github-actions", "**Fixed** in `" + HEAD + "`"),
        ),
        # Older head only: the gate does not deny this head.
        thread(
            comment("greptile-apps", "P1", oid="a" * 40),
            comment("github-actions", "**Fixed** in `abc1234`", oid="a" * 40),
        ),
    ],
)
def test_other_threads_are_not_deadlocks(shape: Obj) -> None:
    assert inspect(Hub([shape], ["factory:review"], [])) == []


def test_a_thread_longer_than_one_page_is_reported_not_guessed() -> None:
    [found] = inspect(Hub([thread(*ANSWER, more=True)], ["factory:review"], []))
    assert found.kind == "unchecked-threads"
    assert "more than 100 comments" in found.text


def test_more_threads_than_one_page_is_reported() -> None:
    hub = Hub([ANSWERED], ["factory:review"], [])
    hub.more_threads = True
    found = inspect(hub)
    assert [stall.kind for stall in found] == ["unchecked-threads", "answered-thread"]


def test_the_doctor_scans_every_watched_pr() -> None:
    class Many(Hub):
        def repo_api(self, route: str) -> Any:
            if route.startswith("pulls?state=open"):
                return [self.pull() for _ in range(60)]
            return super().repo_api(route)

    found = doctor.review_deadlocks(cast(GitHub, Many([ANSWERED], ["factory:review"], [])))
    assert found.status == "warn"
    assert found.text.startswith("60 deadlock(s)")


def test_a_finding_free_repair_verdict_is_a_deadlock() -> None:
    hub = Hub(
        [],
        ["factory:changes-requested"],
        [record(VERDICT.format(verdict="repair", excerpt=NARRATION))],
    )
    [found] = inspect(hub)
    assert found.kind == "empty-repair-verdict"
    assert "names no finding" in found.text
    assert "run PROVIDER auto --force --target pr:44 --action review" in found.hint
    assert "rotisserie factory stats" in found.hint
    assert found.url == "https://x/c1"


@pytest.mark.parametrize(
    ("records", "labels"),
    [
        (
            [record(VERDICT.format(verdict="repair", excerpt=FINDING))],
            ["factory:changes-requested"],
        ),
        # The newest verdict is an approval: nothing sends it to repair now.
        (
            [
                record(VERDICT.format(verdict="repair", excerpt=NARRATION)),
                record(VERDICT.format(verdict="approve", excerpt=FINDING)),
            ],
            ["factory:changes-requested"],
        ),
        # Only the controller's own account writes verdicts.
        (
            [record(VERDICT.format(verdict="repair", excerpt=NARRATION), login="someone")],
            ["factory:changes-requested"],
        ),
        # Not in repair: an empty verdict holds nothing.
        ([record(VERDICT.format(verdict="repair", excerpt=NARRATION))], ["factory:review"]),
        # Unwatched stage: nothing is read at all.
        ([record(VERDICT.format(verdict="repair", excerpt=NARRATION))], ["factory:building"]),
    ],
)
def test_other_verdicts_are_not_deadlocks(records: list[Obj], labels: list[str]) -> None:
    assert inspect(Hub([], labels, records)) == []


def test_doctor_names_the_deadlocked_prs() -> None:
    hub = Hub([ANSWERED], ["factory:review"], [])
    found = doctor.review_deadlocks(cast(GitHub, hub))
    assert found.status == "warn"
    assert "#44" in found.text
    assert "review pr:N" in found.hint
    clean = doctor.review_deadlocks(cast(GitHub, Hub([], ["factory:review"], [])))
    assert clean.status == "ok"


# Model availability: workers failing before or during their session ------------------


class Runs:
    """Worker runs and their jobs, as the Actions API returns them."""

    repo = REPO

    def __init__(self, runs: list[tuple[str, str]]) -> None:
        # (conclusion, failing step name or "")
        self.runs = runs
        self.routes: list[str] = []

    def repo_api(self, route: str) -> Any:
        self.routes.append(route)
        if route.startswith("actions/workflows/"):
            return {
                "workflow_runs": [
                    {"id": index, "conclusion": conclusion}
                    for index, (conclusion, _step) in enumerate(self.runs, start=1)
                ]
            }
        run_id = int(route.split("/")[2])
        _conclusion, step = self.runs[run_id - 1]
        steps = [{"name": step, "conclusion": "failure"}] if step else []
        return {"jobs": [{"name": "run / factory", "steps": steps}]}


def availability(runs: list[tuple[str, str]]) -> doctor.Finding:
    from datetime import UTC, datetime

    from rotisserie.factory_console.tracker import Tracker

    hub = Runs(runs)
    github = cast(GitHub, hub)
    found = doctor.model_availability(
        github, datetime(2026, 11, 23, 6, 30, tzinfo=UTC), Tracker(github)
    )
    assert "created=2026-11-23T05:30:00Z..2026-11-23T06:30:00Z" in hub.routes[0]
    return found


PICK = doctor.MODEL_PICK_STEP
SESSION = "Run continuous fixed-model factory session"


def test_a_fleet_where_no_worker_succeeds_fails_the_doctor() -> None:
    found = availability([("failure", PICK)] * 5 + [("failure", SESSION)] * 10)
    assert found.status == "fail"
    assert found.text == (
        "0 of the last 15 finished workers succeeded; 5 could not pick any model (every "
        "candidate cooling down, used up, or an author of the PR); 10 failed during their "
        "session (mostly provider capacity)"
    )
    assert "factory-model-health-reset.yml" in found.hint


def test_some_workers_without_a_model_is_a_warning() -> None:
    found = availability([("success", "")] * 4 + [("failure", PICK)])
    assert found.status == "warn"
    assert found.text.startswith("4 of the last 5 finished workers succeeded; 1 could not")


@pytest.mark.parametrize(
    "runs",
    [
        [],
        [("success", "")] * 3,
        # A minority of in-session failures is normal churn.
        [("success", "")] * 3 + [("failure", SESSION)],
    ],
)
def test_healthy_or_idle_fleets_are_ok(runs: list[tuple[str, str]]) -> None:
    assert availability(runs).status == "ok"

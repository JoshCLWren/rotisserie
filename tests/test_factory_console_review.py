"""`rotisserie factory review`: who may approve a PR, and safe requeue from the old gate."""

from __future__ import annotations

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import JSON, GitHub, Obj
from rotisserie.factory_console.service import review

REPO = "acme/widgets"
HEAD = "a" * 40
OLD = "b" * 40
BOT: Obj = {"login": "github-actions[bot]"}
policy = runtime.module("factory_review_policy")


class FakeGitHub(GitHub):
    """Serves repo_api paths from a table and records every call."""

    def __init__(self, routes: dict[str, JSON]) -> None:
        super().__init__(REPO)
        self.routes = routes
        self.requests: list[tuple[str, str]] = []

    def repo_api(self, path: str, **kwargs: object) -> JSON:
        method = str(kwargs.get("method", "GET"))
        self.requests.append((method, path))
        if method != "GET":
            return None
        for prefix, value in self.routes.items():
            if path.startswith(prefix):
                return value
        return {}


def pull(labels: list[str], head: str = HEAD, number: int = 7) -> Obj:
    return {
        "number": number,
        "state": "open",
        "title": "Fix #3",
        "body": "Closes #3.",
        "head": {"sha": head, "ref": "factory/43-3-x"},
        "labels": [{"name": name} for name in labels],
    }


def bot(body: str) -> Obj:
    return {"user": BOT, "author_association": "NONE", "body": body}


def approval(head: str = HEAD) -> Obj:
    marker = policy.review_marker(pr=7, head=head, reviewer="17", producer="43", verdict="approve")
    return bot(marker)


def blocked_note(head: str = OLD) -> Obj:
    marker = policy.review_marker(pr=7, head=head, reviewer="17", producer="43", verdict="approve")
    return bot(
        f"{marker}\n\n### Factory semantic review · Factory 17 · APPROVE\n\n"
        "Blocked: exact-head cross-model author/reviewer provenance is missing or invalid."
    )


def author(model: str, head: str = OLD) -> Obj:
    return bot(policy.agent_model_author_marker(pr=7, head=head, model=model, agent="43"))


def routes(pr: Obj, comments: list[Obj], issue_labels: list[str] | None = None) -> dict[str, JSON]:
    return {
        "pulls/7": pr,
        "issues/7/comments": comments,  # type: ignore[dict-item]
        "issues/3": {"labels": [{"name": n} for n in (issue_labels or [])]},
    }


def test_sync_head_blocked_by_the_old_gate_is_requeued_without_inventing_authors() -> None:
    comments = [author("opencode-go/hy4-flash-free"), blocked_note(OLD)]
    github = FakeGitHub(
        routes(pull(["factory", "factory:unowned", "factory:blocked"]), comments,
               ["factory", "factory:unowned", "factory:blocked"])
    )  # fmt: skip
    report = review.inspect(github, 7)
    assert report.status == "no-approval"
    assert report.author_models == ("opencode-go+hy4-flash-free",)
    assert not report.head_attributed
    assert report.blocked_by_review_gate and report.block_head == OLD
    # Two reads: the PR and its comments.
    assert [path for _m, path in github.requests] == [
        "pulls/7",
        "issues/7/comments?per_page=100&page=1",
    ]

    done = review.requeue(github, report)
    assert done.ok and done.issue == 3
    writes = [(m, p) for m, p in github.requests if m != "GET"]
    assert ("DELETE", "issues/7/labels/factory:blocked") in writes
    assert ("DELETE", "issues/3/labels/factory:blocked") in writes
    assert all("comments" not in path for _m, path in writes)  # no markers written


def test_unknown_author_any_reviewer_counts() -> None:
    github = FakeGitHub(
        routes(pull(["factory", "factory:unowned", "factory:review"]), [approval()])
    )
    report = review.inspect(github, 7)
    assert report.eligible and report.author_models == ()


def test_known_author_reviewing_itself_is_self_review() -> None:
    reviewer = bot(
        policy.reviewer_model_marker(
            pr=7, head=HEAD, reviewer_model="zen/hy4-flash", reviewer_agent="17"
        )
    )
    github = FakeGitHub(
        routes(pull(["factory", "factory:unowned", "factory:review"]),
               [author("opencode-go/hy4-flash-free"), approval(), reviewer])
    )  # fmt: skip
    report = review.inspect(github, 7)
    assert report.status == "self-review" and not report.eligible
    assert any("different model" in action for action in report.actions)


def test_requeue_refuses_other_blocks_held_prs_and_moved_heads() -> None:
    other = bot(
        "### Factory semantic review · Factory 17 · REPAIR\n\nBlocked: issue is manual-only."
    )
    github = FakeGitHub(routes(pull(["factory", "factory:unowned", "factory:blocked"]), [other]))
    report = review.inspect(github, 7)
    assert not report.blocked_by_review_gate
    assert not review.requeue(github, report).ok

    held = FakeGitHub(routes(pull(["factory", "factory:12", "factory:blocked"]), [blocked_note()]))
    held_report = review.inspect(held, 7)
    assert held_report.blocked_by_review_gate
    assert "held by factory:12" in review.requeue(held, held_report).text

    moved = FakeGitHub(
        routes(pull(["factory", "factory:unowned", "factory:blocked"]), [blocked_note()])
    )
    moved_report = review.inspect(moved, 7)
    moved.routes["pulls/7"] = pull(["factory", "factory:unowned", "factory:blocked"], head="c" * 40)
    result = review.requeue(moved, moved_report)
    assert not result.ok and "changed" in result.text
    assert all(method == "GET" for method, _path in moved.requests)


def test_blocked_candidates_filter_one_listing() -> None:
    rows = [
        pull(["factory", "factory:blocked"], number=1),
        pull(["factory", "factory:review"], number=2),
        pull(["factory:blocked"], number=3),
    ]
    github = FakeGitHub({})
    found = review.blocked_candidates(github, pulls=rows)
    assert [row["number"] for row in found] == [1]
    assert github.requests == []

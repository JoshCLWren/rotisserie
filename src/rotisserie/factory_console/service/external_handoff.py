"""Outside-agent handoff: verify the evidence, then project the PR to review."""

from __future__ import annotations

import importlib.util
import json
import re
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import quote

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import GitHub, obj, string
from rotisserie.factory_console.service.external_checks import _linked_open_issue

REPAIRS = frozenset({"review-findings", "ci-repair", "merge-conflict"})
COMPLETION_STAGES = frozenset({"factory:review", "factory:ci", "factory:changes-requested"})
WORKFLOW = "rotisserie-external-claim.yml"
# Every form GitHub accepts after a closing keyword: #N, owner/repo#N, issue URL.
CLOSING = re.compile(
    r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+"
    r"(?:https://github\.com/(?P<url_repo>[\w.-]+/[\w.-]+)/issues/(?P<url_n>\d+)"
    r"|(?P<repo>[\w.-]+/[\w.-]+)?#(?P<n>\d+))"
)
EVIDENCE = re.compile(
    r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:issues|pull|actions/runs)/[1-9][0-9]*(?:#(?:issuecomment|discussion_r|pullrequestreview)-[0-9]+)?\Z"
)


def _human_hold(core: ModuleType, pr: dict[str, Any]) -> bool:
    labels = core.labels_of(pr)
    return bool(core.eligibility.is_manual_only(string(pr, "body")) or labels & core.BLOCKED_LABELS)


def _project_review_stage(
    github: GitHub,
    core: ModuleType,
    number: int,
    head: str,
    in_scope: Callable[[dict[str, Any]], None],
) -> None:
    """Put a PR into the factory's existing review queue, exactly as a worker would.

    A factory worker finishing an implementation or repair leaves its PR in
    ``factory:review`` with no owner. This is that same projection: labels
    are hints for the existing controllers, never ownership or approval.
    Labels are added and removed one by one from a fresh read, so a hold or
    any other label someone adds meanwhile is never overwritten.
    """
    fresh = obj(github.repo_api(f"pulls/{number}"))
    if _human_hold(core, fresh):
        raise ValueError("the PR is on a human hold (manual-only or blocked); nothing was changed")
    in_scope(fresh)
    labels = core.labels_of(fresh)
    if core.owner_of(labels) not in (None, "factory:unowned"):
        raise ValueError("handoff PR is owned by another worker")
    missing = sorted({"factory", "factory:review", "factory:unowned"} - labels)
    if missing:
        fields = [arg for label in missing for arg in ("-f", f"labels[]={label}")]
        github.gh("api", f"repos/{github.repo}/issues/{number}/labels", "--method", "POST", *fields)
    stale_labels = labels & (set(core.STAGE_LABELS) - {"factory:review"} | {"factory:available"})
    for stale in sorted(stale_labels):
        github.gh(
            "api",
            f"repos/{github.repo}/issues/{number}/labels/{quote(stale, safe='')}",
            "--method",
            "DELETE",
        )
    final = obj(github.repo_api(f"pulls/{number}"))
    if string(obj(final.get("head")), "sha") != head:
        raise ValueError("the pull request moved during handoff; hand off its new head")
    if _human_hold(core, final):
        raise ValueError("a human hold was added during handoff; the claim stays held")
    in_scope(final)
    actual = core.labels_of(final)
    if core.stage_of(actual) != "factory:review" or core.owner_of(actual) != "factory:unowned":
        raise RuntimeError("could not verify the review handoff labels")


def _closed_issues(repo: str, *texts: str) -> set[int]:
    """Every issue in ``repo`` that GitHub would close from these PR texts."""
    found: set[int] = set()
    for text in texts:
        for match in CLOSING.finditer(text):
            other = match.group("url_repo") or match.group("repo")
            if other and other.lower() != repo.lower():
                continue
            found.add(int(match.group("url_n") or match.group("n")))
    return found


def _delivered_authors(github: GitHub, pr: int, since: str | None) -> list[tuple[str, str]]:
    """``(commit, model)`` for every commit this handoff delivers, or refuse.

    Delivered commits are the PR's own (not on its base branch); for a repair,
    only those new since the claimed head. Each must carry exactly one
    ``Model:`` trailer, so every contributing model is recorded.
    """
    policy = runtime.module("factory_review_policy")
    path = f"repos/{github.repo}/pulls/{pr}/commits?per_page=100"
    pages = json.loads(github.gh("api", "--paginate", "--slurp", path))
    own = [
        c
        for page in (pages if isinstance(pages, list) else [])
        if isinstance(page, list)
        for c in page
        if isinstance(c, dict)
    ]
    if len(own) >= 250:
        raise ValueError("the pull request has too many commits to attribute (250+)")
    if since is not None and own:
        route = f"repos/{github.repo}/compare/{since}...{string(own[-1], 'sha')}?per_page=100"
        compared = json.loads(github.gh("api", "--paginate", "--slurp", route))
        new = {
            string(c, "sha")
            for page in (compared if isinstance(compared, list) else [])
            if isinstance(page, dict) and isinstance(page.get("commits"), list)
            for c in page["commits"]
            if isinstance(c, dict)
        }
        own = [c for c in own if string(c, "sha") in new]
    if not own:
        raise ValueError("no new commit on the claimed PR; release the claim instead")
    authors: list[tuple[str, str]] = []
    for commit in own:
        models = policy.model_trailer_models(string(obj(commit.get("commit")), "message"))
        if len(models) != 1:
            raise ValueError(
                f"commit {string(commit, 'sha')[:12]} needs exactly one "
                "'Model: <your model id>' trailer so the factory can keep its author "
                "from approving this work"
            )
        authors.append((string(commit, "sha"), str(next(iter(models)))))
    return authors


def _record_authors(
    github: GitHub,
    pr: int,
    authors: list[tuple[str, str]],
    client: str,
    environment: dict[str, str],
) -> None:
    """Write a controller-trusted author-model marker for each delivered commit.

    Only the Actions bot's comments count as author records, so this runs in
    the GitHub bridge. Every head of a PR counts toward its known authors, so
    one marker per commit records each contributing model exactly once.
    """
    if environment.get("GITHUB_ACTIONS") != "true":
        raise ValueError(
            "handoffs run in the GitHub bridge so the factory records the head's "
            f"author: gh workflow run {WORKFLOW} --repo {github.repo} --ref main "
            "-f operation=handoff -f client=CLIENT -f claim_id=UUID -f evidence=URL"
        )
    policy = runtime.module("factory_review_policy")
    for commit, model in authors:
        marker = policy.agent_model_author_marker(
            pr=pr, head=commit, model=model, agent=f"external-{client}"
        )
        # REST, not `gh issue comment`: its GraphQL addComment needs
        # pull-requests: write, which the bridge's token does not hold.
        github.gh(
            "api",
            f"repos/{github.repo}/issues/{pr}/comments",
            "--method",
            "POST",
            "-f",
            f"body={marker}",
        )


def _verify_and_project_handoff(
    github: GitHub,
    claim: dict[str, Any],
    evidence: str,
    environment: dict[str, str] | None = None,
) -> None:
    """Validate evidence against the fenced task, then hand the PR to review.

    Runs inside the ledger's locked handoff transaction: if anything here
    fails, the claim stays held and nothing is released. Only a pull request
    proven to carry the claimed work is moved into the review queue.
    """
    prefix = f"https://github.com/{github.repo}/"
    if not evidence.startswith(prefix) or not EVIDENCE.fullmatch(evidence):
        raise ValueError("handoff evidence must be a same-repository GitHub URL")
    route = evidence[len(prefix) :].split("#", 1)[0]
    if not route.startswith("pull/"):
        raise ValueError(
            "hand off the pull request that carries the work; with no PR, release the claim"
        )
    kind, number_str = str(claim["target"]).split(":")
    number = int(number_str)
    action = str(claim["action"])
    core = runtime.module("factory_work_policy")
    pr_number = int(route.split("/")[1])
    pr = obj(github.repo_api(f"pulls/{pr_number}"))
    head = string(obj(pr.get("head")), "sha")
    if string(pr, "state") != "open" or pr.get("draft") is True or not head:
        raise ValueError("handoff PR must be open, not a draft, with an exact head")
    labels = core.labels_of(pr)
    if core.owner_of(labels) not in (None, "factory:unowned"):
        raise ValueError("handoff PR is owned by another worker")
    if _human_hold(core, pr):
        raise ValueError("the PR is on a human hold (manual-only or blocked); nothing was changed")
    stage = core.stage_of(labels)
    head_at_claim = ""
    if action == "implement":
        if kind != "issue":
            raise ValueError("implementation handoff must originate from an issue")
        if stage not in (None, "factory:review"):
            raise ValueError("handoff PR is already in a different factory stage")
    elif action in REPAIRS:
        if kind != "pr" or pr_number != number:
            raise ValueError("handoff evidence points to a different PR")
        if head == claim.get("head"):
            raise ValueError("no new commit on the claimed PR; release the claim instead")
        head_at_claim = str(claim.get("head") or "")
        if stage not in COMPLETION_STAGES:
            raise ValueError("the PR left the factory's completion stages")
    elif action == "review":
        if kind != "pr" or pr_number != number:
            raise ValueError("handoff evidence points to a different PR")
        if head != claim.get("head"):
            raise ValueError("the PR moved since the review claim; release it and claim again")
        if stage != "factory:review":
            raise ValueError("the PR left factory:review")
        _check_handoff_scope(github, core, claim, pr_number, pr)
        review = _claimant_review(github, pr_number, head, str(claim["actor"]), evidence)
        _record_review(github, pr_number, head, str(claim["actor"]), review, environment or {})
        return
    else:
        raise ValueError("unsupported claimed action")

    def in_scope(live: dict[str, Any]) -> None:
        _check_handoff_scope(github, core, claim, pr_number, live)

    in_scope(pr)
    authors = _delivered_authors(
        github, pr_number, None if action == "implement" else head_at_claim
    )
    if authors[-1][0] != head:
        raise ValueError("the pull request moved during handoff; hand off its new head")
    # Every handoff gets the bridge's trusted author records for the commits
    # it delivers; the review controller cannot attest every outside head.
    _record_authors(github, pr_number, authors, str(claim["client"]), environment or {})
    _project_review_stage(github, core, pr_number, head, in_scope)


REVIEW_ID = re.compile(r"#pullrequestreview-(?P<id>[0-9]+)\Z")


def _claimant_review(
    github: GitHub, pr: int, head: str, actor: str, evidence: str
) -> dict[str, Any]:
    """The claimant's latest decisive formal review of the claimed head."""
    pages = json.loads(
        github.gh("api", "--paginate", "--slurp", f"repos/{github.repo}/pulls/{pr}/reviews")
    )
    reviews = [
        row
        for page in (pages if isinstance(pages, list) else [])
        if isinstance(page, list)
        for row in page
        if isinstance(row, dict)
        and string(obj(row.get("user")), "login") == actor
        and string(row, "commit_id") == head
        and string(row, "state") in ("APPROVED", "CHANGES_REQUESTED")
    ]
    wanted = REVIEW_ID.search(evidence)
    if wanted:
        reviews = [row for row in reviews if str(row.get("id")) == wanted.group("id")]
    if not reviews:
        raise ValueError("no Approve or Request changes review by the claimant on the claimed head")
    return reviews[-1]


def _record_review(
    github: GitHub,
    pr: int,
    head: str,
    actor: str,
    review: dict[str, Any],
    environment: dict[str, str],
) -> None:
    """Hand the review to the adopter's trusted recorder; only the Actions bot counts."""
    if environment.get("GITHUB_ACTIONS") != "true":
        raise ValueError(
            "review handoffs run in the GitHub bridge so the factory records the reviewer: "
            f"gh workflow run {WORKFLOW} --repo {github.repo} --ref main "
            "-f operation=handoff -f client=CLIENT -f claim_id=UUID -f evidence=URL"
        )
    script = Path.cwd() / ".github" / "scripts" / "factory_external_review.py"
    if not script.is_file():
        raise RuntimeError("the adopter checkout lacks factory_external_review.py")
    spec = importlib.util.spec_from_file_location("factory_external_review", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load the trusted review recorder")
    recorder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recorder)
    recorder.record(pr_number=pr, head=head, actor=actor, review=review, environment=environment)


def _check_handoff_scope(
    github: GitHub,
    core: ModuleType,
    claim: dict[str, Any],
    pr_number: int,
    pr: dict[str, Any],
) -> None:
    """Refuse a handoff whose PR now links work outside the claim's fence.

    A body edit leaves the head unchanged, so this runs on every read of the
    PR during the handoff, including the final one before the claim releases.
    """
    number = int(str(claim["target"]).split(":")[1])
    if claim["action"] == "implement":
        row = {
            "number": pr_number,
            "headRefName": string(obj(pr.get("head")), "ref"),
            "body": string(pr, "body"),
            "title": string(pr, "title"),
        }
        # GitHub closes what the body names; the factory links by branch first.
        # Both must name the claimed issue.
        if (
            core.issue_explicitly_closed_by_pr(row) != number
            or core.linked_issue_from_pr(row) != number
            or _closed_issues(github.repo, string(pr, "body"), string(pr, "title")) != {number}
        ):
            raise ValueError("PR body must say 'Closes #N' for the claimed issue and nothing else")
        return
    # A repair claim fenced the PR and the issue it linked at reservation. A
    # body edited since to link another open issue would hand off (and
    # release) work this claim never held; a dropped or closed link is fine.
    link = _linked_open_issue(github, pr_number, pr)
    if link is not None and link not in [int(n) for n in claim.get("targets") or []]:
        raise ValueError(
            f"the PR now links #{link}, which this claim does not hold; "
            "release the claim and reserve the PR again"
        )


def _evidence_pr(github: GitHub, evidence: str) -> int | None:
    """The PR number a handoff URL names, so the ledger can check nobody holds it."""
    route = evidence.removeprefix(f"https://github.com/{github.repo}/").split("#", 1)[0]
    return int(route.split("/")[1]) if route.startswith("pull/") else None

#!/usr/bin/env python3
"""An outside agent's approval at the current head authorizes the merge.

Owner decision (2026-11-25): "if an agent approves a pr it should merge it."
The owner account approves with a formal APPROVED review that carries a
``Model:`` line, or with a ``Verdict: approve`` line under the rules of
factory_outside_verdict.py. That approval makes the PR ``factory:ready``, and
the ready drain merges it after its usual exact-head gates. Safeguards: the
approval is on the current head, the approver's model authored no commit on
the PR and is not the recorded head author, and no change request from anyone
stands on the same head. The acceptance gate stays with factory reviewers.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

import factory_outside_verdict as verdicts
import factory_review_exclusion as exclusion
import factory_review_policy as policy

Fetch = Callable[[str], object]
ORIGIN = "outside-approval"
MODEL_VALUE = re.compile(r"(?m)^Model:[ \t]*(?P<value>\S.*?)[ \t]*$")
LEGACY_REPAIR = re.compile(r"-factory-review-v\d+:(?P<head>[0-9a-f]{40}):changes-required -->")
UNTRUSTED_REVIEWERS = frozenset({"coderabbitai", "coderabbitai[bot]"})
HELD = frozenset(
    {
        "factory:building",
        "factory:ready",
        "factory:blocked",
        "factory:parked",
        "ralph-status:blocked",
    }
)
MANUAL_ONLY = re.compile(r"(?m)^[ \t]*(<!-- factory-execution:manual-only -->|manual-only)[ \t]*$")


@dataclass(frozen=True)
class Approval:
    approved: bool
    reason: str
    head: str = ""
    model: str = ""
    url: str = ""
    login: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _rows(value: object) -> list[dict[str, Any]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _paged(fetch: Fetch, path: str, limit: int = 30) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for page in range(1, limit + 1):
        batch = _rows(fetch(f"{path}?per_page=100&page={page}"))
        rows += batch
        if len(batch) < 100:
            break
    return rows


def approver_model(body: str) -> str:
    unfenced = "\n".join(line for line, fenced in verdicts.lines(body) if not fenced)
    found = MODEL_VALUE.findall(unfenced)
    return found[-1] if found else ""


def model_ids(value: str) -> list[str]:
    """The model ids a ``Model:`` value names: the whole value, its parts, and any (id)."""
    ids = [*re.findall(r"\(([^()\s]+)\)", value), *value.split()]
    return [value, *(token for token in ids if re.search(r"[a-z].*[-/]|[-/].*[a-z]", token, re.I))]


def same_model(approver: str, author: str) -> bool:
    return any(
        policy.same_actual_model(mine, theirs)
        for mine in model_ids(approver)
        for theirs in model_ids(author)
    )


def substantive(value: str) -> bool:
    return any(
        any(char.isdigit() for char in policy.canonical_model_identity(token))
        for token in model_ids(value)
    )


def latest_approval(
    reviews: Iterable[Mapping[str, Any]],
    comments: Iterable[Mapping[str, Any]],
    *,
    head: str,
    owner: str,
) -> Mapping[str, Any] | None:
    rows: list[tuple[str, Mapping[str, Any]]] = []
    for review in reviews:
        if review.get("commit_id") != head or not verdicts.owner_authored(review, owner):
            continue
        body = str(review.get("body") or "")
        formal = str(review.get("state") or "").upper() == "APPROVED" and approver_model(body)
        if formal or verdicts.verdict_of(body) == "approve":
            rows.append((str(review.get("submitted_at") or ""), review))
    for comment in comments:
        body = str(comment.get("body") or "")
        if (
            verdicts.owner_authored(comment, owner)
            and verdicts.names_head(body, head)
            and verdicts.verdict_of(body) == "approve"
        ):
            rows.append((str(comment.get("created_at") or ""), comment))
    rows = [
        (at, row)
        for at, row in rows
        if f"{policy.MARKER_PREFIX}-factory-" not in str(row.get("body") or "")
    ]
    return max(rows, key=lambda pair: pair[0])[1] if rows else None


def change_request(
    reviews: Iterable[Mapping[str, Any]],
    comments: Iterable[Mapping[str, Any]],
    *,
    head: str,
    owner: str,
) -> str:
    """Why a change request stands on ``head``, or ``""``."""
    comments = list(comments)
    for review in reviews:
        login = str((review.get("user") or {}).get("login") or "")
        if review.get("commit_id") != head or login in UNTRUSTED_REVIEWERS:
            continue
        if str(review.get("state") or "").upper() == "CHANGES_REQUESTED":
            return f"{login} requested changes on this head"
        if (
            verdicts.owner_authored(review, owner)
            and verdicts.verdict_of(str(review.get("body") or "")) == "changes-requested"
        ):
            return "an outside agent's review requested changes on this head"
    for comment in comments:
        body = str(comment.get("body") or "")
        if (
            verdicts.owner_authored(comment, owner)
            and verdicts.names_head(body, head)
            and f"{policy.MARKER_PREFIX}-factory-" not in body
            and verdicts.verdict_of(body) == "changes-requested"
        ):
            return "an outside agent's summary requested changes on this head"
    for body in policy.controller_comment_bodies(comments):
        for line in body.splitlines():
            marker = policy.REVIEW_MARKER_RE.match(line.strip())
            if marker and marker["head"] == head and marker["verdict"] == "repair":
                return "a factory reviewer requested repair on this head"
            legacy = LEGACY_REPAIR.search(line)
            if legacy and legacy["head"] == head:
                return "a factory reviewer requested changes on this head"
    return ""


def commit_models(commits: Iterable[Mapping[str, Any]]) -> list[str]:
    return [
        match["value"]
        for commit in commits
        for match in MODEL_VALUE.finditer(str((commit.get("commit") or {}).get("message") or ""))
    ]


def evaluate(fetch: Fetch, pr: int, *, owner: str) -> Approval:
    pull = fetch(f"pulls/{pr}")
    pull = pull if isinstance(pull, dict) else {}
    head = str((pull.get("head") or {}).get("sha") or "")
    if pull.get("state") != "open" or pull.get("draft") or not head:
        return Approval(False, "the PR is not an open, ready pull request", head)
    reviews = _paged(fetch, f"pulls/{pr}/reviews")
    comments = exclusion.comment_records(fetch, pr)
    found = latest_approval(reviews, comments, head=head, owner=owner)
    if found is None:
        return Approval(False, "no outside approval on the current head", head)
    body = str(found.get("body") or "")
    model = approver_model(body)
    url = str(found.get("html_url") or "")
    login = str((found.get("user") or {}).get("login") or "")
    if not model or not substantive(model):
        return Approval(False, "the approval names no model id", head, model, url, login)
    blocked = change_request(reviews, comments, head=head, owner=owner)
    if blocked:
        return Approval(False, blocked, head, model, url, login)
    authors = commit_models(_paged(fetch, f"pulls/{pr}/commits"))
    recorded = exclusion.resolve(
        fetch,
        pr=pr,
        head=head,
        base_branch=str((pull.get("base") or {}).get("ref") or ""),
        comments=policy.controller_comment_bodies(comments),
    )
    for author in [*authors, *recorded.excluded]:
        if same_model(model, author):
            return Approval(False, f"{model} authored this PR ({author})", head, model, url, login)
    return Approval(True, "outside approval at the current head", head, model, url, login)


def record(repo: str, pr: int, approval: Approval) -> bool:
    """The merge authorization event in the factory store, where one is configured."""
    try:
        import factory_turso_core as core

        if not core.enabled():
            return False
        detail = {**approval.to_dict(), "event_id": f"outside-approval:{pr}:{approval.head}"}
        core.store().batch(
            [
                core.event_insert(
                    repo, ORIGIN, "merge_authorized", detail, subject_type="pr", subject_number=pr
                )
            ]
        )
        return True
    except Exception as exc:  # noqa: BLE001 - the GitHub facts stay the gate
        print(f"outside approval: store event not recorded: {exc}", file=sys.stderr)
        return False


def authorization(
    repo: str, pr: int, *, head: str, fetch: Fetch | None = None, store: bool = True
) -> dict[str, object] | None:
    """The outside approval that authorizes merging ``head``, recorded in the store."""
    try:
        approval = evaluate(fetch or _fetch(repo), pr, owner=repo.split("/", 1)[0])
    except (RuntimeError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"outside approval for #{pr} unreadable: {exc}", file=sys.stderr)
        return None
    if not approval.approved or approval.head != head:
        return None
    if (
        store
        and not record(repo, pr, approval)
        and os.environ.get("FACTORY_STATE_REQUIRED") == "true"
    ):
        return None
    return approval.to_dict()


def _fetch(repo: str) -> Fetch:
    def read(path: str) -> object:
        proc = subprocess.run(
            ["gh", "api", f"repos/{repo}/{path}"], text=True, capture_output=True, check=True
        )
        return json.loads(proc.stdout or "null")

    return read


def promote(repo: str, pr: int, fetch: Fetch | None = None) -> dict[str, object]:
    """Move an outside-approved factory PR in review to ``factory:ready``."""
    read = fetch or _fetch(repo)
    pull = read(f"issues/{pr}")
    labels = {
        str(label.get("name"))
        for label in _rows((pull or {}).get("labels") if isinstance(pull, dict) else [])
    }
    if "factory" not in labels or labels & HELD:
        return {"pr": pr, "promoted": False, "reason": "not a factory PR waiting for review"}
    body = str(pull.get("body") or "") if isinstance(pull, dict) else ""
    if MANUAL_ONLY.search(re.sub(r"```.*?```|`[^`\n]*`", "", body, flags=re.S)):
        return {"pr": pr, "promoted": False, "reason": "the PR is manual-only"}
    approval = evaluate(read, pr, owner=repo.split("/", 1)[0])
    if not approval.approved:
        return {"pr": pr, "promoted": False, **approval.to_dict()}
    stages = {"factory:review", "factory:changes-requested", "factory:ci"}
    owners = {label for label in labels if re.fullmatch(r"factory:(\d+|local|unowned)", label)}
    keep = sorted(labels - stages - owners - {"factory:available", "factory:approved"})
    next_labels = [*keep, *(sorted(owners) or ["factory:unowned"])[:1], "factory:ready"]
    subprocess.run(
        ["gh", "api", "--method", "PUT", f"repos/{repo}/issues/{pr}/labels", "--input", "-"],
        input=json.dumps({"labels": next_labels}),
        text=True,
        capture_output=True,
        check=True,
    )
    return {"pr": pr, "promoted": True, **approval.to_dict()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("promote", "check"))
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    args = parser.parse_args(argv)
    if args.command == "promote":
        result = promote(args.repo, args.pr)
    else:
        result = evaluate(_fetch(args.repo), args.pr, owner=args.repo.split("/", 1)[0]).to_dict()
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    with __import__("contextlib").suppress(ImportError):
        __import__("factory_crash").install()
    raise SystemExit(main())

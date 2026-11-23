"""Review deadlocks the factory does not get out of by itself.

Two shapes were found by hand on ComicPile (#177):

* **Answered thread** (#5688): a reviewer bot left a thread on the current
  head, a factory worker replied "**Fixed** in <sha>", and nobody resolved the
  thread. The merge gate denies any head with an unresolved, current-head
  thread, so the PR can never pass until a person (or the reviewer, on its next
  pass) resolves it.
* **Empty repair verdict** (#5684): the PR was sent to repair on a semantic
  verdict that holds only tool output and narration, so repair workers have
  nothing to fix. The controller now refuses such verdicts, but PRs already
  sent to repair stay there.

This module only reads. Callers decide what to show or do.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import GitHub, Obj, label_names, obj, string
from rotisserie.factory_console.service import review

CHANGES_REQUESTED = "factory:changes-requested"
# The stages where either deadlock holds a PR.
WATCHED_STAGES = frozenset({"factory:review", "factory:ci", "factory:ready", CHANGES_REQUESTED})
# The merge gate ignores this retired reviewer's threads; so does this check.
IGNORED_REVIEWERS = frozenset({"coderabbitai", "coderabbitai[bot]"})
FACTORY_REPLY = re.compile(r"^\*\*Fixed\*\* in `?[0-9a-f]{7,40}`?")
SCAN_LIMIT = 40

THREADS = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        nodes {
          isResolved
          isOutdated
          path
          comments(first: 50) {
            nodes { url body author { login } commit { oid } }
          }
        }
      }
    }
  }
}
"""


@dataclass(frozen=True)
class Stall:
    pr: int
    kind: str  # answered-thread | empty-repair-verdict
    text: str
    hint: str
    url: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _login(node: object) -> str:
    author = node.get("author") if isinstance(node, dict) else None
    login = author.get("login") if isinstance(author, dict) else None
    return login if isinstance(login, str) else ""


def answered_threads(github: GitHub, number: int, head: str) -> list[Stall]:
    """Unresolved current-head threads the factory answered "Fixed" and nobody closed."""
    owner, name = github.repo.split("/", 1)
    raw = json.loads(
        github.gh(
            "api",
            "graphql",
            "-f",
            f"query={THREADS}",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            "-F",
            f"number={number}",
        )
    )
    pull = obj(obj(obj(obj(raw).get("data")).get("repository")).get("pullRequest"))
    threads = obj(pull.get("reviewThreads")).get("nodes")
    found: list[Stall] = []
    for thread in threads if isinstance(threads, list) else []:
        if not isinstance(thread, dict) or thread.get("isResolved") or thread.get("isOutdated"):
            continue
        comments = obj(thread.get("comments")).get("nodes")
        if not isinstance(comments, list) or not comments:
            continue
        reviewer = _login(comments[0])
        if reviewer in IGNORED_REVIEWERS:
            continue
        on_head = any(
            isinstance(comment, dict) and obj(comment.get("commit")).get("oid") == head
            for comment in comments
        )
        last = comments[-1] if isinstance(comments[-1], dict) else {}
        answered = _login(last).startswith("github-actions") and FACTORY_REPLY.match(
            string(last, "body")
        )
        if not (on_head and answered and len(comments) > 1):
            continue
        path = str(thread.get("path") or "a file")
        found.append(
            Stall(
                pr=number,
                kind="answered-thread",
                text=(
                    f"PR #{number}: {reviewer or 'a reviewer'}'s thread on {path} was answered "
                    "'Fixed' by the factory but is still unresolved, so the merge gate denies "
                    "this head."
                ),
                hint=(
                    "Check the fix and resolve the thread, or ask the reviewer to review again "
                    "(it resolves its own threads)."
                ),
                url=string(last, "url"),
            )
        )
    return found


def _excerpt(body: str) -> str:
    if "```text\n" not in body:
        return ""
    return body.split("```text\n", 1)[1].rsplit("\n```", 1)[0]


def empty_repair_verdict(
    records: Sequence[Obj], number: int, stage: str, *, url: str = ""
) -> Stall | None:
    """The PR is in repair on a verdict that names no finding."""
    if stage != CHANGES_REQUESTED:
        return None
    policy = runtime.module("factory_review_policy")
    trusted = set(policy.controller_comment_bodies(records))
    for record in reversed(list(records)):
        body = string(record, "body")
        if body not in trusted:
            continue
        marker = policy.parse_review_marker(body.splitlines()[0] if body else "")
        if not marker or int(marker["pr"]) != number:
            continue
        verdict = marker["verdict"]
        if verdict not in ("repair", "reject"):
            return None  # the newest verdict did not send it to repair
        if policy.has_actionable_review_findings(_excerpt(body)):
            return None
        return Stall(
            pr=number,
            kind="empty-repair-verdict",
            text=(
                f"PR #{number} is in repair on a {verdict} verdict that names no finding "
                "(only tool output and narration), so repair workers have nothing to fix."
            ),
            hint=f"Review it again: rotisserie factory run --target pr:{number} --action review",
            url=string(record, "html_url") or url,
        )
    return None


def inspect(
    github: GitHub,
    number: int,
    *,
    pull: Obj | None = None,
    records: Sequence[Obj] | None = None,
) -> list[Stall]:
    """Every known deadlock holding one PR."""
    raw = pull if pull is not None else obj(github.repo_api(f"pulls/{number}"))
    labels = label_names(raw)
    stage = next((label for label in sorted(labels) if label in WATCHED_STAGES), "")
    if not stage:
        return []
    head = string(obj(raw.get("head")), "sha")
    found = answered_threads(github, number, head) if head else []
    if stage == CHANGES_REQUESTED:
        if records is None:
            records = review.comment_records(github, number)
        stall = empty_repair_verdict(records, number, stage)
        if stall is not None:
            found.append(stall)
    return found


def scan(github: GitHub, pulls: Sequence[Obj]) -> list[Stall]:
    """Deadlocks across open factory PRs (at most ``SCAN_LIMIT`` of them)."""
    watched = [
        row for row in pulls if "factory" in label_names(row) and label_names(row) & WATCHED_STAGES
    ][:SCAN_LIMIT]
    found: list[Stall] = []
    for row in watched:
        number = row.get("number")
        if isinstance(number, int):
            found += inspect(github, number, pull=row)
    return found

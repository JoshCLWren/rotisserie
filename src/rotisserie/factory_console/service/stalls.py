"""Review deadlocks, and which of them the factory clears by itself.

Two shapes were found by hand on an adopter (#177):

* **Answered thread** (#5688): a reviewer bot left a thread on the current
  head, a factory worker replied "**Fixed** in <sha>", and nobody resolved the
  thread. The merge gate denies any head with an unresolved, current-head
  thread. The factory's self-heal sweep now resolves such a thread when a bot
  started it, no person took part, and the fix is in the head.
* **Empty repair verdict** (#5684): the PR was sent to repair on a semantic
  verdict that holds only tool output and narration, so repair workers have
  nothing to fix. The self-heal sweep now moves it back to review once.

The rules live in ``factory_self_heal_rules`` so this check and the factory's
action always agree. This module only reads. Callers decide what to show or do.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from types import ModuleType

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import GitHub, Obj, label_names, obj, string
from rotisserie.factory_console.service import review

CHANGES_REQUESTED = "factory:changes-requested"
# The stages where either deadlock holds a PR.
WATCHED_STAGES = frozenset({"factory:review", "factory:ci", "factory:ready", CHANGES_REQUESTED})
SELF_HEAL = "The factory clears this by itself on its next dispatcher pass; `baste heal` shows it."

THREADS = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        pageInfo { hasNextPage }
        nodes {
          id
          isResolved
          isOutdated
          path
          comments(first: 100) {
            pageInfo { hasNextPage }
            nodes { url body author { login __typename } commit { oid } }
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
    auto: bool = False  # the factory's self-heal sweep clears it

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


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
    listing = obj(pull.get("reviewThreads"))
    threads = listing.get("nodes")
    found: list[Stall] = []
    if obj(listing.get("pageInfo")).get("hasNextPage"):
        found.append(_unchecked(number, "it has more than 100 review threads"))
    for thread in threads if isinstance(threads, list) else []:
        if not isinstance(thread, dict) or thread.get("isResolved") or thread.get("isOutdated"):
            continue
        page = obj(thread.get("comments"))
        if obj(page.get("pageInfo")).get("hasNextPage"):
            # The newest reply is beyond this page: never guess who spoke last.
            where = str(thread.get("path") or "a file")
            found.append(_unchecked(number, f"its thread on {where} has more than 100 comments"))
            continue
        answered = _rules().answered_thread(thread, head)
        if answered is None:
            continue
        who = answered.reviewer or "a reviewer"
        text = (
            f"PR #{number}: {who}'s thread on {answered.path} was answered 'Fixed' by the "
            "factory but is still unresolved, so the merge gate denies this head."
        )
        if answered.resolvable:
            hint = f"{SELF_HEAL} It resolves the thread once {answered.fix[:12]} is in the head."
        else:
            hint = (
                "A person started or joined this thread, so the factory leaves it to you: check "
                "the fix and resolve the thread, or ask the reviewer to review again."
            )
        found.append(
            Stall(number, "answered-thread", text, hint, answered.url, answered.resolvable)
        )
    return found


def _unchecked(number: int, why: str) -> Stall:
    return Stall(
        pr=number,
        kind="unchecked-threads",
        text=f"PR #{number} could not be fully checked for answered threads: {why}.",
        hint="Look at its unresolved threads on GitHub.",
        url="",
    )


def _review_again(number: int) -> str:
    command = f"baste run PROVIDER auto --force --target pr:{number} --action review"
    choose = "pick PROVIDER from 'baste stats'"
    return f"Review it again: {command} ({choose}; add --take-over if a worker holds it)"


def _rules() -> ModuleType:
    return runtime.module("factory_self_heal_rules")


def empty_repair_verdict(
    records: Sequence[Obj], number: int, stage: str, *, url: str = ""
) -> Stall | None:
    """The PR is in repair on a verdict that names no finding, and was not healed yet."""
    if stage != CHANGES_REQUESTED:
        return None
    found = _rules().empty_verdict(records, number)
    if found is None:
        return None
    text = (
        f"PR #{number} is in repair on a {found.verdict} verdict that names no finding "
        "(only tool output and narration), so repair workers have nothing to fix."
    )
    if found.healed:
        text += " The factory already sent it back to review once for this verdict."
    hint = _review_again(number) if found.healed else f"{SELF_HEAL} Or now: {_review_again(number)}"
    return Stall(number, "empty-repair-verdict", text, hint, found.url or url, not found.healed)


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
    """Deadlocks across every open factory PR in a watched stage."""
    watched = [
        row for row in pulls if "factory" in label_names(row) and label_names(row) & WATCHED_STAGES
    ]
    found: list[Stall] = []
    for row in watched:
        number = row.get("number")
        if isinstance(number, int):
            found += inspect(github, number, pull=row)
    return found

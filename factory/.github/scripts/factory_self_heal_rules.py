#!/usr/bin/env python3
"""Pure rules for the stalls the factory clears by itself.

The trusted sweep (``factory_self_heal.py``) acts on these rules and the
operator console (``baste review``, ``baste recap``, ``baste heal``) reads them,
so both always agree about what is stuck and what the factory already did.

* **Empty repair verdict**: the newest trusted review verdict sent the PR to
  repair but names no finding, so repair workers have nothing to fix.
* **Answered bot thread**: a reviewer bot's thread on the current head that a
  factory worker answered "**Fixed** in <sha>" and nobody resolved.

Every self-heal action leaves one trusted comment whose first line is a marker
(kind, PR, head, reference, avoided model). Nothing here reads the network.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import factory_review_policy as policy
from factory_adopter import MARKER_PREFIX

EMPTY_VERDICT = "empty-verdict"
CONFLICT_SYNC = "conflict-sync"
THREAD_RESOLVED = "thread-resolved"
LOOP_PARKED = "loop-parked"
LOOP_RELEASED = "loop-released"
PARK_RETRY = "park-retry"
PARK_CLOSED = "park-closed"
DUPLICATE_CLOSED = "duplicate-closed"
KINDS = (
    EMPTY_VERDICT, CONFLICT_SYNC, THREAD_RESOLVED, LOOP_PARKED, LOOP_RELEASED, PARK_RETRY,
    PARK_CLOSED, DUPLICATE_CLOSED,
)
CHANGES_REQUESTED = "factory:changes-requested"
VERDICT_HEADER = "### Factory semantic review"
# A gate escalation (factory_acceptance_escalation) is a repair handoff with findings.
ACCEPTANCE_ESCALATION = "-factory-acceptance-rejected-v1:"
HEADLINE = "**Factory self-heal:**"
# The merge gate ignores this retired reviewer's threads; so do these rules.
IGNORED_REVIEWERS = frozenset({"coderabbitai", "coderabbitai[bot]"})
FIXED_REPLY = re.compile(r"^\*\*Fixed\*\*(?: for [^\n]*?)? in `?(?P<sha>[0-9a-f]{7,40})`?")
_TOKEN = r"[A-Za-z0-9][A-Za-z0-9._/+:-]{0,200}"
MARKER_RE = re.compile(
    rf"^<!-- {MARKER_PREFIX}-factory-self-heal-v1:kind-(?P<kind>[a-z-]+):pr-(?P<pr>\d+):"
    rf"head-(?P<head>[0-9a-f]{{40}}|none):ref-(?P<ref>[A-Za-z0-9_-]{{1,120}}):"
    rf"avoid-(?P<avoid>{_TOKEN}) -->$"
)
OUTCOME_MARKER = f"{MARKER_PREFIX}-factory-greptile-outcome-v1"
_HANDOFF_MODEL = re.compile(r"(?m)^.*\bModel: (?P<model>\S+)\s*$")


def valid_token(value: str) -> bool:
    return bool(value) and re.fullmatch(_TOKEN, value) is not None


def marker(*, kind: str, pr: int, head: str, ref: str, avoid: str = "") -> str:
    """The first line of a self-heal comment."""
    if kind not in KINDS:
        raise ValueError(f"unknown self-heal kind: {kind}")
    avoid_token = avoid if valid_token(avoid) else "none"
    line = (
        f"<!-- {MARKER_PREFIX}-factory-self-heal-v1:kind-{kind}:pr-{pr}:"
        f"head-{head or 'none'}:ref-{ref}:avoid-{avoid_token} -->"
    )
    if not MARKER_RE.fullmatch(line):
        raise ValueError("self-heal marker fields are invalid")
    return line


def comment_body(*, kind: str, pr: int, head: str, ref: str, text: str, avoid: str = "") -> str:
    head_line = marker(kind=kind, pr=pr, head=head, ref=ref, avoid=avoid)
    return f"{head_line}\n\n{HEADLINE} {text.strip()}\n"


def parse_marker(body: str) -> dict[str, str] | None:
    first = body.splitlines()[0].strip() if body else ""
    match = MARKER_RE.fullmatch(first)
    if not match:
        return None
    found = match.groupdict()
    return found if found["kind"] in KINDS else None


def heal_text(body: str) -> str:
    """The plain sentence after the headline of a self-heal comment."""
    return body.split(HEADLINE, 1)[1].strip().split("\n", 1)[0] if HEADLINE in body else ""


def heals(records: Iterable[Mapping[str, Any]], *, pr: int) -> list[dict[str, str]]:
    """Trusted self-heal markers on one PR, oldest first, each with its text."""
    found: list[dict[str, str]] = []
    for body in policy.controller_comment_bodies(records):
        parsed = parse_marker(body)
        if parsed and int(parsed["pr"]) == pr:
            found.append({**parsed, "text": heal_text(body)})
    return found


def avoided_reviewers(records: Iterable[Mapping[str, Any]], *, pr: int) -> list[str]:
    """Models a self-heal asked the next review to avoid, because they wrote an empty verdict."""
    return sorted(
        {
            heal["avoid"]
            for heal in heals(records, pr=pr)
            if heal["kind"] == EMPTY_VERDICT and heal["avoid"] != "none"
        }
    )


@dataclass(frozen=True)
class EmptyVerdict:
    pr: int
    verdict: str
    head: str
    reviewer: str
    ref: str  # the verdict comment's id; empty when the read did not carry it
    url: str
    model: str  # the model that wrote it, "" when no record names it
    healed: bool = False  # a self-heal already moved it to review once; never again


def _excerpt(body: str) -> str:
    if "```text\n" not in body:
        return ""
    return body.split("```text\n", 1)[1].rsplit("\n```", 1)[0]


def _reviewer_model(records: Sequence[Mapping[str, Any]], reviewer: str, head: str) -> str:
    """The model named by the readable review handoff for this verdict.

    Only used to steer the next review away from that model, never as trust
    evidence: no trusted record names the model behind a repair verdict.
    """
    headline = f"Factory worker {reviewer} finished a semantic review at `{head[:12]}`."
    for record in reversed(list(records)):
        user = record.get("user")
        if not isinstance(user, Mapping) or user.get("type") != "Bot":
            continue
        body = str(record.get("body") or "")
        if headline not in body:
            continue
        match = _HANDOFF_MODEL.search(body)
        if match:
            return match.group("model")
    return ""


def empty_verdict(records: Sequence[Mapping[str, Any]], number: int) -> EmptyVerdict | None:
    """The newest trusted verdict, when it sent the PR to repair with no finding.

    A newer marker-less controller verdict (a CI or gate repair handoff, which
    always carries findings) means there is nothing left to clear. ``healed``
    says a self-heal already acted on this verdict, so only a person can now.
    """
    rows = list(records)
    trusted = set(policy.controller_comment_bodies(rows))
    healed = {heal["ref"] for heal in heals(rows, pr=number) if heal["kind"] == EMPTY_VERDICT}
    for record in reversed(rows):
        body = str(record.get("body") or "")
        if body not in trusted:
            continue
        first = body.splitlines()[0] if body else ""
        found = policy.parse_review_marker(first)
        if not found:
            if first.startswith(VERDICT_HEADER) or ACCEPTANCE_ESCALATION in first:
                return None
            continue
        if int(found["pr"]) != number:
            continue
        verdict = found["verdict"]
        if verdict not in ("repair", "reject"):
            return None
        if policy.has_actionable_review_findings(_excerpt(body)):
            return None
        ref = str(record.get("id") or "")
        return EmptyVerdict(
            pr=number,
            verdict=verdict,
            head=found["head"],
            reviewer=found["reviewer"],
            ref=ref,
            url=str(record.get("html_url") or ""),
            model=_reviewer_model(rows, found["reviewer"], found["head"]),
            healed=bool(ref) and ref in healed,
        )
    return None


@dataclass(frozen=True)
class AnsweredThread:
    thread_id: str
    path: str
    reviewer: str
    reviewer_is_bot: bool
    people: tuple[str, ...]  # human accounts that commented in the thread
    fix: str  # the commit the factory's reply names
    url: str
    marked: bool = False  # the reply carries the wrapper's outcome marker for this thread

    @property
    def resolvable(self) -> bool:
        """The factory may resolve it: a bot started it, no person took part, and the
        reply is the wrapper's validated outcome for this very thread."""
        return self.reviewer_is_bot and not self.people and self.marked


def _author(node: object) -> tuple[str, bool]:
    author = node.get("author") if isinstance(node, Mapping) else None
    if not isinstance(author, Mapping):
        return "", False
    login = author.get("login")
    login = login if isinstance(login, str) else ""
    return login, author.get("__typename") == "Bot" or login.endswith("[bot]")


def answered_thread(thread: Mapping[str, Any], head: str) -> AnsweredThread | None:
    """An unresolved current-head thread whose last word is the factory's "Fixed" reply.

    Callers must check comment pagination first: a thread with more comments
    than one page could have a newer reply.
    """
    if thread.get("isResolved") or thread.get("isOutdated"):
        return None
    page = thread.get("comments")
    comments = page.get("nodes") if isinstance(page, Mapping) else None
    if not isinstance(comments, list) or len(comments) < 2:
        return None
    rows = [comment for comment in comments if isinstance(comment, Mapping)]
    if len(rows) != len(comments):
        return None
    reviewer, reviewer_is_bot = _author(rows[0])
    if reviewer in IGNORED_REVIEWERS:
        return None
    on_head = any(
        isinstance(row.get("commit"), Mapping) and row["commit"].get("oid") == head for row in rows
    )
    last_login, _ = _author(rows[-1])
    reply = FIXED_REPLY.match(str(rows[-1].get("body") or ""))
    if not (on_head and last_login.startswith("github-actions") and reply):
        return None
    people = tuple(sorted({login or "ghost" for login, bot in map(_author, rows) if not bot}))
    return AnsweredThread(
        thread_id=str(thread.get("id") or ""),
        path=str(thread.get("path") or "a file"),
        reviewer=reviewer,
        reviewer_is_bot=reviewer_is_bot,
        people=people,
        fix=reply.group("sha"),
        url=str(rows[-1].get("url") or ""),
        marked=f"<!-- {OUTCOME_MARKER}:{thread.get('id')}:fixed:" in str(rows[-1].get("body") or ""),
    )

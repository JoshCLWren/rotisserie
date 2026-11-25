#!/usr/bin/env python3
"""The owner's one-head exception to the head-author review rule.

A person adds ``factory:allow-self-review`` to one pull request. While the label
stays on, the model that authored the current head may review and approve it.
Every other gate (exact head, CI, acceptance, main health) still applies.

Fail closed: the most recent ``labeled`` event for the label must come from a
human GitHub account, not a bot or GitHub Actions. The exception binds to one
head. A trusted controller marker names the head it was granted on; before the
marker exists, a head committed after the label was added, or a force-push after
it, is not covered. The drain sweep and the PR reconciler remove the label once
the PR closes or its head changes.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

LABEL = "factory:allow-self-review"
NOTE = "self-review allowed by owner"
BOT_LOGINS = frozenset({"github-actions", "github-actions[bot]", "ghost"})
HEAD_MOVES = frozenset({"head_ref_force_pushed", "head_ref_restored", "head_ref_deleted"})
MAX_EVENT_PAGES = 30
HEAD_RE = re.compile(r"^[0-9a-f]{40}$")
MARKER_RE = re.compile(
    r"<!-- factory-self-review-allowed pr=(?P<pr>\d+) event=(?P<event>\d+)"
    r" head=(?P<head>[0-9a-f]{40}) actor=(?P<actor>[A-Za-z0-9-]+) -->"
)
ORIGIN = "self-review-allowance"
MOVED_AFTER = "the head changed after the label was added"
EARLIER_HEAD = "the label was granted on an earlier head"
NEWER_HEAD = "the head is newer than the label"
HEAD_CHANGED = frozenset({MOVED_AFTER, EARLIER_HEAD, NEWER_HEAD})

Fetch = Callable[[str], object]


def _obj(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class Allowance:
    allowed: bool
    reason: str
    actor: str = ""
    event_id: int = 0
    labeled_at: str = ""

    @property
    def note(self) -> str:
        return f"{NOTE} (label by {self.actor})"


NONE = Allowance(False, "no allow-self-review label")


def label_names(pull: Mapping[str, Any]) -> set[str]:
    names: set[str] = set()
    for label in pull.get("labels") or []:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str):
            names.add(name)
    return names


def human_actor(event: Mapping[str, Any]) -> str:
    """The actor's login when a person performed ``event``, else ``""``."""
    actor = _obj(event.get("actor"))
    login = str(actor.get("login") or "")
    if not login or actor.get("type") != "User" or event.get("performed_via_github_app"):
        return ""
    if login.lower() in BOT_LOGINS or login.lower().endswith("[bot]"):
        return ""
    return login


def issue_events(fetch: Fetch, pr: int) -> list[dict[str, Any]] | None:
    """Every issue event on the PR in order, or ``None`` when any page is unreadable."""
    rows: list[dict[str, Any]] = []
    for page in range(1, MAX_EVENT_PAGES + 1):
        batch = fetch(f"issues/{pr}/events?per_page=100&page={page}")
        if not isinstance(batch, list):
            return None
        rows += [_obj(row) for row in batch]
        if len(batch) < 100:
            return sorted(rows, key=lambda row: int(row.get("id") or 0))
    return None


def markers(comments: Iterable[str], pr: int) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for body in comments:
        for match in MARKER_RE.finditer(str(body or "")):
            if int(match["pr"]) == pr:
                found.append(match.groupdict())
    return found


def check(
    fetch: Fetch,
    pr: int,
    *,
    pull: Mapping[str, Any],
    head: str,
    comments: Iterable[str] = (),
) -> Allowance:
    """Whether the owner's exception covers ``head`` now. Unlabelled PRs cost no reads."""
    if LABEL not in label_names(pull):
        return NONE
    if str(pull.get("state") or "").lower() != "open":
        return Allowance(False, "the pull request is not open")
    if not HEAD_RE.fullmatch(head):
        return Allowance(False, "the current head is unknown")
    try:
        events = issue_events(fetch, pr)
    except RuntimeError, OSError, ValueError:
        events = None
    if events is None:
        return Allowance(False, "the label's events could not be read")
    labeled = [
        index for index, row in enumerate(events)
        if row.get("event") == "labeled" and _obj(row.get("label")).get("name") == LABEL
    ]  # fmt: skip
    if not labeled:
        return Allowance(False, "no labeled event was found for the label")
    last = events[labeled[-1]]
    actor = human_actor(last)
    event_id = int(last.get("id") or 0)
    labeled_at = str(last.get("created_at") or "")
    if not actor or not event_id or not labeled_at:
        who = str(_obj(last.get("actor")).get("login") or "unknown")
        return Allowance(False, f"the label was added by {who}, not a person")
    if any(row.get("event") in HEAD_MOVES for row in events[labeled[-1] + 1 :]):
        return Allowance(False, MOVED_AFTER, actor, event_id)
    bound = {row["head"] for row in markers(comments, pr) if int(row["event"]) == event_id}
    if bound and head not in bound:
        return Allowance(False, EARLIER_HEAD, actor, event_id)
    if not bound:
        try:
            commit = _obj(_obj(fetch(f"commits/{head}")).get("commit"))
        except RuntimeError, OSError, ValueError:
            commit = {}
        committed = str(_obj(commit.get("committer")).get("date") or "")
        if not committed:
            return Allowance(False, "the head commit could not be read", actor, event_id)
        if committed > labeled_at:
            return Allowance(False, NEWER_HEAD, actor, event_id)
    return Allowance(True, NOTE, actor, event_id, labeled_at)


def noted(verdict: Any, allowed_by: str) -> Any:
    """Add the owner's exception to an eligible verdict's reason."""
    if not allowed_by or not getattr(verdict, "eligible", False):
        return verdict
    return replace(
        verdict, reason=f"{verdict.reason} Self-review allowed by owner (label by {allowed_by})."
    )


def announcement(pr: int, head: str, allowance: Allowance) -> str:
    return (
        f"Owner allowed self-review on this PR (label added by {allowance.actor}).\n\n"
        f"It covers head `{head[:10]}` only. Every other merge gate still applies.\n\n"
        f"<!-- factory-self-review-allowed pr={pr} event={allowance.event_id} head={head}"
        f" actor={allowance.actor} -->"
    )


def record_event(repo: str, pr: int, event_type: str, detail: Mapping[str, Any]) -> bool:
    """Best effort: a store event where the factory store is configured."""
    try:
        import factory_turso_core as core

        if not core.enabled():
            return False
        entry = {**detail, "event_id": f"{event_type}:{pr}:{detail.get('grant', '')}"}
        core.store().batch(
            [
                core.event_insert(
                    repo, ORIGIN, event_type, entry, subject_type="pr", subject_number=pr
                )
            ]
        )
        return True
    except Exception as exc:  # noqa: BLE001 - the store event never gates anything
        print(f"self-review allowance: store event not recorded: {exc}", file=sys.stderr)
        return False


def _gh(*args: str) -> str:
    proc = subprocess.run(["gh", *args], text=True, capture_output=True, check=False, timeout=60)
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or f"gh {args[0]} failed")
    return proc.stdout


def _fetch(repo: str) -> Fetch:
    def fetch(path: str) -> object:
        return json.loads(_gh("api", f"repos/{repo}/{path}") or "null")

    return fetch


def _controller_bodies(fetch: Fetch, pr: int) -> list[str]:
    bodies: list[str] = []
    for page in range(1, 31):
        batch = fetch(f"issues/{pr}/comments?per_page=100&page={page}")
        rows = batch if isinstance(batch, list) else []
        bodies += [
            str(_obj(row).get("body") or "") for row in rows
            if _obj(_obj(row).get("user")).get("login") == "github-actions[bot]"
        ]  # fmt: skip
        if len(rows) < 100:
            break
    return bodies


def settle(
    repo: str, pr: int, *, fetch: Fetch, gh: Callable[..., str] = _gh, action: str = ""
) -> str:
    """Announce a new grant once, or drop the label once the PR closes or the head moves."""
    pull = _obj(fetch(f"pulls/{pr}"))
    if LABEL not in label_names(pull):
        return "no label"
    head = str(_obj(pull.get("head")).get("sha") or "")
    comments = _controller_bodies(fetch, pr)
    allowance = check(fetch, pr, pull=pull, head=head, comments=comments)
    closed = str(pull.get("state") or "").lower() != "open"
    granted = {
        row["head"] for row in markers(comments, pr) if int(row["event"]) == allowance.event_id
    }
    moved = action == "synchronize" or (granted and head not in granted)
    if closed or moved or allowance.reason in HEAD_CHANGED:
        why = "the pull request closed" if closed else "the head changed"
        gh("api", "--method", "DELETE", f"repos/{repo}/issues/{pr}/labels/{LABEL}")
        record_event(
            repo,
            pr,
            "self_review_allowance_dropped",
            {"grant": allowance.event_id, "why": why, "head": head},
        )
        return f"dropped: {why}"
    if not allowance.allowed:
        return f"not allowed: {allowance.reason}"
    if head in granted:
        return "already announced"
    gh("pr", "comment", str(pr), "--repo", repo, "--body", announcement(pr, head, allowance))
    record_event(
        repo, pr, "self_review_allowed",
        {"grant": allowance.event_id, "head": head, "actor": allowance.actor, "label": LABEL},
    )  # fmt: skip
    return f"announced: label by {allowance.actor} on {head[:10]}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", type=int, action="append", default=[])
    parser.add_argument("--action", default="", help="the pull_request event action, if any")
    args = parser.parse_args(argv)
    fetch = _fetch(args.repo)
    numbers = list(args.pr)
    if not numbers:
        rows = fetch(f"issues?labels={LABEL}&state=all&sort=updated&per_page=50")
        listed = [_obj(row) for row in rows] if isinstance(rows, list) else []
        numbers = [int(row["number"]) for row in listed if row.get("pull_request")]
    status = 0
    for pr in numbers:
        try:
            print(f"PR #{pr}: {settle(args.repo, pr, fetch=fetch, action=args.action)}")
        except (RuntimeError, OSError, ValueError) as exc:
            print(f"PR #{pr}: self-review allowance not settled: {exc}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    with __import__("contextlib").suppress(ImportError):
        __import__("factory_crash").install()
    raise SystemExit(main())

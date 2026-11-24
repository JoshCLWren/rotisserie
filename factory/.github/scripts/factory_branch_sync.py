#!/usr/bin/env python3
"""Exclusive, compare-and-swap branch synchronization for factory pull requests.

One implementation serves every front end: the trusted scheduler runs it with
store leases through ``main()``; the operator console loads the same module and
supplies its own GitHub reader. A sync:

- leaves closed pull requests and heads that already contain the target alone;
- never runs while a worker holds the pull request. Store leases are
  authoritative; owner labels are consulted only when the factory store is not configured,
  and the result says so;
- holds the target exclusively (a short the factory store operation lease) while it writes,
  so no worker can be assigned the pull request mid-sync;
- asks GitHub to merge the base branch in with ``expected_head_sha``, so a
  worker's newer push is never overwritten. It never force-pushes and never
  pushes to the base branch;
- reports a conflict instead of retrying: GitHub then marks the pull request
  conflicting and the scheduler routes it to merge-conflict work.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Protocol

SYNCED = "synced"
HELD = "held-by-worker"
CURRENT = "already-current"
CONFLICTS = "conflicts"
HEAD_MOVED = "head-moved"
CLOSED = "closed"
ERROR = "error"
STATUSES = (SYNCED, HELD, CURRENT, CONFLICTS, HEAD_MOVED, CLOSED, ERROR)

HEAD_RE = re.compile(r"^[0-9a-f]{40}$")
OWNER_LABEL_RE = re.compile(r"^factory:(?:[0-9]+|local)$")
# A request for the trusted scheduler to sync a PR under a store lease. Not a
# ``factory:`` label, so no owner/stage parser ever reads it as ownership.
QUEUE_LABEL = "factory-sync-main"
# Results that answer a queued request; anything else keeps it queued.
FINAL = frozenset({SYNCED, CURRENT, CONFLICTS, CLOSED})


class HubError(RuntimeError):
    """A GitHub read or write failed; ``kind`` is conflict, head-moved or other."""

    def __init__(self, message: str, kind: str = "other") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class PullView:
    number: int
    state: str  # open | closed
    head: str
    labels: frozenset[str]
    base: str
    merge_state: str = ""  # GitHub's mergeable state when the snapshot had it ("" unknown)

    @property
    def conflicted(self) -> bool:
        return self.merge_state.lower() == "dirty"


# Open pull requests by number, read once and shared by every step of a pass.
# A pull request missing from a snapshot is not open.
Snapshot = Mapping[int, PullView]


class Hub(Protocol):
    """The GitHub operations a sync needs. ``pull`` is only used without a snapshot."""

    def pull(self, number: int) -> PullView: ...

    def contains(self, head: str, target: str) -> bool:
        """Whether ``head`` already contains commit or branch ``target``."""
        ...

    def update_branch(self, number: int, expected_head: str) -> None: ...


class Leases(Protocol):
    """Authoritative target ownership. ``consulted`` is False without the factory store."""

    consulted: bool

    def holder(self, number: int) -> str | None:
        """Who holds the target now, in words, or ``None`` when it is free."""
        ...

    def acquire(self, number: int) -> str | None:
        """A token for exclusive use of the target, or ``None`` when it is taken."""
        ...

    def release(self, number: int, token: str) -> None: ...


@dataclass(frozen=True)
class Request:
    pr: int
    head: str = ""  # the head the plan saw; empty means "whatever is current"
    target: str = ""  # commit or branch the head must contain; empty = the base branch


@dataclass(frozen=True)
class Outcome:
    pr: int
    status: str
    text: str
    head: str = ""
    neon: bool = True  # whether store leases were consulted

    @property
    def ok(self) -> bool:
        return self.status == SYNCED

    def to_dict(self) -> dict[str, object]:
        return {**asdict(self), "ok": self.ok}


class LabelLeases:
    """Fallback when the factory store is not configured: owner labels are the only evidence.

    It cannot hold the target, so a worker assigned between the read and the
    update is possible; the compare-and-swap still protects any push it makes.
    """

    consulted = False

    def __init__(self, hub: Hub, snapshot: Snapshot | None = None) -> None:
        self._hub = hub
        self._seen: dict[int, frozenset[str]] = {
            number: view.labels for number, view in (snapshot or {}).items()
        }

    def remember(self, view: PullView) -> None:
        self._seen[view.number] = view.labels

    def holder(self, number: int) -> str | None:
        labels = self._seen.get(number)
        if labels is None:
            labels = self._hub.pull(number).labels
        owner = sorted(label for label in labels if OWNER_LABEL_RE.fullmatch(label))
        return f"owner label {owner[0]}" if owner else None

    def acquire(self, number: int) -> str | None:
        return ""

    def release(self, number: int, token: str) -> None:
        return None


def sync_pull(
    hub: Hub, leases: Leases, request: Request, *, snapshot: Snapshot | None = None
) -> Outcome:
    """Merge the base branch into one pull request, exclusively and only if safe.

    With a ``snapshot`` the pull request is not re-read: the cost is one compare
    and, when it is behind, one update-branch. A stale snapshot is safe: the
    update is a compare-and-swap on the head the snapshot saw.
    """
    pr = request.pr
    neon = leases.consulted
    note = "" if neon else " (store leases were not consulted; owner labels were)"

    def done(status: str, text: str, head: str = "") -> Outcome:
        return Outcome(pr, status, f"PR #{pr}: {text}{note}", head, neon)

    if snapshot is not None:
        found = snapshot.get(pr)
        if found is None:
            return done(CLOSED, "no longer open; nothing to sync")
        view = found
    else:
        try:
            view = hub.pull(pr)
        except HubError as exc:
            return done(ERROR, f"could not read it: {exc}")
    if isinstance(leases, LabelLeases):
        leases.remember(view)
    if view.state != "open":
        return done(CLOSED, "no longer open; nothing to sync", view.head)
    if request.head and view.head != request.head:
        return done(
            HEAD_MOVED,
            f"its head moved to {view.head[:12]} since the plan saw {request.head[:12]}; "
            "left alone (re-plan to sync the new head)",
            view.head,
        )
    if view.conflicted:
        return done(
            CONFLICTS,
            f"conflicts with {view.base}, so it needs merge-conflict work, not a sync",
            view.head,
        )
    target = request.target or view.base
    if not target or not view.head:
        return done(ERROR, "its head or base branch is unknown; not synced", view.head)
    try:
        if hub.contains(view.head, target):
            return done(CURRENT, f"already contains {_short(target)}; nothing to do", view.head)
        holder = leases.holder(pr)
    except (HubError, RuntimeError) as exc:
        return done(ERROR, f"could not check it: {exc}", view.head)
    if holder:
        return done(HELD, f"{holder} holds it; left alone until it is released", view.head)
    try:
        token = leases.acquire(pr)
    except (RuntimeError, ValueError, OSError) as exc:
        return done(ERROR, f"could not take the sync lease: {exc}", view.head)
    if token is None:
        return done(HELD, "a worker or another sync took it first; left alone", view.head)
    try:
        hub.update_branch(pr, view.head)
    except HubError as exc:
        if exc.kind == "conflict":
            return done(
                CONFLICTS,
                f"conflicts with {view.base}, so it needs merge-conflict work, not a sync; "
                "the scheduler routes it there",
                view.head,
            )
        if exc.kind == "head-moved":
            return done(HEAD_MOVED, "a push landed while syncing; left alone", view.head)
        return done(ERROR, f"not synced: {exc}", view.head)
    finally:
        try:
            leases.release(pr, token)
        except (RuntimeError, OSError):
            pass  # the short TTL frees it; never fail a finished sync over cleanup
    return done(SYNCED, f"merged {view.base} into it; its checks run again", view.head)


def run(
    hub: Hub,
    leases: Leases,
    requests: Iterable[Request],
    *,
    limit: int | None = None,
    snapshot: Snapshot | None = None,
) -> list[Outcome]:
    """Sync each request in order, at most ``limit`` writes (reads are not counted)."""
    outcomes: list[Outcome] = []
    writes = 0
    for request in requests:
        if limit is not None and writes >= limit:
            break
        outcome = sync_pull(hub, leases, request, snapshot=snapshot)
        outcomes.append(outcome)
        if outcome.status in (SYNCED, CONFLICTS, HEAD_MOVED):
            writes += 1
    return outcomes


def _short(ref: str) -> str:
    return ref[:12] if HEAD_RE.fullmatch(ref) else ref


def classify_update_error(message: str) -> str:
    """GitHub's update-branch refusal, as a HubError kind."""
    lowered = message.lower()
    if "merge conflict" in lowered:
        return "conflict"
    if "expected head sha" in lowered or ("head sha" in lowered and "match" in lowered):
        return "head-moved"
    return "other"


# --- Trusted runtime adapters (gh CLI + store ledger) -----------------------------


class GhHub:
    """Hub over the ``gh`` CLI, as every factory script reaches GitHub."""

    def __init__(self, repo: str) -> None:
        self.repo = repo

    def _api(self, path: str, *args: str) -> object:
        proc = subprocess.run(
            ["gh", "api", f"repos/{self.repo}/{path}", *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode:
            message = (proc.stderr or proc.stdout).strip() or f"gh api {path} failed"
            raise HubError(message, classify_update_error(message))
        return json.loads(proc.stdout or "null")

    def pull(self, number: int) -> PullView:
        data = self._api(f"pulls/{number}")
        if not isinstance(data, Mapping):
            raise HubError(f"pull {number} is not an object")
        head = data.get("head")
        base = data.get("base")
        labels = data.get("labels")
        return PullView(
            number,
            str(data.get("state") or ""),
            str(head.get("sha") or "") if isinstance(head, Mapping) else "",
            frozenset(
                str(label.get("name") or "")
                for label in (labels if isinstance(labels, list) else [])
                if isinstance(label, Mapping)
            ),
            str(base.get("ref") or "") if isinstance(base, Mapping) else "",
        )

    def contains(self, head: str, target: str) -> bool:
        data = self._api(f"compare/{target}...{head}")
        status = data.get("status") if isinstance(data, Mapping) else None
        return status in ("ahead", "identical")

    def update_branch(self, number: int, expected_head: str) -> None:
        self._api(
            f"pulls/{number}/update-branch",
            "--method",
            "PUT",
            "-f",
            f"expected_head_sha={expected_head}",
        )

    def open_pulls(self) -> dict[int, PullView]:
        """Every open pull request: one paginated list call (100 per page)."""
        proc = subprocess.run(
            [
                "gh", "api", "--paginate",
                f"repos/{self.repo}/pulls?state=open&per_page=100",
                "--jq",
                ".[] | {number, state, head: .head.sha, base: .base.ref, "
                "labels: [.labels[].name]}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode:
            raise HubError((proc.stderr or proc.stdout).strip() or "open pull list failed")
        return snapshot_from_rows(
            json.loads(line) for line in proc.stdout.splitlines() if line.strip()
        )

    def dequeue(self, number: int) -> None:
        try:
            self._api(f"issues/{number}/labels/{QUEUE_LABEL}", "--method", "DELETE")
        except HubError as exc:
            if "Label does not exist" not in str(exc) and "404" not in str(exc):
                raise


class StoreLeases:
    """Factory store target leases through ``factory_state_ledger`` (trusted runtime only)."""

    consulted = True

    def __init__(self, repo: str, holder: str, ledger: object | None = None) -> None:
        if ledger is None:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import factory_state_ledger as ledger  # type: ignore[no-redef]
        self._ledger = ledger
        self.repo = repo
        self.holder_id = holder

    def holder(self, number: int) -> str | None:
        row = self._ledger.target_lease(self.repo, number)  # type: ignore[attr-defined]
        if not row:
            return None
        if row.get("worker"):
            return f"worker {row['worker']}"
        return f"operation {row.get('operation') or 'unknown'}"

    def acquire(self, number: int) -> str | None:
        token = self._ledger.acquire_operation_lease(  # type: ignore[attr-defined]
            self.repo, self.holder_id, number
        )
        if token == "":
            raise RuntimeError("The factory store is not configured, so no exclusive lease can be taken")
        return token

    def release(self, number: int, token: str) -> None:
        self._ledger.release_operation_lease(  # type: ignore[attr-defined]
            self.repo, number, token, "sync-finished"
        )


def snapshot_from_rows(rows: Iterable[object]) -> dict[int, PullView]:
    """A snapshot from REST pull rows or ``gh pr list --json`` rows (either spelling)."""
    found: dict[int, PullView] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("number"), int):
            continue
        head = row.get("head", row.get("headRefOid"))
        base = row.get("base", row.get("baseRefName"))
        raw_labels = row.get("labels")
        labels = frozenset(
            str(label.get("name") if isinstance(label, Mapping) else label)
            for label in (raw_labels if isinstance(raw_labels, list) else [])
        )
        found[int(row["number"])] = PullView(
            int(row["number"]),
            str(row.get("state") or "open").lower(),
            str((head.get("sha") if isinstance(head, Mapping) else head) or ""),
            labels,
            str((base.get("ref") if isinstance(base, Mapping) else base) or ""),
            str(row.get("mergeable_state") or row.get("mergeStateStatus") or ""),
        )
    return found


def queued_requests(snapshot: Snapshot) -> list[Request]:
    """Pull requests whose sync was requested with the queue label."""
    return [
        Request(number, view.head)
        for number, view in sorted(snapshot.items())
        if QUEUE_LABEL in view.labels
    ]


def _requests(raw: object) -> list[Request]:
    """Requests from a plan: ``rotisserie factory sync --json`` output or a bare list."""
    rows = raw.get("candidates") if isinstance(raw, Mapping) else raw
    if not isinstance(rows, list):
        raise ValueError("sync plan must be a list or an object with candidates")
    found: list[Request] = []
    for row in rows:
        if isinstance(row, int):
            found.append(Request(row))
            continue
        if not isinstance(row, Mapping) or not isinstance(row.get("pr"), int):
            raise ValueError("each sync plan entry needs an integer pr")
        head = str(row.get("head") or "")
        if head and not HEAD_RE.fullmatch(head):
            raise ValueError(f"invalid head for PR #{row['pr']}")
        found.append(Request(int(row["pr"]), head, str(row.get("target") or "")))
    return found


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--plan", help="JSON plan file, or - for stdin")
    source.add_argument(
        "--queued", action="store_true", help=f"sync open PRs labeled {QUEUE_LABEL}"
    )
    parser.add_argument("--holder", default="", help="operation lease holder id")
    parser.add_argument("--max", type=int, default=5, help="most PRs to write per run")
    args = parser.parse_args(argv)
    if not args.repo:
        parser.error("--repo or GITHUB_REPOSITORY is required")
    hub = GhHub(args.repo)
    try:
        snapshot = hub.open_pulls()
    except HubError as exc:
        print(f"factory sync: could not list open pull requests: {exc}", file=sys.stderr)
        return 1
    if args.queued:
        requests = queued_requests(snapshot)
        if not requests:
            print("[]")
            return 0
    else:
        text = sys.stdin.read() if args.plan == "-" else open(args.plan, encoding="utf-8").read()
        requests = _requests(json.loads(text or "[]"))
    holder = args.holder or f"sync-{os.environ.get('GITHUB_RUN_ID', 'local')}"
    leases: Leases
    if os.environ.get("FACTORY_TURSO_URL") or os.environ.get("FACTORY_STATE_SQLITE"):
        leases = StoreLeases(args.repo, holder)
    elif os.environ.get("FACTORY_STATE_REQUIRED") == "true":
        print("factory sync: store leases are required but not configured", file=sys.stderr)
        return 1
    else:
        leases = LabelLeases(hub, snapshot)
    outcomes = run(hub, leases, requests, limit=args.max, snapshot=snapshot)
    for outcome in outcomes:
        print(outcome.text, file=sys.stderr)
        if args.queued and outcome.status in FINAL:
            try:
                hub.dequeue(outcome.pr)
            except HubError as exc:
                print(f"PR #{outcome.pr}: could not clear the sync request: {exc}", file=sys.stderr)
    print(json.dumps([outcome.to_dict() for outcome in outcomes], sort_keys=True))
    return 1 if any(outcome.status == ERROR for outcome in outcomes) else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Synchronize factory pull requests with the default branch, one owner at a time.

This is the permanent replacement for hand-run sync watchers. The decision and
the write live in the factory runtime's ``factory_branch_sync`` module, which
the trusted scheduler runs every tick with store leases; this service plans
candidates from the same evidence ``unstick`` and ``checks`` use and drives that
module from the console.

Ownership: store leases are authoritative. Where the console cannot reach the factory store
(an operator laptop) it *queues* the request (label ``factory-sync-main``) for
the scheduler to perform under a lease. ``now=True`` syncs immediately on owner
labels alone, and every result says the factory store was not consulted.

GitHub cost: every function takes the open-PR rows read once (``open_pull_rows``)
so a pass shares one list call; each candidate then costs one compare, plus one
update-branch when it is behind.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Protocol, cast

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import GitHub, GitHubError, Obj, obj, string
from rotisserie.factory_console.service import baseline, checks
from rotisserie.factory_console.service.queue import PR_FIELDS, _rows

QUEUE_LABEL = "factory-sync-main"


def core() -> ModuleType:
    """The shared sync implementation (``factory_branch_sync``)."""
    return runtime.module("factory_branch_sync")


class SyncOutcome(Protocol):
    """One PR's result from ``factory_branch_sync`` (statuses in its ``STATUSES``)."""

    pr: int
    status: str
    text: str
    head: str
    neon: bool

    @property
    def ok(self) -> bool: ...

    def to_dict(self) -> dict[str, object]: ...


class PullView(Protocol):
    number: int
    head: str
    labels: frozenset[str]
    base: str


@dataclass(frozen=True)
class Candidate:
    """A PR that needs the default branch merged in, and why."""

    pr: int
    head: str  # the head the plan saw; empty = whatever is current
    title: str
    why: str
    target: str = ""  # commit it must contain; empty = the default branch


@dataclass(frozen=True)
class Held:
    pr: int
    text: str


@dataclass(frozen=True)
class Plan:
    candidates: tuple[Candidate, ...]
    held: tuple[Held, ...] = ()
    problems: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict[str, object]:
        return {
            "candidates": [asdict(item) for item in self.candidates],
            "held": [asdict(item) for item in self.held],
            "problems": list(self.problems),
        }


def open_pull_rows(github: GitHub) -> list[Obj]:
    """Every open pull request with heads, labels and merge state: one call."""
    return _rows(
        github.gh(
            "pr",
            "list",
            "--repo",
            github.repo,
            "--state",
            "open",
            "--limit",
            "500",
            "--json",
            PR_FIELDS,
        )
    )


def snapshot(rows: Iterable[Obj]) -> Mapping[int, PullView]:
    """The runtime's snapshot type, built from rows already read."""
    return cast(Mapping[int, PullView], core().snapshot_from_rows(list(rows)))


class GitHubHub:
    """The runtime's Hub protocol over the console's GitHub client."""

    def __init__(self, github: GitHub) -> None:
        self.github = github
        self._core = core()

    def _wrap(self, exc: GitHubError) -> Exception:
        error = cast(type[Exception], self._core.HubError)
        kind = str(self._core.classify_update_error(str(exc)))
        return error(str(exc), kind)

    def pull(self, number: int) -> object:
        try:
            data = obj(self.github.repo_api(f"pulls/{number}"))
        except GitHubError as exc:
            raise self._wrap(exc) from exc
        return next(iter(self._core.snapshot_from_rows([data]).values()))

    def contains(self, head: str, target: str) -> bool:
        try:
            status = string(obj(self.github.repo_api(f"compare/{target}...{head}")), "status")
        except GitHubError as exc:
            raise self._wrap(exc) from exc
        return status in ("ahead", "identical")

    def update_branch(self, number: int, expected_head: str) -> None:
        try:
            self.github.repo_api(
                f"pulls/{number}/update-branch",
                method="PUT",
                fields={"expected_head_sha": expected_head},
            )
        except GitHubError as exc:
            raise self._wrap(exc) from exc


def ledger_directories() -> list[Path]:
    """Where the store ledger can be found: the adopter's overlay, then this checkout."""
    found = [Path.cwd() / ".github" / "scripts"]
    try:
        found.append(runtime.runtime_directory())
    except runtime.RuntimeUnavailableError:
        pass
    return [path for path in found if (path / "factory_state_ledger.py").is_file()]


STORE_ENV = ("FACTORY_TURSO_URL", "FACTORY_STATE_SQLITE")


def store_configured(env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    return any(values.get(name) for name in STORE_ENV)


def load_ledger(env: Mapping[str, str] | None = None) -> tuple[ModuleType | None, str]:
    """The store ledger module, or ``None`` and why it cannot be used here."""
    if not store_configured(env):
        return None, (
            "The factory store is not configured here (FACTORY_TURSO_URL is unset); leases are "
            "visible only to the trusted factory runtime"
        )
    for directory in ledger_directories():
        sys.path.insert(0, str(directory))
        try:
            import factory_state_ledger as ledger  # type: ignore[import-not-found]
        except ImportError as exc:
            return None, f"the store ledger could not load ({exc})"
        finally:
            sys.path.remove(str(directory))
        return cast(ModuleType, ledger), ""
    return None, "the store ledger script was not found (run from an adopter checkout)"


def _red(github: GitHub, number: int) -> list[checks.FailingCheck]:
    try:
        return checks.failing_checks(github, number)
    except GitHubError:
        return []


def stale_plan(github: GitHub, rows: Sequence[Obj]) -> Plan:
    """Factory PRs that are red only because main has since moved past the failure.

    The same rule ``unstick`` applies: a PR whose failing checks came from an
    older main is synced, never handed to a repair worker. A conflicting PR is
    left to merge-conflict work, which merges main anyway. Heads come from
    ``rows``; only the check reads are per PR.
    """
    backlog = runtime.completion_backlog(list(rows))
    heads = {
        number: str(row.get("headRefOid") or "")
        for row in rows
        if isinstance(number := row.get("number"), int)
    }
    with ThreadPoolExecutor(max_workers=6) as pool:
        red = dict(
            zip(
                (item.number for item in backlog),
                pool.map(lambda pr: _red(github, pr.number), backlog),
                strict=True,
            )
        )
    found = {number: failing for number, failing in red.items() if failing}
    try:
        shared = baseline.analyze(github, found) if found else baseline.Report()
    except GitHubError as exc:
        return Plan((), (), (f"could not compare the failures with main: {exc}",))
    candidates: list[Candidate] = []
    held: list[Held] = []
    for item in sorted(backlog, key=lambda pr: pr.number):
        if not red.get(item.number) or not shared.needs_sync(item.number) or item.conflicted:
            continue
        if not item.free:
            held.append(
                Held(item.number, f"PR #{item.number}: {item.owner} holds it; sync after it ends")
            )
            continue
        names = ", ".join(
            dict.fromkeys(v.check for v in shared.for_pr(item.number) if v.not_the_prs)
        )
        candidates.append(
            Candidate(
                item.number,
                heads.get(item.number, ""),
                item.title,
                f"red because of an older main ({names}); main has moved on",
            )
        )
    return Plan(tuple(candidates), tuple(held), tuple(shared.problems))


def explicit_plan(numbers: Sequence[int], *, target: str = "") -> Plan:
    """The PRs the operator named; the sync re-checks each against the snapshot."""
    return Plan(
        tuple(Candidate(number, "", "", "named by the operator", target) for number in numbers)
    )


def execute(
    github: GitHub,
    plan: Plan,
    rows: Sequence[Obj],
    *,
    holder: str,
    limit: int | None = None,
    now: bool = False,
    hub: object | None = None,
    lease_store: object | None = None,
) -> tuple[list[SyncOutcome], str]:
    """Sync each candidate under an exclusive lease. Returns outcomes and a lease note.

    Without the store and without ``now`` this refuses (``RuntimeError``): use :func:`queue`.
    """
    module = core()
    active_hub = hub if hub is not None else GitHubHub(github)
    pulls = module.snapshot_from_rows(list(rows))
    note = ""
    if lease_store is None:
        ledger, note = load_ledger()
        if ledger is not None:
            lease_store = module.StoreLeases(github.repo, holder, ledger)
        elif now:
            lease_store = module.LabelLeases(active_hub, pulls)
        else:
            raise RuntimeError(note)
    requests = [module.Request(item.pr, item.head, item.target) for item in plan.candidates]
    changes = module.run(active_hub, lease_store, requests, limit=limit, snapshot=pulls)
    return [cast(SyncOutcome, change) for change in changes], note


@dataclass(frozen=True)
class Queued:
    pr: int
    ok: bool
    text: str


def queue(github: GitHub, plan: Plan, rows: Sequence[Obj]) -> list[Queued]:
    """Ask the trusted scheduler to sync each candidate under a store lease.

    One label write per PR not already queued; closed PRs are skipped.
    """
    pulls = snapshot(rows)
    done: list[Queued] = []
    for item in plan.candidates:
        view = pulls.get(item.pr)
        if view is None:
            done.append(Queued(item.pr, False, f"PR #{item.pr}: not open; not queued"))
            continue
        if QUEUE_LABEL in view.labels:
            done.append(Queued(item.pr, True, f"PR #{item.pr}: already queued for a leased sync"))
            continue
        try:
            github.repo_api(
                f"issues/{item.pr}/labels", method="POST", fields={"labels[]": QUEUE_LABEL}
            )
        except GitHubError as exc:
            done.append(Queued(item.pr, False, f"PR #{item.pr}: could not queue it: {exc}"))
            continue
        done.append(
            Queued(
                item.pr,
                True,
                f"PR #{item.pr}: queued; the scheduler syncs it under a store lease once no "
                "worker holds it",
            )
        )
    return done


# Lease inspection -----------------------------------------------------------------


@dataclass(frozen=True)
class LeaseRow:
    target: str  # #N or worker:N
    holder: str
    active: bool
    expires_at: str


@dataclass(frozen=True)
class LeaseReport:
    source: str  # neon | labels
    rows: tuple[LeaseRow, ...]
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        rows = [asdict(row) for row in self.rows]
        return {"source": self.source, "rows": rows, "note": self.note}


def _describe(row: Mapping[str, object]) -> str:
    claim = str(row.get("claim_id", ""))[:8]
    if row.get("worker"):
        return f"worker {row['worker']} (claim {claim})"
    if row.get("operation"):
        return f"operation {row['operation']} (claim {claim})"
    return "unknown holder"


def lease_report(
    github: GitHub, rows: Sequence[Obj] | None = None, numbers: Iterable[int] = ()
) -> LeaseReport:
    """Who holds what: The factory store when reachable, otherwise owner labels from ``rows``."""
    wanted = set(numbers)
    ledger, why = load_ledger()
    if ledger is not None:
        found: list[LeaseRow] = []
        for row in ledger.list_leases(github.repo):
            _, kind, ident = str(row["key"]).rsplit(":", 2)
            if wanted and (kind != "target" or int(ident) not in wanted):
                continue
            found.append(
                LeaseRow(
                    f"#{ident}" if kind == "target" else f"{kind}:{ident}",
                    _describe(row),
                    bool(row["active"]),
                    str(row["expires_at"]),
                )
            )
        return LeaseReport("neon", tuple(found))
    pulls = snapshot(rows if rows is not None else open_pull_rows(github))
    labelled: list[LeaseRow] = []
    for number, view in sorted(pulls.items()):
        if wanted and number not in wanted:
            continue
        owner = sorted(label for label in view.labels if _owner(label))
        if owner or wanted:
            holder = f"owner label {owner[0]}" if owner else "free"
            labelled.append(LeaseRow(f"#{number}", holder, bool(owner), ""))
    return LeaseReport("labels", tuple(labelled), why)


def release_expired(github: GitHub) -> tuple[int | None, str]:
    """Purge long-expired lease rows (never an active one). ``None`` without the factory store."""
    ledger, why = load_ledger()
    if ledger is None:
        return None, why
    return int(ledger.release_expired_leases(github.repo)), ""


def _owner(label: str) -> bool:
    suffix = label.removeprefix("factory:")
    return label.startswith("factory:") and (suffix.isdigit() or suffix == "local")

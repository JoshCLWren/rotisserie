"""Which Actions run owns each worker claim, and whether that run still executes.

Worker IDs name roster slots, and later runs reuse them, so a live run for a
worker does not prove that the worker's current claim is its own. The owner of
a claim is the Entry run recorded on its worker row at worker start. Without
that record, it is a run for the same worker created within minutes after the
claim, because the dispatcher claims first and dispatches second.

A claim is renewed only while its owner runs, released at once when its owner
has completed, and otherwise left to expire.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

WORKFLOWS = ("free-model-factory-entry.yml", "factory-fork-execution.yml")
LIVE_STATUSES = frozenset({"queued", "in_progress", "waiting", "requested", "pending"})
CLOCK_SKEW_SECONDS = 120
DISPATCH_WINDOW_SECONDS = 900
LOOKUP_LIMIT = 20
TITLE_RE = re.compile(r"\bFactory\s+(\d+)\b")

Get = Callable[[str], object]
Run = Mapping[str, Any]


class ActiveWorkerResult(tuple[set[int], set[str]]):
    """Pair of resolved workers and credible unresolved runs with set compatibility.

    ``live_runs`` keeps each live run with its resolved worker for claim ownership.
    """

    live_runs: tuple[Run, ...]
    run_workers: dict[str, str]

    def __new__(
        cls,
        workers: set[int],
        unresolved: set[str],
        live_runs: Iterable[Run] = (),
        run_workers: Mapping[str, str] | None = None,
    ) -> ActiveWorkerResult:
        result = tuple.__new__(cls, (workers, unresolved))
        result.live_runs = tuple(live_runs)
        result.run_workers = dict(run_workers or {})
        return result

    def __eq__(self, other: object) -> bool:
        if isinstance(other, set):
            return self[0] == other
        return tuple.__eq__(self, other)


def github_timestamp_epoch(value: object) -> int | None:
    """Parse one GitHub timestamp into epoch seconds when possible."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


@dataclass(frozen=True)
class Owner:
    claim_id: str
    worker: str
    run_id: str
    state: str  # running | completed | unknown
    recorded: bool

    def describe(self) -> str:
        if not self.run_id:
            return "no owning run found"
        how = "recorded" if self.recorded else "dispatched"
        return f"run {self.run_id} ({how}), {self.state}"


def run_worker(run: Run, workers: Mapping[str, str]) -> str:
    match = TITLE_RE.search(str(run.get("display_title") or run.get("name") or ""))
    return match.group(1) if match else workers.get(str(run.get("id") or ""), "")


def run_state(run: Run) -> str:
    status = str(run.get("status") or "").lower()
    if status in LIVE_STATUSES:
        return "running"
    return "completed" if status == "completed" else "unknown"


def worker_claims(leases: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Active worker rows only; target and slot rows share their worker row's claim."""
    return [
        lease
        for lease in leases
        if lease.get("active")
        and str(lease.get("key") or "").rsplit(":", 2)[-2:-1] == ["worker"]
        and str(lease.get("worker") or "").isdecimal()
        and lease.get("claim_id")
    ]


def owner_of(lease: Mapping[str, Any], runs: Sequence[Run], workers: Mapping[str, str]) -> Owner:
    claim, worker = str(lease["claim_id"]), str(lease["worker"])
    recorded = str(lease.get("run_id") or "")
    if recorded:
        found = [run for run in runs if str(run.get("id") or "") == recorded]
        state = run_state(found[0]) if found else "unknown"
        return Owner(claim, worker, recorded, state, True)
    acquired = github_timestamp_epoch(lease.get("acquired_at"))
    candidates: list[Run] = []
    for run in runs if acquired is not None else ():
        created = github_timestamp_epoch(run.get("created_at"))
        if created is None or run_worker(run, workers) != worker:
            continue
        if acquired - CLOCK_SKEW_SECONDS <= created <= acquired + DISPATCH_WINDOW_SECONDS:
            candidates.append(run)
    if not candidates:
        return Owner(claim, worker, "", "unknown", False)
    live = [run for run in candidates if run_state(run) == "running"]
    chosen = live[0] if live else min(candidates, key=lambda run: str(run.get("created_at")))
    return Owner(claim, worker, str(chosen.get("id") or ""), run_state(chosen), False)


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _lookup(get: Get, lease: Mapping[str, Any]) -> list[Run]:
    recorded = str(lease.get("run_id") or "")
    if recorded.isdecimal():
        run = get(f"actions/runs/{recorded}")
        return [run] if isinstance(run, dict) else []
    acquired = github_timestamp_epoch(lease.get("acquired_at"))
    if acquired is None:
        return []
    window = f"{_iso(acquired - CLOCK_SKEW_SECONDS)}..{_iso(acquired + DISPATCH_WINDOW_SECONDS)}"
    found: list[Run] = []
    for workflow in WORKFLOWS:
        page = get(f"actions/workflows/{workflow}/runs?created={window}&per_page=100")
        runs = page.get("workflow_runs") if isinstance(page, dict) else None
        found.extend(run for run in runs or [] if isinstance(run, dict))
    return found


def claim_owners(
    leases: Iterable[Mapping[str, Any]],
    get: Get,
    live_runs: Iterable[Run] = (),
    workers: Mapping[str, str] | None = None,
) -> list[Owner]:
    """The owner of each active worker claim; a failed lookup leaves that owner unknown."""
    workers = dict(workers or {})
    runs = list(live_runs)
    owners: list[Owner] = []
    lookups = 0
    for lease in worker_claims(leases):
        owner = owner_of(lease, runs, workers)
        if owner.state == "unknown" and lookups < LOOKUP_LIMIT:
            lookups += 1
            try:
                runs.extend(_lookup(get, lease))
            except Exception as exc:
                print(
                    f"[factory-controller] owner lookup for claim {owner.claim_id[:8]} failed"
                    f" ({type(exc).__name__}); leaving it to expire",
                    file=sys.stderr,
                )
            owner = owner_of(lease, runs, workers)
        owners.append(owner)
    return owners


def reconcile(repo: str, get: Get, active: object, ledger: Any) -> list[Owner]:
    """Renew claims whose owner runs; release claims whose owner completed."""
    if not ledger.enabled():
        return []
    owners = claim_owners(
        ledger.list_leases(repo),
        get,
        getattr(active, "live_runs", ()),
        getattr(active, "run_workers", {}),
    )
    running = [owner.claim_id for owner in owners if owner.state == "running"]
    if running:
        ledger.renew_owned_claims(repo, running)
    for owner in owners:
        if owner.state != "completed":
            continue
        released = ledger.release_lease(
            repo, owner.worker, owner.claim_id, f"owning-run-{owner.run_id}-completed"
        )
        print(
            f"[factory-controller] released claim {owner.claim_id[:8]} of worker {owner.worker}:"
            f" owning run {owner.run_id} completed" + ("" if released else " (already released)"),
            file=sys.stderr,
        )
    return owners

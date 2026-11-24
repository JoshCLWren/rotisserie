"""Neon history for an estimate, read through the label claim workflow, and its use.

The console starts the adopter's ``rotisserie-label-claim.yml`` in
``estimate-history`` mode and reads the JSON artifact it publishes. Neon
replaces only attempt counts and person signals; features and merge facts
still come from GitHub. An item that started before the history window keeps
its GitHub count.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime

from rotisserie.factory_console.github import GitHub
from rotisserie.factory_console.service import bridge, claimops
from rotisserie.factory_console.service import estimate_neon as neon
from rotisserie.factory_console.service.estimate_model import Item
from rotisserie.factory_console.tracker import Clock

NEON = "Neon history through the label claim workflow"
GITHUB = "GitHub labels and factory marker comments"


@dataclass(frozen=True)
class Runs:
    attempts: int = 0
    refusals: int = 0
    capacity_runs: int = 0
    assignments: int = 0
    signals: tuple[str, ...] = ()


@dataclass(frozen=True)
class History:
    covered_from: datetime
    window_days: int
    rows_read: int
    truncated: bool
    unknown_classes: dict[str, int]
    numbers: dict[int, dict[str, object]]
    bridge: dict[str, object] = field(default_factory=dict)

    def covers(self, started: datetime | None) -> bool:
        return started is not None and started >= self.covered_from

    def runs(self, numbers: tuple[int, ...]) -> Runs:
        seen: dict[str, dict[str, object]] = {}
        counts: dict[str, int] = {}
        assigned: set[str] = set()
        for number in numbers:
            entry = self.numbers.get(number, {})
            for run in _list(entry.get("runs")):
                seen.setdefault(str(run.get("run_id")), run)
            for key, value in _dict(entry.get("counts")).items():
                counts[key] = counts.get(key, 0) + _int(value)
            found = entry.get("assignments")
            if isinstance(found, list):
                assigned.update(key for key in found if isinstance(key, str))
        reached = [run for run in seen.values() if run.get("reached_model") is True]
        signals = [name for name in ("owner_claim", "operator_revoked") if counts.get(name)]
        return Runs(
            attempts=len(reached),
            refusals=sum(_int(run.get("refusals")) for run in seen.values()),
            capacity_runs=len(seen) - len(reached),
            assignments=len(assigned),
            signals=tuple(signals),
        )


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _dict(value: object) -> dict[str, object]:
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}


def _list(value: object) -> list[dict[str, object]]:
    return [_dict(row) for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def parse(data: dict[str, object]) -> History:
    """The workflow's artifact, checked; ``ValueError`` says why it cannot be used."""
    if data.get("mode") != neon.MODE or data.get("schema") != neon.SCHEMA:
        raise ValueError("the workflow did not return estimate history (an older runtime?)")
    if data.get("status") != "ok":
        raise ValueError(f"Neon history is unavailable: {data.get('reason') or 'no reason given'}")
    try:
        covered = datetime.fromisoformat(str(data.get("covered_from")))
    except ValueError:
        raise ValueError("the estimate history has no valid window start") from None
    if covered.tzinfo is None:
        raise ValueError("the estimate history window start has no time zone")
    numbers: dict[int, dict[str, object]] = {}
    for key, value in _dict(data.get("numbers")).items():
        if key.isdecimal() and isinstance(value, dict):
            numbers[int(key)] = _dict(value)
    unknown = {k: _int(v) for k, v in _dict(data.get("unknown_classes")).items()}
    return History(
        covered_from=covered,
        window_days=_int(data.get("window_days")),
        rows_read=_int(data.get("rows_read")),
        truncated=data.get("truncated") is True,
        unknown_classes=unknown,
        numbers=numbers,
        bridge=_dict(data.get("bridge")),
    )


def apply(item: Item, history: History) -> tuple[Item, Runs | None]:
    """The item with Neon's attempts and person signals; ``None`` runs keep marker counts.

    Coverage starts at the item's first factory activity, not the issue's
    creation, so a long-lived backlog issue worked recently still counts. A
    recorded person signal applies whatever the window. A covered item with
    markers but no Neon run keeps its marker count: a missing run record is not
    proof that no worker ran.
    """
    runs = history.runs(item.numbers)
    signals = tuple(dict.fromkeys(item.signals + runs.signals))
    started = item.first_claim or item.pr_opened or item.created_at
    if not history.covers(started):
        return replace(item, signals=signals), None
    if not runs.attempts and not runs.capacity_runs and item.attempts:
        return replace(item, signals=signals), None
    return replace(item, attempts=runs.attempts, signals=signals), runs


def start(github: GitHub) -> dict[str, object]:
    run_id, url = bridge._start(github, claimops.LABEL_WORKFLOW, {"mode": neon.MODE})
    return {"status": "started", "bridge": {"run_id": run_id, "url": url}}


def fetch(
    github: GitHub, clock: Clock, *, started: dict[str, object] | None = None
) -> dict[str, object]:
    result = bridge.run(
        github,
        clock,
        claimops.LABEL_WORKFLOW,
        {"mode": neon.MODE},
        artifact=claimops.LABEL_ARTIFACT,
        timeout=bridge.CLAIM_TIMEOUT,
        started=started,
    )
    return {**result.data, "bridge": result.summary()}


def earlier(github: GitHub, run_id: int) -> dict[str, object]:
    """An earlier run's artifact, or ``{"result": "pending"}`` while it still runs."""
    return claimops.run_report(github, run_id)


def load(data: dict[str, object]) -> tuple[History | None, str]:
    """The history, or ``None`` and why the estimate falls back to GitHub."""
    if data.get("result") in ("pending", "skipped", "unavailable"):
        return None, str(data.get("reason") or "the workflow run has not finished")
    try:
        return parse(data), ""
    except ValueError as exc:
        return None, str(exc)

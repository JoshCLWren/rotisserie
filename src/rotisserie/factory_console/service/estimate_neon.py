"""Exact per-item factory history from the factory store, read only inside the trusted workflow.

The label claim workflow's ``estimate-history`` mode runs this with the
adopter's store secret. It runs one bounded, read-only query over
``factory_state_events`` (``factory_turso_events.history_rows``) and publishes
per-item counts. It never
publishes claim UUIDs, logins, prompts or failure log tails. The console reads
the published counts; it never connects to the factory store itself.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from rotisserie.factory_console.service import estimate_outcomes as outcomes
from rotisserie.factory_console.service import sync as branch_sync

SCHEMA = 1
MODE = "estimate-history"
DEFAULT_DAYS = 45
DAYS_RANGE = (1, 120)
DEFAULT_ROWS = 40000
ROWS_RANGE = (100, 100000)
EVENT_TYPES = (
    "worker_assigned",
    "model_attempt",
    "worker_completed",
    "worker_failed",
    "worker_released",
    "worker_idle",
    "external_reserved",
    "external_revoked",
)
TERMINAL = frozenset(("worker_completed", "worker_failed", "worker_idle"))


def bounds(days: int, rows: int) -> tuple[int, int]:
    for value, (low, high), flag in ((days, DAYS_RANGE, "--days"), (rows, ROWS_RANGE, "--rows")):
        if not low <= value <= high:
            raise ValueError(f"{flag} must be between {low} and {high}")
    return days, rows


def _number(value: object) -> int | None:
    text = str(value) if value is not None else ""
    return int(text) if text.isdecimal() and 0 < int(text) < 10**9 else None


def _targets(value: object) -> list[int]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if not isinstance(value, list):
        return []
    return [n for item in value if (n := _number(item)) is not None]


def _iso(value: object) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value or "")


def _ran(row: Mapping[str, Any]) -> bool:
    code = str(row.get("agent_exit_code") or "")
    duration = str(row.get("duration_ms") or "")
    return code.lstrip("-").isdecimal() or (duration.isdecimal() and int(duration) > 0)


def _weight(row: Mapping[str, Any]) -> int:
    value = row.get("row_count")
    return value if isinstance(value, int) and value > 0 else 1


class _Run:
    def __init__(self) -> None:
        self.at = ""
        self.attempt_rows = 0
        self.ran_rows = 0
        self.refusals = 0
        self.terminal = ""
        self.models: set[str] = set()

    def reached(self) -> bool:
        if self.attempt_rows or self.terminal == "success":
            return True
        if self.ran_rows or not self.terminal:
            return False
        return outcomes.role(self.terminal) == outcomes.ATTEMPT

    def to_dict(self, run_id: str) -> dict[str, object]:
        return {
            "run_id": run_id,
            "at": self.at,
            "reached_model": self.reached(),
            "attempt_rows": self.attempt_rows,
            "refusals": self.refusals,
            "terminal": self.terminal,
            "models": sorted(self.models)[:20],
        }

    def add(self, row: Mapping[str, Any], name: str) -> None:
        if row.get("model"):
            self.models.add(f"{row.get('source') or '?'}/{row.get('model')}")
        if not _ran(row):
            self.refusals += _weight(row)
            return
        self.ran_rows += 1
        role = outcomes.role(name)
        if role == outcomes.CAPACITY:
            self.refusals += 1
        elif role == outcomes.ATTEMPT:
            self.attempt_rows += 1


def summarize(
    rows: Sequence[Mapping[str, Any]],
    *,
    days: int,
    cap: int,
    now: datetime,
) -> dict[str, object]:
    """Per-item counts from the query's rows (newest first, at most ``cap + 1``)."""
    truncated = len(rows) > cap
    kept = rows[:cap]
    covered = now - timedelta(days=days)
    if truncated and kept:
        oldest = kept[-1].get("created_at")
        if isinstance(oldest, datetime):
            covered = max(covered, oldest.astimezone(UTC))
    runs: dict[int, dict[str, _Run]] = defaultdict(lambda: defaultdict(_Run))
    counts: dict[int, Counter[str]] = defaultdict(Counter)
    assigned: dict[int, set[str]] = defaultdict(set)
    classes: dict[int, Counter[str]] = defaultdict(Counter)
    unknown: Counter[str] = Counter()
    for row in reversed(kept):
        kind = str(row.get("event_type") or "")
        run_id = str(row.get("run_id") or "")
        if kind in ("worker_assigned", "worker_released"):
            key = f"{run_id}@{_iso(row.get('created_at'))}"
            for target in _targets(row.get("targets")):
                if kind == "worker_assigned":
                    assigned[target].add(key)
                else:
                    counts[target]["released"] += 1
            continue
        if kind in outcomes.CLAIM_EVENTS:
            claimed = _number(row.get("subject_number"))
            signal = outcomes.claim_signal(kind, row.get("client"))
            if claimed is not None:
                counts[claimed][signal or "outside_claims"] += 1
            continue
        selected = _number(row.get("selected_number"))
        if selected is None or not run_id:
            continue
        name = str(row.get("outcome_class") or "unknown")
        if not outcomes.known(name):
            unknown[name] += _weight(row)
        run = runs[selected][run_id]
        run.at = run.at or _iso(row.get("created_at"))
        if kind in TERMINAL:
            run.terminal = name
            classes[selected][name] += 1
        else:
            run.add(row, name)
    numbers: dict[str, object] = {}
    for number in sorted(set(runs) | set(counts) | set(assigned)):
        numbers[str(number)] = {
            "runs": [run.to_dict(run_id) for run_id, run in runs[number].items()],
            "assignments": sorted(assigned[number]),
            "counts": dict(counts[number]),
            "terminal_classes": dict(classes[number]),
        }
    return {
        "mode": MODE,
        "schema": SCHEMA,
        "status": "ok",
        "window_days": days,
        "row_cap": cap,
        "rows_read": len(kept),
        "truncated": truncated,
        "covered_from": covered.isoformat(),
        "generated_at": now.isoformat(),
        "unknown_classes": dict(unknown),
        "mapping": outcomes.mapping(),
        "numbers": numbers,
    }


def unavailable(reason: str, *, days: int, cap: int, now: datetime) -> dict[str, object]:
    return {
        "mode": MODE,
        "schema": SCHEMA,
        "status": "unavailable",
        "reason": reason,
        "window_days": days,
        "row_cap": cap,
        "generated_at": now.isoformat(),
    }


def read_history(
    repo: str,
    *,
    days: int = DEFAULT_DAYS,
    cap: int = DEFAULT_ROWS,
    now: datetime | None = None,
    ledger: Any = None,
) -> dict[str, object]:
    """Run the bounded query (trusted Actions only) and return the publishable summary."""
    now = now or datetime.now(UTC)
    days, cap = bounds(days, cap)
    if ledger is None:
        ledger, why = branch_sync.load_ledger()
        if ledger is None:
            return unavailable(why, days=days, cap=cap, now=now)
    try:
        # One row past the cap shows the history was truncated.
        rows = ledger.history_rows(repo, days, cap + 1, EVENT_TYPES)
    except Exception as exc:  # noqa: BLE001 - the console falls back to GitHub history
        why = f"the store read failed ({type(exc).__name__})"
        return unavailable(why, days=days, cap=cap, now=now)
    return summarize(rows, days=days, cap=cap, now=now)

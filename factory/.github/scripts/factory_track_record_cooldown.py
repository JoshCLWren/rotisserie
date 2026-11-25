"""Cooldown policy for a model's own failures, scaled by its track record.

Each failure class has its own curve, by strikes in a row (``strike_delay``):

* Task outcomes (``work_failure``, which covers no-change and review verdict
  failures, and ``agent_timeout``) say the task was hard, not that the model
  is broken; item loops belong to the per-item loop breaker. No wait on the
  first strike, then 5 minutes more per strike, capped at 30 minutes.
* Model or infrastructure faults (``provider_failure``, ``environment_failure``,
  ``unknown_failure``, ``control_plane_failure``): the five-minute Fibonacci
  curve, capped at 2 hours.
* ``model_unavailable``: 15 minutes, doubling per strike, capped at 6 hours.
* ``model_policy_violation`` and ``model_retired_410`` keep 6 hours.

Strikes decay: only strikes from the last ``STRIKE_WINDOW_SECONDS`` count, and
any success resets the streak. A proven model gets half or less: a hot model
(``factory_model_track_record.hot`` on its seven-day all-action record) waits
nothing for its first two task strikes and half of every other curve. For
``model_unavailable`` a kept model (a success in the published record, or a
completed run in the last ``RECENT_SUCCESS_HOURS``) waits nothing on the
first strike and at most 15 minutes after.

Readers clamp stored rows to these caps (``outcome_cap``), so rows written
under the old 24-hour curve end promptly. Capacity signals (quota, credit
pool, throttle, account limits) are not handled here and do not change.
Without fresh stats (``FACTORY_TRACK_RECORD_FILE``) no model is proven.

Each value reads ``FACTORY_<NAME>`` from the environment.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from factory_state_operations_backoff import fibonacci_worker_delay

STRIKE_WINDOW_SECONDS = 6 * 3600
TASK_STEP = 300
TASK_CAP = 1800
FAULT_CAP = 7200
LONG_COOLDOWN = 21600
HOT_GRACE = 2
UNAVAILABLE_FIRST = 900
UNAVAILABLE_CAP = 21600
UNAVAILABLE_KEPT_MAX = 900
UNAVAILABLE_WINDOW_SECONDS = STRIKE_WINDOW_SECONDS
RECENT_SUCCESS_HOURS = 24
MAX_STREAK_ROW = 40
TASK_OUTCOMES = frozenset({"work_failure", "agent_timeout"})
LONG_OUTCOMES = frozenset({"model_policy_violation", "model_retired_410"})
WORK_OUTCOMES = TASK_OUTCOMES
ALL_ACTIONS = ""


@dataclass(frozen=True)
class Heat:
    successes: int
    trials: int
    note: str = ""

    def detail(self) -> dict[str, Any]:
        record = self.note or f"{self.successes} of {self.trials} runs succeeded in 7d"
        return {"kept_hot": True, "track_record": record}


def _setting(name: str, default: float) -> int:
    from factory_model_track_record import setting

    return int(setting(name, default))


def unavailable_delay(strikes: int, kept: bool) -> int:
    """Seconds benched after ``strikes`` unavailable signals in a row."""
    if strikes < 1:
        raise ValueError("strike count must be positive")
    if kept:
        return 0 if strikes == 1 else unavailable_cap(True)
    first = _setting("UNAVAILABLE_FIRST", UNAVAILABLE_FIRST)
    return min(first * 2 ** min(strikes - 1, 10), UNAVAILABLE_CAP)


def strike_delay(outcome: str, strike: int, proven: bool = False) -> int:
    """Seconds a model or worker waits after ``strike`` failures of ``outcome`` in a row."""
    if strike < 1:
        raise ValueError("failure streak must be positive")
    if outcome == "model_unavailable":
        return unavailable_delay(strike, proven)
    if outcome in LONG_OUTCOMES:
        return LONG_COOLDOWN
    if outcome in TASK_OUTCOMES:
        step, cap = _setting("TASK_STEP", TASK_STEP), _setting("TASK_CAP", TASK_CAP)
        if proven:
            grace = _setting("HOT_GRACE", HOT_GRACE)
            return 0 if strike <= grace else min(step * (strike - grace) // 2, cap // 2)
        return min(step * (strike - 1), cap)
    cap = _setting("FAULT_CAP", FAULT_CAP)
    delay = min(fibonacci_worker_delay(strike), cap)
    return delay // 2 if proven else delay


def unavailable_cap(kept: bool) -> int:
    """The longest a ``model_unavailable`` signal benches a model."""
    if kept:
        return _setting("UNAVAILABLE_KEPT_MAX", UNAVAILABLE_KEPT_MAX)
    return UNAVAILABLE_CAP


def outcome_cap(outcome: str) -> int:
    """The longest wait ``outcome`` can earn; readers clamp older rows to it."""
    if outcome == "model_unavailable":
        return unavailable_cap(False)
    if outcome in LONG_OUTCOMES:
        return LONG_COOLDOWN
    if outcome in TASK_OUTCOMES:
        return _setting("TASK_CAP", TASK_CAP)
    return _setting("FAULT_CAP", FAULT_CAP)


def delays_for(outcome: str, proven: bool = False) -> str:
    """The JSON streak table ``streak_upsert`` reads for ``outcome``."""
    return json.dumps([strike_delay(outcome, n, proven) for n in range(1, MAX_STREAK_ROW + 1)])


def recent_success_seconds() -> int:
    from factory_model_track_record import setting

    return int(setting("RECENT_SUCCESS_HOURS", RECENT_SUCCESS_HOURS) * 3600)


def heat(source: str, model: str, outcome: str) -> Heat | None:
    """The record that keeps this model hot after ``outcome``, or None."""
    if outcome in LONG_OUTCOMES or not source or not model:
        return None
    try:
        from factory_model_track_record import fleet_prior, hot, records

        found = records(ALL_ACTIONS)
        record = found.get((source, model))
        if record is None or record.proven_bad:
            return None
        if outcome == "model_unavailable":
            if record.successes == 0:
                return None
        elif not hot(record, fleet_prior(found)):
            return None
    except ImportError:
        return None
    return Heat(record.successes, record.trials)


def hot_models(stats: object, now: Any) -> dict[tuple[str, str], Heat]:
    """Every hot model in a published stats snapshot, for the console."""
    from factory_model_track_record import fleet_prior, hot, parse

    found = parse(stats, ALL_ACTIONS, now)
    prior = fleet_prior(found)
    return {
        key: Heat(record.successes, record.trials)
        for key, record in found.items()
        if not record.proven_bad and hot(record, prior)
    }

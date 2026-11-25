"""Short cooldowns for models whose track record is clearly good.

A hot model (``factory_model_track_record.hot`` on its all-action record over
seven days) that fails its own work does not sit out the long Fibonacci curve:

* ``work_failure``, ``unknown_failure`` and ``agent_timeout``: no wait for the
  first ``HOT_GRACE`` failures in a row, then the five-minute Fibonacci curve
  restarted after the grace, capped at ``HOT_MAX_COOLDOWN`` seconds.
* ``model_unavailable``: ``HOT_MAX_COOLDOWN`` seconds instead of six hours for
  any model that succeeded in the published seven-day record, or (read from the
  store by the runtime) completed a run in the last ``RECENT_SUCCESS_HOURS``.
  One catalog miss does not bench such a model; the live catalog, read at
  every selection, still drops a model the provider really removed.

Capacity signals never change: quota, credit pool, account limits, throttles,
provider failures, policy violations and retirement keep their own cooldowns.
An unproven or proven-bad model keeps the normal curve. Without fresh stats
(``FACTORY_TRACK_RECORD_FILE``) every model keeps the normal curve.

Each value reads ``FACTORY_<NAME>`` from the environment, as the hot test does.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from factory_state_operations_backoff import fibonacci_worker_delay

HOT_GRACE = 2
RECENT_SUCCESS_HOURS = 24
HOT_MAX_COOLDOWN = 1800
MAX_STREAK_ROW = 40
WORK_OUTCOMES = frozenset({"work_failure", "unknown_failure", "agent_timeout"})
HOT_OUTCOMES = WORK_OUTCOMES | {"model_unavailable"}
ALL_ACTIONS = ""


@dataclass(frozen=True)
class Heat:
    successes: int
    trials: int
    note: str = ""

    def detail(self) -> dict[str, Any]:
        record = self.note or f"{self.successes} of {self.trials} runs succeeded in 7d"
        return {"kept_hot": True, "track_record": record}


def _settings() -> tuple[int, int]:
    from factory_model_track_record import setting

    return int(setting("HOT_GRACE", HOT_GRACE)), int(setting("HOT_MAX_COOLDOWN", HOT_MAX_COOLDOWN))


def hot_delay(streak: int) -> int:
    """Seconds a hot model waits after ``streak`` work failures in a row."""
    if streak < 1:
        raise ValueError("failure streak must be positive")
    grace, cap = _settings()
    if streak <= grace:
        return 0
    return min(fibonacci_worker_delay(streak - grace), cap)


def hot_delays() -> str:
    """The streak delay table ``streak_upsert`` reads for a hot model."""
    return json.dumps([hot_delay(n) for n in range(1, MAX_STREAK_ROW + 1)])


def unavailable_delay() -> int:
    return _settings()[1]


def recent_success_seconds() -> int:
    from factory_model_track_record import setting

    return int(setting("RECENT_SUCCESS_HOURS", RECENT_SUCCESS_HOURS) * 3600)


def heat(source: str, model: str, outcome: str) -> Heat | None:
    """The record that keeps this model hot after ``outcome``, or None."""
    if outcome not in HOT_OUTCOMES or not source or not model:
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

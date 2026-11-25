"""The valid end of a stored model health cooldown under the current policy.

A health row holds one cause (``last_outcome``) and one end time. An own-failure
cause ends at ``updated_at`` plus its current cap (``outcome_cap``), so a row
written under an older curve, or one that kept a longer end from an older
cause, ends on time. A capacity cause keeps its stored end: a throttle can carry
a daily reset, and the row does not record which kind it was.
"""

from __future__ import annotations

from factory_quota_reset import CAPACITY_OUTCOMES
from factory_track_record_cooldown import LONG_OUTCOMES, TASK_OUTCOMES, outcome_cap

TABLE = "factory_model_health"


def end_sql(unavailable_cap: int | str | None = None, table: str = TABLE) -> str:
    """SQL for the clamped cooldown end of a ``factory_model_health`` row.

    ``unavailable_cap`` is a number or a bound parameter name such as
    ``:unavailable_cap``; by default the uncapped-model ceiling applies.
    """
    unavailable = outcome_cap("model_unavailable") if unavailable_cap is None else unavailable_cap
    cases = " ".join(
        f"WHEN '{name}' THEN {outcome_cap(name)}" for name in sorted(LONG_OUTCOMES | TASK_OUTCOMES)
    )
    capacity = ", ".join(f"'{name}'" for name in sorted(CAPACITY_OUTCOMES))
    return (
        f"CASE WHEN {table}.last_outcome IN ({capacity}) THEN {table}.cooldown_until"
        f" ELSE min({table}.cooldown_until, {table}.updated_at + CASE {table}.last_outcome"
        f" WHEN 'model_unavailable' THEN {unavailable} {cases}"
        f" ELSE {outcome_cap('')} END) END"
    )

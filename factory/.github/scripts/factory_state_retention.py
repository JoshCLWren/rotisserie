"""Bounded retention for raw model-attempt events, keeping per-model history.

Raw ``model_attempt`` events stay for ``RAW_DAYS``. Older ones move, in one
statement, into ``factory_model_attempt_daily``: one row per repository, UTC
day, source, model and outcome, with attempt counts and total duration. The
deleted rows and the added counts commit together, so a concurrent prune
cannot count a row twice or lose it.
"""

from __future__ import annotations

import re

RAW_DAYS = 120
BATCH = 1000

ROLLUP = """
WITH expired AS (
  SELECT id FROM public.factory_state_events
   WHERE event_type = 'model_attempt' AND source = 'github-actions'
     AND detail->>'repo' = $1
     AND created_at < now() - $2::int * interval '1 day'
   ORDER BY id LIMIT $3
), moved AS (
  DELETE FROM public.factory_state_events AS events USING expired
   WHERE events.id = expired.id
  RETURNING events.created_at, events.detail
), counted AS (
  INSERT INTO public.factory_model_attempt_daily AS daily
    (repo, day, source, model, outcome_class, attempts, timed_attempts, duration_ms)
  SELECT $1, (created_at AT TIME ZONE 'UTC')::date,
         coalesce(detail->>'source', ''), coalesce(detail->>'model', ''),
         coalesce(detail->>'outcome_class', 'unknown'), count(*),
         count(*) FILTER (WHERE jsonb_typeof(detail->'duration_ms') = 'number'),
         coalesce(sum((detail->>'duration_ms')::numeric)
                  FILTER (WHERE jsonb_typeof(detail->'duration_ms') = 'number'), 0)::bigint
    FROM moved GROUP BY 2, 3, 4, 5
  ON CONFLICT (repo, day, source, model, outcome_class) DO UPDATE SET
    attempts = daily.attempts + EXCLUDED.attempts,
    timed_attempts = daily.timed_attempts + EXCLUDED.timed_attempts,
    duration_ms = daily.duration_ms + EXCLUDED.duration_ms
  RETURNING 1
) SELECT count(*) AS moved FROM moved
"""


def rollup_model_attempts(repo: str, *, days: int = RAW_DAYS, batch: int = BATCH) -> int:
    """Move one bounded batch of expired attempts into daily counts."""
    from factory_state_cache import _run

    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or not 1 <= batch <= 1000 or days < 30:
        raise ValueError("invalid retention scope")
    return int(_run(ROLLUP, repo, days, batch, one=True)["moved"])


def prune_model_attempts(repo: str, *, rounds: int = 5) -> int:
    """Run bounded batches; skip until bootstrap has created the daily table."""
    import asyncpg

    moved = 0
    for _ in range(rounds):
        try:
            count = rollup_model_attempts(repo)
        except asyncpg.UndefinedTableError:
            return moved
        moved += count
        if count < BATCH:
            break
    return moved

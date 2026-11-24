"""Factory state connection and bounded historical cleanup.

GitHub remains authoritative for claims, reviews, checks, and merges.
The database is isolated in factory_state with a scoped role.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Any

from factory_state_metrics import connect as measured_connect
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from factory_adopter import repository
from factory_state_ledger_core import prefix_pattern


def _dsn() -> str:
    path = os.environ.get("FACTORY_STATE_DSN_FILE", "")
    if not path:
        raise RuntimeError("factory cache is not configured")
    raw = Path(path).read_text(encoding="utf-8").strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in ("postgresql", "postgres"):
        raise ValueError("factory state connection must be a Postgres URL")
    if not parsed.hostname or not parsed.username:
        raise ValueError("factory state connection is missing its host or role")
    # Neon's libpq URL can include channel_binding, which asyncpg does not
    # accept. TLS stays required via sslmode=require in the original DSN.
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
             if key != "channel_binding"]
    return urlunsplit(parsed._replace(query=urlencode(query)))


async def _query(sql: str, *args: Any, one: bool = False) -> Any:
    conn = await measured_connect(dsn=_dsn(), timeout=8, command_timeout=8)
    try:
        if one:
            return await conn.fetchrow(sql, *args)
        return await conn.execute(sql, *args)
    finally:
        await conn.close()


def _run(sql: str, *args: Any, one: bool = False) -> Any:
    return asyncio.run(_query(sql, *args, one=one))


def _prune_obsolete_discovery(repo: str, batch_size: int = 200) -> int:
    """Remove retired discovery families only, with a bounded migration batch."""
    if not 1 <= batch_size <= 1000 or not re.fullmatch(r'[\w.-]+/[\w.-]+', repo):
        raise ValueError('invalid cleanup scope or batch size')
    row = _run(
        """WITH retired AS (
             SELECT cache_key FROM public.factory_state_kv
             WHERE (cache_key LIKE $1 OR cache_key LIKE $2 OR cache_key=ANY($3::text[]))
               AND (refresh_until IS NULL OR refresh_until<now())
             LIMIT $4
           ), removed AS (
             DELETE FROM public.factory_state_kv AS cache USING retired
             WHERE cache.cache_key=retired.cache_key RETURNING cache.cache_key
           ) SELECT count(*) AS deleted FROM removed""",
        prefix_pattern(f'factory-github-get-v1:{repo}:'),
        prefix_pattern(f'factory-projection-v1:{repo}:'),
        [f'{repo}:open-issues', f'{repo}:open-prs'], batch_size, one=True,
    )
    return int(row['deleted'])


def _prune_retired_events(repo: str, batch_size: int = 1000) -> int:
    """Prune retired repository-owned event noise, never unscoped histories.

    Older worker events may lack a repository identifier, so age alone cannot
    authorize their deletion. New lifecycle events include verified repository
    provenance and may expire after 30 days; transactional leases, provider
    reservations, worker metrics, model cooldowns and retries remain intact.
    """
    if not 1 <= batch_size <= 1000 or not re.fullmatch(r'[\w.-]+/[\w.-]+', repo):
        raise ValueError('invalid cleanup scope or batch size')
    github_prefix = f'factory-github-get-v1:{repo}:'
    projection_prefix = f'factory-projection-v1:{repo}:'
    retired_lists = [f'{repo}:open-issues', f'{repo}:open-prs']
    row = _run(
        """WITH retired AS (
             SELECT id FROM public.factory_state_events
              WHERE (event_type='cache_refresh'
                     AND (left(detail->>'key', length($1))=$1
                          OR left(detail->>'key', length($2))=$2
                          OR detail->>'key'=ANY($3::text[])))
                 OR (event_type='target_reconciled'
                     AND left(detail->>'target', length($2))=$2)
                 OR (created_at < now() - interval '30 days'
                     AND (detail->>'repo'=$4 OR detail->>'repository'=$4)
                     AND event_type IN ('cache_refresh',
                                        'target_reconciled', 'target_changed',
                                        'worker_assigned', 'worker_started',
                                        'worker_heartbeat', 'worker_outcome_signal',
                                        'worker_released', 'worker_failed',
                                        'worker_completed', 'worker_idle'))
              ORDER BY id LIMIT $5
           ), removed AS (
             DELETE FROM public.factory_state_events AS events USING retired
              WHERE events.id=retired.id RETURNING events.id
           ) SELECT count(*) AS deleted FROM removed""",
        github_prefix, projection_prefix, retired_lists, repo, batch_size, one=True,
    )
    return int(row['deleted'])


def _prune_retired_check_projection(repo: str, batch_size: int = 250) -> int:
    """Retire advisory check observations after Actions became authoritative.

    The production writer was removed in #5226. Retention is deliberately
    repository-fenced and bounded; never touch exact-head retry coordination.
    """
    if not 1 <= batch_size <= 1000 or not re.fullmatch(r'[\w.-]+/[\w.-]+', repo):
        raise ValueError('invalid cleanup scope or batch size')
    row = _run(
        """WITH retired AS (
             SELECT pr_number, head_sha, check_name
               FROM public.factory_check_projection
              WHERE repo=$1
              ORDER BY pr_number, head_sha, check_name LIMIT $2
           ), removed AS (
             DELETE FROM public.factory_check_projection AS checks USING retired
              WHERE checks.repo=$1
                AND checks.pr_number=retired.pr_number
                AND checks.head_sha=retired.head_sha
                AND checks.check_name=retired.check_name
              RETURNING checks.pr_number
           ) SELECT count(*) AS deleted FROM removed""",
        repo, batch_size, one=True,
    )
    return int(row['deleted'])


def main(argv: list[str]) -> int:
    if argv != ["prune-obsolete"]:
        print("usage: factory_state_cache.py prune-obsolete", file=sys.stderr)
        return 2
    if not os.environ.get("FACTORY_STATE_DSN_FILE"):
        raise RuntimeError("cache cleanup requires trusted Neon credentials")
    repo = repository()
    retired_kv = 0
    retired_events = 0
    for _ in range(5):
        count = _prune_obsolete_discovery(repo)
        retired_kv += count
        if count < 200:
            break
    for _ in range(5):
        count = _prune_retired_events(repo)
        retired_events += count
        if count < 1000:
            break
    retired_checks = 0
    for _ in range(5):
        count = _prune_retired_check_projection(repo)
        retired_checks += count
        if count < 250:
            break
    from factory_state_retention import prune_model_attempts
    rolled = prune_model_attempts(repo)
    print(f"[factory-state] pruned {retired_kv} discovery rows, {retired_events} obsolete events, and {retired_checks} retired check observations; rolled {rolled} old model attempts into daily counts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

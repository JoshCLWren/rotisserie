"""Scoped factory state caching and historical cleanup.

GitHub remains authoritative for claims, reviews, checks, and merges.
The database is isolated in factory_state with a scoped role.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from factory_state_metrics import connect as measured_connect
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


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


def _payload(value: Any) -> list[dict[str, Any]] | None:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, list) else None


def _read(key: str) -> tuple[list[dict[str, Any]] | None, bool]:
    query = "SELECT payload, expires_at FROM public.factory_state_kv WHERE cache_key=$1"
    row = _run(query, key, one=True)
    if row is None:
        return None, False
    data = _payload(row["payload"])
    fresh = bool(data is not None and row["expires_at"] > datetime.now(timezone.utc))
    return data, fresh


def _claim(key: str, owner: str) -> bool:
    row = _run(
        """
        INSERT INTO public.factory_state_kv
          (cache_key, payload, fetched_at, expires_at, refresh_owner, refresh_until)
        VALUES ($1, '[]'::jsonb, to_timestamp(0), to_timestamp(0),
                $2, now() + interval '75 seconds')
        ON CONFLICT(cache_key) DO UPDATE
          SET refresh_owner=EXCLUDED.refresh_owner,
              refresh_until=EXCLUDED.refresh_until
          WHERE factory_state_kv.expires_at <= now()
            AND (factory_state_kv.refresh_until IS NULL
                 OR factory_state_kv.refresh_until < now())
        RETURNING cache_key
        """,
        key, owner, one=True,
    )
    return row is not None


def _publish(key: str, owner: str, data: list[dict[str, Any]], ttl_seconds: int) -> None:
    # A lock that expired or changed owner may not publish an older snapshot.
    # Updating an evidence cache does not need a second append-only event write.
    _run(
        """UPDATE public.factory_state_kv
              SET payload=$3::jsonb, fetched_at=now(),
                  expires_at=now() + $4::int * interval '1 second',
                  refresh_owner=NULL, refresh_until=NULL, updated_at=now()
            WHERE cache_key=$1 AND refresh_owner=$2""",
        key, owner, json.dumps(data), ttl_seconds,
    )


def _refresh_without_rewrite(key: str, owner: str, ttl_seconds: int, records: int) -> None:
    """Extend freshness without retransmitting or rewriting unchanged TOAST."""
    _run(
        """UPDATE public.factory_state_kv
              SET fetched_at=now(), expires_at=now() + $3::int * interval '1 second',
                  refresh_owner=NULL, refresh_until=NULL, updated_at=now()
            WHERE cache_key=$1 AND refresh_owner=$2""",
        key, owner, ttl_seconds,
    )


def _prune_obsolete_discovery(repo: str, batch_size: int = 200) -> int:
    """Remove retired discovery families only, with a bounded migration batch."""
    if not 1 <= batch_size <= 1000 or not re.fullmatch(r'[\w.-]+/[\w.-]+', repo):
        raise ValueError('invalid cleanup scope or batch size')
    row = _run(
        """WITH retired AS (
             SELECT cache_key FROM public.factory_state_kv
             WHERE (left(cache_key, length($1))=$1 OR left(cache_key, length($2))=$2 OR cache_key=ANY($3::text[]))
               AND (refresh_until IS NULL OR refresh_until<now())
             LIMIT $4
           ), removed AS (
             DELETE FROM public.factory_state_kv AS cache USING retired
             WHERE cache.cache_key=retired.cache_key RETURNING cache.cache_key
           ) SELECT count(*) AS deleted FROM removed""",
        f'factory-github-get-v1:{repo}:', f'factory-projection-v1:{repo}:',
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


def _release(key: str, owner: str) -> None:
    _run(
        """
        UPDATE public.factory_state_kv
           SET refresh_owner=NULL, refresh_until=NULL
         WHERE cache_key=$1 AND refresh_owner=$2
        """,
        key, owner,
    )


def _recent_contended(key: str, max_stale_seconds: int) -> list[dict[str, Any]] | None:
    """Share a bounded expired discovery result only while its refresh is owned."""
    row = _run(
        """
        SELECT payload FROM public.factory_state_kv
        WHERE cache_key=$1 AND refresh_until > now()
          AND expires_at > now() - $2::int * interval '1 second'
        """,
        key, max_stale_seconds, one=True,
    )
    return _payload(row["payload"]) if row is not None else None


def cached_json(
    key: str,
    fetch: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
    *,
    ttl_seconds: int = 45,
) -> list[dict[str, Any]]:
    """Get a recent shared snapshot, or fetch once under a short DB refresh lease.

    The caller's fetch function is authoritative, never a model. On database
    errors, use the original live GitHub path. A concurrent cold cache fill
    waits briefly; if it cannot observe a fresh snapshot it fetches live.
    """
    if not 1 <= ttl_seconds <= 3600:
        raise ValueError("invalid factory cache TTL")
    if not os.environ.get("FACTORY_STATE_DSN_FILE"):
        return fetch([])
    try:
        previous, fresh = _read(key)
    except Exception as error:
        print(f"[factory-state] cache read unavailable, fetching GitHub live ({type(error).__name__})", file=sys.stderr)
        return fetch([])
    if fresh:
        print(f"[factory-state] shared cache hit for {key}", file=sys.stderr)
        return previous or []

    owner = f"{os.environ.get('GITHUB_RUN_ID', 'local')}-{uuid.uuid4().hex}"
    try:
        claimed = _claim(key, owner)
    except Exception as error:
        print(f"[factory-state] cache claim unavailable, fetching GitHub live ({type(error).__name__})", file=sys.stderr)
        return fetch(previous or [])

    if not claimed:
        # Another trusted runner holds the refresh lease. Safe discovery
        # callers may share a briefly stale record while it is actively
        # refreshed; no ownership, checks, or merge authorization uses this.
        # Never serve an expired cold/invalid cache or an abandoned lease.
        if previous is not None:
            try:
                shared = _recent_contended(key, min(ttl_seconds, 120))
            except Exception:
                shared = None
            if shared is not None:
                print(f"[factory-state] shared in-flight refresh for {key}", file=sys.stderr)
                return shared

        # A cold cache has no safe stale result. Give the single refresh
        # owner time to publish rather than launching competing GitHub scans
        # after only three seconds. On a genuine outage, retain live fallback.
        for _ in range(15):
            time.sleep(1)
            try:
                data, fresh = _read(key)
            except Exception:
                break
            if fresh:
                return data or []
        print(f"[factory-state] refresh wait exceeded for {key}, fetching GitHub live", file=sys.stderr)
        return fetch(previous or [])

    try:
        result = fetch(previous or [])
        if not isinstance(result, list):
            raise ValueError(f"invalid GitHub discovery payload for {key}")
    except BaseException:
        try:
            _release(key, owner)
        except Exception:
            pass
        raise

    unchanged = previous is not None and result == previous
    try:
        if unchanged:
            _refresh_without_rewrite(key, owner, ttl_seconds, len(result))
        else:
            _publish(key, owner, result, ttl_seconds)
    except Exception as error:
        print(f"[factory-state] cache publication unavailable ({type(error).__name__})", file=sys.stderr)
        try:
            _release(key, owner)
        except Exception:
            pass
    else:
        label = "extended unchanged" if unchanged else "refreshed"
        print(f"[factory-state] {label} {key}: {len(result)} records", file=sys.stderr)
    return result


def main(argv: list[str]) -> int:
    if argv != ["prune-obsolete"]:
        print("usage: factory_state_cache.py prune-obsolete", file=sys.stderr)
        return 2
    if not os.environ.get("FACTORY_STATE_DSN_FILE"):
        raise RuntimeError("cache cleanup requires trusted Neon credentials")
    repo = os.environ.get("GITHUB_REPOSITORY", "JoshCLWren/comic-pile")
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
    print(f"[factory-state] pruned {retired_kv} discovery rows, {retired_events} obsolete events, and {retired_checks} retired check observations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

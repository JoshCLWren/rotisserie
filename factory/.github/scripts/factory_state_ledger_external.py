"""Fenced external agent claims on the factory's shared Neon target keys."""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from factory_state_metrics import connect as measured_connect

from factory_state_cache import _dsn
from factory_state_ledger_core import LEASE_PREFIX, _target_key, enabled


# External agents share the same repository advisory lock and target keys as
# factory workers. They do NOT impersonate numeric worker slots or spend model
# provider reservations. An expired external claim is intentionally still a
# hold until its identity-fenced release or verified operator reconciliation.
EXTERNAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Review is offered since #191: the trusted bridge records the outside
# reviewer's declared model, so the merge gate can check its independence.
EXTERNAL_ACTIONS = frozenset({
    "implement", "review", "review-findings", "ci-repair", "merge-conflict",
})
EXTERNAL_WIP_ACTIONS = frozenset({
    "implement", "review-findings", "ci-repair", "merge-conflict",
})


def _external_request(repo: str, actor: str, client: str, numbers: list[int]) -> list[int]:
    if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
            or not EXTERNAL_ID_RE.fullmatch(actor)
            or not EXTERNAL_ID_RE.fullmatch(client)):
        raise ValueError("invalid external actor, client or repository")
    if not numbers or len(numbers) > 2 or any(type(n) is not int or n < 1 for n in numbers):
        raise ValueError("invalid external lease targets")
    if len(set(numbers)) != len(numbers):
        raise ValueError("external lease targets must be distinct")
    return sorted(numbers)


def _external_key(repo: str, token: str) -> str:
    return f"{LEASE_PREFIX}{repo}:external:{token}"


async def _reserve_external(
    repo: str, actor: str, client: str, numbers: list[int], *,
    target: str, action: str, head: str | None, ttl: int,
    observed_wip: int, wip_limit: int, offer_at: str, wip_exempt: bool,
) -> dict[str, Any]:
    token = str(uuid.uuid4())
    keys = [_target_key(repo, n) for n in numbers]
    conn = await measured_connect(dsn=_dsn(), timeout=8, command_timeout=12)
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", LEASE_PREFIX + repo
            )
            # An expired factory lease frees its target, as factory intake
            # already treats it. An expired external row is NOT reusable: an
            # elapsed timestamp never proves an outside agent stopped.
            await conn.execute(
                """DELETE FROM public.factory_state_kv
                   WHERE cache_key = ANY($1::text[]) AND expires_at <= now()
                     AND coalesce(payload->0->>'external', '') <> 'true'""",
                keys,
            )
            busy = await conn.fetch(
                "SELECT cache_key FROM public.factory_state_kv WHERE cache_key = ANY($1::text[])",
                keys,
            )
            if busy:
                return {
                    "status": "conflict", "reason": "existing_or_unreconciled_lease",
                    "targets": sorted(str(row["cache_key"]) for row in busy),
                    "retry": "refresh_frontier",
                }
            count = await conn.fetchval(
                """SELECT count(*) FROM public.factory_state_kv
                   WHERE starts_with(cache_key, $1)
                     AND payload->0->>'wip' = 'true'""",
                f"{LEASE_PREFIX}{repo}:external:",
            )
            # Factory workers that claimed anything since shortly before the
            # offer snapshot may hold a slot the snapshot could not see yet
            # (their GitHub owner labels are written after the Neon claim).
            # Counting them can only make outside intake stricter.
            newer = await conn.fetchval(
                """SELECT count(*) FROM public.factory_state_kv
                   WHERE starts_with(cache_key, $1) AND expires_at > now()
                     AND fetched_at > $2::timestamptz - interval '5 minutes'""",
                f"{LEASE_PREFIX}{repo}:worker:", datetime.fromisoformat(offer_at),
            )
            if (action == "implement" and not wip_exempt
                    and observed_wip + int(count or 0) + int(newer or 0) >= wip_limit):
                return {
                    "status": "conflict", "reason": "wip_capacity_changed",
                    "retry": "refresh_frontier",
                }
            claim = {
                "claim_id": token,
                "operation": f"external:{actor}/{client}",
                "external": True,
                "actor": actor,
                "client": client,
                "targets": numbers,
                "target": target,
                "action": action,
                "head": head,
                "wip": action in EXTERNAL_WIP_ACTIONS,
                "offer_at": offer_at,
            }
            for key in [*keys, _external_key(repo, token)]:
                await conn.execute(
                    """INSERT INTO public.factory_state_kv
                       (cache_key, payload, fetched_at, expires_at, updated_at)
                       VALUES ($1, $2::jsonb, now(),
                               now() + $3::int * interval '1 second', now())
                       ON CONFLICT (cache_key) DO NOTHING""",
                    key, json.dumps([claim]), ttl,
                )
                # CAS never overwrites a claim, including an expired one.
                matches = await conn.fetchval(
                    """SELECT count(*) FROM public.factory_state_kv
                       WHERE cache_key=$1 AND payload->0->>'claim_id'=$2""",
                    key, token,
                )
                if matches != 1:
                    raise RuntimeError("external lease CAS lost under repository lock")
            expires_at = await conn.fetchval(
                "SELECT expires_at FROM public.factory_state_kv WHERE cache_key=$1",
                _external_key(repo, token),
            )
            await conn.execute(
                """INSERT INTO public.factory_state_events
                   (source,event_type,subject_type,subject_number,detail)
                   VALUES ('rotisserie-external','external_reserved','target',$1,
                           $2::jsonb)""",
                numbers[-1], json.dumps({**claim, "repo": repo}),
            )
        return {
            "status": "reserved", "claim_id": token, "actor": actor,
            "client": client, "target": target, "action": action,
            "head": head, "targets": numbers,
            "expires_at": expires_at.isoformat(),
        }
    finally:
        await conn.close()


def reserve_external(
    repo: str, actor: str, client: str, numbers: list[int], *,
    target: str, action: str, head: str | None, ttl: int = 1800,
    observed_wip: int, wip_limit: int, offer_at: str, wip_exempt: bool = False,
) -> dict[str, Any]:
    """Atomically fence one external claim using the factory's existing keys.

    ``wip_exempt`` carries the work policy's own WIP bypass for urgent issues.
    """
    numbers = _external_request(repo, actor, client, numbers)
    if (action not in EXTERNAL_ACTIONS or not re.fullmatch(r"(issue|pr):[1-9][0-9]*", target)
            or (head is not None and not re.fullmatch(r"[0-9a-f]{40}", head))
            or not 300 <= ttl <= 3600 or not 1 <= wip_limit <= 10000
            or not 0 <= observed_wip <= 10000 or not offer_at):
        raise ValueError("invalid external reservation request")
    try:
        offered = datetime.fromisoformat(offer_at)
    except ValueError:
        raise ValueError("invalid external offer snapshot time") from None
    if offered.tzinfo is None:
        raise ValueError("invalid external offer snapshot time")
    if not enabled():
        raise RuntimeError("trusted external claims require Neon")
    return asyncio.run(_reserve_external(
        repo, actor, client, numbers, target=target, action=action,
        head=head, ttl=ttl, observed_wip=observed_wip, wip_limit=wip_limit,
        offer_at=offer_at, wip_exempt=wip_exempt,
    ))


async def _finish_external(
    repo: str, actor: str, client: str, token: str, *,
    operation: str, evidence: str, ttl: int,
    verify: Callable[[dict[str, Any]], None] | None,
    evidence_target: int | None = None,
) -> dict[str, Any]:
    key = _external_key(repo, token)
    conn = await measured_connect(dsn=_dsn(), timeout=8, command_timeout=12)
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", LEASE_PREFIX + repo
            )
            row = await conn.fetchrow(
                "SELECT payload, expires_at FROM public.factory_state_kv WHERE cache_key=$1",
                key,
            )
            if not row:
                return {"status": "conflict", "reason": "claim_missing_or_replaced"}
            payload = row["payload"]
            value = json.loads(payload) if isinstance(payload, str) else payload
            record = value[0] if isinstance(value, list) and value else {}
            if (record.get("claim_id") != token or record.get("actor") != actor
                    or record.get("client") != client or record.get("external") is not True):
                return {"status": "conflict", "reason": "stale_or_wrong_client"}
            numbers = record["targets"]
            keys = [_target_key(repo, n) for n in numbers]
            confirmed = await conn.fetch(
                """SELECT cache_key, payload->0->>'claim_id' AS id
                   FROM public.factory_state_kv WHERE cache_key=ANY($1::text[])""",
                keys,
            )
            if len(confirmed) != len(keys) or any(row["id"] != token for row in confirmed):
                return {"status": "conflict", "reason": "stale_target_fence"}
            if evidence_target is not None and evidence_target not in numbers:
                # The handed-off PR is outside this claim; it must not be held
                # by any other worker, operation or outside agent right now.
                held = await conn.fetchval(
                    "SELECT 1 FROM public.factory_state_kv WHERE cache_key=$1",
                    _target_key(repo, evidence_target),
                )
                if held:
                    return {"status": "conflict", "reason": "evidence_target_held"}
            if verify is not None:
                # Runs while this transaction holds the repository lock and
                # the verified fences: no other claim, revoke or factory
                # acquisition can interleave, and an exception here rolls
                # back, leaving the claim held rather than silently lost.
                verify({
                    "target": record["target"], "action": record["action"],
                    "head": record.get("head"), "targets": list(numbers),
                    "client": client, "actor": actor, "claim_id": token,
                })
            if operation == "renew":
                if row["expires_at"] <= datetime.now(timezone.utc):
                    return {"status": "conflict", "reason": "lease_expired_liveness_unknown"}
                for target_key in [key, *keys]:
                    await conn.execute(
                        """UPDATE public.factory_state_kv
                           SET expires_at=now()+$3::int*interval '1 second',
                               updated_at=now()
                           WHERE cache_key=$1 AND payload->0->>'claim_id'=$2""",
                        target_key, token, ttl,
                    )
                expires_at = await conn.fetchval(
                    "SELECT expires_at FROM public.factory_state_kv WHERE cache_key=$1", key
                )
                event = "external_renewed"
            else:
                # Handoff is evidence, not approval: independent review, exact-head
                # CI, merge authorization and deployment remain controller gates.
                await conn.execute(
                    """DELETE FROM public.factory_state_kv
                       WHERE cache_key=ANY($1::text[])
                         AND payload->0->>'claim_id'=$2""",
                    [key, *keys], token,
                )
                expires_at = None
                event = "external_handoff" if operation == "handoff" else "external_released"
            await conn.execute(
                """INSERT INTO public.factory_state_events
                   (source,event_type,subject_type,subject_number,detail)
                   VALUES ('rotisserie-external',$1,'target',$2,$3::jsonb)""",
                event, numbers[-1], json.dumps({
                    "repo": repo, "claim_id": token, "actor": actor,
                    "client": client, "target": record["target"],
                    "action": record["action"], "head": record.get("head"),
                    "evidence": evidence,
                }),
            )
        return {
            "status": "renewed" if operation == "renew" else "released",
            "claim_id": token,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "handoff_recorded": operation == "handoff",
        }
    finally:
        await conn.close()


def external_lease_action(
    repo: str, actor: str, client: str, claim_id: str, *,
    operation: str, evidence: str = "", ttl: int = 1800,
    verify: Callable[[dict[str, Any]], None] | None = None,
    evidence_target: int | None = None,
) -> dict[str, Any]:
    """Renew, release or hand off one claim, fenced by UUID, actor and client.

    ``verify`` (handoff only) checks the evidence against the claimed task and
    projects GitHub review stage inside the locked transaction.
    """
    _external_request(repo, actor, client, [1])
    try:
        uuid.UUID(claim_id)
    except (ValueError, AttributeError):
        raise ValueError("invalid claim ID") from None
    if operation not in {"renew", "release", "handoff"} or not 300 <= ttl <= 3600:
        raise ValueError("invalid external lease operation")
    if operation == "handoff" and not evidence.strip():
        raise ValueError("handoff requires durable evidence")
    if (verify is not None or evidence_target is not None) and operation != "handoff":
        raise ValueError("only a handoff verifies evidence")
    if evidence_target is not None and (type(evidence_target) is not int or evidence_target < 1):
        raise ValueError("invalid handoff evidence target")
    if len(evidence) > 2000 or any(c in evidence for c in "\r\n"):
        raise ValueError("invalid external evidence")
    if not enabled():
        raise RuntimeError("trusted external claims require Neon")
    return asyncio.run(_finish_external(
        repo, actor, client, claim_id, operation=operation, evidence=evidence, ttl=ttl,
        verify=verify, evidence_target=evidence_target,
    ))


def external_wip_count(repo: str) -> int:
    """Count all external WIP claims, even expired/unreconciled ones, once each."""
    if not enabled():
        raise RuntimeError("Neon is not configured")

    async def _read() -> int:
        conn = await measured_connect(dsn=_dsn(), timeout=8, command_timeout=8)
        try:
            result = await conn.fetchval(
                """SELECT count(*) FROM public.factory_state_kv
                   WHERE starts_with(cache_key, $1)
                     AND payload->0->>'wip'='true'""",
                f"{LEASE_PREFIX}{repo}:external:",
            )
            return int(result or 0)
        finally:
            await conn.close()

    return asyncio.run(_read())


async def _external_holder(repo: str, number: int) -> dict[str, Any] | None:
    conn = await measured_connect(dsn=_dsn(), timeout=8, command_timeout=8)
    try:
        row = await conn.fetchrow(
            "SELECT payload, expires_at FROM public.factory_state_kv WHERE cache_key=$1",
            _target_key(repo, number),
        )
    finally:
        await conn.close()
    if not row:
        return None
    payload = row["payload"]
    value = json.loads(payload) if isinstance(payload, str) else payload
    record = value[0] if isinstance(value, list) and value else {}
    if not isinstance(record, dict) or record.get("external") is not True:
        return None
    expires_at = row["expires_at"]
    return {
        "claim_id": str(record.get("claim_id") or ""),
        "actor": str(record.get("actor") or ""),
        "client": str(record.get("client") or ""),
        "target": str(record.get("target") or ""),
        "action": str(record.get("action") or ""),
        "targets": [int(n) for n in record.get("targets") or [] if str(n).isdecimal()],
        "expires_at": expires_at.isoformat() if hasattr(expires_at, "isoformat") else str(expires_at),
    }


def external_holder(repo: str, number: int) -> dict[str, Any] | None:
    """Return the external claim holding one target key, or None."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or type(number) is not int or number < 1:
        raise ValueError("invalid external holder query")
    if not enabled():
        raise RuntimeError("Neon is not configured")
    return asyncio.run(_external_holder(repo, number))


async def _revoke_external(repo: str, operator: str, token: str, reason: str) -> dict[str, Any]:
    key = _external_key(repo, token)
    conn = await measured_connect(dsn=_dsn(), timeout=8, command_timeout=12)
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", LEASE_PREFIX + repo
            )
            row = await conn.fetchrow(
                "SELECT payload FROM public.factory_state_kv WHERE cache_key=$1", key
            )
            if not row:
                return {"status": "conflict", "reason": "claim_missing_or_replaced"}
            payload = row["payload"]
            value = json.loads(payload) if isinstance(payload, str) else payload
            record = value[0] if isinstance(value, list) and value else {}
            if record.get("claim_id") != token or record.get("external") is not True:
                return {"status": "conflict", "reason": "stale_claim"}
            numbers = [int(n) for n in record.get("targets") or []]
            keys = [key, *(_target_key(repo, n) for n in numbers)]
            # Fenced by the exact claim UUID: a newer claim on the same target
            # is never removed by revoking an older one.
            await conn.execute(
                """DELETE FROM public.factory_state_kv
                   WHERE cache_key=ANY($1::text[]) AND payload->0->>'claim_id'=$2""",
                keys, token,
            )
            await conn.execute(
                """INSERT INTO public.factory_state_events
                   (source,event_type,subject_type,subject_number,detail)
                   VALUES ('rotisserie-external','external_revoked','target',$1,$2::jsonb)""",
                numbers[-1] if numbers else 0, json.dumps({
                    "repo": repo, "claim_id": token, "operator": operator,
                    "actor": record.get("actor"), "client": record.get("client"),
                    "target": record.get("target"), "action": record.get("action"),
                    "reason": reason,
                }),
            )
        return {
            "status": "revoked", "claim_id": token, "targets": numbers,
            "client": record.get("client"), "actor": record.get("actor"),
        }
    finally:
        await conn.close()


def revoke_external(repo: str, operator: str, claim_id: str, *, reason: str) -> dict[str, Any]:
    """Operator-initiated, UUID-fenced removal of one external claim.

    Never automatic: only an explicit operator request (revoke or take-over)
    calls this, and the event log records who revoked whose claim and why.
    """
    _external_request(repo, operator, "operator", [1])
    try:
        uuid.UUID(claim_id)
    except (ValueError, AttributeError):
        raise ValueError("invalid claim ID") from None
    reason = reason.strip()
    if not reason or len(reason) > 500 or any(c in reason for c in "\r\n"):
        raise ValueError("revoking a claim needs a one-line reason")
    if not enabled():
        raise RuntimeError("trusted external claims require Neon")
    return asyncio.run(_revoke_external(repo, operator, claim_id, reason))

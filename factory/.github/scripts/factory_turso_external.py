"""Fenced external agent claims on the libSQL store, same keys and results as the retired Neon ledger.

The handoff ``verify`` callback calls GitHub, so it runs before the write,
never inside a transaction. The write then re-checks, inside one batch, that
this exact claim still holds every target; if anything changed meanwhile the
handoff is refused and the claim is left as it was.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import factory_turso_core as core
from factory_libsql import placeholders
from factory_state_ledger_core import LEASE_PREFIX, _target_key
from factory_state_ledger_external import EXTERNAL_ACTIONS, EXTERNAL_WIP_ACTIONS, _external_request

ORIGIN = "rotisserie-external"


def _external_key(repo: str, token: str) -> str:
    return f"{LEASE_PREFIX}{repo}:external:{token}"


def _iso(epoch: Any) -> str:
    return datetime.fromtimestamp(int(epoch), UTC).isoformat()


def _require() -> None:
    if not core.enabled():
        raise RuntimeError("trusted external claims require the factory store")


def reserve_external(
    repo: str,
    actor: str,
    client: str,
    numbers: list[int],
    *,
    target: str,
    action: str,
    head: str | None,
    ttl: int = 1800,
    observed_wip: int,
    wip_limit: int,
    offer_at: str,
    wip_exempt: bool = False,
) -> dict[str, Any]:
    """Atomically fence one external claim on the factory's target keys."""
    numbers = _external_request(repo, actor, client, numbers)
    if (
        action not in EXTERNAL_ACTIONS
        or not re.fullmatch(r"(issue|pr):[1-9][0-9]*", target)
        or (head is not None and not re.fullmatch(r"[0-9a-f]{40}", head))
        or not 300 <= ttl <= 3600
        or not 1 <= wip_limit <= 10000
        or not 0 <= observed_wip <= 10000
        or not offer_at
    ):
        raise ValueError("invalid external reservation request")
    try:
        offered = datetime.fromisoformat(offer_at)
    except ValueError:
        raise ValueError("invalid external offer snapshot time") from None
    if offered.tzinfo is None:
        raise ValueError("invalid external offer snapshot time")
    _require()
    db = core.store()
    token = str(uuid.uuid4())
    keys = [_target_key(repo, n) for n in numbers]
    anchor = _external_key(repo, token)
    marks, key_params = placeholders("tk", keys)
    capacity = action == "implement" and not wip_exempt
    guard = (
        f"NOT EXISTS (SELECT 1 FROM factory_lease WHERE lease_key IN ({marks})"
        " AND (expires_at > unixepoch() OR external = 1))"
        " AND (:capacity = 0 OR :observed"
        " + (SELECT count(*) FROM factory_lease WHERE repo = :repo AND kind = 'external' AND wip = 1)"
        " + (SELECT count(*) FROM factory_lease WHERE repo = :repo AND kind = 'worker'"
        " AND expires_at > unixepoch() AND acquired_at > :offer - 300) < :limit)",
        {
            **key_params,
            "capacity": int(capacity),
            "observed": observed_wip,
            "offer": int(offered.timestamp()),
            "limit": wip_limit,
        },
    )
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
    detail = {k: claim[k] for k in ("actor", "client", "target", "action", "head", "offer_at")}
    common = {
        "repo": repo,
        "token": token,
        "ttl": ttl,
        "operation": claim["operation"],
        "external": True,
        "wip": claim["wip"],
        "targets": numbers,
        "detail": detail,
    }
    mine = core.held_guard(anchor, token)
    statements = [core.lease_upsert(key=anchor, kind="external", guard=guard, **common)]
    statements += [
        core.lease_upsert(key=key, kind="target", number=n, guard=mine, **common)
        for key, n in zip(keys, numbers, strict=True)
    ]
    statements.append(
        core.event_insert(
            repo,
            ORIGIN,
            "external_reserved",
            {**claim, "event_id": f"external-reserved:{token}"},
            subject_type="target",
            subject_number=numbers[-1],
            guard=mine,
        )
    )
    core.reconciled_batch(db, statements, lambda: core.claim_of(db, anchor) == token)
    rows = db.read(
        "SELECT expires_at FROM factory_lease WHERE lease_key = :k AND claim_id = :t",
        {"k": anchor, "t": token},
    )
    if not rows:
        held = db.read(
            f"SELECT lease_key FROM factory_lease WHERE lease_key IN ({marks})", key_params
        )
        if held:
            return {
                "status": "conflict",
                "reason": "existing_or_unreconciled_lease",
                "targets": sorted(str(r["lease_key"]) for r in held),
                "retry": "refresh_frontier",
            }
        return {"status": "conflict", "reason": "wip_capacity_changed", "retry": "refresh_frontier"}
    return {
        "status": "reserved",
        "claim_id": token,
        "actor": actor,
        "client": client,
        "target": target,
        "action": action,
        "head": head,
        "targets": numbers,
        "expires_at": _iso(rows[0]["expires_at"]),
    }


def _claim(db: core.Store, repo: str, token: str) -> dict[str, Any] | None:
    rows = db.read(
        "SELECT * FROM factory_lease WHERE lease_key = :k", {"k": _external_key(repo, token)}
    )
    if not rows:
        return None
    row = rows[0]
    return {
        **json.loads(row["detail"]),
        "targets": json.loads(row["targets"]),
        "claim_id": row["claim_id"],
        "external": bool(row["external"]),
        "expires_at": int(row["expires_at"]),
    }


def _fence(
    repo: str, token: str, numbers: list[int], *, active: bool
) -> tuple[str, dict[str, Any]]:
    """True while this claim's own row and every target row still carry its UUID."""
    marks, params = placeholders("fk", [_target_key(repo, n) for n in numbers])
    return (
        "EXISTS (SELECT 1 FROM factory_lease WHERE lease_key = :f_key AND claim_id = :f_token"
        " AND external = 1 AND (:f_active = 0 OR expires_at > unixepoch()))"
        f" AND (SELECT count(*) FROM factory_lease WHERE lease_key IN ({marks})"
        " AND claim_id = :f_token) = :f_count",
        {
            **params,
            "f_key": _external_key(repo, token),
            "f_token": token,
            "f_active": int(active),
            "f_count": len(numbers),
        },
    )


PENDING_SECONDS = 600


def _fence_pending(
    db: core.Store, repo: str, token: str, fence: tuple[str, dict[str, Any]]
) -> str | None:
    """Mark the claim handoff-pending with a fresh nonce; one handoff verifies at a time.

    The mark expires, so a handoff that dies during verification never wedges
    the claim. GitHub verification then runs with no transaction open.
    """
    nonce = str(uuid.uuid4())
    (rows,) = db.batch(
        [
            (
                "UPDATE factory_lease SET detail = json_set(detail, '$.pending', :nonce,"
                " '$.pending_until', unixepoch() + :hold) WHERE lease_key = :f_key AND claim_id = :f_token"
                " AND (detail ->> 'pending' IS NULL OR detail ->> 'pending_until' <= unixepoch())"
                f" AND {fence[0]} RETURNING 1 AS x",
                {**fence[1], "nonce": nonce, "hold": PENDING_SECONDS},
            )
        ]
    )
    return nonce if rows else None


def _clear_pending(db: core.Store, repo: str, token: str, nonce: str) -> None:
    db.batch(
        [
            (
                "UPDATE factory_lease SET detail = json_remove(detail, '$.pending', '$.pending_until')"
                " WHERE lease_key = :k AND claim_id = :t AND detail ->> 'pending' = :n",
                {"k": _external_key(repo, token), "t": token, "n": nonce},
            )
        ]
    )


def external_lease_action(
    repo: str,
    actor: str,
    client: str,
    claim_id: str,
    *,
    operation: str,
    evidence: str = "",
    ttl: int = 1800,
    verify: Callable[[dict[str, Any]], None] | None = None,
    evidence_target: int | None = None,
) -> dict[str, Any]:
    """Renew, release or hand off one claim, fenced by UUID, actor and client."""
    _external_request(repo, actor, client, [1])
    try:
        uuid.UUID(claim_id)
    except ValueError, AttributeError:
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
    _require()
    db = core.store()
    record = _claim(db, repo, claim_id)
    if record is None:
        return {"status": "conflict", "reason": "claim_missing_or_replaced"}
    if (
        record.get("actor") != actor
        or record.get("client") != client
        or record.get("external") is not True
    ):
        return {"status": "conflict", "reason": "stale_or_wrong_client"}
    numbers = [int(n) for n in record["targets"]]
    keys = [_target_key(repo, n) for n in numbers]
    marks, params = placeholders("k", keys)
    confirmed = db.read(f"SELECT claim_id FROM factory_lease WHERE lease_key IN ({marks})", params)
    if len(confirmed) != len(keys) or any(r["claim_id"] != claim_id for r in confirmed):
        return {"status": "conflict", "reason": "stale_target_fence"}
    if operation == "renew" and record["expires_at"] <= datetime.now(UTC).timestamp():
        return {"status": "conflict", "reason": "lease_expired_liveness_unknown"}
    fence_sql, fence_params = _fence(repo, claim_id, numbers, active=operation == "renew")
    if evidence_target is not None and evidence_target not in numbers:
        if db.read(
            "SELECT 1 AS x FROM factory_lease WHERE lease_key = :k",
            {"k": _target_key(repo, evidence_target)},
        ):
            return {"status": "conflict", "reason": "evidence_target_held"}
        fence_sql += " AND NOT EXISTS (SELECT 1 FROM factory_lease WHERE lease_key = :f_evidence)"
        fence_params["f_evidence"] = _target_key(repo, evidence_target)
    if operation == "handoff":
        nonce = _fence_pending(db, repo, claim_id, (fence_sql, fence_params))
        if nonce is None:
            return {"status": "conflict", "reason": "handoff_in_progress"}
        fence_sql += (
            " AND EXISTS (SELECT 1 FROM factory_lease WHERE lease_key = :f_key"
            " AND detail ->> 'pending' = :f_nonce)"
        )
        fence_params["f_nonce"] = nonce
        if verify is not None:
            try:
                verify(
                    {
                        "target": record["target"],
                        "action": record["action"],
                        "head": record.get("head"),
                        "targets": list(numbers),
                        "client": client,
                        "actor": actor,
                        "claim_id": claim_id,
                    }
                )
            except BaseException:
                _clear_pending(db, repo, claim_id, nonce)
                raise
    event_type = {"renew": "external_renewed", "handoff": "external_handoff"}.get(
        operation, "external_released"
    )
    event_id = f"{event_type}:{claim_id}:{uuid.uuid4()}"
    statements = [
        core.event_insert(
            repo,
            ORIGIN,
            event_type,
            {
                "event_id": event_id,
                "claim_id": claim_id,
                "actor": actor,
                "client": client,
                "target": record["target"],
                "action": record["action"],
                "head": record.get("head"),
                "evidence": evidence,
            },
            subject_type="target",
            subject_number=numbers[-1],
            guard=(fence_sql, fence_params),
        )
    ]
    done_sql, done_params = core.event_guard(event_id)
    all_marks, all_params = placeholders("a", [_external_key(repo, claim_id), *keys])
    if operation == "renew":
        statements.append(
            (
                "UPDATE factory_lease SET expires_at = unixepoch() + :ttl"
                f" WHERE lease_key IN ({all_marks}) AND claim_id = :token AND {done_sql}",
                {**all_params, **done_params, "ttl": ttl, "token": claim_id},
            )
        )
    else:
        statements.append(
            (
                f"DELETE FROM factory_lease WHERE lease_key IN ({all_marks}) AND claim_id = :token"
                f" AND {done_sql}",
                {**all_params, **done_params, "token": claim_id},
            )
        )
    result = core.reconciled_batch(db, statements, lambda: core.event_exists(db, event_id))
    if result is not None and not result[0]:
        if operation == "handoff":
            _clear_pending(db, repo, claim_id, fence_params["f_nonce"])
        return {"status": "conflict", "reason": "claim_changed_during_verification"}
    expires = None
    if operation == "renew":
        rows = db.read(
            "SELECT expires_at FROM factory_lease WHERE lease_key = :k",
            {"k": _external_key(repo, claim_id)},
        )
        expires = _iso(rows[0]["expires_at"]) if rows else None
    return {
        "status": "renewed" if operation == "renew" else "released",
        "claim_id": claim_id,
        "expires_at": expires,
        "handoff_recorded": operation == "handoff",
    }


def external_wip_count(repo: str) -> int:
    """All external WIP claims, expired or not, each counted once."""
    _require()
    rows = core.store().read(
        "SELECT count(*) AS n FROM factory_lease WHERE repo = :repo AND kind = 'external'"
        " AND wip = 1",
        {"repo": repo},
    )
    return int(rows[0]["n"])


def external_holder(repo: str, number: int) -> dict[str, Any] | None:
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
        or type(number) is not int
        or number < 1
    ):
        raise ValueError("invalid external holder query")
    _require()
    rows = core.store().read(
        "SELECT * FROM factory_lease WHERE lease_key = :k AND external = 1",
        {"k": _target_key(repo, number)},
    )
    if not rows:
        return None
    row = rows[0]
    detail = json.loads(row["detail"])
    return {
        "claim_id": str(row["claim_id"]),
        "actor": str(detail.get("actor") or ""),
        "client": str(detail.get("client") or ""),
        "target": str(detail.get("target") or ""),
        "action": str(detail.get("action") or ""),
        "targets": [int(n) for n in json.loads(row["targets"])],
        "expires_at": _iso(row["expires_at"]),
    }


def revoke_external(repo: str, operator: str, claim_id: str, *, reason: str) -> dict[str, Any]:
    """Operator-initiated, UUID-fenced removal of one external claim."""
    _external_request(repo, operator, "operator", [1])
    try:
        uuid.UUID(claim_id)
    except ValueError, AttributeError:
        raise ValueError("invalid claim ID") from None
    reason = reason.strip()
    if not reason or len(reason) > 500 or any(c in reason for c in "\r\n"):
        raise ValueError("revoking a claim needs a one-line reason")
    _require()
    db = core.store()
    record = _claim(db, repo, claim_id)
    if record is None:
        return {"status": "conflict", "reason": "claim_missing_or_replaced"}
    if record.get("external") is not True:
        return {"status": "conflict", "reason": "stale_claim"}
    numbers = [int(n) for n in record["targets"]]
    event_id = f"external-revoked:{claim_id}"
    guard = core.held_guard(_external_key(repo, claim_id), claim_id)
    statements = [
        core.event_insert(
            repo,
            ORIGIN,
            "external_revoked",
            {
                "event_id": event_id,
                "claim_id": claim_id,
                "operator": operator,
                "actor": record.get("actor"),
                "client": record.get("client"),
                "target": record.get("target"),
                "action": record.get("action"),
                "reason": reason,
            },
            subject_type="target",
            subject_number=numbers[-1] if numbers else 0,
            guard=guard,
        ),
        (
            "DELETE FROM factory_lease WHERE repo = :repo AND claim_id = :token",
            {"repo": repo, "token": claim_id},
        ),
    ]
    result = core.reconciled_batch(db, statements, lambda: core.event_exists(db, event_id))
    if result is not None and not result[0]:
        return {"status": "conflict", "reason": "claim_missing_or_replaced"}
    return {
        "status": "revoked",
        "claim_id": claim_id,
        "targets": numbers,
        "client": record.get("client"),
        "actor": record.get("actor"),
    }

#!/usr/bin/env python3
"""Retry classes, cooldown constants and Fibonacci backoff for factory workers and models."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

TRANSIENT = {"provider_failure": 900, "provider_throttle": 1800, "provider_quota": 1800}
MODEL_COOLDOWNS = {"model_unavailable": 21600, "model_policy_violation": 21600,
                   "model_retired_410": 21600}
# A failing model must never cool another model on the same platform.
# Keep the old OmniRoute constant for compatibility with existing tests.
MODEL_SCOPED_SOURCES = {"omniroute-free", "omniroute-opencode"}


def model_scoped_transient(source: str, outcome: str) -> bool:
    return outcome in TRANSIENT


def provider_scoped_transient(source: str, outcome: str) -> bool:
    # Compatibility for legacy reconciliation callers; quota, throttle and
    # failures are always tracked by their exact (source, model) identity.
    return False


def _provider_cooldown(source: str, outcome: str, observed: datetime, seconds: int | None = None) -> datetime | None:
    if outcome not in TRANSIENT:
        return None
    if outcome == "provider_quota":
        try:
            from factory_quota_pause import quota_reset_epoch
            reset = quota_reset_epoch(source, int(observed.timestamp()))
            if reset > observed.timestamp():
                return datetime.fromtimestamp(reset, timezone.utc)
        except (ImportError, ValueError, KeyError):
            pass
    return observed + timedelta(seconds=seconds if seconds is not None else TRANSIENT[outcome])


def exponential_provider_delay(
    outcome: str, prior_outcome: str | None, prior_duration: float,
    age_seconds: float,
) -> int:
    """Double recent provider cooldowns without penalizing recovered providers.

    This derives the prior delay from the existing durable Neon budget row,
    avoiding new schema or one independent timer per concurrent worker.
    """
    base = TRANSIENT[outcome]
    if prior_outcome not in TRANSIENT or age_seconds < 0 or age_seconds > 12 * 3600:
        return base
    return min(4 * 3600, max(base, int(max(0, prior_duration) * 2)))



# Worker failures get durable Fibonacci backoff while retaining their roster pins.
# Credential absence is gated before lease assignment, never charged to a model.
WORKER_RETRY_CLASSES = frozenset({
    "agent_timeout", "provider_failure", "provider_throttle", "provider_quota",
    "model_unavailable", "model_policy_violation", "environment_failure",
    "control_plane_failure", "unknown_failure", "work_failure",
})


def fibonacci_worker_delay(streak: int) -> int:
    """Five-minute Fibonacci retries per exact model, with no upper bound."""
    if streak < 1:
        raise ValueError("failure streak must be positive")
    previous, current = 1, 1
    for _ in range(1, streak):
        previous, current = current, previous + current
    return previous * 300


def fibonacci_retry_at(observed: datetime, streak: int) -> datetime:
    """Next retry time; clamps only at the largest representable datetime.

    SQL writers clamp expires_at to 9999-12-31 too, because asyncpg cannot
    decode a later timestamptz into a Python datetime.
    """
    try:
        return observed + timedelta(seconds=fibonacci_worker_delay(streak))
    except OverflowError:
        return datetime.max.replace(tzinfo=timezone.utc)


def worker_backoff_reason(record: Any, source: str, model: str, now: datetime) -> str | None:
    """The 90-day state TTL is not the not-before time for a retry."""
    if record is None:
        return None
    payload = record["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    state = payload[0] if isinstance(payload, list) and payload else {}
    if not isinstance(state, dict):
        return None
    # A catalog-backed slot can execute a different published model each
    # session. Do not erase its worker-level Fibonacci backoff simply because
    # the TSV no longer contains a permanently pinned model ID.
    if state.get("source") and state["source"] != source:
        return None
    # Dynamic slots are not permanently assigned to a model. A known model
    # failure must not suppress the entire slot's alternate model capacity.
    if not model and state.get("outcome") in MODEL_RETRY_CLASSES:
        return None
    if model and model != "@catalog" and state.get("model") != model:
        return None
    retry_at = state.get("next_retry_at")
    # Legacy suspended rows without a not-before timestamp must not gate for 90 days.
    if retry_at is None and "suspended" in state:
        return None
    if retry_at is not None:
        try:
            if datetime.fromisoformat(str(retry_at)) > now:
                return f"Fibonacci retry deferred until {retry_at}"
        except ValueError:
            return "invalid worker backoff state"
        return None
    # Backward compatibility: old timeout keys stored their delay in expires_at.
    return "legacy timeout cooling in Neon" if record["expires_at"] > now else None


MODEL_RETRY_CLASSES = frozenset({
    "agent_timeout", "provider_failure", "provider_throttle", "provider_quota",
    "model_unavailable", "model_policy_violation",
})


def validate_intermediate_model_failures(
    raw: Any, source: str,
) -> list[tuple[str, str, str, str]]:
    """Normalize only attested exact (source, model, outcome, scope) events."""
    from factory_provider_candidates import ADAPTERS

    if not raw:
        return []
    if not isinstance(raw, list) or len(raw) > 1000:
        raise ValueError("invalid trusted intermediate model failures")
    accepted = MODEL_RETRY_CLASSES | {"model_retired_410"}
    seen: dict[tuple[str, str], tuple[str, str]] = {}
    for entry in raw:
        if isinstance(entry, str) and source == "opencode-free":
            failure_source, model, classification, scope = source, entry, "model_unavailable", "model"
        elif isinstance(entry, dict):
            failure_source = entry.get("source", source)
            model, classification = entry.get("model"), entry.get("outcome")
            scope = entry.get("scope", "model")
        else:
            raise ValueError("invalid trusted intermediate model failure entry")
        if (not isinstance(failure_source, str) or failure_source not in ADAPTERS
                or not isinstance(model, str)
                or not re.fullmatch(r"[\w./:-]{1,180}", model)
                or classification not in accepted
                or scope not in {"account", "model", "pool"}
                or (scope == "account" and classification not in {"provider_quota", "provider_failure"})
                or (scope == "pool" and classification != "provider_quota")):
            raise ValueError("invalid trusted intermediate model failure identity")
        # Old account-labelled credit refusals must not suppress healthy peers.
        seen[(failure_source, model)] = (classification, "pool" if scope == "pool" else "model")
    return [
        (s, model, *seen[(s, model)]) for s, model in sorted(seen)
    ]



def attributable_model_failure(classification: str, proven: bool) -> bool:
    """Only blame the shared source/model when inference was attempted."""
    return classification in MODEL_RETRY_CLASSES or (
        proven and classification in {"work_failure", "unknown_failure"}
    )


def historical_failure_streak(
    rows: list[Any], run_id: int, source: str, model: str, *,
    model_only: bool, model_independent: bool = False,
) -> int:
    """Recover a consecutive streak from trusted terminal Neon events.

    The current outcome can already be present in the event log. Ignore it
    and anything newer to avoid double counting or reversing a fresh success.
    Pre-session/unconfigured events cannot prove a model was attempted.
    """
    streak = 0
    for row in rows:
        detail = row["detail"]
        if isinstance(detail, str):
            detail = json.loads(detail)
        if not isinstance(detail, dict):
            continue
        previous_run = int(detail.get("run_id") or 0)
        if previous_run == 0 or previous_run >= run_id:
            continue
        if (detail.get("source") != source or
                (detail.get("model") != model and not model_independent)):
            # Fixed pins reset their streak when repinned. Catalog-backed
            # slots retain worker streak across real upstream model rotation.
            if not model_only:
                break
            continue
        if row["event_type"] == "worker_completed":
            break
        if row["event_type"] != "worker_failed" or detail.get("configured") is False:
            continue
        outcome = str(detail.get("outcome_class") or "unknown_failure")
        if model_only:
            if not attributable_model_failure(outcome, detail.get("exact_model_proven") is True):
                continue
        elif outcome not in WORKER_RETRY_CLASSES:
            continue
        if outcome == "unknown_failure" and detail.get("exact_model_proven") is not True:
            # Old unconfigured workers were incorrectly classified this way.
            continue
        streak += 1
    return streak


async def _recent_streak(
    conn: Any, run_id: int, source: str, model: str, *,
    repo: str, worker: str | None = None, model_independent: bool = False,
) -> int:
    """Read prior attempts after the latest manual health reset in one query."""
    if worker is None:
        rows = await conn.fetch(
            """SELECT event_type,detail FROM public.factory_state_events
               WHERE subject_type='worker'
                 AND event_type IN ('worker_failed','worker_completed')
                 AND detail->>'source'=$1 AND detail->>'model'=$2
                 AND created_at>GREATEST(
                     now()-interval '7 days',
                     COALESCE((SELECT updated_at FROM public.factory_state_kv
                               WHERE cache_key=$3), '-infinity'::timestamptz))
               ORDER BY id DESC LIMIT 64""",
            source, model, f"factory-model-health-reset-v1:{repo}",
        )
    else:
        rows = await conn.fetch(
            """SELECT event_type,detail FROM public.factory_state_events
               WHERE subject_type='worker' AND subject_number=$1::bigint
                 AND event_type IN ('worker_failed','worker_completed')
                 AND created_at>GREATEST(
                     now()-interval '7 days',
                     COALESCE((SELECT updated_at FROM public.factory_state_kv
                               WHERE cache_key=$2), '-infinity'::timestamptz))
               ORDER BY id DESC LIMIT 64""",
            int(worker), f"factory-model-health-reset-v1:{repo}",
        )
    return historical_failure_streak(
        rows, run_id, source, model, model_only=worker is None,
        model_independent=model_independent,
    )

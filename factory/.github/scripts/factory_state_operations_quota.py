#!/usr/bin/env python3
"""Reset-timed cooldowns for quota, throttle and empty-balance refusals.

These refusals describe provider capacity, so they never grow a Fibonacci
streak (factory_state_operations_backoff). One refusal writes one cooldown,
scoped by factory_quota_reset:

- account: factory-provider-backoff-v1:<repo>:source:<source>, which the
  dispatcher filter and the fleet failover export honor for the whole source;
- pool: factory-credit-pool-v1:<repo>:source:<source>, exported as the
  "@credit_pool" exclusion so only that source's priced models are skipped;
- model: factory_model_health for the exact pair.

A later refusal never shortens an active cooldown.
"""

from __future__ import annotations

from typing import Any

PROVIDER_PREFIX = "factory-provider-backoff-v1"
POOL_PREFIX = "factory-credit-pool-v1"
CREDIT_POOL_KEY = "@credit_pool"


def capacity_hints(raw: Any) -> dict[tuple[str, str], dict[str, object]]:
    """Validated FACTORY_QUOTA_RESET markers keyed by (source, model)."""
    from factory_quota_reset import KINDS, RESET_SOURCES

    hints: dict[tuple[str, str], dict[str, object]] = {}
    if not isinstance(raw, list):
        return hints
    for item in raw[:200]:
        if not isinstance(item, dict):
            continue
        source, model = item.get("source"), item.get("model")
        epoch = item.get("reset_at")
        if (
            isinstance(source, str)
            and isinstance(model, str)
            and item.get("kind") in KINDS
            and item.get("reset_source") in RESET_SOURCES
            and isinstance(epoch, int)
            and not isinstance(epoch, bool)
        ):
            hints[(source, model)] = item
    return hints

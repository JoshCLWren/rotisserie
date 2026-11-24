"""The worker's own model exclusions, as the status job published them.

The worker refuses an exact model that ``export-model-exclusions`` lists for
its provider: model health cooldowns, durable per-model backoffs and credit
pool cooldowns. The status job publishes that same export into
``factory-stats.json``, so the planner applies the worker's view instead of
re-deriving it. An absent, malformed or stale block means the planner cannot
know what the worker will refuse.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from rotisserie.factory_console import runtime
from rotisserie.factory_console.github import Obj, obj, timestamp

# The status job republishes a snapshot older than 840 seconds (its freshness job);
# allow one more refresh cycle before treating the block as stale.
STALE_AFTER = timedelta(seconds=2 * 840)
CREDIT_POOL = "@credit_pool"


@dataclass(frozen=True)
class Exclusions:
    sources: dict[str, frozenset[str]]
    pools: frozenset[str]  # providers whose priced models are cooling; free ones still run

    def reason(self, provider: runtime.Provider, recorded: str, model: str) -> str | None:
        """Why the worker would refuse this exact model now, or None."""
        listed = self.sources.get(provider.source, frozenset())
        forms = {recorded, model, model.split("/", 1)[-1]}
        if forms & listed:
            return "the worker excludes it right now (cooldown or backoff)"
        return None

    def unverifiable(self, provider: runtime.Provider) -> bool:
        """The snapshot cannot say which of this provider's models the worker allows."""
        return provider.source in self.pools


def read(stats: Obj, now: datetime) -> Exclusions | None:
    """The published exclusions, or None when they are missing, malformed or stale."""
    block = obj(stats.get("model_exclusions"))
    published = timestamp(block, "generated_at")
    raw = block.get("sources")
    if published is None or now - published > STALE_AFTER or not isinstance(raw, dict):
        return None
    sources: dict[str, frozenset[str]] = {}
    for source, models in raw.items():
        if not isinstance(models, list) or not all(isinstance(item, str) for item in models):
            return None
        sources[source] = frozenset(str(item) for item in models)
    return Exclusions(sources, sources.pop(CREDIT_POOL, frozenset()))

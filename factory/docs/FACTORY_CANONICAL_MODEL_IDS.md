# Canonical provider and model identity

There are two independent identities in the ComicPile factory.

1. **Worker/lease identity** is a stable numeric slot derived from the
   provider source and slot index by
   `.github/scripts/factory_worker_slots.py`. A model changing or disappearing
   never causes a worker to be retired or renumbered.
2. **Execution model identity** is the exact model ID published by the
   connected provider catalog or the installed OpenCode/Kilo CLI. The model
   is selected after provider authentication and capacity checks, immediately
   before running useful work. It is recorded as execution telemetry.

`.github/scripts/factory_provider_registry.py` is the provider integration
registry: endpoints, credential environment names and canonical OpenCode
provider identifiers. It does not contain an allowed-model list.
`.github/scripts/factory_provider_candidates.py`,
`.github/scripts/factory_runtime_model_select.py`, and
`.github/scripts/factory_published_model_catalog.py` discover model
candidates at runtime. A slash in an upstream ID belongs to that upstream
ID; no code should split it into an unapproved second provider.

Pricing is metadata for observability only, never a filter. A catalog entry
does not guarantee chat compatibility, tools, working authentication or useful
code delivery. Incompatible advertised capabilities may be excluded, while
uncertain capabilities require actual execution evidence. Failed executions
use bounded retries/backoffs; neither an old failure nor a price label
permanently removes a model.

No committed TSV, model pin list, expected-worker JSON, or roster snapshot
should exist. Tests use synthetic models and verify the behavioral contract.

Relevant references: https://opencode.ai/docs/providers/ and
https://opencode.ai/docs/models/.

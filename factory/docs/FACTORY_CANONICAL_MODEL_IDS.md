# Canonical provider and model identity

There are two independent identities in the factory.

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

Pricing is generally metadata for observability, not an automatic eligibility
filter. The **existing** `opencode-free` lane is the deliberate exception:
it authenticates with GitHub Actions OIDC, exchanging GitHub's signed job
identity for a short-lived `OPENCODE_API_KEY` using the service account's
`OPENCODE_GO_CLIENT_ID` GitHub Actions secret. No long-lived Go API key is
required. It verifies that the associated workspace
exposes `opencode-go`, and selects only published zero-price, tool-capable
chat models. It prefers `opencode-go/hy4-flash-free` while that exact model
is eligible, then rotates through the remaining free Go models. The Zen
provider `opencode` is explicitly denied by the worker policy. **There is
no second OpenCode lane**. The old source name is retained solely for stable
lease identities and factory store events.

The exchange requires `id-token: write` on the calling Entry and reusable
worker jobs; trusted worker code renews identity before each model attempt.
Only GitHub Actions with an authorized OpenCode service account may sign in.

A catalog entry still does not guarantee useful execution. Other provider
candidate policies remain dynamic. Failed executions retain exact-model
backoff; a free-model quota failure never implies a paid sibling or another
provider is exhausted.

No committed TSV, model pin list, expected-worker JSON, or roster snapshot
should exist. Tests use synthetic models and verify the behavioral contract.

Relevant references: https://opencode.ai/docs/providers/ and
https://opencode.ai/docs/models/.

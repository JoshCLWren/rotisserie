# Bounded fresh-assignment controller

Issue #29 starts with one coherent portable capability: selecting, claiming and
handing off **at most one fresh implementation assignment**. The application
`AssignmentController` consumes a complete `ControllerView` through an injected
`ControllerPort`. It uses the existing deterministic graph policy and the #28
`Assignment` process contract. It neither runs a process nor contacts a host.

## Classification and source

The read-only audit used ComicPile main
`b83a5a7f2c82eaa4764ade87681c5be5db23d2e7` on 2026-10-08. Sources were
`.github/scripts/factory-work-controller.py` (`assign`, `assign_candidate`,
`worker_has_active_lease`, `reconcile_stale_leases`) and
`.github/scripts/factory_work_policy.py` (capacity/backlog counters and
`lease_is_stale`). The archived baseline remains unchanged.

Selection, worker exclusivity, claim/handoff compensation and fail-closed
observations are application behavior over existing domain policy. Host
acquisition, durable compare-and-swap, liveness translation and dispatch are
adapter responsibilities. Labels, workflow identities, provider routing,
product exclusions, no-diff retry history and ranking translation remain adopter
configuration/policy. The CLI is an entrypoint over a captured fixture envelope.

## Observation and effect contract

A view contains the graph, acquisition version, observation time, active WIP,
completion backlog and explicit worker liveness. Its version must cover the
**entire** observation, not just issue labels. Incomplete acquisition raises
`AcquisitionFailure`; absent worker liveness is unknown. Future or excessively
old observations hold closed. Unknown configured-worker or leased-worker
liveness holds closed even when the fresh queue is empty.

The planner distinguishes selection, no eligible **fresh** work, capacity,
backpressure, worker busy, unknown liveness, acquisition failure, stale selection
and required recovery. A fresh-work zero does not mean review/completion is
empty and must not terminate a full dispatcher session. Every recorded lease
fences its target, including expired leases. Expiry never grants implicit
release authority; an expired lease produces `recovery_required`.

Applying a tick claims only the first ranked target when it matches the trusted
assignment. The controller reacquires and validates the exact lease, eligibility,
human boundaries and idle worker before dispatch. A proven refusal triggers a
second acquisition and exact-lease release. Unknown claim/dispatch results retain
ownership. A retry with existing ownership holds closed; automatic resume of a
partially acknowledged controller tick is deliberately not implemented.

An effect adapter must:

- Atomically compare the complete view version and suppress duplicate commands
  by their content-derived idempotency keys.
- On claim, reject a worker already owning any target and enforce current WIP,
  backpressure, eligibility and human gates.
- On dispatch, revalidate current time/authority expiry, the exact active lease,
  worker identity, current graph and human gates, then durably deduplicate the
  handoff before launching it. Return `refused` only with proof no worker started;
  a lost acknowledgement is `unknown`.
- On release, match the exact lease ID and current authoritative idle liveness.
  A stale observation token must conflict. Never translate an ambiguous write
  into `EffectFailure`, which denotes a proven unapplied effect.

These obligations are protocols, not a supplied production GitHub implementation.
The controller defaults to planning; only explicit application `apply=True`
invokes effect ports. The shipped CLI exposes planning only.

## Public fixture caller

From a clean checkout:

```sh
uv sync --locked
uv run python examples/controller/caller.py
```

The caller invokes the public `controller-plan` CLI with the checked-in view and
trusted assignment. It requires an explicit configured worker inventory,
repository allowlist, operation/lease identities and observation-age bound.
The CLI has no apply flag. It records versioned evidence with exact view and
assignment input digests, the policy inputs and the result. It initializes no
graph state and accesses no credentials, remote effects or worker executors.
Exit 0 means selected or a proven fresh-work zero, 3 means a hold, and 2 means
invalid configuration/input. Local append-only evidence is the only write.

The fixture is a credential-free contract example, **not live parity evidence**.
Tests use effect fakes to verify stale claims, duplicate suppression, ambiguous
acknowledgements, changed human gates, dispatch refusal and compensation races.

## Remaining issue #29 work

The next capabilities are bounded stale-lease recovery with attested inactivity,
exact-head review/completion handoffs using contributor-set independence, and a
public effect/caller boundary backed by concrete adopter CAS enforcement. Formal
host approvals alone must never grant that authority. Current product eligibility
and worker-specific ranking need translation before a real adopter may claim
parity with this fresh-work planner.

Golden same-view comparison against the current live controller, rollback/refusal
control, provider bridges, bounded execution observation and a pinned adopter
caller remain required before cutover or duplicate deletion. No ComicPile file,
workflow, schedule, credential or control variable changed here; no autonomous
self-implementation was activated. Issue #29 remains open.

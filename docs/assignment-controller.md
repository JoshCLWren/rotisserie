# Bounded assignment and recovery controllers

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

The next capabilities are exact-head review/completion handoffs using contributor-set independence, and a
public effect/caller boundary backed by concrete adopter CAS enforcement. Formal
host approvals alone must never grant that authority. Current product eligibility
and worker-specific ranking need translation before a real adopter may claim
parity with this fresh-work planner.

Golden same-view comparison against the current live controller, rollback/refusal
control, provider bridges, bounded execution observation and a pinned adopter
caller remain required before cutover or duplicate deletion. No ComicPile file,
workflow, schedule, credential or control variable changed here; no autonomous
self-implementation was activated. Issue #29 remains open.


## Bounded stale-lease recovery

The second issue #29 slice adds application `LeaseRecoveryController.recover`
with `RecoveryRequest`, `RecoveryView`, `RecoveryRelease` and schema-version-1
`RecoveryResult`. It releases at most one expired lease per call, ordered by
expiry then lease identity. A held first lease is never skipped. Clocks,
observation freshness, inactivity TTL and configured worker identities are
explicit inputs. The acquired controller view retains explicit capacity and
backlog inputs, but recovery does not allocate capacity or dispatch work.

A fresh complete acquisition with no expired lease yields `no_expired_lease`.
Incomplete, failed, stale or future graph acquisition yields
`observation_failed`. Missing/unknown worker liveness, unconfigured ownership,
active workers and incomplete global liveness yield `liveness_hold`. The global
completeness attestation must cover unresolved activity that could belong to any
configured worker; ambiguity cannot be translated into idle.

Positive inactivity requires an adapter-attested complete trusted activity read
bound to the **full exact lease**, including worker and acquisition/expiry times.
Missing, untrusted, mismatched, stale or future evidence holds closed. The latest
activity must be within the lease's lifetime up to the observation time; an old
marker predating acquisition cannot authorize release. The evidence observation
must be fresh even though its latest activity is old. Activity age must strictly
exceed the explicit TTL (`activity_hold` at equality). Expiry alone never grants
release authority.

Apply reacquires the complete view and repeats the decision before sending one
`RecoveryRelease`. The durable adapter must atomically compare the full lease
and observation token, fence graph **and activity/liveness changes**, recheck
current expiry/inactivity/idle with its injected clock, and durably deduplicate
the content-derived command key. Renewal timestamps are included in that key.
A new active worker between observation and release must conflict even if issue
ownership labels are unchanged. Reacquisition alone cannot enforce this rule:
production adapter enforcement remains follow-on work. CAS conflicts yield
`stale_state`, proven unapplied writes `release_failed`, and ambiguous writes
`release_uncertain`. An uncertain result grants no follow-up dispatch authority;
the next call must reacquire durable state. No recovery path invokes a worker.

The existing `CoordinationService.recover` remains a local graph simulation
primitive and has **no trusted activity/liveness acquisition**; it is not the
production controller recovery boundary. Its tests and behavior are preserved.

### Public planning caller

```sh
uv run python examples/controller/recovery_caller.py
```

The public `controller-recovery-plan` CLI accepts a schema-version-1 envelope
containing `controller`, `liveness_complete` and exact-lease `activity` evidence.
It requires `--at`, `--max-observation-age`, `--inactivity-ttl`, `--operation` and
an explicit configured worker inventory/repository allowlist. It has no apply
flag, mutation adapter or executor. It records the exact input digest, token,
worker/time/TTL inputs and versioned result without initializing graph state.
Exit 0 means planned recovery or a proven no-expired-lease result; 3 is a hold
and 2 is malformed input/configuration. Fixture attestations demonstrate the
contract and do not confer live mutation authority or prove live parity.

### Recovery source provenance

ComicPile main was refreshed read-only on 2026-10-09 to exact SHA
`8692c718458282af9300edffce72035145ff6655`. The audit inspected
`.github/scripts/factory-work-controller.py`: `reconcile_stale_leases`,
`latest_lease_activity_epoch`, `active_fixed_workers` and
`unresolved_run_has_credible_liveness`; and
`.github/scripts/factory_work_policy.py`: `lease_is_stale`. These were compared
with the same functions in archived baseline
`2dc9b675e1a828b526f074c5f5c151293dedcdee`.

Both source versions require trusted activity and fence fixed workers when runs
are active or unresolved; failed activity reads return no activity. Workflow/job
query failures and ambiguous run identity retain the liveness fence. The source
staleness policy uses a strict greater-than TTL, with host-specific local/fixed
owner rules and a legacy omitted-argument path. Rotisserie preserves the
conservative current intent, strengthens exact-lease evidence and CAS safety,
and does not carry the legacy omitted-liveness shortcut into its public API.
The source loops over owned targets and replaces labels without an exact-lease
CAS; this extraction deliberately bounds each call and requires durable CAS.

Trusted comment authors/markers, pagination, workflow/job identity resolution,
local/fixed worker differences, owner labels, credential scope and TTL selection
remain adopter adapters/configuration. This slice supplies no production host
adapter, golden live same-view parity, exact-head review/completion handoff or
observed cutover. ComicPile, workflows and archived evidence remain unchanged;
issue #29 remains open.

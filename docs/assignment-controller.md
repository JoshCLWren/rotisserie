# Bounded assignment, recovery, review and completion controllers

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

The portable exact-head review and completion capabilities are described below.
A public effect/caller boundary backed by concrete adopter CAS enforcement
remains follow-on work. Formal
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


## Bounded exact-head independent-review handoff

The third issue #29 slice provides `ReviewAssignmentController.tick`, pure
`plan_review`, typed `ReviewRequest`/`ReviewView`/`ReviewEffect`, and version-1
`ReviewResult` and durable `ReviewBinding` shapes. One call selects and hands off
at most one review assignment through `ReviewPort.acquire`, `apply` and
`dispatch`. It reuses the existing worker `Assignment` and process contract;
the controller does not run a worker or interpret its verdict.

The adapter supplies an explicit ordered tuple of current review demand.
Selection follows that tuple deterministically, skipping contributors, workers
that already semantically approved this head, closed work, unmet explicit
prerequisites, human gates, and already fenced targets. Ordering, review-stage
translation and product eligibility stay in adapters/configuration. A held
provenance record is never skipped to widen review eligibility. The selected
change/work/head must match the trusted assignment; a caller cannot authorize a
different candidate by changing repository content or supplying host approvals.

Each candidate contains the existing domain `ReviewIndependenceSignals` and an
explicit fresh, complete, trusted acquisition attestation. Its contributor set
must include the producer and the entire required reachable implementation
lineage, including earlier repairers. Selection uses the domain
`excluded_reviewers()` rule. It additionally requires recorded exact-head
provenance, an attested known producer matching the graph, and resolved
contributor/semantic-approver identities. Missing, unknown, contradictory,
incomplete, untrusted, future or stale provenance yields `provenance_hold`.
Adapters must report identity ambiguity as incomplete acquisition/provenance;
a partial list must never receive a completeness attestation.

This deliberately strengthens mutation authority beyond the existing domain
approval fallback: two semantic reviewers with unknown producer/provenance do
not authorize this controller to claim work. Formal host reviews in the graph
are not authority inputs. The existing read-only `GitHubReviewEvidence` collector
provides domain signals, not a complete acquisition/claim adapter: it neither
attests pagination completeness nor supplies producer attestations before the
first semantic review. Adopters must provide trusted exact-head producer
acquisition rather than upgrade a branch/body cross-check to an attestation.

`no_eligible_review_work` describes only this review lane. It is distinct from
capacity, backlog, provenance, acquisition, liveness, busy-worker and recovery
holds. Global liveness completeness attests a trusted full read, including
unresolved activity; configured and leased workers with absent or unknown
liveness hold closed even with an empty review queue. Inventory, observation
age, time, authority expiry/lease TTL, WIP ceiling and backlog ceiling are
explicit. `controller.active` counts occupied dispatch slots, including claimed
review leases: a successful claim adds exactly one occupied slot, and a release
removes it. This review lane uses the total WIP ceiling; reserved review capacity
constrains production intake, not a separate ceiling on review assignments.

### Durable mutation and dispatch obligations

Apply reacquires and compares the complete observation before claiming. One
`CLAIM` effect **atomically** persists the lease and the exact review binding /
request. The assignment ID is the review-request identity. The binding includes
the full lease (identity, worker, work and timestamps), change, revision,
contributor/producer provenance and the full trusted process assignment.
Its content digest contributes to claim, dispatch and release command keys.
Although the generic domain lease remains work-scoped, the durable binding and
every effect are change/head-scoped; adapters must never persist just the lease
and lose that fence. No partially acknowledged tick is automatically resumed.

After claim, reacquisition must return that exact lease and binding, with only
the expected slot increment. Any other graph, contributor, ordering, capacity,
backlog, binding or worker-liveness change holds closed. Observation timestamps
must remain fresh; refreshed provenance records with changed timestamps also
hold conservatively and require reconciliation. Head changes invalidate both
old provenance and old assignment authority. A renewed/replaced lease or absent
durable review request prevents handoff.

At **every** effect boundary, the adapter must atomically compare the full view
token and full exact binding, recheck current time/expiry and observation
freshness, head, provenance,
human gates, worker exclusivity and capacity, and durably deduplicate the
command. The token covers demand, bindings, graph, capacity, provenance and
liveness, including activity on other workers. Reacquisition cannot eliminate
the race after the final read; enforcing this full-view CAS is mandatory for a
production adapter. Dispatch must durably record the handoff before launching
the existing process contract. `EffectFailure` proves an unapplied claim;
`EffectConflict` proves a rejected CAS and, for dispatch, no worker start.
Other exceptions/unknown outcomes retain ownership.

Only a proven dispatch refusal permits compensation. Reacquisition must still
match the full claimed lease, binding and initial view (with its own claim
removed), with fresh idle liveness. A separate CAS release removes only that
exact lease and review request. Head/contributor changes, renewal, replacement,
new activity and release conflicts retain ownership. An uncertain claim or
handoff never causes compensation; stale ownership needs separate reconciliation.

### Public planning caller and validation

```sh
uv run python examples/controller/review/caller.py
```

`controller-review-plan` takes version-1 review acquisition and assignment JSON,
explicit operation/lease identities, `--at`, `--max-observation-age`, optional
WIP/backlog limits and an allowlisted repository/configured worker inventory.
It has no apply flag, effect adapter or executor. Evidence binds both exact input
digests, view token, policy/time/inventory inputs and the versioned result.
Only the local append-only operation journal is written; graph state is never
initialized. Exit 0 means selected or a proven review zero, 3 means held and 2
means malformed input/configuration. Fixtures are contract examples, not live
mutation authority or same-view parity evidence.

Tests cover all contributor exclusions, ambiguous/untrusted/stale provenance,
formal approval non-authority, exact heads, human gates, explicit prerequisites,
duplicate suppression, bounded ordered selection, unknown acquisition/liveness,
capacity/backlog distinctions, races before and inside each mutation, renewed or
replaced ownership, missing bindings, lost acknowledgements and proven refusal.

### Review source provenance and remaining boundary

ComicPile main was refreshed through read-only GitHub acquisition on 2026-10-09
at exact SHA `8692c718458282af9300edffce72035145ff6655`. Inspection covered
`.github/scripts/factory-work-controller.py` (`assign`, `assign_candidate`,
`pr_contributor_workers`, `resolve_review_head_contributors`),
`factory_work_policy.py` (`candidate_is_independent_for_worker`,
`order_candidates_for_worker`), `factory-review-controller.py`
(`validate_review_lease`, `handle_review`, `record_contribution`) and
`factory_review_policy.py` (`trusted_comment_bodies`, `recorded_pr_contributors`,
`current_head_contributors`, `head_contributor_provenance`,
`head_has_authorized_approval`). These were compared with archived baseline
`2dc9b675e1a828b526f074c5f5c151293dedcdee` at the same source-relative paths.

The archived review policy excluded the single declared producer and allowed
two reviewers when it was unknown. Current source excludes all recorded lineage
contributors plus the declared producer, checks the exact reviewed head, trusts
whole-body contributor markers and filters marker authors/Apps. Current
assignment contributor-read failures fall back to an empty set and downstream
review enforcement; Rotisserie deliberately holds before mutation instead.
Current source claims through ownership labels without full-lease/full-view CAS;
this extraction requires an atomic durable claim/request and bounded handoff.
Trusted actor/marker policy, pagination, workflow identity, worker numbering,
provider routes and product ranking remain adopter-owned.

Completion/merge authority, production adopter CAS enforcement, golden live
same-view comparison, provider bridges, observed bounded execution and tested
rollback/cutover remain follow-on work. No ComicPile file, workflow, schedule or
archived evidence changed. No Rotisserie issue was executed autonomously.
Issue #29 remains open.

## Bounded exact-head completion and guarded merge

The fourth issue #29 slice supplies `CompletionController.tick`, pure
`plan_completion`, `CompletionRequest`, `CompletionEvidence`, `CompletionView`,
`CompletionBinding`, `CompletionRecord`, `CompletionEffect` and version-1
`CompletionResult`. It selects the first adapter-ordered completion demand and
attempts at most one merge through `CompletionPort.acquire`, `claim` and
`complete`. A held first demand is never skipped. No repair/review loop, worker
execution, label normalization or post-merge operation is performed.

This follows the working ComicPile path: semantic exact-head authorization,
contributor exclusion, required CI, mechanical readiness, then an expected-head
merge. The existing contributor-set domain verdict is reused directly. The
older `readiness_decision` and `CoordinationService.completion` remain local
simulation contracts with single-producer readiness; neither grants this
controller authority. Formal host approvals in the graph cannot satisfy the
separate semantic-review evidence lane. A formal change request or unresolved
thread can still block through adopter-supplied mechanical readiness evidence.

### Authority and observations

`ReviewCandidate` is reused as complete, trusted, fresh exact-head provenance.
The producer must be known, attested, match the graph and belong to the complete
reachable contributor lineage. All contributor/approver identities must resolve
to known graph workers. Missing provenance holds even with two approvals: the
domain's historical fallback does not confer controller mutation authority.

Completion evidence separately includes complete trusted latest semantic review
records and exact-head CI/readiness `Check` values. Its attestation covers every
required read, pagination, identity ambiguity, revoked reviews and invalidated
checks. The approved semantic workers must equal the provenance approver set;
no required pending/rejected review, stale revision or contributor approval can
satisfy independent review. CI/readiness required names must exactly match the
explicit non-empty configured sets. Missing gates, unknown states, failed checks,
stale/future evidence, untrusted/incomplete acquisition and contradictory
required graph checks hold closed. The graph's generic evidence objects alone
are not proof that a required mechanical gate passed.

Adapters translate open/draft/mergeability, current-head thread and host review
blockers, target eligibility and any additional product gates into named
readiness checks. Host context names and the distinction between optional and
required checks are explicit caller configuration; there are no product
contexts built into portable code. Fixtures attest to these shapes only, never
to live trust or authority.

`CompletionRequest` carries the configured repository, exact change/head,
completion owner, operation/lease identities, clock, observation-age bound,
positive lease TTL, inventory, WIP/backlog policy and required gate sets. The
acquisition includes fresh global trusted complete liveness, including unresolved
activity. Missing/unknown configured or leased-worker liveness holds even with
an empty queue. Completion participants (owner, contributors and semantic
approvers) must be idle. Known unrelated activity uses capacity; **any new
activity after selection** invalidates the observation. Every existing lease,
including expired leases, fences its target/owner. Duplicate implementations of
one work and durable completion records suppress a second attempt.

Results distinguish `no_eligible_completion_work`, `acquisition_failed`,
`provenance_hold`, `review_hold`, `ci_hold`, `readiness_hold`, `human_gate_hold`,
`prerequisite_hold`, `liveness_hold`, `capacity_hold`, `backpressure_hold` and
`ownership_hold`. Human boundaries require the existing human identity rule;
explicit prerequisites must be completed. An empty completion lane says nothing
about fresh/review demand. The optional backlog ceiling is caller policy, not
an inherited ComicPile production-intake threshold; callers draining completion
can leave it unset. The total WIP ceiling governs the claimed completion slot.

### Durable claim and effect obligations

Apply reacquires and compares the full semantic view before claim. Claim must
atomically persist **both** the full exact lease and a `claimed` completion
record. The binding includes the request, complete provenance/review/check
records and a digest of the entire authorizing graph, gates, demand, liveness,
capacity and prior durable records. This digest and the full binding contribute
to command identity. Merely storing a work-scoped lease loses required authority.

After claim, reacquisition must show that exact lease/binding/phase and only the
expected slot increment. Changed head, contributors, evidence, prerequisites,
human gates, worker activity, demand, capacity/backlog, renewal, replacement or
lost binding holds closed. Even refreshed observation timestamps conservatively
require a new decision. At every mutation boundary, the adapter must atomically
CAS the complete view token and full binding and recheck current clock, lease
expiry, observation age, eligibility and all authority conditions. The token
covers evidence and activity changes as well as graph/ownership changes.

**The actual merge must enforce `expected_revision` in its host transaction.**
Reacquiring the head immediately before an unfenced merge is insufficient.
The full-view fence must also remain effective through the mutation: an adopter
needs serialization/host enforcement that prevents or rejects evidence,
contributor, liveness and human-gate changes between CAS and merge. A local CAS
followed by an independently mutable remote host is not that guarantee.
This PR specifies and tests these obligations with a fake; it supplies no
production distributed enforcement adapter.

Before the remote effect, `complete` durably transitions the record to
`attempting`. It records `completed`, proven `refused`/`failed`, or `unknown`
before acknowledgement. A crash or lost acknowledgement leaves `attempting`
or `unknown`, durably distinguishable from proven non-application. These records
and completed tombstones survive lease expiry. Deduplication must bind the exact
change/head/request/evidence; `attempting`/`unknown` cannot be replayed as a fresh
merge, including under a new operation or lease identity. `EffectConflict` and
`EffectFailure` prove no merge; other exceptions or malformed outcomes are
uncertain. Results retain attempted command identities for reconciliation.

This slice retains ownership on every refusal, failure or uncertain outcome and
implements **no compensating release**. Safe adjudication/release is separate
follow-on work. Recovery adapters must honor completion receipts, never infer
safe release or retry from expiry/inactivity alone, and never delete uncertain
completion state through the generic lease-recovery port. A proven completed
change does not mark its linked work completed, close a host issue, publish a
release or authorize any other post-merge side effect. An adopter must keep
host closing-keyword side effects outside this command's authority or obtain
separate authorization; a host merge API must not silently broaden the scope.

### Public planning boundary

```sh
uv run python examples/controller/completion/caller.py
```

`controller-completion-plan --view VIEW --request REQUEST` accepts strict
version-1 JSON and requires a configured allowlisted repository and exact worker
inventory match. The request is fully versioned and includes explicit time/TTL,
capacity and required-context inputs. It records both exact input digests, the
view token, request and result in the append-only operator journal. No graph
state, credentials, mutation/merge port or worker is accessed. There is no apply
flag. Exit 0 means selected or a proven completion zero, 3 means held and 2 means
malformed input/configuration. The fixture is not golden live parity evidence.

### Source provenance and deliberate differences

ComicPile main was fetched read-only on 2026-10-09 at exact SHA
`8692c718458282af9300edffce72035145ff6655`. Inspection covered:

- `.github/scripts/factory_completion_controller.py` (`signal_completion_mode`,
  `candidate_is_batch_executable`) and `factory_full_completion_controller.py`
  (`current_demand`, `signal_dispatch`): demand/capacity signalling, recovery and
  product worker routing stay in adapters/configuration or earlier capabilities.
- `factory-review-controller.py` (`authorize_ready`, `mechanical_merge_gate`,
  `interpret_required_checks`, `current_head_review_gate`, `interpret_review_threads`,
  `poll_mergeable_gate`, `handle_review`, `record_contribution`) and
  `factory_review_policy.py` (`head_contributor_provenance`,
  `head_has_authorized_approval`, `trusted_comment_bodies`): exact-head semantic
  approval, contributor lineage and mechanical fail-closed behavior inform this
  controller; payload/marker/actor translation remains adapter-owned.
- `.github/workflows/factory-ready-merge-drain.yml`: current authorization and
  gates followed by `--match-head-commit`; main-health context, host concurrency,
  product ordering and post-merge closure are adopter policy. This operation
  retains the expected-head merge guard without activating the workflow.
- `.github/scripts/factory_post_merge_closure.py` and
  `.github/workflows/factory-release-reconcile.yml`: linked-issue closure,
  protected/manual/acceptance-parent handling, successor-work checks, closure
  sweeps and release-writer dispatch are distinct post-merge reconciliation.
  Their graphs and human acceptance gates need separate contracts before cutover.

These were compared with archived baseline
`2dc9b675e1a828b526f074c5f5c151293dedcdee`, particularly the old single-producer
`head_has_authorized_approval`, `authorize_ready`, identical mechanical-gate
sequence and archived merge/release workflows. Current source strengthens
contributor lineage exclusion; Rotisserie reuses that domain rule. The archived
single-producer authorization and current missing-provenance dual-review fallback
are deliberately insufficient for controller mutation authority.

| Behavior | Working source retained | Deliberate strengthening / boundary |
| --- | --- | --- |
| Semantic approval at current head | Required before merge | Complete trusted semantic records; formal approvals cannot substitute |
| Producer and prior repairers excluded | Current contributor lineage rule | Complete attested producer/lineage before ownership mutation |
| Required checks / mechanical gates | Missing, failed, unknown or unresolved gates block | Explicit exact-head CI/readiness sets and complete trusted acquisition |
| Merge head guard | Host `--match-head-commit` | Required at actual effect, alongside full-view CAS enforcement |
| Drain iteration and retries | Adapter-declared demand order | One attempt, no skip over holds and no automatic uncertain retry |
| Ownership / concurrency | Source controller and host serialization | Atomic durable exact binding, full-view CAS and explicit fresh liveness |
| Post-merge closure / release | Separate source scripts/workflows | No authority inherited; separate adopter reconciliation contract |

Tests exercise these source-derived gate scenarios and the deliberate refusal,
race and uncertainty strengthening. They do not execute a live ComicPile view
or prove golden same-view parity. Production adopter enforcement, golden live
parity, provider bridges, safe completion reconciliation/release, post-merge
reconciliation and observed rollback/cutover remain under #29. ComicPile, active
workflows, the issue-9 canary and all archived evidence are unchanged.

## Read-only same-view completion comparison

The next issue #29 slice runs the actual pinned ComicPile completion decision
functions and `CompletionController` planning against one repository-scoped
capture. `controller-completion-compare` emits both decisions and the generated
version-1 completion input, bound to the refreshed source SHA and exact capture
digest. It separates proven existing safety holds from unexplained differences
and records suppressed legacy label effects. Neither approval policy changed.
The source-backed golden fixture, public caller, capture obligations and limits
are documented in [completion-comparison.md](completion-comparison.md).
This is synthetic source comparison evidence, not live parity or host enforcement.

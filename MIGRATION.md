# Copy-first migration

## Provenance

- Source repository: `JoshCLWren/comic-pile` (the first adopter)
- Source commit: `2dc9b675e1a828b526f074c5f5c151293dedcdee`
- Destination repository: `JoshCLWren/rotisserie`
- Migration mode: byte-for-byte copy with source-relative paths preserved;
  destination-owned project and community-health files are authored separately
  from the source snapshot

## Archived snapshot removed

The byte-for-byte snapshot lived under `reference/legacy-factory/`, with a
rule-based inventory in `MIGRATION_INVENTORY.md`, a completeness test, and a
2026-10 drift inventory in `docs/migration/drift-2026-10.md`. All of it was
removed once the factory runtime under `factory/` replaced it. Commit
`67e4059e65531f9ab64c730791c75c11d76b6563` is the last commit that contains the
snapshot. Use `git show 67e4059e:reference/legacy-factory/<path>` to read an
archived file.

The snapshot copied the factory, fixed-model, and free-model workflows; the
controllers, workers, policy, model routing, recovery, visibility, and status
tooling; their Python, shell, and Node tests; and the worker, reviewer, and
orchestration prompts and skills. GitHub never executed the archived workflows.

The active workflows are the read-only `.github/workflows/ci.yml` and the
manually dispatched, issue-9-only `.github/workflows/canary-merge.yml`. Neither
has a schedule. `tests/test_rotisserie_actions.py` enforces this boundary.

## Standalone foundation

Rotisserie now has an independently buildable `src/rotisserie` package with
Apache-2.0 package metadata, Python 3.12–3.14 support, a locked development
environment, and one canonical `./scripts/verify` command. The prototype stays
outside the public package as traceable migration evidence.

## Generic graph domain

The first extraction slice now lives under `src/rotisserie/domain`. It defines
repository-scoped identities and immutable work, change, revision, worker,
lease, check, review, evidence, capacity, and human-boundary values. Validated
snapshots preserve explicit dependency and implementation relationships,
reject cycles and dangling references, serialize through a versioned durable
shape, and scope readiness evidence to an exact revision.

The prototype dependency scanner remains migration evidence: a compatibility
test proves that its accepted output maps into explicit graph edges. Parsing
host text, labels, payloads, and branch names is intentionally not part of the
domain API.

## Deterministic coordination policy

Pure policy under `src/rotisserie/domain/policy.py` now selects and ranks fresh
implementation work, suppresses duplicate changes, decides lease acquisition
and release at explicit clock boundaries, enforces producer/reviewer identity,
and evaluates readiness only from checks and reviews attached to the current
revision. Configurable WIP, reserved review capacity, and completion-backlog
pressure replace the archived Factory environment constants. GitHub labels,
provider routes, payload parsing, and mutations remain adapter concerns for
later phases.

## Secure GitHub graph adapter

The first external adapter now lives under `src/rotisserie/adapters/github`.
Its read side validates a caller-selected repository before translating issue,
dependency, pull request, revision, check, and review fixtures into domain
identities. Pull requests may originate from forks, but their base repository
and linked work must remain inside the configured graph. Evidence from an old
commit remains attached to that revision and cannot satisfy current-head
policy.

The write side exposes typed credential and transport protocols rather than
shelling out to `gh`. Every mutation is bound to one repository installation
and an allowlisted issue or change. Change mutations require an exact head;
labels and state are reconciled as one complete operation; and a content-derived
retry marker is persisted only after the returned state matches the request.
Dry-run mode records a credential-free plan and performs no credential lookup,
inspection, or write. No workflow or autonomous runtime is activated by this
adapter extraction.

## Application orchestration

Provider-neutral services under `src/rotisserie/application` now compose graph
policy with a versioned snapshot/effect protocol. Selection and refill remain
deterministic decisions, while claim, release, dispatch, review transition,
completion, and expired-lease recovery use compare-and-swap commands at every
mutation boundary. Change commands carry an exact revision, and stable
content-derived operation keys let adapters suppress duplicate effects after a
crash between the effect and its acknowledgement.

Operation results have a versioned serializable shape and stable failure
categories. The legacy workflow syntax, provider health heuristics, label
vocabulary, and GitHub CLI behavior remain host-specific migration evidence;
none is imported into the application layer or activated as a workflow.

## Worker runtime contracts

Provider-neutral contracts under `src/rotisserie/application/runtime.py` now
bind each assignment to one work target, worker, authority manifest, expiry,
and (for change work) exact revision. Trusted objectives and authority are
rendered separately from untrusted repository content. Attempt identities,
heartbeats, progress, terminal outcomes, revision evidence, and versioned
secret-free resume packets give AI executors and human bridges the same durable
handoff boundary.

The bounded runtime rejects expired authority, cross-repository results,
replayed state from another assignment, credential-like durable data, and
malformed executor output. It deterministically falls back only for unavailable
or rate-limited executors; no-diff, failure, timeout, and cancellation remain
terminal outcomes. Provider selection, credentials, subprocesses, prompts,
GitHub workflow syntax, and remote mutation remain adapter or product policy,
and no worker execution has been activated.

## Local operator surface

The `rotisserie` entrypoint now exposes inspect, plan, claim, run, review,
complete, recover, and doctor commands over a repository-scoped local adapter.
Schema-versioned TOML configuration requires an explicit repository allowlist;
all mutating commands emit and persist a dry-run plan unless both `--apply` and
the local mutation config gate are present. The adapter atomically persists
graph state and idempotency keys without network or credential access.

Append-only structured operation records carry correlation IDs and drive local
metrics. Diagnostic bundles recursively redact credential-shaped fields and
values. A checked-in fixture and operator guide exercise the full local
simulation from a clean clone. The CLI does not execute workers or expose the
GitHub mutation adapter, and no workflow, schedule, daemon, provider, or remote
mutation has been activated. Archived status dashboards remain visibility
evidence rather than a public Rotisserie API.

## Guarded self-dogfood

The operator now composes externally acquired GitHub fixture data with the
repository-scoped projector and deterministic scheduling policy. Its `dogfood`
command records fixture, read-only, and dry-run stages as append-only evidence,
including a digest of the exact source payload. Dry-run mutation plans use the
bounded GitHub adapter with credential and transport access replaced by
fail-fast sentinels, so producing evidence cannot inspect a token or contact
GitHub.

The command itself remains non-activating: it cannot apply a remote mutation,
dispatch a worker, approve activation, or create a workflow. Its recorded
evidence was reviewed before the separate canary activation described below.

Fixture, live read-only, and real-issue dry-run evidence has now been reviewed
and recorded in [`docs/dogfood-evidence.md`](docs/dogfood-evidence.md). Live
payloads can stream over standard input, leaving only a digest and structured
operation record.

The maintainer approved a manual, write-capable merge canary on 2026-09-27 and
amended issue #9 to use guarded autonomous merging rather than a human merge
gate. The canary is restricted to issue #9 and branch
`rotisserie/canary-9`, requires independent exact-head semantic review evidence
and required CI, rejects forks, and revalidates the head before an expected-SHA
merge. Producer/reviewer separation uses durable Rotisserie worker identities,
matching the first adopter's single-account Factory contract; formal GitHub approval is
not required. A repository variable is the kill switch. Server-side branch
protection requires the Python 3.12, 3.13, and 3.14 CI contexts. No worker
execution or schedule has been activated. The canary lifecycle and recovery
evidence is recorded in [`docs/dogfood-evidence.md`](docs/dogfood-evidence.md).

## External adopter shadowing

The first adopter integration slice defines a versioned, provider-neutral
decision-shadow contract under `src/rotisserie/application`. Legacy and
Rotisserie decisions can be normalized across eligibility, ranking, ownership,
review, completion, and recovery, then compared in a deterministic
machine-readable report that explains missing records and every value mismatch.
The operator exposes that contract as a read-only `shadow` command with hashed,
durable evidence and a distinct divergence exit status; it never initializes
graph state, acquires credentials, or contacts the adopter repository.

The first adopter's label vocabulary, retry rules, worker numbering, provider routes,
prompts, workflows, credentials, and rollback controls remain adopter policy or
adapter concerns. This slice performs no cross-repository read or mutation and
does not activate a workflow. The boundary inventory and staged integration
rules live in [`docs/adopter-integration.md`](docs/adopter-integration.md).

A pure application cutover policy now requires complete, distinct parity runs,
a tested rollback path, explicit operator approval, and a bounded adopter lane.
It holds closed on incomplete or divergent evidence, requires an observed
canary before expansion, and gives rollback requests precedence. The policy
returns a versioned decision only; adopter adapters still own enforcement and
no external mutation or workflow has been activated.

The operator exposes this policy through a versioned `adopt` command so an
external adopter need not import application internals. It validates serialized
shadow reports, hashes the supplied evidence, and records a non-mutating cutover
or rollback decision. Adoption decisions bind the reported current and next
stages, reject out-of-order entry or expansion, and always return an active
canary or expanded lane to the legacy stage on rollback; enforcement remains
entirely adopter-owned.

Rotisserie can now project all six normalized decision dimensions directly
from a versioned graph snapshot through a pure application function and the
read-only `decide` CLI. Explicit dimension declarations preserve coverage when
a queue is empty. The command binds evidence to the exact input digest and an
adopter-supplied graph revision, enforces configured repository scope, and
does not initialize graph state, acquire credentials, contact a host, or
perform a mutation.

The `shadow-project` CLI now provides the direct adopter handoff: it projects a
versioned graph snapshot and compares it with the legacy baseline atomically.
Its durable evidence binds both exact input digests, the projected decisions,
and the divergence report to one adopter revision. It remains repository
scoped and credential-free, and initializes no mutable graph state.

Adopter transition gates now consume versioned rollback-drill and canary-
observation evidence instead of trusting command-line booleans. Evidence is
bound to the exact bounded lane, stage transition, and adopter-owned control
revision; evidence from another lane or revision fails closed. The adopter
still owns producing that evidence and enforcing any authorized transition.

Authorized decisions can now be converted into a versioned, idempotent
transition command bound to the exact adopter control revision, lane, expected
stage, and target stage. This makes the adopter's required compare-and-swap
boundary explicit without giving Rotisserie a credential or host mutation
mechanism.

The first adopter now has a merged adopter translator and a read-only live acquisition path.
Two distinct live, same-view observations at adopter revisions
`cbea5e1ee3bff4cd76f4a7dc64d48f796b80e5f4` and
`657fe86d9a4af8f914ec8f36412368deb3027e70` each compared 94 decisions across all
six dimensions with zero divergence. Completion backlog and its configured limit are
explicit projection inputs, preserving backpressure as portable policy.

The first adopter also owns a bounded `issue-intake` compare-and-swap control that consumes
only the public transition command. Adopter tests cover immediate rollback and stale
or altered transition refusal. After a zero-divergence canary observation, the
checked-in adopter stage advanced to `expanded` at adopter commit
`87748a26485e97f3c061addb0c69b402bae591b1`.

Adopter PR #2958 merged runtime enforcement at
`a110f31b295e0f992ee0bf02466ac244d817ffb3`. The adopter's centralized assignment
boundary now builds a graph snapshot from its exact live controller view and invokes
the public, read-only `rotisserie decide` CLI before any fresh issue claim. The CLI
is pinned to Rotisserie commit `0067e8c49cb3a3142a04f6c56f19795a1e81ad3c`;
missing or invalid control and decision evidence holds issue intake closed, while
the adopter retains its existing PR completion path and immediate `legacy` rollback.

The post-merge adopter dispatcher run
`36633600180`
checked out the merge commit, installed the pinned CLI, evaluated the expanded
issue-intake boundary, and completed successfully. It emitted four PR assignments
and no issue assignment or fresh claim. This observed runtime evidence closes the
Phase 9 external-adopter migration contract; Phase 10 release hardening does not
expand Rotisserie's active workflow or mutation boundary.

## Config-derived worker inventory

The worker inventory is now config-derived rather than bounded by a hard-coded
ceiling. An optional `[workers]` table in operator configuration declares the
`expected`, `retired`, and `scheduled` worker identities, and a pure
`WorkerInventory` domain value validates that membership: the expected set is
non-empty, retired identities are disjoint from expected ones so a roster entry
cannot silently re-enroll a retired worker, and scheduled workers are a subset
of expected. The `plan` and `dogfood` commands derive their work-in-progress
ceiling from the inventory size, falling back to a single worker only when no
inventory is declared.

This extracts the generic mechanism behind adopter commits
`2a7f599b4ef5dbc776ad41c1674b2be9122b734b` and
`6461f689a77544b05f80c4d333f8a0cd37907fb1` (issue #3130), which replaced the
`<=71` fixed-model worker ceilings with a roster-derived expected-worker set.
The first adopter keeps its product-specific concerns — the `factory:N` label
vocabulary, the free-model TSV roster, the `factory-expected-workers.json`
lock, scheduled worker numbering, and model catalogs. Rotisserie takes only the
portable idea: derive the worker set and its capacity bound from configuration,
with no numeric maximum compiled into code.

## Exact-head review independence

The generic review-independence semantics from the first adopter's
`.github/scripts/factory_review_policy.py` (`head_has_authorized_approval` and
the head-contributor provenance it consumes) now live as a pure domain rule in
`src/rotisserie/domain/review.py`. A reviewer is eligible only when absent from
the exact-head contributor set — the recorded change lineage unioned with the
attested producer, because a repair stacks commits onto the prior head rather
than rewriting it. One independent approval authorizes the head. (Until
2026-11-22 a missing record or unattested producer demanded two approvals; the
owner retired that: missing attribution is a diagnostic, never a blocker.) The
only model rule is that a model known from trusted records to have authored
the PR must not approve it; see `evaluate_review_eligibility` in
`factory/.github/scripts/factory_review_policy.py`. The rule generalizes the
existing single-producer
`reviewer_is_independent`; `readiness_decision` is intentionally left on the
legacy check, and wiring the new rule into readiness is a later, additive step.

`src/rotisserie/adapters/github/review_evidence.py` is a read-only adapter that
decodes trusted review-thread comments into those signals. Marker and worker
vocabularies are adopter configuration, so no adopter marker strings enter the
domain. Contributor and semantic-review markers are honored only when the entire
comment body is one marker, closing marker-in-prose forgery for both families.
The producer is taken from the identity attested in the exact-head review
markers; a caller-supplied producer that contradicts it, or contradictory
attestations for one head, fail closed. The trusted-author set is empty by
default — a shared `GITHUB_TOKEN` Actions job posting as `github-actions[bot]`
is not trusted unless an adopter explicitly opts its controller identity in. The
adapter performs no mutation and activates no workflow.

The first adopter keeps the product-specific encoding: the repository-derived marker
prefixes, the `factory:<n>` worker labels, the `factory/<n>-…` branch and
`Worker: opencode-…-factory-<n>` body producer recovery, the semantic verdict
vocabulary, and worker GitHub App slugs.

## Operational runtime fast lane

The maintainer prioritized moving the operational Factory runtime out of
the first adopter on 2026-10-07, ahead of release publication. The first executable
slice (issue #28) extracts process lifecycle from the archived
`free-model-factory-worker.sh` and `free-model-factory-worker-primitives.sh` into
the existing provider-neutral runtime, a versioned application process contract,
a bounded POSIX process adapter, and the public `execute` entrypoint.

Execution requires an explicit active lease, configured repository/worker and
checkout scope, current change head, satisfied human boundaries, separate local
execution configuration, and `--apply`. Dry-run starts no subprocess. The
runtime enforces deadline/expiry, kills process groups, records heartbeats,
suppresses duplicate callers with durable receipts and local locks, and permits
fallback only for unavailability/rate limits. Interrupted receipts require
explicit matching resume evidence. Recognizable credentials and malformed or
foreign results are refused before persistence; raw process output is discarded.

The live adopter controller was inspected read-only at
`03b95add5c7d8363588ef2883c1a0d719e23a80b`. It still owns live acquisition,
claim/release, provider bridges, review/completion and workflow topology. No
adopter source or workflow changed in this slice. The runtime extraction is
not a live adopter cutover; same-view parity, bounded observation and rollback
remain prerequisites for deleting duplicated code. The full boundary, example
and recovery limits are in [`docs/worker-process.md`](docs/worker-process.md).

## Bounded fresh-assignment controller

The first issue #29 capability now composes deterministic fresh-work selection,
claim, execution handoff and proven dispatch-failure compensation over explicit
application acquisition/CAS/dispatch ports. Acquisition versions cover graph,
capacity and liveness together; unknown observations and ambiguous effects hold
closed, while expired leases require explicit recovery instead of implicit
reassignment. Trusted assignments use the existing #28 process contract.

The versioned `controller-plan` entrypoint and fixture-backed thin caller record
exact input digests without credentials, remote effects, graph initialization or
worker execution. The live source audit is pinned to adopter
`b83a5a7f2c82eaa4764ade87681c5be5db23d2e7`. This is the fresh-assignment slice,
not completion or full live-controller parity: recovery, exact-head independent
review/completion, concrete adopter enforcement, golden live comparison and
observed cutover/rollback remain under issue #29. The port obligations and
remaining boundaries are in [`docs/assignment-controller.md`](docs/assignment-controller.md).


## Bounded controller recovery

Issue #29 now also has an application stale-lease recovery operation and public
planning-only `controller-recovery-plan` fixture caller. One call releases at
most one exact expired lease after fresh trusted activity and complete idle
liveness attestations, reacquisition and durable full-view/full-lease CAS.
Observation failures, liveness holds, recent activity, conflicts and ambiguous
writes have distinct versioned results. This supersedes neither the local
coordination simulation nor any live adopter runtime.

The read-only source refresh used adopter main
`8692c718458282af9300edffce72035145ff6655` on 2026-10-09; source functions,
deliberate strengthening and adapter enforcement obligations are recorded in
[`docs/assignment-controller.md`](docs/assignment-controller.md). Production
CAS enforcement, exact-head review/completion, golden live parity, provider
bridges and observed rollback/cutover remain follow-on work. No external
repository, workflow or archived artifact changed.


## Bounded independent-review controller

The next issue #29 capability adds one exact-head independent-review assignment
and handoff over explicit acquisition, trusted liveness, atomic durable
claim/request CAS and dispatch ports. Complete trusted contributor-set and
producer provenance is required before mutation; formal host approvals and the
domain's unknown-provenance approval fallback confer no controller authority.
A full durable binding fences the lease, review request and process assignment
to one change/head. Reacquisition and full-view CAS guard claim, handoff and
proven-refusal compensation; uncertain outcomes retain ownership.

The planning-only `controller-review-plan` CLI and fixture caller record exact
input digests without graph initialization, credentials, effects or workers.
The read-only adopter refresh is pinned to main
`8692c718458282af9300edffce72035145ff6655` on 2026-10-09. Sources, deliberate
strengthening, race tests and production adapter obligations are documented in
[`docs/assignment-controller.md`](docs/assignment-controller.md). Completion /
merge authority, concrete adopter enforcement, golden live same-view parity,
provider bridges and observed rollback/cutover remain under issue #29. The first adopter,
active workflows and archived evidence are unchanged.


## Bounded completion controller

Issue #29 now includes one exact-head completion decision, durable claim and
expected-head merge handoff through explicit application ports. It reuses
complete trusted contributor-set provenance and domain independent review,
requires separate semantic-review and all configured exact-head CI/readiness
evidence, and preserves human gates, prerequisites, ownership, liveness and
capacity. Every effect requires fresh reacquisition and full-view/full-binding
CAS; the actual merge requires a host-enforced expected-head fence. Durable
attempting/unknown records preserve uncertain outcomes without compensation or
unsafe retries. Completion grants no linked-issue closure or release authority.

The public version-1 `controller-completion-plan` CLI and fixture caller perform
planning only. Adopter main was refreshed read-only at
`8692c718458282af9300edffce72035145ff6655` on 2026-10-09. The working semantic /
mechanical / expected-head merge sequence, deliberate safety strengthening,
source comparison and separately classified post-merge reconciliation are in
[`docs/assignment-controller.md`](docs/assignment-controller.md). Concrete
production enforcement, golden live same-view parity, provider bridges, safe
completion adjudication/release, post-merge reconciliation and observed
rollback/cutover remain follow-on work. Issue #29 remains open; the first adopter,
workflows, the issue-9 canary and archived evidence remain unchanged.

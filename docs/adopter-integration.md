# External adopter integration

Issue #10 introduces ComicPile as Rotisserie's first external adopter. The
integration proceeds by observation before mutation: both systems evaluate the
same acquired host snapshot, their normalized decisions are compared, and every
difference must be understood before a bounded canary can be approved.

## Boundary inventory

ComicPile retains product policy and host translation:

- Factory labels, stages, priority labels, branch conventions, and closing-keyword parsing;
- worker-number labels, local-worker identity, and legacy lease activity markers;
- no-diff retry windows and ComicPile-specific repair or migration lanes;
- model/provider routes, health rules, prompts, skills, and agent commands;
- GitHub workflow triggers, repository variables, credentials, and rollback controls.

Rotisserie owns portable coordination behavior:

- explicit work dependencies and implementation relationships;
- deterministic eligibility and ranking over a graph snapshot;
- leases, duplicate-work suppression, capacity, and backpressure;
- producer/reviewer separation and exact-revision review and CI evidence;
- completion readiness, expired-lease recovery, and human-only boundaries;
- versioned decision evidence and divergence reports.

The legacy code under `reference/legacy-factory/` remains evidence. ComicPile
must translate its live state through an external adapter and consume only
Rotisserie's versioned package or CLI boundary; it must not import Rotisserie
internals or receive a Rotisserie credential.

## Shadow report contract

An adopter first translates one acquired host view into Rotisserie's versioned
`GraphSnapshot` shape. Rotisserie can then produce its side of the comparison
without the adopter reimplementing scheduling policy:

```bash
rotisserie --config operator.toml decide \
  --snapshot comic-pile-graph.json \
  --revision comic-pile-snapshot-2026-09-28T120000Z \
  --at 1790596800 > rotisserie-decision-evidence.json
```

`decide` evaluates eligibility, ranking, active ownership, exact-head review,
completion readiness, and expired-lease recovery. Its decision snapshot
explicitly declares all six dimensions even when a dimension has no subjects,
so empty recovery or ranking queues still count as observed coverage. The
opaque revision must identify the same acquired host view used by the legacy
baseline. The command hashes the exact graph input, records a non-mutating
operation, rejects graph identities outside the configured repository, and
does not initialize mutable graph state or contact the host.

`rotisserie.application.DecisionSnapshot` is schema version 1. An adopter emits
one snapshot for the legacy baseline and uses `decide` for Rotisserie. Each observation
uses a stable subject identifier and one of six required dimensions:
eligibility, ranking, ownership, review, completion, or recovery. Outcomes and
reasons are adopter-normalized strings; ranking observations also carry a
zero-based rank.

`compare_decisions` returns a schema-versioned `ShadowReport`. It reports
missing observations and every outcome, reason, and rank mismatch in stable
dimension/subject order. A report with `matches: true` contains no divergence.
Only dimensions declared by both inputs count as observed coverage. Comparison
fails closed unless both inputs name the same graph revision. That
revision is retained in the report so results cannot be mistaken for evidence
about a different host snapshot. Reports also retain their observed decision
dimensions so later cutover policy can reject incomplete parity evidence.

An adopter can compare two normalized snapshot files through the stable CLI:

```bash
rotisserie --config operator.toml shadow \
  --baseline comic-pile-decisions.json \
  --candidate rotisserie-decisions.json
```

Exactly one input may be `-` to stream it over standard input. The command does
not initialize local graph state or acquire host data. It records only the two
input digests, the normalized report, and `remote_mutation: false` in the local
operation journal. A match exits 0; explained divergence exits 3; malformed
input or a revision mismatch exits 2.

For the normal integration path, the adopter can avoid materializing an
intermediate Rotisserie decision file. `shadow-project` projects the supplied
graph and compares it with the legacy baseline in one read-only operation:

```bash
rotisserie --config operator.toml shadow-project \
  --baseline comic-pile-decisions.json \
  --snapshot comic-pile-graph.json \
  --revision comic-pile-snapshot-2026-09-28T120000Z \
  --at 1790596800
```

The baseline must name the requested revision. The durable evidence includes
the exact baseline and graph digests, the projected Rotisserie decisions, and
the complete shadow report. Exactly one input may be streamed on standard
input, allowing the adopter to keep an acquired host view out of durable local
storage. The command enforces configured repository scope, uses the same exit
statuses as `shadow`, and never acquires credentials or mutates local graph or
remote host state.

This contract performs no acquisition or mutation. Snapshot acquisition,
ComicPile translation, report storage, credential scope, and later canary
controls belong to the adopter integration. Mutation remains prohibited until
shadow reports explain all divergence, rollback is tested, and an operator
explicitly approves a bounded lane.

## Staged cutover policy

`rotisserie.application.adoption_decision` is the provider-neutral cutover
boundary. An adopter supplies distinct matching shadow reports, a non-empty
lane scope, versioned evidence that rollback was tested, and explicit operator
approval.
The policy holds closed if evidence is insufficient, replayed, incomplete, or
divergent. Expansion additionally requires an observed canary. The required
number of matching runs is configurable rather than hidden in adopter code.

Rollback-drill and canary-observation evidence is bound to the exact lane and
an adopter-owned control revision. A rollback drill must show an active canary
or expanded lane returning to legacy; a canary observation must show the lane
remaining in canary. Evidence for a different lane or control revision cannot
authorize a transition. The rollback switch takes precedence over missing or
failed parity evidence so an adapter can always leave a canary or expanded
lane. The returned decision is schema-versioned and machine-readable, but it
performs no mutation. ComicPile continues to own the physical switch,
credential scope, live evidence acquisition, and enforcement of the returned
lane at its mutation boundary.

The same policy is available without importing Rotisserie internals:

```bash
rotisserie --config operator.toml adopt \
  --report parity-1.json --report parity-2.json \
  --evidence rollback-drill.json --control-revision controls-2026-09-28 \
  --lane issue-intake --subject label:ready \
  --minimum-matching-runs 2 --operator-approved
```

`rollback-drill.json` uses the public adoption-evidence schema:

```json
{"schema_version":1,"kind":"rollback_drill","lane":{"name":"issue-intake","subjects":["label:ready"]},"control_revision":"controls-2026-09-28","from_stage":"canary","to_stage":"legacy"}
```

`adopt` reads versioned shadow reports, records their digests and the decision,
and exits 3 when policy holds the transition. Its schema-version-2 decision
records both the adopter-reported current stage and the authorized next stage.
Entering a canary is valid only from `legacy`; expansion requires
`--current-stage canary` plus observed canary evidence. A rollback from
`canary` or `expanded` needs no reports or approvals and returns to `legacy`:

```bash
rotisserie --config operator.toml adopt \
  --lane issue-intake --subject label:ready \
  --control-revision controls-2026-09-28 \
  --current-stage canary --rollback-requested
```

Redundant or out-of-order transitions hold closed. The command never enforces
the decision or performs a remote mutation; ComicPile must map the bounded lane,
reported stage, and action onto its own independently scoped switch and reject
the result if the host-side stage changed after evaluation.

For CLI-only adopters, an authorized `adopt` result includes that same
schema-version-1 command as `evidence.transition`. Held decisions emit a null
transition. This lets an adopter apply the exact public compare-and-swap contract
without importing Rotisserie internals or reconstructing an operation key.

An adopter that imports the public package can pass an authorized decision to
`prepare_adoption_transition`. The returned schema-version-1 command binds the
lane and stage change to the exact adopter-owned control revision, and includes
a deterministic operation key for retry suppression. The adopter adapter must
apply it as a compare-and-swap: both `control_revision` and `expected_stage`
must still match before changing only the named lane to `target_stage`. A hold
decision cannot produce a transition. The command contains no credential or
host mutation mechanism; those remain in the adopter's independently scoped
adapter.

## Live parity observations

ComicPile's read-only adopter capture was exercised on 2026-09-28 against fetched
upstream commit `cbea5e1ee3bff4cd76f4a7dc64d48f796b80e5f4`. The captured host view
contained 44 work nodes, three linked changes, 26 exact-head checks, one semantic
review, and no active leases. Its source digest was
`d1bf88323897d965f005a7acd56e5bbea2257136fc213ada7cd09900c37a75e5`.

The atomic `shadow-project` run supplied ComicPile's observed completion backlog of
eight and configured limit of eight as explicit projection inputs. It compared 94
observations across all six required dimensions with zero divergences. The normalized
baseline digest was
`808f039d78cd863fecbbf2f41ba9595b58cfefb2e05d559fb0d9679bde7103bf`; the
graph snapshot digest was
`4c21c44b4c17bdbf9d41780f1058070129084520c4f0c9f5295edb21b440b3e4`.
No host payload, credential, or mutation plan is retained here.

Adopters may also supply `--active-changes` and `--wip-limit`. This preserves
production-side backpressure when a host pauses fresh intake because its active change
count has reached a configured limit, even when its completion backlog is not full.

This first observation did not authorize cutover. A distinct matching run, bounded
lane definition, adopter-owned rollback drill, and explicit transition approval were
still required before a canary could be entered.

A second read-only observation was captured on 2026-09-28 from ComicPile commit
`657fe86d9a4af8f914ec8f36412368deb3027e70`. The capture again contained 44
work nodes, three linked changes, 26 exact-head checks, one semantic review, and no
active leases. Its source digest was
`e03cb2f7181302bc067c1f64d71c21be1c00d3811e9e5b21648ef06e083289ba`.

The second atomic `shadow-project` run compared 94 observations across all six
required dimensions with zero divergences. The normalized baseline digest was
`20f5adaaf5ce2370e6b3ee2cdebd1076b031d2544f3feca4a9ebd1b85ee64c0a`; the
graph snapshot digest was
`4c21c44b4c17bdbf9d41780f1058070129084520c4f0c9f5295edb21b440b3e4`.
No host payload, credential, or mutation plan is retained in the repository.

The two distinct matching runs satisfy the configured parity-count prerequisite.
ComicPile commit `88fa4f3fa0bdcfb697133d37de3c88560b3e570b` added the adopter-owned,
bounded `issue-intake` control. Its tests exercise canary entry, exact-stage and
exact-revision compare-and-swap rejection, and immediate rollback while preserving
the original Factory.

After an explicit canary entry and a zero-divergence observation at ComicPile
revision `6fdf11f92f16006a89f750a3085eaebaff361825`, ComicPile commit
`87748a26485e97f3c061addb0c69b402bae591b1` advanced that bounded lane to
`expanded`. ComicPile PR #2958 then merged the runtime enforcement at commit
`a110f31b295e0f992ee0bf02466ac244d817ffb3`: the centralized assignment path
constructs a repository-scoped graph snapshot from its live controller view,
invokes only the public `rotisserie decide` CLI, and filters fresh issue candidates
to the returned eligible set before any claim mutation. Missing or malformed
control or decision evidence holds fresh intake closed; PR completion remains on
the adopter-owned path, and changing the control back to `legacy` is the immediate
rollback.

Post-merge dispatcher run
[`36633600180`](https://github.com/JoshCLWren/comic-pile/actions/runs/36633600180)
checked out that merge commit, installed Rotisserie from the exact pinned commit
`0067e8c49cb3a3142a04f6c56f19795a1e81ad3c`, and completed successfully. The
expanded intake boundary was evaluated during centralized assignment without an
unavailable/fail-closed error. All four emitted assignments were existing pull
requests (`#2951`, `#2960`, `#2953`, and `#2960`); the run emitted no issue
assignment or fresh issue claim. This is the first observed expanded-stage runtime
execution and completes the bounded Phase 9 adoption contract without removing the
original Factory.

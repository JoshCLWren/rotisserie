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

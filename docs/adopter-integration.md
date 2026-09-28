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

`rotisserie.application.DecisionSnapshot` is schema version 1. An adopter emits
one snapshot for the legacy baseline and one for Rotisserie. Each observation
uses a stable subject identifier and one of six required dimensions:
eligibility, ranking, ownership, review, completion, or recovery. Outcomes and
reasons are adopter-normalized strings; ranking observations also carry a
zero-based rank.

`compare_decisions` returns a schema-versioned `ShadowReport`. It reports
missing observations and every outcome, reason, and rank mismatch in stable
dimension/subject order. A report with `matches: true` contains no divergence.
Comparison fails closed unless both inputs name the same graph revision. That
revision is retained in the report so results cannot be mistaken for evidence
about a different host snapshot.

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

This contract performs no acquisition or mutation. Snapshot acquisition,
ComicPile translation, report storage, credential scope, and later canary
controls belong to the adopter integration. Mutation remains prohibited until
shadow reports explain all divergence, rollback is tested, and an operator
explicitly approves a bounded lane.

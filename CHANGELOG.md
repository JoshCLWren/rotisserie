# Changelog

All notable changes to Rotisserie will be documented in this file.

The project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and [Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the
public version is below `1.0.0`, minor releases may change unstable APIs; every
such change must be called out here and in its release notes.

## [Unreleased]

### Added

- Opt-in operational worker execution through `execute`, a strict version 1
  assignment/process JSON contract, and a trusted POSIX subprocess adapter.
  Active-lease, repository, inventory, human-boundary and exact-head checks
  guard execution; shared deadlines, cancellation, heartbeat records, local
  duplicate locks and durable receipts bound each attempt. Only unavailable
  or rate-limited results fall back. Dry-run starts no subprocess and the
  separate `[execution].enabled` gate is disabled by default. This activates
  no workflow, automatic dispatch, remote mutation or live adopter cutover.
- Portable, host-neutral per-node queue-eligibility rules (`evaluate_eligibility`,
  `EligibilitySignals`, `EligibilityVerdict`) and the exclusion codes they emit.
  `implementation_decision` delegates to this engine for one canonical
  `WorkBlock` ordering and verdict shape, with label and marker encoding left to
  the adopter translator.
- Config-derived worker inventory via an optional `[workers]` TOML table
  declaring `expected`, `retired`, and `scheduled` worker identities. `plan`
  and `dogfood` derive their work-in-progress ceiling from the `expected` size
  and report the resolved `wip_limit`. The `--max-active` flag is now an
  explicit override (canonically spelled `--wip-limit`) whose default changed
  from a fixed `1` to the configured inventory size, falling back to `1` only
  when no `[workers]` table is present.
- Provider-neutral dispatcher zero-work short-circuit: a single-read
  `dispatch_plan` tick that reports a trustworthy `NO_ELIGIBLE_WORK` status
  versus a capacity-bounded `WORK_AVAILABLE`, distinguishing genuine idle from
  WIP-capacity or intake-backpressure holds, surfaced in the `plan` CLI
  envelope via a new `dispatch` field and `--active-changes` flag.
- Portable exact-head review-independence rule in the domain: a reviewer must be
  absent from the exact-head contributor set (recorded lineage unioned with the
  attested producer). A single independent reviewer authorizes the head only
  when provenance is recorded and the producer is attested by exact-head
  evidence; an unattested or unknown producer, or unrecorded provenance, fails
  closed to two distinct eligible reviewers, each carrying a distinct stable
  block code.
- Read-only GitHub review-evidence adapter that projects trusted review-thread
  comments into those signals. Both contributor and semantic-review markers are
  honored only as whole-body comments; identities are normalized once (strip and
  casefold) so exclusion is case-insensitive and the reserved ``unknown``
  sentinel cannot be forged in any casing on any field; the attested producer is
  authoritative over any caller-supplied value; and the trusted-author set is
  empty by default so an adopter must opt its controller identity in. The
  adapter performs no mutation.
- Versioned, idempotent adopter transition commands that bind an authorized
  decision to an exact control revision, bounded lane, and compare-and-swap
  stage change without carrying credentials or host mutation behavior.
- Versioned adopter rollback-drill and canary-observation evidence bound to an
  exact lane, stage transition, and adopter-owned control revision.
- Atomic, read-only `shadow-project` CLI projection and comparison with exact
  baseline and graph digests, repository scoping, and no intermediate
  Rotisserie decision file.
- Schema-version-2 adoption decisions with explicit current and next stages,
  fail-closed transition ordering, and unconditional active-lane rollback to
  the legacy stage.
- Read-only `decide` CLI projection from versioned graph snapshots into all six
  adopter-shadow dimensions, with explicit empty-dimension coverage, input
  digests, repository scoping, and no mutable graph-state initialization.
- Non-mutating `adopt` CLI decisions over validated shadow reports, including
  bounded lanes, explicit approval gates, and unconditional rollback requests.
- Fail-closed external-adopter cutover and rollback decisions with bounded
  lanes, complete shadow-evidence checks, and explicit operator approval.
- Versioned external-adopter decision snapshots and deterministic shadow
  reports across eligibility, ranking, ownership, review, completion, and
  recovery, with exact-graph-revision enforcement.
- Read-only `shadow` CLI comparison with input digests, durable reports, and a
  distinct exit status for explained adopter divergence.
- Guarded dogfood payload streaming over standard input, allowing live
  read-only projection without retaining raw GitHub responses.
- Non-activating self-dogfood commands for fixture projection, read-only
  decisions, credential-free GitHub mutation plans, and durable evidence.
- Standalone Python package, locked development environment, and canonical
  verification commands.
- Explicit migration inventory for every archived source artifact.
- Provider-neutral graph identities, entities, relationships, validation, and
  versioned snapshot serialization under the pure domain boundary.
- Bounded provider-neutral worker assignments, authority manifests, attempt
  events, exact-revision evidence, secret-free resume packets, and deterministic
  executor fallback contracts.
- Versioned, repository-allowlisted local operator configuration and a
  credential-free fixture simulation adapter with atomic durable state.
- JSON CLI commands for graph inspection, planning, claim, bounded run, review,
  completion, recovery, and diagnostics; mutations are dry-run by default.
- Schema-versioned structured operation records, correlation IDs, derived
  metrics, and recursively redacted diagnostic bundles.

## Release notes

Each release entry must describe added, changed, deprecated, removed, fixed,
and security-relevant behavior where applicable. GitHub release notes should
link to the matching changelog entry, state supported Python versions, and
identify any migration or compatibility action an adopter must take.

[Unreleased]: https://github.com/JoshCLWren/rotisserie/compare/v0.1.0...HEAD

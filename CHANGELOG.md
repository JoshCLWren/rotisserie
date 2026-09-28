# Changelog

All notable changes to Rotisserie will be documented in this file.

The project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and [Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the
public version is below `1.0.0`, minor releases may change unstable APIs; every
such change must be called out here and in its release notes.

## [Unreleased]

### Added

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

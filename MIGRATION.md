# Copy-first migration

## Provenance

- Source repository: `JoshCLWren/comic-pile`
- Source commit: `2dc9b675e1a828b526f074c5f5c151293dedcdee`
- Destination repository: `JoshCLWren/rotisserie`
- Migration mode: byte-for-byte copy with source-relative paths preserved;
  destination-owned project and community-health files are authored separately
  from the source snapshot

## Included in this phase

- Factory, fixed-model, and free-model GitHub workflows
- Factory controllers, workers, policy, model routing, recovery, visibility,
  and status tooling
- Factory-focused Python, shell, and Node tests and their fixtures
- Worker, reviewer, and orchestration prompts and skills
- Autonomous Factory policy and execution/acceptance protocols
- The existing generic dependency/executable-policy extraction reference

Every archived path is assigned a future treatment by the checked inventory in
[`MIGRATION_INVENTORY.md`](MIGRATION_INVENTORY.md). Classification does not
move, activate, or make an archived artifact public.

## GitHub Actions safety boundary

Imported workflows are preserved under
`reference/legacy-factory/.github/workflows/`, which GitHub does not execute. They remain
available as migration evidence but cannot dispatch workers, merge pull
requests, create issues, publish status pages, or consume ComicPile-oriented
secrets from this repository.

The only active workflow is `.github/workflows/ci.yml`. It is Rotisserie-only,
has read-only repository permissions, and runs local compilation plus copied
policy/controller tests. It has no schedule and performs no issue, pull-request,
deployment, Pages, database, or external-repository mutations.

`tests/test_rotisserie_actions.py` enforces this boundary. Archived source tests
are retained for behavioral archaeology but are not Rotisserie CI.

ComicPile files were not removed or modified. Runtime names, repository names,
labels, and paths have intentionally not been generalized yet; preserving the
working implementation and tests is the baseline for later extraction work.

## Issue cleanup

The superseded Latticery extraction issues `JoshCLWren/Latticery#1` through
`#6` were closed. ComicPile's matching coordination issues `#2870` through
`#2875` were already closed. No new Factory issue was created for this copy.

## Verification baseline

The copied controller-focused Python suite passes 99 tests, and the copied
Node visibility suite passes 13 tests. In the broader copied suite, 468 tests
pass and 12 fail. Those 12 failures reproduce source-repository drift:

- ten autonomous-policy tests still assert policy version 24 while the source
  policy is version 25;
- two OpenCode tests refer to `.github/workflows/opencode.yml`, which does not
  exist at the source commit.

These source mismatches are recorded rather than repaired during the
byte-for-byte copy phase.

The final audit found 144 byte-identical source paths, plus the preserved
model-recommendation symlink. Every ComicPile tracked path containing
`factory` or `ralph` is present in this repository.

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

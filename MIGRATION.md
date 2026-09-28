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

The active workflows are the read-only `.github/workflows/ci.yml` and the
manually dispatched, issue-9-only `.github/workflows/canary-merge.yml`. Neither
has a schedule. The canary alone has the tested `contents: write` and
`pull-requests: write` scopes; it cannot mutate an external repository or run
pull-request code with its token.

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
matching ComicPile's single-account Factory contract; formal GitHub approval is
not required. A repository variable is the kill switch. Server-side branch
protection requires the Python 3.12, 3.13, and 3.14 CI contexts. No worker
execution or schedule has been activated. The canary lifecycle and recovery
evidence is recorded in [`docs/dogfood-evidence.md`](docs/dogfood-evidence.md).

## External adopter shadowing

The first ComicPile integration slice defines a versioned, provider-neutral
decision-shadow contract under `src/rotisserie/application`. Legacy and
Rotisserie decisions can be normalized across eligibility, ranking, ownership,
review, completion, and recovery, then compared in a deterministic
machine-readable report that explains missing records and every value mismatch.
The operator exposes that contract as a read-only `shadow` command with hashed,
durable evidence and a distinct divergence exit status; it never initializes
graph state, acquires credentials, or contacts the adopter repository.

ComicPile label vocabulary, retry rules, worker numbering, provider routes,
prompts, workflows, credentials, and rollback controls remain adopter policy or
adapter concerns. This slice performs no cross-repository read or mutation and
does not activate a workflow. The boundary inventory and staged integration
rules live in [`docs/adopter-integration.md`](docs/adopter-integration.md).

# Rotisserie

**Let your AIs cook on your issues.**

Rotisserie is an open-source graph-engineering platform for coordinating AI
workers across a software backlog. It treats issues, dependencies, pull
requests, reviews, checks, leases, and human decisions as one live work graph—
then moves the right worker to the right node without losing safety or context.

Rotisserie builds on operating experience with parallel implementation,
independent review, repair, CI, merge readiness, model fallback, and
interrupted-work recovery. Its job is to make those mechanics portable.

> **Current state: external-adopter integration is in progress.**
> Rotisserie now has a standalone package, generic graph and coordination
> policy, secure GitHub adapter, application orchestration, and provider-neutral
> worker contracts, plus a local CLI with durable state and diagnostics. The
> operator can produce fixture, read-only projection, and credential-free
> mutation-plan evidence, completed its guarded merge canary, and now exposes a
> versioned decision-shadow contract for adopter parity reports. General
> autonomous execution and external-repository mutation remain disabled.

## What “graph engineering” means

A backlog is not a queue. Work becomes executable only when its relationships
and evidence allow it:

```text
issue ──depends on──> issue
  │                    │
  └──implemented by──> change ──has head──> revision
                           │                    │
                           ├──reviewed by──> worker
                           ├──validated by──> check
                           └──blocked by──> human boundary
```

Rotisserie makes this graph explicit. Scheduling is a policy decision over the
graph, not a prompt that hopes every worker remembers the current state.

The intended result is a system that can:

- select executable work from dependencies, priority, leases, and capacity;
- coordinate multiple implementation and review workers without duplicate work;
- require independent review and exact-revision evidence before merge;
- recover safely from stale claims, crashed workers, and provider failures;
- preserve a durable handoff packet when a different worker must continue;
- keep credentials, mutation authority, and human-only decisions outside model
  prompts;
- support different model providers, agent CLIs, and human workers behind one
  runtime contract.

## Design principles

1. **The graph is the source of coordination truth.** Prompts guide workers;
   durable state decides what is true.
2. **Claims are leases, not ownership.** A stopped worker must not strand work.
3. **Evidence belongs to an exact revision.** A new commit invalidates stale
   review and readiness.
4. **Policy is pure; adapters perform effects.** Ranking and eligibility should
   be deterministic and testable without GitHub or a model provider.
5. **Mutation is bounded.** Repository allowlists, minimal tokens, explicit
   permissions, and fail-closed checks define what automation may change.
6. **Humans remain first-class graph participants.** Architecture decisions,
   destructive actions, and product acceptance can be explicit human gates.
7. **Models are replaceable workers.** Coordination must survive model churn.

## Repository map

| Path | Purpose |
|---|---|
| `src/rotisserie/domain/` | Provider-neutral graph values and pure coordination policy |
| `src/rotisserie/application/` | Versioned, idempotent coordination use cases over effect protocols |
| `src/rotisserie/application/runtime.py` | Bounded worker, attempt, evidence, recovery, and executor contracts |
| `src/rotisserie/adapters/github/` | Repository-scoped GitHub projection and bounded mutation contracts |
| `src/rotisserie/operator/` | Versioned local configuration, durable simulation state, records, metrics, and diagnostics |
| `src/rotisserie/cli.py` | One-operation CLI with dry-run mutation plans and stable JSON output |
| `prototype/` | Dependency and eligibility migration evidence with domain compatibility tests |
| `tests/` | Rotisserie repository-safety guards |
| `reference/legacy-factory/` | Archived migration evidence, code, tests, prompts, and disabled workflows |
| `.github/workflows/ci.yml` | Read-only Rotisserie CI |
| `.github/workflows/canary-merge.yml` | Manually dispatched, issue-9-only merge canary |

See [MIGRATION.md](MIGRATION.md) for exact snapshot provenance and known source
test drift. [MIGRATION_INVENTORY.md](MIGRATION_INVENTORY.md) accounts for the
intended treatment of every archived artifact.

## Safety boundary

Rotisserie does **not** currently dispatch agents, mutate external issues, or
deploy software. Imported operational Actions live outside `.github/workflows/`
and therefore cannot execute. The only write-capable workflow is the completed,
manually dispatched issue-9 canary; it is repository-scoped, allowlisted, and
unscheduled. Regression tests enforce this boundary.

The operational runtime will only be enabled after repository identity,
permissions, credentials, dry-run behavior, and end-to-end mutation tests are
Rotisserie-specific.

## Development

Rotisserie supports Python 3.12 through 3.14. Install
[uv](https://docs.astral.sh/uv/), then create the locked development
environment and run the same verification used by CI:

```bash
uv sync --locked
./scripts/verify
```

Build the source and wheel distributions with `uv build`. The package is still
pre-release and does not yet expose the prototype as a supported public API.
Version history and release-note conventions live in
[CHANGELOG.md](CHANGELOG.md).

### Local operator quickstart

The checked-in example is credential-free and cannot reach a hosting provider.
Use a temporary copy so the durable simulation state does not modify the
checkout:

```bash
demo_dir="$(mktemp -d)"
cp -R examples/local/. "$demo_dir/"
uv run rotisserie --config "$demo_dir/config.toml" inspect
uv run rotisserie --config "$demo_dir/config.toml" plan --at 10 --max-active 2
uv run rotisserie --config "$demo_dir/config.toml" \
  claim 1 human:producer --lease-id demo-lease --at 10 --expires-at 20
```

The final command only records and prints a dry-run plan. Add `--apply` to an
individual mutating command to change local simulation state; the config must
also explicitly set `local.mutations_enabled = true`. See
[the operator guide](docs/operator.md) for the full simulated lifecycle, JSON
contract, exit codes, recovery, and redacted diagnostics.

The [dogfood guide](docs/dogfood.md) and
[lifecycle evidence](docs/dogfood-evidence.md) describe the guarded self-dogfood
canary. The [external adopter guide](docs/adopter-integration.md) defines the
next phase's product-policy boundary and non-mutating decision-shadow contract.

## Roadmap

The [roadmap issue](https://github.com/JoshCLWren/rotisserie/issues/1) is the
canonical dependency-ordered plan. Current progress:

- [x] establish the standalone project foundation and license;
- [x] define the generic work graph domain model;
- [x] extract deterministic scheduling and coordination policy;
- [x] implement the secure GitHub graph adapter;
- [x] extract dispatch, completion, recovery, and capacity orchestration;
- [x] define provider-neutral worker, evidence, and executor contracts;
- [x] build the operator CLI, configuration model, persistence, and observability surface;
- [x] safely dogfood Rotisserie on its own repository;
- [ ] integrate ComicPile as the first external adopter through the public boundary;
- [ ] harden and publish the first supported open-source release.

No adopter needs to replace working automation before the new boundary is
proven.

## Contributing

Start with an issue whose prerequisites are complete. Preserve behavior before
generalizing it, keep provider and GitHub payload details outside the domain
layer, and accompany every policy change with deterministic tests.

Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. AI
contributors must also follow [AGENTS.md](AGENTS.md). Participation is governed
by our [Code of Conduct](CODE_OF_CONDUCT.md), and vulnerabilities should be
reported through the process in [SECURITY.md](SECURITY.md).

## License

Rotisserie is licensed under the [Apache License 2.0](LICENSE). It permits
commercial and private use, modification, and distribution while preserving
copyright and license notices, and it includes an explicit contributor patent
grant.

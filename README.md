# Rotisserie

**Let your AIs cook on your issues.**

Rotisserie is an open-source graph-engineering platform for coordinating AI
workers across a software backlog. It treats issues, dependencies, pull
requests, reviews, checks, leases, and human decisions as one live work graph—
then moves the right worker to the right node without losing safety or context.

The project is being extracted from the ComicPile Factory, a production system
that has already coordinated parallel implementation, independent review,
repair, CI, merge readiness, model fallback, and interrupted-work recovery.
Rotisserie's job is to turn those proven mechanics into a portable platform.

> **Current state: extraction baseline, not yet ready to install.** The complete
> Factory snapshot is here, but much of it still speaks ComicPile's vocabulary.
> Its operational workflows are deliberately disabled while the generic core
> and repository adapter are separated.

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
| `.github/scripts/` | Copied controllers, policies, worker helpers, and tests |
| `latticery_extraction/` | First generic dependency and eligibility policy slice |
| `scripts/` | Copied local worker and model-routing utilities |
| `tests/` | Factory regression tests plus Rotisserie safety guards |
| `docs/` | Source policy, execution protocol, and prompt documentation |
| `prompts/`, `.agents/`, `.opencode/` | Worker and reviewer instructions |
| `reference/comic-pile-workflows/` | Preserved, non-executable source workflows |
| `.github/workflows/ci.yml` | The only active Action; read-only Rotisserie CI |

See [MIGRATION.md](MIGRATION.md) for exact snapshot provenance and known source
test drift.

## Safety boundary

Rotisserie does **not** currently dispatch agents, mutate issues, merge pull
requests, deploy software, or operate ComicPile. The imported ComicPile Actions
live outside `.github/workflows/` and therefore cannot execute. A regression
test enforces that the sole active workflow is read-only, unscheduled, and free
of ComicPile coupling.

The operational runtime will only be enabled after repository identity,
permissions, credentials, dry-run behavior, and end-to-end mutation tests are
Rotisserie-specific.

## Development

The extraction is intentionally preserving source layout while package and CLI
boundaries are designed. With Python 3.14, `pytest`, and Node.js installed:

```bash
# Generic policy and active-Action safety
python -m pytest -q latticery_extraction tests/test_rotisserie_actions.py

# Copied controller and policy regression suite
PYTHONPATH=.github/scripts python -m pytest -q \
  .github/scripts/test_factory_epic_prd_policy.py \
  .github/scripts/test_factory_issue_pr_state_policy.py \
  .github/scripts/test_factory_review_controller.py \
  .github/scripts/test_factory_review_policy.py \
  .github/scripts/test_factory_review_thread_gate.py \
  .github/scripts/test_factory_stage5.py \
  .github/scripts/test_factory_work_policy.py \
  -k 'not test_workflows_delegate_mechanical_gates_to_controller and not test_fixed_model_factory_schedules_are_active'

# JavaScript controller helpers
node --test .github/scripts/*.test.cjs
```

The two deselected assertions require ComicPile's scheduled workflows to be
active, which is intentionally false in Rotisserie.

## Roadmap

The [open issues](https://github.com/JoshCLWren/rotisserie/issues) are the
canonical, dependency-ordered extraction plan. The major phases are:

1. establish an independent package, test, license, and terminology baseline;
2. extract the generic work graph and pure coordination policy;
3. put GitHub projection and mutation behind a secure adapter;
4. extract dispatch, completion, recovery, and capacity orchestration;
5. define provider-neutral worker and evidence contracts;
6. build an operator CLI, configuration model, and observability surface;
7. safely dogfood Rotisserie on its own repository;
8. integrate ComicPile as the first external adopter;
9. harden and publish the first supported open-source release.

No phase requires deleting the working Factory from ComicPile before the new
boundary is proven.

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

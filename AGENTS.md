# AGENTS.md

Instructions for AI agents working in Rotisserie.

## Mission

Build Rotisserie into a portable graph-engineering platform for coordinating
AI and human workers over issue and change graphs. Preserve the proven
ComicPile Factory behavior while separating generic domain policy from hosting,
repository, provider, and product-specific details.

## Current phase

This repository is a copy-first extraction baseline. Much of the copied code
still contains ComicPile names, paths, labels, issue markers, and operational
assumptions. Treat that code as evidence to classify and migrate—not as the
final public API.

The working ComicPile Factory remains independent and operational. Rotisserie
must not modify, delete, dispatch, or cut over ComicPile unless an issue and a
direct user instruction explicitly authorize that integration step.

## Non-negotiable safety rules

- Do not use the ComicPile Factory, Rotisserie, or any autonomous worker to
  implement Rotisserie issues unless the user explicitly requests it.
- Do not enable copied workflows from `reference/comic-pile-workflows/`.
- `.github/workflows/ci.yml` is the only active workflow until an approved
  activation issue changes the safety boundary.
- Active workflows must not reference ComicPile, use schedules, or receive
  write permissions during the extraction phases. Keep
  `tests/test_rotisserie_actions.py` green.
- Never place credentials in prompts, source, tests, logs, fixtures, comments,
  artifacts, or durable worker state.
- Repository mutations must be allowlisted, scoped to the configured target,
  and fail closed when identity or credentials are missing.
- Never push directly to another repository, close another repository's issue,
  or alter external Actions as an incidental part of Rotisserie work.
- Preserve `reference/comic-pile-workflows/` as migration evidence until an
  issue explicitly replaces or removes each reference artifact.

## Architecture boundaries

Maintain these layers as the extraction proceeds:

1. **Domain** — graph entities and pure deterministic policy. No GitHub payloads,
   shell commands, network calls, environment reads, or model-provider names.
2. **Application** — use cases such as claim, release, dispatch, review,
   completion, recovery, and capacity decisions. Depends on protocols, not
   concrete GitHub or model clients.
3. **Adapters** — GitHub, local process, persistence, clocks, and model/agent
   runtimes. Translate external data into domain types and own side effects.
4. **Entrypoints** — CLI and eventually guarded Actions. Parse configuration,
   construct adapters, invoke one application operation, and report results.

Dependency direction points inward. Domain code must never import adapters or
entrypoints.

## Core invariants

- Work selection is deterministic for the same graph snapshot and policy.
- Dependency references are explicit; casual issue mentions do not block work.
- One active lease controls a mutation target, and leases expire safely.
- Producer and required independent reviewer identities are distinct.
- Review, CI, and readiness evidence belongs to an exact revision SHA.
- A changed head invalidates stale evidence.
- Duplicate implementation of one work node is suppressed.
- Capacity and backpressure are explicit inputs, not hidden constants.
- Interrupted work leaves enough durable, secret-free state to resume safely.
- Human-only gates are representable and cannot be silently bypassed.
- Dry-run mode performs no remote mutation.

## Working rules

Before editing:

1. Read the issue and every declared prerequisite.
2. Inspect `MIGRATION.md` and the relevant source/reference implementation.
3. Classify the behavior as domain, application, adapter, entrypoint, or
   product-specific policy before choosing its destination.
4. Check the worktree and preserve unrelated user changes.

When editing:

- Prefer small public APIs and typed data structures.
- Keep policy functions pure wherever possible; inject clocks and effects.
- Use `pathlib`, precise type annotations, and explicit result types.
- Do not add compatibility aliases without a concrete current consumer.
- Do not preserve ComicPile vocabulary in a generic API merely because the
  copied implementation uses it.
- Do not weaken or delete a regression test unless the issue explicitly
  changes the invariant it proves. Add the replacement assertion first.
- Keep source-reference behavior traceable in commit messages or documentation.

## Tests

Run the checks proportionate to the files changed. At minimum:

```bash
python -m pytest -q latticery_extraction tests/test_rotisserie_actions.py
PYTHONPATH=.github/scripts python -m pytest -q \
  .github/scripts/test_factory_epic_prd_policy.py \
  .github/scripts/test_factory_issue_pr_state_policy.py \
  .github/scripts/test_factory_review_controller.py \
  .github/scripts/test_factory_review_policy.py \
  .github/scripts/test_factory_review_thread_gate.py \
  .github/scripts/test_factory_stage5.py \
  .github/scripts/test_factory_work_policy.py \
  -k 'not test_workflows_delegate_mechanical_gates_to_controller and not test_fixed_model_factory_schedules_are_active'
node --test .github/scripts/*.test.cjs
```

As the standalone package and its tooling land, use the canonical commands
defined by `pyproject.toml` and CI instead of copying this transitional command
list into new scripts.

For every change:

- run focused tests for the changed behavior;
- run `tests/test_rotisserie_actions.py` after any `.github/` change;
- run `git diff --check` before committing;
- report any source-baseline failures separately from new failures;
- never skip, xfail, or suppress a failing test merely to obtain green CI.

## GitHub and issue hygiene

- Issues are the canonical roadmap and must state dependencies explicitly.
- One issue should deliver one coherent architectural capability.
- Do not create duplicate phase issues or mirror them into ComicPile/Latticery.
- PRs should name the issue they satisfy and describe preserved invariants.
- Closing keywords are appropriate only when the entire issue contract is met.
- Do not activate autonomous execution merely because an issue label resembles
  a copied ComicPile Factory label.

## Definition of done

A change is done when its issue contract is fully implemented, relevant tests
pass, documentation reflects the new boundary, no unsafe workflow capability
was introduced, and the result is committed and pushed when the user requested
repository changes.

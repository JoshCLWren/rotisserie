# AGENTS.md

Instructions for AI agents working in Rotisserie.

## Mission

Build Rotisserie into a portable graph-engineering platform for coordinating
AI and human workers over issue and change graphs. Turn operating lessons into
generic domain policy separated from hosting, repository, provider, and
product-specific details.

## Current phase

This repository is an early extraction baseline. The archived copy of the
original factory was removed after extraction; git history keeps it (see
`MIGRATION.md`). Rotisserie contains no adopter-specific code. Each adopter
supplies its own values through configuration.

## Non-negotiable safety rules

- Do not use Rotisserie or any autonomous worker to implement Rotisserie issues
  unless the user explicitly requests it.
- `.github/workflows/ci.yml` and the manually dispatched, issue-9-only
  `.github/workflows/canary-merge.yml` are the only active workflows. Every
  other file in `.github/workflows/` is a reusable factory workflow whose only
  trigger is `workflow_call`; it never runs in Rotisserie. Any further
  activation requires an approved issue that changes the boundary.
- Active workflows must not use schedules. Only the approved canary may receive
  its tested `contents: write` and `pull-requests: write` scopes; all other
  active workflows remain read-only. Keep `tests/test_rotisserie_actions.py`
  green.
- Never place credentials in prompts, source, tests, logs, fixtures, comments,
  artifacts, or durable worker state.
- Repository mutations must be allowlisted, scoped to the configured target,
  and fail closed when identity or credentials are missing.
- Never push directly to another repository, close another repository's issue,
  or alter external Actions as an incidental part of Rotisserie work.

## Factory runtime boundary

`factory/` holds the factory runtime that adopter repositories install. The
factory workflows are reusable workflows in the root `.github/workflows/`, with
only an `on: workflow_call` trigger: they never run in Rotisserie and do not
count as active workflows. `factory/install/install.py` writes the thin callers
from `factory/install/callers/` into an adopter; each caller keeps the
triggers, permissions, concurrency and secrets, and calls the reusable workflow
at a Rotisserie ref. Adopter-specific values belong in the adopter's
`.rotisserie/config.toml`, repository variables, or caller inputs, never in a
reusable workflow or in `factory/`.
Run the factory suite in Docker (`scripts/test-in-docker`; see Tests).

Factory coordination state lives in a libSQL (Turso) store on the free plan
(`factory/docs/FACTORY_STATE_STORE.md`). Only trusted controller jobs get the
store, through `.github/actions/factory-store` (a 0600 token file, removed by
the job's last step); model jobs never do. Every write is one batch with its
authorizing checks inside it. Never add a paid service, an ORM, dual writes or
a fallback store; when the store is unavailable or its quota is spent, the
factory fails closed. Store tests run on SQLite files (`factory/tests/turso_*`).

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
- Do not preserve legacy vocabulary in a generic API merely because the
  archived implementation uses it.
- Do not weaken or delete a regression test unless the issue explicitly
  changes the invariant it proves. Add the replacement assertion first.
- Keep source-reference behavior traceable in commit messages or documentation.
- Any factory operation you do by hand (a script, a polling loop, or a chain of
  `gh` commands) becomes a `baste` command. Put its logic in a
  service under `src/rotisserie/factory_console/service/` that returns plain
  data, so the CLI only renders it and other consumers such as the web UI can
  call it.
- Every new or changed `baste` command also gets a working control
  in both user interfaces in the same PR: add it to the shared allowlist
  (`src/rotisserie/control_contract.py`), give it a control in
  `src/rotisserie/web_ui.html` (its script is `src/rotisserie/web_ui.js`), and map
  it in `src/rotisserie/control_parity.py`.
  The hosted control room (`factory/scripts/hosted_control_room.js` and
  `factory/scripts/hosted/`) routes it from the contract. A server allowlist
  entry alone does not count. `tests/test_control_parity.py` fails for any
  subcommand that is neither mapped nor listed in `CLI_ONLY` with a reason.
- Keep every source file at or under 500 lines, tests included.
  `scripts/check_file_size.py` enforces it as a lint step in
  `./scripts/verify`. Files listed in `tests/file_size_baseline.json` predate
  the rule: they may only shrink, so
  split one into focused modules before you add code to it.

## Tests

Run tests in Docker. The scripts need Linux tools that macOS lacks (`flock`,
`setsid`, GNU `stat` and `date`), so a run on a Mac fails tests that pass in
CI. `scripts/test-in-docker` runs every CI check in a Linux image that matches
the CI runners:

```bash
scripts/test-in-docker
```

Run focused tests in the same image:

```bash
scripts/test-in-docker factory uv run --locked pytest tests/test_x.py
scripts/test-in-docker root uv run --locked pytest tests/test_y.py
```

Docker must be able to see the checkout. On a Mac, keep worktrees under
`$HOME`, not `/tmp`. The full check must pass before a change is considered
complete.

For every change:

- run focused tests for the changed behavior;
- run `tests/test_rotisserie_actions.py` after any `.github/` change;
- run `git diff --check` before committing;
- report any source-baseline failures separately from new failures;
- never skip, xfail, or suppress a failing test merely to obtain green CI.

## GitHub and issue hygiene

- Issues are the canonical roadmap and must state dependencies explicitly.
- One issue should deliver one coherent architectural capability.
- Do not create duplicate or externally mirrored phase issues.
- PRs should name the issue they satisfy and describe preserved invariants.
- Closing keywords are appropriate only when the entire issue contract is met.
- Do not activate autonomous execution merely because an issue carries a
  legacy-looking automation label.

## Definition of done

A change is done when its issue contract is fully implemented, relevant tests
pass, documentation reflects the new boundary, no unsafe workflow capability
was introduced, and the result is committed and pushed when the user requested
repository changes.

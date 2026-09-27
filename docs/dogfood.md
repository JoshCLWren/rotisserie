# Guarded self-dogfood

Rotisserie exposes the first three self-dogfood stages without enabling remote
mutation or autonomous execution. The input is an explicitly acquired GitHub
projection payload in the adapter shape documented by
`tests/fixtures/github/graph.json`.

The command never fetches GitHub data. Operators acquire the payload through a
separately reviewed read-only process, then identify its provenance outside
Rotisserie. Rotisserie validates the configured repository against the payload,
projects the graph, makes deterministic scheduling decisions, and records the
payload SHA-256 in its local operation journal.

Acquisition must classify which host records are work nodes and provide only
explicit dependency edges. Tracking issues, discussions, and casual issue
mentions must not be inferred as executable work or dependencies.

Pass `--payload -` to read a payload from standard input. This lets a reviewed
read-only acquisition command stream host data into the projector without
retaining the raw response on disk. The digest in the operation record remains
the durable provenance link.

## Stages

Run each stage with a configuration whose repository and allowlist match the
payload:

```bash
uv run rotisserie --config dogfood.toml dogfood \
  --stage fixture --payload graph.json --at 0

uv run rotisserie --config dogfood.toml dogfood \
  --stage read-only --payload live-graph.json --at 0

acquire-live-graph-read-only | uv run rotisserie --config dogfood.toml dogfood \
  --stage read-only --payload - --at 0

uv run rotisserie --config dogfood.toml dogfood \
  --stage dry-run --payload live-graph.json --at 0 \
  --target issue:9 --label rotisserie:canary --state open
```

Use `change:NUMBER` plus `--expected-head` when planning a pull-request
mutation. Targets are exact and repository-scoped. Labels form the complete
planned label set rather than an incremental add/remove request.

Every result states `remote_mutation: false` and
`activation_approved: false`. Records are appended to
`local.state_directory/operations.jsonl`; no graph state file is created by
these commands. Dry-run mode constructs the normal GitHub mutation adapter with
credential and transport access forbidden, making accidental inspection or
write attempts fail immediately.

## Guarded merge canary

The maintainer approved the write-capable canary on 2026-09-27. The active
`canary-merge.yml` workflow is manual-only and can merge only a same-repository
pull request from `rotisserie/canary-9` into `main` that closes issue #9. Set
the repository variable `ROTISSERIE_CANARY_ENABLED` to `true` to enable the
kill switch, then dispatch the workflow from `main` with the pull-request
number and exact 40-character head SHA.

The workflow rejects forks, moved heads, drafts, other repositories, branches,
base branches, and issue targets. It requires successful required checks and
controller-persisted semantic review evidence from a worker identity distinct
from the producer and attached to the exact head. It then re-reads the head and
uses GitHub's expected-head merge guard. GitHub auto-merge is not enabled. The
workflow token is exposed only to the trusted merge adapter; no worker or
pull-request code runs with it.

Branch protection independently requires the Python 3.12, 3.13, and 3.14 CI
contexts and resolved conversations. The adapter requires the same complete
successful CI set and refuses GitHub command failures as explicit canary
errors. Formal GitHub `APPROVED` state is not required: the single authenticated
operator account cannot approve its own pull request. This follows ComicPile's
factory contract, where producer/reviewer separation is a worker-identity
boundary recorded in an exact-head semantic-review marker, not a second GitHub
account.

Rollback is immediate: set `ROTISSERIE_CANARY_ENABLED` to any value other than
`true`, or disable `canary-merge.yml`. Cancellation and stale-head drills must
be recorded before issue #9 is complete. Schedules remain prohibited until a
separate approval.

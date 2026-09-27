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

## Stages

Run each stage with a configuration whose repository and allowlist match the
payload:

```bash
uv run rotisserie --config dogfood.toml dogfood \
  --stage fixture --payload graph.json --at 0

uv run rotisserie --config dogfood.toml dogfood \
  --stage read-only --payload live-graph.json --at 0

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

## Activation boundary

Do not interpret successful dry-run evidence as canary approval. Issue #9
requires explicit maintainer approval before any write-capable workflow or
remote execution is added. Until then:

- `.github/workflows/ci.yml` remains the only active workflow;
- no schedule or write permission is allowed;
- no worker receives credentials or mutation authority;
- canary, implementation/review/completion, cancellation, and stale-head
  recovery drills remain inactive.

Before requesting activation, compare fixture and live decisions, review the
recorded plans and payload digests, and retain the evidence needed to explain
every difference.

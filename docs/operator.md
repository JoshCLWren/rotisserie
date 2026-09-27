# Local operator guide

The Rotisserie CLI currently operates only on a repository-scoped local graph
snapshot. It performs no network access, starts no daemon, and does not invoke
an AI provider. This is the supported safe simulation boundary before guarded
self-dogfood.

## Configuration

Configuration is TOML with `schema_version = 1`. The selected repository must
exactly match an entry in `repositories.allow`. Relative snapshot and state
paths are resolved from the configuration file, not the process working
directory. Mutations default to disabled when `local.mutations_enabled` is
omitted.

`examples/local/config.toml` is a complete credential-free example. Future
remote adapters must validate repository identity and credentials before an
effect; this local entrypoint neither accepts nor reads remote credentials.

## Commands

All commands emit one versioned JSON object on standard output.

- `inspect` reports graph counts.
- `plan` selects executable work and exact-head-ready changes.
- `claim`, `run`, `review`, `complete`, and `recover` are mutating commands.
- `doctor` validates local state and reports operation metrics; `--bundle`
  writes a redacted diagnostic file.

Mutating commands are dry-run unless `--apply` is present. A dry-run writes a
schema-versioned operation record but does not alter graph state. Applying also
requires `local.mutations_enabled = true`, providing two explicit gates.

## Simulated lifecycle

Copy the example to a disposable directory as shown in the README, then run:

```bash
config="$demo_dir/config.toml"
uv run rotisserie --config "$config" claim 1 human:producer \
  --lease-id demo-lease --at 10 --expires-at 20 --apply
uv run rotisserie --config "$config" run 1 human:producer --at 11 --apply
uv run rotisserie --config "$config" review 20 demo-head human:reviewer --apply
uv run rotisserie --config "$config" complete 20 demo-head --apply
uv run rotisserie --config "$config" recover --at 20 --apply
uv run rotisserie --config "$config" doctor --bundle
```

The fixture deliberately has separate implementation and ready-completion work
so each application boundary can be exercised without pretending that a local
dispatch produced a real change. `run` records one bounded dispatch effect; it
does not execute a worker.

## Durable output and exit codes

Local graph state is atomically replaced in `graph-state.json`. Append-only
`operations.jsonl` entries include schema version, timestamp, correlation ID,
command, dry-run status, and result. `doctor` derives counters from that journal
and can write `diagnostics.json`. Credential-shaped keys and values are redacted
from both records and bundles.

Exit codes are stable for schema version 1:

| Code | Meaning |
|---:|---|
| `0` | Inspection, plan, dry-run, applied operation, or no-op succeeded |
| `2` | CLI usage, configuration, input, or local-state validation failed |
| `3` | Coordination policy rejected the operation |
| `4` | Reserved for an operational failure that could not be classified safely |

Callers may supply `--correlation-id`; otherwise the entrypoint generates a
UUID. Never put a credential in a correlation ID or any other CLI argument.

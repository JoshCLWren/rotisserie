# Local operator guide

The simulation commands operate on a repository-scoped local graph snapshot
without network access or a daemon. The opt-in `execute` command additionally
runs one explicitly assigned trusted worker process, using a separate execution
configuration gate. See the [worker process guide](worker-process.md) for its
wire protocol, real-process example, authority checks and recovery limits.

## Configuration

Configuration is TOML with `schema_version = 1`. The selected repository must
exactly match an entry in `repositories.allow`. Relative snapshot and state
paths are resolved from the configuration file, not the process working
directory. Mutations default to disabled when `local.mutations_enabled` is
omitted.

`examples/local/config.toml` is a complete credential-free example. Future
remote adapters must validate repository identity and credentials before an
effect; this local entrypoint neither accepts nor reads remote credentials.

### Worker inventory

An optional `[workers]` table declares the worker inventory. The inventory is
config-derived rather than a hard-coded ceiling: the default work-in-progress
limit for `plan` and `dogfood` is the number of `expected` workers, so growing
or shrinking the fleet is a configuration edit instead of a code change to a
numeric maximum.

```toml
[workers]
expected = ["model:alpha", "model:beta", { namespace = "human", value = "lead" }]
scheduled = ["model:alpha"]
retired = ["model:gamma"]
```

Each entry is either a `namespace:value` string or a `{ namespace, value }`
table; neither the namespace nor the value may contain a colon, so both forms
canonicalize to the same identity. `expected` lists the active workers and must
be non-empty when the table is present. `retired` records identities removed
from rotation; it must be disjoint from `expected` so a later roster entry
cannot silently re-enroll a retired worker. `scheduled` is a
declared-but-not-yet-enforced subset of `expected`: it records which workers an
operator intends for automatic dispatch, but no command consumes it today and
the WIP ceiling still derives from the full `expected` size. This mirrors the
deferred `decide`/`shadow-project` parity boundary. Duplicate identities within
any list are rejected. When the table is omitted, the WIP limit falls back to a
single concurrent worker unless `--wip-limit` (also spelled `--max-active`) is
supplied explicitly.

## Commands

All commands emit one versioned JSON object on standard output.

- `inspect` reports graph counts.
- `plan` selects executable work and exact-head-ready changes, reporting the
  resolved `wip_limit`. The limit defaults to the configured worker inventory
  size and can be overridden with `--wip-limit` (also spelled `--max-active`).
- `claim`, `run`, `review`, `complete`, and `recover` are mutating commands.
- `doctor` validates local state and reports operation metrics; `--bundle`
  writes a redacted diagnostic file.
- `execute --assignment FILE` plans one worker handoff; `--apply` additionally
  requires `[execution].enabled=true`, an active lease and a verified checkout.

Mutating commands are dry-run unless `--apply` is present. A dry-run writes a
schema-versioned operation record but does not alter graph state. Applying also
requires `local.mutations_enabled = true`, providing two explicit gates for
simulation state. Worker execution has its own independent gate described above.

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
| `4` | A worker execution ended with a failure, timeout, cancellation, malformed output, or exhausted transient fallback |

Callers may supply `--correlation-id`; otherwise the entrypoint generates a
UUID. Never put a credential in a correlation ID or any other CLI argument.

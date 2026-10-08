# Operational worker process boundary

Rotisserie owns the first executable slice of the Factory runtime: one bounded
worker process handoff. An adopter supplies a trusted provider bridge and
configuration, then calls the public `execute` CLI. Rotisserie handles launch,
expiry, timeout, cancellation, fallback, heartbeat records, and execution
receipts. The live GitHub controller and provider bridges still reside in
ComicPile; this boundary is the first step toward removing that duplication.

## Try the complete handoff

From a development checkout with the locked environment installed:

```bash
uv run python examples/worker-process/demo.py
```

The example creates a temporary Git checkout with a synthetic remote, a graph
with an active lease, and a read-only assignment. It prints a dry-run plan, an
applied `no_diff` result, and a replayed result. It never connects to the remote
or runs a model. The temporary checkout and records are removed on exit.

## Configure an adopter

Add an execution table to the existing version 1 operator TOML:

```toml
[execution]
enabled = false
worktree = "/absolute/path/to/dedicated-checkout"

[[execution.executors]]
name = "primary"
argv = ["/absolute/path/to/python", "/absolute/path/to/trusted-bridge.py"]

[[execution.executors]]
name = "fallback"
argv = ["/absolute/path/to/another-bridge"]
```

The executable path is absolute; arguments are passed directly without a shell.
Relative worktree paths resolve from the operator configuration directory.
Executor order is fallback order. The bridge is trusted operator code, located
outside a worker-editable checkout. Execution is POSIX-only in this adapter.

Use a dedicated checkout with a credential-free `origin` matching the configured
host, owner and repository. An applied operation verifies its checkout root and,
when supplied, exact HEAD. The assignment must match configured repository and
worker inventory, an active lease, its leased work, and any exact-head change.
An unsatisfied human gate refuses execution. Authority may not outlive the lease.

The executor receives a minimal environment and a temporary empty HOME. It does
not inherit the caller's GitHub or provider tokens, SSH agent, shell settings,
or credential helpers. Raw stdout/stderr are never logged. This adapter is a
process lifecycle boundary, **not an OS sandbox**: a trusted bridge can access
files and the network using its OS identity. Use a separately isolated host for
untrusted programs. Keep credentials out of the checkout, argv, assignment,
result, and durable records; provider authentication belongs to a separately
configured bridge/service. Push, PR creation and merge are not authorities in
this contract and remain outside this command.

Create an assignment with the public
`rotisserie.application.process.assignment_to_dict` function or generate its
schema directly. The example shows the full input. Issue context is an untrusted
field; only operator-supplied objective and authority are control data.

```bash
rotisserie --config operator.toml execute --assignment assignment.json
# Set execution.enabled=true before the explicitly applied invocation:
rotisserie --config operator.toml execute --assignment assignment.json --apply
```

The default invocation only records a plan. It starts no subprocess and
initializes no graph state. The existing `local.mutations_enabled` simulation
gate does not grant execution authority; `[execution].enabled` is separate.
Applied execution never claims work implicitly. The adopter must provide a
valid current lease graph before calling it.

## Provider bridge protocol, version 1

The bridge reads one JSON object from stdin:

| Field | Meaning |
|---|---|
| `schema_version` | Integer `1` |
| `assignment` | Version 1 assignment with id, worker, objective, untrusted repository context, optional change, timeout and authority |
| `attempt` | Stable attempt identity for this assignment/executor position |
| `prompt` | Rendered assignment with explicit trusted/untrusted boundaries |
| `resume` | Optional version 1 secret-free resume packet |

It exits zero and writes exactly one JSON response to stdout:

```json
{
  "schema_version": 1,
  "assignment": "assignment-id-from-request",
  "attempt": "attempt-id-from-request",
  "result": {
    "outcome": "no_diff",
    "detail": null,
    "revision": null,
    "evidence": [],
    "resume": null,
    "retry_after_seconds": null
  }
}
```

`process_response` and `ExecutionResult` provide the public typed constructor.
`assignment_from_dict` and `resume_from_dict` provide strict decoders. Unknown
fields, boolean versions/integers, wrong assignment/attempt identity, foreign
repository revisions, wrong resume identity and recognizable credentials are
rejected before persistence. Evidence must name the result's exact revision.
For review evidence, the bridge must report the reviewed authorized head rather
than treating a newly produced revision as a review of an earlier head.

Both request and response are limited to 1 MiB. The effective deadline is the
earlier of timeout and authority expiry; fallback shares the remaining authority
window. SIGINT/SIGTERM cancel the session. Entire process groups are killed and
the leader is reaped on success, error, timeout and cancellation. Bridges must
not daemonize or escape their process group. Safe heartbeats are emitted every
30 seconds. Arbitrary provider output is discarded rather than persisted.

Only `unavailable` and `rate_limited` select the next configured executor.
Failure to start is `unavailable`. A nonzero process exit is `failed`; malformed
or oversized output is `malformed_output`. All other outcomes, including
`no_diff`, are terminal. `retry_after_seconds` is recorded for the caller; this
bounded command does not sleep/retry indefinitely. CLI exit `0` means
`succeeded` or `no_diff`, `4` means another terminal execution outcome, and `2`
means invalid configuration, scope, state, or input.

## Durable operation and recovery

Atomic receipts under `state_directory/executions` bind the assignment id to a
digest of its full content and authority. A terminal receipt is replayed without
launching a process; changed content under the same id fails closed. Start and
terminal records are fsynced. Target, worker, assignment and checkout locks
prevent concurrent callers from dispatching duplicate work within the configured
state directory. All callers sharing a checkout/target must use that same state
directory; this local adapter is not a distributed lease store.

An interrupted receipt stays `started`. It cannot be implicitly retried. After
checking the worktree and interruption, supply a matching secret-free packet:

```bash
rotisserie --config operator.toml execute --assignment assignment.json \
  --resume resume.json --apply
```

The lease, authority and exact head are revalidated. A changed head or expired
lease needs a new assignment/lease, not stale authority reuse. Resume packets
must identify the original assignment, worker and authorized revision. An active
bridge retains the lock descriptors even if the operator is forcibly killed;
the recorded process group also blocks recovery while still alive. Hard host
termination/SIGKILL cannot guarantee graceful process cleanup. An operator must
inspect and stop surviving processes before retrying. The command does not kill
an unverified reused process id. Attempt events and final outcomes are appended
to the existing operation journal, without raw bridge output or credentials.

## Extraction provenance and next boundary

This is application orchestration plus a local process adapter and operator
entrypoint. It rewrites the bounded lifecycle/fallback/handoff ideas in archived
`.github/scripts/free-model-factory-worker.sh` and
`free-model-factory-worker-primitives.sh` from ComicPile
`2dc9b675e1a828b526f074c5f5c151293dedcdee`. The live controller was inspected
read-only at ComicPile `03b95add5c7d8363588ef2883c1a0d719e23a80b` on
2026-10-07. Provider routes, fleet numbering, product checks, labels, workflow
topology and release writing remain adopter concerns. The archive is unchanged.

The next operational slice is a Rotisserie-owned controller over the current
graph/effect protocols: configured host acquisition, scoped claim/release,
execution, and exact-head review/completion. Then ComicPile can consume a pinned
runtime with small bridges/configuration, validate same-view parity, observe a
bounded canary, and delete duplicates after rollback is proven. Release
publication is not on this interim cutover's critical path. This issue activates
no workflow, schedule, self-worker, remote mutation or live ComicPile cutover.

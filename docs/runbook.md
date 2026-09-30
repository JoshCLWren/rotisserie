# Operator runbook and troubleshooting

Use the [local operator guide](operator.md) for the complete simulation. Copy
`examples/local/` to a disposable writable directory before running commands.
No credential or external repository is needed. Inspect and plan first; review
the JSON result before using `--apply`. Local mutations require both that flag
and `local.mutations_enabled = true`. Dry-run still appends an operation record.

## Before an operation

Confirm the selected repository is explicitly allowed, the config points to
the intended snapshot and state directory, and clock and capacity inputs match
the intended simulation. For change operations, supply the current exact head.
Keep implementation and independent review worker identities separate.

## Diagnosing a refusal

| Symptom | Action |
|---|---|
| Exit 2 | Check TOML schema, repository allowlist, paths, command arguments, and serialized input versions |
| Exit 3 | Read the structured rejection; inspect dependencies, lease expiry/owner, human gates, capacity, and current revision |
| Unexpected no-op | Inspect existing state and idempotency evidence before retrying with a new operation |
| Stale revision/version | Acquire a fresh snapshot and review evidence; never edit evidence to match the new head |
| Missing local files | Resolve paths relative to the configuration directory and check filesystem access |
| Shadow divergence | Use the same captured host view for both inputs; explain each mismatch before adoption |

The adopter commands have additional documented statuses; consult the
[adopter guide](adopter-integration.md) rather than assuming every nonzero code
is a retryable failure. `doctor --bundle` writes redacted diagnostics. Inspect
the bundle manually before sharing it; redaction is not exhaustive secret detection.

## Interrupted simulation and rollback

Preserve the snapshot, graph state, and operation journal together before
recovery. Run `inspect` and `doctor`, then `recover --at` with an explicit time
in dry-run mode. Apply only the intended expired-lease recovery. Do not delete a
journal or invent a new idempotency key to conceal a partially applied effect.
To restart a disposable demonstration, use a fresh copy of the example rather
than overwriting evidence from a run under investigation.

For external adoption, request rollback through the versioned `adopt` contract.
The returned transition command must be enforced by the adopter using the exact
control revision and expected stage. Rotisserie does not operate the adopter's
rollback switch. Stop fresh intake when control evidence is invalid, and follow
the adopter's runbook for its existing completion path.

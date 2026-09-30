# Architecture and adapter author guide

Rotisserie coordinates a validated graph. It does not supply a hosted worker
fleet. The CLI composes one operation and reports versioned JSON.

## Dependency direction

Entrypoints construct adapters that implement application protocols. Application
services invoke pure domain policy. Domain modules contain immutable graph
values, explicit dependency edges, and deterministic decisions; they do not
read environment variables, contact hosts, or select model providers.

| Layer | Implementation | Responsibility |
|---|---|---|
| Domain | `src/rotisserie/domain/` | Graph validation, leases, capacity, exact-revision readiness |
| Application | `src/rotisserie/application/` | Coordination effects, runtime contracts, shadow and adoption decisions |
| Adapters | `src/rotisserie/adapters/`, `src/rotisserie/operator/local.py` | Host translation, credential boundaries, persistence and effects |
| Entrypoint | `src/rotisserie/cli.py` | Configuration, operation selection, stable output |

The legacy archive and prototype are migration evidence, outside the supported
package boundary. Provider routes, label vocabulary, workflow topology, and
product release writing belong to adopters. No adopter repository is required
to install or run the local simulation.

## Implementing an adapter

Start with `CoordinationPort` in `application/coordination.py`. `read()` returns
a `GraphView`: a validated `GraphSnapshot` and an opaque concurrency version.
`apply()` consumes an `EffectCommand`. An adapter must compare the command's
expected version with its current state atomically before applying an effect.
Reject stale versions rather than silently rebasing the operation.

Persist the command's idempotency key with its outcome. On retry after a crash,
reconcile the actual effect before acknowledging it; a transport timeout does
not prove the effect failed. Change effects also require the exact revision.
Recheck both revision and repository identity at the mutation boundary.

Translate host data into repository-scoped domain identities before application
policy runs. Preserve evidence for old revisions without treating it as
current-head evidence. Supply explicit clock, capacity, dependency, and human
gate inputs. Do not infer blocking dependencies from casual issue mentions.

A remote adapter must independently enforce repository and target allowlists,
credential identity, minimal authority, and dry-run behavior. The GitHub adapter
separates `CredentialProvider`, `GitHubTransport`, and `MarkerStore`; implementing
these protocols does not authorize activating a workflow or exposing a remote
mutation command. The local adapter is an executable persistence example, not a
distributed locking service.

## Adapter acceptance checks

Exercise stale-version races, duplicate delivery, effect-before-acknowledgement
crashes, expired leases, changed heads, missing identity, wrong repositories,
producer/reviewer identity collisions, and human-only gates. Dry-run tests must
prove that credential lookup and transport inspection/write are never called.
Use synthetic fixtures and secret-free durable state. Runtime adapters must
also reject authority-expired and cross-assignment resume packets.

Use versioned serialized CLI contracts for external adoption. Python protocol
implementations remain an experimental extension surface under the
[compatibility policy](compatibility.md).

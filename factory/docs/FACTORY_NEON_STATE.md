# Factory execution and coordination state

GitHub is authoritative for issues, PRs, labels, ownership at mutation boundaries,
commit SHAs, reviews, CI, merges and closure. Neon never authorizes a merge.
Only trusted control-plane jobs receive the existing restricted `factory_state`
connection in project `delicate-sea-51036121`; model jobs receive no Neon secret.

## Execution handoff

The dispatcher discovers candidates once in its trusted Actions job. Small
assignments travel directly through workflow inputs: worker, target kind/number,
stage, linked issue, branch, exact PR SHA and original claim UUID. Reusable
trusted preparation acquires the worker workflow lock, verifies the live,
unexpired transactional claim and GitHub target/stage, and exports bounded
model-health information through job outputs. The model job reads its assigned
target, rejects stage/head drift, and checks the checkout SHA. Fork routing
rejects head drift too. It does not reconstruct the queue.

Cleanup receives the original claim UUID. A stale completion cannot discover
and release a newer UUID; an unfenced old cleanup fails closed. Input-driven
workers never perform broad startup/EXIT ownership scans. Explicit publication
handoffs and trusted cleanup preserve the existing checkpoint and non-draft PR
behavior. Controller deployments preserve active sessions; normal liveness and
lease expiration recover interruptions.

Entry persists its trusted classified outcome before sending a completion run
ID to the signal-only completion bridge. The bridge performs one Actions
workflow dispatch with no checkout, Neon connection, demand/registry discovery,
model selection or lease mutation. The duplicate refill workflow and five-minute
completion-demand scan are removed. The canonical hourly dispatcher watchdog
recovers missed events; productive roster cycles retain their bounded successor.
Migration-lane reconciliation retains its dedicated event workflow.

## Discovery is per job

`factory_discovery.py` stores only the current trusted job's scheduling memory
under `RUNNER_TEMP`. It is neither a database nor an artifact cache: no TTL,
refresh lease, compression, cross-run synchronization or latest-artifact lookup
exists. The runner deletes these files when its job ends. Repo/kind isolation
prevents accidental inventory reuse. Jev and auxiliary migration/no-progress
recovery consume the same inventory; sensitive target reads stay live.

PR wakeups pass `pr:<number>` to the dispatcher, which reads that PR and its
linked issue instead of listing the repository. Issue wakeups pass their exact
issue. A new issue still requires one open-PR inventory to establish absence of
an existing canonical PR safely; an issue webhook alone cannot prove that
absence. Recovery/roster cycles discover the bounded open issue/PR inventories
once. Worker ownership uses a targeted live label query independent of this
candidate view, so narrowing discovery cannot conceal another active target.
Aged-PR historical discovery runs only on the hourly recovery event.

The generic REST cache, target projection writer, webhook synchronization,
projection reconciliation and compression transport are deleted. The REST shim
only adapts CLI response shapes and reads this job's explicit inventory; generic
API calls and all individual targets/checks/reviews/mutations remain live.

## Storage mapping and remaining dependencies

| Existing storage | Purpose | Destination/status |
| --- | --- | --- |
| `factory-github-get-v1:*` KV | Duplicated GitHub REST responses | Removed; bounded migration cleanup deletes rows |
| `<repo>:open-issues`, `<repo>:open-prs` KV | Candidate discovery | Per-job memory; obsolete rows deleted |
| `factory-projection-v1:*` KV | Duplicated issue/PR state and full-snapshot watermarks | Removed; targeted event inputs and hourly recovery replace synchronization |
| `<repo>:registry-comments` KV | Retired full registry snapshot | Replaced by compact lifecycle/model/provider records, targeted trusted GitHub marker checks, and bounded comment deltas. Remove the row after deployed workflow verification |
| `factory-lease-v1:*` KV | Atomic worker and target exclusion | Retained; acquired together under a short transaction lock, renewed only after live run verification, released by original UUID |
| Worker/model/provider backoff KV families | Fibonacci/exponential retry across independent runs | Retained; no model/provider credentials or source retirement on ordinary transient failures |
| `factory_model_health` | Verified source/model failures, cooldowns, explicit 410 retirement | Retained coordination; prevents concurrent independent runs repeatedly probing broken routes |
| `factory_provider_budget` | Account quota/cooldown/concurrency policy | Retained coordination; GitHub cannot atomically reserve provider capacity |
| `factory_provider_reservation` | Capacity tied to original claim token and expiry | Retained in the same acquisition/release transaction as worker/target leases |
| `factory_pr_stage_retry` | Exact-head/stage retry not-before | Retained coordination; a new SHA is immediately eligible |
| `factory_worker_metrics` | Bounded source/model delivery ranking counters | Retained operational dependency; execution failure remains distinct from persisted code delivery |
| `factory_runtime_event_keys` | Idempotence across independent terminal collectors | Retained coordination; duplicate completion must not double-count/reset breakers |
| `factory_check_projection` | Advisory CI/review observations | Production writer retired; CI/review completions directly wake exact-head PR scheduling, legacy table requires one-time cleanup |
| `factory_reconciliation_cursor` | Backfill and coalesced wakeups | Existing consumers remain; remove obsolete scopes after consumers migrate |
| `factory_state_events` | Lifecycle/retry history plus old discovery observations | Runtime history still has consumers; duplicated/obsolete observations and bounded retention cleanup remain outstanding |

Leases and provider reservations must survive independent workflows because
Actions artifacts, outputs and concurrency groups cannot atomically reserve a
worker, all linked targets and provider capacity. The database lock covers only
claim/renew/release transactions, never a model session. GitHub ownership is
rechecked before label changes; exact-head CI/review/merge gates remain live.

The status job's bounded `prune-obsolete` migration removes only this repository's
retired GET/target/open-inventory keys, skips an active old refresh lock, and
processes at most 1,000 records per invocation. It cannot delete lease, backoff,
registry or provider state. This cleanup is not a replacement cache. Physical
Postgres allocation can remain until ordinary vacuum reclaims dead storage.

## CI and review completion routing

The check-completion workflow sends an explicit `pr:<number>` wakeup only for
an open, non-draft PR on its current exact head. It never uses an advisory
database snapshot to authorize a merge. Ordinary CI/guard/review success and
failure events do **no Neon lookup or write**. A failed trusted review transports
only its exact head and workflow-run ID into the retained
`factory_pr_stage_retry` table, idempotently, so a broken reviewer does not
immediately spin. The lightweight event job still verifies live GitHub PR
state before dispatch. This replaces the old per-check projection writer and
its additional coalesced-database wakeup.

The old `factory_check_projection` relation is no longer created by fresh
schema initialization or consumed by factory readers. Its historical rows
remain physically present until the separately verified retention migration
removes them.

## Measurements and scope status

Baseline inspection on 2026-11-15, before migration:

| Category | Observed rows / bytes |
| --- | --- |
| KV physical allocation | ~537 live rows; 174,710,784 allocated bytes including indexes/dead tuples |
| Generic GET payloads | 72 rows; 12,351,733 payload bytes |
| Repository discovery/registry payloads | 3 rows; 989,729 payload bytes |
| Target projection payloads | 401 rows; 928,150 payload bytes |
| Lease payloads | 21 rows; 2,577 payload bytes |
| Worker/model/provider backoff payloads | 38 rows; 7,494 payload bytes |
| Event relation | 13,813 rows; 3,973,120 allocated bytes |
| Dispatcher 43770738388 artifacts | One Jev decision artifact; 444 bytes |

`pg_stat_statements` is unavailable. Cumulative table counters are not per-cycle
query measurements, and physical allocation is not active payload size. Do not
claim achieved query or storage savings from these baseline figures alone.

Merged handoff #5218 (`16753f4f69`) passed main CI 43770738444 and production
deployment 43770738284. Completion #5219 (`4a670a4085`) used actual fast-merge run
43771375653 and passed main CI 43771394475. Live completion signal 43771827145
completed successfully with an actual terminal Entry run ID and woke dispatcher
43771835176, whose run title carries that completion context.

**The registry-snapshot migration:** after the production cutover, legacy check-table cleanup,
redundant lifecycle observations, final bounded-retention policy/cleanup,
complete live worker/continuation evidence and actual before/after per-cycle
measurements remain. A green merge or a simulated handoff does not establish
those acceptance criteria.

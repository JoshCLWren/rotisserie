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
| `factory_reconciliation_cursor` | Backfill and coalesced wakeups | Dead: no caller wrote a checkpoint or wakeup, the table held 0 rows (Nov 24), and the registry-observation bootstrap that read it is removed. Not carried to SQLite |
| `factory_state_events` | Lifecycle/retry history plus old discovery observations | Runtime history still has consumers; lifecycle events expire after 30 days, raw model attempts after 120 days into daily counts (see below) |

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

## Backoffs: real failures, quota and throttle

A real execution failure (agent timeout, provider failure, unavailable model,
proven work failure) grows a Fibonacci streak per worker and per exact
source/model pair: 5, 5, 10, 15, 25 ... minutes. The delay stops growing at
24 hours (`MAX_FIBONACCI_DELAY_SECONDS`). Rows written before the cap are read
as at most 24 hours after their last update.

A quota, throttle or empty-balance refusal is provider capacity, not a model
fault. It never grows a streak. `factory_quota_reset.py` decides one cooldown:

| Refusal | Cooldown ends | Blocks |
| --- | --- | --- |
| Daily quota | The provider's next daily reset | Whole account, or the pair where limits are per model |
| Monthly quota | First instant of the next UTC month | Whole account |
| Empty credit balance ("Insufficient balance", "Add usage credits", "out of credits") | Recheck after 24 hours | Priced models of that source only (`@credit_pool` exclusion) |
| Throttle (HTTP 429 without a daily limit) | The provider's wait hint, else 5 minutes (at most 1 hour) | Same as the source's scope |
| Oversized request (input above a per-minute token limit) | No cooldown | Nothing |

Reset policies per source (`RESET_POLICIES`):

- `openrouter-free`: daily, account, next UTC midnight. The OpenRouter limits
  page counts free-model requests "per UTC day". The trusted probe reads
  `GET /api/v1/key` (`free_model_daily_requests.remaining`) without inference.
- `groq-free`: daily, per model. Refusal text such as "try again in 1m26.4s"
  sets the time.
- `gemini-free`: daily, per model, next midnight in America/Los_Angeles (the
  Gemini rate-limit page; unverified from this repository).
- `mistral`, `ollama-cloud`, `cohere-eval`: monthly, account.
- Every other source: no documented reset, so the conservative default is the
  next UTC midnight, account scope.

Where the cooldown lives:

- account: `factory-provider-backoff-v1:<repo>:source:<source>`. The dispatcher
  filter and `export-fleet-failover-sources` skip the source until it expires.
- pool: `factory-credit-pool-v1:<repo>:source:<source>`, exported to the runner
  as `"@credit_pool": [sources]` in `export-model-exclusions`. Selection drops
  only the models whose catalog row publishes a positive price.
- model: `factory_model_health` status `cooling` for the exact pair.

A later refusal never shortens an active cooldown. Old Fibonacci rows whose
outcome was quota or throttle no longer gate dispatch.

### Reset hints and the probe

After a quota or throttle failover the worker writes one trusted marker:
`FACTORY_QUOTA_RESET: source= model= outcome= kind= reset_at= reset_source=`.
`reset_source` is `text` (the refusal named the wait), `header` (Retry-After or
X-RateLimit-Reset from the probe), `key_endpoint` (OpenRouter key endpoint) or
`policy` (the default above). Parsed resets are clamped between now and a
ceiling per kind: 1 hour for a throttle, 2 days for a daily quota, 7 days for an
empty balance, 32 days for a monthly quota. Only provider refusal lines of the
OpenCode log count as text evidence (JSON error events, or lines that start as
an error or carry an HTTP status), never agent prose or tool output.

`factory_quota_probe.py` sends at most one request per source per run, from the
worker shell and never from the model. It prints only parsed numbers. A 401 or
403 is a blocked probe and never a reset signal. OpenCode sends per-session
`x-opencode-*` headers and its own User-Agent to every provider; they carry no
credential and the probe does not copy them. It does copy the registry headers
and the HTTP-Referer and X-Title that OpenCode adds for NVIDIA. The OpenCode Zen
and Go lane (`opencode-free`), `kilo-auto` and `omniroute-free` are never probed
and use the policy default. `export-backoffs` shows each provider's probe
method (`key_endpoint`, `inference` or `none`).

### Dispatch eligibility

- A catalog slot (no pinned model) is admitted only when its source's live
  published catalog lists a model outside the exported exclusions and any
  credit-pool cooldown (`factory_state_operations_slots.py`). Sources without a
  key-free catalog, and unreadable catalogs, stay eligible; the run job checks
  the authenticated catalog before a session.
- A review PR whose lease ended before a session (for example because every
  eligible model authored it) cools in `cooling_prs` from its second such abort
  in 24 hours. It waits `FACTORY_REVIEW_FAILURE_COOLDOWN_SECONDS` times
  (aborts - 1) after the latest abort, up to 6 hours.

### Operator commands

- `rotisserie factory backoffs --via github` lists per provider: capacity
  cooldowns with their reset source, Fibonacci backoffs (old quota-caused
  streaks are marked), and model health cooldowns, with next retry in local
  time. It runs `export-backoffs` in the trusted label claim workflow.
- `rotisserie factory backoffs reset --quota-only --via github` deletes the
  Fibonacci rows and cooling health rows that quota and throttle refusals wrote,
  and the account and credit-pool cooldowns (use it after adding credits or when
  a policy default was wrong). It caps every other Fibonacci row at one day after
  its last update, keeps real streaks, and does not write the full-reset marker.
  The full reset also clears credit-pool cooldowns. It runs
  `reset-model-health --quota-only` in the Factory Model Health Reset workflow.

## Slot leases (several holders on one target)

A normal lease lets one worker hold an issue or PR. Incident racing (#239)
needs several separate attempts on one issue, and a planned collaborative mode
needs one lease per piece. Both use slots of one target:

- key `factory-lease-v1:<repo>:slot:<number>:<slot>`, where `<slot>` is
  `attempt-K` or `piece-<id>`. Slot keys are outside the `target:` namespace,
  so the single-holder readers (frontier, external claims) never see them;
- `acquire_slot(repo, worker, number, slots)` in
  `factory_state_ledger_slots.py` is the only allocator. Under the repository
  advisory lock it takes the first free slot from the ordered list, writes the
  slot row and the worker row with one claim UUID, and reserves provider
  capacity like a normal lease;
- one claim per worker; siblings on one target use distinct providers unless
  the caller passes `distinct="none"`;
- an expired slot is free again, like an expired worker lease. Renewal of
  verified running workers keeps a live attempt's slot;
- `release_lease` and `renew_verified_workers` release and renew slot rows by
  claim UUID, so a slot claim has no separate release path.

Nothing assigns slots yet: the dispatcher still leases one worker per item.

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

## Access paths, indexes and retention (#254)

The original tables existed before these lookups grew hot, and
`ensure_schema` stops at its first table check. The dispatcher's trusted
`bootstrap` step therefore runs `factory_state_indexes.ensure_upgrades`. It
checks each object with `to_regclass` and creates a missing one under a
non-blocking advisory lock with a 5-second lock timeout. A failure (for
example a role that does not own the table) is printed in the step output and
does not stop the factory. A fresh database gets the same statements from
`factory-state-schema.sql`.

| Object | Serves |
| --- | --- |
| `idx_factory_state_events_event_id` | Event dedup (`detail->>'event_id'`), `recorded_runtime_runs` |
| `idx_factory_state_events_run_id` | `run_workers` run-to-worker lookup |
| `idx_factory_state_events_worker_recent` | Reliability export, runtime view, failure streak (lifecycle and terminal events only) |
| `idx_factory_state_events_retired` | Retired `cache_refresh`/`target_reconciled` arms of the prune (empty once cleaned) |
| `idx_factory_state_kv_key_prefix` | Key-prefix reads (`text_pattern_ops`); prefixes are escaped `LIKE` patterns |
| `factory_model_attempt_daily` | Daily per-model attempt counts for retained history |

The lifecycle dedup is one `INSERT ... SELECT ... WHERE NOT EXISTS` under the
repository lock, not a read followed by a write. The dedup index is not
unique, because existing rows were never checked for duplicates.

Retention: lifecycle events keep the existing 30-day, repository-fenced
prune. Raw `model_attempt` events are kept for 120 days. The status job then
moves older ones, in one statement, into `factory_model_attempt_daily` (repo,
UTC day, source, model, outcome: attempts, timed attempts, total duration).
The longest raw reader is `rotisserie factory estimate-history`: 45 days by
default and at most 120 (`estimate_neon.py` `DEFAULT_DAYS`, `DAYS_RANGE`).
The reliability export and runtime view read 24 hours and 7 days, and the
outcome reconciler reads 12 hours. A reader that needs per-model history older
than 120 days reads the daily table.

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

## SQLite and libSQL schema

`factory-state.sqlite.sql` is the schema for the Turso (libSQL) move (#253).
It keeps the coordination tables above with typed, indexed columns for the
properties that queries filter on, a unique `event_id`, epoch-second times
compared against the database clock, a `repo` column on every table and a
`_schema_version` table (Turso Cloud cannot set `PRAGMA user_version`). Leases
and backoffs leave the generic key-value table for `factory_lease` and
`factory_backoff`. The discovery cache, the reconciliation cursor and the
check projection are not carried over.

## libSQL store (D3)

`factory_libsql.py` and the `factory_turso_*` modules implement the same
functions on libSQL. Nothing imports them until the cutover (#257) changes
the imports of `factory_state_ledger.py` and `factory_state_operations.py`.
Every write is one Hrana batch: BEGIN IMMEDIATE, the statements, COMMIT, in
one HTTP request. The first statement holds every authorizing check (key
free, external work in progress, daily cap, slot provider) and each later
statement runs only if that row carries this call's claim or event identity.
An external handoff marks the claim pending with a nonce, verifies on
GitHub with no transaction open, then finalizes only if the nonce and every
target row still match. A lost response is reconciled by reading the claim
or event back before one resend. An unreachable or `BLOCKED` store raises,
so acquisition fails closed.

The trusted environment names the store: `FACTORY_TURSO_URL` with the token
in the file `FACTORY_TURSO_TOKEN_FILE`, or `FACTORY_STATE_SQLITE` for a
local file. `FACTORY_STATE_REQUIRED=true` makes an unconfigured store fatal.

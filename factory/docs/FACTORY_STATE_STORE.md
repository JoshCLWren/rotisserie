# Factory execution and coordination state

GitHub is authoritative for issues, PRs, labels, ownership at mutation boundaries,
commit SHAs, reviews, CI, merges and closure. The factory store never
authorizes a merge. The store is a libSQL (Turso) database on the free plan.
Only trusted control-plane jobs receive its URL and a token file (the
`factory-store` action); model jobs receive no store credential. Jobs that only
read get `FACTORY_TURSO_READ_TOKEN`. The factory no longer uses Neon; the
adopter application database is separate and unaffected.

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
workflow dispatch with no checkout, store connection, demand/registry discovery,
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

## Storage mapping

| Responsibility | Store table | Notes |
| --- | --- | --- |
| Worker, target, slot, operation and external claims | `factory_lease` | Typed `kind`; acquired in one write batch, renewed only after live run verification, released by original UUID; an expired external claim still holds |
| Worker, model, provider and credit-pool backoffs | `factory_backoff` | Fibonacci streaks grow inside the upsert; quota and throttle refusals write reset-timed cooldowns |
| Health reset marker | `factory_repo_state.health_reset_at` | |
| Model health and retirement | `factory_model_health` | |
| Operator provider limits | `factory_provider_budget` | Only `concurrency_limit` and `daily_limit`; the daily cap counts today's `worker_assigned` events |
| Provider capacity | `factory_provider_reservation` | Written in the same batch as the claim |
| Exact-head PR retry | `factory_pr_stage_retry` | A new SHA is immediately eligible |
| Delivery ranking counters | `factory_worker_metrics` | |
| Runtime projection idempotence | `factory_runtime_event_keys` | A duplicate completion changes nothing |
| Lifecycle, assignment and attempt history | `factory_state_events` | `event_id` is UNIQUE; lifecycle events expire after 30 days, raw model attempts after 120 days into daily counts |
| Model attempt retention | `factory_model_attempt_daily` | |
| Resumable read cursors | `factory_cursor` | One row per name, added in schema version 3. `attempt-history` keeps the last retry-history read time and only its trusted markers, so the next dispatch job reads one delta page (a full read every 6 hours). `outcome-reconcile` lets one dispatcher job per 15 minutes backfill Entry outcomes. `gh-usage:dispatch` and `gh-usage:merge` keep the last 12 jobs' GitHub request counts and the workflow token budget for `baste recap` and `baste limits`. |

Not carried from Neon: the discovery and projection key families
(`factory-github-get-v1`, `factory-projection-v1`, open issue and PR lists),
`factory_reconciliation_cursor` (0 rows, no writer) and
`factory_check_projection` (writer retired; only the old prune touched it).
The migration tool (`factory_turso_migrate.py`) copies the live tables and
reports the skipped families as counts.

Leases and provider reservations must survive independent workflows because
Actions artifacts, outputs and concurrency groups cannot atomically reserve a
worker, all linked targets and provider capacity. A write batch covers only
claim/renew/release, never a model session or a GitHub call. GitHub ownership
is rechecked before label changes; exact-head CI/review/merge gates remain live.

The status job's bounded `factory_state_operations.py prune` step deletes this
repository's lifecycle events older than 30 days and rolls raw model attempts
older than 120 days into daily counts, at most 1,000 rows per batch and five
batches per run. It cannot delete lease, backoff or provider state.

## Backoffs: real failures, quota and throttle

A model's own failure grows a streak per worker and per exact source/model
pair (`factory_track_record_cooldown.py`). Each class has its own curve:

| Class | Outcomes | Wait after 1 / 3 / 5 / 9 strikes | Cap |
| --- | --- | --- | --- |
| Task | `work_failure` (also no-change and review verdicts), `agent_timeout` | 0 / 10 / 20 / 30 min | 30 min |
| Fault | `provider_failure`, `environment_failure`, `unknown_failure`, `control_plane_failure` | 5 / 10 / 25 / 120 min (Fibonacci) | 2 h |
| Unavailable | `model_unavailable` | 15 / 60 / 240 / 360 min | 6 h |
| Long | `model_policy_violation`, `model_retired_410` | 6 h | 6 h |

Task outcomes say the task was hard, not that the model is broken; item loops
belong to the per-item loop breaker. Only strikes from the last six hours
count, and a success clears the streak. A proven model (clearly above the
fleet rate over seven days) waits nothing for its first two task strikes and
half of every other curve. For `model_unavailable`, a model with a recent
success is not benched by its first strike and never for more than 15
minutes. Readers clamp every stored row to its class cap, so rows written
under the old 24-hour curve end without a reset. Each model streak row stores
the run id and the error text that caused it.

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

A later refusal never shortens an active account or pool cooldown. On the
model health row, a capacity refusal never inherits an end from a different
cause. The row ends at the later of the new end and the older row's end
clamped to its own cause's current cap, and `last_outcome` names the cause
that owns that end. An older `model_unavailable` row counts for at most 15
minutes on a kept model. A real longer quota or throttle end is kept. Readers
of `factory_model_health` (dispatcher exclusions, forced runs, `baste
backoffs`) apply the same clamp (`factory_health_cooldown.py`). A capacity
row keeps its stored end, because a throttle row can carry a daily reset.
Old Fibonacci rows whose outcome was quota or throttle no longer gate
dispatch.

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
  eligible model authored its current head) cools in `cooling_prs` from its second such abort
  in 24 hours. It waits `FACTORY_REVIEW_FAILURE_COOLDOWN_SECONDS` times
  (aborts - 1) after the latest abort, up to 6 hours.

### Operator commands

- `rotisserie factory backoffs --via github` lists per provider: capacity
  cooldowns with their reset source, Fibonacci backoffs (old quota-caused
  streaks are marked), model health cooldowns with the run and error behind
  them, and models kept hot by their track record, with next retry in local
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
  `factory_turso_slots.py` is the only allocator. In one write batch it takes
  the first free slot from the ordered list, writes the slot row and the worker
  row with one claim UUID, and reserves provider capacity like a normal lease;
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
failure events do **no store lookup or write**. A failed trusted review transports
only its exact head and workflow-run ID into the retained
`factory_pr_stage_retry` table, idempotently, so a broken reviewer does not
immediately spin. The lightweight event job still verifies live GitHub PR
state before dispatch. This replaces the old per-check projection writer and
its additional coalesced-database wakeup.

The old `factory_check_projection` relation is no longer created by fresh
schema initialization or consumed by factory readers. Its historical rows
remain physically present until the separately verified retention migration
removes them.

## Retention

Lifecycle events keep the 30-day, repository-fenced prune. Raw
`model_attempt` events are kept for 120 days. The status job then moves older
ones, in one batch, into `factory_model_attempt_daily` (repo, UTC day, source,
model, outcome: attempts, timed attempts, total duration). The longest raw
reader is `rotisserie factory estimate-history`: 45 days by default and at
most 120 (`estimate_neon.py` `DEFAULT_DAYS`, `DAYS_RANGE`). The reliability
export and runtime view read 24 hours and 7 days, and the outcome reconciler
reads 12 hours. A reader that needs per-model history older than 120 days
reads the daily table. Reliability and runtime-view exports run at most once
an hour.

## Neon measurements before the store (history)

Baseline inspection on 2026-11-15, on the retired Neon database:

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

## Store schema

`factory-state.sqlite.sql` is the schema. It has typed, indexed columns for the
properties that queries filter on, a unique `event_id`, epoch-second times
compared against the database clock, a `repo` column on every table and a
`_schema_version` table (Turso Cloud cannot set `PRAGMA user_version`). The
dispatcher's `bootstrap` step applies it; every statement is idempotent.
`ensure_schema` runs the statements only when the stored version is behind
`SCHEMA_VERSION`, unless it is forced (`baste schema --apply`). Each attempt
stores its result, the objects it created and any error in the one-row
`_schema_upgrade` table. A failed attempt is stored in a second write, then
the error is raised. `factory_turso_schema.py status` reads the result and the
objects without a write, so the read-only token can run it.

## libSQL store

`factory_libsql.py` and the `factory_turso_*` modules implement the factory
state. `factory_state_ledger.py` and `factory_state_operations.py` keep the
public names and the command lines that workflows call. Every write is one
Hrana batch: BEGIN IMMEDIATE, the statements, COMMIT, in one HTTP request.
The first statement holds every authorizing check (key free, external work in
progress, daily cap, slot provider) and each later statement runs only if that
row carries this call's claim or event identity. An external handoff marks the
claim pending with a nonce, verifies on GitHub with no transaction open, then
finalizes only if the nonce and every target row still match. A lost response
is reconciled by reading the claim or event back before one resend. An
unreachable or `BLOCKED` store raises, so acquisition fails closed.

The trusted environment names the store: `FACTORY_TURSO_URL` with the token
in the file `FACTORY_TURSO_TOKEN_FILE`, or `FACTORY_STATE_SQLITE` for a local
file. `FACTORY_STATE_REQUIRED=true` makes an unconfigured store fatal; each
reusable workflow sets it whenever the adopter passes the store URL.

New factory claims pause while the published usage report (`db_usage` in
`factory-stats.json`) is critical: the dispatcher saves it to
`FACTORY_DB_USAGE_FILE`, and `acquire_lease` and `acquire_slot` raise
`ClaimsPaused`. An operator's explicit target is not paused by usage. Renewal
and release never pause. A `BLOCKED` store refuses every request on its own.

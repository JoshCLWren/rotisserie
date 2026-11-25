-- Rotisserie factory coordination state for SQLite and libSQL (Turso).
-- Standard SQLite 3.43 SQL only: STRICT tables, UPSERT, RETURNING, ->>.
-- Every time is INTEGER epoch seconds. Expiry compares against the database
-- clock, unixepoch(), never a runner clock. Every table has a typed repo
-- column, so one repository's rows (a test namespace) delete with one WHERE.
-- Hot properties are typed, indexed columns; detail holds cold JSON only.
-- GitHub remains authoritative for labels, reviews, checks and merges.

CREATE TABLE IF NOT EXISTS _schema_version (
    version INTEGER PRIMARY KEY,
    applied_at INTEGER NOT NULL
) STRICT;

-- The last schema upgrade attempt, so `baste schema` and doctor show it
-- without a search through run logs. One database-wide row, like
-- _schema_version; created is a JSON list, or NULL when it was not read.
CREATE TABLE IF NOT EXISTS _schema_upgrade (
    name TEXT PRIMARY KEY CHECK (name = 'last'),
    version INTEGER NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('ok', 'failed')),
    error TEXT,
    created TEXT CHECK (created IS NULL OR json_valid(created)),
    source TEXT NOT NULL,
    workflow TEXT,
    run_id TEXT,
    at INTEGER NOT NULL
) STRICT;

-- Worker, target, slot and external claims. One row per held key; every row
-- of one claim shares claim_id, so release and renewal are fenced by it.
-- An expired external row still holds its target until an identity-fenced
-- release or an operator revoke.
CREATE TABLE IF NOT EXISTS factory_lease (
    lease_key TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('worker', 'target', 'slot', 'external')),
    number INTEGER,
    claim_id TEXT NOT NULL,
    worker TEXT,
    operation TEXT,
    external INTEGER NOT NULL DEFAULT 0 CHECK (external IN (0, 1)),
    wip INTEGER NOT NULL DEFAULT 0 CHECK (wip IN (0, 1)),
    source TEXT,
    slot TEXT,
    acquired_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    targets TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(targets)),
    detail TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail))
) STRICT;
CREATE INDEX IF NOT EXISTS idx_factory_lease_claim ON factory_lease (claim_id);
CREATE INDEX IF NOT EXISTS idx_factory_lease_repo_kind
    ON factory_lease (repo, kind, number, expires_at);

-- Provider capacity held by one claim; released with the claim.
CREATE TABLE IF NOT EXISTS factory_provider_reservation (
    claim_id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    worker TEXT NOT NULL,
    source TEXT NOT NULL,
    expires_at INTEGER NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS idx_factory_provider_reservation_active
    ON factory_provider_reservation (repo, source, expires_at);

-- Operator-set provider limits. No row means no limit beyond the checked-in
-- source budget; acquisition enforces both inside its write statement.
CREATE TABLE IF NOT EXISTS factory_provider_budget (
    repo TEXT NOT NULL,
    source TEXT NOT NULL,
    concurrency_limit INTEGER CHECK (concurrency_limit IS NULL OR concurrency_limit > 0),
    daily_limit INTEGER CHECK (daily_limit IS NULL OR daily_limit >= 0),
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (repo, source)
) STRICT;

-- Fibonacci backoffs (worker, source/model) and reset-timed capacity
-- cooldowns (provider account, credit pool). One row per key.
CREATE TABLE IF NOT EXISTS factory_backoff (
    backoff_key TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('worker', 'model', 'provider', 'pool')),
    source TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    worker TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '',
    streak INTEGER NOT NULL DEFAULT 0,
    run_id INTEGER NOT NULL DEFAULT 0,
    next_retry_at INTEGER,
    expires_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail))
) STRICT;
CREATE INDEX IF NOT EXISTS idx_factory_backoff_repo_kind
    ON factory_backoff (repo, kind, expires_at);

-- Per-repository markers: the last manual model-health reset.
CREATE TABLE IF NOT EXISTS factory_repo_state (
    repo TEXT PRIMARY KEY,
    health_reset_at INTEGER
) STRICT;

CREATE TABLE IF NOT EXISTS factory_model_health (
    repo TEXT NOT NULL,
    source TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unknown'
        CHECK (status IN ('unknown', 'healthy', 'cooling', 'unavailable', 'retired')),
    last_outcome TEXT,
    cooldown_until INTEGER,
    last_verified_at INTEGER,
    last_run_id INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (repo, source, model)
) STRICT;
-- Only blocking rows are indexed: dispatch reads them, never the healthy majority.
CREATE INDEX IF NOT EXISTS idx_factory_model_health_blocking
    ON factory_model_health (repo, cooldown_until)
    WHERE status IN ('retired', 'unavailable', 'cooling');

-- Exact-head PR stage retry not-before; a new head SHA is a new row.
CREATE TABLE IF NOT EXISTS factory_pr_stage_retry (
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    stage TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    outcome TEXT NOT NULL,
    retry_after INTEGER,
    last_run_id INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (repo, pr_number, head_sha, stage)
) STRICT;

CREATE TABLE IF NOT EXISTS factory_worker_metrics (
    repo TEXT NOT NULL,
    worker TEXT NOT NULL,
    source TEXT NOT NULL,
    model TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    productive INTEGER NOT NULL DEFAULT 0,
    completed INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    last_run_id INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (repo, worker, source, model)
) STRICT;

-- One terminal projection per run, across independent collectors.
CREATE TABLE IF NOT EXISTS factory_runtime_event_keys (
    repo TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    PRIMARY KEY (repo, run_id)
) STRICT;

-- Append-only history. origin is the writer (github-actions,
-- rotisserie-external); source and model are the provider and model.
-- event_id is unique, so duplicate ingestion is one INSERT OR IGNORE.
CREATE TABLE IF NOT EXISTS factory_state_events (
    id INTEGER PRIMARY KEY,
    repo TEXT NOT NULL,
    origin TEXT NOT NULL,
    event_type TEXT NOT NULL,
    subject_type TEXT,
    subject_number INTEGER,
    event_id TEXT UNIQUE,
    run_id TEXT,
    worker TEXT,
    claim_id TEXT,
    source TEXT,
    model TEXT,
    outcome_class TEXT,
    created_at INTEGER NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail))
) STRICT;
CREATE INDEX IF NOT EXISTS idx_factory_events_type_time
    ON factory_state_events (repo, event_type, created_at);
-- Partial indexes keep model attempts (most rows) out of lookups that never read them.
CREATE INDEX IF NOT EXISTS idx_factory_events_run
    ON factory_state_events (repo, run_id) WHERE event_type <> 'model_attempt';
CREATE INDEX IF NOT EXISTS idx_factory_events_model
    ON factory_state_events (source, model, created_at)
    WHERE event_type IN ('worker_failed', 'worker_completed');
CREATE INDEX IF NOT EXISTS idx_factory_events_worker
    ON factory_state_events (repo, worker, created_at)
    WHERE event_type IN ('worker_failed', 'worker_completed');
-- The provider daily cap counts today's assignments inside every acquisition.
CREATE INDEX IF NOT EXISTS idx_factory_events_assigned
    ON factory_state_events (repo, source, created_at)
    WHERE event_type = 'worker_assigned';

-- Raw model attempts older than the retention window, counted per UTC day.
CREATE TABLE IF NOT EXISTS factory_model_attempt_daily (
    repo TEXT NOT NULL,
    day TEXT NOT NULL,
    source TEXT NOT NULL,
    model TEXT NOT NULL,
    outcome_class TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    timed_attempts INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (repo, day, source, model, outcome_class)
) STRICT;

-- Small resumable cursors: a read watermark and the compact state that goes
-- with it, such as trusted retry markers or the last outcome reconcile time.
CREATE TABLE IF NOT EXISTS factory_cursor (
    repo TEXT NOT NULL,
    name TEXT NOT NULL,
    cursor_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail)),
    PRIMARY KEY (repo, name)
) STRICT;

INSERT OR IGNORE INTO _schema_version (version, applied_at) VALUES (1, unixepoch());
INSERT OR IGNORE INTO _schema_version (version, applied_at) VALUES (2, unixepoch());

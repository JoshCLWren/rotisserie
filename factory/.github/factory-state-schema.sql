-- Factory shared discovery state, isolated from product tables, in the
-- adopter's Neon database factory_state.
-- The factory_state_rw role has read_write access to ONLY this database.
-- GitHub remains authoritative for leases, reviews, checks, and merges.

CREATE TABLE IF NOT EXISTS public.factory_state_kv (
    cache_key text PRIMARY KEY,
    payload jsonb NOT NULL DEFAULT '[]'::jsonb,
    fetched_at timestamptz NOT NULL DEFAULT '1970-01-01T00:00:00Z',
    expires_at timestamptz NOT NULL DEFAULT '1970-01-01T00:00:00Z',
    refresh_owner text,
    refresh_until timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT factory_state_payload_array CHECK (jsonb_typeof(payload) = 'array'),
    CONSTRAINT factory_state_refresh_pair CHECK (
        (refresh_owner IS NULL) = (refresh_until IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS public.factory_state_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source text NOT NULL,
    event_type text NOT NULL,
    subject_type text,
    subject_number bigint,
    created_at timestamptz NOT NULL DEFAULT now(),
    detail jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_factory_state_events_recent
    ON public.factory_state_events (created_at DESC);

-- Also applied to existing databases by factory_state_indexes.py through
-- the trusted dispatcher bootstrap; keep both lists identical.
CREATE INDEX IF NOT EXISTS idx_factory_state_events_event_id ON public.factory_state_events ((detail->>'event_id'));
CREATE INDEX IF NOT EXISTS idx_factory_state_events_run_id ON public.factory_state_events ((detail->>'run_id'));
CREATE INDEX IF NOT EXISTS idx_factory_state_events_worker_recent ON public.factory_state_events (created_at) WHERE event_type IN ('worker_started', 'worker_heartbeat', 'worker_outcome_signal', 'worker_completed', 'worker_idle', 'worker_failed');
CREATE INDEX IF NOT EXISTS idx_factory_state_events_retired ON public.factory_state_events (id) WHERE event_type IN ('cache_refresh', 'target_reconciled');
CREATE INDEX IF NOT EXISTS idx_factory_state_kv_key_prefix ON public.factory_state_kv (cache_key text_pattern_ops);
CREATE TABLE IF NOT EXISTS public.factory_model_attempt_daily ( repo text NOT NULL, day date NOT NULL, source text NOT NULL, model text NOT NULL, outcome_class text NOT NULL, attempts bigint NOT NULL DEFAULT 0, timed_attempts bigint NOT NULL DEFAULT 0, duration_ms bigint NOT NULL DEFAULT 0, PRIMARY KEY (repo, day, source, model, outcome_class));

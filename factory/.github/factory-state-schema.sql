-- ComicPile factory shared discovery state, isolated from product tables.
-- Applied 2026-11-13 to Neon project comic-pile, database factory_state.
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

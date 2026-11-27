-- Runtime ownership is retained across manager restarts and expired requests.
-- This migration creates new management ledgers only; it changes no user rows.
CREATE TABLE platform.worker_runtime_owners (
    owner_key VARCHAR(32) PRIMARY KEY CHECK (owner_key ~ '^[a-f0-9]{32}$'),
    next_generation BIGINT NOT NULL DEFAULT 1 CHECK (next_generation > 0),
    operation_id UUID,
    operation_started_at TIMESTAMPTZ,
    storage JSONB,
    storage_admitted BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK (storage IS NULL OR jsonb_typeof(storage) = 'object'),
    CHECK (NOT storage_admitted OR storage IS NOT NULL)
);

CREATE TABLE platform.worker_runtime_reservations (
    owner_key VARCHAR(32) NOT NULL REFERENCES platform.worker_runtime_owners(owner_key),
    generation BIGINT NOT NULL CHECK (generation > 0),
    request_id UUID NOT NULL,
    purpose VARCHAR(16) NOT NULL CHECK (purpose IN ('worker', 'card')),
    state VARCHAR(16) NOT NULL CHECK (state IN ('CLAIMED','BOUND','READY','DELETING','UNCERTAIN','RELEASED')),
    expires_at BIGINT NOT NULL CHECK (expires_at BETWEEN 1 AND 4102444800),
    catalog_hash CHAR(64) NOT NULL CHECK (catalog_hash ~ '^[a-f0-9]{64}$'),
    policy_hash CHAR(64) NOT NULL CHECK (policy_hash ~ '^[a-f0-9]{64}$'),
    empty_storage BOOLEAN NOT NULL,
    management JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(management) = 'object'),
    handle JSONB,
    native_workspace_id VARCHAR(128),
    last_error_code VARCHAR(64),
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    released_at TIMESTAMPTZ,
    release_audit_id UUID,
    PRIMARY KEY (owner_key, generation),
    UNIQUE (owner_key, request_id),
    CHECK (handle IS NULL OR jsonb_typeof(handle) = 'object'),
    CHECK (state NOT IN ('BOUND','READY') OR handle IS NOT NULL),
    CHECK (state <> 'READY' OR native_workspace_id IS NOT NULL),
    CHECK ((state = 'RELEASED') = (released_at IS NOT NULL AND release_audit_id IS NOT NULL))
);

-- Workers and offline helpers occupy the SAME owner slot. No TTL predicate.
CREATE UNIQUE INDEX worker_runtime_one_active_owner
    ON platform.worker_runtime_reservations(owner_key) WHERE state <> 'RELEASED';

CREATE TABLE platform.worker_runtime_events (
    event_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_key VARCHAR(32) NOT NULL,
    generation BIGINT,
    operation_id UUID NOT NULL,
    event_type VARCHAR(40) NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(details) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX worker_runtime_events_owner ON platform.worker_runtime_events(owner_key, event_id);

COMMENT ON TABLE platform.worker_runtime_reservations IS
 'Persistent runtime exclusion; expires_at never releases a reservation; no model or route credentials are stored.';
COMMENT ON COLUMN platform.worker_runtime_owners.operation_id IS
 'Fencing token for the manager holding the PostgreSQL session advisory lock; not an expiring ownership lease.';

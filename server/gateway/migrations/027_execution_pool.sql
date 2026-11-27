CREATE SCHEMA execution;
-- Runtime ownership is retained across manager restarts and expired requests.
-- This migration creates new management ledgers only; it changes no user rows.
CREATE TABLE execution.worker_runtime_owners (
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

CREATE TABLE execution.worker_runtime_reservations (
    owner_key VARCHAR(32) NOT NULL REFERENCES execution.worker_runtime_owners(owner_key),
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
    ON execution.worker_runtime_reservations(owner_key) WHERE state <> 'RELEASED';

CREATE TABLE execution.worker_runtime_events (
    event_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_key VARCHAR(32) NOT NULL,
    generation BIGINT,
    operation_id UUID NOT NULL,
    event_type VARCHAR(40) NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(details) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX worker_runtime_events_owner ON execution.worker_runtime_events(owner_key, event_id);

COMMENT ON TABLE execution.worker_runtime_reservations IS
 'Persistent runtime exclusion; expires_at never releases a reservation; no model or route credentials are stored.';
COMMENT ON COLUMN execution.worker_runtime_owners.operation_id IS
 'Fencing token for the manager holding the PostgreSQL session advisory lock; not an expiring ownership lease.';

-- Durable admission only: never persist a lease, catalog, prompt or credential.
-- Old active generations retain capacity until existing node proof permits release.
ALTER TABLE execution.worker_runtime_reservations
  ADD COLUMN cpu_millis integer NOT NULL DEFAULT 1000 CHECK (cpu_millis > 0),
  ADD COLUMN memory_mib integer NOT NULL DEFAULT 1536 CHECK (memory_mib > 0),
  ADD COLUMN ephemeral_mib integer NOT NULL DEFAULT 1024 CHECK (ephemeral_mib > 0),
  ADD COLUMN reconcile_attempts integer NOT NULL DEFAULT 0 CHECK (reconcile_attempts >= 0),
  ADD COLUMN reconcile_after timestamptz,
  ADD COLUMN attention_required boolean NOT NULL DEFAULT false;

CREATE TABLE execution.worker_runtime_queue (
  sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  request_id uuid NOT NULL UNIQUE,
  owner_key text NOT NULL REFERENCES execution.worker_runtime_owners(owner_key),
  state text NOT NULL CHECK (state IN ('WAITING','ADMITTED','CANCELLED','EXPIRED')),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  expires_at timestamptz NOT NULL,
  completed_at timestamptz,
  generation bigint,
  CHECK (expires_at > created_at),
  CHECK ((state = 'WAITING') = (completed_at IS NULL)),
  CHECK ((state = 'ADMITTED') = (generation IS NOT NULL))
);
CREATE UNIQUE INDEX worker_runtime_queue_one_waiter ON execution.worker_runtime_queue(owner_key) WHERE state='WAITING';
CREATE INDEX worker_runtime_queue_fifo ON execution.worker_runtime_queue(sequence) WHERE state='WAITING';
COMMENT ON TABLE execution.worker_runtime_queue IS 'FIFO admission metadata, one waiting request per owner. Fresh authenticated polling is required to start. Queue expiry never releases a runtime generation.';

-- Offline file cleanup occupies the same durable owner slot as its worker.
ALTER TABLE execution.worker_runtime_reservations
  DROP CONSTRAINT worker_runtime_reservations_purpose_check;
ALTER TABLE execution.worker_runtime_reservations
  ADD CONSTRAINT worker_runtime_reservations_purpose_check
  CHECK (purpose IN ('worker','card','files'));
ALTER TABLE execution.worker_runtime_reservations ADD COLUMN runtime_fingerprint CHAR(64)
  CHECK(runtime_fingerprint IS NULL OR runtime_fingerprint ~ '^[a-f0-9]{64}$');
CREATE UNIQUE INDEX worker_runtime_fingerprint_unique
  ON execution.worker_runtime_reservations(owner_key,runtime_fingerprint)
  WHERE runtime_fingerprint IS NOT NULL;
COMMENT ON COLUMN execution.worker_runtime_reservations.runtime_fingerprint IS
 'SHA256 of generation route token, persisted before Pod creation; hash only, not a credential. Used to require proven RELEASED source writers before offline exact-file cleanup.';

-- Compute allocations are independent of web chat. Never infer release from TTL.
ALTER TABLE execution.worker_runtime_owners ADD COLUMN IF NOT EXISTS home_node text;
ALTER TABLE execution.worker_runtime_reservations
  ADD COLUMN IF NOT EXISTS resource_class text NOT NULL DEFAULT 'legacy',
  ADD COLUMN IF NOT EXISTS execution_node text NOT NULL DEFAULT '';
ALTER TABLE execution.worker_runtime_queue
  ADD COLUMN IF NOT EXISTS resource_class text NOT NULL DEFAULT 'legacy',
  ADD COLUMN IF NOT EXISTS execution_node text NOT NULL DEFAULT '';
-- Preserve the node used by old persistent volumes, including released workers.
UPDATE execution.worker_runtime_owners o SET home_node=r.node
FROM (SELECT DISTINCT ON (owner_key) owner_key,handle->>'node_name' node
      FROM execution.worker_runtime_reservations WHERE handle IS NOT NULL
      ORDER BY owner_key,generation DESC) r
WHERE o.owner_key=r.owner_key AND o.home_node IS NULL;
UPDATE execution.worker_runtime_reservations SET execution_node=handle->>'node_name'
WHERE handle IS NOT NULL AND execution_node='';
CREATE INDEX IF NOT EXISTS worker_runtime_queue_node_fifo
  ON execution.worker_runtime_queue(execution_node,sequence) WHERE state='WAITING';
CREATE INDEX IF NOT EXISTS worker_runtime_reservations_node_active
  ON execution.worker_runtime_reservations(execution_node) WHERE state<>'RELEASED';

-- A-side native chat uses the same engine with a separate ledger and budgets.
ALTER TABLE platform.worker_runtime_owners ADD COLUMN home_node text;
ALTER TABLE platform.worker_runtime_reservations ADD COLUMN resource_class text NOT NULL DEFAULT 'legacy', ADD COLUMN execution_node text NOT NULL DEFAULT '';
ALTER TABLE platform.worker_runtime_queue ADD COLUMN resource_class text NOT NULL DEFAULT 'legacy', ADD COLUMN execution_node text NOT NULL DEFAULT '';
UPDATE platform.worker_runtime_owners o SET home_node=r.node FROM (SELECT DISTINCT ON(owner_key) owner_key,handle->>'node_name' node FROM platform.worker_runtime_reservations WHERE handle IS NOT NULL ORDER BY owner_key,generation DESC) r WHERE o.owner_key=r.owner_key;
UPDATE platform.worker_runtime_reservations SET execution_node=handle->>'node_name' WHERE handle IS NOT NULL;

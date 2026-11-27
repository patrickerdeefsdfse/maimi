-- Durable admission only: never persist a lease, catalog, prompt or credential.
-- Old active generations retain capacity until existing node proof permits release.
ALTER TABLE platform.worker_runtime_reservations
  ADD COLUMN cpu_millis integer NOT NULL DEFAULT 1000 CHECK (cpu_millis > 0),
  ADD COLUMN memory_mib integer NOT NULL DEFAULT 1536 CHECK (memory_mib > 0),
  ADD COLUMN ephemeral_mib integer NOT NULL DEFAULT 1024 CHECK (ephemeral_mib > 0),
  ADD COLUMN reconcile_attempts integer NOT NULL DEFAULT 0 CHECK (reconcile_attempts >= 0),
  ADD COLUMN reconcile_after timestamptz,
  ADD COLUMN attention_required boolean NOT NULL DEFAULT false;

CREATE TABLE platform.worker_runtime_queue (
  sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  request_id uuid NOT NULL UNIQUE,
  owner_key text NOT NULL REFERENCES platform.worker_runtime_owners(owner_key),
  state text NOT NULL CHECK (state IN ('WAITING','ADMITTED','CANCELLED','EXPIRED')),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  expires_at timestamptz NOT NULL,
  completed_at timestamptz,
  generation bigint,
  CHECK (expires_at > created_at),
  CHECK ((state = 'WAITING') = (completed_at IS NULL)),
  CHECK ((state = 'ADMITTED') = (generation IS NOT NULL))
);
CREATE UNIQUE INDEX worker_runtime_queue_one_waiter ON platform.worker_runtime_queue(owner_key) WHERE state='WAITING';
CREATE INDEX worker_runtime_queue_fifo ON platform.worker_runtime_queue(sequence) WHERE state='WAITING';
COMMENT ON TABLE platform.worker_runtime_queue IS 'FIFO admission metadata, one waiting request per owner. Fresh authenticated polling is required to start. Queue expiry never releases a runtime generation.';

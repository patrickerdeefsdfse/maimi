-- Offline file cleanup occupies the same durable owner slot as its worker.
ALTER TABLE platform.worker_runtime_reservations
  DROP CONSTRAINT worker_runtime_reservations_purpose_check;
ALTER TABLE platform.worker_runtime_reservations
  ADD CONSTRAINT worker_runtime_reservations_purpose_check
  CHECK (purpose IN ('worker','card','files'));
ALTER TABLE platform.worker_runtime_reservations ADD COLUMN runtime_fingerprint CHAR(64)
  CHECK(runtime_fingerprint IS NULL OR runtime_fingerprint ~ '^[a-f0-9]{64}$');
CREATE UNIQUE INDEX worker_runtime_fingerprint_unique
  ON platform.worker_runtime_reservations(owner_key,runtime_fingerprint)
  WHERE runtime_fingerprint IS NOT NULL;
COMMENT ON COLUMN platform.worker_runtime_reservations.runtime_fingerprint IS
 'SHA256 of generation route token, persisted before Pod creation; hash only, not a credential. Used to require proven RELEASED source writers before offline exact-file cleanup.';

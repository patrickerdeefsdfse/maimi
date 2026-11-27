-- Candidate migration. The release must prove 017 is the exact predecessor.
-- Adds a dedicated card lifecycle ledger only. No existing row is changed,
-- no historical artifact is claimed, and no business file is removed here.
-- Integer UTC seconds are also used by the native card-v1 descriptor.
CREATE TABLE platform.card_file_refs (
 owner_key CHAR(32) NOT NULL CHECK(owner_key ~ '^[a-f0-9]{32}$'),
 session_id VARCHAR(80) NOT NULL
   CHECK(session_id ~ '^(session-)?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'),
 artifact_id VARCHAR(64) NOT NULL CHECK(artifact_id ~ '^pjf_[A-Za-z0-9_-]{32}$'),
 creation_job_id UUID,
 logical_job_id VARCHAR(128),
 role VARCHAR(16) NOT NULL CHECK(role IN ('input','photo','preview','print','template','intermediate')),
 provenance VARCHAR(24) NOT NULL CHECK(provenance IN ('new-card-write','legacy-card-print','legacy-card-state')),
 storage_kind VARCHAR(16) NOT NULL CHECK(storage_kind IN ('card-v1','legacy-native')),
 storage_id VARCHAR(40),
 reference_hash CHAR(64) NOT NULL CHECK(reference_hash ~ '^[a-f0-9]{64}$'),
 sha256 CHAR(64) NOT NULL CHECK(sha256 ~ '^[a-f0-9]{64}$'),
 file_bytes BIGINT NOT NULL CHECK(file_bytes BETWEEN 1 AND 134217728),
 scope_epoch UUID,
 encrypted_descriptor TEXT NOT NULL CHECK(octet_length(encrypted_descriptor) BETWEEN 1 AND 32768),
 created_at BIGINT NOT NULL DEFAULT floor(extract(epoch FROM clock_timestamp()))::bigint
   CHECK(created_at BETWEEN 0 AND 4102444800),
 expires_at BIGINT GENERATED ALWAYS AS (created_at + 172800) STORED,
 status VARCHAR(20) NOT NULL DEFAULT 'pending'
   CHECK(status IN ('pending','active','cleanup_pending','deleted','retained_shared')),
 cleanup_token UUID,
 lease_until BIGINT,
 cleanup_attempts INTEGER NOT NULL DEFAULT 0 CHECK(cleanup_attempts BETWEEN 0 AND 2147483647),
 next_attempt_at BIGINT NOT NULL DEFAULT 0 CHECK(next_attempt_at >= 0),
 last_error_code VARCHAR(48),
 completed_at BIGINT,
 reclaimed_bytes BIGINT NOT NULL DEFAULT 0 CHECK(reclaimed_bytes BETWEEN 0 AND file_bytes),
 PRIMARY KEY(owner_key,session_id,artifact_id),
 UNIQUE(owner_key,storage_id),
 UNIQUE(owner_key,session_id,creation_job_id,role,reference_hash),
 CHECK((storage_kind='card-v1' AND storage_id IS NOT NULL AND storage_id ~ '^cfs_[a-f0-9]{32}$'
     AND creation_job_id IS NOT NULL AND scope_epoch IS NOT NULL AND provenance='new-card-write')
   OR (storage_kind='legacy-native' AND storage_id IS NULL AND provenance IN ('legacy-card-print','legacy-card-state'))),
 CHECK((status='cleanup_pending' AND cleanup_token IS NOT NULL AND lease_until IS NOT NULL)
    OR (status<>'cleanup_pending' AND cleanup_token IS NULL AND lease_until IS NULL)),
 CHECK((status IN ('deleted','retained_shared') AND completed_at IS NOT NULL)
    OR (status NOT IN ('deleted','retained_shared') AND completed_at IS NULL)),
 CHECK(status<>'retained_shared' OR (storage_kind='legacy-native' AND reclaimed_bytes=0))
);

-- Deliberately no FK to project_scopes/artifacts: explicit session deletion
-- must not erase an unfinished physical cleanup obligation.
CREATE INDEX card_file_refs_due ON platform.card_file_refs(storage_kind,expires_at,next_attempt_at)
 WHERE status NOT IN ('deleted','retained_shared');
CREATE INDEX card_file_refs_pending_quota ON platform.card_file_refs(owner_key,session_id)
 WHERE status = 'pending';

CREATE FUNCTION platform.card_file_ref_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
 IF ROW(NEW.owner_key,NEW.session_id,NEW.artifact_id,NEW.creation_job_id,NEW.logical_job_id,
        NEW.role,NEW.provenance,NEW.storage_kind,NEW.storage_id,NEW.reference_hash,NEW.sha256,
        NEW.file_bytes,NEW.scope_epoch,NEW.encrypted_descriptor,NEW.created_at)
 IS DISTINCT FROM
    ROW(OLD.owner_key,OLD.session_id,OLD.artifact_id,OLD.creation_job_id,OLD.logical_job_id,
        OLD.role,OLD.provenance,OLD.storage_kind,OLD.storage_id,OLD.reference_hash,OLD.sha256,
        OLD.file_bytes,OLD.scope_epoch,OLD.encrypted_descriptor,OLD.created_at)
 THEN RAISE EXCEPTION 'card_file_ref_identity_immutable'; END IF;
 IF OLD.status IN ('deleted','retained_shared') AND NEW IS DISTINCT FROM OLD
 THEN RAISE EXCEPTION 'card_file_ref_tombstone_immutable'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER card_file_ref_immutable BEFORE UPDATE ON platform.card_file_refs
FOR EACH ROW EXECUTE FUNCTION platform.card_file_ref_immutable();

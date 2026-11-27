-- Managed cloud files. Physical deletion and logical access expiry are distinct.
-- No caller supplied path; no foreign key cascade may release charged bytes.
CREATE TABLE platform.storage_accounts (
 owner_key CHAR(32) PRIMARY KEY CHECK(owner_key ~ '^[a-f0-9]{32}$'),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE platform.storage_files (
 file_id UUID PRIMARY KEY,
 owner_key CHAR(32) NOT NULL REFERENCES platform.storage_accounts,
 session_id VARCHAR(128) NOT NULL,
 request_key CHAR(64) NOT NULL,
 request_digest CHAR(64) NOT NULL,
 kind VARCHAR(24) NOT NULL CHECK(kind IN ('ordinary','enterprise_input','enterprise_intermediate','enterprise_output')),
 job_id VARCHAR(128),
 name VARCHAR(255) NOT NULL,
 media_type VARCHAR(160) NOT NULL,
 sha256 CHAR(64) NOT NULL,
 expected_bytes BIGINT NOT NULL CHECK(expected_bytes BETWEEN 0 AND 134217728),
 physical_bytes BIGINT NOT NULL DEFAULT 0 CHECK(physical_bytes BETWEEN 0 AND 134217728),
 status VARCHAR(16) NOT NULL CHECK(status IN ('reserved','active','reclaiming','deleted')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 expires_at TIMESTAMPTZ NOT NULL,
 first_transfer_at TIMESTAMPTZ,
 lease_until TIMESTAMPTZ,
 cleanup_token UUID,
 retry_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 deletion_reason VARCHAR(40),
 deleted_at TIMESTAMPTZ,
 last_error VARCHAR(80),
 UNIQUE(owner_key,request_key)
);
CREATE INDEX storage_files_due ON platform.storage_files(expires_at,retry_at)
 WHERE status <> 'deleted';
CREATE INDEX storage_files_owner_session ON platform.storage_files(owner_key,session_id)
 WHERE status <> 'deleted';
CREATE TABLE platform.storage_downloads (
 lease_id UUID PRIMARY KEY,
 file_id UUID NOT NULL REFERENCES platform.storage_files,
 owner_key CHAR(32) NOT NULL,
 session_id VARCHAR(128) NOT NULL,
 expires_at TIMESTAMPTZ NOT NULL,
 completed_at TIMESTAMPTZ,
 expected_bytes BIGINT NOT NULL,
 expected_sha256 CHAR(64) NOT NULL
);
CREATE INDEX storage_downloads_live ON platform.storage_downloads(file_id,expires_at)
 WHERE completed_at IS NULL;
CREATE TABLE platform.storage_events (
 event_id BIGSERIAL PRIMARY KEY,
 file_id UUID NOT NULL REFERENCES platform.storage_files,
 owner_key CHAR(32) NOT NULL,
 session_id VARCHAR(128) NOT NULL,
 kind VARCHAR(24) NOT NULL,
 action VARCHAR(24) NOT NULL CHECK(action IN ('expired','deleted')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(file_id,action)
);
-- Content-bearing enterprise state gets an independent finite deadline.
-- Existing row values are not migrated or deleted by this schema-only change.
ALTER TABLE platform.project_jobs ADD COLUMN IF NOT EXISTS content_expires_at TIMESTAMPTZ;
ALTER TABLE platform.project_artifacts ADD COLUMN IF NOT EXISTS managed_file_id UUID;
ALTER TABLE platform.project_session_state ADD COLUMN IF NOT EXISTS lifecycle_job_id UUID;
CREATE INDEX IF NOT EXISTS project_artifacts_managed_file ON platform.project_artifacts(managed_file_id)
 WHERE managed_file_id IS NOT NULL;
CREATE TABLE platform.storage_task_files (
 owner_key CHAR(32) NOT NULL,
 session_id VARCHAR(128) NOT NULL,
 task_id VARCHAR(128) NOT NULL,
 file_id UUID NOT NULL REFERENCES platform.storage_files,
 role VARCHAR(24) NOT NULL CHECK(role IN ('input','intermediate','output')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(owner_key,session_id,task_id,file_id)
);
-- A delivered native workspace file has two copies until the source runtime
-- confirms exact path/version/digest removal or that generation is destroyed.
-- This journal is never permission to delete an arbitrary path.
CREATE TABLE platform.storage_source_copies (
 file_id UUID NOT NULL REFERENCES platform.storage_files,
 owner_key CHAR(32) NOT NULL,
 session_id VARCHAR(128) NOT NULL,
 runtime_fingerprint CHAR(64) NOT NULL,
 source_path TEXT NOT NULL,
 source_version_hash CHAR(64) NOT NULL,
 expected_sha256 CHAR(64) NOT NULL,
 expected_bytes BIGINT NOT NULL,
 state VARCHAR(24) NOT NULL DEFAULT 'temporary_retained'
  CHECK(state IN ('temporary_retained','cleanup_required','removed_confirmed')),
 recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(file_id,runtime_fingerprint,source_path)
);
ALTER TABLE platform.hybrid_attachments ADD COLUMN managed_file_id UUID;
ALTER TABLE platform.hybrid_attachments ADD COLUMN managed_owner_key CHAR(32);
ALTER TABLE platform.hybrid_attachments ADD COLUMN managed_session_id VARCHAR(128);
ALTER TABLE platform.hybrid_attachments DROP CONSTRAINT attachment_content_location;
ALTER TABLE platform.hybrid_attachments ADD CONSTRAINT attachment_content_location CHECK (
 (managed_file_id IS NULL AND ((content IS NOT NULL AND object_key IS NULL) OR
   (content IS NULL AND object_key IS NOT NULL AND object_key ~ '^[a-f0-9]{64}/[a-f0-9]{64}$'))) OR
 (managed_file_id IS NOT NULL AND managed_owner_key IS NOT NULL AND managed_owner_key ~ '^[a-f0-9]{32}$' AND managed_session_id IS NOT NULL
   AND content IS NULL AND object_key IS NULL));
ALTER TABLE platform.hybrid_runs ADD COLUMN content_expires_at TIMESTAMPTZ;
ALTER TABLE platform.hybrid_runs ADD COLUMN managed_output_file_id UUID;
ALTER TABLE platform.hybrid_runs ADD COLUMN managed_owner_key CHAR(32);
ALTER TABLE platform.hybrid_runs ADD COLUMN managed_session_id VARCHAR(128);

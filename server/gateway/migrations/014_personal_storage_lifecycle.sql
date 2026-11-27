-- Native DSH remains the runtime format. PostgreSQL owns searchable structured
-- history projections, lifecycle receipts and file metadata; blobs stay on disk.
ALTER TABLE platform.hybrid_attachments ADD COLUMN object_key TEXT;
ALTER TABLE platform.hybrid_attachments ALTER COLUMN content DROP NOT NULL;
ALTER TABLE platform.hybrid_attachments ADD CONSTRAINT attachment_content_location
  CHECK ((content IS NOT NULL AND object_key IS NULL) OR
         (content IS NULL AND object_key IS NOT NULL AND object_key ~ '^[a-f0-9]{64}/[a-f0-9]{64}$'));

CREATE TABLE platform.personal_cloud_history (
  owner_key CHAR(32) NOT NULL,
  session_id TEXT NOT NULL,
  owner_user_no VARCHAR(100) NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  native_format TEXT NOT NULL,
  source_digest CHAR(64) NOT NULL,
  last_activity_at TIMESTAMPTZ NOT NULL,
  header JSONB NOT NULL,
  event_count INTEGER NOT NULL CHECK(event_count >= 0),
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','quarantined','purged')),
  quarantined_at TIMESTAMPTZ,
  purge_after TIMESTAMPTZ,
  restored_at TIMESTAMPTZ,
  trash_id UUID,
  restore_requested_at TIMESTAMPTZ,
  legal_hold BOOLEAN NOT NULL DEFAULT FALSE,
  synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY(owner_key,session_id),
  CHECK(owner_key ~ '^[a-f0-9]{32}$'),
  CHECK(length(session_id) BETWEEN 1 AND 200)
);
CREATE INDEX personal_cloud_history_owner_activity ON platform.personal_cloud_history
  (owner_user_no,status,last_activity_at DESC,session_id);
CREATE INDEX personal_cloud_history_expiry ON platform.personal_cloud_history(status,purge_after)
  WHERE NOT legal_hold;
CREATE TABLE platform.personal_cloud_history_events (
  owner_key CHAR(32) NOT NULL,
  session_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
  event JSONB NOT NULL,
  PRIMARY KEY(owner_key,session_id,ordinal),
  FOREIGN KEY(owner_key,session_id) REFERENCES platform.personal_cloud_history ON DELETE CASCADE
);
CREATE TABLE platform.personal_cloud_storage_jobs (
  owner_key CHAR(32) PRIMARY KEY CHECK(owner_key ~ '^[a-f0-9]{32}$'),
  owner_user_no VARCHAR(100) NOT NULL,
  last_attempt_at TIMESTAMPTZ,
  last_success_at TIMESTAMPTZ,
  last_error_code VARCHAR(100),
  synced_sessions INTEGER NOT NULL DEFAULT 0,
  skipped_sessions INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE platform.personal_cloud_lifecycle_audit (
  id BIGSERIAL PRIMARY KEY,
  owner_key CHAR(32) NOT NULL,
  session_id TEXT NOT NULL,
  action VARCHAR(40) NOT NULL,
  source_digest CHAR(64),
  occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- No content in audit. Project files are intentionally excluded from chat expiry.

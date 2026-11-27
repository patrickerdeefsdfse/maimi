-- Candidate only; release assembly must verify 016 is still the next migration.
-- No file bodies, passwords, upstream cookies or invocation parameters in SQL.
CREATE TABLE platform.project_scopes (
 owner_key CHAR(32) NOT NULL REFERENCES platform.personal_cloud_retention(owner_key),
 session_id VARCHAR(80) NOT NULL,
 epoch UUID NOT NULL,
 fence BIGINT NOT NULL DEFAULT 0 CHECK(fence >= 0),
 PRIMARY KEY(owner_key,session_id),
 CHECK(session_id ~ '^(session-)?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
);
CREATE TABLE platform.project_session_state (
 owner_key CHAR(32) NOT NULL, session_id VARCHAR(80) NOT NULL,
 state_key VARCHAR(160) NOT NULL, encrypted_payload TEXT NOT NULL,
 payload_bytes INTEGER NOT NULL CHECK(payload_bytes BETWEEN 0 AND 4194304),
 version BIGINT NOT NULL DEFAULT 1, fence BIGINT NOT NULL,
 expires_at TIMESTAMPTZ,
 PRIMARY KEY(owner_key,session_id,state_key),
 FOREIGN KEY(owner_key,session_id) REFERENCES platform.project_scopes ON DELETE CASCADE
);
CREATE TABLE platform.project_artifacts (
 owner_key CHAR(32) NOT NULL, session_id VARCHAR(80) NOT NULL,
 artifact_id VARCHAR(64) NOT NULL, source VARCHAR(20) NOT NULL CHECK(source IN ('native_message','project_output')),
 reference_hash CHAR(64) NOT NULL, encrypted_payload TEXT NOT NULL,
 file_bytes INTEGER NOT NULL CHECK(file_bytes BETWEEN 0 AND 33554432),
 created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(owner_key,session_id,artifact_id),
 UNIQUE(owner_key,session_id,source,reference_hash),
 FOREIGN KEY(owner_key,session_id) REFERENCES platform.project_scopes ON DELETE CASCADE
);
CREATE TABLE platform.project_jobs (
 owner_key CHAR(32) NOT NULL, session_id VARCHAR(80) NOT NULL,
 job_id UUID NOT NULL UNIQUE, request_id UUID NOT NULL,
 invocation_id VARCHAR(128) NOT NULL, lease_hash CHAR(64) NOT NULL,
 connector VARCHAR(50) NOT NULL, action VARCHAR(80) NOT NULL,
 input_hash CHAR(64) NOT NULL, process_id VARCHAR(64) NOT NULL,
 status VARCHAR(20) NOT NULL CHECK(status IN ('running','cancelling','completed','failed','cancelled','interrupted')),
 deadline TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 encrypted_result TEXT, error_code VARCHAR(100),
 PRIMARY KEY(owner_key,session_id,request_id),
 FOREIGN KEY(owner_key,session_id) REFERENCES platform.project_scopes ON DELETE CASCADE
);
CREATE INDEX project_jobs_running ON platform.project_jobs(status,deadline);
CREATE TABLE platform.project_invocation_revocations (
 owner_key CHAR(32) NOT NULL, session_id VARCHAR(80) NOT NULL,
 invocation_id VARCHAR(128) NOT NULL, lease_hash CHAR(64) NOT NULL,
 expires_at TIMESTAMPTZ NOT NULL,
 PRIMARY KEY(owner_key,session_id,invocation_id),
 FOREIGN KEY(owner_key,session_id) REFERENCES platform.project_scopes ON DELETE CASCADE
);

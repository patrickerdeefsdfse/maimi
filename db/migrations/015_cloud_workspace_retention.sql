-- One shared cloud workspace per employee. First adoption grants a full 30 days.
CREATE TABLE platform.personal_cloud_retention (
 owner_key CHAR(32) PRIMARY KEY CHECK(owner_key ~ '^[a-f0-9]{32}$'),
 workspace_id UUID NOT NULL UNIQUE REFERENCES platform.hybrid_workspaces(id),
 owner_user_no VARCHAR(100) NOT NULL,
 policy_started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 last_access_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 deletion_id UUID,
 deletion_started_at TIMESTAMPTZ,
 cleared_at TIMESTAMPTZ,
 archive_migrated_at TIMESTAMPTZ,
 last_attempt_at TIMESTAMPTZ,
 last_error_code VARCHAR(80),
 CHECK ((deletion_id IS NULL) = (deletion_started_at IS NULL))
);
INSERT INTO platform.personal_cloud_retention(owner_key,workspace_id,owner_user_no)
 SELECT replace(id::text,'-',''),id,owner_user_no FROM platform.hybrid_workspaces WHERE kind='personal';
CREATE INDEX personal_cloud_retention_due ON platform.personal_cloud_retention(last_access_at);
-- Only opaque identifiers/control state remain here after permanent deletion.
CREATE TABLE platform.personal_cloud_session_deletions (
 owner_key CHAR(32) NOT NULL REFERENCES platform.personal_cloud_retention(owner_key),
 session_id VARCHAR(80) NOT NULL CHECK(session_id ~ '^(session-)?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'),
 requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 completed_at TIMESTAMPTZ,
 last_error_code VARCHAR(80),
 PRIMARY KEY(owner_key,session_id)
);
-- The removed recycle bin cannot retain an additional recoverable content copy.
INSERT INTO platform.personal_cloud_session_deletions(owner_key,session_id)
 SELECT h.owner_key,h.session_id FROM platform.personal_cloud_history h
 JOIN platform.personal_cloud_retention r ON r.owner_key=h.owner_key AND r.owner_user_no=h.owner_user_no
 WHERE h.status='quarantined' AND h.session_id ~ '^(session-)?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$';
-- Immutable departmental documents stay immutable. Explicit whole-personal-
-- workspace expiry may delete versions, but can never edit a version.
CREATE OR REPLACE FUNCTION platform.reject_document_version_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' AND EXISTS (
  SELECT 1 FROM platform.hybrid_documents d
  JOIN platform.personal_cloud_retention r ON r.workspace_id=d.workspace_id
  JOIN platform.hybrid_workspaces w ON w.id=r.workspace_id
  WHERE d.id=OLD.document_id AND w.kind='personal' AND w.owner_user_no=r.owner_user_no
    AND d.owner_user_no=r.owner_user_no AND r.deletion_id IS NOT NULL
 ) THEN RETURN OLD; END IF;
 RAISE EXCEPTION 'document versions are immutable';
END; $$;

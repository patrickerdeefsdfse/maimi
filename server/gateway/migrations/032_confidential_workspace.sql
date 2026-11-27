-- Ordinary and confidential Hosts have different PVCs, histories and leases.
ALTER TABLE platform.hybrid_workspaces DROP CONSTRAINT hybrid_workspaces_kind_check;
ALTER TABLE platform.hybrid_workspaces ADD CONSTRAINT hybrid_workspaces_kind_check
  CHECK (kind IN ('department','group','personal','confidential'));
ALTER TABLE platform.hybrid_workspaces DROP CONSTRAINT personal_workspace_owner;
ALTER TABLE platform.hybrid_workspaces ADD CONSTRAINT personal_workspace_owner
  CHECK ((kind IN ('personal','confidential')) = (owner_user_no IS NOT NULL));
CREATE UNIQUE INDEX hybrid_confidential_workspace_owner
  ON platform.hybrid_workspaces(owner_user_no) WHERE kind='confidential';

CREATE OR REPLACE FUNCTION platform.reject_document_version_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' AND EXISTS (
  SELECT 1 FROM platform.hybrid_documents d
  JOIN platform.personal_cloud_retention r ON r.workspace_id=d.workspace_id
  JOIN platform.hybrid_workspaces w ON w.id=r.workspace_id
  WHERE d.id=OLD.document_id AND w.kind IN ('personal','confidential') AND w.owner_user_no=r.owner_user_no
    AND d.owner_user_no=r.owner_user_no AND r.deletion_id IS NOT NULL
 ) THEN RETURN OLD; END IF;
 RAISE EXCEPTION 'document versions are immutable';
END; $$;

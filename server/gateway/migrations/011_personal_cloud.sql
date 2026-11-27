-- Preserve historical department/group data; new native cloud Hosts are private.
ALTER TABLE platform.hybrid_workspaces DROP CONSTRAINT IF EXISTS hybrid_workspaces_kind_check;
ALTER TABLE platform.hybrid_workspaces ADD CONSTRAINT hybrid_workspaces_kind_check CHECK(kind IN ('department','group','personal'));
ALTER TABLE platform.hybrid_workspaces ADD COLUMN owner_user_no VARCHAR(100);
ALTER TABLE platform.hybrid_workspaces ADD CONSTRAINT personal_workspace_owner CHECK((kind='personal')=(owner_user_no IS NOT NULL));
CREATE UNIQUE INDEX hybrid_personal_workspace_owner ON platform.hybrid_workspaces(owner_user_no) WHERE kind='personal';

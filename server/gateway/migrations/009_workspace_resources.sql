-- Additive: no personal DSH data is imported or uploaded.
ALTER TABLE platform.hybrid_workspaces ADD COLUMN IF NOT EXISTS kind VARCHAR(16) NOT NULL DEFAULT 'department' CHECK (kind IN ('department','group'));
ALTER TABLE platform.hybrid_workspace_grants ADD COLUMN IF NOT EXISTS can_publish BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE platform.hybrid_workspace_grants ADD COLUMN IF NOT EXISTS can_retrieve BOOLEAN NOT NULL DEFAULT FALSE;
CREATE TABLE IF NOT EXISTS platform.hybrid_documents (
 id UUID PRIMARY KEY,
 workspace_id UUID NOT NULL REFERENCES platform.hybrid_workspaces(id),
 owner_user_no VARCHAR(100) NOT NULL,
 name VARCHAR(200) NOT NULL,
 current_version INTEGER NOT NULL CHECK(current_version>0),
 visibility VARCHAR(16) NOT NULL DEFAULT 'private' CHECK(visibility IN ('private','workspace')),
 enabled BOOLEAN NOT NULL DEFAULT TRUE,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS platform.hybrid_document_versions (
 document_id UUID NOT NULL REFERENCES platform.hybrid_documents(id),
 version INTEGER NOT NULL CHECK(version>0),
 name VARCHAR(200) NOT NULL,
 content TEXT NOT NULL,
 sha256 CHAR(64) NOT NULL,
 size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 0 AND 262144),
 created_by VARCHAR(100) NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(document_id,version)
);
CREATE INDEX IF NOT EXISTS hybrid_documents_visible ON platform.hybrid_documents(workspace_id,updated_at DESC);
ALTER TABLE platform.hybrid_runs ADD COLUMN IF NOT EXISTS document_refs JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE platform.hybrid_runs ADD COLUMN IF NOT EXISTS runner_manifest JSONB;
-- Version payloads are immutable; withdrawal is done on the parent document.
CREATE OR REPLACE FUNCTION platform.reject_document_version_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 RAISE EXCEPTION 'document versions are immutable';
END; $$;
DROP TRIGGER IF EXISTS hybrid_document_version_immutable ON platform.hybrid_document_versions;
CREATE TRIGGER hybrid_document_version_immutable BEFORE UPDATE OR DELETE ON platform.hybrid_document_versions
 FOR EACH ROW EXECUTE FUNCTION platform.reject_document_version_mutation();
CREATE OR REPLACE FUNCTION platform.reject_plugin_contract_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF ROW(OLD.plugin_key,OLD.version,OLD.runner,OLD.model_policy)
      IS DISTINCT FROM ROW(NEW.plugin_key,NEW.version,NEW.runner,NEW.model_policy) THEN
   RAISE EXCEPTION 'plugin contracts are immutable; publish a new version';
 END IF;
 RETURN NEW;
END; $$;
DROP TRIGGER IF EXISTS hybrid_plugin_contract_immutable ON platform.hybrid_plugins;
CREATE TRIGGER hybrid_plugin_contract_immutable BEFORE UPDATE ON platform.hybrid_plugins
 FOR EACH ROW EXECUTE FUNCTION platform.reject_plugin_contract_mutation();

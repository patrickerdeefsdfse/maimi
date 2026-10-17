-- Global capability catalog with explicit employee grants. No team workspace UI.
CREATE TABLE IF NOT EXISTS platform.enterprise_plugin_catalog (
 plugin_key VARCHAR(160) NOT NULL,
 version VARCHAR(80) NOT NULL,
 name VARCHAR(200) NOT NULL,
 description TEXT NOT NULL DEFAULT '',
 runner VARCHAR(160) NOT NULL,
 model_policy JSONB NOT NULL DEFAULT '{"mode":"actor-model"}',
 enabled BOOLEAN NOT NULL DEFAULT TRUE,
 PRIMARY KEY(plugin_key,version)
);
CREATE TABLE IF NOT EXISTS platform.enterprise_plugin_grants (
 plugin_key VARCHAR(160) NOT NULL,
 version VARCHAR(80) NOT NULL,
 user_no VARCHAR(100) NOT NULL,
 enabled BOOLEAN NOT NULL DEFAULT TRUE,
 PRIMARY KEY(plugin_key,version,user_no),
 FOREIGN KEY(plugin_key,version) REFERENCES platform.enterprise_plugin_catalog(plugin_key,version)
);
CREATE TABLE IF NOT EXISTS platform.enterprise_native_bindings (
 owner_user_no VARCHAR(100) NOT NULL,
 request_id UUID NOT NULL,
 native_session_id UUID NOT NULL,
 location VARCHAR(8) NOT NULL CHECK(location IN ('local','cloud')),
 request_hash CHAR(64) NOT NULL,
 plugin_key VARCHAR(160) NOT NULL,
 plugin_version VARCHAR(80) NOT NULL,
 conversation_id UUID,
 run_id UUID,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(owner_user_no,request_id),
 FOREIGN KEY(plugin_key,plugin_version) REFERENCES platform.enterprise_plugin_catalog(plugin_key,version)
);
CREATE INDEX IF NOT EXISTS enterprise_native_results ON platform.enterprise_native_bindings
 (owner_user_no,location,native_session_id,created_at DESC);
DROP TRIGGER IF EXISTS enterprise_plugin_contract_immutable ON platform.enterprise_plugin_catalog;
CREATE TRIGGER enterprise_plugin_contract_immutable BEFORE UPDATE ON platform.enterprise_plugin_catalog
 FOR EACH ROW EXECUTE FUNCTION platform.reject_plugin_contract_mutation();
INSERT INTO platform.enterprise_plugin_catalog(plugin_key,version,name,description,runner)
 VALUES('knowledge-qa','1.0.0','文件问答','分析本次明确附带的文本资料，使用你自己的可用模型。','knowledge-qa-v1')
 ON CONFLICT DO NOTHING;
-- No employee receives access by logging in; an administrator grants a plugin.

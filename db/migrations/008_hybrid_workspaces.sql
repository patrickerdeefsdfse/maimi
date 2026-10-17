-- Department workspaces are provisioned by administrators, never on login.
CREATE TABLE IF NOT EXISTS platform.hybrid_workspaces (
    id UUID PRIMARY KEY,
    organization_code VARCHAR(100) NOT NULL,
    name VARCHAR(200) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    default_model VARCHAR(200) NOT NULL DEFAULT 'deepseek-flash-v4.1',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS platform.hybrid_workspace_grants (
    workspace_id UUID NOT NULL REFERENCES platform.hybrid_workspaces(id),
    user_no VARCHAR(100) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    can_chat BOOLEAN NOT NULL DEFAULT TRUE,
    can_share BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (workspace_id, user_no)
);
CREATE TABLE IF NOT EXISTS platform.hybrid_plugins (
    workspace_id UUID NOT NULL REFERENCES platform.hybrid_workspaces(id),
    plugin_key VARCHAR(160) NOT NULL,
    version VARCHAR(80) NOT NULL,
    name VARCHAR(200) NOT NULL,
    runner VARCHAR(100) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    model_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (workspace_id, plugin_key, version)
);
CREATE TABLE IF NOT EXISTS platform.hybrid_plugin_grants (
    workspace_id UUID NOT NULL,
    plugin_key VARCHAR(160) NOT NULL,
    version VARCHAR(80) NOT NULL,
    user_no VARCHAR(100) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (workspace_id, plugin_key, version, user_no),
    FOREIGN KEY (workspace_id, plugin_key, version)
      REFERENCES platform.hybrid_plugins(workspace_id, plugin_key, version)
);
CREATE TABLE IF NOT EXISTS platform.hybrid_attachments (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES platform.hybrid_workspaces(id),
    owner_user_no VARCHAR(100) NOT NULL,
    request_id UUID NOT NULL,
    operation VARCHAR(16) NOT NULL CHECK (operation IN ('chat','plugin')),
    plugin_key VARCHAR(160),
    plugin_version VARCHAR(80),
    name VARCHAR(200) NOT NULL,
    sha256 CHAR(64) NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes BETWEEN 0 AND 262144),
    content BYTEA NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((operation = 'chat' AND plugin_key IS NULL AND plugin_version IS NULL)
        OR (operation = 'plugin' AND plugin_key IS NOT NULL AND plugin_version IS NOT NULL)),
    UNIQUE (workspace_id, owner_user_no, request_id, name, sha256),
    UNIQUE (id, workspace_id, owner_user_no)
);
CREATE TABLE IF NOT EXISTS platform.hybrid_conversations (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES platform.hybrid_workspaces(id),
    owner_user_no VARCHAR(100) NOT NULL,
    operation VARCHAR(16) NOT NULL CHECK (operation IN ('chat','plugin')),
    plugin_key VARCHAR(160),
    plugin_version VARCHAR(80),
    title VARCHAR(200) NOT NULL,
    visibility VARCHAR(16) NOT NULL DEFAULT 'private' CHECK (visibility IN ('private','workspace')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((operation = 'chat' AND plugin_key IS NULL AND plugin_version IS NULL)
        OR (operation = 'plugin' AND plugin_key IS NOT NULL AND plugin_version IS NOT NULL)),
    UNIQUE (id, workspace_id, owner_user_no)
);
CREATE TABLE IF NOT EXISTS platform.hybrid_runs (
    id UUID PRIMARY KEY,
    conversation_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    owner_user_no VARCHAR(100) NOT NULL,
    request_id UUID NOT NULL,
    request_hash CHAR(64) NOT NULL,
    input_text TEXT NOT NULL,
    attachment_ids UUID[] NOT NULL DEFAULT '{}',
    status VARCHAR(16) NOT NULL CHECK (status IN ('running','completed','failed')),
    simulation BOOLEAN NOT NULL DEFAULT FALSE,
    effective_model VARCHAR(200),
    result JSONB,
    error_code VARCHAR(100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    UNIQUE (workspace_id, owner_user_no, request_id),
    FOREIGN KEY (conversation_id, workspace_id, owner_user_no)
      REFERENCES platform.hybrid_conversations(id, workspace_id, owner_user_no)
);
CREATE INDEX IF NOT EXISTS hybrid_conversations_visible
 ON platform.hybrid_conversations(workspace_id, owner_user_no, updated_at DESC);
CREATE INDEX IF NOT EXISTS hybrid_runs_conversation
 ON platform.hybrid_runs(conversation_id, created_at);
CREATE TABLE IF NOT EXISTS platform.hybrid_audit (
    id BIGSERIAL PRIMARY KEY,
    workspace_id UUID NOT NULL,
    actor_user_no VARCHAR(100) NOT NULL,
    action VARCHAR(100) NOT NULL,
    resource_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

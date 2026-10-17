CREATE TABLE IF NOT EXISTS platform.projects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES platform.organizations(id) ON DELETE CASCADE,
    project_key VARCHAR(160) NOT NULL,
    display_name VARCHAR(300) NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    default_provider_key VARCHAR(100),
    default_model_key VARCHAR(200),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (organization_id, project_key)
);

CREATE TABLE IF NOT EXISTS platform.project_members (
    project_id UUID NOT NULL REFERENCES platform.projects(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES platform.users(id) ON DELETE CASCADE,
    role_key VARCHAR(100) NOT NULL DEFAULT 'member',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, user_id)
);

CREATE TABLE IF NOT EXISTS platform.project_plugins (
    project_id UUID NOT NULL REFERENCES platform.projects(id) ON DELETE CASCADE,
    plugin_id UUID NOT NULL REFERENCES platform.plugins(id) ON DELETE CASCADE,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (project_id, plugin_id)
);

CREATE TABLE IF NOT EXISTS platform.project_knowledge_bases (
    project_id UUID NOT NULL REFERENCES platform.projects(id) ON DELETE CASCADE,
    document_id UUID NOT NULL REFERENCES platform.documents(id) ON DELETE CASCADE,
    PRIMARY KEY (project_id, document_id)
);

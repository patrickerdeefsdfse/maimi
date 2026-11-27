CREATE TABLE IF NOT EXISTS platform.roles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES platform.organizations(id) ON DELETE CASCADE,
    role_key VARCHAR(100) NOT NULL,
    display_name VARCHAR(200) NOT NULL,
    UNIQUE (organization_id, role_key)
);

CREATE TABLE IF NOT EXISTS platform.permissions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    permission_key VARCHAR(160) NOT NULL UNIQUE,
    display_name VARCHAR(200) NOT NULL
);

CREATE TABLE IF NOT EXISTS platform.user_roles (
    user_id UUID NOT NULL REFERENCES platform.users(id) ON DELETE CASCADE,
    role_id UUID NOT NULL REFERENCES platform.roles(id) ON DELETE CASCADE,
    organization_id UUID NOT NULL REFERENCES platform.organizations(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, role_id, organization_id)
);

CREATE TABLE IF NOT EXISTS platform.role_permissions (
    role_id UUID NOT NULL REFERENCES platform.roles(id) ON DELETE CASCADE,
    permission_id UUID NOT NULL REFERENCES platform.permissions(id) ON DELETE CASCADE,
    PRIMARY KEY (role_id, permission_id)
);

CREATE TABLE IF NOT EXISTS platform.model_catalog (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_key VARCHAR(100) NOT NULL,
    model_key VARCHAR(200) NOT NULL,
    display_name VARCHAR(200) NOT NULL,
    capabilities JSONB NOT NULL DEFAULT '{}'::jsonb,
    secret_ref VARCHAR(200) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (provider_key, model_key)
);

CREATE TABLE IF NOT EXISTS platform.agent_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID REFERENCES platform.organizations(id),
    user_id UUID NOT NULL REFERENCES platform.users(id),
    conversation_id UUID NOT NULL REFERENCES platform.conversations(id) ON DELETE CASCADE,
    idempotency_key VARCHAR(200) NOT NULL,
    provider_key VARCHAR(100) NOT NULL,
    model_key VARCHAR(200) NOT NULL,
    status VARCHAR(32) NOT NULL,
    error_code VARCHAR(100),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (organization_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS platform.capability_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES platform.organizations(id),
    user_id UUID NOT NULL REFERENCES platform.users(id),
    plugin_key VARCHAR(160) NOT NULL,
    capability VARCHAR(160) NOT NULL,
    idempotency_key VARCHAR(200) NOT NULL,
    input_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(32) NOT NULL,
    stage VARCHAR(64) NOT NULL DEFAULT 'queued',
    progress SMALLINT NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    locked_by VARCHAR(160),
    locked_at TIMESTAMPTZ,
    heartbeat_at TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (organization_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS platform.outbox_events (
    id BIGSERIAL PRIMARY KEY,
    organization_id UUID REFERENCES platform.organizations(id),
    event_type VARCHAR(160) NOT NULL,
    aggregate_type VARCHAR(100) NOT NULL,
    aggregate_id UUID NOT NULL,
    payload JSONB NOT NULL,
    published_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS platform.documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES platform.organizations(id),
    owner_user_id UUID NOT NULL REFERENCES platform.users(id),
    title VARCHAR(500) NOT NULL,
    content_hash CHAR(64) NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    index_status VARCHAR(32) NOT NULL DEFAULT 'pending',
    embedding_model VARCHAR(200),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (organization_id, content_hash, version)
);

CREATE TABLE IF NOT EXISTS platform.artifacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES platform.organizations(id),
    owner_user_id UUID NOT NULL REFERENCES platform.users(id),
    storage_ref TEXT NOT NULL,
    content_hash CHAR(64) NOT NULL,
    mime_type VARCHAR(200),
    size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS platform.idempotency_keys (
    organization_id UUID NOT NULL REFERENCES platform.organizations(id) ON DELETE CASCADE,
    key_value VARCHAR(200) NOT NULL,
    request_hash CHAR(64) NOT NULL,
    response JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ,
    PRIMARY KEY (organization_id, key_value)
);

INSERT INTO platform.model_catalog
    (provider_key, model_key, display_name, capabilities, secret_ref)
VALUES
    ('deepseek', 'agent-default', 'Company default model route', '{"chat":true,"stream":true,"tools":true}'::jsonb, 'secret://model/deepseek'),
    ('deepseek', 'caoxi-default', 'Caoxi model route', '{"chat":true,"stream":true,"tools":true}'::jsonb, 'secret://model/deepseek'),
    ('deepseek', 'liuqinghuai-default', 'Liuqinghuai model route', '{"chat":true,"stream":true,"tools":true}'::jsonb, 'secret://model/deepseek'),
    ('deepseek', 'liuqinghuai-ocr', 'OCR model route', '{"chat":true,"stream":true}'::jsonb, 'secret://model/deepseek')
ON CONFLICT (provider_key, model_key) DO NOTHING;

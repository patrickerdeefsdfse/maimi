CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS platform;

CREATE TABLE IF NOT EXISTS platform.organizations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code VARCHAR(100) NOT NULL UNIQUE,
    name VARCHAR(200) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS platform.users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    external_user_no VARCHAR(100) NOT NULL UNIQUE,
    display_name VARCHAR(200) NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS platform.model_providers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_key VARCHAR(100) NOT NULL UNIQUE,
    display_name VARCHAR(200) NOT NULL,
    base_url TEXT NOT NULL,
    default_model VARCHAR(200) NOT NULL,
    secret_ref VARCHAR(200) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS platform.conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID REFERENCES platform.organizations(id),
    user_id UUID NOT NULL REFERENCES platform.users(id),
    provider_key VARCHAR(100) NOT NULL,
    model VARCHAR(200) NOT NULL,
    title VARCHAR(500) NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS platform.messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES platform.conversations(id) ON DELETE CASCADE,
    role VARCHAR(32) NOT NULL,
    content JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS platform.plugins (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    plugin_key VARCHAR(160) NOT NULL UNIQUE,
    display_name VARCHAR(200) NOT NULL,
    manifest JSONB NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS platform.audit_events (
    id BIGSERIAL PRIMARY KEY,
    organization_id UUID REFERENCES platform.organizations(id),
    user_id UUID REFERENCES platform.users(id),
    event_type VARCHAR(160) NOT NULL,
    resource_type VARCHAR(100) NOT NULL,
    resource_id UUID,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO platform.model_providers
    (provider_key, display_name, base_url, default_model, secret_ref)
VALUES
    ('deepseek', 'DeepSeek via platform routes', 'http://ai-model-service:8094/v1/model-gateway', 'agent-default', 'secret://model/deepseek')
ON CONFLICT (provider_key) DO NOTHING;

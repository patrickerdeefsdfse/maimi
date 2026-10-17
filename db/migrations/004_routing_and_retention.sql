-- Conversation routing and retention policy metadata. Cleanup workers execute
-- deletion only after checking the effective policy and writing an audit event.
ALTER TABLE platform.conversations
    ADD COLUMN IF NOT EXISTS route_mode VARCHAR(32) NOT NULL DEFAULT 'auto',
    ADD COLUMN IF NOT EXISTS project_id UUID REFERENCES platform.projects(id),
    ADD COLUMN IF NOT EXISTS plugin_key VARCHAR(160),
    ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;

ALTER TABLE platform.conversations
    DROP CONSTRAINT IF EXISTS conversations_route_mode_check;
ALTER TABLE platform.conversations
    ADD CONSTRAINT conversations_route_mode_check
    CHECK (route_mode IN ('auto', 'dsh', 'enterprise'));

CREATE TABLE IF NOT EXISTS platform.conversation_retention_policies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID REFERENCES platform.organizations(id) ON DELETE CASCADE,
    project_id UUID REFERENCES platform.projects(id) ON DELETE CASCADE,
    policy_key VARCHAR(160) NOT NULL,
    retention_days INTEGER NOT NULL CHECK (retention_days BETWEEN 1 AND 3650),
    max_messages INTEGER CHECK (max_messages IS NULL OR max_messages BETWEEN 1 AND 1000000),
    cleanup_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    legal_hold BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (organization_id, project_id, policy_key)
);

CREATE TABLE IF NOT EXISTS platform.conversation_retention_overrides (
    conversation_id UUID PRIMARY KEY REFERENCES platform.conversations(id) ON DELETE CASCADE,
    retention_days INTEGER CHECK (retention_days IS NULL OR retention_days BETWEEN 1 AND 3650),
    cleanup_enabled BOOLEAN,
    legal_hold BOOLEAN NOT NULL DEFAULT FALSE,
    expires_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS conversations_cleanup_idx
    ON platform.conversations (expires_at)
    WHERE archived_at IS NULL;

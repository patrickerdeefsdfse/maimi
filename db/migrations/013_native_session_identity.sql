-- DSH0.1.6 generates session-UUID, and also accepts an explicit UUID.
-- Preserve the exact native identity; never strip a prefix and merge sessions.
ALTER TABLE platform.enterprise_native_bindings
    ALTER COLUMN native_session_id TYPE VARCHAR(80) USING native_session_id::text;
ALTER TABLE platform.enterprise_native_bindings
    ADD CONSTRAINT enterprise_native_session_identity_format
    CHECK (native_session_id ~ '^(session-)?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$');

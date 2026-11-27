-- The gateway changes publication state, while published content remains byte-identical.
REVOKE UPDATE ON platform.plugin_releases FROM maimi_app;
GRANT UPDATE (state, updated_at) ON platform.plugin_releases TO maimi_app;

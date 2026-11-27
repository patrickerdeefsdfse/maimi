-- Retire only the removed employee-card capability. Old issued files keep their
-- existing lifecycle, and historical native bindings may keep their FK target.
DELETE FROM platform.enterprise_plugin_grants WHERE plugin_key='employee-cards';
UPDATE platform.enterprise_plugin_catalog SET enabled=FALSE WHERE plugin_key='employee-cards';
DELETE FROM platform.enterprise_plugin_catalog p WHERE p.plugin_key='employee-cards'
 AND NOT EXISTS (SELECT 1 FROM platform.enterprise_native_bindings b
                 WHERE b.plugin_key=p.plugin_key AND b.plugin_version=p.version);

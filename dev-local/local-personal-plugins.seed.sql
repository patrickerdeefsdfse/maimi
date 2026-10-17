INSERT INTO platform.enterprise_plugin_catalog(plugin_key,version,name,description,runner,model_policy)
VALUES('synthetic-summary','1.0.0','本地测试文件摘要','仅用于本地合成数据验证','synthetic-summary-v1','{"mode":"deterministic-no-model"}') ON CONFLICT DO NOTHING;
INSERT INTO platform.enterprise_plugin_grants(plugin_key,version,user_no)
VALUES('synthetic-summary','1.0.0','LOCAL-FIXTURE-A'),('synthetic-summary','1.0.0','LOCAL-FIXTURE-B') ON CONFLICT DO NOTHING;

-- Route all model calls through the internal TokenKey-compatible service.
-- The TokenKey credential stays outside PostgreSQL in a server secret file.

INSERT INTO platform.model_providers
    (provider_key, display_name, base_url, default_model, secret_ref)
VALUES
    ('tokenkey', '公司 TokenKey 模型服务', 'https://tokenkey.megmeet.com/v1',
     'deepseek-flash-v4.1', 'secret://model/tokenkey')
ON CONFLICT (provider_key) DO UPDATE
SET display_name = EXCLUDED.display_name,
    base_url = EXCLUDED.base_url,
    default_model = EXCLUDED.default_model,
    secret_ref = EXCLUDED.secret_ref,
    updated_at = now();

INSERT INTO platform.model_catalog
    (provider_key, model_key, display_name, capabilities, secret_ref)
VALUES
    ('tokenkey', 'deepseek-flash-v4.1', 'DeepSeek V4.1 Flash（TokenKey）',
     '{"chat":true,"stream":true,"tools":true}'::jsonb,
     'secret://model/tokenkey')
ON CONFLICT (provider_key, model_key) DO UPDATE
SET display_name = EXCLUDED.display_name,
    capabilities = EXCLUDED.capabilities,
    secret_ref = EXCLUDED.secret_ref,
    enabled = TRUE,
    updated_at = now();


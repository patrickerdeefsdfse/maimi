-- The new system uses the internal TokenKey-compatible service as its only
-- enabled model provider. Keep legacy rows for audit/history, but prevent
-- direct provider credentials or stale aliases from being selected.
UPDATE platform.model_providers
   SET enabled = FALSE,
       updated_at = now()
 WHERE provider_key <> 'tokenkey';

UPDATE platform.model_catalog
   SET enabled = FALSE,
       updated_at = now()
 WHERE provider_key <> 'tokenkey';


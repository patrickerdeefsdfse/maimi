CREATE TABLE IF NOT EXISTS maimi_im.model_consent (
    generation uuid PRIMARY KEY,
    employee text NOT NULL CHECK (employee ~ '^M[0-9]{8}$'),
    lease_ciphertext bytea NOT NULL,
    expires_at bigint NOT NULL
);
CREATE INDEX IF NOT EXISTS im_model_consent_expiry ON maimi_im.model_consent(expires_at);


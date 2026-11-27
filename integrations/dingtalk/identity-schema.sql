CREATE SCHEMA IF NOT EXISTS maimi_im;

CREATE TABLE IF NOT EXISTS maimi_im.bind_attempt (
    ticket text PRIMARY KEY,
    employee text NOT NULL CHECK (employee ~ '^M[0-9]{8}$'),
    account text NOT NULL CHECK (length(account)=64),
    claim_hash text UNIQUE NOT NULL CHECK (length(claim_hash)=64),
    peer_hash text CHECK (length(peer_hash)=64),
    proof_hash text CHECK (length(proof_hash)=64),
    expires_at bigint NOT NULL
);
CREATE TABLE IF NOT EXISTS maimi_im.binding (
    account text NOT NULL,
    peer_hash text NOT NULL,
    employee text NOT NULL CHECK (employee ~ '^M[0-9]{8}$'),
    generation uuid NOT NULL,
    valid_since bigint NOT NULL,
    valid_until bigint NOT NULL,
    PRIMARY KEY (account, peer_hash),
    UNIQUE (account, employee),
    CHECK (valid_until>valid_since)
);
CREATE TABLE IF NOT EXISTS maimi_im.receipt (
    receipt_key text PRIMARY KEY CHECK(length(receipt_key)=64),
    generation uuid NOT NULL,
    reserved_at bigint NOT NULL,
    state text NOT NULL CHECK (state IN ('reserved','finished','uncertain'))
);
CREATE INDEX IF NOT EXISTS im_attempt_expiry ON maimi_im.bind_attempt(expires_at);
CREATE INDEX IF NOT EXISTS im_receipt_retention ON maimi_im.receipt(reserved_at);

CREATE TABLE IF NOT EXISTS maimi_im.question_job (
    job_id uuid PRIMARY KEY, receipt_key text UNIQUE NOT NULL, generation uuid NOT NULL,
    employee text NOT NULL, conversation_key text NOT NULL, state text NOT NULL
        CHECK (state IN ('queued','running','finished','uncertain','expired')),
    created_at bigint NOT NULL, deadline bigint NOT NULL, retain_until bigint NOT NULL,
    started_at bigint, question bytea, answer bytea
);
CREATE INDEX IF NOT EXISTS im_job_claim ON maimi_im.question_job(created_at) WHERE state='queued';
CREATE INDEX IF NOT EXISTS im_job_owner ON maimi_im.question_job(generation,conversation_key,created_at);
CREATE INDEX IF NOT EXISTS im_job_retention ON maimi_im.question_job(retain_until);


-- Additive and safe for the previous gateway during the rolling upgrade.
ALTER TABLE platform.enterprise_plugin_grants
    ADD COLUMN IF NOT EXISTS decision_at timestamptz,
    ADD COLUMN IF NOT EXISTS changed_at timestamptz;

CREATE TABLE IF NOT EXISTS platform.plugin_batch_jobs (
    id uuid PRIMARY KEY,
    actor varchar(100) NOT NULL,
    request_id uuid NOT NULL,
    fingerprint char(64) NOT NULL,
    parent_id uuid REFERENCES platform.plugin_batch_jobs(id),
    enabled boolean NOT NULL,
    total integer NOT NULL CHECK (total BETWEEN 1 AND 5000),
    pending integer NOT NULL CHECK (pending >= 0),
    changed integer NOT NULL DEFAULT 0 CHECK (changed >= 0),
    unchanged integer NOT NULL DEFAULT 0 CHECK (unchanged >= 0),
    failed integer NOT NULL DEFAULT 0 CHECK (failed >= 0),
    intent_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (total = pending + changed + unchanged + failed),
    UNIQUE (actor,request_id)
);
CREATE INDEX IF NOT EXISTS plugin_batch_jobs_owner ON platform.plugin_batch_jobs(actor,created_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS plugin_batch_jobs_ready ON platform.plugin_batch_jobs(created_at,id) WHERE pending > 0;
CREATE UNIQUE INDEX IF NOT EXISTS plugin_batch_jobs_retry ON platform.plugin_batch_jobs(parent_id) WHERE parent_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS platform.plugin_batch_items (
    job_id uuid NOT NULL REFERENCES platform.plugin_batch_jobs(id),
    employee varchar(100) NOT NULL,
    plugin varchar(160) NOT NULL,
    version varchar(80) NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','changed','unchanged','failed')),
    error_code text,
    finished_at timestamptz,
    PRIMARY KEY (job_id,employee,plugin,version),
    CHECK ((status = 'failed') = (error_code IS NOT NULL)),
    CHECK ((status = 'pending') = (finished_at IS NULL))
);
CREATE INDEX IF NOT EXISTS plugin_batch_items_ready ON platform.plugin_batch_items(job_id,employee,plugin,version) WHERE status='pending';
CREATE TABLE IF NOT EXISTS platform.plugin_device_reports (
    employee varchar(100) NOT NULL,
    client text NOT NULL CHECK(client IN ('desktop','web')),
    device_id uuid NOT NULL,
    plugin_key varchar(160) NOT NULL,
    version varchar(80) NOT NULL,
    status text NOT NULL CHECK(status IN ('installed','removed','failed')),
    usable boolean,
    reported_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(employee,client,plugin_key,version,device_id),
    FOREIGN KEY(plugin_key,version) REFERENCES platform.plugin_releases(plugin_key,version),
    CHECK (usable IS DISTINCT FROM true OR status='installed')
);
CREATE INDEX IF NOT EXISTS plugin_device_reports_recent ON platform.plugin_device_reports(employee,plugin_key,version,client,reported_at DESC);
GRANT SELECT, INSERT, UPDATE ON platform.plugin_batch_jobs,platform.plugin_batch_items,platform.plugin_device_reports TO maimi_app;

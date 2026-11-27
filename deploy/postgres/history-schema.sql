-- Additive monitoring schema. No application tables are altered.
-- Apply as database owner; grant only this schema's DML to the existing gateway role.
BEGIN;
SET LOCAL lock_timeout='3s';
CREATE SCHEMA IF NOT EXISTS maimi_monitor;
CREATE TABLE IF NOT EXISTS maimi_monitor.series (
  step integer NOT NULL CHECK(step IN (60,3600,86400)),
  bucket bigint NOT NULL, metric text NOT NULL, scope text NOT NULL,
  samples integer NOT NULL CHECK(samples>0), total double precision NOT NULL,
  low double precision NOT NULL, high double precision NOT NULL,
  PRIMARY KEY(step,bucket,metric,scope)
);
CREATE TABLE IF NOT EXISTS maimi_monitor.receipts (
  id uuid PRIMARY KEY, recorded_at bigint NOT NULL
);
CREATE INDEX IF NOT EXISTS receipts_recorded ON maimi_monitor.receipts(recorded_at);
CREATE TABLE IF NOT EXISTS maimi_monitor.user_days (
  day bigint NOT NULL, owner_key varchar(32) NOT NULL,
  source text NOT NULL CHECK(source IN ('web','desktop','combined')),
  kind text NOT NULL CHECK(kind IN ('activity','model','task','office')),
  requests bigint NOT NULL DEFAULT 0, failed bigint NOT NULL DEFAULT 0,
  unknown bigint NOT NULL DEFAULT 0, duration_ms bigint NOT NULL DEFAULT 0,
  input_tokens bigint NOT NULL DEFAULT 0, output_tokens bigint NOT NULL DEFAULT 0,
  token_samples bigint NOT NULL DEFAULT 0, last_seen bigint NOT NULL,
  PRIMARY KEY(day,owner_key,source,kind)
);
CREATE TABLE IF NOT EXISTS maimi_monitor.day_totals (
  day bigint PRIMARY KEY, dau integer NOT NULL, wau integer NOT NULL,
  model_requests bigint NOT NULL, failed bigint NOT NULL,
  input_tokens bigint NOT NULL, output_tokens bigint NOT NULL,
  token_samples bigint NOT NULL, updated_at bigint NOT NULL
);
CREATE TABLE IF NOT EXISTS maimi_monitor.devices (
  owner_key varchar(32) NOT NULL, device_key varchar(32) NOT NULL,
  version varchar(64) NOT NULL, last_seen bigint NOT NULL,
  PRIMARY KEY(owner_key,device_key)
);
CREATE TABLE IF NOT EXISTS maimi_monitor.state (
  name text PRIMARY KEY, value bigint NOT NULL
);
INSERT INTO maimi_monitor.state(name,value) VALUES ('schema_version',101),('started_at',extract(epoch from clock_timestamp())::bigint)
ON CONFLICT(name) DO NOTHING;
COMMIT;

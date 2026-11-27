-- Authoritative rc.3 session event store. Existing JSONL/projection data is not imported implicitly.
CREATE TABLE platform.native_sessions (
 owner_key varchar(32) NOT NULL CHECK(owner_key ~ '^[a-f0-9]{32}$'),
 session_id varchar(200) NOT NULL,
 header jsonb,
 inherited_event_count bigint NOT NULL DEFAULT 0 CHECK(inherited_event_count >= 0),
 event_count bigint NOT NULL DEFAULT 0 CHECK(event_count >= 0),
 title varchar(500) NOT NULL DEFAULT '',
 revision bigint NOT NULL DEFAULT 1,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 last_activity_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 writer_id uuid,
 writer_generation bigint,
 writer_until timestamptz,
 purged_at timestamptz,
 PRIMARY KEY(owner_key,session_id),
 CHECK((purged_at IS NULL AND header IS NOT NULL) OR (purged_at IS NOT NULL AND header IS NULL))
);
CREATE TABLE platform.native_session_events (
 owner_key varchar(32) NOT NULL,
 session_id varchar(200) NOT NULL,
 seq bigint NOT NULL CHECK(seq>=0),
 event jsonb NOT NULL,
 PRIMARY KEY(owner_key,session_id,seq),
 FOREIGN KEY(owner_key,session_id) REFERENCES platform.native_sessions(owner_key,session_id)
);
CREATE INDEX native_session_retention ON platform.native_sessions(last_activity_at) WHERE purged_at IS NULL;
COMMENT ON TABLE platform.native_sessions IS 'Web-only authoritative history. Purged IDs are tombstones, never reusable. Desktop history stays local.';

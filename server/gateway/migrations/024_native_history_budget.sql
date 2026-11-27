-- Logical UTF-8 JSON bytes, distinct from PG physical/replication/backup usage.
ALTER TABLE platform.native_sessions ADD COLUMN logical_bytes bigint NOT NULL DEFAULT 0 CHECK(logical_bytes>=0);
UPDATE platform.native_sessions s SET logical_bytes=COALESCE(octet_length(s.header::text),0)+
 (SELECT COALESCE(sum(octet_length(e.event::text)),0) FROM platform.native_session_events e WHERE e.owner_key=s.owner_key AND e.session_id=s.session_id);
CREATE TABLE platform.native_history_accounts (
 owner_key varchar(32) PRIMARY KEY CHECK(owner_key ~ '^[a-f0-9]{32}$'),
 logical_bytes bigint NOT NULL DEFAULT 0 CHECK(logical_bytes>=0)
);
INSERT INTO platform.native_history_accounts SELECT owner_key,sum(logical_bytes) FROM platform.native_sessions GROUP BY owner_key;

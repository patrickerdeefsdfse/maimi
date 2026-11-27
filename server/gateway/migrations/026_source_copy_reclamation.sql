-- Exact source-copy receipts; files are never acknowledged merely because a
-- cleanup request was sent. Historical worker release proof belongs to 025.
ALTER TABLE platform.storage_source_copies ADD COLUMN source_id UUID NOT NULL DEFAULT gen_random_uuid();
ALTER TABLE platform.storage_source_copies ADD CONSTRAINT storage_source_copies_source_id UNIQUE(source_id);
ALTER TABLE platform.storage_source_copies DROP CONSTRAINT storage_source_copies_state_check;
ALTER TABLE platform.storage_source_copies ADD CONSTRAINT storage_source_copies_state_check
 CHECK(state IN ('temporary_retained','cleanup_required','manual_review','removed_confirmed'));
ALTER TABLE platform.storage_source_copies ADD COLUMN cleanup_operation UUID;
ALTER TABLE platform.storage_source_copies ADD COLUMN cleanup_lease_until TIMESTAMPTZ;
ALTER TABLE platform.storage_source_copies ADD COLUMN next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp();
ALTER TABLE platform.storage_source_copies ADD COLUMN cleanup_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE platform.storage_source_copies ADD COLUMN cleanup_last_error VARCHAR(64);
ALTER TABLE platform.storage_source_copies ADD COLUMN cleanup_receipt JSONB;
ALTER TABLE platform.storage_source_copies ADD COLUMN removed_at TIMESTAMPTZ;
CREATE INDEX storage_source_copies_pending ON platform.storage_source_copies(next_attempt_at)
 WHERE state='cleanup_required';

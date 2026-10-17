-- Apply only after exact predecessor/schema checks, inside one transaction.
-- The migration controller owns locking, migration ledger and validation.
-- No existing artifact, encrypted payload, byte count or business row is changed.
ALTER TABLE platform.project_artifacts
    ADD COLUMN output_kind VARCHAR(32) NOT NULL DEFAULT 'ordinary';

ALTER TABLE platform.project_artifacts
    ADD CONSTRAINT project_artifacts_file_limit_v2 CHECK (
        file_bytes >= 0 AND (
            (output_kind = 'ordinary' AND file_bytes <= 33554432)
            OR
            (output_kind = 'card-print-v1' AND source = 'project_output'
                AND file_bytes <= 134217728)
        )
    );

ALTER TABLE platform.project_artifacts
    DROP CONSTRAINT project_artifacts_file_bytes_check;

-- A released empty-database cutover left discovery referencing the frozen copy.
-- Validate its replacement before removing that obsolete UUID-existence check.
DO $repair$
DECLARE
    capture_column smallint;
    constraint_row record;
BEGIN
    IF (SELECT layout FROM storage_layout WHERE singleton)='partitioned' THEN
        SELECT attnum INTO STRICT capture_column FROM pg_attribute
            WHERE attrelid='discovery_snapshot'::regclass AND attname='capture_id';
        IF NOT EXISTS (SELECT 1 FROM pg_constraint
            WHERE conrelid='discovery_snapshot'::regclass AND contype='f'
              AND confrelid='capture_identity'::regclass AND conkey=ARRAY[capture_column]) THEN
            ALTER TABLE discovery_snapshot ADD CONSTRAINT discovery_snapshot_capture_identity_fk
                FOREIGN KEY(capture_id) REFERENCES capture_identity(capture_id);
        END IF;
        FOR constraint_row IN SELECT conname FROM pg_constraint
            WHERE conrelid='discovery_snapshot'::regclass AND contype='f'
              AND confrelid='capture_identity'::regclass AND conkey=ARRAY[capture_column]
              AND NOT convalidated
        LOOP
            EXECUTE format('ALTER TABLE discovery_snapshot VALIDATE CONSTRAINT %I', constraint_row.conname);
        END LOOP;
        FOR constraint_row IN SELECT conname FROM pg_constraint
            WHERE conrelid='discovery_snapshot'::regclass AND contype='f'
              AND confrelid=to_regclass('capture_legacy_004') AND conkey=ARRAY[capture_column]
        LOOP
            EXECUTE format('ALTER TABLE discovery_snapshot DROP CONSTRAINT %I', constraint_row.conname);
        END LOOP;
    END IF;
END
$repair$;
INSERT INTO schema_migration(version) VALUES (9) ON CONFLICT DO NOTHING;

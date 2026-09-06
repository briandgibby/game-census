-- Discovery follows P2 storage. Version 3 in the original main release owned
-- these tables; idempotent version 6 also upgrades the separate P2 layout.
ALTER TABLE request_attempt ALTER COLUMN app_id DROP NOT NULL;
ALTER TABLE capture ALTER COLUMN app_id DROP NOT NULL;
DO $nullable$
BEGIN
    IF (SELECT layout FROM storage_layout WHERE singleton)='legacy' THEN
        ALTER TABLE capture_partitioned ALTER COLUMN app_id DROP NOT NULL;
    END IF;
END
$nullable$;
CREATE TABLE IF NOT EXISTS discovery_snapshot (
    capture_id uuid PRIMARY KEY,
    source text NOT NULL, observed_at timestamptz NOT NULL,
    parameters jsonb NOT NULL, value jsonb NOT NULL, parser_version text NOT NULL
);
DO $reference$
BEGIN
    IF NOT EXISTS(SELECT 1 FROM pg_constraint
                  WHERE conrelid='discovery_snapshot'::regclass AND contype='f') THEN
        IF (SELECT layout FROM storage_layout WHERE singleton)='partitioned' THEN
            ALTER TABLE discovery_snapshot ADD CONSTRAINT discovery_snapshot_capture_identity_fk
                FOREIGN KEY(capture_id) REFERENCES capture_identity(capture_id);
        ELSE
            ALTER TABLE discovery_snapshot ADD CONSTRAINT discovery_snapshot_capture_fk
                FOREIGN KEY(capture_id) REFERENCES capture(capture_id);
        END IF;
    END IF;
END
$reference$;
CREATE INDEX IF NOT EXISTS discovery_snapshot_source_time ON discovery_snapshot(source, observed_at DESC);
CREATE TABLE IF NOT EXISTS catalog_entry (
    capture_id uuid NOT NULL REFERENCES discovery_snapshot(capture_id),
    app_id bigint NOT NULL CHECK(app_id BETWEEN 1 AND 4294967295), name text NOT NULL,
    PRIMARY KEY(capture_id, app_id)
);
CREATE INDEX IF NOT EXISTS catalog_entry_app ON catalog_entry(app_id);
INSERT INTO schema_migration(version) VALUES (6) ON CONFLICT DO NOTHING;

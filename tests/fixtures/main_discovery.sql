ALTER TABLE request_attempt ALTER COLUMN app_id DROP NOT NULL;
ALTER TABLE capture ALTER COLUMN app_id DROP NOT NULL;
CREATE TABLE IF NOT EXISTS discovery_snapshot (
    capture_id uuid PRIMARY KEY REFERENCES capture(capture_id),
    source text NOT NULL, observed_at timestamptz NOT NULL,
    parameters jsonb NOT NULL, value jsonb NOT NULL, parser_version text NOT NULL
);
CREATE INDEX IF NOT EXISTS discovery_snapshot_source_time ON discovery_snapshot(source, observed_at DESC);
CREATE TABLE IF NOT EXISTS catalog_entry (
    capture_id uuid NOT NULL REFERENCES discovery_snapshot(capture_id),
    app_id bigint NOT NULL CHECK(app_id BETWEEN 1 AND 4294967295), name text NOT NULL,
    PRIMARY KEY(capture_id, app_id)
);
CREATE INDEX IF NOT EXISTS catalog_entry_app ON catalog_entry(app_id);
INSERT INTO schema_migration(version) VALUES (3) ON CONFLICT DO NOTHING;

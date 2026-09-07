-- These projections are rebuildable from capture parameters and response bytes.
-- Existing generic discovery_snapshot values remain the enrichment projection.
CREATE TABLE IF NOT EXISTS source_policy (
    policy_hash text PRIMARY KEY CHECK(length(policy_hash)=64),
    source text NOT NULL,
    parameters jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS catalog_checkpoint (
    capture_id uuid PRIMARY KEY REFERENCES discovery_snapshot(capture_id),
    scan_id uuid NOT NULL,
    started_at timestamptz NOT NULL,
    mode text NOT NULL CHECK(mode IN ('full','incremental')),
    if_modified_since bigint NOT NULL CHECK(if_modified_since BETWEEN 0 AND 4294967295),
    last_appid bigint NOT NULL CHECK(last_appid BETWEEN 0 AND 4294967295),
    complete boolean NOT NULL,
    policy_hash text NOT NULL REFERENCES source_policy(policy_hash)
);
CREATE INDEX IF NOT EXISTS catalog_checkpoint_scan ON catalog_checkpoint(scan_id);
CREATE INDEX IF NOT EXISTS catalog_checkpoint_complete ON catalog_checkpoint(started_at DESC) WHERE complete;
INSERT INTO schema_migration(version) VALUES (7) ON CONFLICT DO NOTHING;

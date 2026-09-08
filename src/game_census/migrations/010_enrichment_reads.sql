-- Bounded per-app source history and same-query predecessor lookups.
-- Projections are still owned by the versioned capture replay command.
CREATE INDEX IF NOT EXISTS enrichment_history_app ON discovery_snapshot
    (source,(value->>'app_id'),observed_at DESC,capture_id DESC);
CREATE INDEX IF NOT EXISTS enrichment_history_query ON discovery_snapshot
    (source,(value->>'app_id'),(value->>'query_hash'),observed_at DESC,capture_id DESC);
INSERT INTO schema_migration(version) VALUES(10) ON CONFLICT DO NOTHING;

-- Canonical adoption and tracking-end events; acquired observations stay intact.
CREATE TABLE IF NOT EXISTS cohort_event (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    previous_id bigint REFERENCES cohort_event(id),
    recorded_at timestamptz NOT NULL,
    policy_hash text NOT NULL CHECK(length(policy_hash)=64),
    policy jsonb NOT NULL,
    members jsonb NOT NULL CHECK(jsonb_array_length(members) BETWEEN 1 AND 25),
    exploration_cursor bigint NOT NULL CHECK(exploration_cursor BETWEEN 0 AND 4294967295),
    basis jsonb NOT NULL,
    changes jsonb NOT NULL,
    event_hash text NOT NULL CHECK(length(event_hash)=64)
);
CREATE TABLE IF NOT EXISTS tracking_stop (
    interval_id bigint PRIMARY KEY REFERENCES tracking_interval(id),
    ended_at timestamptz NOT NULL,
    cohort_event_id bigint NOT NULL REFERENCES cohort_event(id)
);

-- Ending an interval changes coverage in every later cached bucket. Serialize
-- against builders exactly as tracking starts do; retained inputs rebuild it.
CREATE OR REPLACE FUNCTION invalidate_stopped_tracking_rollups() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    prior_interval bigint;
    next_interval bigint;
    target_app bigint;
BEGIN
    IF TG_OP IN ('UPDATE','DELETE') THEN prior_interval := OLD.interval_id; END IF;
    IF TG_OP IN ('INSERT','UPDATE') THEN next_interval := NEW.interval_id; END IF;
    FOR target_app IN SELECT DISTINCT app_id FROM tracking_interval
        WHERE id IN (prior_interval,next_interval) ORDER BY app_id
    LOOP
        PERFORM lock_rollup_app(target_app);
        UPDATE player_rollup_cache SET valid=false WHERE app_id=target_app AND valid;
    END LOOP;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$;
CREATE OR REPLACE TRIGGER stopped_tracking_rollup_invalidation
    BEFORE INSERT OR UPDATE OR DELETE ON tracking_stop
    FOR EACH ROW EXECUTE FUNCTION invalidate_stopped_tracking_rollups();

-- One rebuildable name-selection rule for enrolled summaries and catalog reads.
-- An enrollment label is only a fallback; it cannot hide an acquired Steam name.
CREATE OR REPLACE VIEW latest_app_name AS
    WITH names AS (
        SELECT e.app_id,e.name,s.observed_at,s.source,e.capture_id
          FROM catalog_entry e JOIN discovery_snapshot s USING(capture_id)
        UNION ALL SELECT app_id,name,observed_at,'steam_store_metadata',capture_id FROM app_name
        UNION ALL SELECT app_id,'Steam app '||app_id,created_at,'enrollment',NULL::uuid FROM app
    ) SELECT DISTINCT ON(app_id) app_id,name,observed_at,source FROM names
      ORDER BY app_id,(source='enrollment'),observed_at DESC,source,capture_id DESC;
INSERT INTO schema_migration(version) VALUES (8) ON CONFLICT DO NOTHING;

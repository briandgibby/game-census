-- Derived exact buckets. Retained captures/samples/tracking remain their owners.
CREATE TABLE IF NOT EXISTS player_rollup_cache (
    app_id bigint NOT NULL REFERENCES app(app_id),
    bucket_start timestamptz NOT NULL,
    bucket_seconds integer NOT NULL CHECK(bucket_seconds BETWEEN 300 AND 86400 AND 86400 % bucket_seconds=0),
    source text NOT NULL,
    source_version text NOT NULL,
    metric_policy_version text NOT NULL,
    cache_version text NOT NULL,
    input_revision text NOT NULL CHECK(length(input_revision)=64),
    payload jsonb NOT NULL,
    payload_checksum text NOT NULL CHECK(length(payload_checksum)=64),
    valid boolean NOT NULL DEFAULT true,
    built_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(app_id,bucket_start,bucket_seconds,source,source_version,metric_policy_version,cache_version)
);

CREATE OR REPLACE FUNCTION lock_rollup_app(target_app bigint) RETURNS void LANGUAGE sql AS $$
    SELECT pg_advisory_xact_lock(734801005000000000 + target_app);
$$;

CREATE OR REPLACE FUNCTION invalidate_player_rollups() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    first_app bigint;
    second_app bigint;
    horizon double precision;
BEGIN
    first_app := CASE WHEN TG_OP='DELETE' THEN OLD.app_id ELSE NEW.app_id END;
    second_app := CASE WHEN TG_OP='UPDATE' THEN OLD.app_id ELSE first_app END;
    PERFORM lock_rollup_app(LEAST(first_app,second_app));
    IF first_app<>second_app THEN
        PERFORM lock_rollup_app(GREATEST(first_app,second_app));
    END IF;
    -- Four is the code-owned maximum gap-cap multiplier, not an operator value.
    -- A changed sample can alter later carry even when those buckets are empty.
    IF TG_TABLE_NAME='tracking_interval' THEN
        UPDATE player_rollup_cache SET valid=false WHERE app_id IN(first_app,second_app) AND valid;
    ELSE
        IF TG_OP IN ('UPDATE','DELETE') THEN
            SELECT COALESCE(max(interval_seconds),604800)*4.0 INTO horizon FROM tracking_interval WHERE app_id=OLD.app_id;
            UPDATE player_rollup_cache SET valid=false WHERE app_id=OLD.app_id AND valid
                AND bucket_start+(bucket_seconds*interval '1 second')>OLD.observed_at
                AND bucket_start<OLD.observed_at+(horizon*interval '1 second');
        END IF;
        IF TG_OP IN ('INSERT','UPDATE') THEN
            SELECT COALESCE(max(interval_seconds),604800)*4.0 INTO horizon FROM tracking_interval WHERE app_id=NEW.app_id;
            UPDATE player_rollup_cache SET valid=false WHERE app_id=NEW.app_id AND valid
                AND bucket_start+(bucket_seconds*interval '1 second')>NEW.observed_at
                AND bucket_start<NEW.observed_at+(horizon*interval '1 second');
        END IF;
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$;

-- Replacing trigger definitions preserves their invalidation purpose during a
-- verified partition cutover; PostgreSQL installs these on child partitions.
CREATE OR REPLACE TRIGGER player_sample_rollup_invalidation
    BEFORE INSERT OR UPDATE OR DELETE ON player_sample
    FOR EACH ROW EXECUTE FUNCTION invalidate_player_rollups();
CREATE OR REPLACE TRIGGER tracking_rollup_invalidation
    BEFORE INSERT OR UPDATE OR DELETE ON tracking_interval
    FOR EACH ROW EXECUTE FUNCTION invalidate_player_rollups();
INSERT INTO schema_migration(version) VALUES(5) ON CONFLICT DO NOTHING;

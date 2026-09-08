-- One explicit owner for a closed UTC month's acquired captures.
-- Primary rows remain a readable copy; no retention timer removes history.
CREATE TABLE IF NOT EXISTS archive_owner (
    month date PRIMARY KEY CHECK(extract(day FROM month)=1),
    adopted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    backup_id text NOT NULL,
    proof_id text NOT NULL,
    archive_path text NOT NULL,
    manifest jsonb NOT NULL
);
CREATE OR REPLACE FUNCTION guard_archived_month() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS(SELECT 1 FROM archive_owner WHERE month=date_trunc('month',NEW.received_at AT TIME ZONE 'UTC')::date) THEN
        RAISE EXCEPTION 'Capture month belongs to an immutable archive; inspect archive status before changing history';
    END IF;
    RETURN NEW;
END $$;
CREATE OR REPLACE TRIGGER capture_archive_guard BEFORE INSERT ON capture
FOR EACH ROW EXECUTE FUNCTION guard_archived_month();
INSERT INTO schema_migration(version) VALUES(11) ON CONFLICT DO NOTHING;

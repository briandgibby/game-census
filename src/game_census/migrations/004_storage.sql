-- capture_identity is a derived routing/uniqueness registry. Capture payloads
-- remain canonical. No existing source table is renamed or deleted by this SQL.
CREATE TABLE IF NOT EXISTS storage_layout (
    singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
    layout text NOT NULL DEFAULT 'legacy' CHECK(layout IN ('legacy','partitioned')),
    converted_at timestamptz
);
INSERT INTO storage_layout(singleton) VALUES(true) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS storage_transition (
    transition_id uuid PRIMARY KEY,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    source_layout text NOT NULL,
    target_layout text NOT NULL,
    report jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS capture_identity (
    capture_id uuid PRIMARY KEY,
    attempt_id uuid UNIQUE NOT NULL REFERENCES request_attempt(attempt_id),
    received_at timestamptz NOT NULL,
    UNIQUE(capture_id,received_at),
    UNIQUE(capture_id,attempt_id,received_at)
);
-- This audit owns the exact membership of the original migration snapshot;
-- frozen legacy copies can therefore be rebuilt after later captures arrive.
CREATE TABLE IF NOT EXISTS storage_legacy_member (
    capture_id uuid PRIMARY KEY REFERENCES capture_identity(capture_id),
    transition_id uuid NOT NULL REFERENCES storage_transition(transition_id)
);
DO $storage$
BEGIN
    IF (SELECT layout FROM storage_layout WHERE singleton)='legacy' THEN
        CREATE TABLE IF NOT EXISTS capture_partitioned (
            capture_id uuid NOT NULL,
            attempt_id uuid NOT NULL,
            run_id uuid NOT NULL REFERENCES collection_run(run_id),
            app_id bigint NOT NULL REFERENCES app(app_id),
            source text NOT NULL, source_version text NOT NULL,
            request_started_at timestamptz NOT NULL, received_at timestamptz NOT NULL,
            http_status integer NOT NULL, parameters jsonb NOT NULL,
            payload bytea NOT NULL, checksum text NOT NULL CHECK(length(checksum)=64),
            capture_form text NOT NULL,
            PRIMARY KEY(capture_id,received_at),
            FOREIGN KEY(capture_id,attempt_id,received_at)
                REFERENCES capture_identity(capture_id,attempt_id,received_at)
                DEFERRABLE INITIALLY DEFERRED
        ) PARTITION BY RANGE(received_at);
        CREATE INDEX IF NOT EXISTS capture_partitioned_app_time ON capture_partitioned(app_id,received_at DESC);
        CREATE TABLE IF NOT EXISTS player_sample_partitioned (
            capture_id uuid NOT NULL,
            app_id bigint NOT NULL REFERENCES app(app_id),
            observed_at timestamptz NOT NULL,
            player_count bigint NOT NULL CHECK(player_count>=0),
            parser_version text NOT NULL,
            PRIMARY KEY(capture_id,observed_at),
            FOREIGN KEY(capture_id,observed_at) REFERENCES capture_identity(capture_id,received_at)
                DEFERRABLE INITIALLY DEFERRED
        ) PARTITION BY RANGE(observed_at);
        CREATE INDEX IF NOT EXISTS player_sample_partitioned_app_time ON player_sample_partitioned(app_id,observed_at DESC);
        IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid='capture_identity'::regclass
                      AND conname='capture_identity_has_payload') THEN
            ALTER TABLE capture_identity ADD CONSTRAINT capture_identity_has_payload
                FOREIGN KEY(capture_id,received_at) REFERENCES capture_partitioned(capture_id,received_at)
                DEFERRABLE INITIALLY DEFERRED;
        END IF;
    END IF;
END
$storage$;
INSERT INTO schema_migration(version) VALUES(4) ON CONFLICT DO NOTHING;

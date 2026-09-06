-- Durable plan approval and immutable occurrence identity; no timer is enabled here.
CREATE TABLE IF NOT EXISTS schedule_attestation (
    run_id uuid PRIMARY KEY REFERENCES collection_run(run_id),
    plan_hash text NOT NULL CHECK (length(plan_hash)=64),
    plan jsonb NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS schedule_acknowledgment (
    acknowledgment_id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES schedule_attestation(run_id),
    plan_hash text NOT NULL,
    acknowledged_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS schedule_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
    enabled boolean NOT NULL DEFAULT false,
    plan_hash text,
    epoch bigint NOT NULL DEFAULT 0,
    enabled_at timestamptz,
    cursor_at timestamptz
);
INSERT INTO schedule_state(singleton) VALUES(true) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS schedule_event (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    action text NOT NULL,
    plan_hash text,
    details jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS scheduled_job (
    job_id uuid PRIMARY KEY,
    plan_hash text NOT NULL,
    app_id bigint NOT NULL REFERENCES app(app_id),
    source text NOT NULL,
    scheduled_at timestamptz NOT NULL,
    deadline timestamptz NOT NULL,
    state text NOT NULL CHECK(state IN ('pending','leased','retry_pending','succeeded','failed','missed','cancelled')),
    lease_owner uuid,
    lease_version bigint NOT NULL DEFAULT 0,
    lease_until timestamptz,
    attempt_count integer NOT NULL DEFAULT 0 CHECK(attempt_count>=0),
    next_attempt_at timestamptz NOT NULL,
    capture_id uuid UNIQUE REFERENCES capture(capture_id),
    error jsonb,
    UNIQUE(app_id,source,scheduled_at),
    CHECK(deadline>scheduled_at),
    CHECK((state='succeeded')=(capture_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS scheduled_job_claim ON scheduled_job(plan_hash,state,scheduled_at DESC,next_attempt_at);
CREATE TABLE IF NOT EXISTS scheduled_attempt (
    attempt_id uuid PRIMARY KEY REFERENCES request_attempt(attempt_id),
    job_id uuid NOT NULL REFERENCES scheduled_job(job_id),
    lease_version bigint NOT NULL
);
CREATE TABLE IF NOT EXISTS scheduled_run (
    run_id uuid PRIMARY KEY REFERENCES collection_run(run_id),
    plan_hash text NOT NULL,
    epoch bigint NOT NULL,
    requested_cycles integer NOT NULL,
    max_run_seconds integer NOT NULL
);
CREATE TABLE IF NOT EXISTS schedule_adapter_stop (
    stop_id uuid PRIMARY KEY,
    plan_hash text NOT NULL,
    source text NOT NULL,
    stopped_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    error jsonb NOT NULL,
    resolved_at timestamptz
);
INSERT INTO schema_migration(version) VALUES(3) ON CONFLICT DO NOTHING;

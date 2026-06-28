CREATE TABLE IF NOT EXISTS external_commands (
    command_id              text        PRIMARY KEY,
    idempotency_key         text        NOT NULL UNIQUE,
    run_id                  text        NULL,
    integration             text        NOT NULL,
    request_payload         jsonb       NOT NULL,
    expected_run_id         text        NULL,
    expected_stream_version integer     NULL,
    expected_task_id        text        NULL,
    job_handle              text        NULL,
    dedup_supported         boolean     NOT NULL DEFAULT false,
    status                  text        NOT NULL DEFAULT 'pending'
                                CHECK (status IN ('pending','dispatched','succeeded','failed','stale_ignored')),
    result                  jsonb       NULL,
    result_event_id         text        NULL REFERENCES events(event_id),
    cost_units              numeric(18,4) NULL,
    attempts                integer     NOT NULL DEFAULT 0,
    dispatched_at           timestamptz NULL,
    completed_at            timestamptz NULL,
    created_at              timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS external_commands_status_idx ON external_commands (status, created_at);
CREATE INDEX IF NOT EXISTS external_commands_run_idx    ON external_commands (run_id) WHERE run_id IS NOT NULL;

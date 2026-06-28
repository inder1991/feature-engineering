CREATE TABLE timers (
    timer_id          text        PRIMARY KEY,
    idempotency_key   text        NOT NULL UNIQUE,
    aggregate         text        NOT NULL,
    aggregate_id      text        NOT NULL,
    task_id           text        NULL,
    kind              text        NOT NULL
                          CHECK (kind IN ('sla','reminder','escalation','auto_park',
                                          'experiment_expiry','business_repair','cost_breaker')),
    fire_at           timestamptz NOT NULL,
    business_calendar text        NULL,
    status            text        NOT NULL DEFAULT 'scheduled'
                          CHECK (status IN ('scheduled','leased','fired','cancelled')),
    lease_owner       text        NULL,
    lease_expires_at  timestamptz NULL,
    cas_task_version  integer     NULL,
    payload           jsonb       NOT NULL DEFAULT '{}',
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX timers_due_idx  ON timers (fire_at) WHERE status = 'scheduled';
CREATE INDEX timers_task_idx ON timers (task_id) WHERE task_id IS NOT NULL;

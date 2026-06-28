from __future__ import annotations

# Phase 04: durable runtime I — transactional outbox, worker queue, idempotency
# ledger (§5.2/§5.3). DDL verbatim from the shared contract (overview §
# "Database schema"). Registered into Phase 01's MIGRATIONS list by editing
# src/sp0/db/migrations.py.
RUNTIME_CORE_DDL = """
-- Phase 04: durable runtime I — transactional outbox, worker queue, idempotency ledger.

-- outbox — transactional outbox + leased relay (§5.2). Partitioned by aggregate key.
CREATE TABLE outbox (
    id               bigserial   PRIMARY KEY,
    message_id       text        NOT NULL UNIQUE,                 -- consumer idempotency key
    partition_key    text        NOT NULL,                        -- 'run:...' | 'feature:...' | 'request:...'
    topic            text        NOT NULL,
    payload          jsonb       NOT NULL,
    caused_by_event  text        NULL REFERENCES events(event_id),
    status           text        NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending','leased','sent','dead')),
    lease_owner      text        NULL,
    lease_expires_at timestamptz NULL,
    attempts         integer     NOT NULL DEFAULT 0,
    max_attempts     integer     NOT NULL DEFAULT 12,
    next_attempt_at  timestamptz NOT NULL DEFAULT now(),
    last_error       text        NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    sent_at          timestamptz NULL
);
CREATE INDEX outbox_dispatch_idx  ON outbox (status, next_attempt_at) WHERE status IN ('pending','leased');
CREATE INDEX outbox_partition_idx ON outbox (partition_key, id);

-- queue — worker queue, claimed via SELECT ... FOR UPDATE SKIP LOCKED (§5.2).
CREATE TABLE queue (
    id               bigserial   PRIMARY KEY,
    message_id       text        NOT NULL UNIQUE,                 -- idempotency
    partition_key    text        NOT NULL,                        -- aggregate key
    handler          text        NOT NULL,                        -- registered step-handler name
    payload          jsonb       NOT NULL,
    status           text        NOT NULL DEFAULT 'ready'
                         CHECK (status IN ('ready','leased','done','dead')),
    lease_owner      text        NULL,
    lease_expires_at timestamptz NULL,
    attempts         integer     NOT NULL DEFAULT 0,
    max_attempts     integer     NOT NULL DEFAULT 12,
    available_at     timestamptz NOT NULL DEFAULT now(),
    priority         integer     NOT NULL DEFAULT 100,
    last_error       text        NULL,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX queue_claim_idx ON queue (priority, available_at, id) WHERE status = 'ready';
-- per-aggregate serialization: only one in-flight lease per partition
CREATE UNIQUE INDEX queue_one_inflight_per_partition ON queue (partition_key) WHERE status = 'leased';
-- Worker claim pattern:
--   SELECT * FROM queue
--    WHERE status='ready' AND available_at <= now()
--      AND partition_key NOT IN (SELECT partition_key FROM queue WHERE status='leased')
--    ORDER BY priority, available_at, id
--    FOR UPDATE SKIP LOCKED LIMIT 1;

-- processed_messages — idempotency ledger (§5.3). Pruned by global_seq watermark.
CREATE TABLE processed_messages (
    message_id      text        PRIMARY KEY,
    aggregate       text        NOT NULL,
    aggregate_id    text        NOT NULL,
    result_event_id text        NULL REFERENCES events(event_id),
    processed_seq   bigint      NOT NULL,                         -- global_seq at processing time
    processed_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX processed_messages_prune_idx ON processed_messages (processed_seq);
"""

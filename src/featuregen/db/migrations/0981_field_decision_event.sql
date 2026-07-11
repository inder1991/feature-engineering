-- src/featuregen/db/migrations/0981_field_decision_event.sql
-- Spec §5.2: append-only, replayable field-decision events (the generic-field decision log; typed
-- facts stay in the OVERLAY_FACT_* events). Write-once — a supersession is a NEW row, never an update.
CREATE TABLE IF NOT EXISTS field_decision_event (
    decision_event_id       text        PRIMARY KEY,
    logical_ref             text        NOT NULL,
    field_name              text        NOT NULL,
    event_type              text        NOT NULL,   -- resolved|confirmed|rejected|staled|superseded
    selected_evidence_ids   jsonb       NOT NULL DEFAULT '[]',
    evidence_set_hash       text        NOT NULL,
    display_value_hash      text        NULL,
    load_bearing_value_hash text        NULL,
    conflict_status         text        NOT NULL,
    reason_codes            jsonb       NOT NULL DEFAULT '[]',
    field_policy_version    text        NOT NULL,
    resolver_version        text        NOT NULL,
    actor_ref               text        NULL,
    supersedes_event_id     text        NULL,
    created_at              timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS field_decision_event_object_idx
    ON field_decision_event (logical_ref, field_name, created_at);

-- src/featuregen/db/migrations/0507_overlay_tables.sql
-- SP-1 Phase 1: overlay read model, immutable evidence, dependency index, catalog fingerprint
-- snapshot, and projection-checkpoint init. All CREATE … IF NOT EXISTS; checkpoint insert is
-- ON CONFLICT DO NOTHING — fully idempotent.

-- Hot merged-view read model (one row per fact_key).
CREATE TABLE IF NOT EXISTS overlay_fact_state (
    fact_key           text        PRIMARY KEY,
    object_ref         text        NOT NULL,
    fact_type          text        NOT NULL,
    use_case           text        NULL,
    status             text        NOT NULL,
    value              jsonb       NULL,
    confirmers         jsonb       NOT NULL DEFAULT '[]',
    confirmed_at       timestamptz NULL,
    expires_at         timestamptz NULL,
    prior_value        jsonb       NULL,
    confirmed_event_id text        NULL,
    updated_seq        bigint      NOT NULL
);

-- Workflow / task read model (in-flight proposals & re-verifications).
CREATE TABLE IF NOT EXISTS overlay_proposal (
    fact_key             text        PRIMARY KEY,
    status               text        NOT NULL,
    proposed_value       jsonb       NOT NULL,
    proposal_fingerprint text        NOT NULL,
    draft_event_id       text        NOT NULL,
    target_event_id      text        NULL,
    evidence_ref         text        NULL,
    partial_confirmers   jsonb       NOT NULL DEFAULT '[]',
    object_ref           text        NOT NULL,
    fact_type            text        NOT NULL,
    use_case             text        NULL,
    prior_value          jsonb       NULL,
    updated_seq          bigint      NOT NULL
);

-- Immutable evidence (written at propose time — NOT a projection; aggregate metrics only).
CREATE TABLE IF NOT EXISTS overlay_evidence (
    evidence_id       text        PRIMARY KEY,
    fact_key          text        NOT NULL,
    table_snapshot_at timestamptz NULL,
    row_count         bigint      NULL,
    sample_size       bigint      NULL,
    profile_version   text        NULL,
    thresholds_used   jsonb       NULL,
    metric_values     jsonb       NULL,
    created_by        jsonb       NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now()
);

-- General dependency index (projection-maintained): which facts reference which objects.
CREATE TABLE IF NOT EXISTS overlay_fact_dependency (
    fact_key   text NOT NULL,
    ref_object text NOT NULL,
    PRIMARY KEY (fact_key, ref_object)
);
CREATE INDEX IF NOT EXISTS overlay_fact_dependency_ref_idx
    ON overlay_fact_dependency (ref_object);

-- Catalog fingerprint snapshot for change detection.
CREATE TABLE IF NOT EXISTS overlay_catalog_object (
    object_ref          text        PRIMARY KEY,
    native_oid          text        NULL,
    columns_fingerprint text        NULL,
    type_fingerprint    text        NULL,
    last_seen_seq       bigint      NULL,
    updated_at          timestamptz NOT NULL DEFAULT now()
);

-- Projection checkpoint for the overlay (non-analytics, fail-closed) projection.
INSERT INTO projection_checkpoints (projection_name) VALUES ('overlay')
ON CONFLICT DO NOTHING;

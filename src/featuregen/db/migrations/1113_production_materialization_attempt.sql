-- src/featuregen/db/migrations/1113_production_materialization_attempt.sql
-- §9.1 — the production MATERIALIZATION state machine, exactly as the 2026-08-23 spec wrote it
-- (docs/superpowers/specs/2026-08-23-production-acts-state-machines.md §2). Engineering BEHIND
-- an unavailable action: §0.1.0 keeps MATERIALIZE_PRODUCTION refusing at the decision service,
-- so no row here can exist until the owner opens the policy — building the machine first is what
-- makes opening it a decision rather than a project.
--
-- ▲ UNKNOWN_OUTCOME is a FIRST-CLASS state — "the cluster did something and we do not know what"
-- is the state crash recovery actually finds, and a machine without it will guess. The
-- reconciler resolves it by asking the cluster about `external_operation_id`; unreachable stays
-- UNKNOWN with a gauge, never a guess (the released-message discipline).

CREATE TABLE IF NOT EXISTS production_materialization_attempt (
    attempt_id                  text PRIMARY KEY CHECK (btrim(attempt_id) <> ''),
    sealed_artifact_id          text NOT NULL CHECK (btrim(sealed_artifact_id) <> ''),
    environment_id              text NOT NULL CHECK (btrim(environment_id) <> ''),
    logical_group_name          text NOT NULL CHECK (btrim(logical_group_name) <> ''),
    target_ref                  text,
    status                      text NOT NULL DEFAULT 'REQUESTED'
        CONSTRAINT production_materialization_status_v1 CHECK (status IN (
            'REQUESTED', 'CLAIMED', 'RUNNING', 'UNKNOWN_OUTCOME', 'STAGED',
            'SUCCEEDED', 'REFUSED', 'FAILED', 'CANCELLED')),
    -- §8.2's request-time decision, rechecked by the worker before the act.
    action_decision_revision_id text NOT NULL REFERENCES action_decision_revision(decision_id),
    -- The reconciler's question to the cluster is keyed on this; STORED BEFORE the submit call,
    -- because an identity recorded after the crash window is no use inside it.
    external_operation_id       text,
    staging_path                text,
    quarantine_path             text,
    requested_by                text NOT NULL CHECK (btrim(requested_by) <> ''),
    requested_at                timestamptz NOT NULL,
    terminal_detail_json        jsonb,
    lease_owner                 text,
    lease_expires_at            timestamptz,
    lease_fence                 bigint NOT NULL DEFAULT 0,
    attempts                    integer NOT NULL DEFAULT 0,

    -- A SUCCEEDED attempt names where it staged; a FAILED one with staging present names its
    -- quarantine — the operator sweep reads THIS column, and the platform never auto-deletes.
    CONSTRAINT production_materialization_success_was_staged CHECK (
        status <> 'SUCCEEDED' OR staging_path IS NOT NULL)
);

-- One LIVE attempt per (environment, group) — the money-guard scope: terminals release the slot.
CREATE UNIQUE INDEX IF NOT EXISTS production_materialization_one_live
    ON production_materialization_attempt (environment_id, logical_group_name)
    WHERE status IN ('REQUESTED', 'CLAIMED', 'RUNNING', 'UNKNOWN_OUTCOME', 'STAGED');

CREATE INDEX IF NOT EXISTS production_materialization_due
    ON production_materialization_attempt (lease_expires_at)
    WHERE status IN ('REQUESTED', 'CLAIMED', 'RUNNING', 'UNKNOWN_OUTCOME', 'STAGED');

-- ── the OUTPUT IDENTITY: what the attempt actually produced, content-addressed ──────────────────
-- Publication binds to a THING through the composite FK in 1114 — "publish what THAT attempt
-- produced" becomes schema, not reader convention (§9.1's forgery rule: the publication request
-- names the ATTEMPT; the server resolves its output; a client-supplied output id has no column).
CREATE TABLE IF NOT EXISTS materialized_output_revision (
    output_revision_id  text PRIMARY KEY CHECK (btrim(output_revision_id) <> ''),
    attempt_id          text NOT NULL REFERENCES production_materialization_attempt(attempt_id),
    output_manifest_hash text NOT NULL CHECK (btrim(output_manifest_hash) <> ''),
    row_count           bigint NOT NULL CHECK (row_count >= 0),
    produced_at         timestamptz NOT NULL DEFAULT now(),

    -- One output per attempt: an attempt that measured twice is two attempts. Also the UNIQUE
    -- half of 1114's composite FK.
    CONSTRAINT materialized_output_one_per_attempt UNIQUE (attempt_id),
    CONSTRAINT materialized_output_attempt_pair UNIQUE (attempt_id, output_revision_id)
);

CREATE OR REPLACE FUNCTION materialized_output_revision_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'materialized_output_revision is append-only: it is what a materialization '
                    'MEASURED, and a measurement that can be rewritten is not evidence';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS materialized_output_revision_no_change ON materialized_output_revision;
CREATE TRIGGER materialized_output_revision_no_change
    BEFORE UPDATE OR DELETE ON materialized_output_revision
    FOR EACH ROW EXECUTE FUNCTION materialized_output_revision_write_once();

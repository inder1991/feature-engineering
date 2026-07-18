-- src/featuregen/db/migrations/1002_live_activation.sql
-- Phase 3C.2a live-activation interlock (append-only, no signing). enablement_evaluation is a
-- persisted, content-hashed run of the 3C.1 machine gate (server-assembled from trusted sources).
-- live_activation_decision is the human APPROVE/REVOKE bound to one evaluation + this deployment.
-- WORM: both are write-once (INSERT only); UPDATE/DELETE/TRUNCATE revoked from featuregen_app
-- (mirror 0971). Approval is permitted only over a result='PASS' evaluation (enforced in code).
CREATE TABLE IF NOT EXISTS enablement_evaluation (
    evaluation_id     text        PRIMARY KEY,
    telemetry_window  jsonb       NOT NULL,
    population_report jsonb       NOT NULL,
    gold_set_result   jsonb       NOT NULL,
    stability_result  jsonb       NOT NULL,
    layer_b_labels    jsonb       NULL,
    version_vector    jsonb       NOT NULL,
    result            text        NOT NULL CHECK (result IN ('PASS', 'FAIL')),
    content_hash      text        NOT NULL,
    evaluated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS live_activation_decision (
    decision_id            text        PRIMARY KEY,
    evaluation_id          text        NOT NULL REFERENCES enablement_evaluation(evaluation_id),
    deployment_id          text        NOT NULL,
    decision               text        NOT NULL CHECK (decision IN ('APPROVE', 'REVOKE')),
    decided_by             text        NOT NULL,
    reason                 text        NOT NULL DEFAULT '',
    decided_at             timestamptz NOT NULL DEFAULT now(),
    supersedes_decision_id text        NULL REFERENCES live_activation_decision(decision_id)
);
CREATE INDEX IF NOT EXISTS live_activation_by_deployment
    ON live_activation_decision (deployment_id, decided_at DESC);

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'featuregen_app') THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON enablement_evaluation FROM featuregen_app;
        REVOKE UPDATE, DELETE, TRUNCATE ON live_activation_decision FROM featuregen_app;
    END IF;
END $$;

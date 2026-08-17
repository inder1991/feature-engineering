-- src/featuregen/db/migrations/1081_publication_attempt.sql
-- S10 (docs/superpowers/plans/2026-08-15-integration-consolidation.md): the publication ATTEMPT and
-- its four-way outcome, including the uncertain one.
--
-- WHY FOUR OUTCOMES AND NOT TWO. Publication crosses two planes: a Hive swap and a PostgreSQL
-- transaction. There is a real window where the swap succeeds and the transaction later rolls back,
-- and no amount of care inside one plane closes it. The alternative to admitting that is a
-- distributed transaction, which this platform does not have and would not want on the publish
-- path. So an attempt ends in STARTED · SUCCEEDED · FAILED · UNKNOWN_RECONCILIATION_REQUIRED, and
-- the fourth is the honest name for "the swap may or may not have landed".
--
-- AN UNCERTAIN ATTEMPT BLOCKS RETRY UNTIL RECONCILED. Retrying an attempt that may already have
-- swapped is how a group gets published twice, or published from an artifact the operator thought
-- had failed. `reconciled_at` and `reconciled_outcome` are how it stops being uncertain, and the
-- partial unique index below is the mechanism: at most ONE unreconciled uncertain attempt per
-- (environment, group), so a retry cannot even be recorded while one is outstanding.
--
-- STARTED IS ALSO BLOCKING, deliberately. A row that says STARTED and never moved is an attempt
-- whose fate nobody knows — operationally the same uncertainty, arrived at by crashing rather than
-- by catching. Treating it as retryable would be assuming a crash means nothing happened.
--
-- ENVIRONMENT-SCOPED, per F3 and C-D6 (1085). Every key here is at least
-- (environment_id, logical_group_name): environment is deployment placement, and an attempt that
-- did not say which one could block a retry in a cluster it never touched.
--
-- CAPABILITY IS RECORDED HERE AND NOWHERE IN S9. Verification must not require a publication
-- capability and publication must — so the attestation lives on this table, and `1080` deliberately
-- has no column for it.
--
-- NOT APPLIED. This file is written, not run.

CREATE TABLE IF NOT EXISTS publication_attempt (
    attempt_id        text PRIMARY KEY CHECK (btrim(attempt_id) <> ''),

    environment_id    text NOT NULL CHECK (btrim(environment_id) <> ''),
    logical_group_name text NOT NULL CHECK (btrim(logical_group_name) <> ''),

    -- WHAT is being published: the exact verified output, and the artifact behind it.
    verified_output_revision_id text NOT NULL CHECK (btrim(verified_output_revision_id) <> ''),
    sealed_artifact_id text NOT NULL CHECK (btrim(sealed_artifact_id) <> ''),

    -- The active revision the caller READ before deciding to publish. NULL only for the first
    -- publication of a group, where there is none — never as "I did not check".
    expected_active_revision_id text CHECK (
        expected_active_revision_id IS NULL OR btrim(expected_active_revision_id) <> ''),

    -- Reselected against the CURRENT environment at publish time, not inherited from the
    -- compilation: the mechanism the artifact was rendered for may no longer be the right one.
    publish_mechanism text NOT NULL CHECK (btrim(publish_mechanism) <> ''),

    -- The capability attestation publication requires and verification must not (§0.3).
    capability_attestation text NOT NULL CHECK (btrim(capability_attestation) <> ''),

    outcome           text NOT NULL CHECK (outcome IN (
                          'started', 'succeeded', 'failed',
                          'unknown_reconciliation_required')),
    detail            text NOT NULL,

    -- How an uncertain attempt stops being uncertain. Both NULL together or both set together —
    -- a reconciliation with no outcome is a check somebody started and did not finish.
    reconciled_at     text,
    reconciled_outcome text CHECK (reconciled_outcome IN ('succeeded', 'failed')),

    started_at        text NOT NULL CHECK (btrim(started_at) <> ''),
    recorded_at       timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT publication_attempt_reconciliation_is_whole
        CHECK ((reconciled_at IS NULL) = (reconciled_outcome IS NULL)),
    -- Only an UNCERTAIN attempt is reconciled. Reconciling a SUCCEEDED one would restate a fact the
    -- attempt already established.
    CONSTRAINT publication_attempt_only_uncertain_is_reconciled
        CHECK (reconciled_at IS NULL OR outcome = 'unknown_reconciliation_required')
);

-- AT MOST ONE BLOCKING ATTEMPT per (environment, group). Both STARTED and the uncertain outcome
-- count: a STARTED row that never moved is an attempt whose fate nobody knows, which is the same
-- operational uncertainty reached by crashing rather than by catching.
CREATE UNIQUE INDEX IF NOT EXISTS publication_attempt_one_blocking
    ON publication_attempt (environment_id, logical_group_name)
    WHERE reconciled_at IS NULL
      AND outcome IN ('started', 'unknown_reconciliation_required');

CREATE INDEX IF NOT EXISTS publication_attempt_by_group
    ON publication_attempt (environment_id, logical_group_name, recorded_at);

-- ── outcome and reconciliation are the only things that move ─────────────────────────────────────
-- An attempt is a record of something that happened to two systems. What it published, where, and
-- under whose capability can never change; the outcome moves once (started → one of three) and the
-- reconciliation fields move once. Everything else is frozen on arrival.
CREATE OR REPLACE FUNCTION s10_publication_attempt_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'publication_attempt is append-only';
    END IF;
    IF (NEW.attempt_id, NEW.environment_id, NEW.logical_group_name,
        NEW.verified_output_revision_id, NEW.sealed_artifact_id,
        NEW.expected_active_revision_id, NEW.publish_mechanism, NEW.capability_attestation,
        NEW.started_at)
       IS DISTINCT FROM
       (OLD.attempt_id, OLD.environment_id, OLD.logical_group_name,
        OLD.verified_output_revision_id, OLD.sealed_artifact_id,
        OLD.expected_active_revision_id, OLD.publish_mechanism, OLD.capability_attestation,
        OLD.started_at) THEN
        RAISE EXCEPTION 'publication_attempt is append-only except outcome and reconciliation';
    END IF;
    -- A settled attempt stays settled: only STARTED may move, and only the uncertain outcome may
    -- gain a reconciliation.
    IF OLD.outcome <> NEW.outcome AND OLD.outcome <> 'started' THEN
        RAISE EXCEPTION 'publication_attempt outcome % is already settled', OLD.outcome;
    END IF;
    IF OLD.reconciled_at IS NOT NULL AND NEW.reconciled_at IS DISTINCT FROM OLD.reconciled_at THEN
        RAISE EXCEPTION 'publication_attempt reconciliation is recorded once';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS publication_attempt_guard ON publication_attempt;
CREATE TRIGGER publication_attempt_guard
    BEFORE UPDATE OR DELETE ON publication_attempt
    FOR EACH ROW EXECUTE FUNCTION s10_publication_attempt_guard();

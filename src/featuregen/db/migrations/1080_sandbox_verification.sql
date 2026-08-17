-- src/featuregen/db/migrations/1080_sandbox_verification.sql
-- S9 (docs/superpowers/plans/2026-08-15-integration-consolidation.md): on-demand sandbox
-- verification — the attempt, its staging path, and the verified output it may produce.
--
-- NO PUBLICATION CAPABILITY, ANYWHERE. Verification must run without one and publication must
-- require one (§0.3), so there is no publication column here at all — not a nullable one, not a
-- boolean defaulting to false. A column would eventually be read as "may publish", which is exactly
-- the attestation S9 is defined to run without. Publication's own state lives in S10's tables.
--
-- TWO ATTEMPTS DO NOT SHARE A PATH. `attempt` is part of the execution identity (C-D13) because the
-- existing staging root is GENERATION-SCOPED: without it a second verification of the same
-- generation writes over the first, and "the exact staging output" then names two different things.
-- The unique index on `staging_path` is the database saying the same thing — a path collision is
-- refused rather than discovered when the second run overwrites the first's rows.
--
-- STALENESS IS THREE-WAY, AND THE THIRD VALUE IS THE POINT. A comparable OBSERVED input that
-- changed is STALE; an identical observation is CURRENT; an UNPINNED one is NEITHER — it was never
-- pinned to anything, so no content comparison can say whether it moved. Storing that as a boolean
-- would force UNPINNED into one of the two, and both answers are lies: "current" claims a check
-- nobody could run, "stale" claims a change nobody observed. `input_observation_strength` is what
-- decides which of the three applies, and it is stored on the verified output.
--
-- `PINNED` IS NEVER A CLAIM WITHOUT ENFORCED READS. The CHECK below forbids the combination
-- directly: an output claiming PINNED strength must record that its reads were enforced, because
-- "pinned" means the run could only have read what it pinned — and without enforcement that is a
-- description of intent rather than of what happened.
--
-- NOT APPLIED. This file is written, not run.

CREATE TABLE IF NOT EXISTS verification_attempt (
    execution_hash   text PRIMARY KEY CHECK (btrim(execution_hash) <> ''),

    generation_authorization_revision_id text NOT NULL CHECK (
        btrim(generation_authorization_revision_id) <> ''),
    check_set_hash   text NOT NULL CHECK (btrim(check_set_hash) <> ''),
    inventory_observation_id text NOT NULL CHECK (btrim(inventory_observation_id) <> ''),

    -- Counted from 1. Zero would collide with the generation-scoped root this field replaces.
    attempt          integer NOT NULL CHECK (attempt >= 1),
    run_parameters   jsonb NOT NULL,

    -- PER ATTEMPT, and unique. Two attempts sharing a path is the defect `attempt` exists to fix.
    staging_path     text NOT NULL CHECK (btrim(staging_path) <> ''),

    -- The exact sealed artifact this attempt verified. A verification not tied to one verifies
    -- whatever happened to be rendered.
    sealed_artifact_id text NOT NULL CHECK (btrim(sealed_artifact_id) <> ''),

    started_at       text NOT NULL CHECK (btrim(started_at) <> ''),
    recorded_at      timestamptz NOT NULL DEFAULT now(),

    -- NOTHING ABOUT PUBLICATION. See the header: a column here would eventually be read as
    -- permission, and verification is defined to run without one.
    CONSTRAINT verification_attempt_is_per_attempt UNIQUE (
        generation_authorization_revision_id, attempt)
);

CREATE UNIQUE INDEX IF NOT EXISTS verification_attempt_staging_path
    ON verification_attempt (staging_path);

CREATE TABLE IF NOT EXISTS verified_output_revision (
    revision_id      text PRIMARY KEY CHECK (btrim(revision_id) <> ''),
    execution_hash   text NOT NULL REFERENCES verification_attempt(execution_hash),

    check_set_hash   text NOT NULL CHECK (btrim(check_set_hash) <> ''),
    validator_versions jsonb NOT NULL,

    -- An output pinning NO policy hashes could never go stale: a policy changed afterwards would
    -- leave it vouching for an artifact whose meaning moved, with nothing able to notice.
    pinned_policy_hashes jsonb NOT NULL CHECK (jsonb_array_length(pinned_policy_hashes) > 0),

    input_observation_strength text NOT NULL CHECK (
        input_observation_strength IN ('pinned', 'observed', 'unpinned')),

    -- Whether the run's reads were ENFORCED, not merely intended. See the CHECK below.
    reads_enforced   boolean NOT NULL,

    retention_state  text NOT NULL CHECK (
        retention_state IN ('live', 'marked_orphan', 'quarantined', 'swept')),

    recorded_at      timestamptz NOT NULL DEFAULT now(),

    -- "PINNED" means the run could only have read what it pinned. Without enforcement that is a
    -- description of intent, and a staleness answer computed from it would be about a promise.
    CONSTRAINT verified_output_pinned_requires_enforced_reads
        CHECK (input_observation_strength <> 'pinned' OR reads_enforced)
);

CREATE INDEX IF NOT EXISTS verified_output_revision_by_execution
    ON verified_output_revision (execution_hash);

-- ── append-only ──────────────────────────────────────────────────────────────────────────────────
-- An attempt is a record of something that happened and a verified output is what a publication
-- rests on. `retention_state` is the ONE thing that legitimately moves (live → marked_orphan →
-- quarantined → swept, reused from `runtime/blob_gc`), so the trigger permits an UPDATE that
-- changes only that column and refuses every other edit.
CREATE OR REPLACE FUNCTION s9_verification_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION s9_verified_output_retention_only() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'verified_output_revision is append-only';
    END IF;
    IF (NEW.revision_id, NEW.execution_hash, NEW.check_set_hash, NEW.validator_versions,
        NEW.pinned_policy_hashes, NEW.input_observation_strength, NEW.reads_enforced)
       IS DISTINCT FROM
       (OLD.revision_id, OLD.execution_hash, OLD.check_set_hash, OLD.validator_versions,
        OLD.pinned_policy_hashes, OLD.input_observation_strength, OLD.reads_enforced) THEN
        RAISE EXCEPTION 'verified_output_revision is append-only except retention_state';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS verification_attempt_no_update ON verification_attempt;
CREATE TRIGGER verification_attempt_no_update
    BEFORE UPDATE OR DELETE ON verification_attempt
    FOR EACH ROW EXECUTE FUNCTION s9_verification_write_once();

DROP TRIGGER IF EXISTS verified_output_revision_retention_only ON verified_output_revision;
CREATE TRIGGER verified_output_revision_retention_only
    BEFORE UPDATE OR DELETE ON verified_output_revision
    FOR EACH ROW EXECUTE FUNCTION s9_verified_output_retention_only();

-- src/featuregen/db/migrations/1009_feature_contract_validation.sql
-- Delivery C4 (event-sourced feature-contract validation lifecycle) Task 1: the tables only (the
-- state PROJECTION registration + confirm-time event emission + effective-state read land in
-- C4-T2..T4). C4 turns contract validation into a version-scoped, APPEND-ONLY EVENT lifecycle whose
-- current-state PROJECTION is the authoritative effective stamp going forward. It sits ON TOP of the
-- shipped `contract.validation_status`/`requirements` columns (1003): those STAY UNCHANGED as the
-- INITIAL stamp at confirm; this event stream is authoritative thereafter. This migration adds NO
-- validation columns to the mutable `feature` table and NO mutable requirements JSON to `contract`
-- (the 1003 columns remain the initial stamp only).
--   feature_contract_validation_event  — APPEND-ONLY, write-once lifecycle log (the authority).
--   feature_contract_validation_state  — REBUILDABLE PROJECTION, 1 row per contract_id (the read
--     model C4-T2 will register with projections/runner.py and UPSERT; NOT write-once by design —
--     a projection replay must be able to overwrite it).
--   feature_validation_requirement     — IMMUTABLE, write-once, version/fingerprint/hash-keyed
--     requirement rows. External-dataset binding is DELIBERATELY NOT embedded here (that is Delivery I).
-- All statements idempotent: CREATE ... IF NOT EXISTS / CREATE OR REPLACE.

-- 1) The APPEND-ONLY validation lifecycle log (write-once). Each row is one lifecycle event for a
--    contract; a supersession/invalidation is a NEW row, never an update. `seq` is a per-table
--    monotonic ordering column (GENERATED ALWAYS AS IDENTITY, UNIQUE) the state projection folds in
--    order — its UNIQUE index doubles as the ordering (seq) index, so no separate seq index is added.
CREATE TABLE IF NOT EXISTS feature_contract_validation_event (
    event_id     text        PRIMARY KEY,
    contract_id  text        NOT NULL REFERENCES contract(contract_id),
    seq          bigint      GENERATED ALWAYS AS IDENTITY,
    event_type   text        NOT NULL
                 CHECK (event_type IN ('ASSESSED', 'EXTERNAL_PASSED', 'EXTERNAL_FAILED',
                                       'INVALIDATED', 'SUPERSEDED')),
    payload      jsonb       NOT NULL DEFAULT '{}',
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT feature_contract_validation_event_seq_unique UNIQUE (seq)
);
CREATE INDEX IF NOT EXISTS feature_contract_validation_event_contract_idx
    ON feature_contract_validation_event (contract_id);

CREATE OR REPLACE FUNCTION feature_contract_validation_event_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'feature_contract_validation_event records are write-once: % not allowed on event_id=%',
        TG_OP, COALESCE(OLD.event_id, NEW.event_id);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER feature_contract_validation_event_no_mutation
    BEFORE UPDATE OR DELETE ON feature_contract_validation_event
    FOR EACH ROW EXECUTE FUNCTION feature_contract_validation_event_write_once();

-- 2) The REBUILDABLE current-state PROJECTION — exactly one row per contract_id. C4-T2 registers this
--    with projections/runner.py (projection_checkpoints/skips/degraded) and UPSERTs it as it folds the
--    event log. `applied_seq` records the highest event `seq` folded into this row (a projection
--    watermark / idempotency guard). DELIBERATELY NOT write-once: a rebuildable read model must be
--    overwritable by replay — the log above is the authority, this table is derived.
CREATE TABLE IF NOT EXISTS feature_contract_validation_state (
    contract_id            text        PRIMARY KEY REFERENCES contract(contract_id),
    validation_status      text        NOT NULL
                           CHECK (validation_status IN ('design_checked',
                                                        'needs_external_validation', 'rejected')),
    effective_verification text        NOT NULL
                           CHECK (effective_verification IN ('UNVERIFIED', 'DESIGN-CHECKED',
                                                            'DATA-CHECKED', 'USEFULNESS-CHECKED')),
    -- MF-4: TERMINAL supersession marker. A re-confirm mints a new contract version and emits a
    -- SUPERSEDED event for the prior version; the fold sets this true + demotes effective_verification
    -- to UNVERIFIED, so the retired version reads not-live and a late EXTERNAL_PASSED can never
    -- resurrect it. Derived (the fold is a pure function of the event prefix), so a replay reproduces
    -- it identically. Additive + nullable-with-default = existing/replayed rows default false.
    superseded             boolean     NOT NULL DEFAULT false,
    applied_seq            bigint      NOT NULL DEFAULT 0,
    updated_at             timestamptz NOT NULL DEFAULT now()
);
-- Idempotent add for a database that already applied an earlier 1009 without the column.
ALTER TABLE feature_contract_validation_state
    ADD COLUMN IF NOT EXISTS superseded boolean NOT NULL DEFAULT false;

-- 3) The IMMUTABLE requirement rows (write-once), version- + fingerprint- + content-hash-keyed. A
--    re-assessment against a new schema version or a changed metadata input yields NEW rows (distinct
--    on the UNIQUE key below); an existing requirement is never mutated. External-dataset binding is
--    DELIBERATELY absent — that is Delivery I.
CREATE TABLE IF NOT EXISTS feature_validation_requirement (
    requirement_id             text        PRIMARY KEY,
    contract_id                text        NOT NULL REFERENCES contract(contract_id),
    requirement_schema_version text        NOT NULL,
    metadata_input_fingerprint text        NOT NULL,
    code                       text        NOT NULL,
    subject_json               jsonb       NOT NULL DEFAULT '{}',
    params_json                jsonb       NOT NULL DEFAULT '{}',
    blocking                   boolean     NOT NULL DEFAULT true,
    content_hash               text        NOT NULL,
    created_at                 timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT feature_validation_requirement_identity_unique
        UNIQUE (contract_id, requirement_schema_version, metadata_input_fingerprint, content_hash)
);
CREATE INDEX IF NOT EXISTS feature_validation_requirement_contract_idx
    ON feature_validation_requirement (contract_id);

CREATE OR REPLACE FUNCTION feature_validation_requirement_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'feature_validation_requirement records are write-once: % not allowed on requirement_id=%',
        TG_OP, COALESCE(OLD.requirement_id, NEW.requirement_id);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER feature_validation_requirement_no_mutation
    BEFORE UPDATE OR DELETE ON feature_validation_requirement
    FOR EACH ROW EXECUTE FUNCTION feature_validation_requirement_write_once();

-- MF-3 (WORM breach): the BEFORE UPDATE OR DELETE row triggers above are FOR EACH ROW and, like
-- every row trigger, do NOT fire on a statement-level TRUNCATE — so without this the app role could
-- vaporize the append-only authority log (and the immutable requirement rows) while the mutable
-- state table keeps serving DATA-CHECKED. Mirror the 0900/1002 deployment control: a guarded REVOKE
-- of destructive DML from the production non-superuser 'featuregen_app' role. No-op in the superuser
-- test cluster where the role is absent (a superuser bypasses grants anyway). The REBUILDABLE
-- feature_contract_validation_state is DELIBERATELY excluded — a projection replay must overwrite it.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'featuregen_app') THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON feature_contract_validation_event FROM featuregen_app;
        REVOKE UPDATE, DELETE, TRUNCATE ON feature_validation_requirement FROM featuregen_app;
    END IF;
END $$;

-- src/featuregen/db/migrations/1115_feature_run_spine_foundation.sql
-- Run-spine FOUNDATION (spec 2026-08-22-feature-run-spine-design.md rev 3.1, §6.1/§5/§13).
--
-- IDENTITY IS A CHAIN THE DATABASE REFUSES TO MIS-ASSEMBLE. Every business input is pinned by a
-- COMPOSITE foreign key carrying generation_run_id, so a row cannot combine valid identifiers from
-- unrelated runs and hash the false combination. All chain columns are NOT NULL — MATCH SIMPLE
-- cannot disarm the checks.
-- NO FORK COLUMNS, deliberately (spec §6.1 [R3.1]): forked_from_attempt_id / fork_plan_revision_id
-- arrive with the actionable increment's tables; a foundation FK to a table it does not create
-- would fail on a fresh database.
-- feature_run_state ships EMPTY: rows are minted lazily by the first run-centric mutation, and the
-- foundation performs none (read-only increment).
-- NOT APPLIED. This file is written, not run.

-- additive UNIQUE supersets of existing PKs, so the composite FKs below have targets
CREATE UNIQUE INDEX IF NOT EXISTS contract_generation_input_chain_key
    ON contract_generation_input (generation_run_id, intent_id, confirmed_scope_id);
CREATE UNIQUE INDEX IF NOT EXISTS contract_considered_revision_chain_key
    ON contract_considered_revision (considered_revision_id, generation_run_id, intent_id);
CREATE UNIQUE INDEX IF NOT EXISTS catalog_metadata_snapshot_chain_key
    ON catalog_metadata_snapshot (snapshot_id, generation_run_id);

CREATE TABLE IF NOT EXISTS feature_run_identity (
    generation_run_id  text PRIMARY KEY
        REFERENCES feature_generation_run (generation_run_id),
    workflow_definition_version text NOT NULL CHECK (workflow_definition_version = 'V1'),
    intent_id                   text NOT NULL,
    confirmed_scope_id          text NOT NULL,
    generation_input_content_hash text NOT NULL CHECK (btrim(generation_input_content_hash) <> ''),
    considered_revision_id      text NOT NULL,
    considered_content_hash     text NOT NULL CHECK (btrim(considered_content_hash) <> ''),
    metadata_snapshot_id        text NOT NULL,
    metadata_snapshot_content_hash text NOT NULL CHECK (btrim(metadata_snapshot_content_hash) <> ''),
    owner_subject               text NOT NULL CHECK (btrim(owner_subject) <> ''),
    owner_tenant                text NULL,
    -- BOTH ancestry columns are FK'd to this table (spec §6.1: "parent and root are workflow-V1
    -- runs"). A root row naming ITSELF passes: FK triggers fire at end of statement, by which time
    -- the row exists. Without the root FK a forked row could name a root that is not a run at all.
    root_generation_run_id      text NOT NULL
        REFERENCES feature_run_identity (generation_run_id),
    parent_generation_run_id    text NULL REFERENCES feature_run_identity (generation_run_id),
    run_identity_hash           text NOT NULL CHECK (btrim(run_identity_hash) <> ''),
    created_by                  text NOT NULL,
    created_at                  timestamptz NOT NULL DEFAULT now(),

    -- a root points to itself; parent NULL exactly for a root (spec §6.1)
    CONSTRAINT feature_run_identity_root_shape CHECK (
        (parent_generation_run_id IS NULL) = (generation_run_id = root_generation_run_id)),
    FOREIGN KEY (generation_run_id, intent_id, confirmed_scope_id)
        REFERENCES contract_generation_input (generation_run_id, intent_id, confirmed_scope_id),
    FOREIGN KEY (considered_revision_id, generation_run_id, intent_id)
        REFERENCES contract_considered_revision (considered_revision_id, generation_run_id, intent_id),
    FOREIGN KEY (metadata_snapshot_id, generation_run_id)
        REFERENCES catalog_metadata_snapshot (snapshot_id, generation_run_id)
);

CREATE OR REPLACE FUNCTION feature_run_identity_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'feature_run_identity is write-once: % not allowed on %',
        TG_OP, COALESCE(OLD.generation_run_id, '?');
END;
$$ LANGUAGE plpgsql;
CREATE OR REPLACE TRIGGER feature_run_identity_no_mutation
    BEFORE UPDATE OR DELETE ON feature_run_identity
    FOR EACH ROW EXECUTE FUNCTION feature_run_identity_write_once();

-- mutable DISPLAY metadata, identity-free (renaming a run re-keys nothing)
CREATE TABLE IF NOT EXISTS feature_run_profile (
    generation_run_id text PRIMARY KEY,
    display_name      text NULL,
    description       text NULL,
    archived          boolean NOT NULL DEFAULT false,
    updated_at        timestamptz NOT NULL DEFAULT now()
);

-- the only mutable COORDINATION row per run; rows minted lazily, NONE in the foundation.
-- PK/FK per spec §5 — free to declare while the table ships empty, and it keeps a coordination row
-- from ever naming a run that does not exist.
CREATE TABLE IF NOT EXISTS feature_run_state (
    generation_run_id text PRIMARY KEY
        REFERENCES feature_generation_run (generation_run_id),
    state_version     bigint NOT NULL DEFAULT 0
);

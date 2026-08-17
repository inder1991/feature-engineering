-- src/featuregen/db/migrations/1075_policy_occurrences_and_realizations.sql
-- S4 (docs/superpowers/plans/2026-08-15-integration-consolidation.md): policy occurrences, policy
-- realization revisions, the current pointer per family, and retained conflict findings.
--
-- WHAT THIS CLOSES, VERIFIED RATHER THAN ASSUMED. Nothing in this codebase resolves a governed
-- policy reference today. `parse_policy_ref` (banking_policies.py:129) checks the SHAPE and the
-- KIND against an in-process registry and never touches a connection — its own docstring calls it
-- "the check a carrier runs BEFORE any store lookup". The single call site
-- (policy_occurrences.py:208) discards the name half. And `semantic_eligibility.py:326-327` emits
-- `STATUS_POLICY_UNRESOLVED` on the mere PRESENCE of a ref, with no lookup between the two lines —
-- its own resolution text says why: "this recipe reads a governed status policy no resolver serves
-- yet". So today EVERY status ref is unresolved by definition, and "unresolvable" is not a state
-- the platform can distinguish. These tables are what make it distinguishable.
--
-- THE HOUSE POINTER SHAPE, and I checked before choosing. ELEVEN `*_current` tables in this tree
-- use an immutable `*_revision` table plus a SEPARATE pointer row carrying `pointer_version`,
-- advanced by compare-and-swap (e.g. dataset_serving_policy_current, 1048:53-65, advanced by
-- `serving_policy_store.advance_serving_policy_pointer` which refuses on a version mismatch).
-- `feature_active_revision` (1055) resolves "current" by newest-seq instead, and it is the ONLY
-- table that does — its header scopes that argument to publication rather than generalising it.
-- A realization family is a policy family, so it follows the eleven, not the one.
--
-- "NO V2 PATH WRITES THROUGH THE MUTABLE UPSERT" — the acceptance clause, and the upsert is
-- concrete: `eligibility_store.py:58-64` does
-- `ON CONFLICT (catalog_source, table_name) DO UPDATE SET …` with no version guard and no history,
-- so a policy is overwritten in place and the previous meaning is gone. Nothing below is reachable
-- from that path: realizations are append-only, the pointer is CAS-guarded, and the V2 store
-- imports nothing from `data_agent.eligibility_store`.
--
-- CONFLICT FINDINGS ARE RETAINED, including resolved ones. A realization that resolved a conflict
-- still HAD one, and dropping the finding destroys the only record that the question was ever open
-- — which is exactly what a later reviewer needs to see.
--
-- NOT APPLIED. This file is written, not run.

-- ── occurrences ──────────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS policy_occurrence_set (
    set_id       text PRIMARY KEY CHECK (btrim(set_id) <> ''),
    -- Which bound input set these occurrences were derived over. S3's table, so an occurrence can
    -- never name a binding that was made against no inventory.
    bound_input_set_revision_id text NOT NULL
                                REFERENCES bound_input_set_revision(revision_id),
    content_hash text NOT NULL CHECK (btrim(content_hash) <> ''),
    recorded_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS policy_occurrence (
    occurrence_hash  text PRIMARY KEY CHECK (btrim(occurrence_hash) <> ''),
    set_id           text NOT NULL REFERENCES policy_occurrence_set(set_id),

    -- Every part of the occurrence's own proof (C-C7). "This formula needs a reversal policy"
    -- cannot be verified against anything; these seven columns can.
    expr_path        text NOT NULL CHECK (btrim(expr_path) <> ''),
    policy_ref_field text NOT NULL CHECK (btrim(policy_ref_field) <> ''),
    policy_kind      text NOT NULL CHECK (btrim(policy_kind) <> ''),
    policy_ref       text NOT NULL CHECK (policy_ref LIKE '%:%'),
    semantic_role    text NOT NULL CHECK (btrim(semantic_role) <> ''),
    bound_dataset    text NOT NULL CHECK (btrim(bound_dataset) <> ''),
    bound_column     text NOT NULL,
    environment_id   text NOT NULL CHECK (btrim(environment_id) <> '')
);

CREATE INDEX IF NOT EXISTS policy_occurrence_by_set ON policy_occurrence (set_id);
CREATE INDEX IF NOT EXISTS policy_occurrence_by_ref ON policy_occurrence (policy_ref);

-- C-A3c's deferred gate lands here: PROVENANCE PINNED ON THE OCCURRENCE. `MeasureFact` already
-- names itself "the provenance an occurrence must pin", and the defect it exists to close is
-- specific: a per-row-currency monetary operand used to arrive looking NON-MONETARY with nothing
-- recorded, so the FX requirement could not fire and a mixed-currency population was summed in
-- silence. Recording the read is what turns that silence into a visible absence.
--
-- KEYED BY SET, not by occurrence alone. Two derivations of the same occurrence at different times
-- read a catalog that may have moved, and each pins what IT saw; keying on the occurrence would let
-- the second overwrite the first, which is the same in-place restatement this whole migration
-- refuses everywhere else.
--
-- `disposition = 'absent'` IS A POSITIVE STATEMENT — "nobody has decided this column's unit", which
-- is the ordinary case since most columns are not measures. It is stored, never skipped, because a
-- missing row and a recorded absence are the two things this table exists to keep apart.
CREATE TABLE IF NOT EXISTS policy_occurrence_measure_read (
    set_id                text NOT NULL REFERENCES policy_occurrence_set(set_id),
    occurrence_hash       text NOT NULL CHECK (btrim(occurrence_hash) <> ''),
    field                 text NOT NULL CHECK (field IN ('unit', 'currency')),

    -- '' exactly when the disposition is 'absent'. An UNREADABLE fact never reaches this table:
    -- `read_measure_facts` refuses first, which is the whole point of that seam.
    value                 text NOT NULL,
    disposition           text NOT NULL CHECK (disposition IN ('resolved', 'absent')),

    -- The verified-decision seam's own provenance, carried whole. Which decision, over which
    -- evidence, under which policy and resolver — without these, "resolved" is a word rather than
    -- something a later reader can re-derive.
    producer              text,
    strength              text,
    decision_event_id     text,
    selected_evidence_ids jsonb NOT NULL,
    policy_version        text NOT NULL,
    resolver_version      text,

    recorded_at           timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (set_id, occurrence_hash, field)
);

CREATE INDEX IF NOT EXISTS policy_occurrence_measure_read_by_occurrence
    ON policy_occurrence_measure_read (occurrence_hash);

-- ── realizations ─────────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS policy_realization_revision (
    revision_id             text PRIMARY KEY CHECK (btrim(revision_id) <> ''),

    -- C-C8's family key, frozen as five parts. `family_key_hash` is stored as well as its parts so
    -- the pointer below can key on one column while a reader can still see WHY two families differ.
    family_key_hash         text NOT NULL CHECK (btrim(family_key_hash) <> ''),
    policy_kind             text NOT NULL CHECK (btrim(policy_kind) <> ''),
    policy_ref              text NOT NULL CHECK (btrim(policy_ref) <> ''),
    bound_dataset           text NOT NULL CHECK (btrim(bound_dataset) <> ''),
    environment_id          text NOT NULL CHECK (btrim(environment_id) <> ''),
    semantic_role           text NOT NULL CHECK (btrim(semantic_role) <> ''),

    -- SEPARATE from revision_id, and the separation is load-bearing: two proposals that execute
    -- identically share this and never the id, so an LLM proposal cannot inherit a source's
    -- approval by agreeing with it.
    executable_content_hash text NOT NULL CHECK (btrim(executable_content_hash) <> ''),
    cas_pointer             text NOT NULL CHECK (btrim(cas_pointer) <> ''),
    provenance              text NOT NULL CHECK (provenance IN (
                                'source_derived', 'llm_proposed', 'human_authored')),
    recorded_at             timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT policy_realization_revision_id_is_not_content
        CHECK (revision_id <> executable_content_hash)
);

CREATE INDEX IF NOT EXISTS policy_realization_revision_by_family
    ON policy_realization_revision (family_key_hash);

-- What each revision ANSWERS. C-C8 created `realizes_occurrences` and this is its durable form:
-- without it a realization floats free of any reason to exist, and an occurrence nothing answers
-- could not be detected.
CREATE TABLE IF NOT EXISTS policy_realization_occurrence (
    revision_id     text NOT NULL REFERENCES policy_realization_revision(revision_id),
    occurrence_hash text NOT NULL CHECK (btrim(occurrence_hash) <> ''),
    PRIMARY KEY (revision_id, occurrence_hash)
);

CREATE INDEX IF NOT EXISTS policy_realization_occurrence_by_occurrence
    ON policy_realization_occurrence (occurrence_hash);

-- RETAINED, resolved or not.
CREATE TABLE IF NOT EXISTS policy_realization_conflict (
    revision_id text NOT NULL REFERENCES policy_realization_revision(revision_id),
    code        text NOT NULL CHECK (btrim(code) <> ''),
    detail      text NOT NULL,
    resolved    boolean NOT NULL,
    PRIMARY KEY (revision_id, code)
);

-- ── the current pointer, CAS-guarded (the house shape) ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS policy_realization_current (
    family_key_hash text PRIMARY KEY CHECK (btrim(family_key_hash) <> ''),
    revision_id     text NOT NULL REFERENCES policy_realization_revision(revision_id),
    pointer_version integer NOT NULL CHECK (pointer_version >= 1),
    -- WHO made this revision current. A byte-identical re-derivation REUSES the revision (identity
    -- is content), so without this the act of making it current would leave no trace of who did it
    -- — the same reason 1048:59-61 carries it.
    declared_by     text NOT NULL CHECK (btrim(declared_by) <> ''),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- ── append-only where it must be ─────────────────────────────────────────────────────────────────
-- The pointer is DELIBERATELY not covered: it is the one mutable row in this migration and it moves
-- only under a version check. Everything it points AT is immutable, which is what makes moving it
-- safe — the history is the revisions, never the pointer.
CREATE OR REPLACE FUNCTION s4_policy_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS policy_occurrence_set_no_update ON policy_occurrence_set;
CREATE TRIGGER policy_occurrence_set_no_update
    BEFORE UPDATE OR DELETE ON policy_occurrence_set
    FOR EACH ROW EXECUTE FUNCTION s4_policy_write_once();

DROP TRIGGER IF EXISTS policy_occurrence_no_update ON policy_occurrence;
CREATE TRIGGER policy_occurrence_no_update
    BEFORE UPDATE OR DELETE ON policy_occurrence
    FOR EACH ROW EXECUTE FUNCTION s4_policy_write_once();

DROP TRIGGER IF EXISTS policy_occurrence_measure_read_no_update ON policy_occurrence_measure_read;
CREATE TRIGGER policy_occurrence_measure_read_no_update
    BEFORE UPDATE OR DELETE ON policy_occurrence_measure_read
    FOR EACH ROW EXECUTE FUNCTION s4_policy_write_once();

DROP TRIGGER IF EXISTS policy_realization_revision_no_update ON policy_realization_revision;
CREATE TRIGGER policy_realization_revision_no_update
    BEFORE UPDATE OR DELETE ON policy_realization_revision
    FOR EACH ROW EXECUTE FUNCTION s4_policy_write_once();

DROP TRIGGER IF EXISTS policy_realization_conflict_no_update ON policy_realization_conflict;
CREATE TRIGGER policy_realization_conflict_no_update
    BEFORE UPDATE OR DELETE ON policy_realization_conflict
    FOR EACH ROW EXECUTE FUNCTION s4_policy_write_once();

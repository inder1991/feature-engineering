-- src/featuregen/db/migrations/1011_contract_pointer_model.sql
-- Delivery H (contract-version pointer model) — SCHEMA FOUNDATION ONLY. Delivery H rewrites feature
-- contract persistence from a mutate-the-feature model to an IMMUTABLE contract-version +
-- `feature_current_contract` pointer model. This migration lays the additive schema; NOTHING writes to
-- the new tables/columns here (the pointer-model write path is H2b, the reverse-dependency population is
-- H2c). It is STRICTLY ADDITIVE — no existing column is dropped or retyped, no data is rewritten, no
-- writer is added — so it can land safely AHEAD of the behavior change. `confirm_contract` is unchanged.
--
-- Idempotent in the repo style: ADD COLUMN IF NOT EXISTS, CREATE TABLE/INDEX IF NOT EXISTS, CREATE OR
-- REPLACE FUNCTION/TRIGGER, and every ALTER ... ADD CONSTRAINT guarded by a pg_constraint existence
-- check (mirrors 0972). The migration runner ledgers by checksum, but the raw SQL stays re-runnable so
-- the WORM-revoke branch can be re-applied in tests exactly as apply_migrations runs it.
--
-- REF-TYPE POLICY (matches the established codebase convention): stable-target links are real FKs;
-- historical / rebuildable / append-only-log refs stay plain text.
--   * FKs (stable targets): contract_id -> contract, feature_id -> feature, the composite
--     (feature_id, contract_id) -> contract, feature_versions.contract_id -> contract.
--   * TEXT (NOT FKs): graph_ref (graph rebuilds may drop nodes — brief), decision_id / fact_id /
--     event_id. field_decision_event (0981) is an APPEND-ONLY log whose supersession mints a NEW row
--     (no stable per-decision dimension key), overlay_fact_state (0507) is a MUTABLE projection that a
--     re-propose RESETS (M-9), and events are historical — so none is a stable FK target. This mirrors
--     0984 (graph_node.*_decision_id stored as plain text, NOT FKs) and 1007/1008 (snapshot/run refs
--     kept as strings, explicitly "NOT foreign keys ... mirrors how the codebase keeps historical
--     graph/run refs as strings, and avoids any insert-ordering hazard").

-- ---------------------------------------------------------------------------------------------------
-- 1) Additive columns on `contract` (all NULLABLE — no backfill, no writer yet, no CHECK).
--    metadata_snapshot_id is DELIBERATELY OMITTED: 1008 already added `contract.metadata_snapshot_id
--    text` (its immutable binding). generation_source carries NO CHECK constraint (H1 owns the enum:
--    recipe | llm_freeform | user_defined). initial_validation_status / initial_verification are the
--    at-confirm INITIAL stamp axis, SEPARATE from the mutable 1003 `validation_status` (which stays
--    unchanged) and the 0968/0973 hyphenated `verification`.
-- ---------------------------------------------------------------------------------------------------
ALTER TABLE contract ADD COLUMN IF NOT EXISTS metadata_input_fingerprint text;
ALTER TABLE contract ADD COLUMN IF NOT EXISTS generation_source          text;
ALTER TABLE contract ADD COLUMN IF NOT EXISTS recipe_id                  text;
ALTER TABLE contract ADD COLUMN IF NOT EXISTS physical_plan_id           text;
ALTER TABLE contract ADD COLUMN IF NOT EXISTS planner_declaration_id     text;
ALTER TABLE contract ADD COLUMN IF NOT EXISTS initial_validation_status  text;
ALTER TABLE contract ADD COLUMN IF NOT EXISTS initial_verification       text;

-- ---------------------------------------------------------------------------------------------------
-- 2) Orphan audit BEFORE relying on the contract.feature_id -> feature FK. Runs FIRST (before the
--    guarded FK re-statement below) so a re-apply against a DB whose FK was somehow dropped FAILS LOUD
--    with a remediation report rather than silently. It DELETES / REPARENTS NOTHING: a bank-grade
--    catalog never silently mutates governed rows — a human must remediate the orphan lineage. On a
--    consistent DB (the FK from 0972 has always held) this finds zero orphans and is a clean no-op.
-- ---------------------------------------------------------------------------------------------------
DO $$
DECLARE
    orphan_count int;
    orphan_ids   text;
BEGIN
    SELECT count(*), string_agg(c.contract_id, ', ' ORDER BY c.contract_id)
      INTO orphan_count, orphan_ids
      FROM contract c
      LEFT JOIN feature f ON f.feature_id = c.feature_id
     WHERE f.feature_id IS NULL;
    IF orphan_count > 0 THEN
        RAISE EXCEPTION
            'migration 1011 orphan audit FAILED: % contract row(s) reference a feature_id with no '
            'matching feature. REMEDIATE the source lineage (do NOT delete or reparent these rows) '
            'before applying 1011. Orphan contract_id(s): %', orphan_count, orphan_ids;
    END IF;
END $$;

-- contract.feature_id -> feature FK. Already installed by 0972 (contract_feature_id_fk); restated here
-- idempotently (as 0910 restates 0071's trigger) so this migration is self-consistent and the guard
-- makes it a no-op on the deployed DB. Only added AFTER the orphan audit above passes.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'contract_feature_id_fk') THEN
        ALTER TABLE contract ADD CONSTRAINT contract_feature_id_fk
            FOREIGN KEY (feature_id) REFERENCES feature (feature_id);
    END IF;
END $$;

-- Composite UNIQUE (feature_id, contract_id) — the target `feature_current_contract` (below) references
-- with its composite FK, so a contract can NEVER become current for a feature it does not belong to.
-- contract_id is already globally unique (PRIMARY KEY), so this is trivially satisfiable on any row.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'contract_feature_contract_unique') THEN
        ALTER TABLE contract ADD CONSTRAINT contract_feature_contract_unique
            UNIQUE (feature_id, contract_id);
    END IF;
END $$;

-- ---------------------------------------------------------------------------------------------------
-- 3) contract_input_column — the resolved input items a contract version was built from (physically
--    write-once). Populated in H2b; created here. graph_ref is a historical string (nullable — graph
--    rebuilds may drop nodes); decision_id / fact_id are plain text per the ref-type policy above.
-- ---------------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS contract_input_column (
    contract_id  text        NOT NULL REFERENCES contract (contract_id),
    source       text        NOT NULL,   -- catalog / source of the input
    graph_ref    text        NULL,       -- historical graph node ref (string, NOT an FK)
    logical_ref  text        NULL,
    physical_ref text        NULL,
    role         text        NULL,       -- measure | entity | time | grain | join | support | ...
    decision_id  text        NULL,       -- field-decision ref (text; see ref-type policy)
    fact_id      text        NULL,       -- governed-fact ref (text; see ref-type policy)
    item_hash    text        NOT NULL,   -- content hash of this input item
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (contract_id, item_hash)
);
CREATE INDEX IF NOT EXISTS contract_input_column_contract_idx
    ON contract_input_column (contract_id);

CREATE OR REPLACE FUNCTION contract_input_column_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'contract_input_column records are write-once: % not allowed on '
        '(contract_id=%, item_hash=%)',
        TG_OP, COALESCE(OLD.contract_id, NEW.contract_id), COALESCE(OLD.item_hash, NEW.item_hash);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER contract_input_column_no_mutation
    BEFORE UPDATE OR DELETE ON contract_input_column
    FOR EACH ROW EXECUTE FUNCTION contract_input_column_write_once();

-- ---------------------------------------------------------------------------------------------------
-- 4) contract_metadata_dependency — the reverse dependency rows (which catalog metadata a contract
--    version depends on), for drift-impact fan-out (physically write-once). Populated in H2c; created
--    here. Same ref-type policy: graph_ref / decision_id / fact_id / event_id are plain text.
-- ---------------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS contract_metadata_dependency (
    contract_id    text        NOT NULL REFERENCES contract (contract_id),
    catalog_source text        NOT NULL,
    graph_ref      text        NULL,     -- historical graph node ref (string, NOT an FK)
    logical_ref    text        NULL,
    decision_id    text        NULL,     -- field-decision ref (text; see ref-type policy)
    fact_id        text        NULL,     -- governed-fact ref (text; see ref-type policy)
    event_id       text        NULL,     -- source event ref (text; see ref-type policy)
    item_hash      text        NOT NULL, -- content hash of this dependency item
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (contract_id, item_hash)
);
CREATE INDEX IF NOT EXISTS contract_metadata_dependency_contract_idx
    ON contract_metadata_dependency (contract_id);

CREATE OR REPLACE FUNCTION contract_metadata_dependency_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'contract_metadata_dependency records are write-once: % not allowed on '
        '(contract_id=%, item_hash=%)',
        TG_OP, COALESCE(OLD.contract_id, NEW.contract_id), COALESCE(OLD.item_hash, NEW.item_hash);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER contract_metadata_dependency_no_mutation
    BEFORE UPDATE OR DELETE ON contract_metadata_dependency
    FOR EACH ROW EXECUTE FUNCTION contract_metadata_dependency_write_once();

-- ---------------------------------------------------------------------------------------------------
-- 5) feature_current_contract — the pointer from a registered feature to its CURRENT contract version.
--    This is the MUTABLE CAS target (optimistic concurrency via pointer_version): DELIBERATELY NOT
--    write-once (no no_mutation trigger, no revoke) — repointing to a new version is an in-place
--    UPDATE. The composite FK (feature_id, contract_id) -> contract's composite UNIQUE guarantees the
--    contract genuinely belongs to that feature; the single feature_id -> feature FK keeps the feature
--    real even before any contract is written.
-- ---------------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS feature_current_contract (
    feature_id      text        PRIMARY KEY REFERENCES feature (feature_id),
    contract_id     text        NOT NULL,
    pointer_version integer     NOT NULL,   -- CAS / optimistic-concurrency guard
    set_at          timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (feature_id, contract_id)
        REFERENCES contract (feature_id, contract_id)
);

-- ---------------------------------------------------------------------------------------------------
-- 6) feature_versions.contract_id — nullable FK -> contract (feature_versions exists, 0060). ADD COLUMN
--    is DDL (not a row UPDATE), so the 0060 feature_versions_no_mutation write-once trigger does not
--    fire; existing rows carry NULL. FK guarded like 0972.
-- ---------------------------------------------------------------------------------------------------
ALTER TABLE feature_versions ADD COLUMN IF NOT EXISTS contract_id text NULL;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'feature_versions_contract_id_fk') THEN
        ALTER TABLE feature_versions ADD CONSTRAINT feature_versions_contract_id_fk
            FOREIGN KEY (contract_id) REFERENCES contract (contract_id);
    END IF;
END $$;

-- ---------------------------------------------------------------------------------------------------
-- 7) WORM TRUNCATE guard (copy 0900/1001/1002/1009 exactly). The BEFORE UPDATE OR DELETE row triggers
--    above are FOR EACH ROW and, like every row trigger, do NOT fire on a statement-level TRUNCATE — so
--    revoke destructive DML on the two write-once tables from the production non-superuser role. Guarded
--    (no-op in the superuser test cluster where the role is absent; a superuser bypasses grants anyway,
--    so this is a DEPLOYMENT control). feature_current_contract is DELIBERATELY EXCLUDED — it is the
--    mutable pointer, and `contract` itself is untouched (its no-mutation posture is H2c/H2d territory).
-- ---------------------------------------------------------------------------------------------------
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'featuregen_app') THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON contract_input_column FROM featuregen_app;
        REVOKE UPDATE, DELETE, TRUNCATE ON contract_metadata_dependency FROM featuregen_app;
    END IF;
END $$;

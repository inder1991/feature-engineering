-- src/featuregen/db/migrations/1134_identity_persistence.sql
--
-- Cross-catalog serving plan (2026-08-24, rev 13) task B1: PERSISTENCE for the three-layer
-- identity contracts step 3 built as code-only types, plus the composed digests of the seven-stage
-- identity chain. Seven tables, all append-only, all content-addressed:
--
--   logical_feature_plan_revision      LogicalFeaturePlanV2  -> logical_digest      (R9: MEANING)
--   logical_plan_provenance_record     LogicalPlanProvenanceV1 side-car (NEVER hashed into
--                                      identity; many per plan — see below)
--   physical_execution_plan_revision   PhysicalExecutionPlanV1 -> physical_digest   (R2: HOW)
--   render_profile_revision            RenderProfileV1 -> render_profile_digest
--   generation_configuration_revision  GenerationConfigurationV1 -> generation_configuration_digest
--   member_output_contract_revision    MemberOutputContractV1 (build-scoped configuration owns NO
--                                      per-feature output field; this table is its single owner)
--   identity_digest_record             one row per COMPOSED stage of the chain
--
-- RESERVATION. 1130-1139 were assigned to this plan by T0 at live head 1121 (1122-1129 belong to
-- the remediation program's registry; 1117 stays reserved-unused). The §T0 mapping row for 1134 is
-- "identity persistence: logical-plan, physical-plan, render-profile revisions;
-- GenerationConfigurationV1 + MemberOutputContractV1; digest-chain records" (task B1). Migration
-- files apply lexically and are checksummed by the ledger — immutable once merged or applied
-- anywhere, INCLUDING the documented persistent-FEATUREGEN_TEST_DSN mode. 1130 (execution context)
-- and 1131 (dependency snapshot) precede this file lexically, so the context revisions the
-- physical-plan rows name already have their store.
--
-- THE DIGEST COLUMN IS THE CONTENT HASH, BY CONSTRUCTION. Every layer's digest function in
-- `planner/identity_chain.py` is exactly `materialize_hash(contract.content_payload())`, so the
-- published join point (`logical_digest`, `physical_digest`, `render_profile_digest`,
-- `generation_configuration_digest`) and the family's `content_hash` are the SAME value. Both
-- columns exist because consumers join on the published NAME (C3 keys a card's
-- `semantic_feature_id` on `logical_digest`), and a named CHECK pins them equal so the join point
-- can never become a second, independently-writable value that drifts from the content it claims
-- to summarize — the defect this whole chain exists to prevent. A second named CHECK pins
-- `revision_id = '<prefix>' || content_hash`, so an id cannot address content it does not derive
-- from. The behavioural half lives in the store: every load RECONSTRUCTS the typed contract from
-- the stored payload, RECOMPUTES the digest through step 3's own function, and refuses a row that
-- cannot reproduce its own identity.
--
-- MemberOutputContractV1 carries NO digest column: the identity chain gives it no named digest of
-- its own — it rides `member_execution_input_digest`. Its `content_hash` is its identity, and
-- honest absence beats inventing a fifth digest name nothing consumes.
--
-- PROVENANCE IS A SEPARATE TABLE, NOT A COLUMN (R9's staleness law). Hypothesis text, planning
-- request hash, chooser revision, menu hash and display text never enter a digest, so the SAME
-- feature reached from a DIFFERENT hypothesis is one plan row. A provenance column would silently
-- keep only the first writer's hypothesis (the content-addressed insert is ON CONFLICT DO
-- NOTHING); an append-only side table keeps every one of them. The record's identity is the PAIR
-- (revision_id, provenance payload) — the same hypothesis recorded against two different plans is
-- two records — which is why `content_hash` covers the revision_id too.
--
-- THE DIGEST-CHAIN TABLE. One row per composed stage: `stage` (closed vocabulary, matching the
-- store's constants), `inputs` (the exact named inputs, canonical and self-contained so the digest
-- can be recomputed with NO further reads) and `digest` (what the stage function returned). The
-- store never accepts a digest from a caller — it computes it — so drift is impossible at write;
-- the load recomputes and refuses on disagreement, which is what catches a row written by hand.
-- UNIQUE (stage, digest) makes the chain walkable BACKWARDS: a digest resolves to exactly one
-- input record, never two rival input sets claiming one identity.
--
-- FOREIGN KEYS — DECIDED, NOT DEFAULTED (A4's platform discovery, applied a third time). Postgres
-- refuses to TRUNCATE a table referenced by a FK BEFORE any BEFORE TRUNCATE trigger fires, so an
-- FK onto an append-only table replaces that table's append-only refusal with a foreign-key one
-- and the guard stops proving itself. EVERY table here is an append-only identity, so NOTHING here
-- carries a foreign key — not `physical_execution_plan_revision.logical_digest` onto
-- `logical_feature_plan_revision`, not `.execution_context_revision_id` onto 1130's table, not
-- `logical_plan_provenance_record.revision_id` onto its plan, not the digest-chain inputs onto the
-- layers they compose. In place of each, the store LOADS AND CONTENT-VERIFIES the referenced row
-- before it writes (A4's "a snapshot pins revisions that exist, never ones it would have to
-- invent"), and append-only rows never disappear, so a verified reference cannot decay.
-- `join_validation_policy_revision_id` is stored as a scalar with NO existence check at all: the
-- JoinValidationPolicyRevisionV1 store is 1136's (task B2/B2b) and does not exist yet — an honest
-- unchecked pin now, tightened when its store lands, never a fabricated FK.
--
-- SCALARS BESIDE THE JSONB (the 1131 convention) so B2's binding chain can ask relational
-- questions without opening a payload: which physical plans realize this logical identity, and
-- which of them were adopted in this execution context.

CREATE TABLE IF NOT EXISTS logical_feature_plan_revision (
    revision_id    text        PRIMARY KEY,
    -- R9's identity and C3's future semantic_feature_id. Equal to content_hash by CHECK.
    logical_digest text        NOT NULL,
    -- LogicalFeaturePlanV2.content_payload() — MEANING only; the provenance side-car is absent.
    content        jsonb       NOT NULL,
    content_hash   text        NOT NULL,
    recorded_at    timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT logical_feature_plan_revision_hash_chk CHECK (
        content_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT logical_feature_plan_revision_digest_chk CHECK (
        logical_digest = content_hash),
    CONSTRAINT logical_feature_plan_revision_id_chk CHECK (
        revision_id = 'lfp_' || content_hash),
    CONSTRAINT logical_feature_plan_revision_content_chk CHECK (
        jsonb_typeof(content) = 'object'),
    CONSTRAINT logical_feature_plan_revision_content_hash_key UNIQUE (content_hash)
);

CREATE TABLE IF NOT EXISTS logical_plan_provenance_record (
    provenance_id text        PRIMARY KEY,
    -- The plan this provenance was recorded against. NO FK (see header); the store verifies.
    revision_id   text        NOT NULL,
    -- LogicalPlanProvenanceV1: hypothesis, planning-request hash, chooser revision, menu hash,
    -- display text. Recorded and displayed; NEVER identity material.
    content       jsonb       NOT NULL,
    content_hash  text        NOT NULL,
    recorded_at   timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT logical_plan_provenance_record_hash_chk CHECK (
        content_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT logical_plan_provenance_record_id_chk CHECK (
        provenance_id = 'lpp_' || content_hash),
    CONSTRAINT logical_plan_provenance_record_revision_chk CHECK (
        revision_id ~ '^lfp_[0-9a-f]{64}$'),
    CONSTRAINT logical_plan_provenance_record_content_chk CHECK (
        jsonb_typeof(content) = 'object'),
    CONSTRAINT logical_plan_provenance_record_content_hash_key UNIQUE (content_hash)
);

CREATE INDEX IF NOT EXISTS logical_plan_provenance_record_revision_idx
    ON logical_plan_provenance_record (revision_id);

CREATE TABLE IF NOT EXISTS physical_execution_plan_revision (
    revision_id                        text        PRIMARY KEY,
    physical_digest                    text        NOT NULL,
    -- The logical identity this realization serves — a VALUE, pinned, never a back-reference that
    -- could re-aim the meaning (R2: a physical plan may change without touching the feature).
    logical_digest                     text        NOT NULL,
    execution_context_revision_id      text        NOT NULL,
    -- The exact guard-policy revision (1136's store, task B2/B2b): stored, not yet checkable.
    join_validation_policy_revision_id text        NOT NULL,
    content                            jsonb       NOT NULL,
    content_hash                       text        NOT NULL,
    recorded_at                        timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT physical_execution_plan_revision_hash_chk CHECK (
        content_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT physical_execution_plan_revision_digest_chk CHECK (
        physical_digest = content_hash),
    CONSTRAINT physical_execution_plan_revision_id_chk CHECK (
        revision_id = 'pxp_' || content_hash),
    CONSTRAINT physical_execution_plan_revision_logical_chk CHECK (
        logical_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT physical_execution_plan_revision_context_chk CHECK (
        btrim(execution_context_revision_id) <> ''),
    CONSTRAINT physical_execution_plan_revision_policy_chk CHECK (
        btrim(join_validation_policy_revision_id) <> ''),
    CONSTRAINT physical_execution_plan_revision_content_chk CHECK (
        jsonb_typeof(content) = 'object'),
    CONSTRAINT physical_execution_plan_revision_content_hash_key UNIQUE (content_hash)
);

CREATE INDEX IF NOT EXISTS physical_execution_plan_revision_logical_idx
    ON physical_execution_plan_revision (logical_digest);
CREATE INDEX IF NOT EXISTS physical_execution_plan_revision_context_idx
    ON physical_execution_plan_revision (execution_context_revision_id);

CREATE TABLE IF NOT EXISTS render_profile_revision (
    revision_id           text        PRIMARY KEY,
    render_profile_digest text        NOT NULL,
    content               jsonb       NOT NULL,
    content_hash          text        NOT NULL,
    recorded_at           timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT render_profile_revision_hash_chk CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT render_profile_revision_digest_chk CHECK (render_profile_digest = content_hash),
    CONSTRAINT render_profile_revision_id_chk CHECK (revision_id = 'rpf_' || content_hash),
    CONSTRAINT render_profile_revision_content_chk CHECK (jsonb_typeof(content) = 'object'),
    CONSTRAINT render_profile_revision_content_hash_key UNIQUE (content_hash)
);

CREATE TABLE IF NOT EXISTS generation_configuration_revision (
    revision_id                     text        PRIMARY KEY,
    generation_configuration_digest text        NOT NULL,
    content                         jsonb       NOT NULL,
    content_hash                    text        NOT NULL,
    recorded_at                     timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT generation_configuration_revision_hash_chk CHECK (
        content_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT generation_configuration_revision_digest_chk CHECK (
        generation_configuration_digest = content_hash),
    CONSTRAINT generation_configuration_revision_id_chk CHECK (
        revision_id = 'gcf_' || content_hash),
    CONSTRAINT generation_configuration_revision_content_chk CHECK (
        jsonb_typeof(content) = 'object'),
    CONSTRAINT generation_configuration_revision_content_hash_key UNIQUE (content_hash)
);

CREATE TABLE IF NOT EXISTS member_output_contract_revision (
    revision_id  text        PRIMARY KEY,
    content      jsonb       NOT NULL,
    content_hash text        NOT NULL,
    recorded_at  timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT member_output_contract_revision_hash_chk CHECK (
        content_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT member_output_contract_revision_id_chk CHECK (
        revision_id = 'moc_' || content_hash),
    CONSTRAINT member_output_contract_revision_content_chk CHECK (
        jsonb_typeof(content) = 'object'),
    CONSTRAINT member_output_contract_revision_content_hash_key UNIQUE (content_hash)
);

CREATE TABLE IF NOT EXISTS identity_digest_record (
    digest_id    text        PRIMARY KEY,
    -- The chain stage, closed. Widening it means widening this CHECK, which is a NEW migration
    -- and therefore a review gate.
    stage        text        NOT NULL,
    -- What the stage function returned over `inputs`.
    digest       text        NOT NULL,
    -- The exact named inputs, canonical and SELF-CONTAINED: the digest is recomputable from this
    -- object alone, so a stored digest can always be checked against what it claims to summarize.
    inputs       jsonb       NOT NULL,
    content_hash text        NOT NULL,
    recorded_at  timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT identity_digest_record_hash_chk CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT identity_digest_record_digest_chk CHECK (digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT identity_digest_record_id_chk CHECK (digest_id = 'idg_' || content_hash),
    CONSTRAINT identity_digest_record_stage_chk CHECK (stage IN (
        'formula_binding',
        'member_execution_input',
        'member_compile',
        'build_compilation',
        'sealed_artifact')),
    CONSTRAINT identity_digest_record_inputs_chk CHECK (jsonb_typeof(inputs) = 'object'),
    CONSTRAINT identity_digest_record_content_hash_key UNIQUE (content_hash),
    -- The chain walks backwards: one digest, exactly one input record.
    CONSTRAINT identity_digest_record_stage_digest_key UNIQUE (stage, digest)
);

-- ── append-only guards ────────────────────────────────────────────────────────────────────────
-- ONE raiser for all seven tables (they are one identity substrate, delivered together and
-- refusing for one reason). It names the offending table through TG_TABLE_NAME and the operation
-- through TG_OP; both are assigned in statement-level TRUNCATE triggers, while OLD/NEW are NOT,
-- which is why the raiser touches neither.
CREATE OR REPLACE FUNCTION identity_persistence_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        '% is append-only: % is not allowed. These rows are content-addressed identities that '
        'plans, bindings, builds and sealed artifacts pin BY ID — rewriting one would silently '
        're-aim everything that pinned it. Record a NEW row instead.', TG_TABLE_NAME, TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER logical_feature_plan_revision_no_mutation
    BEFORE UPDATE OR DELETE ON logical_feature_plan_revision
    FOR EACH ROW EXECUTE FUNCTION identity_persistence_append_only();
-- A FOR EACH ROW trigger does not fire on TRUNCATE; the statement-level guard is the only one
-- that does (and no FK anywhere lets Postgres refuse the TRUNCATE before it runs).
CREATE OR REPLACE TRIGGER logical_feature_plan_revision_no_truncate
    BEFORE TRUNCATE ON logical_feature_plan_revision
    FOR EACH STATEMENT EXECUTE FUNCTION identity_persistence_append_only();

CREATE OR REPLACE TRIGGER logical_plan_provenance_record_no_mutation
    BEFORE UPDATE OR DELETE ON logical_plan_provenance_record
    FOR EACH ROW EXECUTE FUNCTION identity_persistence_append_only();
CREATE OR REPLACE TRIGGER logical_plan_provenance_record_no_truncate
    BEFORE TRUNCATE ON logical_plan_provenance_record
    FOR EACH STATEMENT EXECUTE FUNCTION identity_persistence_append_only();

CREATE OR REPLACE TRIGGER physical_execution_plan_revision_no_mutation
    BEFORE UPDATE OR DELETE ON physical_execution_plan_revision
    FOR EACH ROW EXECUTE FUNCTION identity_persistence_append_only();
CREATE OR REPLACE TRIGGER physical_execution_plan_revision_no_truncate
    BEFORE TRUNCATE ON physical_execution_plan_revision
    FOR EACH STATEMENT EXECUTE FUNCTION identity_persistence_append_only();

CREATE OR REPLACE TRIGGER render_profile_revision_no_mutation
    BEFORE UPDATE OR DELETE ON render_profile_revision
    FOR EACH ROW EXECUTE FUNCTION identity_persistence_append_only();
CREATE OR REPLACE TRIGGER render_profile_revision_no_truncate
    BEFORE TRUNCATE ON render_profile_revision
    FOR EACH STATEMENT EXECUTE FUNCTION identity_persistence_append_only();

CREATE OR REPLACE TRIGGER generation_configuration_revision_no_mutation
    BEFORE UPDATE OR DELETE ON generation_configuration_revision
    FOR EACH ROW EXECUTE FUNCTION identity_persistence_append_only();
CREATE OR REPLACE TRIGGER generation_configuration_revision_no_truncate
    BEFORE TRUNCATE ON generation_configuration_revision
    FOR EACH STATEMENT EXECUTE FUNCTION identity_persistence_append_only();

CREATE OR REPLACE TRIGGER member_output_contract_revision_no_mutation
    BEFORE UPDATE OR DELETE ON member_output_contract_revision
    FOR EACH ROW EXECUTE FUNCTION identity_persistence_append_only();
CREATE OR REPLACE TRIGGER member_output_contract_revision_no_truncate
    BEFORE TRUNCATE ON member_output_contract_revision
    FOR EACH STATEMENT EXECUTE FUNCTION identity_persistence_append_only();

CREATE OR REPLACE TRIGGER identity_digest_record_no_mutation
    BEFORE UPDATE OR DELETE ON identity_digest_record
    FOR EACH ROW EXECUTE FUNCTION identity_persistence_append_only();
CREATE OR REPLACE TRIGGER identity_digest_record_no_truncate
    BEFORE TRUNCATE ON identity_digest_record
    FOR EACH STATEMENT EXECUTE FUNCTION identity_persistence_append_only();

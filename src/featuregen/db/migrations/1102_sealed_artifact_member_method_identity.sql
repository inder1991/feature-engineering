-- src/featuregen/db/migrations/1102_sealed_artifact_member_method_identity.sql
-- WHICH EXACT METHOD authored each member — the thing a production certificate is matched against.
--
-- WHY 1099 IS NOT ENOUGH. `sealed_artifact_member_provenance` records the authoring METHOD, derived
-- from evidence, per member. That answers *which of two methods*. A certificate certifies more than
-- a category: it covers a MODEL, a pair of frozen author/critic contracts, a grammar and a schema
-- version — or a blueprint revision and the expectation it stood on. Matching a certificate against
-- "LLM_AUTHORED" is matching on the wrong thing, and it would pass for a model the platform never
-- evaluated.
--
-- ▲ EXTEND, NEVER ALTER. 1099 is applied and carries a BEFORE UPDATE OR DELETE trigger that raises,
-- so `ADD COLUMN` there would succeed and then be unfillable — the backfill is an UPDATE. A
-- companion table is the only shape that can carry this.
--
-- ▲ THE IDENTITY CANNOT DISAGREE WITH THE PROVENANCE BESIDE IT. `authoring_method` is repeated here
-- and travels inside the composite foreign key, so a row claiming an LLM identity cannot attach to a
-- member whose evidence established a reviewed blueprint. Two independent columns describing one
-- fact is the defect 1101 exists to forbid one level up; repeating it here would be the same
-- mistake with a different pair of tables.
--
-- ▲ WHERE THE PAYLOAD COMES FROM, verified rather than assumed. The production authoring lane
-- freezes NO configuration per run — `frozen_configuration_json` has exactly one writer and it is
-- the SHADOW evaluation lane. The evidence lives in `llm_dispatch`, which carries `provider`,
-- `model`, `provider_contract_hash` and `call_role` per call, and in the run's REVIEW_BYPASSED trace
-- event, whose payload names `blueprint_revision` and `expectation_hash`. That is BETTER than a
-- frozen configuration for this purpose: it records what was ACTUALLY SENT rather than what
-- configuration happened to be current. Deriving from `current_formula_generation_settings()` at
-- sealing time would be 1099's own backfill error relocated — today's evidence, yesterday's bytes.

-- ▲ THE KEY THE COMPOSITE REFERENCE NEEDS, on 1099's table. A superset of its primary key, so it
-- is unique by construction and cannot fail on existing rows — and adding an INDEX to an
-- append-only table is safe: 1099's guard fires on row UPDATE/DELETE, not on DDL.
CREATE UNIQUE INDEX IF NOT EXISTS sealed_artifact_member_provenance_method_key
    ON sealed_artifact_member_provenance (artifact_id, member_name, authoring_method);

CREATE TABLE IF NOT EXISTS sealed_artifact_member_method_identity (
    artifact_id          text NOT NULL CHECK (btrim(artifact_id) <> ''),
    member_name          text NOT NULL CHECK (btrim(member_name) <> ''),

    -- Repeated from 1099 SO IT CAN BE CONSTRAINED, not for convenience. See the header.
    authoring_method     text NOT NULL CHECK (authoring_method IN (
                             'LLM_AUTHORED', 'REVIEWED_RECIPE_BLUEPRINT')),

    -- The hash a certificate is matched against.
    method_identity_hash text NOT NULL CHECK (btrim(method_identity_hash) <> ''),

    -- ▲ INSPECTABLE, so the hash is CHECKABLE. A bare hash can only ever be compared; when a
    -- production act refuses on a mismatch, the next question is always "which field moved", and a
    -- payload nobody can read makes that unanswerable.
    method_identity_json jsonb NOT NULL,

    -- Provenance, and therefore outside every identity here — 1099's rule.
    derived_at           timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (artifact_id, member_name),

    CONSTRAINT sealed_artifact_member_method_identity_agrees_with_provenance
        FOREIGN KEY (artifact_id, member_name, authoring_method)
        REFERENCES sealed_artifact_member_provenance (artifact_id, member_name, authoring_method)
);

CREATE INDEX IF NOT EXISTS sealed_artifact_member_method_identity_by_hash
    ON sealed_artifact_member_method_identity (method_identity_hash);

-- ▲ APPEND-ONLY, the same guard as 1099. A method identity that can be edited after sealing is a
-- certificate match that can be arranged afterwards.
CREATE OR REPLACE FUNCTION sealed_artifact_member_method_identity_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'sealed_artifact_member_method_identity is append-only: it records how a sealed '
                    'member was authored, and a record that can be rewritten cannot be matched '
                    'against a certificate';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS sealed_artifact_member_method_identity_no_change
    ON sealed_artifact_member_method_identity;
CREATE TRIGGER sealed_artifact_member_method_identity_no_change
    BEFORE UPDATE OR DELETE ON sealed_artifact_member_method_identity
    FOR EACH ROW EXECUTE FUNCTION sealed_artifact_member_method_identity_write_once();

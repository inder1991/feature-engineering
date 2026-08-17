-- src/featuregen/db/migrations/1077_materialization_group_membership.sql
-- S6 (docs/superpowers/plans/2026-08-15-integration-consolidation.md): the V2 group plan and its
-- MEMBERSHIP, so "which features does this group publish" is a query rather than a re-derivation.
--
-- WHY THIS IS AN ACCEPTANCE CLAUSE AND NOT A CONVENIENCE. Until now both directions —
-- group→features and feature→group — could only be answered by re-running the compilation. A
-- question answered by re-deriving is a question answered DIFFERENTLY once the inputs move, which
-- is the one thing a membership record must never do. The rows are written from the PLAN, so what
-- is queryable is what was planned rather than what someone believed was planned.
--
-- ENVIRONMENT-SCOPED, per F3. The group's key is at least `(environment_id, logical_group_name)`.
-- Environment is DEPLOYMENT PLACEMENT, not feature meaning, so it must not be folded into the
-- group name — and it has to appear in the uniqueness constraint and in every query, or two
-- environments publishing the same logical group silently share one membership. The unique index
-- below is on the triple, not the pair, because one logical group in one environment legitimately
-- has a HISTORY of plans: a new plan hash is a new revision of the same group, not a collision.
--
-- NO `feature_name` COLUMN, deliberately. A feature's published column name IS its name in a plan —
-- `build_group_plan` compares `group.feature_names` against the planned column names — so a second
-- column holding the same string could only ever agree or be wrong.
--
-- `ir_hash` IS ON THE MEMBER, because a membership that could not say which compiled plan produces
-- a column would answer the forward question with names nobody can trace back. It comes from the
-- plan's own `PlannedFeature`, which already refuses a blank one.
--
-- NOT APPLIED. This file is written, not run.

CREATE TABLE IF NOT EXISTS materialization_group_v2 (
    group_plan_hash               text PRIMARY KEY CHECK (btrim(group_plan_hash) <> ''),

    environment_id                text NOT NULL CHECK (btrim(environment_id) <> ''),
    logical_group_name            text NOT NULL CHECK (btrim(logical_group_name) <> ''),
    materialization_contract_hash text NOT NULL CHECK (
                                      btrim(materialization_contract_hash) <> ''),

    entity_key_columns            text[] NOT NULL CHECK (cardinality(entity_key_columns) > 0),
    business_dt_column            text NOT NULL CHECK (btrim(business_dt_column) <> ''),

    -- A NAMED rule set, never V1's ordinal. An ordinal identifies a counter's position, not the
    -- rules a type was decided under, and the two are indistinguishable once persisted as a string.
    physical_type_policy          text NOT NULL CHECK (
                                      physical_type_policy ~ '^[a-z0-9][a-z0-9-]*(/[a-z0-9][a-z0-9-]*)+@[0-9]+$'),

    recorded_at                   timestamptz NOT NULL DEFAULT now()
);

-- The forward question's index, on the PAIR — never on the name alone.
CREATE INDEX IF NOT EXISTS materialization_group_v2_by_environment_and_name
    ON materialization_group_v2 (environment_id, logical_group_name);

CREATE UNIQUE INDEX IF NOT EXISTS materialization_group_v2_identity
    ON materialization_group_v2 (environment_id, logical_group_name, group_plan_hash);

CREATE TABLE IF NOT EXISTS materialization_group_member (
    group_plan_hash text NOT NULL REFERENCES materialization_group_v2(group_plan_hash),
    column_name     text NOT NULL CHECK (btrim(column_name) <> ''),
    ir_hash         text NOT NULL CHECK (btrim(ir_hash) <> ''),

    -- One column, published once. Two rows would make which IR produces it a row-order accident.
    PRIMARY KEY (group_plan_hash, column_name)
);

-- The reverse question: which groups publish this column.
CREATE INDEX IF NOT EXISTS materialization_group_member_by_column
    ON materialization_group_member (column_name);

-- ── append-only ──────────────────────────────────────────────────────────────────────────────────
-- A group plan is content-addressed and its membership is what a published table's columns mean.
-- Editing either would restate what a sealed compilation publishes without the compilation changing.
CREATE OR REPLACE FUNCTION s6_group_membership_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS materialization_group_v2_no_update ON materialization_group_v2;
CREATE TRIGGER materialization_group_v2_no_update
    BEFORE UPDATE OR DELETE ON materialization_group_v2
    FOR EACH ROW EXECUTE FUNCTION s6_group_membership_write_once();

DROP TRIGGER IF EXISTS materialization_group_member_no_update ON materialization_group_member;
CREATE TRIGGER materialization_group_member_no_update
    BEFORE UPDATE OR DELETE ON materialization_group_member
    FOR EACH ROW EXECUTE FUNCTION s6_group_membership_write_once();

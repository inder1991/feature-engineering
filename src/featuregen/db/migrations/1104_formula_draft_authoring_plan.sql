-- src/featuregen/db/migrations/1104_formula_draft_authoring_plan.sql
-- WHICH METHOD WAS CHOSEN FOR THIS DRAFT, and the evidence that chose it — decided once, before
-- authoring, and never recomputed.
--
-- WHY IT IS FOUNDATION RATHER THAN STEP-4 WORK. Identity V2 folds `formula_strategy` and
-- `strategy_identity_hash` into the draft identity. An earlier sequence created those facts in a
-- later step while requiring identity V2 in an earlier one — step 2 needed step 4's facts, and step
-- 4 could not begin until step 2 finished. An identity that cannot be composed without strategy
-- facts is telling you the strategy contract IS foundation.
--
-- ▲ RE-READ, NEVER RECOMPUTED. The worker reads this row; it does not resolve the strategy again.
-- A registry or a review can move between the request and the work, and a second resolution would
-- silently author under a method nobody chose — while the draft identity, which folded the FIRST
-- answer, still claimed the first one.
--
-- ▲ AND THE STRATEGY IS NEVER A CLIENT LABEL. The client may ask for an LLM retry after a
-- deterministic refusal, but it does that by creating a server-authored override revision the
-- resolver consumes as evidence (parent §11.3). Nothing here is written from a request body.

CREATE TABLE IF NOT EXISTS formula_draft_authoring_plan (
    formula_draft_id            text PRIMARY KEY REFERENCES formula_draft(formula_draft_id),

    -- WHERE THE IDEA CAME FROM. Deliberately separate from the method below: a recipe-origin
    -- feature may be LLM-authored, and today every one is. Migration 1088's CHECK tied
    -- origin='recipe' to a reviewed blueprint revision, which made the required fallback —
    -- "recipe recommendation, LLM-authored formula" — UNREPRESENTABLE in the database.
    candidate_origin            text NOT NULL CHECK (candidate_origin IN (
                                    'recipe', 'llm_intent', 'user_definition')),

    -- HOW THE FORMULA WAS PRODUCED.
    formula_strategy            text NOT NULL CHECK (formula_strategy IN (
                                    'REVIEWED_RECIPE_BLUEPRINT', 'LLM_AUTHORED')),

    -- The evidence that chose it, hashed — folded into identity V2, so an under-specified payload
    -- is an under-specified draft identity.
    strategy_identity_hash      text NOT NULL CHECK (btrim(strategy_identity_hash) <> ''),

    recipe_id                   text,
    recipe_revision_hash        text,

    -- ▲ NEVER THE RECIPE ID. The reviewed registry's own docstring: "The key is an EXPECTATION REF,
    -- never a recipe id. 295 of the 317 registry recipes declare a ref that is not their own name."
    expectation_ref             text,
    expectation_generation      text CHECK (expectation_generation IN ('v1', 'v2')),

    reviewed_blueprint_revision text,
    reviewed_blueprint_hash     text,

    provider_contract_hash      text,

    -- The override that produced an LLM retry, when there was one. Nullable: most LLM drafts are
    -- the ordinary route, not a retry after a deterministic refusal.
    method_override_revision_id text,

    planned_at                  timestamptz NOT NULL DEFAULT now(),

    -- ▲ A REVIEWED BLUEPRINT NAMES ITS BLUEPRINT AND SPENDS NOTHING. Generation v2 is required
    -- because two of the three reviewed recipes are Formula V1, so "a reviewed expectation exists"
    -- does not identify a V3-producible method.
    CONSTRAINT formula_draft_authoring_plan_reviewed_names_its_blueprint
        CHECK (formula_strategy <> 'REVIEWED_RECIPE_BLUEPRINT' OR (
            recipe_id IS NOT NULL
            AND expectation_ref IS NOT NULL
            AND expectation_generation = 'v2'
            AND reviewed_blueprint_revision IS NOT NULL
            AND reviewed_blueprint_hash IS NOT NULL
            AND provider_contract_hash IS NULL)),

    -- ▲ AND AN LLM DRAFT NAMES ITS CONTRACT AND CANNOT CLAIM A REVIEW. Without this the strongest
    -- claim in the system is one nullable column away from any row.
    CONSTRAINT formula_draft_authoring_plan_llm_names_its_contract
        CHECK (formula_strategy <> 'LLM_AUTHORED' OR (
            provider_contract_hash IS NOT NULL
            AND reviewed_blueprint_revision IS NULL
            AND reviewed_blueprint_hash IS NULL))
);

CREATE INDEX IF NOT EXISTS formula_draft_authoring_plan_by_strategy
    ON formula_draft_authoring_plan (formula_strategy);

-- ▲ APPEND-ONLY. The plan is what the worker re-reads instead of resolving again; one that can be
-- edited after the draft's identity folded it is a plan the identity no longer describes.
CREATE OR REPLACE FUNCTION formula_draft_authoring_plan_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'formula_draft_authoring_plan is append-only: the draft identity folded this '
                    'plan, so a plan that can be rewritten is one the identity no longer describes';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS formula_draft_authoring_plan_no_change ON formula_draft_authoring_plan;
CREATE TRIGGER formula_draft_authoring_plan_no_change
    BEFORE UPDATE OR DELETE ON formula_draft_authoring_plan
    FOR EACH ROW EXECUTE FUNCTION formula_draft_authoring_plan_write_once();

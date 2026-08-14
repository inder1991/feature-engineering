-- src/featuregen/db/migrations/1068_recipe_formula_shadow_work_item_binding_plan.sql
-- Task B2 of the formula/execution seam: the FROZEN PLAN ENVELOPE rides the work item.
--
-- ⚠️ RESERVATION CORRECTED AT EXECUTION. That plan's D-8 reserves 1055 (G-3), 1066 (B1's
-- `semantic_option_decision.binding_plan`) and 1067 (B4's request→option link) and says "nothing
-- else" — but B2 as authored requires the envelope to be a DURABLE, HASH-SEALED field of
-- `recipe_formula_shadow_work_item` ("hashed into `_work_item_material` so
-- `verify_work_item_payload` covers it"), and that table has no column it could ride in. Its five
-- jsonb columns are all spoken for and none may absorb it: `provider_input_json` is the payload
-- the egress whitelist seals and B2's own acceptance asserts BYTE-IDENTICAL,
-- `binding_envelope_json` is the formula AUTHORITY envelope (a different envelope, with its own
-- content hash), and the other three are the bound expectation, the frozen configuration and the
-- request identity. So the reservation was short by one and this file is it. The plan text is
-- corrected in the same commit.
--
--   * ADDITIVE and NULLABLE, no backfill. Every work item written before this migration keeps NULL
--     and hashes EXACTLY as it did — `_work_item_material` adds the two keys only when a plan is
--     present, which is what lets a pre-B2 row go on verifying against its sealed `payload_hash`.
--     Backfilling would be impossible in any case: 1023's write-once triggers refuse UPDATE.
--   * NO DEFAULT. `'{}'::jsonb` would be a plan with no source table and an empty read set — a
--     shape `fold_frozen_binding_plan` never returns, and one compilation's divergence check would
--     read as "the human approved reading nothing".
--   * `binding_plan_hash` is the plan's own content hash under the shadow store's hasher
--     (`recipe_formula_shadow.content_hash`), stored beside the plan for the same reason every
--     other `*_hash` column here is: so a reader can prove what it holds without re-deriving it
--     from a second copy.
ALTER TABLE recipe_formula_shadow_work_item
    ADD COLUMN IF NOT EXISTS binding_plan_json jsonb NULL,
    ADD COLUMN IF NOT EXISTS binding_plan_hash text  NULL;

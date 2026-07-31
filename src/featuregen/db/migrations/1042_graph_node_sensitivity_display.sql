-- src/featuregen/db/migrations/1042_graph_node_sensitivity_display.sql
-- The sensitivity DISPLAY axis (ingestion-richness Task 3, Step 2) — a dedicated column,
-- deliberately NOT graph_node.sensitivity.
--
-- WHY NOT the existing `sensitivity` column (what the plan first assumed):
--   * `sensitivity` is the read-scope TAG. Migration 0993 pins its vocabulary to exactly
--     read_scope.SENSITIVITY_ROLES' keys ('pii','restricted') — the display axis must be able to
--     say 'confidential' (proxy-class concepts; the governed floor level), which that CHECK
--     forbids, and materialize/classify fails CLOSED on any tag outside the role map.
--   * `sensitivity` is an INPUT to the GENERATED enforcement column `visible_requires` (1032):
--     writing 'restricted' into it on an untagged column would CHANGE enforcement — and this
--     projection's hard invariant is that it NEVER writes enforcement (display is not authority).
--
-- So the display axis gets its own column, in the governed-floor vocabulary
-- (safety_floor.SENSITIVITY_ORDER minus the no-requirement levels). Precedence, applied by
-- overlay/upload/axis_projection.py (fill-only-NULL, catalog-scoped, idempotent):
--   1. non-empty `visible_requires` -> its strongest label (the tag 'pii' displays at its
--      registry level, 'restricted') — display never understates what reads are gated on;
--   2. else the concept registry's class (Concept.sensitivity via the existing
--      _CONCEPT_SENSITIVITY_TO_RESTRICTION mapping: pii/special_category/protected_attribute ->
--      'restricted', proxy -> 'confidential');
--   3. else NULL — unknown is honest.
-- No decision-id companion: a projection, not a decision; provenance rides the projection report
-- + the `axis_projection` stage detail. Read-scope enforcement remains `visible_requires` alone.
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS sensitivity_display text NULL;

ALTER TABLE graph_node DROP CONSTRAINT IF EXISTS graph_node_sensitivity_display_check;
ALTER TABLE graph_node ADD CONSTRAINT graph_node_sensitivity_display_check
    CHECK (sensitivity_display IS NULL OR sensitivity_display IN
           ('confidential', 'restricted', 'prohibited'));

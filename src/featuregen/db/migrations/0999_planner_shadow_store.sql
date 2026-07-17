-- src/featuregen/db/migrations/0999_planner_shadow_store.sql
-- Phase-3B.4 shadow harness: durable, append-only (WORM) telemetry for the shadow contract
-- classifier. Three tables capture the FULL population so the 3C-enablement gate can be computed
-- with provable capture integrity:
--   * planner_shadow_dispatch      — one row per shadow run, written FIRST (before scope resolution),
--                                    recording the EXACT expected eligible-recipe set + hash. It carries
--                                    NO catalog_scope_id (that is a resolve_catalog_scope output whose
--                                    pre-loop failure the manifest must survive). Capture integrity =
--                                    every manifest recipe has a run-result row (manifest<->results
--                                    reconciliation is the durable loss signal — never a circular row).
--   * planner_shadow_run_result    — one row per (run, recipe): the recipe-level disposition. Three
--                                    ORTHOGONAL axes — planner_outcome (planning), compile_status +
--                                    incomplete_reason + counts (compile completeness, relative to
--                                    PATH-RESOLVED candidates), capture_status (write health).
--   * planner_shadow_plan_observation — one row per candidate physical plan. Compile-only fields are
--                                    NULLABLE (tier-1 / rejected / compile-off candidates have no
--                                    compiled contract); is_compiled is the cross-field guard. is_selected
--                                    is DERIVED at read time (a join to run_result.selected_...), never a
--                                    column that could disagree.
-- All three tables are append-only (WORM): the app role may INSERT but never UPDATE/DELETE/TRUNCATE
-- (mirror 0971_worm_truncate_revoke.sql). Every enum column carries a CHECK; JSON columns carry a
-- jsonb_typeof='object' CHECK plus a stored payload hash; counts carry nonnegative + consistency CHECKs.

-- Dispatch manifest — the durable "expected set" for a shadow run. compiler_versions is a JSON object of
-- every rule/registry version stamped at dispatch; recipe_hash pins the eligible set so a silent change
-- to the eligible list is detectable. compile_flag/telemetry_flag record which gates were on.
CREATE TABLE IF NOT EXISTS planner_shadow_dispatch (
    generation_run_id     text        PRIMARY KEY,
    eligible_recipe_ids   text[]      NOT NULL,
    recipe_hash           text        NOT NULL,
    expected_count        int         NOT NULL CHECK (expected_count >= 0),
    invocation_predicate  text        NOT NULL,
    compile_flag          boolean     NOT NULL,
    telemetry_flag        boolean     NOT NULL,
    applicability_version text        NOT NULL,
    producer_commit       text        NOT NULL,
    compiler_versions     jsonb       NOT NULL CHECK (jsonb_typeof(compiler_versions) = 'object'),
    compiler_versions_hash text       NOT NULL,
    payload_schema_version text       NOT NULL,
    created_at            timestamptz NOT NULL DEFAULT now()
);

-- Recipe-level result. planner_outcome is the TOTAL mapping of the planning disposition; compile_status
-- is relative to PATH-RESOLVED candidates (only source_to_target_resolved plans compile), so
-- 'not_applicable' = no path-resolved candidate and 'compile_disabled' = telemetry-on/compile-off with
-- >=1 path-resolved candidate (must fail Gate 1). bounding is the planner BoundingMetricsV1 (truncation).
CREATE TABLE IF NOT EXISTS planner_shadow_run_result (
    generation_run_id      text        NOT NULL REFERENCES planner_shadow_dispatch (generation_run_id) ON DELETE CASCADE,
    recipe_id              text        NOT NULL,
    catalog_scope_id       text        NULL,
    planner_input_hash     text        NULL,
    planner_outcome        text        NOT NULL CHECK (planner_outcome IN
                               ('compiled', 'no_physical_plan', 'internal_error',
                                'no_authorized_catalog', 'template_not_found', 'preloop_failure')),
    compile_status         text        NOT NULL CHECK (compile_status IN
                               ('complete', 'incomplete', 'not_applicable', 'compile_disabled')),
    incomplete_reason      text        NULL CHECK (incomplete_reason IN ('budget_count', 'budget_time', 'error')),
    path_resolved_eligible int         NOT NULL CHECK (path_resolved_eligible >= 0),
    compiled_count         int         NOT NULL CHECK (compiled_count >= 0),
    skipped_count          int         NOT NULL CHECK (skipped_count >= 0),
    capture_status         text        NOT NULL CHECK (capture_status IN ('persisted', 'persistence_partial')),
    selected_contract_physical_plan_id text NULL,
    selected_contract_id   text        NULL,
    contract_result_status text        NULL CHECK (contract_result_status IS NULL OR contract_result_status IN
                               ('not_compiled', 'resolved', 'safety_rejected',
                                'unresolved_aggregation_declaration', 'unresolved_freshness',
                                'unresolved_ingredient_connectivity', 'unresolved_safety_evaluation',
                                'unresolved_temporal_declaration')),
    bounding               jsonb       NOT NULL CHECK (jsonb_typeof(bounding) = 'object'),
    payload_schema_version text        NOT NULL,
    created_at             timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (generation_run_id, recipe_id),
    CONSTRAINT run_result_count_consistency CHECK (compiled_count + skipped_count = path_resolved_eligible),
    CONSTRAINT run_result_incomplete_reason_scope CHECK ((incomplete_reason IS NULL) = (compile_status <> 'incomplete'))
);
CREATE INDEX IF NOT EXISTS planner_shadow_run_result_recipe_idx
    ON planner_shadow_run_result (recipe_id);

-- Per-candidate observation. Compile-only fields (contract_id, contract_input_hash,
-- contract_resolution_status, declaration_status, declarations, declarations_output_hash, replay_stamp)
-- are NULLABLE; is_compiled is the cross-field guard. contract_input_hash indexed for shape sampling.
CREATE TABLE IF NOT EXISTS planner_shadow_plan_observation (
    generation_run_id        text        NOT NULL,
    recipe_id                text        NOT NULL,
    physical_plan_id         text        NOT NULL,
    path_resolution_status   text        NOT NULL CHECK (path_resolution_status IN
                                 ('ingredient_binding_only', 'source_to_target_rejected', 'source_to_target_resolved')),
    is_compiled              boolean     NOT NULL,
    contract_id              text        NULL,
    contract_input_hash      text        NULL,
    contract_resolution_status text      NULL CHECK (contract_resolution_status IS NULL OR contract_resolution_status IN
                                 ('not_compiled', 'resolved', 'safety_rejected',
                                  'unresolved_aggregation_declaration', 'unresolved_freshness',
                                  'unresolved_ingredient_connectivity', 'unresolved_safety_evaluation',
                                  'unresolved_temporal_declaration')),
    declaration_status       text        NULL CHECK (declaration_status IS NULL OR declaration_status IN
                                 ('not_compiled', 'resolved', 'safety_rejected',
                                  'unresolved_aggregation_declaration', 'unresolved_ingredient_connectivity',
                                  'unresolved_safety_evaluation', 'unresolved_temporal_declaration')),
    contract_primary_reason_code text    NULL,
    contract_reason_codes    text[]      NOT NULL DEFAULT '{}',
    bridge_count             int         NOT NULL CHECK (bridge_count >= 0),
    tier                     text        NOT NULL CHECK (tier IN
                                 ('tier_1_single_catalog', 'tier_2_one_bridge', 'tier_3_multi_bridge')),
    preference_rank          int         NOT NULL,
    declarations             jsonb       NULL CHECK (declarations IS NULL OR jsonb_typeof(declarations) = 'object'),
    declarations_output_hash text        NULL,
    replay_stamp             jsonb       NULL CHECK (replay_stamp IS NULL OR jsonb_typeof(replay_stamp) = 'object'),
    payload_schema_version   text        NOT NULL,
    created_at               timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (generation_run_id, recipe_id, physical_plan_id),
    FOREIGN KEY (generation_run_id, recipe_id)
        REFERENCES planner_shadow_run_result (generation_run_id, recipe_id) ON DELETE CASCADE,
    CONSTRAINT plan_obs_compiled_hash CHECK (is_compiled = (contract_input_hash IS NOT NULL)),
    CONSTRAINT plan_obs_compiled_stamp CHECK (is_compiled = (replay_stamp IS NOT NULL)),
    CONSTRAINT plan_obs_uncompiled_no_contract CHECK (is_compiled OR contract_id IS NULL)
);
CREATE INDEX IF NOT EXISTS planner_shadow_plan_observation_run_idx
    ON planner_shadow_plan_observation (generation_run_id);
CREATE INDEX IF NOT EXISTS planner_shadow_plan_observation_shape_idx
    ON planner_shadow_plan_observation (contract_input_hash);

-- WORM append-only guard (mirror 0971_worm_truncate_revoke.sql / 0974): revoke destructive DML from the
-- production app role when it exists. No-op in the superuser test cluster (a superuser bypasses grants),
-- so this control relies on production running under the NON-superuser featuregen_app role.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'featuregen_app') THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON planner_shadow_dispatch         FROM featuregen_app;
        REVOKE UPDATE, DELETE, TRUNCATE ON planner_shadow_run_result       FROM featuregen_app;
        REVOKE UPDATE, DELETE, TRUNCATE ON planner_shadow_plan_observation FROM featuregen_app;
    END IF;
END $$;

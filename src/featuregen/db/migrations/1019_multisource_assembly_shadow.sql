-- src/featuregen/db/migrations/1019_multisource_assembly_shadow.sql
-- Phase 3C.2b-i-A · Task 10 — multi-source assembly SHADOW telemetry (mirrors 0999_planner_shadow_store).
-- Durable, append-only (WORM) capture of the multi-source assembly planner's output so the exact-plan +
-- determinism gate is computable AFTER the process exits, with provable capture integrity. Four tables:
--   * ..._dispatch       — one row per shadow run, written FIRST (the durable "expected set" of intent ids,
--                          before any planning). Carries NO per-intent state: capture integrity = every
--                          manifest intent id has an intent_result row (manifest<->results reconciliation,
--                          the durable loss signal — never a circular self-report).
--   * ..._intent_result  — one row per (run, intent): the intent-level disposition on FOUR orthogonal axes.
--                          semantic_outcome (the ASSEMBLY axis: was a governed plan assembled, or which
--                          semantic gate failed), compile_completeness (the CONTRACT axis: a plan can be
--                          assembly-`resolved` while contract-`incomplete` — stale/safety-gapped — that is
--                          NOT operationally resolved; the two axes are NEVER collapsed), technical_status
--                          (technical/preservation/truncation health), capture_status (write health).
--   * ..._candidate      — one row per (run, intent, candidate plan): physical landing + the plan's four
--                          determinism hashes (contract in/out, read-set, replay envelope) + rank + the
--                          per-candidate declaration evidence.
--   * ..._operand_obs    — one row per (run, intent, plan, slot): the operand's pin/role/path strategy/
--                          governed endpoints/source binding (identities + enums + provenance ONLY).
-- All four are append-only (WORM): the app role may INSERT but never UPDATE/DELETE/TRUNCATE (mirror
-- 0971_worm_truncate_revoke.sql / 0999). Every enum column carries a CHECK over its closed vocabulary;
-- JSON columns carry a jsonb_typeof CHECK; the dispatch + intent_result rows carry a stored payload_hash so
-- a divergent re-write for the same key is detectable by read-back-compare (never ON CONFLICT DO NOTHING).

-- Dispatch manifest — the durable "expected set" for a shadow run, written BEFORE planning so a pre-loop
-- failure is visible. versions is a JSON object of every rule/registry version stamped at dispatch;
-- payload_hash pins (expected set + versions + provenance) so a silent change is a detectable conflict.
CREATE TABLE IF NOT EXISTS multisource_assembly_shadow_dispatch (
    run_id                 text        PRIMARY KEY,
    expected_intent_ids    jsonb       NOT NULL CHECK (jsonb_typeof(expected_intent_ids) = 'array'),
    expected_count         int         NOT NULL CHECK (expected_count >= 0),
    versions               jsonb       NOT NULL CHECK (jsonb_typeof(versions) = 'object'),
    versions_hash          text        NOT NULL,
    shadow_flag            boolean     NOT NULL,
    producer_commit        text        NOT NULL,
    payload_hash           text        NOT NULL,
    payload_schema_version text        NOT NULL,
    created_at             timestamptz NOT NULL DEFAULT now()
);

-- Intent-level result. FOUR orthogonal axes, each with its own CHECK vocabulary. compile_completeness is
-- the CONTRACT axis (distilled from ContractResolutionStatus): 'complete' = operationally clean resolve,
-- 'incomplete' = a plan was assembled but its contract is unresolved (stale / safety gap / unresolved
-- declaration), 'not_applicable' = no plan to compile. selected_plan_id is the assembly-axis selection.
CREATE TABLE IF NOT EXISTS multisource_assembly_shadow_intent_result (
    run_id                 text        NOT NULL REFERENCES multisource_assembly_shadow_dispatch (run_id) ON DELETE CASCADE,
    intent_id              text        NOT NULL,
    semantic_outcome       text        NOT NULL CHECK (semantic_outcome IN
                               ('resolved', 'operand_shape_invalid', 'unsupported_path_aggregation',
                                'ordering_anchor_missing', 'no_governed_path',
                                'realization_endpoint_ungoverned', 'no_common_physical_grain',
                                'ambiguous_physical_grain', 'aggregation_unsafe_on_path',
                                'temporal_paths_incompatible', 'source_binding_ungoverned',
                                'not_evaluated')),
    compile_completeness   text        NOT NULL CHECK (compile_completeness IN
                               ('complete', 'incomplete', 'not_applicable')),
    technical_status       text        NOT NULL CHECK (technical_status IN
                               ('ok', 'operand_or_slot_not_preserved', 'technical_failure',
                                'budget_truncated')),
    capture_status         text        NOT NULL CHECK (capture_status IN
                               ('persisted', 'persistence_partial')),
    normalized_intent_hash text        NOT NULL,
    selected_plan_id       text        NULL,
    reason_codes           jsonb       NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(reason_codes) = 'array'),
    payload_hash           text        NOT NULL,
    payload_schema_version text        NOT NULL,
    created_at             timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, intent_id)
);
CREATE INDEX IF NOT EXISTS multisource_assembly_shadow_intent_result_run_idx
    ON multisource_assembly_shadow_intent_result (run_id);

-- Per-candidate observation. The four determinism hashes (contract in/out, read-set, replay envelope) plus
-- the physical landing + declaration evidence make the exact-plan + determinism gate computable offline.
CREATE TABLE IF NOT EXISTS multisource_assembly_shadow_candidate (
    run_id                 text        NOT NULL,
    intent_id              text        NOT NULL,
    plan_id                text        NOT NULL,
    physical_landing       jsonb       NOT NULL CHECK (jsonb_typeof(physical_landing) = 'object'),
    contract_input_hash    text        NOT NULL,
    contract_output_hash   text        NOT NULL,
    read_set_hash          text        NOT NULL,
    replay_envelope_hash   text        NOT NULL,
    rank                   int         NOT NULL CHECK (rank >= 0),
    declaration_evidence   jsonb       NOT NULL CHECK (jsonb_typeof(declaration_evidence) = 'object'),
    payload_schema_version text        NOT NULL,
    created_at             timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, intent_id, plan_id),
    FOREIGN KEY (run_id, intent_id)
        REFERENCES multisource_assembly_shadow_intent_result (run_id, intent_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS multisource_assembly_shadow_candidate_intent_idx
    ON multisource_assembly_shadow_candidate (run_id, intent_id);
CREATE INDEX IF NOT EXISTS multisource_assembly_shadow_candidate_shape_idx
    ON multisource_assembly_shadow_candidate (contract_input_hash);

-- Per-operand observation. Identities / enums / provenance ONLY (no free-form text): the operand's pin,
-- its semantic role, the per-path strategy, the governed endpoints (source + intermediates + landing),
-- the governed source binding, and the ORDERED governed crossings of its path (I-1). governed_endpoints
-- and crossings are JSON arrays; the rest are JSON objects. role carries a CHECK over the closed
-- SemanticRole vocabulary (M20), matching the discipline of the four intent_result axis columns.
--
-- crossings (I-1): one record per governed crossing/segment of the operand's binding_plan path, in path
-- order — {kind, catalog, table, bridge_fact_key|realization_ref, authority, confirmed_event_id}. It makes
-- crossing-governedness FALSIFIABLE from persisted telemetry (the gate asserts every crossing is a
-- governed authority: a VERIFIED bridge or an approved/declared realization). confirmed_event_id is
-- AUDIT-ONLY (re-queried from entity_bridge_edge for a crossed VERIFIED bridge): it is a per-EVENT id, so
-- it is DELIBERATELY excluded from the divergent-duplicate payload_hash (which hashes only the
-- deterministic crossing identity) and never enters any plan/contract identity.
CREATE TABLE IF NOT EXISTS multisource_assembly_shadow_operand_obs (
    run_id                 text        NOT NULL,
    intent_id              text        NOT NULL,
    plan_id                text        NOT NULL,
    slot_id                text        NOT NULL,
    pin                    jsonb       NOT NULL CHECK (jsonb_typeof(pin) = 'object'),
    role                   text        NOT NULL CHECK (role IN
                               ('measure', 'counted', 'time', 'numerator', 'denominator',
                                'minuend', 'subtrahend')),
    path_strategy          jsonb       NOT NULL CHECK (jsonb_typeof(path_strategy) = 'object'),
    governed_endpoints     jsonb       NOT NULL CHECK (jsonb_typeof(governed_endpoints) = 'array'),
    source_binding         jsonb       NOT NULL CHECK (jsonb_typeof(source_binding) = 'object'),
    crossings              jsonb       NOT NULL DEFAULT '[]'::jsonb
                               CHECK (jsonb_typeof(crossings) = 'array'),
    payload_schema_version text        NOT NULL,
    created_at             timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, intent_id, plan_id, slot_id),
    FOREIGN KEY (run_id, intent_id, plan_id)
        REFERENCES multisource_assembly_shadow_candidate (run_id, intent_id, plan_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS multisource_assembly_shadow_operand_obs_plan_idx
    ON multisource_assembly_shadow_operand_obs (run_id, intent_id, plan_id);

-- WORM append-only guard (mirror 0971_worm_truncate_revoke.sql / 0999): revoke destructive DML from the
-- production app role when it exists. No-op in the superuser test cluster (a superuser bypasses grants),
-- so this control relies on production running under the NON-superuser featuregen_app role.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'featuregen_app') THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON multisource_assembly_shadow_dispatch      FROM featuregen_app;
        REVOKE UPDATE, DELETE, TRUNCATE ON multisource_assembly_shadow_intent_result FROM featuregen_app;
        REVOKE UPDATE, DELETE, TRUNCATE ON multisource_assembly_shadow_candidate     FROM featuregen_app;
        REVOKE UPDATE, DELETE, TRUNCATE ON multisource_assembly_shadow_operand_obs   FROM featuregen_app;
    END IF;
END $$;

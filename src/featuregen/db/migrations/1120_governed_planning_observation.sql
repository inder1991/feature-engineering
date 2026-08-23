-- src/featuregen/db/migrations/1120_governed_planning_observation.sql
-- Stage 1B (cross-catalog programme rev 5) task S1B-1: the DURABLE record of what governed
-- planning actually did — one row per planned request, for EVERY outcome, plus the
-- rejection-specific child that turns "we could not cross here" into a countable demand, plus the
-- SME judgement stream Stage 1C reads.
--
-- RESERVATION. main tops out at 1118; 1117 and 1119 are legal gaps (reserved by sibling branches
-- that may or may not land). 1120/1121 are this task's pair: 1120 is the append-only evidence,
-- 1121 is the one mutable worker-coordination table, kept in a SEPARATE file precisely so the
-- exception is visible in the filename rather than buried inside an append-only migration.
--
-- Shape decisions:
--  * APPEND-ONLY (the 1034/1060/1062 guard idiom: a row-level BEFORE UPDATE OR DELETE trigger and
--    a statement-level BEFORE TRUNCATE trigger sharing one plpgsql raiser). An observation is what
--    a run SAW under a frozen scope; rewriting it destroys exactly the comparison the telemetry
--    exists to make, and a demand that can be edited cannot be counted.
--  * IDENTITY COLUMNS MIRROR contract/governed_identity.py's GovernedVariantIdentityV1 EXACTLY
--    (canonical_definition_id, definition_origin, governed_variant_id, planning_request_hash,
--    parameter_binding_hash, physical_plan_content_hash). Same names, so a reader never has to
--    translate between the code's identity and the table's.
--  * catalog_scope_material holds the scope's STABLE shape (sorted catalogs + policy/resolution
--    versions + target entity) and NEVER the watermark-bearing scope_id: that re-mints on every
--    ingest, so grouping on it would shatter one queue into one group per ingest.
--  * NO FOREIGN KEY TO graph_node ANYWHERE. A demand names entities and relationships as TEXT: the
--    whole point of a bridge demand is that the graph does not yet sanction the crossing, so a
--    referential dependency on the graph would make the unmet case unrecordable.
--  * The intent-lineage trigger makes run→intent denormalization structurally honest: the row's
--    intent_id must be IS NOT DISTINCT FROM the run's own. feature_generation_run.intent_id has
--    been NULLABLE since 1006 (a run may exist before a Gate-#1 choice is recorded), so a missing
--    intent is representable — but ONLY when the run genuinely has none.

-- ── 1) governed_planning_observation — one row per planned request, ALL outcomes ───────────────
CREATE TABLE IF NOT EXISTS governed_planning_observation (
    observation_id          text        PRIMARY KEY,
    generation_run_id       text        NOT NULL
        REFERENCES feature_generation_run (generation_run_id),
    intent_id               text        NULL REFERENCES contract_intent (intent_id),
    observation_mode        text        NOT NULL,
    -- GovernedVariantIdentityV1, column-for-column:
    definition_origin       text        NOT NULL,
    canonical_definition_id text        NOT NULL,
    recipe_id               text        NULL,               -- recipe-origin only (R14)
    governed_variant_id     text        NOT NULL,           -- gvar_<64hex>
    planning_request_hash   text        NOT NULL,
    parameter_binding_hash  text        NOT NULL DEFAULT '',
    physical_plan_content_hash text     NOT NULL DEFAULT '',
    selected_physical_plan_id  text     NOT NULL DEFAULT '',   -- bp_… display id when resolved
    contract_id             text        NULL,
    -- what was asked, and of what:
    primary_objective       text        NOT NULL DEFAULT '',
    target_entity           text        NOT NULL,
    anchor_catalog_source   text        NOT NULL DEFAULT '',
    -- what came back:
    resolution_status       text        NOT NULL,           -- the contract/plan status string
    reason_codes            jsonb       NOT NULL DEFAULT '[]'::jsonb,
    participating_catalogs  jsonb       NOT NULL DEFAULT '[]'::jsonb,
    hop_count               integer     NOT NULL DEFAULT 0,
    bridge_count            integer     NOT NULL DEFAULT 0,
    authority_floor_status  text        NOT NULL DEFAULT '', -- '' | met | unmet | unevaluated
    safety_status           text        NOT NULL DEFAULT '',
    readiness               text        NOT NULL DEFAULT '',
    intent_corroborated     boolean     NOT NULL DEFAULT false,
    param_divergence        jsonb       NOT NULL DEFAULT '[]'::jsonb,
    -- the STABLE scope shape (never the watermark-bearing scope_id — see the header):
    catalog_scope_material  jsonb       NOT NULL DEFAULT '{}'::jsonb,
    metadata_snapshot_id    text        NULL,
    compiler_input_fingerprint text     NOT NULL DEFAULT '',
    recorded_at             timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT governed_planning_observation_mode_chk CHECK (
        observation_mode IN ('live', 'telemetry')),
    CONSTRAINT governed_planning_observation_origin_chk CHECK (
        definition_origin IN ('recipe_v2', 'llm_intent')),
    -- MODE-INCLUSIVE: the same variant observed live AND in telemetry is two rows, honestly; a
    -- replay within one mode is one.
    CONSTRAINT governed_planning_observation_identity_key UNIQUE (
        generation_run_id, observation_mode, governed_variant_id)
);

CREATE INDEX IF NOT EXISTS governed_planning_observation_run_idx
    ON governed_planning_observation (generation_run_id, recorded_at);
CREATE INDEX IF NOT EXISTS governed_planning_observation_definition_idx
    ON governed_planning_observation (canonical_definition_id, recorded_at);
CREATE INDEX IF NOT EXISTS governed_planning_observation_variant_idx
    ON governed_planning_observation (governed_variant_id, recorded_at);
CREATE INDEX IF NOT EXISTS governed_planning_observation_resolution_idx
    ON governed_planning_observation (definition_origin, resolution_status, recorded_at);
CREATE INDEX IF NOT EXISTS governed_planning_observation_anchor_idx
    ON governed_planning_observation (anchor_catalog_source, recorded_at);

-- ── 2) bridge_demand_observation — the rejection-specific CHILD ────────────────────────────────
-- demand_identity_hash is the FULL sha256 hex of the unmet hop's material (never a prefix): the
-- store is the single owner of that material, and to_endpoint_hint is deliberately NOT in it.
CREATE TABLE IF NOT EXISTS bridge_demand_observation (
    demand_id            text        PRIMARY KEY,
    observation_id       text        NOT NULL
        REFERENCES governed_planning_observation (observation_id),
    demand_queue         text        NOT NULL,
    demand_identity_hash text        NOT NULL,           -- FULL sha256 hex
    recipe_revision_hash text        NOT NULL,
    relationship_id      text        NOT NULL DEFAULT '',
    relationship_version text        NOT NULL DEFAULT '',
    from_entity          text        NOT NULL DEFAULT '',
    to_entity            text        NOT NULL DEFAULT '',
    position_catalog     text        NOT NULL DEFAULT '',
    position_table_ref   text        NOT NULL DEFAULT '',
    hop_index            integer     NOT NULL DEFAULT -1,
    verdict              text        NOT NULL,
    realizers            jsonb       NOT NULL DEFAULT '[]'::jsonb,
    near_side_key_refs   jsonb       NOT NULL DEFAULT '[]'::jsonb,
    -- ADVISORY, NON-IDENTITY: a guess at the far endpoint, useful to a human sizing the work and
    -- never hashed into demand_identity_hash. Two demands that differ only here are ONE demand.
    to_endpoint_hint     text        NOT NULL DEFAULT '',
    recorded_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT bridge_demand_queue_chk CHECK (
        demand_queue IN ('bridge_demand', 'realization_gap', 'planner_capacity')),
    CONSTRAINT bridge_demand_verdict_chk CHECK (
        verdict IN ('unsanctioned_bridge', 'missing_realization',
                    'bounded_out_max_bridges', 'bounded_out_max_frontier_states')),
    -- The routing rule, structural rather than conventional: the queue a demand lands in is a
    -- FUNCTION of its verdict, so a caller cannot file a bridge demand under capacity.
    CONSTRAINT bridge_demand_queue_routing_chk CHECK (
        (verdict = 'unsanctioned_bridge' AND demand_queue = 'bridge_demand')
        OR (verdict = 'missing_realization' AND demand_queue = 'realization_gap')
        OR (verdict IN ('bounded_out_max_bridges', 'bounded_out_max_frontier_states')
            AND demand_queue = 'planner_capacity')),
    CONSTRAINT bridge_demand_identity_key UNIQUE (observation_id, demand_identity_hash)
);

CREATE INDEX IF NOT EXISTS bridge_demand_observation_queue_idx
    ON bridge_demand_observation (demand_queue, recorded_at);
CREATE INDEX IF NOT EXISTS bridge_demand_observation_identity_idx
    ON bridge_demand_observation (demand_identity_hash, recorded_at);
CREATE INDEX IF NOT EXISTS bridge_demand_observation_group_idx
    ON bridge_demand_observation (demand_queue, relationship_id, from_entity, to_entity);

-- ── 3) governed_plan_review_event — append-only SME judgements (Stage 1C reads these) ──────────
-- The 1060 idiom: "current" is a READ projection (newest event per observation), never a mutable
-- column, and a changed mind is a NEW event that names the one it supersedes.
CREATE TABLE IF NOT EXISTS governed_plan_review_event (
    event_id            text        PRIMARY KEY,
    observation_id      text        NOT NULL
        REFERENCES governed_planning_observation (observation_id),
    reviewer            text        NOT NULL,
    reviewer_role       text        NOT NULL,
    decision            text        NOT NULL,
    rationale           text        NOT NULL DEFAULT '',
    supersedes_event_id text        NULL REFERENCES governed_plan_review_event (event_id),
    recorded_at         timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT governed_plan_review_decision_chk CHECK (
        decision IN ('approved', 'changes_required', 'rejected'))
);

CREATE INDEX IF NOT EXISTS governed_plan_review_event_observation_idx
    ON governed_plan_review_event (observation_id, recorded_at);

-- ── the intent-lineage trigger, shared by 1120's observation table and 1121's outbox ───────────
-- Declared here (1120 applies first, lexically) and reused by 1121, so the two tables cannot drift
-- apart on the one rule that makes their denormalized intent_id trustworthy.
CREATE OR REPLACE FUNCTION governed_observation_intent_lineage() RETURNS trigger AS $$
DECLARE
    run_intent text;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM feature_generation_run
                    WHERE generation_run_id = NEW.generation_run_id) THEN
        -- The foreign key owns this refusal; pre-empting it here would report a lineage problem
        -- for what is really a missing run.
        RETURN NEW;
    END IF;
    SELECT g.intent_id INTO run_intent
      FROM feature_generation_run g
     WHERE g.generation_run_id = NEW.generation_run_id;
    IF NEW.intent_id IS DISTINCT FROM run_intent THEN
        RAISE EXCEPTION
            '%.intent_id % does not match feature_generation_run.intent_id % for run %: a missing '
            'intent is representable ONLY when the run genuinely has none',
            TG_TABLE_NAME, COALESCE(quote_literal(NEW.intent_id), 'NULL'),
            COALESCE(quote_literal(run_intent), 'NULL'), NEW.generation_run_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER governed_planning_observation_intent_lineage
    BEFORE INSERT ON governed_planning_observation
    FOR EACH ROW EXECUTE FUNCTION governed_observation_intent_lineage();

-- ── append-only guards (one raiser per table; it names the table and the reason) ───────────────
-- TG_OP is assigned in statement-level TRUNCATE triggers; OLD/NEW are NOT, which is why neither
-- raiser touches them.
CREATE OR REPLACE FUNCTION governed_planning_observation_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'governed_planning_observation is append-only: % is not allowed. An observation is what a '
        'run saw under a frozen scope — rewriting it destroys the comparison the telemetry exists '
        'to make. Record a NEW observation instead.', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER governed_planning_observation_no_mutation
    BEFORE UPDATE OR DELETE ON governed_planning_observation
    FOR EACH ROW EXECUTE FUNCTION governed_planning_observation_append_only();
-- A FOR EACH ROW trigger does not fire on TRUNCATE; this is the only guard that does.
CREATE OR REPLACE TRIGGER governed_planning_observation_no_truncate
    BEFORE TRUNCATE ON governed_planning_observation
    FOR EACH STATEMENT EXECUTE FUNCTION governed_planning_observation_append_only();

CREATE OR REPLACE FUNCTION bridge_demand_observation_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'bridge_demand_observation is append-only: % is not allowed. A demand is evidence that a '
        'crossing was needed and unavailable; demand that can be edited cannot be counted.', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER bridge_demand_observation_no_mutation
    BEFORE UPDATE OR DELETE ON bridge_demand_observation
    FOR EACH ROW EXECUTE FUNCTION bridge_demand_observation_append_only();
CREATE OR REPLACE TRIGGER bridge_demand_observation_no_truncate
    BEFORE TRUNCATE ON bridge_demand_observation
    FOR EACH STATEMENT EXECUTE FUNCTION bridge_demand_observation_append_only();

CREATE OR REPLACE FUNCTION governed_plan_review_event_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'governed_plan_review_event is append-only: % is not allowed. Review history is evidence, '
        'and evidence that can be rewritten proves nothing — record a NEW event that supersedes '
        'the old one.', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER governed_plan_review_event_no_mutation
    BEFORE UPDATE OR DELETE ON governed_plan_review_event
    FOR EACH ROW EXECUTE FUNCTION governed_plan_review_event_append_only();
CREATE OR REPLACE TRIGGER governed_plan_review_event_no_truncate
    BEFORE TRUNCATE ON governed_plan_review_event
    FOR EACH STATEMENT EXECUTE FUNCTION governed_plan_review_event_append_only();

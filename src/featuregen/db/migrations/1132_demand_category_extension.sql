-- src/featuregen/db/migrations/1132_demand_category_extension.sql
--
-- Cross-catalog serving plan (2026-08-24, rev 13) task A7: the demand vocabulary gains ONE
-- category. The §T0 mapping row for 1132 is "demand-category extension:
-- `CARDINALITY_EVIDENCE_REQUIRED` verdict + queue-routing CHECK extension on
-- `bridge_demand_observation`".
--
-- WHY A MIGRATION AND NOT AN EDIT. 1120's two CHECKs are CLOSED sets (§V fact V7), and 1120 is
-- immutable as a FILE: `migrations.py` records a sha256 of every migration's source and raises
-- `migration ... checksum drift` if that source ever changes. A new category is therefore DDL, in
-- a NEW file, that DROPs and re-ADDs the two constraints under their existing names. Re-adding
-- under the same name is deliberate: a reader who greps `bridge_demand_verdict_chk` finds ONE
-- constraint whose definition is the whole current vocabulary, rather than 1120's original plus a
-- second constraint that silently narrows it again.
--
-- WHAT IS ADDED, AND WHAT DELIBERATELY IS NOT (R6, per-category judgement — the demand ledger
-- funds BRIDGE work, and a queue that ranks anything else ranks noise):
--
--  * `cardinality_evidence_required` — ADDED. A4c mints PROVISIONAL sandbox realizations with
--    `cardinality=UNKNOWN`, so the platform now has a third realization state: the crossing is
--    sanctioned, a realization EXISTS, and what is missing is the cardinality EVIDENCE that makes
--    it costable. Before this row that case was indistinguishable from `missing_realization` —
--    "go build a realization" filed against one that already exists. It routes to
--    `realization_gap` and NOT to a fourth queue: the work is realization work, addressed to the
--    person who already owns the realization, and splitting the queue would split the ranking.
--
--  * `directional_realization_missing` — NOT added. The ledger already spells this
--    `missing_realization`. A synonym would split one queue's counts across two names and make
--    every rollup understate the demand it was built to measure.
--
--  * `directional_mapping_incomplete` — NOT added. It is a refusal of the A4c PRODUCER, which
--    runs on the adoption path, not on a planning occurrence. Every demand row is a CHILD of a
--    `governed_planning_observation` — one row per PLANNED request — so filing it would require
--    inventing an observation for a request nobody planned, which is the fabrication 1120 exists
--    to prevent. When the PLANNER meets the same gap it already files `unsanctioned_bridge` or
--    `missing_realization`, and the missing mapping rides the row's `realizers` evidence.
--
--  * `allocation_policy_required` — NOT added. Its crossing is sanctioned, realized AND has
--    PROVEN cardinality; nothing about the bridge is missing. What is missing is a feature-
--    modelling decision (how a known M:N contributes at final grain), chartered out of this
--    increment. 1120's own rule — "a concept mismatch is a modelling problem, not a missing
--    bridge" — drops it.
--
--  * `temporal_join_policy_missing` — NOT added. It is a per-DATASET governance gap with its own
--    authority (`DatasetTemporalPolicyRevisionV1`), while this table's grouping key is the
--    CROSSING (relationship_id, from_entity, to_entity). Filing dataset work under a crossing
--    would attribute it to a bridge nobody needs to build and inflate the bridge queue.
--
--  * `REALIZATION_ATTACHMENT_DEFECT` — NEVER added (R6 states it as an ops alert). A platform
--    fault attributed to somebody's feature is a mis-ranked queue AND a mis-addressed ticket. Its
--    channel today is the operational log (`governed_telemetry_worker` already records a failed
--    realization lookup as `realization_lookup_failed` — a state of the QUESTION, never a demand)
--    plus the `realizers[].realization_state` disclosure on rows that do file. Routing ops alerts
--    to an alert sink is OUT OF SCOPE for A7 and belongs with the operator-activation phase.
--
-- NO NEW TABLE, NO NEW FOREIGN KEY. A7 adds no store: satisfaction is a PROJECTION over this
-- append-only history (a current-unresolved READ), never a status column, and a satisfaction
-- table would also have needed an FK onto an append-only table — which A4 proved re-contracts the
-- referenced table's TRUNCATE guard (Postgres refuses the TRUNCATE with FeatureNotSupported
-- BEFORE the table's own `BEFORE TRUNCATE` raiser can fire).
--
-- WIDENING ONLY. Both statements enlarge the accepted set, so re-validation against existing rows
-- cannot fail: every row that satisfied the old CHECK satisfies the new one.

-- ── the verdict vocabulary ─────────────────────────────────────────────────────────────────────
ALTER TABLE bridge_demand_observation
    DROP CONSTRAINT IF EXISTS bridge_demand_verdict_chk;
ALTER TABLE bridge_demand_observation
    ADD CONSTRAINT bridge_demand_verdict_chk CHECK (
        verdict IN ('unsanctioned_bridge', 'missing_realization',
                    'bounded_out_max_bridges', 'bounded_out_max_frontier_states',
                    'cardinality_evidence_required'));

-- ── the routing rule: the queue a demand lands in is a FUNCTION of its verdict ─────────────────
-- Structural rather than conventional, exactly as 1120 wrote it: a caller cannot file evidence
-- demand under capacity, and the two realization verdicts share ONE queue because they are two
-- states of one person's work.
ALTER TABLE bridge_demand_observation
    DROP CONSTRAINT IF EXISTS bridge_demand_queue_routing_chk;
ALTER TABLE bridge_demand_observation
    ADD CONSTRAINT bridge_demand_queue_routing_chk CHECK (
        (verdict = 'unsanctioned_bridge' AND demand_queue = 'bridge_demand')
        OR (verdict IN ('missing_realization', 'cardinality_evidence_required')
            AND demand_queue = 'realization_gap')
        OR (verdict IN ('bounded_out_max_bridges', 'bounded_out_max_frontier_states')
            AND demand_queue = 'planner_capacity'));

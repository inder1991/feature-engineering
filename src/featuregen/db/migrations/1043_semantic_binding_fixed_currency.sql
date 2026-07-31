-- src/featuregen/db/migrations/1043_semantic_binding_fixed_currency.sql
-- Ingestion-richness Task 4 — un-stall semantic bindings: the FIXED-CURRENCY literal shape.
--
-- The live defect: 126 semantic_binding_candidate rows -> 0 proposals -> 0 edges, because the D2
-- shortlist could only mark a pairing STRONG when a table had EXACTLY ONE currency column. The fix
-- adds name-affinity disambiguation (code-only) AND a FIXED-CURRENCY candidate/fact shape for a
-- measure whose own name embeds its ISO-4217 code (`counter_party_amt_aed` -> literal 'AED').
-- This migration is the durable half of that second shape:
--
--   1) semantic_binding_candidate — the 1014 kind-shape CHECK gains the literal variant:
--      currency_binding with NO target and proposed_value = {"currency_code": ...} (the closed
--      `known_currency_codes()` registry is enforced in code + the E1 write gate). Every existing
--      row satisfies the widened CHECK (it is a strict superset), so legacy data can never abort.
--   2) semantic_binding_edge — a literal edge carries `currency_code` with a NULL `to_ref`
--      (there is no target currency column). Exactly one of (to_ref, currency_code) is set.
--   3) graph_node — the fixed-currency operational projection mirrors the 1015 entity pattern:
--      `currency` (0957) becomes governable with `declared_currency` (the file's display value,
--      restored on demotion — never data loss, and the durable divergence signal),
--      `currency_fact_key` / `currency_fact_event_id` (provenance; demotion locates by fact_key),
--      and `currency_status` ('VERIFIED' while operational — the SECOND fail-closed read gate).
--
-- Additive + re-runnable (ADD COLUMN IF NOT EXISTS / DROP CONSTRAINT IF EXISTS then ADD), and
-- safe against seeded legacy rows: no rewrite, only widened CHECKs and NULL-able columns.

-- 1) candidate kind shape: currency = (target XOR currency_code value); entity unchanged.
ALTER TABLE semantic_binding_candidate
    DROP CONSTRAINT IF EXISTS semantic_binding_candidate_kind_shape;
ALTER TABLE semantic_binding_candidate
    ADD CONSTRAINT semantic_binding_candidate_kind_shape CHECK (
        (binding_kind = 'currency_binding'
             AND target_graph_ref IS NOT NULL AND proposed_value IS NULL)
        OR
        (binding_kind = 'currency_binding'
             AND target_graph_ref IS NULL AND proposed_value IS NOT NULL)
        OR
        (binding_kind = 'entity_assignment'
             AND target_graph_ref IS NULL AND proposed_value IS NOT NULL)
    );

-- 2) literal edges: to_ref becomes NULL-able; currency_code added; exactly one of the two is set.
ALTER TABLE semantic_binding_edge ALTER COLUMN to_ref DROP NOT NULL;
ALTER TABLE semantic_binding_edge ADD COLUMN IF NOT EXISTS currency_code text NULL;
ALTER TABLE semantic_binding_edge
    DROP CONSTRAINT IF EXISTS semantic_binding_edge_target_shape;
ALTER TABLE semantic_binding_edge
    ADD CONSTRAINT semantic_binding_edge_target_shape CHECK (
        (to_ref IS NOT NULL AND currency_code IS NULL)
        OR
        (to_ref IS NULL AND currency_code IS NOT NULL)
    );

-- 3) graph_node fixed-currency projection columns (mirrors the 1015 entity quartet).
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS declared_currency      text NULL;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS currency_fact_key      text NULL;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS currency_fact_event_id text NULL;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS currency_status        text NULL
    CHECK (currency_status IS NULL OR currency_status = 'VERIFIED');
CREATE INDEX IF NOT EXISTS graph_node_currency_fact_key_idx
    ON graph_node (currency_fact_key) WHERE currency_fact_key IS NOT NULL;

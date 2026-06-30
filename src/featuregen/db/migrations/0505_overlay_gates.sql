-- src/featuregen/db/migrations/0505_overlay_gates.sql
-- SP-1 Phase 1 (design §2.2): extend SP-0's human-gate model for overlay confirmations.
-- Additive + idempotent.

-- 1. Overlay routing/CAS columns (nullable, like run_id/feature_id).
ALTER TABLE human_tasks ADD COLUMN IF NOT EXISTS fact_key        text;
ALTER TABLE human_tasks ADD COLUMN IF NOT EXISTS draft_event_id  text;
ALTER TABLE human_tasks ADD COLUMN IF NOT EXISTS target_event_id text;
ALTER TABLE human_tasks ADD COLUMN IF NOT EXISTS evidence_ref    text;

-- 2. Widen the gate CHECK (auto-named human_tasks_gate_check) with the two overlay gates.
ALTER TABLE human_tasks DROP CONSTRAINT IF EXISTS human_tasks_gate_check;
ALTER TABLE human_tasks ADD CONSTRAINT human_tasks_gate_check CHECK (
    gate IN ('CLARIFICATION','DATA_STEWARD','COMPLIANCE',
             'INDEPENDENT_VALIDATION','FINAL_APPROVAL',
             'OVERLAY_DATA_OWNER','OVERLAY_COMPLIANCE')
);

-- 3. Partial index for open overlay tasks by fact_key.
CREATE INDEX IF NOT EXISTS human_tasks_fact_idx
    ON human_tasks (fact_key) WHERE status = 'open';

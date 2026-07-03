-- src/featuregen/db/migrations/0509_use_case_onboarding_gates.sql
-- SP-2 Phase 1 (design §2.1 #6, §5.4, §11): admit a new-banking-use-case onboarding gate + park
-- hold-state, mirroring SP-1's 0505_overlay_gates.sql. Additive + idempotent.

-- 1. Widen the gate CHECK (auto-named human_tasks_gate_check) with USE_CASE_ONBOARDING. This
--    DROP/ADD PRESERVES SP-0's base gates AND SP-1's OVERLAY_DATA_OWNER/OVERLAY_COMPLIANCE (0509
--    sorts after 0505, so it rebuilds on top of them).
ALTER TABLE human_tasks DROP CONSTRAINT IF EXISTS human_tasks_gate_check;
ALTER TABLE human_tasks ADD CONSTRAINT human_tasks_gate_check CHECK (
    gate IN ('CLARIFICATION','DATA_STEWARD','COMPLIANCE',
             'INDEPENDENT_VALIDATION','FINAL_APPROVAL',
             'OVERLAY_DATA_OWNER','OVERLAY_COMPLIANCE',
             'USE_CASE_ONBOARDING')
);

-- 2. The NEEDS_USE_CASE_ONBOARDING hold needs NO DDL because it is NOT a DB value at all (X6). It is
--    NOT stored in SP-0's RUN_PARKED.waiting_on_fact: that field is SP-1's fact-confirmed-resume key
--    (run_lifecycle.py:112 matches `payload->>'waiting_on_fact' = fact_key`), so overloading the
--    onboarding hold there would let a later fact_confirmed_resume WRONGLY unpark it. The hold is
--    represented entirely in the domain layer — the `feature_contract` folded status
--    NEEDS_USE_CASE_ONBOARDING (via the USE_CASE_ONBOARDING_REQUESTED event, P4) + the
--    USE_CASE_ONBOARDING gate task above; a run parked for onboarding sets waiting_on_fact=None. The
--    canonical constant is intake.events.NEEDS_USE_CASE_ONBOARDING (no CHECK exists to widen).

-- 3. Partial index for open onboarding tasks by run (mirrors human_tasks_fact_idx).
CREATE INDEX IF NOT EXISTS human_tasks_use_case_onboarding_idx
    ON human_tasks (run_id) WHERE gate = 'USE_CASE_ONBOARDING' AND status = 'open';

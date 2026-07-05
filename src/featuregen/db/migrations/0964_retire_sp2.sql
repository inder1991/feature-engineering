-- src/featuregen/db/migrations/0964_retire_sp2.sql
-- SP-2 intake retired (2026-07-05): seed_sp2_authz is gone, so remove the dead SP-2 command authz rows.
-- Idempotent (a fresh DB never had them — nothing seeds them anymore). LEFT in place (harmless dead
-- artifacts, like the WORM feature_contract_events tables from 0508): the 'feature_contract' projection
-- checkpoint (seeded by 0510) — nothing advances it post-retirement.
DELETE FROM authz_policy WHERE action IN (
    'submit_intent', 'answer_clarification', 'select_candidate_doc', 'open_gate1_task',
    'confirm_contract', 'request_edit', 'reject_intent', 'withdraw_intent', 'advance_intake');

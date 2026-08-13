-- src/featuregen/db/migrations/1065_semantic_option_decision_evidence.sql
-- Remediation D1 (reservation 1065 per the 2026-08-13 remediation plan): the decision row
-- grows to the FULL evidence record — what the served card was built from, frozen at
-- serving, additively (same table, no second store; the append-only triggers from 1063
-- already refuse UPDATE/DELETE/TRUNCATE, so these columns are write-once with the row).
--
--   * evidence — the complete audit: full planning request + verdicts + per-candidate
--     eligibility (the losing shortlist and its truncation marker included) + the typed
--     validation (families tri-state included).
--   * decision_manifest — PLAN-15's seal: the content hashes of every consumed input
--     (frozen context, authority matrix, gauntlet version, planning request, binding plan,
--     operand-class map), so the decision can prove WHAT it consumed.
ALTER TABLE semantic_option_decision
    ADD COLUMN IF NOT EXISTS evidence         jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS decision_manifest jsonb NOT NULL DEFAULT '{}'::jsonb;

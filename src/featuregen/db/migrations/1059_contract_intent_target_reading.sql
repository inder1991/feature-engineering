-- src/featuregen/db/migrations/1059_contract_intent_target_reading.sql
-- Intake build increment 2 (router-quality plan 2026-08-10, D7 reservation 1059 appended in this
-- same commit). The storage decision, executed: the MODEL's ticket draft lives in structured_result
-- (increment 1, no DDL) like every other model output; the HUMAN's confirmation extends
-- contract_intent — human decisions live with the intent they govern.
--
-- The signed reading of one hypothesis:
--  * target_window_days / target_type / business_domain — the three ticket fields beyond target_ref
--    (which 0965 already carries). Each downstream guard consumes one: the near-label critic reads
--    the window, the menu ordering reads the domain, the disposition stage reads the type. NULL is
--    honest "not declared", never a default.
--  * target_provenance — how target_ref came to be: 'human_confirmed' (a person clicked, fuzzy path)
--    | 'user_typed' (the person literally named the column; code matched it — human-origin by
--    construction, shows-doesn't-gate) | 'exploring' (an explicit no-target declaration; near-label
--    candidates are withheld downstream, never silently trusted). NULL = the pre-intake legacy path
--    (client-supplied target_ref at considered-set time), unchanged.
--  * target_confirmed_by / target_confirmed_at — the audit half of "the veto runs on a value a human
--    signed".
-- All columns additive + NULL: every existing row and every legacy write path is untouched.
ALTER TABLE contract_intent ADD COLUMN IF NOT EXISTS target_window_days  integer     NULL;
ALTER TABLE contract_intent ADD COLUMN IF NOT EXISTS target_type         text        NULL;
ALTER TABLE contract_intent ADD COLUMN IF NOT EXISTS business_domain     jsonb       NULL;
ALTER TABLE contract_intent ADD COLUMN IF NOT EXISTS target_provenance   text        NULL;
ALTER TABLE contract_intent ADD COLUMN IF NOT EXISTS target_confirmed_by text        NULL;
ALTER TABLE contract_intent ADD COLUMN IF NOT EXISTS target_confirmed_at timestamptz NULL;

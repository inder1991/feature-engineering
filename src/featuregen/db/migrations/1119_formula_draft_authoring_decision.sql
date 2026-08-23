-- src/featuregen/db/migrations/1119_formula_draft_authoring_decision.sql
-- Stage I Task 5 — the per-draft AUTHOR_FORMULA decision, DURABLE where the worker re-reads it.
--
-- The generation and verification lanes both record their request-time decision on the row the
-- worker claims (§7.1/§8.2); authoring was the one governed act still deciding nothing durable.
-- The column lands on the AUTHORING PLAN, the row the worker already re-reads instead of
-- recomputing (owner ruling 2026-08-23 item 2) — one more fact the plan carries, same discipline.
--
-- ▲ A NEW FILE rather than an edit to 1104, deliberately: 1104 is unapplied THIS MINUTE, but the
-- §20.1 cutover is queued on the user's command and may apply it live between this edit and its
-- deploy — an edited migration whose original was applied live breaks the checksum ledger, and
-- the race is not worth a column. Nullable EXPAND (the 1095 lesson): plan rows written before
-- this file carry NULL, and the worker treats a NULL on a post-contract plan row as the bypass
-- it is (ACTION_DECISION_MISSING) — the column is nullable; the GATE is not.

ALTER TABLE formula_draft_authoring_plan
    ADD COLUMN IF NOT EXISTS action_decision_revision_id text
        REFERENCES action_decision_revision(decision_id);

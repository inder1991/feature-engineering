-- src/featuregen/db/migrations/1064_feature_lifecycle_state.sql
-- Remediation A2 (reservation 1064 per the 2026-08-13 remediation plan): the idea/governed
-- lifecycle split. Today every row in `feature` reads as equally real to the registry, the
-- model-consumer surface, and lineage/impact — PLAN-05's bypass writes land indistinguishable
-- from contract-confirmed features. This column makes the difference a FACT:
--
--   * 'idea'     — a saved sketch (direct POST /features). Browsable, labeled, and NEVER a
--                  model consumer's feature.
--   * 'governed' — created/promoted through contract confirmation, behind activation policy.
--
-- Backfill is deliberate, NOT a blanket default (validated finding 2): a row whose feature has
-- a CURRENT contract pointer earned 'governed'; everything else is honestly an idea. No
-- column default — every writer must state its intent (register_feature's mandatory kwarg).
ALTER TABLE feature ADD COLUMN IF NOT EXISTS lifecycle_state text;

UPDATE feature f SET lifecycle_state = CASE
    WHEN EXISTS (SELECT 1 FROM feature_current_contract fcc
                 WHERE fcc.feature_id = f.feature_id) THEN 'governed'
    ELSE 'idea'
END
WHERE lifecycle_state IS NULL;

ALTER TABLE feature ALTER COLUMN lifecycle_state SET NOT NULL;
ALTER TABLE feature DROP CONSTRAINT IF EXISTS feature_lifecycle_state_chk;
ALTER TABLE feature ADD CONSTRAINT feature_lifecycle_state_chk
    CHECK (lifecycle_state IN ('idea', 'governed'));

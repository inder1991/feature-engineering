-- src/featuregen/db/migrations/0975_recognition_dims.sql
-- Phase-2B multi-dimension recognition: the recognizer now PROPOSES two optional, human-confirmable
-- intent dimensions alongside the use-case candidates — the modelling framework/regime(s) it is being
-- modelled under (modelling_contexts) and a single soft prediction grain (target_entity) — plus the
-- non-fatal per-dimension warnings raised while cleaning them (e.g. UNKNOWN_MODELLING_CONTEXT). These
-- are stamped on the recognition ATTEMPT (the proposal), so they can later be reconciled against the
-- human-confirmed scope.
--
-- ADDITIVE ONLY: ALTER TABLE ADD COLUMN with defaults, so every existing intent_recognition_attempt
-- row backfills to the empty defaults ('[]'/NULL) and no Phase-1 behaviour changes. IF NOT EXISTS keeps
-- the migration re-appliable (idempotent). The parent table stays append-only (WORM) — the app role
-- may INSERT the new columns but never UPDATE/DELETE (grants unchanged from 0974).

ALTER TABLE intent_recognition_attempt
    ADD COLUMN IF NOT EXISTS modelling_contexts jsonb NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE intent_recognition_attempt
    ADD COLUMN IF NOT EXISTS target_entity text;

ALTER TABLE intent_recognition_attempt
    ADD COLUMN IF NOT EXISTS warnings jsonb NOT NULL DEFAULT '[]'::jsonb;

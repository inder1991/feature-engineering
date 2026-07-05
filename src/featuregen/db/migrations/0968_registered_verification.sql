-- src/featuregen/db/migrations/0968_registered_verification.sql
-- Persist the §14.5 verification stamp onto the DURABLE registered artifacts (was only in the Gate #1
-- audit snapshot). In the no-DB world a confirmed feature/contract is DESIGN-CHECKED (structurally safe
-- — gauntlet-passed); the column lets brownfield register-as-is stamp honestly + later DATA/USEFULNESS.
ALTER TABLE feature  ADD COLUMN IF NOT EXISTS verification text NOT NULL DEFAULT 'DESIGN-CHECKED';
ALTER TABLE contract ADD COLUMN IF NOT EXISTS verification text NOT NULL DEFAULT 'DESIGN-CHECKED';

-- src/featuregen/db/migrations/1002_contract_validation_status.sql
-- Phase-2 Slice 3 (3A-ii): carry the honest tri-state validation onto the persisted contract. A
-- feature confirmed while NEEDS_EXTERNAL_VALIDATION must persist that HONESTLY, never be silently
-- recorded as DESIGN_CHECKED. This is a NEW axis, SEPARATE from the hyphenated `verification` stamp
-- (0968/0973): validation_status uses the underscore VALIDATION_STATES vocabulary and carries the
-- typed requirements (each {code, operand:[catalog, object_ref], detail}). Re-confirm = a new row, so
-- the history of what still needed external validation is preserved.
ALTER TABLE contract ADD COLUMN IF NOT EXISTS validation_status text NOT NULL DEFAULT 'DESIGN_CHECKED';
ALTER TABLE contract ADD COLUMN IF NOT EXISTS requirements       jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE contract ADD CONSTRAINT contract_validation_status_ck
    CHECK (validation_status IN ('DESIGN_CHECKED', 'NEEDS_EXTERNAL_VALIDATION', 'REJECTED'));

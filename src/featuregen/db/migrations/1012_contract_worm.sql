-- src/featuregen/db/migrations/1012_contract_worm.sql
-- Delivery H2d — contract immutability (WORM). Closes the H2 pointer-model unit: a confirmed contract
-- VERSION is now physically immutable. Every later lifecycle change is an APPEND-ONLY validation event
-- (1009 feature_contract_validation_event) or a pointer REPOINT (1011 feature_current_contract) — NEVER
-- a mutation of the contract row. H2b made `confirm_contract` INSERT a NEW version each time (never an
-- UPDATE), and a repo-wide grep found NO `UPDATE contract` / `DELETE FROM contract` / upsert-on-contract
-- writer in `src/`, so this trigger only LOCKS IN the append-only posture the write path already follows
-- (it changes no behavior; it makes tampering physically impossible).
--
-- Mirrors the established write-once pattern (0900 events / 1009 validation log / 1011 input+dependency):
--   * a BEFORE UPDATE OR DELETE row trigger that RAISEs — a FOR EACH ROW trigger fires for EVERY role
--     (a superuser cannot bypass a trigger the way it bypasses grants), so row DML is physically blocked;
--   * a guarded REVOKE UPDATE, DELETE, TRUNCATE ON contract FROM featuregen_app — a FOR EACH ROW trigger
--     does NOT fire on a statement-level TRUNCATE, so the revoke is the TRUNCATE control. It is a
--     DEPLOYMENT control (production runs under the NON-superuser `featuregen_app` role; a superuser
--     bypasses grants), guarded by a role-exists check so it is a clean no-op in the superuser test
--     cluster where the role is absent. `feature_current_contract` is DELIBERATELY untouched — it is the
--     MUTABLE CAS pointer (repointing to a new version is an in-place UPDATE), matching 1011.
--
-- Idempotent / re-runnable in the repo style: CREATE OR REPLACE FUNCTION + CREATE OR REPLACE TRIGGER
-- (PostgreSQL 14+) + a guarded REVOKE, so apply_migrations stays safely re-runnable and the test suite
-- can re-apply this exact SQL.

CREATE OR REPLACE FUNCTION contract_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'contract versions are immutable (WORM): % not allowed on contract_id=%. Every '
        'lifecycle change is an append-only validation event or a pointer repoint, never a contract '
        'mutation.',
        TG_OP, COALESCE(OLD.contract_id, NEW.contract_id);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER contract_no_mutation
    BEFORE UPDATE OR DELETE ON contract
    FOR EACH ROW EXECUTE FUNCTION contract_write_once();

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'featuregen_app') THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON contract FROM featuregen_app;
    END IF;
END $$;

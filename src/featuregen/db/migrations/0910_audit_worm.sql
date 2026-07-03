-- src/featuregen/db/migrations/0910_audit_worm.sql
-- WORM (write-once, read-many) posture for the tamper-evident security_audit chain
-- (review BLOCKER #4). Two complementary controls:
--
--   1. A BEFORE UPDATE OR DELETE row trigger, so even a superuser cannot silently row-level
--      UPDATE or DELETE audit rows (edits / tail-truncation). This mirrors documents_no_mutation
--      (Phase 02) and events_no_mutation (0900). The identical trigger was first installed by
--      0071_security_audit_append_only; it is restated here (idempotently) so the consolidated
--      WORM posture lives in one place alongside the grant revoke. CREATE OR REPLACE FUNCTION +
--      CREATE OR REPLACE TRIGGER (PostgreSQL 14+) keeps apply_migrations re-runnable.
--
--   2. A grant revoke removing UPDATE/DELETE/TRUNCATE from the production application role.
--      A row trigger CANNOT stop a statement-level `TRUNCATE security_audit` (TRUNCATE does
--      not fire FOR EACH ROW triggers) and a superuser bypasses grants entirely, so blocking
--      TRUNCATE is a DEPLOYMENT control: production runs under the NON-superuser role
--      'featuregen_app', from which destructive DML is revoked here. The role does not exist
--      in ephemeral test clusters (tests run as the cluster superuser), so the revoke is
--      guarded and is a no-op there while remaining present for real deployments.
--
-- Sorts after 0900_events_write_once and 0071 (which creates security_audit).

CREATE OR REPLACE FUNCTION security_audit_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'security_audit is append-only: % not allowed on security_event_id=%',
        TG_OP, COALESCE(OLD.security_event_id, NEW.security_event_id);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER security_audit_no_mutation
    BEFORE UPDATE OR DELETE ON security_audit
    FOR EACH ROW EXECUTE FUNCTION security_audit_append_only();

-- Guarded grant revoke: revoke destructive DML from the production app role when it exists.
-- No-op in the superuser test cluster where 'featuregen_app' is absent.
--
-- security_audit AND events are both revoked here. The events_no_mutation trigger (0900) is a
-- BEFORE UPDATE OR DELETE row trigger and, like every FOR EACH ROW trigger, does NOT fire on a
-- statement-level TRUNCATE — so without this revoke the source-of-truth `events` ledger could be
-- TRUNCATE'd by the app role. This is the SAME deployment control as for security_audit (a
-- superuser still bypasses grants entirely; blocking TRUNCATE therefore relies on production
-- running under the NON-superuser 'featuregen_app' role).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'featuregen_app') THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON security_audit FROM featuregen_app;
        REVOKE UPDATE, DELETE, TRUNCATE ON events FROM featuregen_app;
    END IF;
END $$;

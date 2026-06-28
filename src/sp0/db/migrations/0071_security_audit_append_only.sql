-- src/sp0/db/migrations/0071_security_audit_append_only.sql
-- Physical append-only enforcement for the tamper-evident security_audit chain (§6.2).
--
-- security_audit is a hash-chained, regulator-retained audit stream. The entry_hash chain
-- (verify_chain) is tamper-EVIDENT, but without a physical guard a privileged actor could
-- still UPDATE/DELETE rows (tail-truncation, edits). This trigger makes such mutation
-- physically impossible at the row level, mirroring documents_no_mutation (Phase 02) and
-- feature_versions_no_mutation (Phase 06). Rows are insert-only; no UPDATE, no DELETE.
--
-- Sorts after 0070_identity_authz_gates (which creates security_audit). Idempotent:
-- CREATE OR REPLACE FUNCTION + CREATE OR REPLACE TRIGGER (PostgreSQL 14+), so the whole
-- migration set can be re-applied against an existing schema.
CREATE OR REPLACE FUNCTION security_audit_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'security_audit is append-only: % not allowed on security_event_id=%',
        TG_OP, COALESCE(OLD.security_event_id, NEW.security_event_id);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER security_audit_no_mutation
    BEFORE UPDATE OR DELETE ON security_audit
    FOR EACH ROW EXECUTE FUNCTION security_audit_append_only();

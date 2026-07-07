-- src/featuregen/db/migrations/0971_worm_truncate_revoke.sql
-- Extend the WORM TRUNCATE guard to llm_call and blob. Both have row-level no_mutation triggers
-- (0510/0511, BEFORE UPDATE OR DELETE FOR EACH ROW) — but a row trigger does NOT fire on a
-- statement-level TRUNCATE, so the append-only LLM-call audit and the immutable blob store could be
-- TRUNCATE'd by the production app role. Mirror 0910 (security_audit/events): revoke destructive DML
-- from featuregen_app when it exists (no-op in the superuser test cluster; a superuser bypasses grants
-- regardless, so this control relies on production running under the NON-superuser role).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'featuregen_app') THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON llm_call FROM featuregen_app;
        REVOKE UPDATE, DELETE, TRUNCATE ON blob     FROM featuregen_app;
    END IF;
END $$;

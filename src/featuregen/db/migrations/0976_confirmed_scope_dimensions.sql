-- src/featuregen/db/migrations/0976_confirmed_scope_dimensions.sql
-- Phase-2B confirmed-scope dimensions: the recognizer PROPOSES two optional intent dimensions on the
-- attempt (0975 modelling_contexts / target_entity); the human CONFIRMS them at Gate #1. This is the
-- confirmed half — a normalized child of confirmed_generation_scope, one row per confirmed dimension
-- value, carrying rich provenance: the ``source`` (did the human accept the LLM proposal, add a value,
-- replace one, or inherit a project/organization default) and, for a replacement, the ``replaces_value``
-- it superseded. Joining these confirmed rows against the attempt's proposals (via
-- confirmed_generation_scope.recognition_id) yields the proposed-vs-confirmed delta
-- (accepted / rejected / added / replaced) — see scope_records.confirmation_delta.
--
-- Mirrors confirmed_scope_use_case (0974): normalized child, cascades on scope delete for referential
-- tidiness (the parent is append-only). An ``unscoped`` scope confirms no dimensions -> zero rows.
-- Append-only (WORM): the app role may INSERT but never UPDATE/DELETE/TRUNCATE (mirror 0971).

CREATE TABLE IF NOT EXISTS confirmed_scope_dimension (
    scope_id       text NOT NULL REFERENCES confirmed_generation_scope (scope_id) ON DELETE CASCADE,
    dimension      text NOT NULL CHECK (dimension IN ('modelling_context', 'target_entity')),
    value          text NOT NULL,
    source         text NOT NULL CHECK (source IN (
                        'accepted_llm_proposal', 'user_added', 'user_replacement',
                        'project_default', 'organization_default')),
    replaces_value text NULL,
    display_order  int  NOT NULL DEFAULT 0,
    PRIMARY KEY (scope_id, dimension, value)
);
CREATE INDEX IF NOT EXISTS confirmed_scope_dimension_scope_idx
    ON confirmed_scope_dimension (scope_id);

-- WORM append-only guard (mirror 0971_worm_truncate_revoke.sql): revoke destructive DML from the
-- production app role when it exists. No-op in the superuser test cluster (a superuser bypasses
-- grants), so this control relies on production running under the NON-superuser featuregen_app role.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'featuregen_app') THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON confirmed_scope_dimension FROM featuregen_app;
    END IF;
END $$;

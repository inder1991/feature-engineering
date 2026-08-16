-- src/featuregen/db/migrations/1085_environment_scoped_publication.sql
-- C-D6 (docs/superpowers/plans/2026-08-15-integration-consolidation.md): environment scoping on
-- `group_binding` and `feature_active_revision`.
--
-- THE DEFECT THIS CLOSES. Both tables are keyed on `logical_group_name` alone, and every reader is
-- environment-blind. `publish.py:790` reads the active revision `WHERE logical_group_name = %s`;
-- `publish.py:805` computes the next seq the same way. So the moment a second environment exists,
-- environment B's first publication computes `max(seq)+1` ACROSS BOTH environments, which does
-- strictly extend B's (empty) sequence — so 1055's ordering trigger passes — and `read_active_revision`
-- then hands B a row belonging to A. The trigger is precisely the mechanism that would otherwise
-- have made the wrong seq visible, which is why the schema, the trigger, the sequence calculation
-- and the readers are ONE coordinated change and cannot be landed separately.
--
-- WHY environment_id IS A COLUMN AND NOT PART OF THE NAME. `physical_target_for` and
-- `derive_namespace` are consumed INSIDE the renderer (`render/project.py:1305-1307`) and their
-- output lands in the sealed bytes, so mangling the environment into the group name would move
-- `generated_project_hash` and invalidate every artifact manifest and execution proof. But that is
-- the smaller reason. The real one: an environment is DEPLOYMENT PLACEMENT, not feature meaning.
-- The same feature, computed the same way, is the same feature in sandbox and in production.
--
-- NULLABLE, WITH PARTIAL INDEXES — house precedent, not a default choice. `1070:38-42` and
-- `1069:29-34` both add a column with NO backfill and say why: "Legacy rows keep a permanent,
-- truthful NULL." The alternative (`ADD COLUMN ... NOT NULL DEFAULT 'sandbox'`) would assert that
-- every existing publication happened in an environment nobody recorded, and both target tables
-- carry append-only triggers, so a backfill UPDATE would have to fight a guard whose whole purpose
-- is to refuse rewrites. The one in-repo `ADD COLUMN + UPDATE + SET NOT NULL` example
-- (`1064:14-26`) is on `feature`, which has no such trigger.
--
-- THE V1/V2 NAMESPACE, RECONCILED. The plan asks for this and notes environment_id alone does not
-- solve it. It is resolved DELIBERATELY IN FAVOUR OF ONE FLAT NAMESPACE PER ENVIRONMENT, with the
-- language recorded for provenance and NOT part of the key. Two groups named `account_daily` in one
-- environment publish to ONE physical table, whatever language authored them — so that is a
-- collision regardless, and a language discriminator in the key would permit it. C-D7's allocator
-- enforces the same rule at allocation time by checking every bound name; this makes it a database
-- fact. `formula_language` answers "which language produced this binding", which is an audit
-- question, not an identity one.
--
-- NOT APPLIED. This file is written, not run. Applying it is an operator action, and it must ship
-- in the SAME release as the Python half (`publish.py`, `materialization_runs.py`) — see the top.

-- ── group_binding ────────────────────────────────────────────────────────────────────────────────
ALTER TABLE group_binding
    ADD COLUMN IF NOT EXISTS environment_id   text CHECK (btrim(environment_id) <> ''),
    ADD COLUMN IF NOT EXISTS formula_language text CHECK (formula_language IN ('v1', 'v2'));

-- The old flat rule was an INLINE, UNNAMED `UNIQUE` (1034:88), which PostgreSQL auto-names
-- `group_binding_logical_group_name_key`. Dropped by that name, IF EXISTS, so re-running is safe
-- and so is a database that never had it.
ALTER TABLE group_binding
    DROP CONSTRAINT IF EXISTS group_binding_logical_group_name_key;

-- Two partial indexes rather than one constraint over a nullable column, because
-- `UNIQUE (environment_id, logical_group_name)` would treat every legacy NULL as distinct and
-- silently permit duplicate legacy names that the old constraint forbade.
CREATE UNIQUE INDEX IF NOT EXISTS group_binding_scoped_name
    ON group_binding (environment_id, logical_group_name)
    WHERE environment_id IS NOT NULL;

-- Legacy rows keep EXACTLY the guarantee they were written under. This cannot fail on existing
-- data, because it is the same rule the dropped constraint enforced over the same rows.
CREATE UNIQUE INDEX IF NOT EXISTS group_binding_legacy_name
    ON group_binding (logical_group_name)
    WHERE environment_id IS NULL;

-- ── feature_active_revision ──────────────────────────────────────────────────────────────────────
ALTER TABLE feature_active_revision
    ADD COLUMN IF NOT EXISTS environment_id text CHECK (btrim(environment_id) <> '');

ALTER TABLE feature_active_revision
    DROP CONSTRAINT IF EXISTS feature_active_revision_ordered;

CREATE UNIQUE INDEX IF NOT EXISTS feature_active_revision_scoped_seq
    ON feature_active_revision (environment_id, logical_group_name, seq)
    WHERE environment_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS feature_active_revision_legacy_seq
    ON feature_active_revision (logical_group_name, seq)
    WHERE environment_id IS NULL;

-- The current-pointer read path, scoped. The unscoped index stays for the legacy rows it serves.
CREATE INDEX IF NOT EXISTS feature_active_revision_scoped_current
    ON feature_active_revision (environment_id, logical_group_name, seq DESC)
    WHERE environment_id IS NOT NULL;

-- ── the ordering guard, environment-aware ────────────────────────────────────────────────────────
-- `IS NOT DISTINCT FROM` rather than `=`: a legacy row's environment is NULL, and `NULL = NULL` is
-- NULL, so `=` would compare a legacy row against nothing and let any seq through. The guard must
-- treat "both legacy" as "same group", which is what the legacy rows were written under.
CREATE OR REPLACE FUNCTION feature_active_revision_ordered()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM feature_active_revision
        WHERE logical_group_name = NEW.logical_group_name
          AND environment_id IS NOT DISTINCT FROM NEW.environment_id
          AND seq >= NEW.seq
    ) THEN
        RAISE EXCEPTION
            'feature_active_revision: seq % does not extend group % in environment %',
            NEW.seq, NEW.logical_group_name, coalesce(NEW.environment_id, '<legacy>');
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS feature_active_revision_ordered ON feature_active_revision;
CREATE TRIGGER feature_active_revision_ordered
    BEFORE INSERT ON feature_active_revision
    FOR EACH ROW EXECUTE FUNCTION feature_active_revision_ordered();

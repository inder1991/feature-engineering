-- src/featuregen/db/migrations/1013_recipe_aggregation_declaration.sql
-- Delivery H (H3a) — the DURABLE, versioned recipe aggregation-declaration registry.
--
-- The contract compiler ALREADY consumes aggregation declarations via
-- CompilerContext.agg_declarations ((recipe_id, need_role) -> declared AggregationFunction), but in
-- production that map was EMPTY — declarations were injected in-memory only for gold tests, so a
-- recipe whose aggregation cannot be soundly auto-derived (a non-additive rate, a stock rolled
-- across time, a weighted_average) failed with unresolved_aggregation_declaration. This table is the
-- governed source that populates agg_declarations in production, loaded ONCE per run by
-- build_compiler_context (load_aggregation_declarations). Preserves the compiler invariant: the
-- ONLY source of a declared function is this registry — the compiler NEVER infers one from the
-- column (validate, never fabricate).
--
-- IMMUTABLE-PER-VERSION (WORM). A declaration is write-once: a change is a NEW row with a NEW
-- declaration_version + a fresh [effective_from, effective_to) interval, NEVER an in-place UPDATE.
-- The read projection selects exactly ONE active declaration per (recipe_id, need_role) whose
-- interval contains the compile time; two overlapping active rows for one key are a CONFLICT that
-- fails the compile (fail-closed — never a silent pick). Mirrors the established write-once pattern
-- (0900 events / 1002 live-activation / 1012 contract):
--   * a BEFORE UPDATE OR DELETE row trigger that RAISEs — physically blocks row DML for every role
--     including the owner under the normal session_replication_role = origin. It is NOT an absolute
--     bar against a superuser (who can set session_replication_role = replica or DISABLE TRIGGER);
--     the real production guarantee is this trigger PLUS the NON-superuser featuregen_app role;
--   * a guarded REVOKE UPDATE, DELETE, TRUNCATE ON ... FROM featuregen_app — a FOR EACH ROW trigger
--     does NOT fire on a statement-level TRUNCATE, so the revoke is the TRUNCATE control. It is a
--     DEPLOYMENT control (production runs under the NON-superuser featuregen_app role; a superuser
--     bypasses grants too), guarded by a role-exists check so it is a clean no-op in the superuser
--     test cluster where the role is absent.
--
-- Idempotent / re-runnable in the repo style: CREATE TABLE IF NOT EXISTS + CREATE OR REPLACE
-- FUNCTION + CREATE OR REPLACE TRIGGER (PostgreSQL 14+) + a guarded REVOKE, so apply_migrations
-- stays safely re-runnable and the test suite can re-apply this exact SQL.

CREATE TABLE IF NOT EXISTS recipe_aggregation_declaration (
    declaration_id       text        PRIMARY KEY,
    recipe_id            text        NOT NULL,
    need_role            text        NOT NULL,
    -- the declared aggregation function compile_aggregation consumes (the AggregationFunction vocab).
    function             text        NOT NULL CHECK (function IN (
        'sum', 'count', 'min', 'max', 'weighted_average', 'ratio_recompute', 'take_latest')),
    declaration_version  integer     NOT NULL,
    authority            text        NOT NULL,           -- provenance/authority of the declaration
    provenance           text        NULL,               -- optional provenance detail
    effective_from       timestamptz NOT NULL,
    effective_to         timestamptz NULL,               -- NULL = open interval (active from _from on)
    -- immutable content hash of (recipe_id, need_role, function, declaration_version, authority) —
    -- verified on read, so a superuser-bypassed row mutation is detectable (fail-closed).
    content_hash         text        NOT NULL,
    created_at           timestamptz NOT NULL DEFAULT now(),
    -- a (recipe, role, version) is minted once — a new version is a NEW row, never a re-mint.
    UNIQUE (recipe_id, need_role, declaration_version),
    -- a closed interval must be well-formed: to > from (an open interval is NULL and unconstrained).
    CHECK (effective_to IS NULL OR effective_to > effective_from)
);

CREATE INDEX IF NOT EXISTS recipe_aggregation_declaration_key_idx
    ON recipe_aggregation_declaration (recipe_id, need_role);

CREATE OR REPLACE FUNCTION recipe_aggregation_declaration_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'recipe aggregation declarations are immutable-per-version (WORM): % not '
        'allowed on declaration_id=%. A change is a NEW version (a new row), never a mutation.',
        TG_OP, COALESCE(OLD.declaration_id, NEW.declaration_id);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER recipe_aggregation_declaration_no_mutation
    BEFORE UPDATE OR DELETE ON recipe_aggregation_declaration
    FOR EACH ROW EXECUTE FUNCTION recipe_aggregation_declaration_write_once();

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'featuregen_app') THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON recipe_aggregation_declaration FROM featuregen_app;
    END IF;
END $$;

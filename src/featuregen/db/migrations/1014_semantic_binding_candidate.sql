-- src/featuregen/db/migrations/1014_semantic_binding_candidate.sql
-- Delivery D (D1) — the IMMUTABLE semantic-binding candidate store.
--
-- Delivery D produces REVIEWABLE relationship candidates at ingestion time — a column's business
-- entity (entity_assignment) and a measure's currency column (currency_binding) — WITHOUT letting the
-- LLM invent identity/truth. This migration is the durable STORE those candidates land in:
--   * semantic_binding_candidate_set      — one immutable set per (run, attempt, table, fingerprint).
--   * semantic_binding_candidate          — one immutable candidate per (set, kind, subject, target).
--   * current_semantic_binding_candidate_set — the MUTABLE compare-and-swap current-projection.
--   * semantic_binding_candidate_proposal — the insert-only candidate->governed-fact link (D2/E wire).
--
-- NO fact is created here (candidates only). `binding_kind` values MATCH the E1 governed fact types
-- (`entity_assignment` / `currency_binding`); a candidate is LINKED to a governed fact — never made
-- into one — via semantic_binding_candidate_proposal AFTER propose_fact succeeds (D2/D4).
--
-- IMMUTABLE (WORM). A candidate set and its candidates are write-once: a change is a NEW attempt (a new
-- row with a new attempt_no), NEVER an in-place UPDATE. Mirrors the established write-once pattern
-- (0900 events / 1002 live-activation / 1012 contract / 1013 declaration):
--   * a BEFORE UPDATE OR DELETE row trigger that RAISEs — blocks row DML for every role including the
--     owner under the normal session_replication_role = origin. It is NOT an absolute bar against a
--     superuser (who can set session_replication_role = replica or DISABLE TRIGGER); the real
--     production guarantee is this trigger PLUS the NON-superuser featuregen_app role;
--   * a guarded REVOKE UPDATE, DELETE, TRUNCATE ... FROM featuregen_app — a FOR EACH ROW trigger does
--     NOT fire on a statement-level TRUNCATE, so the revoke is the TRUNCATE control. Guarded by a
--     role-exists check so it is a clean no-op in the superuser test cluster where the role is absent.
-- `current_semantic_binding_candidate_set` is DELIBERATELY untouched — it is the MUTABLE CAS
-- projection (repointing to a new set is an in-place UPDATE), matching feature_current_contract (1011).
-- `semantic_binding_candidate_proposal` is insert-only with a no-UPDATE trigger (DELETE stays open so a
-- stale DRAFT link can be retired when its candidate leaves the current set — see D1 store_projection).
--
-- Kind CHECKs (fail-closed shape): `currency_binding` requires a TARGET column
-- (`target_graph_ref NOT NULL`) and NO free value (`proposed_value` SQL NULL — the currency IS the
-- target ref); `entity_assignment` requires a registry value in `proposed_value` (NOT NULL) and NO
-- target ref (`target_graph_ref NULL` — the entity is a closed-vocabulary member, not a catalog ref).
--
-- Idempotent / re-runnable in the repo style: CREATE TABLE IF NOT EXISTS + CREATE OR REPLACE FUNCTION
-- + CREATE OR REPLACE TRIGGER (PostgreSQL 14+) + a guarded REVOKE, so apply_migrations stays safely
-- re-runnable and the test suite can re-apply this exact SQL.

-- =====================================================================================================
-- 1) semantic_binding_candidate_set — IMMUTABLE. One set per (run, attempt, table, input fingerprint,
--    task versions). The deterministic candidate_set_id is a stable hash of exactly the UNIQUE tuple,
--    so replaying the SAME attempt yields the SAME id (idempotent ON CONFLICT); an explicit RETRY is a
--    NEW attempt_no -> a NEW id -> a NEW row that may SUPERSEDE a partial/failed attempt WITHOUT
--    mutating it. `content_hash` is the deterministic hash of the set's candidate content — verified on
--    rebuild (fail-closed on an impossible content-hash conflict).
-- =====================================================================================================
CREATE TABLE IF NOT EXISTS semantic_binding_candidate_set (
    candidate_set_id          text        PRIMARY KEY,
    catalog_source            text        NOT NULL,
    table_graph_ref           text        NOT NULL,
    ingestion_run_id          text        NOT NULL,
    attempt_no                integer     NOT NULL CHECK (attempt_no >= 1),
    metadata_input_fingerprint text       NOT NULL,
    task_version              text        NOT NULL,
    prompt_version            text        NOT NULL,
    schema_version            text        NOT NULL,
    config_version            text        NOT NULL,
    completion_status         text        NOT NULL
        CHECK (completion_status IN ('complete', 'partial', 'failed')),
    content_hash              text        NOT NULL,
    created_at                timestamptz NOT NULL DEFAULT now(),
    -- idempotent replay: the SAME attempt (all identity dims equal) is minted once.
    UNIQUE (ingestion_run_id, attempt_no, catalog_source, table_graph_ref,
            metadata_input_fingerprint, task_version, prompt_version, schema_version, config_version)
);
CREATE INDEX IF NOT EXISTS semantic_binding_candidate_set_table_idx
    ON semantic_binding_candidate_set (catalog_source, table_graph_ref);

-- =====================================================================================================
-- 2) semantic_binding_candidate — IMMUTABLE. One candidate per (set, kind, subject, target, input).
--    The deterministic candidate_id is a stable hash of exactly the UNIQUE tuple. `evidence_json` (the
--    "+ evidence" the brief names) rides on this immutable row — no separate evidence table.
-- =====================================================================================================
CREATE TABLE IF NOT EXISTS semantic_binding_candidate (
    candidate_id       text        PRIMARY KEY,
    candidate_set_id   text        NOT NULL
        REFERENCES semantic_binding_candidate_set (candidate_set_id),
    catalog_source     text        NOT NULL,
    subject_graph_ref  text        NOT NULL,
    subject_logical_ref text       NOT NULL,
    binding_kind       text        NOT NULL
        CHECK (binding_kind IN ('currency_binding', 'entity_assignment')),
    target_graph_ref   text        NULL,
    target_logical_ref text        NULL,
    proposed_value     jsonb       NULL,
    disposition        text        NOT NULL CHECK (disposition IN ('strong', 'weak', 'rejected')),
    reason_codes       jsonb       NOT NULL DEFAULT '[]',
    evidence_json      jsonb       NOT NULL DEFAULT '{}',
    input_hash         text        NOT NULL,
    model_version      text        NOT NULL,
    prompt_version     text        NOT NULL,
    schema_version     text        NOT NULL,
    config_version     text        NOT NULL,
    llm_call_ref       text        NULL REFERENCES llm_dispatch (dispatch_ref),
    created_at         timestamptz NOT NULL DEFAULT now(),
    -- Kind shape (fail-closed): currency binds a TARGET column with no free value; entity carries a
    -- registry value with no target ref. This also pins binding_kind to the closed pair.
    CONSTRAINT semantic_binding_candidate_kind_shape CHECK (
        (binding_kind = 'currency_binding'
             AND target_graph_ref IS NOT NULL AND proposed_value IS NULL)
        OR
        (binding_kind = 'entity_assignment'
             AND target_graph_ref IS NULL AND proposed_value IS NOT NULL)
    ),
    -- deterministic id backing: NULLS NOT DISTINCT so an entity_assignment's NULL target still collides
    -- (PostgreSQL 15+). The PK is the primary idempotency backstop; this is the secondary guard.
    UNIQUE NULLS NOT DISTINCT
        (candidate_set_id, binding_kind, subject_graph_ref, target_graph_ref, input_hash)
);
CREATE INDEX IF NOT EXISTS semantic_binding_candidate_set_fk_idx
    ON semantic_binding_candidate (candidate_set_id);

-- =====================================================================================================
-- 3) current_semantic_binding_candidate_set — the MUTABLE compare-and-swap current-projection (NO
--    WORM). At most one row per (catalog_source, table_graph_ref). `candidate_set_id` is NULL when
--    currentness is `unverifiable` (a partial/failed set, or a set whose fingerprint no longer matches
--    the table's live metadata) — never silently keeping a stale set current. Rebuildable with NO LLM.
-- =====================================================================================================
CREATE TABLE IF NOT EXISTS current_semantic_binding_candidate_set (
    catalog_source             text        NOT NULL,
    table_graph_ref            text        NOT NULL,
    candidate_set_id           text        NULL
        REFERENCES semantic_binding_candidate_set (candidate_set_id),
    metadata_input_fingerprint text        NOT NULL,
    status                     text        NOT NULL CHECK (status IN ('current', 'unverifiable')),
    projected_at               timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (catalog_source, table_graph_ref),
    -- a `current` projection MUST point at a set; an `unverifiable` gap MUST NOT.
    CONSTRAINT current_semantic_binding_status_shape CHECK (
        (status = 'current' AND candidate_set_id IS NOT NULL)
        OR
        (status = 'unverifiable' AND candidate_set_id IS NULL)
    )
);

-- =====================================================================================================
-- 4) semantic_binding_candidate_proposal — the insert-only candidate -> governed-fact LINK, written
--    ONLY after propose_fact succeeds (D2/D4). Not a fact; a durable pointer from a candidate to the
--    fact_key / event it justified. UPDATE is blocked (WORM the link content); DELETE stays open so a
--    stale DRAFT link is retired when its candidate leaves the current set (store_projection). A
--    VERIFIED fact's link is NEVER deleted — its survival past its candidate leaving IS the durable
--    divergence/re-review signal.
-- =====================================================================================================
CREATE TABLE IF NOT EXISTS semantic_binding_candidate_proposal (
    candidate_id      text        PRIMARY KEY
        REFERENCES semantic_binding_candidate (candidate_id),
    fact_key          text        NOT NULL,
    proposed_event_id text        NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS semantic_binding_candidate_proposal_fact_idx
    ON semantic_binding_candidate_proposal (fact_key);

-- =====================================================================================================
-- WORM triggers — the two immutable tables RAISE on UPDATE or DELETE; the proposal link RAISEs on
-- UPDATE only (DELETE = retire a stale link).
-- =====================================================================================================
CREATE OR REPLACE FUNCTION semantic_binding_candidate_set_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'semantic_binding_candidate_set is immutable (WORM): % not allowed on '
        'candidate_set_id=%. A change is a NEW attempt (a new row), never a mutation.',
        TG_OP, COALESCE(OLD.candidate_set_id, NEW.candidate_set_id);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER semantic_binding_candidate_set_no_mutation
    BEFORE UPDATE OR DELETE ON semantic_binding_candidate_set
    FOR EACH ROW EXECUTE FUNCTION semantic_binding_candidate_set_write_once();

CREATE OR REPLACE FUNCTION semantic_binding_candidate_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'semantic_binding_candidate is immutable (WORM): % not allowed on candidate_id=%. '
        'A change is a NEW attempt (a new row), never a mutation.',
        TG_OP, COALESCE(OLD.candidate_id, NEW.candidate_id);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER semantic_binding_candidate_no_mutation
    BEFORE UPDATE OR DELETE ON semantic_binding_candidate
    FOR EACH ROW EXECUTE FUNCTION semantic_binding_candidate_write_once();

CREATE OR REPLACE FUNCTION semantic_binding_candidate_proposal_no_update() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'semantic_binding_candidate_proposal is insert-only: UPDATE not allowed on '
        'candidate_id=%. Retire a stale DRAFT link by DELETE; a VERIFIED link is never touched.',
        OLD.candidate_id;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER semantic_binding_candidate_proposal_no_mutation
    BEFORE UPDATE ON semantic_binding_candidate_proposal
    FOR EACH ROW EXECUTE FUNCTION semantic_binding_candidate_proposal_no_update();

-- =====================================================================================================
-- Guarded REVOKE — destructive DML off the NON-superuser app role (the TRUNCATE control the row
-- triggers cannot provide). The two immutable tables lose UPDATE/DELETE/TRUNCATE; the proposal link
-- loses UPDATE/TRUNCATE but KEEPS DELETE (stale-link retirement) + INSERT + SELECT. The mutable current
-- projection is left fully writable (the app repoints it via CAS). No-op where the role is absent.
-- =====================================================================================================
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'featuregen_app') THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON semantic_binding_candidate_set FROM featuregen_app;
        REVOKE UPDATE, DELETE, TRUNCATE ON semantic_binding_candidate FROM featuregen_app;
        REVOKE UPDATE, TRUNCATE ON semantic_binding_candidate_proposal FROM featuregen_app;
    END IF;
END $$;

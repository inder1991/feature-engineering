-- src/featuregen/db/migrations/0974_intent_scope_records.sql
-- Phase-1B scoped grounding: append-only records for the intent -> recognition -> run -> scope
-- lineage. An objective's intent (contract_intent, immutable) is RECOGNISED into a proposal
-- (intent_recognition_attempt, idempotent on intent+redacted-input) BEFORE any generation exists.
-- When a human commits to generate, a generation run is minted and the confirmed scope is written
-- (confirmed_generation_scope) with the canonical run->scope linkage UNIQUE(generation_run_id) —
-- exactly one governing scope per run; supersedes_scope_id is lineage/history only, never used to
-- derive the governing scope. The accepted use-cases are stored normalized as child rows
-- (confirmed_scope_use_case), one per use-case, each stamped with its origin — so the recognizer's
-- PROPOSALS (attempt.candidates) versus the human's CHOICES (child rows) delta stays queryable.
-- All three tables are append-only (WORM): the app role may INSERT but never UPDATE/DELETE/TRUNCATE.

-- Recognition attempt — the recognizer's proposal for an intent. No generation_run_id: recognition
-- precedes generation. Idempotent on (intent_id, input_hash): the same intent + redacted input
-- resolves to the same attempt. candidates holds the recognizer's PROPOSALS; the version quintet
-- (taxonomy / applicability-mapping / recognizer-model / prompt / recipe-registry) stamps replay.
CREATE TABLE IF NOT EXISTS intent_recognition_attempt (
    recognition_id               text        PRIMARY KEY,
    intent_id                    text        NOT NULL,
    input_hash                   text        NOT NULL,
    status                       text        NOT NULL,
    candidates                   jsonb       NOT NULL DEFAULT '[]'::jsonb,
    ambiguity_note               text        NULL,
    taxonomy_version             text        NOT NULL,
    applicability_mapping_version text       NOT NULL,
    recognizer_model_id          text        NOT NULL,
    prompt_version               text        NOT NULL,
    recipe_registry_version      text        NOT NULL,
    created_at                   timestamptz NOT NULL DEFAULT now(),
    created_by                   jsonb       NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (intent_id, input_hash)
);
CREATE INDEX IF NOT EXISTS intent_recognition_attempt_intent_idx
    ON intent_recognition_attempt (intent_id);

-- Confirmed generation scope — the human-confirmed governing scope for exactly one generation run.
-- UNIQUE(generation_run_id) is the canonical run->scope linkage (scope_for_run looks up by run id).
-- recognition_id ties the scope back to the proposal it was confirmed from (nullable: a scope may be
-- authored without a recognition, e.g. broaden-to-unscoped). supersedes_scope_id is lineage ONLY.
CREATE TABLE IF NOT EXISTS confirmed_generation_scope (
    scope_id            text        PRIMARY KEY,
    intent_id           text        NOT NULL,
    generation_run_id   text        NOT NULL,
    recognition_id      text        NULL REFERENCES intent_recognition_attempt (recognition_id),
    supersedes_scope_id text        NULL REFERENCES confirmed_generation_scope (scope_id),
    expansion           text        NOT NULL,
    scope_mode          text        NOT NULL CHECK (scope_mode IN ('scoped', 'unscoped')),
    confirmation_source text        NOT NULL,
    confirmed_by        text        NOT NULL,
    confirmed_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (generation_run_id)
);
CREATE INDEX IF NOT EXISTS confirmed_generation_scope_intent_idx
    ON confirmed_generation_scope (intent_id);

-- Confirmed scope use-cases — normalized child, one row per accepted use-case. relationship marks
-- primary/secondary; origin records whether the LLM proposed it, the user added it, or the user
-- overrode a proposal. Cascades on scope delete for referential tidiness (the parent is append-only).
CREATE TABLE IF NOT EXISTS confirmed_scope_use_case (
    scope_id      text NOT NULL REFERENCES confirmed_generation_scope (scope_id) ON DELETE CASCADE,
    use_case_id   text NOT NULL,
    relationship  text NOT NULL CHECK (relationship IN ('primary', 'secondary')),
    origin        text NOT NULL CHECK (origin IN ('llm_proposed', 'user_added', 'user_overridden')),
    display_order int  NOT NULL DEFAULT 0,
    PRIMARY KEY (scope_id, use_case_id)
);
CREATE INDEX IF NOT EXISTS confirmed_scope_use_case_scope_idx
    ON confirmed_scope_use_case (scope_id);

-- WORM append-only guard (mirror 0971_worm_truncate_revoke.sql): revoke destructive DML from the
-- production app role when it exists. No-op in the superuser test cluster (a superuser bypasses
-- grants), so this control relies on production running under the NON-superuser featuregen_app role.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'featuregen_app') THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON intent_recognition_attempt FROM featuregen_app;
        REVOKE UPDATE, DELETE, TRUNCATE ON confirmed_generation_scope FROM featuregen_app;
        REVOKE UPDATE, DELETE, TRUNCATE ON confirmed_scope_use_case   FROM featuregen_app;
    END IF;
END $$;

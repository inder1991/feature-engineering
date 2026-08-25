-- src/featuregen/db/migrations/1130_execution_context_revision.sql
--
-- Cross-catalog serving plan (2026-08-24, rev 13) task A3: the server-owned EXECUTION CONTEXT —
-- one immutable, content-addressed revision per (environment_id, execution_tier, purpose) triple.
-- This is the type `PhysicalExecutionPlanV1.execution_context_revision_id` points at, and the id
-- half of R3's adoption key `(selection_revision_id, execution_context_revision_id)`. R2 pins the
-- other direction: NOTHING here may ever enter logical identity — the logical plan's digest is
-- proven identical across different context revisions.
--
-- RESERVATION. main and the live cluster top out at 1121; 1122-1129 belong to another program
-- (the remediation plan's registry — its 1121 is dead, the number applied live as
-- governed_telemetry_outbox). 1130-1139 were assigned to this plan by T0 at live head 1121; the
-- §T0 mapping row for 1130 is "execution-context revision store" (task A3). Migration files apply
-- lexically and are checksummed by the ledger — immutable once merged or applied anywhere.
--
-- THE ENVIRONMENT BINDING, verified before writing this file: the platform has NO canonical
-- environment table. `environment_id` is a COLUMN CONVENTION — born on `data_source_connection`
-- (1037, `text NOT NULL`), routed on by 1041's `(kind, tier, environment_id)` partial-unique
-- index, and spelled `text NOT NULL CHECK (btrim(environment_id) <> '')` on every later surface
-- (1034, 1074-1078, 1081, 1082, 1085, 1095, 1114). The column is not unique anywhere, so a
-- FOREIGN KEY is impossible; the honest binding is the same named non-blank CHECK every sibling
-- uses plus the store's own validation.
--
-- TIER SPELLINGS, verified: `bridge_realization.ExecutionTier` is a StrEnum whose members are
-- SANDBOX = 'sandbox' and PRODUCTION = 'production'. The PERSISTED spelling everywhere (the
-- jsonb applicability-scope payloads round-trip `ExecutionTier(payload[...])` by VALUE) is the
-- lowercase value, so the CHECK closes over 'sandbox'/'production' — never the Python member
-- NAMES, which would mint a second spelling of the same vocabulary.
--
-- PURPOSE, closed: the step-3/4 purpose vocabulary has exactly one member today —
-- `bridge_realization_proposal.FEATURE_GENERATION_PURPOSE = 'feature_generation'`. Widening the
-- vocabulary is a NEW migration (this file is checksummed), which is the review gate we want.
--
-- Shape decisions:
--  * APPEND-ONLY (the 1034/1060/1062/1120 guard idiom: a row-level BEFORE UPDATE OR DELETE
--    trigger and a statement-level BEFORE TRUNCATE trigger sharing one plpgsql raiser). A context
--    revision is an identity other records point at; editing it would silently re-aim every
--    adoption and physical plan that pinned it.
--  * CONTENT-ADDRESSED: revision_id = 'ecx_' || sha256(canonical semantic payload), minted by the
--    store (the dtp_/jvp_ family). content_hash is UNIQUE so the same content can never be
--    smuggled in under a second id — concurrent writers converge on ONE row per triple.
--  * recorded_at is provenance, never identity: it does not enter the content hash.

CREATE TABLE IF NOT EXISTS execution_context_revision (
    revision_id    text        PRIMARY KEY,
    environment_id text        NOT NULL,
    execution_tier text        NOT NULL,
    purpose        text        NOT NULL,
    content_hash   text        NOT NULL,
    recorded_at    timestamptz NOT NULL DEFAULT now(),

    -- The honest environment binding (see header): the platform-wide non-blank convention.
    CONSTRAINT execution_context_revision_environment_chk CHECK (btrim(environment_id) <> ''),
    -- ExecutionTier's persisted value spellings, closed.
    CONSTRAINT execution_context_revision_tier_chk CHECK (
        execution_tier IN ('sandbox', 'production')),
    -- The step-3/4 purpose vocabulary, closed.
    CONSTRAINT execution_context_revision_purpose_chk CHECK (
        purpose IN ('feature_generation')),
    CONSTRAINT execution_context_revision_content_hash_key UNIQUE (content_hash)
);

-- ── append-only guards (one raiser; it names the table and the reason) ─────────────────────────
-- TG_OP is assigned in statement-level TRUNCATE triggers; OLD/NEW are NOT, which is why the
-- raiser touches neither.
CREATE OR REPLACE FUNCTION execution_context_revision_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'execution_context_revision is append-only: % is not allowed. A context revision is an '
        'identity that physical plans and adoptions pin by id — rewriting it would re-aim them '
        'all. Ensure a NEW revision instead.', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER execution_context_revision_no_mutation
    BEFORE UPDATE OR DELETE ON execution_context_revision
    FOR EACH ROW EXECUTE FUNCTION execution_context_revision_append_only();
-- A FOR EACH ROW trigger does not fire on TRUNCATE; this is the only guard that does.
CREATE OR REPLACE TRIGGER execution_context_revision_no_truncate
    BEFORE TRUNCATE ON execution_context_revision
    FOR EACH STATEMENT EXECUTE FUNCTION execution_context_revision_append_only();

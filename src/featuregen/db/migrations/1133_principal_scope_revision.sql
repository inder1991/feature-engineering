-- src/featuregen/db/migrations/1133_principal_scope_revision.sql
--
-- Cross-catalog serving plan (2026-08-24, rev 13) task B0a — SERVER-DERIVED AUTHORITY.
--
-- THE HOLE THIS CLOSES. Both generation routes took the caller's word for its own read scope:
-- `GenerationIn.roles` (build_sets.py:166 → :363) and the untyped `execution_parameters["roles"]`
-- (code_generation_jobs.py:186 → code_generation_coordinator.py:357), and those strings became
-- `GenerationJobV2.roles` → gate2_v2 → `decide_read_scope`. A caller could claim read roles it was
-- never granted, and cross-catalog generation makes that reach ACROSS catalogs. Roles are now
-- resolved server-side from the authenticated principal (`api/deps.py:147 get_identity` →
-- `identity/local_session.py:105 resolve_session`), frozen HERE, and the queue carries this
-- binding's id instead of any claims.
--
-- RESERVATION. The §T0 mapping (assigned 2026-08-24 at live head 1121) gives 1133 to B0a:
-- "principal/data-scope revision store". 1122-1129 belong to the remediation plan; 1130-1132 are
-- this plan's A3/A4/A7. Migration files apply lexically and are checksummed by the ledger.
--
-- TWO TABLES, because they answer two different questions.
--   * `principal_scope_revision` — WHAT was resolved: one immutable, content-addressed snapshot of
--     a principal and its data scope. Content-addressed so the same principal+scope always answers
--     the same id (one row for a hundred requests), and so a scope can never be edited under a
--     pinned id.
--   * `principal_scope_binding` — WHICH act that snapshot authorized: one row per
--     (subject_kind, subject_id), naming the revision and, where one exists, the action decision.
--     The queue payload carries `binding_id` and NOTHING ELSE about authority; the worker compares
--     the payload's claim against this row (the 1108 rule: THE ROW IS AUTHORITATIVE, the payload is
--     a claim), then reads the frozen claims through it.
--
-- WHY role_claims IS THE SCOPE, and groups are not stored. `resolve_session` derives roles from
-- group memberships; the roles are what `decide_read_scope` reads. Storing groups too would put a
-- second, non-load-bearing fact inside a content hash, so that an unrelated group change forked the
-- identity of an unchanged scope.
--
-- WHY EVERY STORED COLUMN IS HASHED. The table is content-addressed with a UNIQUE content_hash: a
-- column outside the hash would let two different values share one id, and the first writer's
-- value would silently answer for the second's.
--
-- THE TWO ENVELOPE FIELDS THIS TABLE DOES NOT CARRY, on the record. `IdentityEnvelope` also has
-- `source_of_authority` and `attestation`. Both are unset on every principal this platform mints
-- today (`build_human_identity` sets neither, and `resolve_session` is the only authenticated
-- producer), so storing them would hash a constant `null` — and the `groups` reasoning above does
-- NOT cover them: they are not derivation inputs, they are claims about WHO VOUCHED for the
-- principal, which is exactly the kind of fact a scope revision should carry once anything sets
-- one. When OIDC or service principals land and either becomes non-null, adding it is a NEW
-- migration AND a re-identification: every existing revision's content hash changes, so the
-- existing rows keep old ids that no longer describe the payload, and every binding pinning one
-- keeps pointing at the older content. Plan that as an expand/adopt, never an in-place widening.
--
-- FOREIGN KEYS — DECIDED, NOT DEFAULTED (A4's platform discovery). Postgres refuses to TRUNCATE a
-- table referenced by a FK BEFORE any BEFORE TRUNCATE trigger fires, so an FK onto an append-only
-- table replaces its append-only refusal with a foreign-key one — the guard stops proving itself.
-- Both tables here are append-only identities, so:
--   * `principal_scope_binding.revision_id` carries NO FK onto `principal_scope_revision`; the
--     store loads and content-verifies the revision before it writes a binding, and the revision
--     table is append-only so a verified row never disappears.
--   * `subject_id` is polymorphic over `subject_kind` (a code-generation job or a generation
--     request), which no FK can express at all; the CHECK closes the kind vocabulary instead.
--   * `action_decision_revision_id` names a row in the append-only `action_decision_revision`
--     (1106) and carries no FK for the same reason, kept consistent rather than mixed.

CREATE TABLE IF NOT EXISTS principal_scope_revision (
    revision_id     text        PRIMARY KEY,
    subject         text        NOT NULL,
    actor_kind      text        NOT NULL,
    authenticated   boolean     NOT NULL,
    auth_method     text        NOT NULL,
    -- The DATA SCOPE: the server-resolved role claims, sorted and deduplicated by the store. A
    -- JSON array so the ORDER is the canonical one the hash was taken over.
    role_claims     jsonb       NOT NULL,
    tenant          text,
    on_behalf_of    text,
    impersonation   text,
    break_glass     boolean     NOT NULL,
    content_hash    text        NOT NULL,
    recorded_at     timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT principal_scope_revision_subject_chk CHECK (btrim(subject) <> ''),
    CONSTRAINT principal_scope_revision_actor_kind_chk CHECK (btrim(actor_kind) <> ''),
    CONSTRAINT principal_scope_revision_auth_method_chk CHECK (btrim(auth_method) <> ''),
    -- An ARRAY, never an object or a scalar: the scope is an ordered list of claims.
    CONSTRAINT principal_scope_revision_claims_chk CHECK (jsonb_typeof(role_claims) = 'array'),
    CONSTRAINT principal_scope_revision_content_hash_key UNIQUE (content_hash)
);

CREATE TABLE IF NOT EXISTS principal_scope_binding (
    binding_id                 text        PRIMARY KEY,
    revision_id                text        NOT NULL,
    subject_kind               text        NOT NULL,
    subject_id                 text        NOT NULL,
    action_decision_revision_id text,
    content_hash               text        NOT NULL,
    bound_at                   timestamptz NOT NULL DEFAULT now(),

    -- The closed subject vocabulary. Widening it is a NEW migration, which is the review gate we
    -- want: a new subject kind is a new act that runs under somebody's frozen scope.
    CONSTRAINT principal_scope_binding_subject_kind_chk CHECK (
        subject_kind IN ('code_generation_job', 'generation_request')),
    CONSTRAINT principal_scope_binding_subject_id_chk CHECK (btrim(subject_id) <> ''),
    CONSTRAINT principal_scope_binding_revision_chk CHECK (btrim(revision_id) <> ''),
    CONSTRAINT principal_scope_binding_content_hash_key UNIQUE (content_hash),
    -- ONE AUTHORITY PER ACT. A second, different binding for the same subject would make "under
    -- whose scope did this run" a question with two answers, which is exactly the ambiguity a
    -- forged payload would exploit.
    CONSTRAINT principal_scope_binding_subject_key UNIQUE (subject_kind, subject_id)
);

CREATE INDEX IF NOT EXISTS principal_scope_binding_revision_idx
    ON principal_scope_binding (revision_id);

-- ── append-only guards (the 1034/1060/1062/1120/1130 idiom: one raiser per table) ──────────────
-- TG_OP is assigned in statement-level TRUNCATE triggers; OLD/NEW are NOT, which is why the
-- raisers touch neither.
CREATE OR REPLACE FUNCTION principal_scope_revision_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'principal_scope_revision is append-only: % is not allowed. A scope revision is the frozen '
        'proof of what a principal was authorized to read; editing one would silently re-authorize '
        'every act that pinned it. Resolve the principal again and record a NEW revision.', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER principal_scope_revision_no_mutation
    BEFORE UPDATE OR DELETE ON principal_scope_revision
    FOR EACH ROW EXECUTE FUNCTION principal_scope_revision_append_only();
-- A FOR EACH ROW trigger does not fire on TRUNCATE; this is the only guard that does.
CREATE OR REPLACE TRIGGER principal_scope_revision_no_truncate
    BEFORE TRUNCATE ON principal_scope_revision
    FOR EACH STATEMENT EXECUTE FUNCTION principal_scope_revision_append_only();

CREATE OR REPLACE FUNCTION principal_scope_binding_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'principal_scope_binding is append-only: % is not allowed. A binding is what a queued act '
        'names as its authority; rewriting one would re-aim a running job at a scope nobody '
        'granted it.', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER principal_scope_binding_no_mutation
    BEFORE UPDATE OR DELETE ON principal_scope_binding
    FOR EACH ROW EXECUTE FUNCTION principal_scope_binding_append_only();
CREATE OR REPLACE TRIGGER principal_scope_binding_no_truncate
    BEFORE TRUNCATE ON principal_scope_binding
    FOR EACH STATEMENT EXECUTE FUNCTION principal_scope_binding_append_only();

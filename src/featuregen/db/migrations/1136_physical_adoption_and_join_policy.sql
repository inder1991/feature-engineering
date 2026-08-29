-- src/featuregen/db/migrations/1136_physical_adoption_and_join_policy.sql
--
-- Cross-catalog serving plan (2026-08-24, rev 13) task B2: R3's PHYSICAL-PLAN ADOPTION chain and
-- the JoinValidationPolicyRevisionV1 store.
--
--   selection_physical_plan_adoption_revision  R3: append-only, user-confirmed, ENVIRONMENT-SCOPED
--   join_validation_policy_revision            the guard policy a physical plan pins by id
--
-- RESERVATION. 1130-1139 were assigned to this plan by T0 at live head 1121 (1122-1129 belong to
-- the remediation program's registry; 1117 stays reserved-unused). The §T0 mapping row for 1136 is
-- "physical-adoption store (R3 append-only root + CAS head per (selection_revision_id,
-- execution_context_revision_id)) + JoinValidationPolicyRevisionV1 store" (tasks B2/B2b). Migration
-- files apply lexically and are checksummed by the ledger — immutable once merged or applied
-- anywhere, INCLUDING the documented persistent-FEATUREGEN_TEST_DSN mode. 1134 (the physical-plan
-- revisions adopted here) and 1135 (the binding chain whose combined binding names an adoption)
-- both precede this file lexically.
--
-- ── R3, PROPERTY BY PROPERTY ────────────────────────────────────────────────────────────────
-- "physical adoption: append-only, user-confirmed, environment-scoped — root + CAS head per
--  (selection_revision_id, execution_context_revision_id); POST names physical_plan_revision_id;
--  partial-unique root; one successor per predecessor; semantic-only content_hash; build members'
--  adoptions match the build environment."
--
--   APPEND-ONLY          a row-level BEFORE UPDATE OR DELETE raiser and a statement-level BEFORE
--                        TRUNCATE raiser (the 1034/1060/1120/1130/1134 idiom). An adoption is what
--                        a build pins; editing one would re-aim a generated artifact's realization
--                        after the person confirmed it.
--   USER-CONFIRMED       `confirmed_by` / `confirmed_at` are NOT NULL. An adoption exists BECAUSE
--                        someone confirmed it — there is no proposed state here; candidates live in
--                        1037's provisional realization revisions (A4c) and become adopted by
--                        acquiring a row in this table.
--   ENVIRONMENT-SCOPED   the chain key is (selection_revision_id, execution_context_revision_id),
--                        and 1130's context revision IS (environment_id, tier, purpose). A sandbox
--                        adoption and a production adoption of one selection are two independent
--                        chains that never compete: two roots, two heads, both current.
--   PARTIAL-UNIQUE ROOT  UNIQUE (selection, context) WHERE supersedes IS NULL. Exactly one root per
--                        scope, so "where does this chain start" has one answer and two racing
--                        first-ever confirmations cannot both win.
--   ONE SUCCESSOR        UNIQUE (supersedes) WHERE supersedes IS NOT NULL. A predecessor may be
--                        superseded at most once; two successors would fork the chain and "which
--                        adoption is current" would depend on which fork a reader walked.
--   THE CAS             is those two partial-unique indexes, and nothing else. The head is found by
--                        ABSENCE of a successor, never by newest timestamp — two adoptions recorded
--                        in one transaction share a clock, and "newest by time" would be a coin
--                        flip. A confirmation that names a stale head loses to the unique index
--                        rather than to a read-then-write check that another writer could slip past.
--   SEMANTIC-ONLY HASH   content_hash covers (selection, context, physical plan, supersedes) and
--                        NOT confirmed_by/confirmed_at/recorded_at. Re-confirming the same physical
--                        plan on the same head is the SAME revision and ONE row, whoever asked and
--                        whenever; the actor and the clock are provenance and the first writer's
--                        are kept (the insert is ON CONFLICT DO NOTHING).
--   BUILD-ENVIRONMENT    the structural half is 1135's deferred trigger (all members of one build
--                        set share one execution context) plus this file's deferred trigger below
--                        (a combined binding's adoption must exist, be for THAT selection, name
--                        THAT physical plan, and sit in THAT context). The match against a
--                        generation REQUEST's `environment_id` is the store's check, because the
--                        environment is a column on `generation_request` and there is no build-set
--                        column to constrain against.
--
-- ── WHAT THE ADOPTION DOES NOT CARRY, AND WHY ───────────────────────────────────────────────
-- Neither the logical digest nor the join-validation policy has a column here. Both are already
-- pinned INSIDE the physical plan this adoption names (`physical_execution_plan_revision.
-- logical_digest`, and `join_validation_policy_revision_id` in the same row), and 1134 indexes the
-- first for exactly this kind of relational question. A second writable copy of either could
-- disagree with the plan it claims to describe — the precise defect the binding chain exists to
-- make unrepresentable — so the store reads them THROUGH the pinned plan and verifies the
-- agreement, rather than storing them twice and hoping.
--
-- ── FOREIGN KEYS, DECIDED ───────────────────────────────────────────────────────────────────
--   REAL FK: selection_revision_id -> feature_selection_revision (1072). That table carries a
--            row-level append-only trigger but NO truncate raiser (1092 and 1101 already reference
--            it), so nothing is disarmed and the database checks the leg for free.
--   STORE CHECK: physical_plan_revision_id -> 1134 (truncate raiser); execution_context_revision_id
--            -> 1130 (truncate raiser); supersedes -> THIS table. The last is a self-reference and
--            is deliberately NOT an FK either: this table has its own truncate raiser, and the
--            house rule after A4's discovery is that a raiser which cannot fire is not a guard.
--            The two partial-unique indexes carry the chain's integrity, and the store verifies the
--            predecessor with a VERIFYING LOAD — which proves more than an FK could, because it
--            proves the predecessor can still reproduce its own identity.
--
-- ── THE JOIN-VALIDATION POLICY STORE ────────────────────────────────────────────────────────
-- `physical_execution_plan_revision.join_validation_policy_revision_id` (1134) has been an honest
-- UNCHECKED pin since B1 shipped: "its store is 1136's and does not exist yet". This is that store,
-- and B1's write path now verifies the pin. The policy's `content_hash` is SEMANTIC-ONLY by its own
-- contract (`declared_by`/`declared_at` are excluded), so re-declaring one policy under another
-- name is the SAME revision — the declarer columns here keep the FIRST declaration's provenance,
-- which is what "the same policy declared twice is one policy" means.

-- ── the guard policy ────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS join_validation_policy_revision (
    revision_id  text        PRIMARY KEY,
    -- JoinValidationPolicyRevisionV1.content_payload() — the closed enums, the coverage bounds, the
    -- snapshot-SELECTION rule and the fan-out declaration. SEMANTIC only.
    content      jsonb       NOT NULL,
    content_hash text        NOT NULL,
    -- PROVENANCE, outside the hash: who declared this policy and when they said they did.
    declared_by  text        NOT NULL,
    declared_at  text        NOT NULL,
    recorded_at  timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT join_validation_policy_revision_hash_chk CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT join_validation_policy_revision_id_chk CHECK (
        revision_id = 'jvp_' || content_hash),
    CONSTRAINT join_validation_policy_revision_content_chk CHECK (
        jsonb_typeof(content) = 'object'),
    CONSTRAINT join_validation_policy_revision_declared_by_chk CHECK (btrim(declared_by) <> ''),
    CONSTRAINT join_validation_policy_revision_declared_at_chk CHECK (btrim(declared_at) <> ''),
    CONSTRAINT join_validation_policy_revision_content_hash_key UNIQUE (content_hash)
);

-- ── the adoption chain ──────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS selection_physical_plan_adoption_revision (
    adoption_revision_id            text        PRIMARY KEY,

    -- R3's chain key: WHICH choice, in WHICH environment.
    selection_revision_id           text        NOT NULL
                                        REFERENCES feature_selection_revision(revision_id),
    execution_context_revision_id   text        NOT NULL,

    -- What the POST names (R3). 1134's physical plan, adopted exactly as it is.
    physical_plan_revision_id       text        NOT NULL,

    -- NULL for the root of this scope's chain; the exact predecessor otherwise.
    supersedes_adoption_revision_id text,

    -- R3's "user-confirmed". PROVENANCE — outside the content hash.
    confirmed_by                    text        NOT NULL,
    confirmed_at                    text        NOT NULL,

    content_hash                    text        NOT NULL,
    recorded_at                     timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT selection_physical_plan_adoption_hash_chk CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT selection_physical_plan_adoption_id_chk CHECK (
        adoption_revision_id = 'spa_' || content_hash),
    CONSTRAINT selection_physical_plan_adoption_plan_chk CHECK (
        physical_plan_revision_id ~ '^pxp_[0-9a-f]{64}$'),
    CONSTRAINT selection_physical_plan_adoption_context_chk CHECK (
        btrim(execution_context_revision_id) <> ''),
    CONSTRAINT selection_physical_plan_adoption_supersedes_chk CHECK (
        supersedes_adoption_revision_id IS NULL
        OR supersedes_adoption_revision_id ~ '^spa_[0-9a-f]{64}$'),
    -- A revision superseding ITSELF would be a chain with no start and no end.
    CONSTRAINT selection_physical_plan_adoption_not_self_chk CHECK (
        supersedes_adoption_revision_id IS DISTINCT FROM adoption_revision_id),
    CONSTRAINT selection_physical_plan_adoption_confirmed_by_chk CHECK (btrim(confirmed_by) <> ''),
    CONSTRAINT selection_physical_plan_adoption_confirmed_at_chk CHECK (btrim(confirmed_at) <> ''),
    CONSTRAINT selection_physical_plan_adoption_content_hash_key UNIQUE (content_hash)
);

-- R3's PARTIAL-UNIQUE ROOT: exactly one chain start per (selection, execution context).
CREATE UNIQUE INDEX IF NOT EXISTS selection_physical_plan_adoption_root_key
    ON selection_physical_plan_adoption_revision
       (selection_revision_id, execution_context_revision_id)
    WHERE supersedes_adoption_revision_id IS NULL;

-- R3's ONE SUCCESSOR PER PREDECESSOR: the fork-free chain, and the CAS's second half.
CREATE UNIQUE INDEX IF NOT EXISTS selection_physical_plan_adoption_successor_key
    ON selection_physical_plan_adoption_revision (supersedes_adoption_revision_id)
    WHERE supersedes_adoption_revision_id IS NOT NULL;

-- The head lookup: every adoption in one scope, so "the one nothing supersedes" is an index scan.
CREATE INDEX IF NOT EXISTS selection_physical_plan_adoption_scope_idx
    ON selection_physical_plan_adoption_revision
       (selection_revision_id, execution_context_revision_id);

CREATE INDEX IF NOT EXISTS selection_physical_plan_adoption_plan_idx
    ON selection_physical_plan_adoption_revision (physical_plan_revision_id);

-- ── append-only guards ──────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION physical_adoption_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        '% is append-only: % is not allowed. A policy revision and an adoption are identities that '
        'physical plans and build members pin BY ID — rewriting one would silently re-aim what a '
        'person confirmed. Record a NEW revision instead.', TG_TABLE_NAME, TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER join_validation_policy_revision_no_mutation
    BEFORE UPDATE OR DELETE ON join_validation_policy_revision
    FOR EACH ROW EXECUTE FUNCTION physical_adoption_append_only();
-- A FOR EACH ROW trigger does not fire on TRUNCATE; the statement-level guard is the only one that
-- does, and no FK points at either table, so nothing can make Postgres refuse first.
CREATE OR REPLACE TRIGGER join_validation_policy_revision_no_truncate
    BEFORE TRUNCATE ON join_validation_policy_revision
    FOR EACH STATEMENT EXECUTE FUNCTION physical_adoption_append_only();

CREATE OR REPLACE TRIGGER selection_physical_plan_adoption_no_mutation
    BEFORE UPDATE OR DELETE ON selection_physical_plan_adoption_revision
    FOR EACH ROW EXECUTE FUNCTION physical_adoption_append_only();
CREATE OR REPLACE TRIGGER selection_physical_plan_adoption_no_truncate
    BEFORE TRUNCATE ON selection_physical_plan_adoption_revision
    FOR EACH STATEMENT EXECUTE FUNCTION physical_adoption_append_only();

-- ── the adoption leg of 1135's combined binding, now that its table exists ──────────────────
-- 1135 could only pin the adoption id's FORMAT (its table did not exist yet, and an FK onto an
-- append-only table would disarm the raiser above in any case). This is the rest of the law, and it
-- is the "previewable selection without a confirmed adoption" refusal: a combined binding IS the
-- declaration that a selection may be previewed, and it may not exist unless the user's confirmed
-- adoption exists, belongs to THAT selection, names THAT physical plan, and sits in THAT execution
-- context. Deferred so the adoption and the binding may be written in either order in one
-- transaction.
CREATE OR REPLACE FUNCTION build_member_combined_binding_adoption_agrees() RETURNS trigger AS $$
DECLARE
    adopted selection_physical_plan_adoption_revision%ROWTYPE;
BEGIN
    SELECT * INTO adopted FROM selection_physical_plan_adoption_revision
    WHERE adoption_revision_id = NEW.physical_adoption_revision_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'combined binding % names adoption %, which does not exist at COMMIT: a selection is '
            'previewable because a person CONFIRMED a physical plan for it, and a binding without '
            'that confirmation would generate a realization nobody adopted',
            NEW.combined_binding_id, NEW.physical_adoption_revision_id;
    END IF;
    IF adopted.selection_revision_id <> NEW.selection_revision_id THEN
        RAISE EXCEPTION
            'combined binding % is for selection % but adoption % was confirmed for selection %: '
            'an adoption belongs to the choice it was confirmed against, never to a look-alike',
            NEW.combined_binding_id, NEW.selection_revision_id,
            NEW.physical_adoption_revision_id, adopted.selection_revision_id;
    END IF;
    IF adopted.physical_plan_revision_id <> NEW.physical_plan_revision_id THEN
        RAISE EXCEPTION
            'combined binding % names physical plan % but adoption % confirmed %: the plan that '
            'generates is the plan that was confirmed',
            NEW.combined_binding_id, NEW.physical_plan_revision_id,
            NEW.physical_adoption_revision_id, adopted.physical_plan_revision_id;
    END IF;
    IF adopted.execution_context_revision_id <> NEW.execution_context_revision_id THEN
        RAISE EXCEPTION
            'combined binding % declares execution context % but adoption % was confirmed in %: '
            'R3 scopes an adoption to ONE environment, and a build may not borrow another one''s',
            NEW.combined_binding_id, NEW.execution_context_revision_id,
            NEW.physical_adoption_revision_id, adopted.execution_context_revision_id;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS build_member_combined_binding_adoption_total
    ON build_member_combined_binding;
CREATE CONSTRAINT TRIGGER build_member_combined_binding_adoption_total
    AFTER INSERT ON build_member_combined_binding
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION build_member_combined_binding_adoption_agrees();

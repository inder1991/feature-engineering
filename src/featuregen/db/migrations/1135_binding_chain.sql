-- src/featuregen/db/migrations/1135_binding_chain.sql
--
-- Cross-catalog serving plan (2026-08-24, rev 13) task B2: THE TOTAL BINDING CHAIN — the DDL that
-- makes "the join the user confirmed is the join that generates" structurally true rather than
-- conventional. Four binding tables carry one logical identity from the option a person was shown,
-- through the draft authored for it, through the selection they made, into the build member that
-- generates:
--
--   considered_option_plan_binding   (considered_revision_id, option_id) -> logical plan (1134)
--   formula_draft_plan_binding       formula_draft_id -> the SAME logical plan as its option
--   selection_formula_plan_binding   (selection, draft) -> the SAME logical plan as its draft
--   build_member_combined_binding    logical + physical + render, named by build_set_member
--
-- RESERVATION. 1130-1139 were assigned to this plan by T0 at live head 1121 (1122-1129 belong to
-- the remediation program's registry; 1117 stays reserved-unused). The §T0 mapping row for 1135 is
-- "binding-chain relational agreement (selection<->formula<->plan FKs; legacy hashes preserved as
-- provenance pins)" (task B2). Migration files apply lexically and are checksummed by the ledger —
-- immutable once merged or applied anywhere, INCLUDING the documented persistent-FEATUREGEN_TEST_DSN
-- mode. 1134 (the identity layers) precedes this file lexically, so the logical, physical and
-- render revisions these bindings name already have their store; 1136 (the adoption + join-policy
-- stores) FOLLOWS it, which is why the adoption leg here is a format CHECK plus a store check and
-- 1136 adds the deferred trigger that closes it.
--
-- ── WHICH LEGS ARE FOREIGN KEYS, AND WHY THE OTHERS ARE NOT ──────────────────────────────────
-- A4's platform discovery, now applied five times: Postgres refuses to TRUNCATE a table that is
-- referenced by a foreign key BEFORE that table's own BEFORE TRUNCATE raiser fires, so an FK onto
-- an append-only table silently REPLACES its append-only refusal with a foreign-key one and the
-- guard stops proving itself. The rule this file follows:
--
--   * a leg between two tables where the REFERENCED table has NO truncate raiser is a real FK —
--     it is free, it is checked by the database, and nothing is weakened. Those are:
--       formula_draft_plan_binding      -> formula_draft                    (1090, no raiser)
--       formula_draft_plan_binding      -> considered_option_plan_binding   (this file, no raiser)
--       selection_formula_plan_binding  -> selection_formula_binding        (1101, no raiser)
--       selection_formula_plan_binding  -> formula_draft_plan_binding       (this file)
--       build_member_combined_binding   -> selection_formula_plan_binding   (this file)
--       build_set_member                -> build_member_combined_binding    (this file)
--
--   * a leg pointing at a table that DOES carry a truncate raiser is a VERIFYING LOAD in the
--     store, never an FK. Those are:
--       considered_option_plan_binding  -> semantic_option_decision  (1063 HAS a truncate raiser)
--       every leg onto 1134's identity layers (logical plan, physical plan, render profile)
--       build_member_combined_binding   -> 1136's adoption revision (append-only, and later)
--
-- B1's doctrine, adopted verbatim: "an FK proves a row exists; a verifying load proves it can still
-- reproduce its identity." Every store-check leg here routes through a 1134 loader on a DERIVABLE
-- primary key ('lfp_' || digest, 'pxp_' || digest, 'rpf_' || digest), so a binding can never pin a
-- row that has drifted from the identity it publishes — something no foreign key could ever check.
--
-- ── TOTALITY: A PARENT CANNOT EXIST WITHOUT ITS REQUIRED CHILD ───────────────────────────────
-- An ordinary FK proves the CHILD references a parent. The plan's law is the other direction: a
-- parent row must not exist without its binding. Two mechanisms, chosen per table by whether the
-- insert order permits the parent to name the child:
--
--   * PARENT-CARRIED BINDING ID + COMPOSITE FK, where insert order allows it. `build_set_member`
--     gets `combined_binding_id`, and the composite FK (combined_binding_id, selection_revision_id)
--     forces the member and its binding to name ONE selection — 1101's proven shape, one level up.
--
--   * DEFERRED CONSTRAINT TRIGGER checked at COMMIT, where circularity forbids the first. The
--     option/draft/member bindings all name their parent, so the parent cannot also carry a NOT
--     NULL reference to them: the two rows would each require the other to exist first. A
--     DEFERRABLE INITIALLY DEFERRED constraint trigger states the same law without the circle —
--     insert in either order, and at COMMIT the parent must have its binding.
--
-- ▲ TOTALITY IS SCOPED, NOT RETROFITTED. Every one of these tables already holds legacy rows that
-- were created before a logical plan existed, and the plan is explicit that those rows are PRE-PLAN
-- and are refused for cross-catalog generation rather than back-filled with a plan nobody chose. So
-- the law fires only for rows that have ENTERED the planned lane:
--   * an option declares itself with `semantic_option_decision.requires_logical_plan_binding`
--     (added here, DEFAULT false — so every existing and every ordinary row is untouched and
--     honestly pre-plan);
--   * a DRAFT inherits the requirement from its option — no second marker, because a draft for a
--     planned option is planned by construction and a marker could disagree with its option;
--   * a BUILD MEMBER inherits it from its selection: once a selection has a
--     `selection_formula_plan_binding`, no build may ever name that selection without a combined
--     binding. A marker-free rule, derived from the chain rather than declared beside it.
--
-- ▲ THE LEGACY HASHES RIDE AS PROVENANCE (round 10's ruling: relational agreement, never hash
-- equality). `planning_request_hash` and `binding_plan_hash` are stored beside the bindings and are
-- NEVER compared for equality against a logical digest — they hash different payloads, so comparing
-- them would be meaningless. What binds is RELATIONAL: the composite foreign keys above make it
-- impossible for a draft, a selection and a build member to name different plans, and 1101's own
-- composite keys already make it impossible for the selection and the draft to describe different
-- work. The hashes stay so an operator can still trace the request a binding came from.
--
-- ▲ NO `formula_draft_authoring_plan` REUSE. 1104's table of that name records WHICH METHOD
-- authored a draft (recipe blueprint vs LLM) — a different job entirely. This file's draft-side
-- table is `formula_draft_plan_binding`: WHICH LOGICAL PLAN the draft was authored against.

-- ── 1. the option's logical plan ─────────────────────────────────────────────────────────────
-- WHICH FEATURE MEANING the served option is a plan for. The root of the chain: everything below
-- inherits this logical digest through a foreign key, so no descendant can quietly change it.
CREATE TABLE IF NOT EXISTS considered_option_plan_binding (
    considered_revision_id   text        NOT NULL,
    option_id                text        NOT NULL,

    -- 1134's logical plan, named BOTH ways: by revision id (what a loader addresses) and by digest
    -- (what the descendants' composite keys carry). The CHECK below pins them to one value, so the
    -- two spellings can never disagree.
    logical_plan_revision_id text        NOT NULL,
    logical_digest           text        NOT NULL,

    -- PROVENANCE PIN (round 10): the request this option was planned under. Traceable, never
    -- compared against a digest.
    planning_request_hash    text        NOT NULL,

    recorded_at              timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (considered_revision_id, option_id),
    CONSTRAINT considered_option_plan_binding_considered_chk CHECK (
        btrim(considered_revision_id) <> ''),
    CONSTRAINT considered_option_plan_binding_option_chk CHECK (btrim(option_id) <> ''),
    CONSTRAINT considered_option_plan_binding_digest_chk CHECK (
        logical_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT considered_option_plan_binding_plan_chk CHECK (
        logical_plan_revision_id = 'lfp_' || logical_digest),
    CONSTRAINT considered_option_plan_binding_request_chk CHECK (
        btrim(planning_request_hash) <> '')
);

-- The key the draft binding's composite FK points at. A superset of the primary key, so it is
-- satisfied by construction and costs only the index.
CREATE UNIQUE INDEX IF NOT EXISTS considered_option_plan_binding_plan_key
    ON considered_option_plan_binding (considered_revision_id, option_id, logical_digest);

CREATE INDEX IF NOT EXISTS considered_option_plan_binding_by_digest
    ON considered_option_plan_binding (logical_digest);

-- ── 2. the draft's logical plan ──────────────────────────────────────────────────────────────
-- The key `formula_draft` needs so a binding can reference the draft AND its candidate together.
-- A superset of the primary key: it cannot fail on existing data.
CREATE UNIQUE INDEX IF NOT EXISTS formula_draft_candidate_key
    ON formula_draft (formula_draft_id, considered_revision_id, option_id);

CREATE TABLE IF NOT EXISTS formula_draft_plan_binding (
    formula_draft_id         text        PRIMARY KEY,

    -- Carried, not derived, because both composite foreign keys below need them in this row: one
    -- column, two parents, no way to satisfy them with different values.
    considered_revision_id   text        NOT NULL,
    option_id                text        NOT NULL,

    logical_plan_revision_id text        NOT NULL,
    logical_digest           text        NOT NULL,

    -- PROVENANCE PIN.
    planning_request_hash    text        NOT NULL,

    recorded_at              timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT formula_draft_plan_binding_digest_chk CHECK (logical_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT formula_draft_plan_binding_plan_chk CHECK (
        logical_plan_revision_id = 'lfp_' || logical_digest),
    CONSTRAINT formula_draft_plan_binding_request_chk CHECK (btrim(planning_request_hash) <> ''),

    -- The draft exists and was authored for THIS candidate.
    CONSTRAINT formula_draft_plan_binding_draft_agrees
        FOREIGN KEY (formula_draft_id, considered_revision_id, option_id)
        REFERENCES formula_draft (formula_draft_id, considered_revision_id, option_id),

    -- ▲ THE CROSS-PLAN CLOSURE. The draft's plan must BE the option's plan — not a plan that
    -- happens to exist, not a plan for a different option. Two independent columns could disagree;
    -- this composite key makes the disagreement unrepresentable.
    CONSTRAINT formula_draft_plan_binding_option_plan_agrees
        FOREIGN KEY (considered_revision_id, option_id, logical_digest)
        REFERENCES considered_option_plan_binding (considered_revision_id, option_id,
                                                   logical_digest)
);

-- The key the selection binding's composite FK points at.
CREATE UNIQUE INDEX IF NOT EXISTS formula_draft_plan_binding_plan_key
    ON formula_draft_plan_binding (formula_draft_id, logical_digest);

-- ── 3. the selection's logical plan ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS selection_formula_plan_binding (
    selection_revision_id    text        NOT NULL,
    formula_draft_id         text        NOT NULL,

    logical_plan_revision_id text        NOT NULL,
    logical_digest           text        NOT NULL,

    -- PROVENANCE PINS, both of them: 1072's `feature_selection_revision` columns, copied here so
    -- the request and the plan-variant a selection was made under stay traceable from the binding.
    -- Never equality-compared against `logical_digest` — different payloads, different meanings.
    planning_request_hash    text        NOT NULL,
    binding_plan_hash        text        NOT NULL,

    recorded_at              timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (selection_revision_id, formula_draft_id),
    CONSTRAINT selection_formula_plan_binding_digest_chk CHECK (
        logical_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT selection_formula_plan_binding_plan_chk CHECK (
        logical_plan_revision_id = 'lfp_' || logical_digest),
    CONSTRAINT selection_formula_plan_binding_request_chk CHECK (
        btrim(planning_request_hash) <> ''),
    CONSTRAINT selection_formula_plan_binding_variant_chk CHECK (
        btrim(binding_plan_hash) <> ''),

    -- ▲ 1101 ALREADY PROVED THE SELECTION AND THE DRAFT DESCRIBE THE SAME WORK (candidate, option,
    -- planning request, formula content). This leg REQUIRES that proof rather than repeating it:
    -- no plan binding without the pin it extends.
    CONSTRAINT selection_formula_plan_binding_pin_agrees
        FOREIGN KEY (selection_revision_id, formula_draft_id)
        REFERENCES selection_formula_binding (selection_revision_id, formula_draft_id),

    -- ▲ AND THE PLAN IS THE DRAFT'S PLAN. This is the "selection binding across plans" refusal:
    -- a selection cannot be bound to a logical plan its own formula was not authored against.
    CONSTRAINT selection_formula_plan_binding_draft_plan_agrees
        FOREIGN KEY (formula_draft_id, logical_digest)
        REFERENCES formula_draft_plan_binding (formula_draft_id, logical_digest)
);

-- The key the combined binding's composite FK points at.
CREATE UNIQUE INDEX IF NOT EXISTS selection_formula_plan_binding_plan_key
    ON selection_formula_plan_binding (selection_revision_id, formula_draft_id, logical_digest);

CREATE INDEX IF NOT EXISTS selection_formula_plan_binding_by_digest
    ON selection_formula_plan_binding (logical_digest);

-- ── 4. the build member's COMBINED binding: logical + physical + render ──────────────────────
-- What a build member actually needs to generate: the meaning (logical), the realization the user
-- adopted (physical, in one execution context), and the profile it renders under. Content-addressed
-- over exactly those three, so the same combination is one row however many members reach it.
--
-- ▲ THE PHYSICAL AND RENDER LEGS ARE STORE CHECKS. `physical_execution_plan_revision` and
-- `render_profile_revision` (1134) and `selection_physical_plan_adoption_revision` (1136) all carry
-- BEFORE TRUNCATE raisers; an FK onto any of them would disarm the guard. The store loads and
-- content-verifies each one before writing, and 1136 adds a DEFERRED CONSTRAINT TRIGGER that
-- re-states the adoption leg in the database once its table exists.
CREATE TABLE IF NOT EXISTS build_member_combined_binding (
    combined_binding_id           text        PRIMARY KEY,

    -- LOGICAL: through the selection's plan binding, which through its own keys reaches the draft
    -- and the option. One foreign key, the whole chain.
    selection_revision_id         text        NOT NULL,
    formula_draft_id              text        NOT NULL,
    logical_digest                text        NOT NULL,

    -- PHYSICAL: the adopted plan and the context it was adopted in (R3's environment scope).
    physical_plan_revision_id     text        NOT NULL,
    physical_digest               text        NOT NULL,
    physical_adoption_revision_id text        NOT NULL,
    execution_context_revision_id text        NOT NULL,

    -- RENDER.
    render_profile_revision_id    text        NOT NULL,
    render_profile_digest         text        NOT NULL,

    content_hash                  text        NOT NULL,
    recorded_at                   timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT build_member_combined_binding_hash_chk CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT build_member_combined_binding_id_chk CHECK (
        combined_binding_id = 'cmb_' || content_hash),
    CONSTRAINT build_member_combined_binding_logical_chk CHECK (
        logical_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT build_member_combined_binding_physical_chk CHECK (
        physical_digest ~ '^[0-9a-f]{64}$'
        AND physical_plan_revision_id = 'pxp_' || physical_digest),
    CONSTRAINT build_member_combined_binding_render_chk CHECK (
        render_profile_digest ~ '^[0-9a-f]{64}$'
        AND render_profile_revision_id = 'rpf_' || render_profile_digest),
    -- 1136 mints adoption ids as 'spa_' || sha256(semantic payload). The format is pinned here so a
    -- combined binding cannot name something that is not an adoption at all; 1136's deferred
    -- trigger proves the row exists and agrees.
    CONSTRAINT build_member_combined_binding_adoption_chk CHECK (
        physical_adoption_revision_id ~ '^spa_[0-9a-f]{64}$'),
    CONSTRAINT build_member_combined_binding_context_chk CHECK (
        btrim(execution_context_revision_id) <> ''),
    CONSTRAINT build_member_combined_binding_content_hash_key UNIQUE (content_hash),

    CONSTRAINT build_member_combined_binding_logical_agrees
        FOREIGN KEY (selection_revision_id, formula_draft_id, logical_digest)
        REFERENCES selection_formula_plan_binding (selection_revision_id, formula_draft_id,
                                                   logical_digest)
);

-- ▲ THE KEY `build_set_member` NEEDS (1101's shape, one level up). The member already carries
-- `selection_revision_id`; this lets it reference the binding AND the selection together, so the
-- two can never name different selections.
CREATE UNIQUE INDEX IF NOT EXISTS build_member_combined_binding_member_key
    ON build_member_combined_binding (combined_binding_id, selection_revision_id);

CREATE INDEX IF NOT EXISTS build_member_combined_binding_by_selection
    ON build_member_combined_binding (selection_revision_id, execution_context_revision_id);

-- APPEND-ONLY. A binding that can be edited is a pin that moves, which is the whole defect. One
-- raiser for the four tables: they are one chain, delivered together, refusing for one reason.
CREATE OR REPLACE FUNCTION binding_chain_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        '% is append-only: % is not allowed. A binding is what proves the plan a build generates '
        'is the plan a person confirmed; editing one would re-aim that proof after the fact. '
        'Record a NEW binding instead.', TG_TABLE_NAME, TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER considered_option_plan_binding_no_mutation
    BEFORE UPDATE OR DELETE ON considered_option_plan_binding
    FOR EACH ROW EXECUTE FUNCTION binding_chain_append_only();
CREATE OR REPLACE TRIGGER formula_draft_plan_binding_no_mutation
    BEFORE UPDATE OR DELETE ON formula_draft_plan_binding
    FOR EACH ROW EXECUTE FUNCTION binding_chain_append_only();
CREATE OR REPLACE TRIGGER selection_formula_plan_binding_no_mutation
    BEFORE UPDATE OR DELETE ON selection_formula_plan_binding
    FOR EACH ROW EXECUTE FUNCTION binding_chain_append_only();
CREATE OR REPLACE TRIGGER build_member_combined_binding_no_mutation
    BEFORE UPDATE OR DELETE ON build_member_combined_binding
    FOR EACH ROW EXECUTE FUNCTION binding_chain_append_only();

-- ▲ NO BEFORE TRUNCATE RAISERS ON THESE FOUR, AND THAT IS THE HONEST CHOICE. Every one of them is
-- FK-referenced by the next link in the chain (the fourth by `build_set_member` below), so Postgres
-- would refuse a TRUNCATE with FeatureNotSupported BEFORE any raiser could fire — the exact defect
-- A4 found, and a guard that can never prove itself is worse than an honest absence. The chain's
-- immutability is carried by the row-level guards above; the identity layers these bindings point
-- at (1134/1136) are the raiser-protected tables, and they have no FK pointed at them at all.

-- ── the member carries its binding (parent-carried id + composite FK) ────────────────────────
ALTER TABLE build_set_member
    ADD COLUMN IF NOT EXISTS combined_binding_id text;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'build_set_member_combined_pinned_v1') THEN
        -- ▲ NULLABLE, DELIBERATELY, and the totality trigger below is what makes it total where it
        -- must be. A member for a PRE-PLAN selection carries NULL and is honestly pre-plan (and is
        -- refused for cross-catalog generation by the store); a member for a selection that has
        -- entered the planned lane cannot commit without a binding. Under MATCH SIMPLE a NULL in a
        -- composite foreign key skips the check entirely, which is exactly the semantics wanted
        -- here — and is why the requirement is stated as a trigger rather than left to the FK.
        ALTER TABLE build_set_member
            ADD CONSTRAINT build_set_member_combined_pinned_v1
            FOREIGN KEY (combined_binding_id, selection_revision_id)
            REFERENCES build_member_combined_binding (combined_binding_id, selection_revision_id);
    END IF;
END $$;

-- ── TOTALITY, mechanism by mechanism ─────────────────────────────────────────────────────────
-- 1. AN OPTION THAT DECLARES ITSELF PLANNED MUST HAVE A LOGICAL PLAN BINDING.
-- The marker defaults false, so every existing row and every ordinary (single-catalog, pre-plan)
-- option is untouched. Adding a defaulted column is DDL and does not fire 1063's row-level
-- append-only trigger.
ALTER TABLE semantic_option_decision
    ADD COLUMN IF NOT EXISTS requires_logical_plan_binding boolean NOT NULL DEFAULT false;

CREATE OR REPLACE FUNCTION considered_option_plan_binding_is_total() RETURNS trigger AS $$
BEGIN
    IF NEW.requires_logical_plan_binding AND NOT EXISTS (
            SELECT 1 FROM considered_option_plan_binding b
            WHERE b.considered_revision_id = NEW.considered_revision_id
              AND b.option_id = NEW.option_id) THEN
        RAISE EXCEPTION
            'option %/% declares that it requires a logical plan binding and has none at COMMIT: '
            'a served cross-catalog option IS a plan, and an option without one would let a person '
            'choose a feature whose meaning was never recorded',
            NEW.considered_revision_id, NEW.option_id;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

-- ▲ THE `WHEN` IS LOAD-BEARING, NOT AN OPTIMISATION. A deferred constraint trigger QUEUES a pending
-- event for every row it fires on, and Postgres refuses ALTER TABLE / TRUNCATE on a table with
-- pending events (`ObjectInUse`). Without this guard EVERY ordinary option insert would leave one,
-- so a transaction that inserted an option and then altered the table — exactly what re-applying an
-- older migration against a populated table does — would fail for a reason having nothing to do
-- with plans. With it, only rows in the PLANNED lane queue anything at all. The function re-checks
-- the same condition so the law survives the `WHEN` being dropped by a later editor.
DROP TRIGGER IF EXISTS semantic_option_decision_plan_binding_total ON semantic_option_decision;
CREATE CONSTRAINT TRIGGER semantic_option_decision_plan_binding_total
    AFTER INSERT ON semantic_option_decision
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    WHEN (NEW.requires_logical_plan_binding)
    EXECUTE FUNCTION considered_option_plan_binding_is_total();

-- 2. A DRAFT FOR A PLANNED OPTION MUST HAVE A PLAN BINDING.
-- The requirement is INHERITED from the option rather than declared again on the draft: a second
-- marker could disagree with the first, and "planned" is a property of the candidate, not of the
-- attempt to author it.
CREATE OR REPLACE FUNCTION formula_draft_plan_binding_is_total() RETURNS trigger AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM semantic_option_decision d
               WHERE d.considered_revision_id = NEW.considered_revision_id
                 AND d.option_id = NEW.option_id
                 AND d.requires_logical_plan_binding)
       AND NOT EXISTS (SELECT 1 FROM formula_draft_plan_binding b
                       WHERE b.formula_draft_id = NEW.formula_draft_id) THEN
        RAISE EXCEPTION
            'formula draft % is for planned option %/% and has no plan binding at COMMIT: a '
            'cross-catalog formula is authored AGAINST a logical plan, and a draft without one '
            'would be a formula for a meaning nobody pinned',
            NEW.formula_draft_id, NEW.considered_revision_id, NEW.option_id;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS formula_draft_plan_binding_total ON formula_draft;
CREATE CONSTRAINT TRIGGER formula_draft_plan_binding_total
    AFTER INSERT ON formula_draft
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION formula_draft_plan_binding_is_total();

-- 3. A BUILD MEMBER FOR A PLANNED SELECTION MUST NAME A COMBINED BINDING, and every member of one
--    build set must have been adopted in ONE execution context (the structural half of R3's
--    "build members' adoptions match the build environment"; the environment itself lives on
--    `generation_request`, so the match against a REQUEST is the store's check, not this one).
-- Marker-free: the selection's own plan binding is the declaration. Deferred, so the member and its
-- binding may be written in either order inside one transaction.
CREATE OR REPLACE FUNCTION build_member_combined_binding_is_total() RETURNS trigger AS $$
DECLARE
    contexts integer;
BEGIN
    IF NEW.combined_binding_id IS NULL THEN
        IF EXISTS (SELECT 1 FROM selection_formula_plan_binding b
                   WHERE b.selection_revision_id = NEW.selection_revision_id) THEN
            RAISE EXCEPTION
                'build set % position % names planned selection % with no combined binding at '
                'COMMIT: a member of a planned selection must pin the logical, physical and render '
                'identities it generates from, or the build would resolve them at build time — the '
                'drift this whole chain exists to prevent',
                NEW.revision_id, NEW.position, NEW.selection_revision_id;
        END IF;
        RETURN NULL;
    END IF;

    SELECT count(DISTINCT c.execution_context_revision_id) INTO contexts
    FROM build_set_member m
    JOIN build_member_combined_binding c ON c.combined_binding_id = m.combined_binding_id
    WHERE m.revision_id = NEW.revision_id;
    IF contexts > 1 THEN
        RAISE EXCEPTION
            'build set % mixes % execution contexts across its members: one build renders one '
            'environment, and members adopted in different contexts would produce an artifact no '
            'single environment could execute',
            NEW.revision_id, contexts;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS build_set_member_combined_binding_total ON build_set_member;
CREATE CONSTRAINT TRIGGER build_set_member_combined_binding_total
    AFTER INSERT ON build_set_member
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION build_member_combined_binding_is_total();

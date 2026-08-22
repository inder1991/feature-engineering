-- src/featuregen/db/migrations/1103_retirement_scope_and_authoring_identity.sql
-- DECOUPLING TWO GUARDS THAT RIDE ON ONE MECHANISM, and giving each its own key.
--
-- THE MONEY guard means "do not pay twice for the same exact configuration".
-- THE RETIREMENT guard means "do not silently regenerate something deliberately withdrawn".
-- Today they are the same code path: `request_draft` inserts ON CONFLICT (formula_identity_hash) DO
-- NOTHING and reads `formula_draft_retirement` ONLY WHEN THE INSERT LOSES. So retirement is enforced
-- as a side effect of identity collision — and any correction to the identity hash, however right,
-- moves retirement with it. A won INSERT is today's silent permission to spend.
--
-- ▲ WHICH IS WHY `_authoring_config_hash` CANNOT SIMPLY BE FIXED. It returns a CONSTANT (getattr on
-- a dict), and correcting it re-mints every identity, so every INSERT then wins and NO retirement
-- row is ever read again. Two governance rules, one mechanism, and the one-line fix moves both.
--
-- So retirement moves off the identity hash onto a key that survives every future composition
-- change: the RETIREMENT SCOPE KEY, over the five identity fields that are not the authoring
-- configuration. All five are already columns on `formula_draft` (1090), so it is computable today
-- with no fabrication.
--
-- ▲ AND THE SCOPE KEY IS ALSO THE AUTHORING SUBJECT. Those same five fields are what an
-- AUTHOR_FORMULA authorization authorizes (parent §0.1.4). One tuple, one hash, three uses:
-- retirement withdraws a subject, authorization authorizes a subject, and the money guard's
-- non-configuration half IS that subject. Naming it twice is how the two definitions drift apart.

-- ── the authoring identity companion ────────────────────────────────────────────────────────────
-- ▲ A COMPANION TABLE, NOT COLUMNS ON `formula_draft`. 1090's `formula_draft_guard` freezes a named
-- ten-column tuple and permits UPDATE elsewhere — but a companion means no UPDATE to `formula_draft`
-- is needed at all. Extend, never alter.
-- ▲ CREATED BEFORE THE TABLE THAT REFERENCES IT. A composite foreign key needs a matching unique
-- constraint on its parent to already exist; declaring it afterwards fails with "no unique
-- constraint matching given keys", which names the symptom and not the order. A superset of
-- `formula_draft`'s primary key, so it is unique by construction and cannot fail on existing rows.
CREATE UNIQUE INDEX IF NOT EXISTS formula_draft_config_key
    ON formula_draft (formula_draft_id, authoring_config_hash);

CREATE TABLE IF NOT EXISTS formula_draft_authoring_identity (
    formula_draft_id      text PRIMARY KEY REFERENCES formula_draft(formula_draft_id),

    -- V1 is the CONSTANT era. Recorded as what it literally was, which is a record of the DEFECT
    -- and never a claim about a historical model configuration.
    identity_version      smallint NOT NULL CHECK (identity_version IN (1, 2)),

    retirement_scope_key  text NOT NULL CHECK (btrim(retirement_scope_key) <> ''),

    -- Inspectable, so the hash is checkable. A bare hash can only be compared, and "which field
    -- moved" is the question every mismatch raises.
    config_payload_json   jsonb NOT NULL,

    -- ▲ TRAVELS INSIDE A COMPOSITE FOREIGN KEY, below. Without that, a V2 companion could attach to
    -- a draft whose stored `authoring_config_hash` is something else — an identity version
    -- describing a draft it does not match. Two independent columns for one fact is the defect 1101
    -- exists to forbid, and repeating it here would be the same mistake with different tables.
    config_hash           text NOT NULL CHECK (btrim(config_hash) <> ''),

    recorded_at           timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT formula_draft_authoring_identity_matches_its_draft
        FOREIGN KEY (formula_draft_id, config_hash)
        REFERENCES formula_draft (formula_draft_id, authoring_config_hash)
);

CREATE INDEX IF NOT EXISTS formula_draft_authoring_identity_by_scope
    ON formula_draft_authoring_identity (retirement_scope_key);

-- ── the tombstone ───────────────────────────────────────────────────────────────────────────────
-- ▲ RETIREMENT SCOPE IS A GOVERNANCE DECISION, NOT A MIGRATION. Today "retire this draft" means an
-- EXACT draft identity. An earlier revision keyed this table `retirement_scope_key PRIMARY KEY`,
-- which silently widened every retirement to "this candidate under every current and future model,
-- prompt and strategy" — a new governance power granted by a schema choice. It also could not
-- represent what it then described: every exact draft of one candidate shares that candidate's
-- scope key, so a single-column key means retiring configuration A consumes the key and B can never
-- be retired, a candidate-wide tombstone cannot follow an exact one, and an exception cannot say
-- WHICH tombstone it overrides.
CREATE TABLE IF NOT EXISTS formula_draft_retirement_tombstone (
    tombstone_id                text PRIMARY KEY CHECK (btrim(tombstone_id) <> ''),

    scope                       text NOT NULL CHECK (scope IN (
                                    'EXACT_DRAFT', 'CANDIDATE_ACROSS_CONFIGURATIONS')),

    -- The candidate this covers, both scopes.
    retirement_scope_key        text NOT NULL CHECK (btrim(retirement_scope_key) <> ''),

    -- Present exactly for EXACT_DRAFT. ▲ Historical retirements are EXACT — reinterpreting them as
    -- candidate-wide would retroactively withdraw formulas nobody withdrew.
    exact_formula_identity_hash text,

    -- WHAT THIS TOMBSTONE ACTUALLY COVERS: the scope key alone for a candidate-wide retirement, the
    -- exact identity for an exact one. Deriving it lets one unique constraint stop a duplicate of
    -- the same act while permitting both kinds, and any number of exact ones, to coexist.
    coverage_identity_hash      text NOT NULL CHECK (btrim(coverage_identity_hash) <> ''),

    formula_draft_id            text NOT NULL REFERENCES formula_draft(formula_draft_id),
    reason                      text NOT NULL CHECK (btrim(reason) <> ''),
    detail                      text,
    replacement_draft_id        text REFERENCES formula_draft(formula_draft_id),
    retired_by                  text NOT NULL CHECK (btrim(retired_by) <> ''),
    recorded_at                 timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT formula_draft_retirement_tombstone_exact_names_its_identity
        CHECK ((scope = 'EXACT_DRAFT') = (exact_formula_identity_hash IS NOT NULL)),

    CONSTRAINT formula_draft_retirement_tombstone_once
        UNIQUE (scope, coverage_identity_hash)
);

CREATE INDEX IF NOT EXISTS formula_draft_retirement_tombstone_by_scope
    ON formula_draft_retirement_tombstone (retirement_scope_key);

-- ── the exception ───────────────────────────────────────────────────────────────────────────────
-- ▲ AN EXCEPTION MUST BIND, NOT MERELY EXIST. An earlier revision keyed it
-- `(retirement_scope_key, approved_at)` — a scope and a timestamp — which authorizes ANY
-- regeneration of that candidate, at ANY cost, under ANY configuration, FOR EVER.
CREATE TABLE IF NOT EXISTS formula_draft_regeneration_exception (
    exception_id                text PRIMARY KEY CHECK (btrim(exception_id) <> ''),

    -- ▲ THE EXACT TOMBSTONE IT OVERRIDES. Nullable: a legacy regeneration with no retirement is not
    -- an override of anything, and forcing one would invent a withdrawal that never happened.
    tombstone_id                text REFERENCES formula_draft_retirement_tombstone(tombstone_id),

    -- WHAT it authorizes creating. Not "a regeneration" — THIS one.
    target_formula_identity_hash text NOT NULL CHECK (btrim(target_formula_identity_hash) <> ''),
    provider_contract_hash       text NOT NULL CHECK (btrim(provider_contract_hash) <> ''),
    strategy_identity_hash       text NOT NULL CHECK (btrim(strategy_identity_hash) <> ''),

    -- ▲ NULLABLE UNTIL 1105, DELIBERATELY. `llm_spend_authorization_revision` does not exist yet and
    -- migrations apply in lexical order, so the reference cannot be declared here. 1105 adds the
    -- foreign key and the NOT NULL — expand here, contract there, which is the same discipline 1100
    -- uses and the reason an image-only rollback stays safe across this range.
    llm_spend_authorization_id  text,

    actor_subject               text NOT NULL CHECK (btrim(actor_subject) <> ''),

    -- TRUE only when a person overrode a retirement, so the exception is auditable rather than a
    -- flag nobody can find. Constrained to agree with `tombstone_id`: an override with nothing to
    -- override is a claim about an act that did not happen.
    overrides_tombstone         boolean NOT NULL,

    max_uses                    smallint NOT NULL DEFAULT 1 CHECK (max_uses >= 1),
    uses_consumed               smallint NOT NULL DEFAULT 0 CHECK (uses_consumed >= 0),

    -- ▲ NOT OPTIONAL. An approval granted inside a triage window must not still authorize a
    -- re-spend three months later, when nobody remembers granting it.
    expires_at                  timestamptz NOT NULL,
    approved_at                 timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT formula_draft_regeneration_exception_override_names_a_tombstone
        CHECK (overrides_tombstone = (tombstone_id IS NOT NULL)),
    CONSTRAINT formula_draft_regeneration_exception_uses_within_max
        CHECK (uses_consumed <= max_uses)
);

CREATE INDEX IF NOT EXISTS formula_draft_regeneration_exception_by_target
    ON formula_draft_regeneration_exception (target_formula_identity_hash);

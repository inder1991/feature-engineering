-- src/featuregen/db/migrations/1097_recipe_formula_evaluation_contract.sql
-- The IDENTITY a formula evaluation run was conducted under — recorded once, beside the run.
--
-- WHY A TABLE AND NOT MORE COLUMNS ON `recipe_formula_eval_run`. The V2/V3 lane must persist nine
-- things the 1029 run row has no place for: the evaluator contract, the expectation schema, the
-- formula WIRE version, the canonicalization version, and a corpus and registry that are not V1's.
-- Adding them as columns would have meant a second migration for the next version, and a run row
-- that is half identity and half configuration. Here the identity is ONE value — a row, addressed
-- by the hash of its own content — so "what was this evaluated under" has a single answer that can
-- be compared, cited and joined, rather than nine columns that can drift apart.
--
-- ADDRESSED BY CONTENT, so the same identity is the same row. Two runs under identical versions
-- share a contract; a single changed version mints a different hash and therefore a different row.
-- That is what makes "did these two runs measure the same thing" a key comparison instead of a
-- nine-way column diff.
--
-- ▲ AND IT IS WHY V1 EVIDENCE CANNOT BE RELABELLED. `recipe_formula_eval` stamps V1's
-- `OPERATION_GRAMMAR_VERSION` / `OUTPUT_POLICY_VERSION`, both of which happen to equal 1 — the same
-- integers the V2 constants carry today. That is ACCIDENTAL NUMERIC EQUALITY, not compatibility:
-- swapping an import would have turned V1 evidence into "V2" without changing anything about what
-- was actually evaluated. The fields that cannot collide by accident — the evaluator contract, the
-- expectation schema, the wire version, the corpus — are recorded here precisely so the difference
-- survives in the data rather than living in whichever module a reader happens to open.

CREATE TABLE IF NOT EXISTS recipe_formula_evaluation_contract (
    -- The canonical hash of every other identity-bearing column in this row. Not a surrogate id:
    -- recomputing it from the columns is how a reader checks the row has not been tampered with.
    contract_hash                 text PRIMARY KEY CHECK (length(contract_hash) = 64),

    -- WHICH EVALUATOR. The one field that no version-number coincidence can forge.
    evaluator_contract_version    text NOT NULL CHECK (btrim(evaluator_contract_version) <> ''),

    -- WHAT AN EXPECTATION IS under this contract — 'formula-v2' means reviewed gold fixtures
    -- pinned by canonical proposal hash, not V1's unary count-distinct blueprints.
    --
    -- Deliberately NOT a closed vocabulary. A closed list would make the database refuse an
    -- identity the code had correctly produced the day a v3 expectation shape exists, and refusing
    -- to RECORD what happened is the one failure this table must never have.
    expectation_schema            text NOT NULL CHECK (btrim(expectation_schema) <> ''),

    -- THE WIRE the provider was asked to speak, which is separate from the language the formula is
    -- written in: a V3 wire carries a V2 formula plus optional row selections.
    formula_wire_schema_version   integer NOT NULL CHECK (formula_wire_schema_version > 0),

    operation_grammar_version     integer NOT NULL CHECK (operation_grammar_version > 0),
    output_policy_version         integer NOT NULL CHECK (output_policy_version > 0),
    canonicalization_version      integer NOT NULL CHECK (canonicalization_version > 0),

    -- THE REVIEWED MATERIAL. Version and content hash both, because a corpus that grew without its
    -- version moving is the failure this pair exists to make visible.
    corpus_version                text NOT NULL CHECK (btrim(corpus_version) <> ''),
    corpus_content_hash           text NOT NULL CHECK (length(corpus_content_hash) = 64),
    expectation_registry_hash     text NOT NULL CHECK (length(expectation_registry_hash) = 64),

    -- THE PROVIDER CONTRACTS, byte-frozen upstream and cited here so a run's identity includes what
    -- the model was actually shown.
    author_provider_contract_hash text NOT NULL CHECK (btrim(author_provider_contract_hash) <> ''),
    critic_provider_contract_hash text NOT NULL CHECK (btrim(critic_provider_contract_hash) <> ''),

    recorded_at                   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS recipe_formula_evaluation_contract_by_evaluator
    ON recipe_formula_evaluation_contract (evaluator_contract_version, recorded_at DESC);

-- ── immutable, with no later-filled field ───────────────────────────────────────────────────────
-- Unlike a draft retirement — whose replacement is legitimately named later — every column here is
-- known at the moment the identity is minted. So this guard refuses UPDATE outright rather than
-- permitting a narrow one: an identity that could be edited is not an identity.
CREATE OR REPLACE FUNCTION recipe_formula_evaluation_contract_guard() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'recipe_formula_evaluation_contract is immutable: an evaluation identity that '
                    'could be edited or removed would let a run claim it measured something it did '
                    'not. A different identity is a different row';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS recipe_formula_evaluation_contract_no_change
    ON recipe_formula_evaluation_contract;
CREATE TRIGGER recipe_formula_evaluation_contract_no_change
    BEFORE UPDATE OR DELETE ON recipe_formula_evaluation_contract
    FOR EACH ROW EXECUTE FUNCTION recipe_formula_evaluation_contract_guard();

-- ── the link from a run to what it was run under ────────────────────────────────────────────────
-- NULLABLE, for exactly as long as the V1 lane still exists. A V1 run has no contract here and must
-- not pretend to; the V2/V3 lane refuses to create a run without one, in code, today.
--
-- ▲ THIS COLUMN BECOMES NOT NULL WHEN THE V1 EVALUATOR IS DELETED — that is step 5 of the §0.5
-- transition ("make a missing evaluation/version identity TERMINAL"), and it is a later migration
-- rather than a promise, because the V1 rows have to be gone first.
ALTER TABLE recipe_formula_eval_run
    ADD COLUMN IF NOT EXISTS evaluation_contract_hash text NULL
        REFERENCES recipe_formula_evaluation_contract (contract_hash);

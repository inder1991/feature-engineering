-- src/featuregen/db/migrations/1069_contract_option_link.sql
-- SUCCESSOR 4 of the formula/execution seam (reservation 1069): a governed contract records the
-- SEMANTIC OPTION it was minted from.
--
-- THE HOLE THIS CLOSES. C3 built `requirements_closed` as a real read of the CONTRACT-keyed
-- validation store, and `POST /materialization-runs` — the one route that gates materialization —
-- had no way to name a contract, so it passed `contract_id=None` and the read answered `False`
-- forever. The missing fact was not a query; it was a RECORD: nothing anywhere said which contract
-- a given option decision produced. `contract` is keyed by feature identity and
-- `semantic_option_decision` by (considered_revision_id, option_id), and no row joined the two.
--
-- WHY THE LINK LIVES ON `contract`, and why it is stamped at the INSERT.
--   * A contract is minted FROM a choice of an option. `confirm_contract` is the single writer
--     (one production call site, `api/routes/contract.py`), and the confirm route already holds
--     BOTH halves of the option key there — it loads `load_frozen_option_facts` for the A2
--     confirm-time re-check immediately before minting. The moment both keys are in hand is the
--     moment the link is written, and no later reconstruction is possible or attempted.
--   * The reverse direction cannot exist. `semantic_option_decision` (1063) is APPEND-ONLY and is
--     written at GENERATION — before any contract exists — so a contract_id column there could
--     never be filled without a mutation its own triggers refuse.
--   * A dedicated link table would be a third store for a fact that is one-to-one with a contract
--     VERSION and immutable with it, and it would need its own write-once triggers to earn what
--     1012 already gives this table for free.
--   * A join BY NAME is refused on purpose. `feature_name` is not identity: a re-confirm mints a
--     NEW contract version under the same name (H2b), and two intents may choose options that
--     carry the same feature name. Resolving a contract by name would be a guess wearing a join's
--     clothes.
--
-- NULLABLE, and the WORM table makes that honest rather than a concession. 1012 forbids UPDATE and
-- DELETE on `contract`, so these columns can ONLY ever be written by the INSERT that mints the row.
-- Every contract governed before this migration therefore keeps a permanent, truthful NULL — there
-- is no backfill path, and inventing one would mean asserting a provenance nobody recorded. Reads
-- stay FAIL-CLOSED on a NULL: no link → no contract_id → `requirements_closed` False. The defect
-- narrows to legacy rows, and it narrows honestly.
--
-- Cardinality: MANY contracts to ONE option (a re-confirm of the same choice mints a new version,
-- and both versions name the option they came from); at most ONE option per contract version.
--
-- Shape follows 1067's house style, for the same reasons stated there:
--   * COMPOSITE FOREIGN KEY to `semantic_option_decision (considered_revision_id, option_id)` —
--     1063's own UNIQUE constraint `semantic_option_decision_option_uq` is the target. A pair
--     naming no decision row is refused by the DATABASE, not by a writer's good intentions.
--   * MATCH SIMPLE (the default): a row with either column NULL satisfies the FK, so the legacy
--     and never-served paths are unconstrained while a *stated* pair must be real.
--   * The half-stated case is its own named CHECK, so the refusal says "an option is named by BOTH
--     halves" instead of quoting a foreign key at somebody.
--   * NO ON DELETE / ON UPDATE: `semantic_option_decision` is append-only, so a cascade would
--     describe an event that cannot happen.
--   * An index on the pair, because the question this link exists to answer — "which contract did
--     this approved option mint?" — is a lookup BY the option, not by the contract.
ALTER TABLE contract
    ADD COLUMN IF NOT EXISTS considered_revision_id text NULL,
    ADD COLUMN IF NOT EXISTS option_id              text NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'contract_option_fk'
          AND conrelid = 'contract'::regclass
    ) THEN
        ALTER TABLE contract
            ADD CONSTRAINT contract_option_fk
            FOREIGN KEY (considered_revision_id, option_id)
            REFERENCES semantic_option_decision (considered_revision_id, option_id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'contract_option_is_whole'
          AND conrelid = 'contract'::regclass
    ) THEN
        ALTER TABLE contract
            ADD CONSTRAINT contract_option_is_whole
            CHECK ((considered_revision_id IS NULL) = (option_id IS NULL));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS contract_option_idx
    ON contract (considered_revision_id, option_id)
    WHERE considered_revision_id IS NOT NULL;

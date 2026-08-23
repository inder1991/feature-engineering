-- src/featuregen/db/migrations/1096_formula_draft_retirement.sql
-- Retiring a formula draft WITHOUT deleting it — because deleting it is impossible, by design.
--
-- THE PROBLEM. A draft can become wrong about itself. The first case is real: until `027cc923` the
-- draft worker declared `formula_schema: 3` while driving the provider under the v2 author contract,
-- so drafts reached READY holding a v2 formula under a v3 label. Admission refuses them now, but the
-- row still reads READY — a claim that can never be built.
--
-- AND IT CANNOT BE DELETED. `formula_draft_guard` (1090) raises on every DELETE: "formula_draft is
-- append-only". That is correct and stays. A draft is what a person was shown and what an authoring
-- run was spent on; deleting it would destroy the record of both, and the failures recorded on
-- BLOCKED and FAILED drafts are the only evidence those defects were ever real.
--
-- SO RETIREMENT IS AN APPEND, like everything else here. The draft stays exactly as it was; a
-- retirement row says it is no longer the current answer and names what replaced it. Readers exclude
-- or label retired drafts rather than finding them absent — which is also what makes "why is this
-- draft gone?" an answerable question instead of a gap.
--
-- ONE RETIREMENT PER DRAFT. The primary key is the draft id: retiring twice is not two facts, and a
-- second row would let two reasons and two replacements disagree about one draft.
--
-- APPLIED to the live kind cluster on 2026-08-21 (192 migrations). Backup taken first:
-- ~/featuregen-backups/featuregen-pre-1096-20260821-161831.sql (134M), dump -> scratch
-- restore -> dry run -> probe -> live.

CREATE TABLE IF NOT EXISTS formula_draft_retirement (
    formula_draft_id     text PRIMARY KEY REFERENCES formula_draft (formula_draft_id),

    -- WHY, in the vocabulary a reader can act on. Closed, because an open text field becomes a
    -- place to write sentences nobody queries.
    reason               text NOT NULL CHECK (reason IN (
                             -- the manifest and the stored formula name different languages
                             'SCHEMA_CONTRACT_MISMATCH',
                             -- the candidate it was drafted for was superseded
                             'CANDIDATE_SUPERSEDED',
                             -- an operator withdrew it
                             'WITHDRAWN'
                         )),

    -- Free text BESIDE the code, never instead of it: the code is what a query filters on, the
    -- detail is what a person reads.
    detail               text NOT NULL DEFAULT '',

    -- WHAT REPLACED IT, if anything yet. Nullable because retirement and regeneration are separate
    -- acts — regeneration spends provider money and is somebody's decision — and a NOT NULL column
    -- here would force a placeholder that reads as a draft nobody made.
    replacement_draft_id text NULL REFERENCES formula_draft (formula_draft_id),

    retired_by           text NOT NULL CHECK (btrim(retired_by) <> ''),
    retired_at           timestamptz NOT NULL DEFAULT now(),

    -- A draft cannot replace itself: that is a retirement that retires nothing.
    CHECK (replacement_draft_id IS DISTINCT FROM formula_draft_id)
);

CREATE INDEX IF NOT EXISTS formula_draft_retirement_by_reason
    ON formula_draft_retirement (reason, retired_at DESC);

-- ── append-only, for the reason the draft itself is ─────────────────────────────────────────────
-- Except the replacement, which is filled in when regeneration happens — a separate, later act. It
-- may be set ONCE: changing it would make "what replaced this draft" a question with two answers.
CREATE OR REPLACE FUNCTION formula_draft_retirement_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'formula_draft_retirement is append-only: a retirement that could be '
                        'deleted would make a draft silently current again';
    END IF;
    IF (NEW.formula_draft_id, NEW.reason, NEW.detail, NEW.retired_by, NEW.retired_at)
       IS DISTINCT FROM
       (OLD.formula_draft_id, OLD.reason, OLD.detail, OLD.retired_by, OLD.retired_at) THEN
        RAISE EXCEPTION 'a recorded retirement is immutable except for its replacement';
    END IF;
    IF OLD.replacement_draft_id IS NOT NULL
       AND NEW.replacement_draft_id IS DISTINCT FROM OLD.replacement_draft_id THEN
        RAISE EXCEPTION 'this retirement already names a replacement: "what replaced this draft" '
                        'must not have two answers';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS formula_draft_retirement_no_change ON formula_draft_retirement;
CREATE TRIGGER formula_draft_retirement_no_change
    BEFORE UPDATE OR DELETE ON formula_draft_retirement
    FOR EACH ROW EXECUTE FUNCTION formula_draft_retirement_guard();

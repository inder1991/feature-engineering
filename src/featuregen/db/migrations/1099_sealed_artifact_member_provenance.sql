-- src/featuregen/db/migrations/1099_sealed_artifact_member_provenance.sql
-- HOW EACH FEATURE IN A SEALED ARTIFACT WAS ACTUALLY AUTHORED — recorded at sealing, per member.
--
-- WHY THIS EXISTS. Production publication must present the certificate that MATCHES how a formula
-- was authored: a platform gold evaluation for an LLM-authored one, a deterministic compiler
-- certification for a reviewed-blueprint one. `sealed_artifact_v2` records formula and IR hashes,
-- the plan, the environment and the authorization — and nothing about the authoring RUN or METHOD.
-- Both disappear at sealing, so the production gate has no basis on which to choose.
--
-- ▲ A CHILD TABLE, NOT A COLUMN, and this is the load-bearing shape decision. One artifact can hold
-- SEVERAL features authored differently — a reviewed-blueprint instantiation beside an LLM-authored
-- novel feature — so a single `authoring_method` on the artifact could only ever be a lie about one
-- of them. The production gate folds every member with an ALL-MUST-PASS rule; that needs a row per
-- member.
--
-- ▲ AND CANDIDATE ORIGIN IS NOT AUTHORING METHOD. `formula_draft_worker` ALWAYS drives the LLM
-- author and critic, so a feature the user picked from a RECIPE recommendation is still
-- LLM_AUTHORED and still needs the LLM certificate. Deriving the method from `generation_source`
-- would hand recipe-origin features a certificate that never covered how they were written. The
-- method here is DERIVED FROM VERIFIED EVIDENCE — reconciled provider calls, or a REVIEW_BYPASSED
-- trace naming the exact blueprint — never from what the candidate called itself.
--
-- ▲ WRITTEN DURING SEALING, NEVER RECONSTRUCTED. Looking up "the latest draft for this selection"
-- afterwards is not the same question: another draft may have been created since, and the answer
-- would describe a formula this artifact does not contain.

CREATE TABLE IF NOT EXISTS sealed_artifact_member_provenance (
    artifact_id            text NOT NULL REFERENCES sealed_artifact_v2 (artifact_id),

    -- The published feature name within the artifact. With `artifact_id` it is the key: one
    -- provenance per member, and a second row for one member would make "how was this authored"
    -- a question with two answers.
    member_name            text NOT NULL CHECK (btrim(member_name) <> ''),

    -- WHAT THE PERSON SELECTED, and the draft and run that answered it. All three, because they
    -- answer different questions: which choice, which artifact of that choice, and which authoring
    -- act produced it.
    selection_revision_id  text NOT NULL CHECK (btrim(selection_revision_id) <> ''),
    formula_draft_id       text NULL REFERENCES formula_draft (formula_draft_id),
    authoring_run_id       text NULL REFERENCES formula_authoring_run (authoring_run_id),

    -- The formula this member actually carries, so the row can be tied to the sealed bytes rather
    -- than merely to a draft that may since have moved on.
    formula_content_hash   text NOT NULL CHECK (btrim(formula_content_hash) <> ''),

    -- HOW IT WAS AUTHORED. A closed vocabulary, because the production gate dispatches on it and an
    -- unrecognised value must never be interpretable as "probably fine".
    --
    -- `HUMAN_AUTHORED` is deliberately ABSENT rather than present-and-refused: there is no human
    -- authoring evidence lane yet, so a row claiming it could not be substantiated. When that lane
    -- exists it arrives with the evidence that makes it checkable, in a later migration.
    authoring_method       text NOT NULL CHECK (authoring_method IN (
                               -- reconciled author AND critic provider calls
                               'LLM_AUTHORED',
                               -- a REVIEW_BYPASSED trace naming the exact reviewed blueprint
                               'REVIEWED_RECIPE_BLUEPRINT'
                           )),

    -- The evidence the method was DERIVED from, hashed. This is what makes the method checkable
    -- rather than merely asserted: a reader can re-derive it and compare.
    authoring_evidence_hash text NOT NULL CHECK (length(authoring_evidence_hash) = 64),

    recorded_at            timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (artifact_id, member_name),

    -- An LLM-authored member MUST name the run whose provider calls are its evidence. Without it
    -- the method cannot be re-derived, and an unverifiable claim is what this table exists to stop.
    CONSTRAINT sealed_artifact_member_llm_names_its_run CHECK (
        authoring_method <> 'LLM_AUTHORED' OR authoring_run_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS sealed_artifact_member_provenance_by_method
    ON sealed_artifact_member_provenance (authoring_method, recorded_at DESC);

CREATE INDEX IF NOT EXISTS sealed_artifact_member_provenance_by_run
    ON sealed_artifact_member_provenance (authoring_run_id);

-- ── append-only, for the reason the artifact itself is ──────────────────────────────────────────
-- This is the record of how a published number was produced. A row that could be edited after the
-- fact would let an artifact acquire a more convenient authoring method than the one it had.
CREATE OR REPLACE FUNCTION sealed_artifact_member_provenance_guard() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'sealed_artifact_member_provenance is append-only: how a sealed feature was '
                    'authored is a fact about a past act, and a production certificate is chosen '
                    'from it';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS sealed_artifact_member_provenance_no_change
    ON sealed_artifact_member_provenance;
CREATE TRIGGER sealed_artifact_member_provenance_no_change
    BEFORE UPDATE OR DELETE ON sealed_artifact_member_provenance
    FOR EACH ROW EXECUTE FUNCTION sealed_artifact_member_provenance_guard();

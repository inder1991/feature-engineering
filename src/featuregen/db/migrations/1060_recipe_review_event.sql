-- src/featuregen/db/migrations/1060_recipe_review_event.sql
-- BR-23 schema half, pulled forward into R1 per the banking-recipe plan's re-baseline amendment
-- (D7 reservation 1060 appended in this same commit): the family migrations (BR-11..BR-16) must
-- have somewhere DURABLE to record the SME decisions they produce as they produce them — not a
-- late re-review pass. The validity fold, decision APIs, invalidation wiring and batch reports
-- remain BR-23 proper; this is the store they will read.
--
-- Shape decisions:
--  * APPEND-ONLY (1034's guard idiom: row-level UPDATE/DELETE + statement-level TRUNCATE triggers,
--    one shared function): review history is evidence, and evidence that can be rewritten proves
--    nothing. A changed mind is a NEW event superseding the old one, attributably.
--  * REVISION-SPECIFIC by construction: `recipe_revision_hash` is the canonical-recipe-v2 hash,
--    so an approval simply does not exist for any edited definition — "a changed formula
--    automatically makes the previous approval stale" is a lookup miss, not a status flip.
--  * `supersedes_event_id` is the explicit audit chain (self-referencing FK); "current" is a READ
--    projection (newest event per recipe+revision), never a mutable column.
--  * List-shaped evidence (objectives, gold refs, policy deps, stages) is jsonb ARRAYS written by
--    the schema layer (closed vocabularies enforced in recipe_review.py, CHECKs here for the
--    decision set) — the store guards shape, the code guards meaning.
CREATE TABLE IF NOT EXISTS recipe_review_event (
    event_id                        text        PRIMARY KEY,
    recipe_id                       text        NOT NULL,
    recipe_revision_hash            text        NOT NULL,   -- canonical-recipe-v2 hash
    output_id                       text        NOT NULL DEFAULT '',
    decision                        text        NOT NULL,
    reviewer                        text        NOT NULL,
    reviewer_role                   text        NOT NULL,
    reviewed_primary_objective      text        NOT NULL DEFAULT '',
    reviewed_supporting_objectives  jsonb       NOT NULL DEFAULT '[]'::jsonb,
    formula_expectation_hash        text        NULL,
    gold_corpus_refs                jsonb       NOT NULL DEFAULT '[]'::jsonb,
    policy_dependencies             jsonb       NOT NULL DEFAULT '[]'::jsonb,
    permitted_stages                jsonb       NOT NULL DEFAULT '[]'::jsonb,
    prohibited_stages               jsonb       NOT NULL DEFAULT '[]'::jsonb,
    rationale                       text        NOT NULL DEFAULT '',
    evidence_refs                   jsonb       NOT NULL DEFAULT '[]'::jsonb,
    supersedes_event_id             text        NULL REFERENCES recipe_review_event(event_id),
    recorded_at                     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT recipe_review_decision_chk CHECK (
        decision IN ('approved', 'changes_required', 'rejected', 'retired'))
);

CREATE INDEX IF NOT EXISTS recipe_review_event_recipe_idx
    ON recipe_review_event (recipe_id, recorded_at);
CREATE INDEX IF NOT EXISTS recipe_review_event_revision_idx
    ON recipe_review_event (recipe_id, recipe_revision_hash, recorded_at);

CREATE OR REPLACE FUNCTION recipe_review_event_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'recipe_review_event is append-only: %% is not allowed. Review history is evidence, and '
        'evidence that can be rewritten proves nothing — record a NEW event that supersedes the '
        'old one.';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER recipe_review_event_no_mutation
    BEFORE UPDATE OR DELETE ON recipe_review_event
    FOR EACH ROW EXECUTE FUNCTION recipe_review_event_append_only();
-- A FOR EACH ROW trigger does not fire on TRUNCATE; this is the only guard that does.
CREATE OR REPLACE TRIGGER recipe_review_event_no_truncate
    BEFORE TRUNCATE ON recipe_review_event
    FOR EACH STATEMENT EXECUTE FUNCTION recipe_review_event_append_only();

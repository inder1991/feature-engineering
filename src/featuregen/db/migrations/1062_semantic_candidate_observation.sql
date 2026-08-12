-- src/featuregen/db/migrations/1062_semantic_candidate_observation.sql
-- SE-10 slice 1 (reservation 1062 per the semantic-eligibility plan): the durable
-- semantic-candidate observation store — every shadow (and later semantic_v1) run's
-- per-candidate truth, persisted so fleet metrics come from ROWS, never from grepped logs.
--
-- Shape decisions:
--  * APPEND-ONLY (the 1034/1060 guard idiom): an observation is what a run SAW under a frozen
--    context; rewriting it would destroy exactly the comparison the shadow exists to make.
--  * Keyed to the run AND the frozen context hash: the same recipe observed under two catalog
--    states is two rows, honestly.
--  * `verdicts` / `eligibility` are the binder's own outputs serialized whole (bounded by the
--    binder's 16-per-operand shortlist), `policy_hashes` pins the authority matrix + operand
--    class map + planning-request identity the row was decided under.
CREATE TABLE IF NOT EXISTS semantic_candidate_observation (
    observation_id          text        PRIMARY KEY,
    generation_run_id       text        NOT NULL,
    catalog_source          text        NOT NULL,
    context_hash            text        NOT NULL,
    source_origin           text        NOT NULL,
    source_definition_id    text        NOT NULL,
    planning_request_hash   text        NOT NULL,
    relationship            text        NOT NULL DEFAULT '',
    binding_state           text        NOT NULL,
    readiness               text        NOT NULL,
    review_current          boolean     NOT NULL DEFAULT false,
    temporal_blocked        boolean     NOT NULL DEFAULT false,
    verdicts                jsonb       NOT NULL DEFAULT '[]'::jsonb,
    eligibility             jsonb       NOT NULL DEFAULT '[]'::jsonb,
    policy_hashes           jsonb       NOT NULL DEFAULT '{}'::jsonb,
    recorded_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT semantic_candidate_binding_state_chk CHECK (
        binding_state IN ('bound', 'ambiguous', 'missing', 'blocked'))
);

CREATE INDEX IF NOT EXISTS semantic_candidate_observation_run_idx
    ON semantic_candidate_observation (generation_run_id, recorded_at);
CREATE INDEX IF NOT EXISTS semantic_candidate_observation_defn_idx
    ON semantic_candidate_observation (source_definition_id, recorded_at);

CREATE OR REPLACE FUNCTION semantic_candidate_observation_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'semantic_candidate_observation is append-only: %% is not allowed. An observation is '
        'what a run saw under a frozen context — rewriting it destroys the comparison the '
        'shadow exists to make.';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER semantic_candidate_observation_no_mutation
    BEFORE UPDATE OR DELETE ON semantic_candidate_observation
    FOR EACH ROW EXECUTE FUNCTION semantic_candidate_observation_append_only();
CREATE OR REPLACE TRIGGER semantic_candidate_observation_no_truncate
    BEFORE TRUNCATE ON semantic_candidate_observation
    FOR EACH STATEMENT EXECUTE FUNCTION semantic_candidate_observation_append_only();

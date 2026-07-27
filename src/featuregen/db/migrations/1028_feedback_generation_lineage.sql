-- Delivery R6: explicit scope and generation-run supersession for confirmed feedback rounds.
-- Historical broaden rows keep their existing scope-only lineage; every new writer supplies both.

ALTER TABLE confirmed_generation_scope
    ADD COLUMN supersedes_generation_run_id text NULL;
ALTER TABLE confirmed_generation_scope
    ADD CONSTRAINT confirmed_generation_scope_superseded_run_fk
    FOREIGN KEY (supersedes_generation_run_id)
    REFERENCES feature_generation_run(generation_run_id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE OR REPLACE FUNCTION featuregen_check_feedback_supersession_lineage() RETURNS trigger AS $$
BEGIN
    IF (
        NEW.supersedes_scope_id IS NULL
        AND NEW.supersedes_generation_run_id IS NULL
    ) THEN
        RETURN NULL;
    END IF;
    IF NOT EXISTS (
        SELECT 1
          FROM confirmed_generation_scope prior
         WHERE prior.scope_id = NEW.supersedes_scope_id
           AND prior.generation_run_id = NEW.supersedes_generation_run_id
           AND prior.intent_id = NEW.intent_id
    ) THEN
        RAISE EXCEPTION 'confirmed scope % has inconsistent feedback supersession lineage',
            NEW.scope_id;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER confirmed_generation_scope_feedback_lineage_deferred
    AFTER INSERT ON confirmed_generation_scope
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION featuregen_check_feedback_supersession_lineage();


-- Delivery R1: seal the exact redacted recognition input and the exact generation input used by one
-- confirmed-scope feature-generation run. Legacy recognition rows remain readable for audit, but their
-- nullable input columns cannot authorize confirmation-required generation.

ALTER TABLE intent_recognition_attempt
    ADD COLUMN IF NOT EXISTS input_json jsonb;

ALTER TABLE intent_recognition_attempt
    ADD COLUMN IF NOT EXISTS input_content_hash text;

ALTER TABLE intent_recognition_attempt
    ADD COLUMN IF NOT EXISTS redaction_policy_version text;

CREATE TABLE IF NOT EXISTS contract_generation_input (
    generation_run_id              text        PRIMARY KEY
        REFERENCES feature_generation_run (generation_run_id),
    intent_id                      text        NOT NULL REFERENCES contract_intent (intent_id),
    recognition_id                 text        NOT NULL
        REFERENCES intent_recognition_attempt (recognition_id),
    confirmed_scope_id             text        NOT NULL
        REFERENCES confirmed_generation_scope (scope_id),
    redacted_hypothesis            text        NOT NULL,
    redacted_definition            text        NOT NULL DEFAULT '',
    redacted_prediction_goal       text        NOT NULL DEFAULT '',
    redacted_feedback              text        NOT NULL DEFAULT '',
    target_ref                     text,
    recognition_input_content_hash text        NOT NULL,
    generation_input_content_hash  text        NOT NULL,
    created_by                     jsonb       NOT NULL,
    created_at                     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS contract_generation_input_intent_idx
    ON contract_generation_input (intent_id);
CREATE INDEX IF NOT EXISTS contract_generation_input_recognition_idx
    ON contract_generation_input (recognition_id);

-- The copied identifiers and recognition hash are not merely provenance labels: they must describe
-- one internally consistent intent -> recognition -> run -> confirmed-scope chain.
CREATE OR REPLACE FUNCTION contract_generation_input_check_lineage() RETURNS trigger AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM feature_generation_run r
          JOIN confirmed_generation_scope s
            ON s.generation_run_id = r.generation_run_id
          JOIN intent_recognition_attempt a
            ON a.recognition_id = s.recognition_id
         WHERE r.generation_run_id = NEW.generation_run_id
           AND r.intent_id = NEW.intent_id
           AND s.scope_id = NEW.confirmed_scope_id
           AND s.intent_id = NEW.intent_id
           AND s.recognition_id = NEW.recognition_id
           AND a.intent_id = NEW.intent_id
           AND a.input_content_hash = NEW.recognition_input_content_hash
    ) THEN
        RAISE EXCEPTION
            'contract_generation_input lineage is inconsistent for generation_run_id=%',
            NEW.generation_run_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER contract_generation_input_lineage_on_insert
    BEFORE INSERT ON contract_generation_input
    FOR EACH ROW EXECUTE FUNCTION contract_generation_input_check_lineage();

CREATE OR REPLACE FUNCTION contract_generation_input_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'contract_generation_input records are write-once: % not allowed for generation_run_id=%',
        TG_OP, COALESCE(OLD.generation_run_id, NEW.generation_run_id);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER contract_generation_input_no_mutation
    BEFORE UPDATE OR DELETE ON contract_generation_input
    FOR EACH ROW EXECUTE FUNCTION contract_generation_input_write_once();

-- Recognition attempts were documented as append-only in 0974, but only role grants enforced that
-- promise. A trigger makes the new sealed input immutable even in privileged/test sessions.
CREATE OR REPLACE FUNCTION intent_recognition_attempt_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'intent_recognition_attempt records are write-once: % not allowed for recognition_id=%',
        TG_OP, COALESCE(OLD.recognition_id, NEW.recognition_id);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER intent_recognition_attempt_no_mutation
    BEFORE UPDATE OR DELETE ON intent_recognition_attempt
    FOR EACH ROW EXECUTE FUNCTION intent_recognition_attempt_write_once();

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'featuregen_app') THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON contract_generation_input FROM featuregen_app;
    END IF;
END $$;

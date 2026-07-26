-- Delivery R2: append-only, exact Gate-1 choices. The legacy contract_gate1_choice table remains the
-- emergency-mode latest pointer; confirmation-required draft/confirm use this run/revision/option record.

CREATE TABLE IF NOT EXISTS contract_gate1_choice_revision (
    choice_id                         text        PRIMARY KEY,
    intent_id                         text        NOT NULL REFERENCES contract_intent (intent_id),
    generation_run_id                 text        NOT NULL
        REFERENCES feature_generation_run (generation_run_id),
    considered_revision_id            text        NOT NULL
        REFERENCES contract_considered_revision (considered_revision_id),
    considered_content_hash           text        NOT NULL,
    option_id                          text        NOT NULL,
    canonical_candidate_identity_hash text        NOT NULL,
    actor                              jsonb       NOT NULL,
    why                                text        NOT NULL DEFAULT '',
    chosen_at                          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (generation_run_id, considered_revision_id, option_id)
);
CREATE INDEX IF NOT EXISTS contract_gate1_choice_revision_intent_idx
    ON contract_gate1_choice_revision (intent_id);

CREATE OR REPLACE FUNCTION contract_gate1_choice_revision_check_lineage() RETURNS trigger AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM contract_considered_revision r
          JOIN feature_generation_run g
            ON g.generation_run_id = r.generation_run_id
         WHERE r.considered_revision_id = NEW.considered_revision_id
           AND r.intent_id = NEW.intent_id
           AND r.generation_run_id = NEW.generation_run_id
           AND r.considered_content_hash = NEW.considered_content_hash
           AND g.intent_id = NEW.intent_id
    ) THEN
        RAISE EXCEPTION
            'contract_gate1_choice_revision lineage is inconsistent for choice_id=%',
            NEW.choice_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER contract_gate1_choice_revision_lineage_on_insert
    BEFORE INSERT ON contract_gate1_choice_revision
    FOR EACH ROW EXECUTE FUNCTION contract_gate1_choice_revision_check_lineage();

CREATE OR REPLACE FUNCTION contract_gate1_choice_revision_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'contract_gate1_choice_revision records are write-once: % not allowed for choice_id=%',
        TG_OP, COALESCE(OLD.choice_id, NEW.choice_id);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER contract_gate1_choice_revision_no_mutation
    BEFORE UPDATE OR DELETE ON contract_gate1_choice_revision
    FOR EACH ROW EXECUTE FUNCTION contract_gate1_choice_revision_write_once();

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'featuregen_app') THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON contract_gate1_choice_revision FROM featuregen_app;
    END IF;
END $$;

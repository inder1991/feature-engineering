-- Child-1 prerequisite: immutable formula authoring manifests and ordered trace events.
CREATE TABLE IF NOT EXISTS formula_authoring_run (
    authoring_run_id text PRIMARY KEY,
    intent_hash      text NOT NULL,
    versions         jsonb NOT NULL,
    actor             jsonb NULL,
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS formula_authoring_trace_event (
    authoring_run_id text NOT NULL REFERENCES formula_authoring_run (authoring_run_id),
    seq              integer NOT NULL CHECK (seq >= 0),
    kind             text NOT NULL CHECK (kind IN (
        'author_turn', 'critic_result', 'validation_result', 'completed', 'failed'
    )),
    idempotency_key  text NOT NULL UNIQUE,
    llm_call_ref     text NULL REFERENCES llm_call (llm_call_ref),
    payload          jsonb NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (authoring_run_id, seq)
);

CREATE UNIQUE INDEX IF NOT EXISTS formula_authoring_one_terminal
    ON formula_authoring_trace_event (authoring_run_id)
    WHERE kind IN ('completed', 'failed');

CREATE OR REPLACE FUNCTION formula_authoring_reject_after_terminal() RETURNS trigger AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM formula_authoring_trace_event
         WHERE authoring_run_id = NEW.authoring_run_id
           AND kind IN ('completed', 'failed')
    ) THEN
        RAISE EXCEPTION 'formula authoring run % is already terminal', NEW.authoring_run_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER formula_authoring_no_event_after_terminal
    BEFORE INSERT ON formula_authoring_trace_event
    FOR EACH ROW EXECUTE FUNCTION formula_authoring_reject_after_terminal();

CREATE OR REPLACE FUNCTION formula_authoring_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% records are write-once: % is not allowed',
        TG_TABLE_NAME, TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER formula_authoring_run_no_mutation
    BEFORE UPDATE OR DELETE ON formula_authoring_run
    FOR EACH ROW EXECUTE FUNCTION formula_authoring_write_once();

CREATE OR REPLACE TRIGGER formula_authoring_event_no_mutation
    BEFORE UPDATE OR DELETE ON formula_authoring_trace_event
    FOR EACH ROW EXECUTE FUNCTION formula_authoring_write_once();

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'featuregen_app') THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON formula_authoring_run FROM featuregen_app;
        REVOKE UPDATE, DELETE, TRUNCATE ON formula_authoring_trace_event FROM featuregen_app;
    END IF;
END $$;

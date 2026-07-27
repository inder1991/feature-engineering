-- Delivery R8: immutable 100-case recognition release evaluation.

CREATE TABLE recognition_eval_run (
    eval_run_id              text PRIMARY KEY,
    corpus_version           text NOT NULL,
    corpus_content_hash      text NOT NULL,
    taxonomy_version         text NOT NULL,
    applicability_version    text NOT NULL,
    recipe_registry_version  text NOT NULL,
    provider                 text NOT NULL,
    model                    text NOT NULL,
    prompt_id                text NOT NULL,
    prompt_version           integer NOT NULL,
    prompt_content_hash      text NOT NULL,
    schema_id                text NOT NULL,
    schema_version           integer NOT NULL,
    schema_content_hash      text NOT NULL,
    generation_controls      jsonb NOT NULL,
    runner_kind              text NOT NULL CHECK (runner_kind IN ('REAL_PROVIDER', 'FAKE_TEST')),
    stability_case_count     integer NOT NULL CHECK (
        stability_case_count >= 0 AND stability_case_count <= 100),
    repeat_count             integer NOT NULL CHECK (repeat_count >= 0),
    token_budget             bigint NOT NULL CHECK (token_budget > 0),
    cost_budget              numeric NOT NULL CHECK (cost_budget >= 0),
    code_commit              text NOT NULL,
    created_by               jsonb NOT NULL,
    created_at               timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE recognition_eval_case (
    eval_run_id          text NOT NULL REFERENCES recognition_eval_run(eval_run_id),
    case_id              text NOT NULL,
    case_json            jsonb NOT NULL,
    case_content_hash    text NOT NULL,
    PRIMARY KEY (eval_run_id, case_id)
);

CREATE TABLE recognition_eval_attempt (
    attempt_id             text PRIMARY KEY,
    eval_run_id            text NOT NULL,
    case_id                text NOT NULL,
    repeat_index           integer NOT NULL CHECK (repeat_index >= 0),
    llm_call_ref           text NOT NULL REFERENCES llm_call(llm_call_ref),
    recognition_json       jsonb NOT NULL,
    recognition_hash       text NOT NULL,
    recognized_primary     text NULL,
    status                 text NOT NULL,
    false_narrowing        boolean NOT NULL,
    technical_failure      boolean NOT NULL,
    abstained              boolean NOT NULL,
    input_tokens           bigint NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens          bigint NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    cost_amount            numeric NOT NULL DEFAULT 0 CHECK (cost_amount >= 0),
    created_at             timestamptz NOT NULL DEFAULT now(),
    UNIQUE (eval_run_id, case_id, repeat_index),
    UNIQUE (eval_run_id, llm_call_ref),
    FOREIGN KEY (eval_run_id, case_id)
        REFERENCES recognition_eval_case(eval_run_id, case_id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE recognition_eval_artifact (
    artifact_id          text PRIMARY KEY,
    eval_run_id          text NOT NULL UNIQUE REFERENCES recognition_eval_run(eval_run_id),
    result               text NOT NULL CHECK (result IN ('PASS', 'FAIL')),
    report_json          jsonb NOT NULL,
    content_hash         text NOT NULL,
    evaluated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION recognition_eval_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% records are write-once: % is not allowed', TG_TABLE_NAME, TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER recognition_eval_run_no_mutation
    BEFORE UPDATE OR DELETE ON recognition_eval_run
    FOR EACH ROW EXECUTE FUNCTION recognition_eval_write_once();
CREATE TRIGGER recognition_eval_case_no_mutation
    BEFORE UPDATE OR DELETE ON recognition_eval_case
    FOR EACH ROW EXECUTE FUNCTION recognition_eval_write_once();
CREATE TRIGGER recognition_eval_attempt_no_mutation
    BEFORE UPDATE OR DELETE ON recognition_eval_attempt
    FOR EACH ROW EXECUTE FUNCTION recognition_eval_write_once();
CREATE TRIGGER recognition_eval_artifact_no_mutation
    BEFORE UPDATE OR DELETE ON recognition_eval_artifact
    FOR EACH ROW EXECUTE FUNCTION recognition_eval_write_once();

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'featuregen_app') THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON recognition_eval_run FROM featuregen_app;
        REVOKE UPDATE, DELETE, TRUNCATE ON recognition_eval_case FROM featuregen_app;
        REVOKE UPDATE, DELETE, TRUNCATE ON recognition_eval_attempt FROM featuregen_app;
        REVOKE UPDATE, DELETE, TRUNCATE ON recognition_eval_artifact FROM featuregen_app;
    END IF;
END $$;

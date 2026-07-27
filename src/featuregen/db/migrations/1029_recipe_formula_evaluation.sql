-- Delivery R7: immutable inputs, attempts and result artifacts for the formula readiness gate.

CREATE TABLE recipe_formula_eval_run (
    eval_run_id                    text PRIMARY KEY,
    corpus_version                 text NOT NULL,
    corpus_content_hash            text NOT NULL,
    expectation_registry_hash      text NOT NULL,
    operation_grammar_version      integer NOT NULL,
    output_policy_version          integer NOT NULL,
    author_provider_contract_hash  text NOT NULL,
    critic_provider_contract_hash  text NOT NULL,
    provider                       text NOT NULL,
    model                          text NOT NULL,
    generation_controls            jsonb NOT NULL,
    code_commit                    text NOT NULL,
    shadow_window_start            timestamptz NOT NULL,
    shadow_window_end              timestamptz NOT NULL,
    shadow_generation_run_ids      jsonb NOT NULL CHECK (
        jsonb_typeof(shadow_generation_run_ids) = 'array'),
    token_budget                   bigint NOT NULL CHECK (token_budget > 0),
    cost_budget                    numeric NOT NULL CHECK (cost_budget >= 0),
    runner_kind                    text NOT NULL CHECK (
        runner_kind IN ('REAL_PROVIDER', 'FAKE_TEST')),
    created_by                     jsonb NOT NULL,
    created_at                     timestamptz NOT NULL DEFAULT now(),
    CHECK (shadow_window_end > shadow_window_start)
);

CREATE TABLE recipe_formula_eval_case (
    eval_run_id          text NOT NULL REFERENCES recipe_formula_eval_run(eval_run_id),
    case_id              text NOT NULL,
    recipe_id            text NOT NULL,
    case_kind            text NOT NULL CHECK (case_kind IN ('clean', 'adversarial')),
    case_input_json      jsonb NOT NULL,
    case_input_hash      text NOT NULL,
    expected_json        jsonb NOT NULL,
    expected_hash        text NOT NULL,
    repeat_group         text NULL,
    PRIMARY KEY (eval_run_id, case_id)
);

CREATE TABLE recipe_formula_eval_attempt (
    attempt_id                  text PRIMARY KEY,
    eval_run_id                 text NOT NULL,
    case_id                     text NOT NULL,
    repeat_index                integer NOT NULL CHECK (repeat_index >= 0),
    authoring_run_id            text NULL REFERENCES formula_authoring_run(authoring_run_id),
    author_dispatch_refs        jsonb NOT NULL CHECK (jsonb_typeof(author_dispatch_refs) = 'array'),
    critic_dispatch_refs        jsonb NOT NULL CHECK (jsonb_typeof(critic_dispatch_refs) = 'array'),
    llm_call_refs               jsonb NOT NULL CHECK (jsonb_typeof(llm_call_refs) = 'array'),
    disposition                 text NOT NULL,
    outcome_json                jsonb NOT NULL,
    outcome_hash                text NOT NULL,
    input_tokens                bigint NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens               bigint NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    cost_amount                 numeric NOT NULL DEFAULT 0 CHECK (cost_amount >= 0),
    created_at                  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (eval_run_id, case_id, repeat_index),
    FOREIGN KEY (eval_run_id, case_id)
        REFERENCES recipe_formula_eval_case(eval_run_id, case_id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE recipe_formula_eval_artifact (
    artifact_id          text PRIMARY KEY,
    eval_run_id          text NOT NULL UNIQUE REFERENCES recipe_formula_eval_run(eval_run_id),
    result               text NOT NULL CHECK (result IN ('PASS', 'FAIL')),
    report_json          jsonb NOT NULL,
    content_hash         text NOT NULL,
    evaluated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION recipe_formula_eval_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% records are write-once: % is not allowed', TG_TABLE_NAME, TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER recipe_formula_eval_run_no_mutation
    BEFORE UPDATE OR DELETE ON recipe_formula_eval_run
    FOR EACH ROW EXECUTE FUNCTION recipe_formula_eval_write_once();
CREATE TRIGGER recipe_formula_eval_case_no_mutation
    BEFORE UPDATE OR DELETE ON recipe_formula_eval_case
    FOR EACH ROW EXECUTE FUNCTION recipe_formula_eval_write_once();
CREATE TRIGGER recipe_formula_eval_attempt_no_mutation
    BEFORE UPDATE OR DELETE ON recipe_formula_eval_attempt
    FOR EACH ROW EXECUTE FUNCTION recipe_formula_eval_write_once();
CREATE TRIGGER recipe_formula_eval_artifact_no_mutation
    BEFORE UPDATE OR DELETE ON recipe_formula_eval_artifact
    FOR EACH ROW EXECUTE FUNCTION recipe_formula_eval_write_once();

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'featuregen_app') THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON recipe_formula_eval_run FROM featuregen_app;
        REVOKE UPDATE, DELETE, TRUNCATE ON recipe_formula_eval_case FROM featuregen_app;
        REVOKE UPDATE, DELETE, TRUNCATE ON recipe_formula_eval_attempt FROM featuregen_app;
        REVOKE UPDATE, DELETE, TRUNCATE ON recipe_formula_eval_artifact FROM featuregen_app;
    END IF;
END $$;

-- Delivery B: durable recipe-formula shadow population and checked delivery identity.
ALTER TABLE outbox ADD COLUMN IF NOT EXISTS payload_hash text NULL;
ALTER TABLE queue ADD COLUMN IF NOT EXISTS payload_hash text NULL;
ALTER TABLE queue ADD COLUMN IF NOT EXISTS lease_fence bigint NOT NULL DEFAULT 0;
ALTER TABLE llm_dispatch ADD COLUMN IF NOT EXISTS physical_request_hash text NULL;
ALTER TABLE llm_dispatch ADD COLUMN IF NOT EXISTS authoring_run_id text NULL
    REFERENCES formula_authoring_run(authoring_run_id);
ALTER TABLE llm_dispatch ADD COLUMN IF NOT EXISTS call_role text NULL;
ALTER TABLE llm_dispatch ADD COLUMN IF NOT EXISTS turn_index integer NULL;
ALTER TABLE llm_dispatch ADD COLUMN IF NOT EXISTS canonical_turn_input_hash text NULL;
ALTER TABLE llm_dispatch ADD COLUMN IF NOT EXISTS provider_contract_hash text NULL;
ALTER TABLE llm_dispatch ADD COLUMN IF NOT EXISTS prompt_content_hash text NULL;
ALTER TABLE llm_dispatch ADD COLUMN IF NOT EXISTS schema_content_hash text NULL;

CREATE TABLE IF NOT EXISTS recipe_formula_shadow_expected_run (
    generation_run_id            text PRIMARY KEY
                                      REFERENCES feature_generation_run(generation_run_id),
    intent_id                    text NOT NULL REFERENCES contract_intent(intent_id),
    confirmed_scope_id           text NOT NULL REFERENCES confirmed_generation_scope(scope_id),
    considered_revision_id       text NOT NULL
                                      REFERENCES contract_considered_revision(considered_revision_id),
    considered_content_hash      text NOT NULL,
    shadow_flag                  boolean NOT NULL,
    ranking_flag                 boolean NOT NULL,
    invocation_predicate_version text NOT NULL,
    capture_policy_version       text NOT NULL,
    expected_manifest_id         text NOT NULL UNIQUE,
    declaration_hash             text NOT NULL,
    declared_at                  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS recipe_formula_shadow_run_manifest (
    manifest_id                  text PRIMARY KEY,
    generation_run_id            text NOT NULL UNIQUE
                                      REFERENCES recipe_formula_shadow_expected_run(generation_run_id),
    intent_id                    text NOT NULL,
    considered_revision_id       text NOT NULL,
    considered_content_hash      text NOT NULL,
    ranking_version              text NULL,
    ranking_json                 jsonb NOT NULL CHECK (jsonb_typeof(ranking_json) = 'array'),
    ranking_hash                 text NOT NULL,
    capture_entries              jsonb NOT NULL CHECK (jsonb_typeof(capture_entries) = 'array'),
    expected_observation_count   integer NOT NULL CHECK (expected_observation_count >= 0),
    capture_axis                 text NOT NULL CHECK (capture_axis IN
                                      ('CAPTURED', 'SKIPPED_RANKING_DISABLED')),
    capture_policy_version       text NOT NULL,
    manifest_hash                text NOT NULL,
    status                       text NOT NULL DEFAULT 'OPEN'
                                      CHECK (status IN ('OPEN', 'COMPLETE', 'INCOMPLETE')),
    actual_observation_count     integer NULL CHECK (actual_observation_count >= 0),
    reconciliation_hash          text NULL,
    created_at                   timestamptz NOT NULL DEFAULT now(),
    terminal_at                  timestamptz NULL,
    CONSTRAINT recipe_formula_shadow_manifest_terminal_shape CHECK (
        (status = 'OPEN' AND actual_observation_count IS NULL
                         AND reconciliation_hash IS NULL AND terminal_at IS NULL)
        OR
        (status IN ('COMPLETE', 'INCOMPLETE') AND actual_observation_count IS NOT NULL
                                               AND reconciliation_hash IS NOT NULL
                                               AND terminal_at IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS recipe_formula_shadow_observation (
    observation_id               text PRIMARY KEY,
    idempotency_key              text NOT NULL UNIQUE,
    capture_entry_id             text NOT NULL UNIQUE,
    generation_run_id            text NOT NULL
                                      REFERENCES recipe_formula_shadow_expected_run(generation_run_id),
    intent_id                    text NOT NULL,
    considered_revision_id       text NOT NULL,
    considered_content_hash      text NOT NULL,
    metadata_snapshot_id         text NULL,
    metadata_snapshot_content_hash text NULL,
    recipe_id                    text NOT NULL,
    recipe_candidate_key         text NULL,
    recipe_expectation_json      jsonb NULL,
    recipe_expectation_hash      text NULL,
    binding_envelope_json        jsonb NULL,
    binding_envelope_hash        text NULL,
    provider_input_json          jsonb NULL,
    provider_input_hash          text NULL,
    frozen_configuration_json    jsonb NULL,
    frozen_configuration_hash    text NULL,
    request_identity_json        jsonb NULL,
    request_read_scope_hash      text NULL,
    capture_axis                 text NOT NULL CHECK (capture_axis IN
                                      ('CAPTURED','CAPTURE_INPUT_INCOMPLETE','BUDGET_TRUNCATED')),
    authorization_axis           text NOT NULL DEFAULT 'NOT_EVALUATED' CHECK (
                                      authorization_axis IN
                                      ('NOT_EVALUATED','AUTHORIZED_CURRENT',
                                       'AUTHORIZATION_REVOKED','AUTHORIZATION_UNVERIFIABLE',
                                       'AUTHORIZATION_SCOPE_CHANGED')),
    authority_axis               text NOT NULL DEFAULT 'NOT_EVALUATED',
    drift_axis                   text NOT NULL DEFAULT 'NOT_EVALUATED' CHECK (
                                      drift_axis IN
                                      ('NOT_EVALUATED','CURRENT','DRIFT_DRIFTED',
                                       'DRIFT_UNVERIFIABLE','AUTHORITY_DRIFTED')),
    configuration_axis           text NOT NULL DEFAULT 'NOT_EVALUATED' CHECK (
                                      configuration_axis IN
                                      ('NOT_EVALUATED','CURRENT','DRIFTED','UNVERIFIABLE')),
    delivery_axis                text NOT NULL DEFAULT 'NOT_ENQUEUED' CHECK (
                                      delivery_axis IN
                                      ('NOT_ENQUEUED','NOT_DISPATCHED','EGRESS_REJECTED',
                                       'DISPATCHED_AUDITED','PRIOR_DISPATCH_UNRECONCILED')),
    authoring_axis               text NOT NULL DEFAULT 'NOT_RUN' CHECK (
                                      authoring_axis IN
                                      ('NOT_RUN','RESOLVED','NEEDS_REVIEW','UNSUPPORTED',
                                       'REJECTED','TECHNICAL_FAILURE')),
    technical_axis               text NOT NULL DEFAULT 'OK',
    authoring_run_id             text NULL,
    authoring_result_json        jsonb NULL,
    payload_hash                 text NOT NULL,
    created_at                   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS recipe_formula_shadow_observation_run_idx
    ON recipe_formula_shadow_observation(generation_run_id);

CREATE TABLE IF NOT EXISTS recipe_formula_shadow_work_item (
    work_item_id                 text PRIMARY KEY,
    idempotency_key              text NOT NULL UNIQUE,
    capture_entry_id             text NOT NULL UNIQUE,
    generation_run_id            text NOT NULL
                                      REFERENCES recipe_formula_shadow_expected_run(generation_run_id),
    intent_id                    text NOT NULL,
    considered_revision_id       text NOT NULL,
    considered_content_hash      text NOT NULL,
    metadata_snapshot_id         text NULL,
    metadata_snapshot_content_hash text NULL,
    recipe_id                    text NOT NULL,
    recipe_candidate_key         text NOT NULL,
    recipe_expectation_json      jsonb NOT NULL,
    recipe_expectation_hash      text NOT NULL,
    binding_envelope_json        jsonb NOT NULL,
    binding_envelope_hash        text NOT NULL,
    provider_input_json          jsonb NOT NULL,
    provider_input_hash          text NOT NULL,
    frozen_configuration_json    jsonb NOT NULL,
    frozen_configuration_hash    text NOT NULL,
    request_identity_json        jsonb NOT NULL,
    request_read_scope_hash      text NOT NULL,
    payload_hash                 text NOT NULL,
    created_at                   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS recipe_formula_shadow_work_item_run_idx
    ON recipe_formula_shadow_work_item(generation_run_id);

CREATE OR REPLACE FUNCTION recipe_formula_shadow_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% records are write-once: % is not allowed', TG_TABLE_NAME, TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER recipe_formula_shadow_expected_no_mutation
    BEFORE UPDATE OR DELETE ON recipe_formula_shadow_expected_run
    FOR EACH ROW EXECUTE FUNCTION recipe_formula_shadow_write_once();

CREATE OR REPLACE TRIGGER recipe_formula_shadow_observation_no_mutation
    BEFORE UPDATE OR DELETE ON recipe_formula_shadow_observation
    FOR EACH ROW EXECUTE FUNCTION recipe_formula_shadow_write_once();

CREATE OR REPLACE TRIGGER recipe_formula_shadow_work_item_no_mutation
    BEFORE UPDATE OR DELETE ON recipe_formula_shadow_work_item
    FOR EACH ROW EXECUTE FUNCTION recipe_formula_shadow_write_once();

CREATE OR REPLACE FUNCTION recipe_formula_shadow_manifest_transition() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'recipe formula manifest records are write-once: DELETE is not allowed';
    END IF;
    IF OLD.status <> 'OPEN' OR NEW.status NOT IN ('COMPLETE', 'INCOMPLETE') THEN
        RAISE EXCEPTION 'invalid recipe formula manifest transition % -> %', OLD.status, NEW.status;
    END IF;
    IF (to_jsonb(NEW) - ARRAY['status','actual_observation_count','reconciliation_hash','terminal_at'])
       IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY['status','actual_observation_count','reconciliation_hash','terminal_at'])
    THEN
        RAISE EXCEPTION 'recipe formula manifest immutable fields cannot change';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER recipe_formula_shadow_manifest_guard
    BEFORE UPDATE OR DELETE ON recipe_formula_shadow_run_manifest
    FOR EACH ROW EXECUTE FUNCTION recipe_formula_shadow_manifest_transition();

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'featuregen_app') THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON recipe_formula_shadow_expected_run FROM featuregen_app;
        REVOKE UPDATE, DELETE, TRUNCATE ON recipe_formula_shadow_observation FROM featuregen_app;
        REVOKE UPDATE, DELETE, TRUNCATE ON recipe_formula_shadow_work_item FROM featuregen_app;
        REVOKE DELETE, TRUNCATE ON recipe_formula_shadow_run_manifest FROM featuregen_app;
    END IF;
END $$;

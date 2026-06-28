-- src/sp0/db/migrations/0060_aggregates_lifecycle.sql
-- Phase 06 owns these tables (declared in the shared contract; columns/constraints verbatim).

CREATE TABLE feature_versions (
    feature_version_id            text        PRIMARY KEY,        -- 'fv_...'
    feature_id                    text        NOT NULL,
    produced_by_run               text        NOT NULL,
    base_feature_version_id       text        NULL REFERENCES feature_versions(feature_version_id),
    verification_stamp            text        NOT NULL
                                      CHECK (verification_stamp IN ('DESIGN-CHECKED','DATA-CHECKED','USEFULNESS-CHECKED')),
    risk_tier                     text        NOT NULL,
    approval_type                 text        NOT NULL CHECK (approval_type IN ('EXPERIMENTAL','PRODUCTION')),
    approved_use_cases            text[]      NOT NULL DEFAULT '{}',
    blocked_use_cases             text[]      NOT NULL DEFAULT '{}',
    required_artifact_refs        jsonb       NOT NULL DEFAULT '{}',
    dsl_operation_catalog_version text        NULL,
    approval                      jsonb       NOT NULL DEFAULT '{}',
    expires_at                    timestamptz NULL,
    content_hash                  text        NOT NULL,
    immutable                     boolean     NOT NULL DEFAULT true,
    created_at                    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX feature_versions_feature_idx ON feature_versions (feature_id);
CREATE INDEX feature_versions_base_idx    ON feature_versions (base_feature_version_id);

-- Physical immutability (no UPDATE/DELETE) — mirrors Phase 02's documents write-once trigger.
-- The `immutable` boolean flag is a convention only; this trigger is the enforcement backstop.
CREATE OR REPLACE FUNCTION feature_versions_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'feature_versions are immutable: % not allowed on feature_version_id=%',
        TG_OP, COALESCE(OLD.feature_version_id, NEW.feature_version_id);
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER feature_versions_no_mutation
    BEFORE UPDATE OR DELETE ON feature_versions
    FOR EACH ROW EXECUTE FUNCTION feature_versions_write_once();

CREATE TABLE feature_active_versions (
    feature_id         text        NOT NULL,
    use_case           text        NOT NULL,
    feature_version_id text        NOT NULL REFERENCES feature_versions(feature_version_id),
    activation_state   text        NOT NULL
                           CHECK (activation_state IN ('ACTIVE_EXPERIMENTAL','PRODUCTION','DEPRECATED')),
    activated_seq      bigint      NOT NULL,
    activated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (feature_id, use_case)
);

CREATE TABLE consumers (
    consumer_id        text        PRIMARY KEY,
    feature_id         text        NOT NULL,
    feature_version_id text        NULL REFERENCES feature_versions(feature_version_id),
    consumer_kind      text        NOT NULL CHECK (consumer_kind IN ('model','feature')),
    consumer_ref       text        NOT NULL,
    edge_status        text        NOT NULL DEFAULT 'active' CHECK (edge_status IN ('active','deregistered')),
    registered_by      jsonb       NOT NULL,
    registered_at      timestamptz NOT NULL DEFAULT now(),
    deregistered_at    timestamptz NULL,
    UNIQUE (feature_id, consumer_kind, consumer_ref)
);
CREATE INDEX consumers_feature_active_idx ON consumers (feature_id) WHERE edge_status = 'active';

CREATE TABLE concept_claims (
    concept_key   text        PRIMARY KEY,
    request_id    text        NOT NULL,
    claimed_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE command_idempotency (
    idempotency_key   text        PRIMARY KEY,
    action            text        NOT NULL,
    result            jsonb       NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now()
);

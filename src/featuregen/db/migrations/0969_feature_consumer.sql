-- src/featuregen/db/migrations/0969_feature_consumer.sql
-- SP-14 model<->feature consumer registration: links a model/consumer to the features it uses. This is
-- the "which models consume this feature?" inventory that unblocks change-impact + deprecation scoping
-- (a pure metadata edge; no data plane). model_ref = a model-inventory ID; purpose + environment scope it.
CREATE TABLE IF NOT EXISTS feature_consumer (
    consumer_id   text        PRIMARY KEY,
    model_ref     text        NOT NULL,
    feature_id    text        NOT NULL REFERENCES feature (feature_id) ON DELETE CASCADE,
    purpose       text        NOT NULL DEFAULT '',
    environment   text        NOT NULL DEFAULT 'dev',
    actor         text        NOT NULL DEFAULT '',
    registered_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (model_ref, feature_id, environment)
);
CREATE INDEX IF NOT EXISTS feature_consumer_feature_idx ON feature_consumer (feature_id);
CREATE INDEX IF NOT EXISTS feature_consumer_model_idx   ON feature_consumer (model_ref);

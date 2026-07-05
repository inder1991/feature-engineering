-- src/featuregen/db/migrations/0960_contract.sql
-- Phase-5 governed feature contract: the human-confirmed, versioned definition. It points at a
-- registered feature (feature/feature_derives_from), so freshness lineage + drift impact apply — a
-- contract KNOWS when its inputs drifted. A re-confirm of the same feature is a new version; history stays.
CREATE TABLE IF NOT EXISTS contract (
    contract_id  text        PRIMARY KEY,
    feature_id   text        NOT NULL,     -- the registered feature this contract governs
    feature_name text        NOT NULL,
    definition   text        NOT NULL DEFAULT '',
    version      int         NOT NULL,
    actor        jsonb       NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS contract_feature_name_idx ON contract (feature_name);
CREATE INDEX IF NOT EXISTS contract_feature_id_idx   ON contract (feature_id);

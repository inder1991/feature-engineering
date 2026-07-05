-- src/featuregen/db/migrations/0958_feature_layer.sql
-- Phase-2 feature layer: a registered feature and the catalog columns it derives from. This is the
-- "feature source" (S1) the phase-2 assist was gated on — features enter here, and the graph's
-- derives-from edges (feature_derives_from) power freshness lineage and drift impact.
CREATE TABLE IF NOT EXISTS feature (
    feature_id    text        PRIMARY KEY,
    name          text        NOT NULL,
    description   text        NOT NULL DEFAULT '',
    grain_table   text        NULL,      -- the entity/table the feature is computed at
    aggregation   text        NULL,      -- e.g. avg_90d, count, sum
    as_of_column  text        NULL,      -- the point-in-time column used
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS feature_derives_from (
    feature_id     text NOT NULL REFERENCES feature (feature_id) ON DELETE CASCADE,
    catalog_source text NOT NULL,
    object_ref     text NOT NULL,        -- the source column the feature reads
    PRIMARY KEY (feature_id, catalog_source, object_ref)
);
CREATE INDEX IF NOT EXISTS feature_derives_from_col_idx
    ON feature_derives_from (catalog_source, object_ref);   -- reverse: which features use a column

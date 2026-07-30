-- 1036 — immutable revisions of logical-catalog to physical-dataset bindings.
--
-- The platform's bridge/fact/dependency namespace remains public.<table>[.<column>]. A binding
-- revision is the separate, content-addressed statement of which environment, schema, table,
-- connection and layout declaration an executor actually used. Observations and directional join
-- realizations reference this revision; changing the physical address never re-keys the logical
-- bridge fact.
CREATE TABLE IF NOT EXISTS physical_dataset_binding_revision (
    binding_revision_id text        PRIMARY KEY,
    binding_id          text        NOT NULL,
    content_hash        text        NOT NULL,
    catalog_logical_ref text        NOT NULL,
    connection_id       text        NOT NULL,
    physical_id         text        NOT NULL,
    binding_json        jsonb       NOT NULL,
    recorded_by         text        NULL,
    recorded_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT physical_binding_revision_id_ck
        CHECK (binding_revision_id ~ '^pbr_[0-9a-f]{64}$'),
    CONSTRAINT physical_binding_content_hash_ck
        CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT physical_binding_revision_content_uq
        UNIQUE (binding_id, content_hash)
);

CREATE INDEX IF NOT EXISTS physical_binding_logical_ref_idx
    ON physical_dataset_binding_revision (catalog_logical_ref, recorded_at DESC);

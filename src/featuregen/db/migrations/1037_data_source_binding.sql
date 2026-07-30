-- src/featuregen/db/migrations/1037_data_source_binding.sql
--
-- Where a catalog source physically LIVES, and what may read it.
--
-- Nothing in src/ has ever constructed a PhysicalDatasetBindingV1 — only tests — because the
-- information to build one does not exist anywhere. `PhysicalObjectIdentityV1` requires a database;
-- the catalog records `schema_name` and no database, and there was no connection registry at all.
-- So the data agent could compile a plan and never address a table, and `ExecutionInputs` could not
-- be assembled by any caller. These two tables are that missing configuration.
--
-- WHY TWO TABLES. A connection is an ACCESS grant — a host, a principal, a schema allowlist — and is
-- reviewed as one. A binding is an ADDRESS that names a connection. Folding them together would mean
-- re-approving the access grant every time a table is bound, and would lose the property
-- `authorize_binding` depends on: only the CONNECTION knows which schemas were approved, so a
-- binding can never widen its own reach.
--
-- NO CREDENTIAL IS STORED. `secret_ref` is a reference resolved at the point of use
-- (`open_connection(secret_resolver=...)`), so this table can be read, dumped and reviewed without
-- exposing anything. A password column here would put the credential in every backup of the
-- catalog.

CREATE TABLE IF NOT EXISTS data_source_connection (
    connection_id       text PRIMARY KEY,
    environment_id      text        NOT NULL,
    kind                text        NOT NULL,      -- postgres | hive (connection._ENGINES)
    host                text        NOT NULL,
    port                integer     NOT NULL,
    auth_mechanism      text        NOT NULL,
    secret_ref          text        NOT NULL,      -- a REFERENCE; never the secret
    execution_principal text        NOT NULL,      -- identity-bearing: two profiles read under
                                                   -- different principals are not interchangeable
    allowed_schemas     text[]      NOT NULL DEFAULT '{}',
    active              boolean     NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS physical_dataset_binding (
    binding_id          text PRIMARY KEY,
    catalog_source      text        NOT NULL,
    catalog_logical_ref text        NOT NULL,
    connection_id       text        NOT NULL REFERENCES data_source_connection(connection_id),
    database_name       text        NOT NULL,      -- the component the catalog does not carry
    schema_name         text        NOT NULL,
    table_name          text        NOT NULL,
    object_kind         text        NOT NULL DEFAULT 'table',
    created_at          timestamptz NOT NULL DEFAULT now()
);

-- One binding per physical table per source. A second binding for the same table would let two
-- callers address it through different connections — i.e. under different principals and different
-- allowlists — and which one applied would depend on read order.
CREATE UNIQUE INDEX IF NOT EXISTS physical_dataset_binding_identity_idx
    ON physical_dataset_binding (catalog_source, lower(schema_name), lower(table_name));

-- Resolution is by (source, table): a plan names `ftr::tran_repos.cif_id`, and the schema is not in
-- the logical ref, so lookup cannot require it.
CREATE INDEX IF NOT EXISTS physical_dataset_binding_lookup_idx
    ON physical_dataset_binding (catalog_source, lower(table_name));

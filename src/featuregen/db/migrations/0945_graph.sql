-- src/featuregen/db/migrations/0945_graph.sql
-- Graph + search slice: table/column nodes and contains edges, rebuilt per catalog_source at the
-- end of ingest from the canonical rows. search_doc is a weighted tsvector (column name > definition
-- > table) driving ranked full-text search. Deterministic; no LLM. Join edges (approved_join) and
-- concept/domain enrichment are later increments.
CREATE TABLE IF NOT EXISTS graph_node (
    catalog_source text     NOT NULL,
    object_ref     text     NOT NULL,
    kind           text     NOT NULL,           -- 'table' | 'column'
    table_name     text     NOT NULL,
    column_name    text     NULL,
    data_type      text     NULL,
    definition     text     NULL,
    is_grain       boolean  NOT NULL DEFAULT false,
    is_as_of       boolean  NOT NULL DEFAULT false,
    search_doc     tsvector NULL,
    PRIMARY KEY (catalog_source, object_ref)
);
CREATE INDEX IF NOT EXISTS graph_node_search_idx ON graph_node USING GIN (search_doc);

CREATE TABLE IF NOT EXISTS graph_edge (
    catalog_source text NOT NULL,
    kind           text NOT NULL,               -- 'contains'
    from_ref       text NOT NULL,
    to_ref         text NOT NULL,
    PRIMARY KEY (catalog_source, kind, from_ref, to_ref)
);

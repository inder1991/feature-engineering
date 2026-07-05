-- src/featuregen/db/migrations/0967_entity_suggestion.sql
-- Advisory entity-resolution suggestions: the LLM proposes which business entity an id-like column
-- denotes (Customer, Account, ...); a human confirms before it becomes the column's entity (a wrong
-- entity mis-links catalogs). 'applied' suggestions are re-applied by build_graph so a confirmed tag
-- survives re-upload (the upload itself may never declare it).
CREATE TABLE IF NOT EXISTS entity_suggestion (
    catalog_source   text        NOT NULL,
    object_ref       text        NOT NULL,
    table_name       text        NOT NULL,
    column_name      text        NOT NULL,
    suggested_entity text        NOT NULL,
    status           text        NOT NULL DEFAULT 'pending',   -- pending | applied | dismissed
    actor            jsonb,
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (catalog_source, object_ref)
);

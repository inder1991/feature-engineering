-- First-release hardening #3, the deferred provenance piece: per-object run provenance. The 0994
-- manifest records COUNTS + fingerprints; these two READ-MODEL association tables record WHICH run
-- observed/changed a catalog object and WHICH run asserted/changed an overlay fact, so a reviewer
-- can ask "which runs touched this object/fact" (index on object_ref / fact_key) and "what did
-- this run touch" (the UNIQUE constraints lead on ingestion_run_id, so no second run-id index is
-- needed). Written batched, ON CONFLICT DO NOTHING, on the ingest's own connection — the rows
-- commit atomically with the ingest they describe; a provenance-write failure is contained by the
-- writer (fail-soft) and never aborts the ingest.
--   relation vocabularies are CLOSED by CHECK: an object is 'observed' (the run saw it in the
--   upload) or 'changed' (the run's drift diff retired it: drop / type_change / rename); a fact is
--   'asserted' (the run (re)asserted it) or 'changed' (the assertion changed its value). Both
--   relations may hold for one (run, ref) — the UNIQUE key includes relation.
--   DELIBERATELY not the reserved overlay-event `run_id` column (overlay fact events require
--   run_id IS NULL): provenance keys on a dedicated ingestion_run_id FK to the 0994 manifest.
CREATE TABLE IF NOT EXISTS ingestion_run_object (
    id               bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ingestion_run_id text        NOT NULL REFERENCES ingestion_run(id),
    catalog_source   text        NOT NULL,
    object_ref       text        NOT NULL,
    relation         text        NOT NULL CHECK (relation IN ('observed', 'changed')),
    at               timestamptz NOT NULL,
    CONSTRAINT ingestion_run_object_unique UNIQUE (ingestion_run_id, object_ref, relation)
);
CREATE INDEX IF NOT EXISTS ingestion_run_object_ref_idx
    ON ingestion_run_object (object_ref);

CREATE TABLE IF NOT EXISTS ingestion_run_fact (
    id               bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ingestion_run_id text        NOT NULL REFERENCES ingestion_run(id),
    fact_key         text        NOT NULL,
    relation         text        NOT NULL CHECK (relation IN ('asserted', 'changed')),
    at               timestamptz NOT NULL,
    CONSTRAINT ingestion_run_fact_unique UNIQUE (ingestion_run_id, fact_key, relation)
);
CREATE INDEX IF NOT EXISTS ingestion_run_fact_key_idx
    ON ingestion_run_fact (fact_key);

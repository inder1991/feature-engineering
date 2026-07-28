-- 1033 — durable storage for bounded data observations (Release 1 step 6).
--
-- The first evidence in this system derived from REAL DATA rather than from a metadata file. Until
-- now every fact about a column came from what someone declared; an observation is what the data
-- actually contains.
--
-- IMMUTABLE VERSIONS, SEPARATE CURRENT POINTER. A new observation never overwrites an older one:
-- profiles are dated evidence, and a later partial run must not be able to erase an earlier
-- complete one. `latest` is derived by ordering, not by mutation — the same shape `field_evidence`
-- uses, and the reason a re-profile can never silently retract what a previous profile proved.
--
-- WHAT IS DELIBERATELY NOT STORED: rows, and any value beyond the bounds a plan explicitly opted
-- into. `MIN(cif_id)` is a customer identifier, so bounds are opt-in per column upstream and only
-- what was opted into can arrive here.
--
-- PROVENANCE IS PART OF THE EVIDENCE, not operational detail:
--   * `method` (exact|approximate) decides what the evidence can SUPPORT. A sampled profile that
--     finds a duplicate DISPROVES uniqueness; one that finds none proves nothing.
--   * `complete` plus `failures` keep a partial observation from reading as a whole one.
--   * `partitions_read` states what was actually covered — empty means an unpartitioned table, and
--     is never shorthand for "everything".
--   * `execution_principal` decides what the read could SEE. Two profiles of one table taken under
--     different principals are not interchangeable.

CREATE TABLE IF NOT EXISTS data_observation (
    observation_id        text        PRIMARY KEY,
    physical_id           text        NOT NULL,   -- source::database::schema::table
    catalog_source        text        NOT NULL,
    connection_id         text        NOT NULL,
    execution_principal   text        NOT NULL,
    dialect               text        NOT NULL,   -- hive | postgres
    row_count             bigint      NOT NULL,
    method                text        NOT NULL,
    complete              boolean     NOT NULL,
    partitions_read       text[]      NOT NULL DEFAULT '{}',
    failures              text[]      NOT NULL DEFAULT '{}',
    observed_at           timestamptz NOT NULL,
    CONSTRAINT data_observation_method_ck CHECK (method IN ('exact', 'approximate')),
    CONSTRAINT data_observation_rows_ck   CHECK (row_count >= 0)
);

CREATE INDEX IF NOT EXISTS data_observation_current_idx
    ON data_observation (physical_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS data_observation_column (
    observation_id        text        NOT NULL REFERENCES data_observation (observation_id),
    column_name           text        NOT NULL,
    non_null_count        bigint      NOT NULL,
    distinct_count        bigint      NOT NULL,
    observed_rows         bigint      NOT NULL,
    -- NULL unless the plan opted this column into value bounds. A bound is a real VALUE.
    minimum               text        NULL,
    maximum               text        NULL,
    PRIMARY KEY (observation_id, column_name),
    CONSTRAINT data_observation_column_counts_ck
        CHECK (non_null_count >= 0 AND distinct_count >= 0 AND non_null_count <= observed_rows)
);

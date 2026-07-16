-- src/featuregen/db/migrations/0991_file_declared_join.sql
-- Governed-join drift detection needs to raise divergences ONLY for FILE-DECLARED joins (a row's
-- `joins_to`), NEVER for a Pass-C-DISCOVERED join (proposed from upload metadata alone, never in
-- any file). Neither the approved_join fact payload, the graph_edge columns, nor the
-- `pass_c_candidate_evidence` ledger carries a DURABLE origin signal: the fact/edge are identical
-- for both paths, and the Pass-C ledger is DELETE-then-rewritten every ingest cycle
-- (ingest.py `_run_pass_c`), so a Pass-C join not re-discovered this cycle (exactly the drop
-- scenario) loses its row — it cannot mark provenance for a join that is no longer produced.
--
-- This is that missing durable marker: one row per (catalog_source, unordered column-ref pair) the
-- moment a FILE declares the join (`joins_to`). The drift detector records a marker for every
-- currently-declared join and considers a VERIFIED edge for divergence ONLY when its unordered
-- pair has a marker — so a Pass-C-discovered VERIFIED join (no marker) is never a false 'dropped'.
--
--   from_ref/to_ref   the column-ref pair (public.{table}.{column}), stored SORTED so the same pair
--                     lands on one row regardless of the file's authoring direction (mirrors
--                     pass_c_candidate_evidence).
--   declared_at       when the file last declared this join (upsert-refreshed; audit only).
--
-- Additive, monotonic (never deleted here — a stale marker is harmless: it only gates whether a
-- VERIFIED edge, itself fact-derived, is drift-checked). Flag-off byte-for-byte: only the governed
-- drift seam writes/reads it.
CREATE TABLE IF NOT EXISTS file_declared_join (
    catalog_source text        NOT NULL,
    from_ref       text        NOT NULL,
    to_ref         text        NOT NULL,
    declared_at    timestamptz NOT NULL,
    PRIMARY KEY (catalog_source, from_ref, to_ref)
);

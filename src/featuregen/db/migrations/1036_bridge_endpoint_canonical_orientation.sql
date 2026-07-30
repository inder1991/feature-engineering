-- src/featuregen/db/migrations/1036_bridge_endpoint_canonical_orientation.sql
-- Converge cross-catalog bridge rows onto ONE endpoint orientation.
--
-- A bridge is an UNORDERED pair: (left, right) and (right, left) denote the same bridge, and
-- `fact_key` has always canonicalized the endpoints. `entity_bridge_candidate_evidence`'s PRIMARY
-- KEY is the ORDERED five-tuple, so a bridge written with its endpoints swapped landed on a SECOND
-- row under the SAME fact_key — observed live as one bridge with two contradictory rows
-- (text/attested against uuid/declared). The write side no longer produces that shape; this
-- converges the rows already stored.
--
-- HYGIENE, NOT CORRECTNESS. `ledger_evidence_by_fact_key` merges per fact_key at READ time, in the
-- canonical orientation, so the read model is already right whether or not this ran, and stays
-- right if some row here is left alone. What this buys is a ledger whose PRIMARY KEY is reachable:
-- a stranded non-canonical row can never be updated again by `bridge_propose`'s ON CONFLICT (which
-- now writes canonical endpoints), so its fact_key and proposed_event_id would go stale for good.
--
-- ABORT-PROOF ON LEGACY DATA, deliberately — this runs against databases nobody has inspected:
--   * no casts that can fail on unexpected JSON (`->` compared to a jsonb literal, never `::bool`);
--   * a non-object evidence_json is left untouched rather than concatenated (`||` would error);
--   * the in-place swap is skipped when the target PK already exists, so it can never raise a
--     unique violation. A row it declines to move is simply merged by the reader instead.
--
-- The canonical order is decided by (lower(catalog_source), lower(object_ref)). A legal bridge has
-- two DISTINCT catalog sources (the table's own CHECK), so in practice the source alone decides —
-- the same first component the application's ordering rule uses.

-- 1. Fold each NON-CANONICAL row into its canonical twin where both exist, then drop the twin.
--    Merge rules mirror the read model exactly: grain flags OR (read in the canonical orientation,
--    so the twin's RIGHT flag is this row's LEFT), the WEAKEST type_basis (a contradicted
--    "attested" must rank the link down, never up), and the lexically-first data_type_family.
WITH pair AS (
    SELECT c.entity_id,
           c.left_catalog_source, c.left_object_ref,
           c.right_catalog_source, c.right_object_ref,
           t.left_catalog_source  AS twin_left_catalog_source,
           t.left_object_ref      AS twin_left_object_ref,
           t.right_catalog_source AS twin_right_catalog_source,
           t.right_object_ref     AS twin_right_object_ref,
           COALESCE(c.evidence_json -> 'left_is_grain'  = 'true'::jsonb, false)
        OR COALESCE(t.evidence_json -> 'right_is_grain' = 'true'::jsonb, false) AS left_is_grain,
           COALESCE(c.evidence_json -> 'right_is_grain' = 'true'::jsonb, false)
        OR COALESCE(t.evidence_json -> 'left_is_grain'  = 'true'::jsonb, false) AS right_is_grain,
           LEAST(c.data_type_family, t.data_type_family) AS data_type_family,
           (SELECT b FROM (VALUES (COALESCE(c.evidence_json ->> 'type_basis', '')),
                                  (COALESCE(t.evidence_json ->> 'type_basis', ''))) AS v(b)
             ORDER BY CASE b WHEN 'attested' THEN 2 WHEN 'declared' THEN 1 ELSE 0 END, b
             LIMIT 1) AS type_basis
      FROM entity_bridge_candidate_evidence c
      JOIN entity_bridge_candidate_evidence t
        ON t.entity_id = c.entity_id
       AND lower(t.left_catalog_source)  = lower(c.right_catalog_source)
       AND lower(t.left_object_ref)      = lower(c.right_object_ref)
       AND lower(t.right_catalog_source) = lower(c.left_catalog_source)
       AND lower(t.right_object_ref)     = lower(c.left_object_ref)
     WHERE (lower(c.left_catalog_source), lower(c.left_object_ref))
         < (lower(c.right_catalog_source), lower(c.right_object_ref))
),
merged AS (
    UPDATE entity_bridge_candidate_evidence c
       SET data_type_family = p.data_type_family,
           evidence_json = CASE WHEN jsonb_typeof(c.evidence_json) = 'object'
                THEN c.evidence_json || jsonb_build_object(
                         'left_is_grain',  p.left_is_grain,
                         'right_is_grain', p.right_is_grain,
                         'type_basis',     p.type_basis)
                ELSE c.evidence_json END,
           updated_at = now()
      FROM pair p
     WHERE c.entity_id            = p.entity_id
       AND c.left_catalog_source  = p.left_catalog_source
       AND c.left_object_ref      = p.left_object_ref
       AND c.right_catalog_source = p.right_catalog_source
       AND c.right_object_ref     = p.right_object_ref
    RETURNING 1
)
DELETE FROM entity_bridge_candidate_evidence d
      USING pair p
      WHERE d.entity_id            = p.entity_id
        AND d.left_catalog_source  = p.twin_left_catalog_source
        AND d.left_object_ref      = p.twin_left_object_ref
        AND d.right_catalog_source = p.twin_right_catalog_source
        AND d.right_object_ref     = p.twin_right_object_ref;

-- 2. Re-orient the non-canonical rows that had no twin. Every SET expression reads the OLD row, so
--    these four assignments are a swap. The NOT EXISTS guard keeps the statement from ever raising
--    a unique violation on a case-variant twin step 1 matched but the PK does not.
UPDATE entity_bridge_candidate_evidence c
   SET left_catalog_source  = c.right_catalog_source,
       left_object_ref      = c.right_object_ref,
       right_catalog_source = c.left_catalog_source,
       right_object_ref     = c.left_object_ref,
       evidence_json = CASE WHEN jsonb_typeof(c.evidence_json) = 'object'
            THEN c.evidence_json || jsonb_build_object(
                     'left_is_grain',  COALESCE(c.evidence_json -> 'right_is_grain', 'false'::jsonb),
                     'right_is_grain', COALESCE(c.evidence_json -> 'left_is_grain',  'false'::jsonb))
            ELSE c.evidence_json END
 WHERE (lower(c.left_catalog_source), lower(c.left_object_ref))
     > (lower(c.right_catalog_source), lower(c.right_object_ref))
   AND NOT EXISTS (
        SELECT 1 FROM entity_bridge_candidate_evidence x
         WHERE x.entity_id            = c.entity_id
           AND x.left_catalog_source  = c.right_catalog_source
           AND x.left_object_ref      = c.right_object_ref
           AND x.right_catalog_source = c.left_catalog_source
           AND x.right_object_ref     = c.left_object_ref);

-- 3. The VERIFIED projection. Keyed by fact_key, so it never held a duplicate — but it could hold
--    the OTHER orientation of the same bridge, which is how `entity_bridge_edge` and the ledger came
--    to describe one link with two shapes. No per-side flags live here.
UPDATE entity_bridge_edge
   SET left_catalog_source  = right_catalog_source,
       left_object_ref      = right_object_ref,
       right_catalog_source = left_catalog_source,
       right_object_ref     = left_object_ref
 WHERE (lower(left_catalog_source), lower(left_object_ref))
     > (lower(right_catalog_source), lower(right_object_ref));

-- src/featuregen/db/migrations/0990_governed_join_divergence.sql
-- Governed-join drift detection: when a re-uploaded source RETARGETS or DROPS a `joins_to` that
-- humans already VERIFIED as an approved_join, ingest records ONE advisory divergence row here
-- (overlay/upload/join_drift.py). ADVISORY ONLY — detection NEVER changes the VERIFIED fact/edge
-- (no auto-demote); the old join stays operational until a human acts on it.
--
--   kind                 'retargeted' (the upload declares a DIFFERENT target for the verified
--                        from-column) | 'dropped' (the upload no longer declares any join on it).
--   declared_to_ref      the upload's declared target (public.{table}.{column}); NULL for 'dropped'.
--   source_snapshot_id   the ingestion-run id that detected it (NULL for non-glossary uploads).
--   acknowledged_*       a platform-admin's "seen it" — acknowledging hides the row from the open
--                        list; a FRESH detection re-opens it (acknowledged_* reset to NULL).
--
-- UNIQUE (catalog_source, from_ref, verified_to_ref): a re-upload REFRESHES the same divergence
-- in place — never a duplicate per upload. The partial index serves the open-list query.
CREATE TABLE IF NOT EXISTS governed_join_divergence (
    id                 bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    catalog_source     text        NOT NULL,
    from_ref           text        NOT NULL,
    verified_to_ref    text        NOT NULL,
    declared_to_ref    text        NULL,
    kind               text        NOT NULL CHECK (kind IN ('retargeted', 'dropped')),
    source_snapshot_id text        NULL,
    detected_at        timestamptz NOT NULL,
    acknowledged_at    timestamptz NULL,
    acknowledged_by    text        NULL,
    UNIQUE (catalog_source, from_ref, verified_to_ref)
);
CREATE INDEX IF NOT EXISTS governed_join_divergence_open_idx
    ON governed_join_divergence (catalog_source) WHERE acknowledged_at IS NULL;

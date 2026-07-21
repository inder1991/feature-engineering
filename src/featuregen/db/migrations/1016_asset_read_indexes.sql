-- 1016_asset_read_indexes.sql — composite indexes for the asset read model's hot paths (Delivery F).
--
-- Most of the plan's F2 index targets already exist: reverse run history uses
-- ``ingestion_run_object_source_ref_at_idx`` (functional, lower(object_ref)); the semantic sets/edges
-- carry their own PK/endpoint indexes from 1014/1015; field_evidence is indexed by (logical_ref,
-- field_name); feature reverse lineage rides existing keys. The one GENUINELY new hot path is the
-- reverse SUBJECT lookup the F2-audit LLM-audit-summaries subsection introduces: "which dispatches
-- touched THIS (catalog_source, object_ref / logical_ref)?" — 1005 only indexed
-- llm_dispatch_subject(dispatch_ref) (the forward join), so the reverse read would seq-scan.
--
-- Additive + idempotent (CREATE INDEX IF NOT EXISTS); no data change.

CREATE INDEX IF NOT EXISTS llm_dispatch_subject_object_idx
    ON llm_dispatch_subject (catalog_source, object_ref);

CREATE INDEX IF NOT EXISTS llm_dispatch_subject_logical_idx
    ON llm_dispatch_subject (catalog_source, logical_ref);

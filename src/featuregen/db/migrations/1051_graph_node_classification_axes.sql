-- src/featuregen/db/migrations/1051_graph_node_classification_axes.sql
-- Joint enrichment step (verified-interfaces doc D7 reservation 1051; product decisions D13.1/D13.2).
--
-- Three COLUMN-node display-projection columns for the fine-grained classification axes:
--
--   * bian_path / process_path — already captured as SOURCE `field_evidence` by the FTR glossary
--     reader (`ingest._SOURCE_FIELDS`), but with no field policy and no flat column they could
--     never reach `graph_node`, so the facet mechanism (which requires literal columns) and every
--     flat reader were blind to them. This migration only adds the columns; the resolution +
--     projection wiring lives in `field_policies` / `field_resolution`.
--   * sub_domain — the D13.2 LLM-proposed FINER axis beside the coarse source `domain`. Recommend-
--     ation tier exactly like `domain`: visible, labeled `llm_proposed`, human-editable through the
--     existing four-eyes flow, NEVER load-bearing and NEVER overwriting `domain`.
--
-- Rebuildable, NEVER authoritative — the same contract as every other projected display column
-- (0984/1031/1040/1042/1047): `build_graph` recreates `graph_node` with these NULL on every upload
-- and `resolve_and_project` re-derives them from the surviving evidence, so a lost column is a
-- re-projection away and no operational read may consult them (authority stays in the decision log).
--
-- No CHECK constraint on any of the three: unlike `authority_role`/`temporal_storage_model` (closed
-- vocabularies, migration 1047) these are OPEN vocabularies — a BIAN/process path is the source's
-- own " > "-joined hierarchy, and D13.2 deliberately defers a curated sub-domain list ("sub_domain
-- v1 is free-text-constrained-by-prompt like `domain`"). A CHECK here would reject legitimate
-- source data; the bounds that DO apply are enforced in code before persistence (the accept gates
-- in `enrich`, the per-value egress caps in `enrich_llm`).

ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS bian_path    text NULL;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS process_path text NULL;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS sub_domain   text NULL;

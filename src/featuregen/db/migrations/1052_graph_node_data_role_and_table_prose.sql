-- src/featuregen/db/migrations/1052_graph_node_data_role_and_table_prose.sql
-- Release-A consumption step (verified-interfaces doc D7 reservation 1052 — the ONLY number this
-- stream owns). Two additions, both required by SEARCH, both rebuildable display projections:
--
--   * data_role — the DERIVED display classification of a table (profile §6.1 / correction 4). It
--     is NOT a second evidence field: `field_resolution` re-derives it from the node's OWN
--     projected `table_role` (+ `event_or_snapshot`) through the ONE adapter
--     `profile_vocab.data_role_from_table_role`, so legacy canonical `table_role='bridge'` displays
--     as `crosswalk` without one stored evidence row being re-keyed. The literal column exists
--     because the facet mechanism reads literal `graph_node` columns and nothing else
--     (`search.py::_COLUMN_FACETS`); a read-time CASE cannot be faceted, counted or filtered by the
--     existing query family. A rebuildable projection is NOT the duplicate store correction 4
--     forbids — the same contract as `sensitivity_display` (1042), which likewise projects a value
--     derived from data the row already carries.
--
--   * business_context — the table-narrative prose field (profile plan Task 1) had a registered
--     field policy and real `field_evidence` rows, but NO flat column, so it could never reach
--     `graph_node.search_doc`. The table FTS slot for `definition` was hardcoded blank as well
--     (`graph._search_doc_params`), which made the ONE piece of prose a technical catalog has about
--     a table unmatchable: a read-time join cannot reach FTS matching, so the text must be IN the
--     document. `1052` adds the column; `graph._SEARCH_DOC` gains its slot and
--     `rebuild_search_doc` re-derives it with insert-time parity (the single-expression invariant).
--     `business_context_decision_id` is its display-not-authority link, exactly like every other
--     resolved field (0984/1031/1047 pattern).
--
-- Rebuildable, NEVER authoritative: `build_graph` recreates `graph_node` with both NULL on every
-- upload and the unconditional `table_display_reprojection` stage re-derives them from the
-- surviving evidence. No operational read may consult either column — authority stays in the
-- decision log / the assembled `DatasetSemanticProfileV1`.
--
-- CHECK on `data_role` only: it is a CLOSED vocabulary (profile_vocab.DataRole, all seven members,
-- including the legacy canonical `fact` a role with no event/snapshot signal keeps). Adding a
-- member here is therefore a deliberate migration, not a silent widening. `business_context` is
-- free prose bounded in code before persistence (field_correction._MAX_LEN / the asset-profile
-- route's 4000-char bound), which is where every other prose bound lives.

ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS data_role                   text NULL;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS business_context            text NULL;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS business_context_decision_id text NULL;

ALTER TABLE graph_node DROP CONSTRAINT IF EXISTS graph_node_data_role_check;
ALTER TABLE graph_node ADD CONSTRAINT graph_node_data_role_check
    CHECK (data_role IS NULL OR data_role IN
           ('event_fact', 'snapshot_fact', 'fact', 'dimension', 'reference', 'crosswalk',
            'unknown'));

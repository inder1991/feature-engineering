-- src/featuregen/db/migrations/1031_graph_node_measure_annotation_links.sql
-- E4a T3 — close the unit loop: the AI proposes a unit, a HUMAN confirms it, and the confirmation
-- must reach `graph_node.unit`/`.currency` (the only columns the feature gauntlet's
-- `_column_meta` reads) for the UNIT_CONSISTENT / CURRENCY_CONSISTENT requirement to clear.
--
-- That makes `unit`/`currency` PROJECTED display fields, so — exactly like every other projected
-- field since 0984 — each needs its companion `*_decision_id` link back to the
-- field_decision_event that authored the displayed value. The display ≠ authority boundary is
-- unchanged: operational code reads the DECISION (is_feature_eligible), never the flat column.
--
-- The link is ALSO load-bearing for the PROJECTION-WIPE guard in field_resolution._project_display:
-- `build_graph` populates these two flat columns straight from the uploaded file, so a NULL link
-- means "the file wrote this, the resolver did not". The resolver may therefore never NULL a value
-- it did not author — it clears only where its own link is already set.
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS unit_decision_id     text NULL;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS currency_decision_id text NULL;

-- src/featuregen/db/migrations/0987_graph_edge_join_authority_links.sql
-- Phase 3A Task 8 (Pass C): link an operational `joins` graph_edge to the VERIFIED approved_join
-- fact that authorized it. The reverse projector (overlay/upload/passc/projection.py) stamps these
-- when it projects a confirmed fact into an edge; the async demotion hook (reject_fact /
-- fire_due_overlay_expiries) flips them the moment the fact leaves VERIFIED, so a rejected or
-- expired join stops traversing immediately (not at the next upload).
--
--   approved_join_fact_key   which overlay fact stream authorized this edge (NULL = file-declared).
--   approved_join_event_id   the OVERLAY_FACT_CONFIRMED event id — audit link to the confirmation.
--   approved_join_status     the fact's folded status as last synced; feature-construction readers
--                            traverse a LINKED edge only when 'VERIFIED' (governed edge filter).
--                            An UNLINKED edge (fact_key NULL) is untouched by the filter, so a
--                            flag-off declared catalog stays byte-for-byte.
--   authority_updated_at     when the authority/link columns last transitioned (audit).
ALTER TABLE graph_edge ADD COLUMN IF NOT EXISTS approved_join_fact_key text;
ALTER TABLE graph_edge ADD COLUMN IF NOT EXISTS approved_join_event_id text;
ALTER TABLE graph_edge ADD COLUMN IF NOT EXISTS approved_join_status text;
ALTER TABLE graph_edge ADD COLUMN IF NOT EXISTS authority_updated_at timestamptz;

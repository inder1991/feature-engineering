-- src/featuregen/db/migrations/1040_graph_node_party_role.sql
-- The per-COLUMN party-role axis of the Semantic Axis Model (ingestion-richness Task 3, Step 3b).
--
-- WHICH PARTY a column describes relative to the row's subject — the third axis beside entity
-- type (Concept.entity_link) and identifier namespace (Concept.namespace). `sender_bic` and
-- `receiver_bic` share ONE concept (`bank_bic`, namespace `swift_bic`) and differ only here;
-- folding the role into concepts would breed `bank_bic_sender`, `bank_bic_receiver`, ... — the
-- combinatorial explosion this axis exists to prevent.
--
-- ADVISORY BY CONTRACT: nothing may consume a party role in a join-candidacy or execution
-- predicate. It explains links and names features ("amounts received FROM counterparty banks");
-- it never gates. A wrong role can change no candidate set, no plan, no execution outcome —
-- pinned by an import-gate test (test_axis_projection) and the plan's must-die mutation.
--
-- Filled by the deterministic display projection (overlay/upload/axis_projection.py) from the
-- column-name token normalizer (party_vocab.normalize_party_role): fill-only-NULL, catalog-scoped,
-- honest abstention (NULL) on ambiguity. No decision-id companion column: this is a projection,
-- not a decision — provenance rides the projection report + the `axis_projection` stage detail.
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS party_role text NULL;

-- Closed vocabulary = party_vocab.PartyRole (0993 house style: an enum-like column carries a
-- CHECK so a typo is an impossible row, not a silently meaningless one). Extending the enum
-- requires extending this CHECK in the same change.
ALTER TABLE graph_node DROP CONSTRAINT IF EXISTS graph_node_party_role_check;
ALTER TABLE graph_node ADD CONSTRAINT graph_node_party_role_check
    CHECK (party_role IS NULL OR party_role IN
           ('subject', 'sender', 'receiver', 'intermediary', 'reimbursement', 'counterparty'));

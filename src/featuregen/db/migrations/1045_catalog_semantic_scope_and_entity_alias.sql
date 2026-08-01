-- src/featuregen/db/migrations/1045_catalog_semantic_scope_and_entity_alias.sql
--
-- Semantic plan Task 2 (interface ledger D7 reservation: 1045). ONE migration, TWO clearly
-- sectioned concerns of one correction: the identifier ISSUER axis, and the counterparty
-- display-entity alias backfill (D12.1).

-- ── Section 1: catalog semantic scope — the identifier ISSUER axis ─────────────────────────────
--
-- `Concept.namespace` names an identifier SCHEME (cif, swift_bic, ...). Scheme equality across
-- catalogs is NOT value-space equality: two banks' "cif" registries are disjoint worlds. The
-- issuer is a per-CATALOG operational fact an operator declares once (mirroring `catalog_engine`:
-- one catalog, one declaration, declarer + timestamp as audit trail). No institution name is
-- hardcoded anywhere — `issuer_scope` is free text an operator supplies through
-- PUT /data-sources/catalogs/{catalog_source}/semantic-scope.
--
-- `basis` says WHY the issuer holds: 'catalog_scope' (this catalog's contents are issued by the
-- declared institution) or 'global_scheme' (the operator asserts a globally-issued scheme
-- catalog-wide — rare; the code-level GLOBAL_SCHEME_ISSUERS registry usually answers first).
-- The 'unresolved' state is deliberately NOT declarable: it is the honest ABSENCE of a row, and
-- resolution reports it as (NULL, 'unresolved') — a missing issuer never looks configured.
CREATE TABLE IF NOT EXISTS catalog_semantic_scope (
    catalog_source text        PRIMARY KEY,
    issuer_scope   text        NOT NULL CHECK (btrim(issuer_scope) <> ''),
    basis          text        NOT NULL CHECK (basis IN ('catalog_scope', 'global_scheme')),
    declared_by    text        NOT NULL,
    declared_at    timestamptz NOT NULL DEFAULT now()
);

-- ── Section 2: counterparty display-entity backfill (D12.1) ────────────────────────────────────
--
-- `counterparty` is a PARTY ROLE (graph_node.party_role, migration 1040), not an entity: a
-- counterparty CIF identifies a CUSTOMER of this bank seen through a role. The registry member
-- `counterparty_id` keeps `entity_link='counterparty'` byte-stable — governed bridge fact keys
-- hash `entity_id`, and flipping the registry would orphan confirmation streams — so the
-- correction is PROJECTION-LAYER: stored display entities flip to 'customer' here, and the alias
-- seam (`concepts.display_entity`) keeps new projections consistent.
--
-- Guards, mirroring axis_projection's fill rules:
--   * only rows whose display entity IS the alias artifact ('counterparty') flip — an explicitly
--     different stored entity is a decision, not an artifact, and survives byte-for-byte;
--   * governed entity assignments are NEVER touched (entity_fact_key / entity_status VERIFIED —
--     the governed re-projection owns those columns).
--
-- The entity rides the search_doc's domain slot, and search_doc's weighted expression lives ONLY
-- in overlay/upload/graph.py (never copied into SQL). The companion Python step
-- `graph.reproject_alias_entity_search_docs` — run by `python -m featuregen migrate` and the
-- FEATUREGEN_AUTO_MIGRATE startup path right after migrations apply — rebuilds the affected
-- rows' documents through that one expression.
UPDATE graph_node
   SET entity = 'customer'
 WHERE kind = 'column'
   AND concept = 'counterparty_id'
   AND entity = 'counterparty'
   AND entity_fact_key IS NULL
   AND entity_status IS DISTINCT FROM 'VERIFIED';

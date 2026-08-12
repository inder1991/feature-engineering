-- src/featuregen/db/migrations/1045_catalog_semantic_scope.sql
--
-- Semantic plan Task 2 (interface ledger D7 reservation: 1045), as REVISED by D12.1: the
-- identifier ISSUER axis only. The originally-planned counterparty->customer entity backfill was
-- WITHDRAWN — review probes proved `graph_node.entity` is a fact-key input (via
-- `bridge_grounding.advisory_entity_id` -> `_entity_pick` -> `fact_key`), so rewriting it would
-- re-key governed bridge facts: a human-REJECTED decoy pair would resurrect as a consumable link
-- under a fresh key, and a VERIFIED link would duplicate with stranded realizations. The
-- `customer` correction is READ-TIME ONLY through the alias seam (`concepts.display_entity`),
-- never a stored rewrite — the `sensitivity` vs `sensitivity_display` precedent.
--
-- ── catalog semantic scope — the identifier ISSUER axis ────────────────────────────────────────
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

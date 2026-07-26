# E5 — Cross-Catalog Data Ontology View (design)

Date: 2026-07-26 · Status: design for review · Parent: `2026-07-26-llm-metadata-enrichment-design.md` (this is E5, Step 2 of the enrichment→ontology arc) · Reference model: Palantir Foundry ontology.

## Goal

Turn the governed, cross-catalog-consistent metadata that E1–E4 produce into a **data ontology** the business can see and use, in **three forms**:
- **(a) Semantic map (query):** "show every column, across all catalogs, that means `balance`"; "all properties of the `Customer` object type."
- **(b) Entity-relationship model:** the object types (`Customer` → `Account` → `Transaction`) with their properties and typed link types + cardinality.
- **(c) Visual navigable graph:** an interactive map of entities ↔ concepts ↔ links, spanning catalogs, with honest authority tiers.

The whole point of the ontology is the **cross-catalog unification** — one semantic picture over many source systems — which is only tractable because every catalog classifies against **one shared concept registry** (the make-or-break fact, confirmed: `concepts.py:842,885`, global, no per-source variant).

## Foundations it stands on (mostly already built)

The ontology is an assembly + view over signals the rest of the program produces — it invents little new *storage*:
- **Object types = entities** — `graph_node.entity` (free-text tag, LLM-suggest→human-confirm via `entity.py:94-161`) plus the governed, concept-keyed **`entity_bridge_edge`** (migration 0989, cross-catalog by construction). E1's enrichment adds each entity's **primary-key** column + **title** property (Foundry object-type framing).
- **Properties = enriched column fields** — concept, definition, domain, analytical role, value semantics, unit/currency, synonyms — the E1–E2 output on `graph_node`.
- **Link types = relationships** — intra-catalog governed joins (`graph_edge` kind='joins' + `approved_join`), cross-catalog `entity_bridge_edge`, and the E3 typed entity link-types with cardinality.
- **Shared class vocabulary + hierarchy** — the 281-concept registry with `group` (19 axes), `is_a` (50 edges), `entity_link` (`concepts.py:29-82`) — the ontology's class layer.
- **Source-qualified identity** — `logical_ref` embeds `catalog_source` (`object_ref.py:33`), so everything is cross-catalog-addressable.
- **A cross-catalog traversal engine already exists** — `lineage_graph` (`lineage.py:73`) already expands across catalogs via `graph_node.entity` + joins + features.

## The load-bearing new capability — cross-catalog entity resolution

The one genuinely new mechanism. Today entities unify via `entity_bridge_edge` (identifier columns denoting the same entity across two uploads, concept-group='identifier' + `entity_link`, human-VERIFIED — `bridge_candidates.py`). E5 must make this a **first-class, complete resolution**: for every entity, resolve its instances across *all* catalogs into one object type (`Customer` in core-banking = `Customer` in cards), so the ontology shows one `Customer`, not three.
- **Reuse:** the concept-keyed bridge (never the free-text tag) + its overlay-fact governance (`bridge_projection.py`, VERIFIED-only).
- **Build:** a resolution pass that proposes bridges for *all* entity identifiers (not just pairwise on upload), an **authority-tiered** merge (source/human/AI-proposed/verified), and conflict handling (§Governance).

## The ontology read-model (source-agnostic)

A new read layer that assembles **object types → properties → link types** into one graph, **not anchored on a single `catalog_source`** (today's `/search`, `/graph/lineage` anchor per-source or per-column). It:
- lists object types (entities) with their resolved cross-catalog membership, primary-key, title, and property set;
- lists link types with direction + cardinality;
- exposes the concept **class hierarchy** (`group`/`is_a`) — held in the registry today but emitted by **no** endpoint (a real gap to close);
- carries an **authority tier** on every node/edge (source-declared / rulebook-derived / AI-proposed / human-verified) so the ontology is honest about what's trusted.

## The three views (on that read-model)

- **(a) Semantic map / query** — reuse `/search`'s flat facet read (`search.py`, facets on source/domain/entity/concept…) + a source-agnostic concept query. Answers "every column meaning X across catalogs" and "properties of object type Y."
- **(b) Entity-relationship model** — object types + properties + typed link-types with cardinality; the concept hierarchy as the class backbone. A structural (schematic) rendering.
- **(c) Visual navigable graph** — extend the validated prototype `AssetDetailSampleScreen.tsx` (authority-tiered graph, entity nodes, candidate edges) into a **source-agnostic ontology explorer**: click an entity → its properties, its links (with cardinality), traverse to related entities across catalogs, filter by concept/domain/authority tier.

## Governance & trust (carry the program's principles)

- **Authority-tiered everywhere** — every object type, property, and link renders its provenance + confidence (source-declared vs rulebook vs AI-proposed vs human-verified), reusing P1a/P2's honest-provenance model. No AI-proposed edge is silently "true."
- **Cross-catalog entity resolution is human-VERIFIED** before it's operational (mirrors join/bridge governance's distinct-confirmer control); AI proposes the merge, a human confirms it.
- **Cross-catalog conflict is surfaced, not hidden** — the reconciliation queue from E2 (same entity/concept modelled differently across catalogs) feeds the ontology so divergence is visible.
- **Non-blocking / advisory** — an unresolved or AI-proposed entity/link still appears in the ontology, flagged, never blocking feature-gen (consistent with the whole program).

## Reuse vs build (grounded)

- **REUSE:** `graph_node` (node model), `entity_bridge_edge` + `graph_edge` + E3 links (edges), `lineage_graph` (cross-catalog traversal), `/search` (query facets), `logical_ref` (identity), `AssetDetailSampleScreen.tsx` (UI target), the concept registry (classes).
- **BUILD:** the complete cross-catalog **entity resolution** pass; a **source-agnostic** ontology read-model/route; a **concept-hierarchy** endpoint (`group`/`is_a`); the three view surfaces; authority-tier rendering on ontology nodes/edges.

## Phasing (within E5)
- **E5a — Resolution + read-model:** cross-catalog entity resolution (AI-proposed, human-verified) + the source-agnostic ontology read-model + concept-hierarchy exposure. Headless, testable.
- **E5b — The three views:** the semantic-map query (a), the entity-relationship model (b), the visual navigable graph (c) — query and schematic first, visual explorer last.

## Dependencies & gating
- **Gated on E1's proven feature-gen lift** — per the product review, the ontology's payoff is future/unproven, so it must not carry the business case; build it after E1 shows value.
- **Needs E3** (typed link types + cardinality) for the relationship layer, and E1's entity PK/title.
- Cross-catalog entity resolution is the critical path; the views are cheap once it + the read-model exist.

## Out of scope / deferred
- **Foundry's Actions / Functions (the "kinetic" write-back layer)** — NOT adopted. Our kinetic layer is **governed feature generation**; the ontology drives features, not operational write-back apps.
- Operational data virtualization / serving the ontology as a query engine over live data — this is a metadata/semantic view, not a data-plane.
- Ontology *editing* as a modelling tool (create object types by hand) — the ontology is *derived* from enrichment + governed resolution, not hand-authored (humans confirm/correct, not author from scratch).

## Success criteria
- **(a)** a source-agnostic query returns every column across all catalogs for a concept/object type, with authority tiers.
- **(b)** the entity-relationship model renders object types (with PK + title), their properties, and typed link types with cardinality, backed by the concept hierarchy.
- **(c)** a data scientist can visually navigate from one entity across a cross-catalog link to a related entity in another catalog, with honest provenance on every node/edge.
- **Resolution:** the same real-world entity across catalogs resolves to one object type (human-verified); conflicts are surfaced, not silently merged.
- **Trust:** no AI-proposed node/edge renders as trusted; every element shows its authority tier.
- **Non-blocking:** an unresolved/AI-proposed entity still appears (flagged) and never blocks feature-gen.

## Risks
- **Entity-resolution errors merge distinct entities** → concept-keyed bridge + human-verify before operational + authority tiers; AI only proposes.
- **Ontology drift across catalogs** → the shared concept registry is the anchor; conflict detection (E2) surfaces divergence.
- **Scope creep toward a Foundry clone** → explicitly no actions/functions/data-plane; this is a derived semantic view, gated on proven enrichment value.
- **Cost/complexity of the visual explorer** → query + schematic views (a, b) land first and carry most of the value; the visual explorer (c) is last.

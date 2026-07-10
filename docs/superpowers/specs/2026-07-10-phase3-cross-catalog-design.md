# Phase 3 — Cross-Catalog Recipe Grounding: Design & Roadmap

> **Status:** Design + phased roadmap. **Phase 3A is specified for implementation** in this document; Phases 3B–3D are roadmap context with their key decisions named but not yet designed. Each later phase gets its own detailed (task-by-task, TDD) planning pass before implementation.
> **Initiative:** the fourth phase of intent-aware recipe selection (after Phase 0 taxonomy, 1A recognizer, 1B scoped grounding, 2 ranking + dimensions). See `intent-aware-recipe-selection` memory.
> **Branch:** to be created off `main` at execution time (whole initiative already merged + pushed to `origin/main` `5c78678`).

## The product gap

The platform lets a user upload related data as **separate catalogs** — customer master, deposit accounts, transactions, cards, collections — and ask for features at a confirmed **target entity** such as `customer`. Two grounding paths exist today with opposite cross-catalog posture:

- The **LLM candidate pipeline** (`feature_assist.py`) already gathers candidate columns across *every* catalog holding the target entity (the entity-scoped gather), and disambiguates per-derive (`AMBIGUOUS_CATALOG` fail-closed).
- The **deterministic recipe/template lens** (`templates.py` `ground_template` / `contract/gate1.py` `build_considered_set`) is **hard single-catalog**. A `Template.needs` list can only bind columns from *one* `catalog_source`, and `build_considered_set` **skips the template lens entirely on an entity-only, multi-catalog run** (`gate1.py`: `if catalog_source is not None:`).

The intent-aware initiative (applicability → grounding → disposition → ranking) is built on the **deterministic** lens. So the user-visible defect is: **the governed, deterministic recipe experience disappears exactly when the user has done the right thing and uploaded normalized data across catalogs.** Phase 3 closes that gap: a deterministic recipe must bind its ingredients across multiple catalogs, discover a **governed** join plan through the entity model and **sanctioned entity bridges**, preserve source-level provenance and freshness, and surface the same applicability / safety / policy / ranking evidence a single-catalog recipe receives.

## Canonical acceptance scenario

```text
Target entity: customer

Catalog A — customer_master   Catalog B — transactions      Catalog C — accounts
  customer_id                   account_id                    account_id
  segment                       transaction_amount            customer_id
  relationship_start_date       transaction_timestamp         account_status

Recipe: "Average transaction value per customer over the previous 90 days"
```

The end state (delivered across 3A–3C) must:

1. recognize the recipe is applicable to the confirmed use case;
2. bind `transaction_amount` + `transaction_timestamp` from the transactions catalog;
3. bind the account→customer relationship from the accounts catalog;
4. bind/confirm the customer entity from customer master;
5. build a **declared** join-and-aggregation plan at customer grain;
6. **reject** the plan if the entity path is ambiguous or unsanctioned;
7. preserve each catalog + object reference in provenance;
8. require **every** participating catalog to satisfy freshness;
9. run universal safety + contextual policy on the resulting binding plan;
10. rank the recipe alongside single-catalog recipes.

**The corrected logical path** (the planner must not jump from a transaction object to a bridge merely because an `account_id` column exists):

```text
transaction
  -- intra-catalog realization (transactions catalog) -->
account (as represented in transactions)
  -- entity bridge (same-entity identity: account) -->
account (as represented in accounts)
  -- semantic roll-up (account → customer) -->
customer
```

The first leg is an **intra-catalog realization** of the global `transaction → account` relationship — not a bridge. Bridges connect two catalog-local representations of an *already-known* entity; they never assert a new semantic roll-up.

## Invariants carried forward (fold into every phase)

1. **NO data plane.** The platform produces feature **definitions/specs**, never computed values. A cross-catalog result is a *feature-contract definition* (a declared join + aggregation plan), never a computed feature or a stored row-level join.
2. **Flag-gated default-off = byte-identical** to prior behaviour — *except* a genuinely behaviour-neutral, regression-equivalent mechanism replacement (Phase-0 discipline), which needs no flag.
3. **Fail-open asymmetry.** Relevance/grain uncertainty **broadens or stays soft** (never narrows on doubt); a cross-catalog **join** that is ambiguous or unsanctioned **fails explicit** (closed). Missing entity metadata is `UNKNOWN` (soft), never a hard reject.
4. **LLM proposes, deterministic code + humans dispose.** Metadata-derived entity relationships are *proposals*, never automatically active graph edges.
5. **Append-only / immutable records** where state is persisted.
6. **Universal safety is additive-only** (`_safe_to_bind` — leakage-anchor + protected/special-category) and never relaxed; cross-catalog binding runs it per participating column.
7. **F4 preserved — the governance wall stays up.** A single-catalog adapter must **never** attest a fact whose endpoint is in another catalog. Cross-catalog `approved_join` attestation remains forbidden (`identity.py` `join_write_error`). Cross-catalog paths are composed of *intra-catalog* `approved_join`s stitched by *governed entity bridges* — the exact adjacency `find_cross_catalog_path` already uses. Because the cross-catalog result is a contract *definition*, no new attested join fact is created, so F4 is never approached.
8. **Declared-metadata boundary.** The platform validates *declared* semantics only (see "Scope boundary" below). It cannot and must not claim to validate observed row-level join quality.

## Decomposition (foundation → shadow → enforce → expand)

This mirrors the pattern that has worked all initiative.

- **Phase 3A — Entity & grain graph foundation.** Replace the hardcoded `_ENTITY_ROLLUP` map with a curated, versioned **global semantic entity-relationship graph**; rewire `entity_compatibility` to traverse it with **regression-equivalent** behaviour. Define (but do not populate/traverse) stable contracts for catalog realizations, governed bridges, and proposals. **Behaviour-neutral, independently shippable.** *This document specifies 3A in full.*
- **Phase 3B — Cross-catalog recipe binding in shadow.** Enrich `Template.needs`; compose the *physical* graph (catalog realizations from existing join edges + governed bridges); a deterministic cross-catalog **binding-plan** builder; compute plans **in shadow** (no user-visible change) and measure against expert-authored expected plans.
- **Phase 3C — Live deterministic cross-catalog grounding.** Stop skipping the template lens on entity-scoped runs; prefer single-catalog then sanctioned-bridge plans; multi-catalog freshness fail-closed; safety / policy / ranking unchanged. Flag-gated.
- **Phase 3D — Optional expansion.** Additional governed relationship types, broader cross-catalog joins, hard `INCOMPATIBLE` — only after evidence justifies it.

## Governance model (asymmetric C) — cross-phase context

The entity graph draws on **three edge classes with distinct roles** plus a proposal artifact. They do **not** compete through one score; each validates a different part of a path.

| Class | Answers | Authority | Active in 3A? |
|---|---|---|---|
| **Global semantic relationship** (`EntityRelationshipDefinitionV1`) | *Is entity grain A semantically derivable into B?* | `global_entity_model` | **Yes** — the only active edge class |
| **Catalog realization** (`CatalogEntityRelationshipV1`) | *How does this catalog physically realize that relationship?* | `catalog_declared` | Contract only |
| **Entity bridge** (`EntityBridgeV1`) | *How do two catalog-local representations refer to the same entity?* | `entity_bridge` | Contract only |
| **Relationship proposal** (`EntityRelationshipProposalV1`) | *Metadata-derived candidate — evidence, not truth.* | (proposal) | Contract only; **never traversed** |

Operating rules (activated in 3B, contracts locked in 3A):
- The **curated global model** is authoritative for *semantic validity*. Catalog declarations provide *physical realization*, lower-authority and scoped to their catalog; they may **not** silently redefine the global model. Entity bridges assert *cross-catalog identity*, not new roll-ups. Inferred relationships are **proposals only** — never inserted into or traversed by the active graph without governance.
- **Conflicts fail closed.** A catalog declaration whose cardinality contradicts the global model (e.g. global `many_to_one`, catalog `many_to_many` for joint accounts) yields `RELATIONSHIP_DECLARATION_CONFLICT` — never a silent override; the conflict is treated as evidence the semantic model is too simple.
- A catalog edge with **no** global relationship is a `catalog_local_relationship`: usable only within that catalog, not eligible for cross-catalog planning or global compatibility, until governed and promoted.

---

# Phase 3A — Entity & grain graph foundation (specified)

## Scope statement

> Phase 3A replaces the hardcoded entity roll-up map with a curated, versioned **global semantic relationship graph** and rewires entity compatibility to traverse it with **regression-equivalent** behaviour. It defines stable contracts for catalog realizations, governed entity bridges, and relationship proposals, but does **not** populate or traverse those edge classes. Physical catalog realization, bridge governance, composite conflict resolution, and cross-catalog planning begin in Phase 3B, where they have their first active consumer.

**The load-bearing acceptance criterion:** for every currently-supported `(source, target)` pair, `new graph-backed compatibility == old _ENTITY_ROLLUP compatibility`. Same `EXACT / DERIVABLE / UNKNOWN`, same ranking, same grain-warning output. No flag, because the seed graph *is* the map.

## What 3A ships (four things)

### 1. Global semantic entity model — in-code governed registry

An immutable, version-controlled tuple `ENTITY_RELATIONSHIPS_V1` of `EntityRelationshipDefinitionV1`, seeded with **exactly** the five existing roll-ups — and nothing more in 3A. Any *new* relationship that could introduce a second path for an existing pair (and thus change an output) is deferred to 3D; keeping the seed to the five is what makes regression-equivalence airtight. Entity ids are drawn from the closed `known_entities()` vocabulary (the distinct `Concept.entity_link` values); the builder rejects any relationship whose endpoint is not in it.

The seed (regression-equivalent to `_ENTITY_ROLLUP`):

```text
transaction   → account    many_to_one  rollup  (aggregation_required)
account       → customer   many_to_one  rollup  (aggregation_required)
card_account  → customer   many_to_one  rollup  (aggregation_required)
facility      → obligor    many_to_one  rollup  (aggregation_required)
policy        → customer   many_to_one  rollup  (aggregation_required)
```

These five edges form a **forest** (each source has ≤1 outgoing roll-up parent), so no `(source, target)` pair has two distinct paths → `AMBIGUOUS` is provably unreachable from the seed. This is what makes regression-equivalence hold while the engine gains ambiguity capability.

### 2. Semantic traversal engine

Operates only on active global semantic relationships in 3A. Deterministic, cycle-safe, direction-aware, **path-returning** (not merely boolean), preserves aggregation requirements, and detects *distinct* valid paths (so it can report ambiguity rather than hide it behind shortest-path tie-breaking).

```python
class EntitySemanticPathV1(BaseModel):
    # one distinct directed roll-up chain source→…→target, in order; each hop names its
    # relationship and carries the aggregation the roll-up requires (metadata for 3B).
    hops: tuple[EntityRelationshipRefV1, ...]   # (relationship_id, from_entity, to_entity,
                                                #  cardinality, aggregation_required, allowed_aggregations)

class EntityCompatibilityResultV1(BaseModel):
    status: EntityCompatibility            # EXACT | DERIVABLE | AMBIGUOUS | UNKNOWN
    source_entity: EntityId
    target_entity: EntityId
    paths: tuple[EntitySemanticPathV1, ...]  # () for EXACT/UNKNOWN; one for DERIVABLE; all for AMBIGUOUS
    reason_codes: tuple[str, ...]
    graph_version: str
```

Traversal semantics:

```text
source == target                     → EXACT       (paths = ())
exactly one valid directed path      → DERIVABLE   (paths carries the single roll-up chain)
multiple semantically distinct paths → AMBIGUOUS   (paths carries all; NEVER auto-pick shortest)
no path                              → UNKNOWN
```

`AMBIGUOUS` is exercised by **synthetic multi-path fixtures only**; the production seed never emits it.

### 3. `entity_compatibility` rewrite — byte-identical adapter

Delete `_ENTITY_ROLLUP` and `_rolls_up_to` (exit criterion: **removed, not retained as a fallback**). The existing public function keeps its exact signature and enum return, delegating to the resolver:

```python
def entity_compatibility(t: Template, target_entity: str | None = None) -> EntityCompatibility:
    if target_entity is None:
        return EntityCompatibility.UNKNOWN            # unchanged
    source = _grain_entity(t)                          # unchanged derivation
    if source is None:
        return EntityCompatibility.UNKNOWN            # unchanged
    return resolve_entity_compatibility(source, target_entity, ENTITY_GRAPH).status
```

`resolve_entity_compatibility(source_entity, target_entity, graph) -> EntityCompatibilityResultV1` is the new richer API 3B consumes. Existing callers (`_rank_signals`, the `entity_grain_mismatch` warning) see the identical `EntityCompatibility` enum. `graph_version` is exposed where compatibility results feed rank explanations / warnings, so a graph bump is observable in provenance without mutating a prior projection.

### 4. Stable contracts for later edge classes

Define, validate, and test the types — **contracts, not active graph inputs**:

```python
class EntityRelationshipDefinitionV1(BaseModel):   # ACTIVE in 3A
    relationship_id: str
    from_entity: EntityId
    to_entity: EntityId
    relationship_type: RelationshipType            # rollup | parent_child | ownership | membership | identity
    cardinality: Cardinality                       # one_to_one | one_to_many | many_to_one | many_to_many
    traversal_direction: TraversalDirection        # forward | reverse | both
    aggregation_required: bool
    allowed_aggregations: tuple[AggregationType, ...]
    status: RelationshipStatus                      # active | deprecated
    version: str

class CatalogEntityRelationshipV1(BaseModel):      # CONTRACT ONLY in 3A
    realization_id: str
    relationship_id: str                            # references a global definition
    catalog_source: str
    from_object_ref: str
    to_object_ref: str
    declared_cardinality: Cardinality
    adapter_id: str
    authority: Literal["catalog_declared"]
    status: RelationshipStatus

class EntityBridgeV1(BaseModel):                    # CONTRACT ONLY in 3A
    bridge_id: str
    entity_id: EntityId
    left_catalog_source: str
    left_object_ref: str
    right_catalog_source: str
    right_object_ref: str
    authority: Literal["entity_bridge"]
    status: RelationshipStatus

class EntityRelationshipProposalV1(BaseModel):     # CONTRACT ONLY in 3A; NEVER traversed
    proposal_id: str
    proposed_from_entity: EntityId
    proposed_to_entity: EntityId
    proposed_cardinality: Cardinality
    evidence_refs: tuple[str, ...]
    source_catalog: str
    inferred_by: str
    status: Literal["pending", "accepted", "rejected"]

class GraphEdgeAuthority(StrEnum):
    GLOBAL_ENTITY_MODEL = "global_entity_model"
    CATALOG_DECLARED = "catalog_declared"
    ENTITY_BRIDGE = "entity_bridge"
```

## "Precedence-capable", not runtime precedence

3A traversal uses only global semantic edges and therefore performs **no cross-authority resolution**. The contracts preserve edge **authority and role** so 3B can apply *role-specific* precedence and conflict rules. There is **no single numeric priority** (`global=100, catalog=50, bridge=25` is explicitly rejected) — the classes do different jobs and must not compete through one score. Role-based evaluation (semantic validity / physical realization / cross-catalog identity / discovery evidence) is a **3B** concern.

## Deliverables (tasks)

- **3A.1 — Entity graph contracts.** The four edge contracts + supporting types (`EntityId`, `RelationshipId`, `RelationshipType`, `Cardinality`, `TraversalDirection`, `AggregationType`, `RelationshipStatus`, `GraphEdgeAuthority`, `EntitySemanticPathV1`). Validation rejects: self-relationships (unless explicitly allowed), invalid cardinality, unsupported reverse traversal, aggregation-required relationships with no allowed aggregation, duplicate active relationship ids, dangling entity ids (endpoint ∉ `known_entities()`), invalid version identifiers.
- **3A.2 — Curated semantic registry.** `ENTITY_RELATIONSHIPS_V1` with stable ids, version, descriptions, endpoints, direction, cardinality, aggregation requirement, optional deprecation metadata. Seeded from the five roll-ups.
- **3A.3 — Semantic graph builder.** Build an immutable graph from active global relationships: deterministic ordering, duplicate-edge normalization, cycle-safe traversal support, inactive-edge exclusion, an exact `graph_version`, and **no catalog or bridge inputs**.
- **3A.4 — Compatibility traversal.** `resolve_entity_compatibility(source, target, graph) -> EntityCompatibilityResultV1` with the four-outcome semantics above; no shortest-path tie-breaking to hide ambiguity.
- **3A.5 — Rewire Phase 2B.** Replace `_ENTITY_ROLLUP`/`_rolls_up_to` with the resolver (thin adapter), delete the old map, expose `graph_version` where results affect rank explanations/warnings.
- **3A.6 — Future-contract feasibility spike (tests only).** Prove the *contracts* can represent real upload metadata **without activating them**: a pure transform `catalog_relationship_from_join_edge(edge, endpoint_entities) -> CatalogEntityRelationshipV1` tested against representative existing `graph_edge` joins (`transaction.account_id → account.account_id`, `account.customer_id → customer.customer_id`, `facility.borrower_id → borrower.borrower_id`), and a transform proving existing computed `EntityBridge` data maps into `EntityBridgeV1`. Outside the active graph, outside `entity_compatibility`, not persisted, explicitly a contract-validation spike. (The endpoint→entity resolver reads `graph_node.entity` / the column's `Concept.entity_link`.)

## Exit criteria

1. Every existing hardcoded relationship exists in the curated registry.
2. Every current compatibility fixture returns the same result (`old == new`, all pairs).
3. Ranking and grain-warning responses are unchanged.
4. The old hardcoded map is **removed**, not retained as fallback.
5. Traversal is cycle-safe and deterministic.
6. `AMBIGUOUS` is covered by synthetic tests but **not emitted by the production seed**.
7. Aggregation-required metadata is preserved in returned paths.
8. Graph version is stable and observable.
9. Existing catalog join metadata can be represented by `CatalogEntityRelationshipV1` (feasibility spike).
10. Existing bridge data can be represented by `EntityBridgeV1` (feasibility spike).
11. Catalog realizations and bridges are **not** active traversal inputs.
12. No migration or governance UI is introduced (the in-code registry needs no persistence in 3A).

## Testing approach

- **Regression-equivalence** (the load-bearing test): enumerate every `(source, target)` pair the old map handled (including transitive `transaction → customer`) plus representative non-pairs, and assert `resolve_entity_compatibility(...).status` equals the pre-3A `entity_compatibility` output. Run the existing `test_ranking_signals.py` + route-ranking suites unchanged — they must stay green.
- **Traversal properties:** cycle graphs terminate; reverse traversal is not assumed for a `forward`-only edge; duplicate identical paths collapse deterministically; inactive edges excluded.
- **Ambiguity (synthetic):** a fixture graph with two distinct paths `transaction → account → customer` and `transaction → card → customer` returns `AMBIGUOUS` with both paths, never an arbitrary winner.
- **Contract validation:** every rejection rule in 3A.1 has a failing-input test.
- **Feasibility spike:** the two transforms produce valid `V1` contracts over real fixture join/bridge data.

## Scope boundary (holds for all of Phase 3)

Phase 3 validates **declared** semantics only: declared entity relationships, declared cardinality, declared aggregation, sanctioned bridge usage, metadata-level temporal semantics, freshness of all participating catalogs, path uniqueness, source provenance. It does **not** — and with no data plane, cannot — validate observed join coverage, orphan/duplicate-key rates, row-level temporal alignment, empirical leakage, or observed cardinality violations. This boundary stays explicit in every artifact.

---

# Named 3B decisions to record now

These are deferred, not forgotten — they surface where the planner is their first active consumer.

- **Governed bridge transition (a behaviour change).** Today `find_cross_catalog_path` treats *any* two columns sharing an entity as a permissive bridge. 3B must move to: an active governed `EntityBridgeV1` is traversable; an unapproved same-entity coincidence is a proposal or a rejected path. This changes the dormant path-finder's behaviour → behind the 3B shadow path.
- **Catalog realization activation.** 3B derives candidate realizations from existing join edges, validates endpoint entities, binds them to global relationship ids, detects cardinality conflicts, decides treatment of unmapped local relationships, and composes physical paths.
- **Composite conflict handling.** 3B defines the runtime outcomes: `VALID | AMBIGUOUS | RELATIONSHIP_CONFLICT | UNSANCTIONED_BRIDGE | MISSING_REALIZATION | UNKNOWN`.
- **Physical planner preference (deterministic).** Likely: complete authoritative single-catalog plan → preferred; cross-catalog with one governed bridge → next; multiple bridges → lower / initially unsupported; ambiguous / conflicting / unsanctioned → rejected. Prefer single-catalog when equivalent (simpler, less governance).
- **Enriched `Template.needs`.** 3B evolves the needs contract (source_grain, target_grain, aggregation function+window, temporal_role, join_role, unit/currency expectations, authoritative-source requirement) across the 153 templates — new fields must be optional/derived to avoid a breaking migration of the library.
- **Freshness integration.** The binding plan exposes `participating_catalogs`; reuse `resolve_fact`'s existing per-source drift guard (already fails closed unless every catalog is fresh). Distinguish *plan structurally authorable* from *fact resolvable now* — a stale catalog yields `resolution_status = blocked_by_stale_catalog`, not permanent `unbuildable`.

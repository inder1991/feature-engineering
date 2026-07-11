# Phase 3B — Cross-Catalog Recipe Binding in Shadow: Design

> **Status:** Design (v2 — the 20-finding design review folded in at full scope). Ready for planning. Phase 3B is decomposed into **five sequential sub-parts (3B.1, 3B.2A, 3B.2B, 3B.3, 3B.4)**, each an independently testable increment; each gets its own task-by-task TDD plan at execution.
> **Initiative:** Phase 3B of intent-aware recipe selection. Builds on Phase 3A (the entity/grain graph, shipped to `origin/main`).
> **Parent spec:** `docs/superpowers/specs/2026-07-10-phase3-cross-catalog-design.md`. **Next DB migration number is `0977`.**

## The core semantic principle (the review's through-line)

3B's real risk is not determinism — it's **translating a declared *semantic* entity relationship into a physical *catalog* path** without inferring the wrong thing. Every rule below exists to keep that translation explicit and governed. Three distinctions are load-bearing and must never be conflated:

1. **Object grain ≠ join-key entity.** A join `transactions.account_id → accounts.account_id` has both endpoint columns at entity `account` (the *join key*), but the semantic hop it realizes is `transaction → account` (the *object grains*). The object grain of a table is the `entity_link` of its `is_grain` column.
2. **Required/allowed grain ≠ actual bound grain.** A recipe need declares which grains are *acceptable*; the *actual* grain is read off the column's containing object after binding.
3. **Authority is stamped, never assumed.** Whether a hop rests on an approved join, a declared join, an inferred join, a sanctioned bridge, or an unsanctioned coincidence is recorded on the plan — measured in shadow, enforced in 3C.

Nothing is inferred from a column name, a concept name, or a need's tuple position.

## What 3B does, and what it deliberately does not

The deterministic recipe lens binds a recipe's ingredients from a **single** catalog today. Phase 3B computes a **cross-catalog binding plan** — ingredients bound across separately-uploaded catalogs, joined through a governed entity path — **in shadow**: plans are computed, persisted append-only, and measured against expert-authored expected plans, with **zero user-visible behaviour change**. Making the planner *live* (wiring it into grounding, enforcing rejections) is **Phase 3C**.

**Canonical acceptance scenario:** target entity `customer`; `customer_master`, `transactions`, `accounts` uploaded as separate catalogs; *"average transaction value per customer over the previous 90 days"* must bind `transaction_amount`/`transaction_timestamp` from `transactions`, cross to `accounts` via a governed `account` bridge, roll `account → customer`, and produce one auditable binding-and-join-and-aggregation plan at customer grain — or an explicit non-`VALID` status.

## Invariants (fold into every sub-part)

1. **NO data plane.** A binding plan is a *feature-contract definition*, never a computed value or a stored row-level join.
2. **Shadow-first.** 3B computes + persists + measures; it changes **no** disposition/ranking/output. Flag-gated default-off. "Fail-closed" in 3B = an **explicit non-VALID status on the plan record**, not a blocked user path (that is 3C).
3. **F4 preserved.** No cross-catalog attestation. Cross-catalog paths compose *intra-catalog* realizations stitched by *governed same-entity bridges*; the result is a definition, so no cross-catalog fact is attested.
4. **Fail-open asymmetry.** Relevance/grain uncertainty stays soft; an ambiguous/unsanctioned/conflicting **join** fails *explicit* — a definite non-VALID status; never a silent partial plan.
5. **LLM proposes, deterministic code + humans dispose.** Realizations + candidate bridges are derived by deterministic code over *declared* metadata; a derived bridge is a **proposal**, never auto-active.
6. **Append-only / immutable + reproducible.** Plan records, proposals, and bridge sanctions are append-only and event-sourced. Every plan stamps a full input-version envelope and is idempotent by input hash, so it replays exactly.
7. **Catalog authorization.** The planner searches only the run's **authorized** catalog set (read-scope + explicit request scope + workspace); it never bridges into a catalog the actor cannot read.
8. **Universal safety is additive-only**, staged explicitly (below); a plan is never called clean without a distinct safety decision.
9. **Declared-metadata-only boundary.** No observed join coverage, orphan/duplicate rates, row-level alignment, or empirical leakage — ever.

## Decomposition

| Sub-part | Delivers | Findings resolved |
|---|---|---|
| **3B.1** Resolved recipe binding metadata | governed per-need grain/role/temporal metadata, versioned + resolved | #1 #2 #3 #4 |
| **3B.2A** Catalog realization derivation | object-grain-aware realizations from declared joins, normalized + authority-stamped | #5 #6 #7 |
| **3B.2B** Governed bridges | identifier-qualified bridge proposals + event-sourced sanction lifecycle | #8 #9 #10 |
| **3B.3** Bounded deterministic planner | candidate plans + physical paths + agg/temporal + freshness/safety staging + replay | #11–#20 |
| **3B.4** Shadow persistence + evaluation | append-only idempotent records + objective exit gates | #20 + gates |

Strict dependency order; each reads the one before. 3C (live) reads 3B.3's planner.

---

## 3B.1 — Resolved recipe binding metadata (#1 #2 #3 #4)

**Grain as a constraint, not an assertion (#1).** A need declares which grains are acceptable; the actual grain is derived from the bound object:

```python
# added to Need (optional; None -> derived at resolve time):
#   allowed_source_grains: tuple[str, ...]   # acceptable grains for this ingredient ((),= unconstrained)
#   join_role: JoinRole | None
#   temporal_role: TemporalRole | None

class IngredientBindingV1:
    need_role: str
    catalog_source: str
    object_ref: str
    actual_source_grain: str   # entity of the bound object's is_grain column — NOT the concept entity_link
```

The planner compares `allowed_source_grains` (or unconstrained) against `actual_source_grain`. Concept `entity_link` is the *join-key* entity, never treated as the object's actual grain.

**Explicit anchor semantics, no tuple-position inference (#2).** Anchor identity comes from an explicit role model + a template-level declaration, never "first entity-linking need":

```python
class JoinRole(StrEnum):
    SOURCE_ENTITY_KEY = "source_entity_key"          # fixes the recipe's source grain
    TARGET_ENTITY_KEY = "target_entity_key"          # the grain the plan rolls up to
    INTERMEDIATE_ENTITY_KEY = "intermediate_entity_key"  # a hop key supplied by an intermediate catalog
    MEASURE = "measure"
    TIME = "time"

class Template:
    source_entity: str | None            # the recipe's source grain entity
    source_entity_need_role: str | None  # which need carries the source key
```

Validation **rejects** any recipe with more than one entity-linked need whose anchor is not explicitly resolved (no ambiguous derivation is allowed to stand).

**Versioned resolved metadata — no runtime rederivation (#3).** Derivation runs **once**, producing an immutable, versioned resolved view the planner consumes; historical plans never silently rederive when concept metadata evolves:

```python
@dataclass(frozen=True)
class ResolvedNeedMetadataV1:
    role: str
    concept: str
    allowed_source_grains: tuple[str, ...]
    join_role: JoinRole
    temporal_role: TemporalRole
    derivation_source: Literal["explicit_recipe", "concept_registry", "template_default"]

# versions: need_metadata_version · concept_registry_version · recipe_registry_version
```

3B.1 ships: the derivation function, structural validation, a **complete derivation report** (every field's source), explicit per-recipe overrides where derivation is ambiguous, and the versioned resolved registry. Behaviour-neutral, no flag (nothing reads it until 3B.3).

**Governed temporal role (#4).** `temporal_role` derives from *governed* metadata — the existing `is_as_of` column flag + the concept `"temporal"` group — never from names. The controlled vocabulary (concepts may declare, initially seeded from `is_as_of`):

```python
class TemporalRole(StrEnum):
    NONE = "none"
    EVENT_TIME = "event_time"
    AS_OF_TIME = "as_of_time"
    INGESTION_TIME = "ingestion_time"
    VALID_FROM = "valid_from"
    VALID_TO = "valid_to"
```

The contract **explicitly notes** that event-time does not prove availability-time (availability/ingestion may lag business-event time); 3B declares temporal roles, it does not validate row-level PIT.

**Prunes retained, with the correction (#1 note):** no per-need `target_grain` (it's the confirmed scope's `target_entity`); no per-need `unit`/`currency` — **but** the spec makes explicit that the existing mixed-unit/mixed-currency gauntlet applies to the **assembled cross-catalog binding set**, not per-catalog, so two individually-clean catalogs cannot yield a mixed-currency plan.

---

## 3B.2A — Catalog realization derivation (#5 #6 #7)

A `graph_edge` join realizes a semantic relationship when the **object grains** of its endpoints match the relationship's entities, joined by a key. The 3A `CatalogEntityRelationshipV1` (inactive, so safe to extend) is extended to carry both:

```python
@dataclass(frozen=True)
class CatalogEntityRelationshipV1:
    realization_id: str
    catalog_source: str
    from_object_ref: str; from_object_grain: str    # object grain (is_grain column's entity)
    to_object_ref: str;   to_object_grain: str
    from_key_ref: str;    from_key_entity: str       # the join-key column + its entity
    to_key_ref: str;      to_key_entity: str
    relationship_id: str                             # bound global relationship (from_object_grain -> to_object_grain)
    declared_cardinality: Cardinality
    authority: RealizationAuthority
    status: RelationshipStatus
```

**Direction + cardinality normalization (#6).** A join authored in either direction realizes one directional relationship; `normalize_realization(join_edge, from_grain, to_grain, global_rel)`: (1) test forward orientation, (2) test reverse, inverting cardinality (`N:1 ↔ 1:N`), (3) reject if neither matches, (4) reject if both orientations match but imply different semantics. Reverse-authored joins get explicit tests.

**Realization authority, stamped not assumed (#7).** Existing single-catalog grounding legitimately uses **declared** joins; 3B preserves that but stamps the level:

```python
class RealizationAuthority(StrEnum):
    APPROVED_JOIN = "approved_join"     # attested approved_join fact
    DECLARED_JOIN = "declared_join"     # uploaded graph_edge join
    INFERRED_JOIN = "inferred_join"     # metadata-inferred
```

In **shadow**, every authority level is *recorded* and *measured* (which levels plans lean on); the VALID-capability rule (`approved` VALID-capable; `declared` preserved-but-flagged; `inferred` proposal-only) is expressed as plan status and **enforced in 3C**, not blocked in shadow. Existing single-catalog behaviour is byte-identical.

**Unmapped joins** (entity pair with no global relationship) → a `catalog_local_relationship`: intra-catalog-only, not cross-catalog-traversable, recorded as a proposal.

**Cache key (#5 close):** realizations are derived per upload and cached on a composite immutable key — `catalog schema/snapshot fingerprint · global-graph fingerprint · concept-registry version · realization-derivation version` — never on the mutable `catalog_source` name alone.

---

## 3B.2B — Governed bridges (#8 #9 #10)

**Candidate eligibility — not every shared-entity pair (#8).** A candidate bridge requires, from *existing* metadata: both endpoints are **identifier** concepts (concept `group="identifier"`) for the **same** `entity_link`; both are key-like (`is_grain` / entity-key role); **compatible `data_type`**; **distinct** catalog sources; **same workspace**; and recorded **evidence** (same entity, key roles, type compatibility, namespace where available). Arbitrary entity coincidence never produces a proposal.

**A bridge-specific proposal contract (#9)** — distinct from the semantic `EntityRelationshipProposalV1`, because a bridge asserts *cross-catalog identity of the same entity*, not a new `A → B` relationship:

```python
@dataclass(frozen=True)
class EntityBridgeProposalV1:
    proposal_id: str
    entity_id: str
    left_endpoint: CatalogEntityEndpointV1    # (catalog_source, object_ref, key_ref, identifier_role, namespace)
    right_endpoint: CatalogEntityEndpointV1
    evidence: BridgeEvidenceV1
    status: RelationshipProposalStatus
```

**Event-sourced sanction lifecycle (#10).** A sanctioned bridge is not a mutable `status=active` row — its active state is **derived from an append-only event log**, so it can be suspended/retired/corrected/superseded (e.g. after catalog drift):

```
entity_bridge_version        # immutable bridge definition (endpoints, entity, namespace, catalog fingerprints)
entity_bridge_event          # proposed | sanctioned | activated | suspended | retired  (append-only)
                             #   each event carries: actor/authority, effective_from, catalog_snapshot_ids, reason
```

Active state is a projection over events (the Phase-2-policy pattern). A bridge is bound to its catalogs' schema fingerprints, so drift can trigger a governance alert / re-review. 3B provides the code/admin sanction path (no UI — that is 3C), and every 3C-exposed action uses this same event model. Bridges are **workspace/organization-scoped**. The old permissive `cross_join_via_entity` stays dormant.

---

## 3B.3 — Bounded deterministic planner (#11–#20)

For each applicable recipe + confirmed `target_entity`, over the **authorized catalog set**, the planner enumerates candidate plans, prefers deterministically, and preserves all evidence.

**Catalog authorization bounds the search (#17):**

```python
@dataclass(frozen=True)
class CatalogScopeV1:
    workspace_id: str                          # the run's org/workspace (single-tenant constant until a
                                               #   multi-tenant model lands; the contract scopes by it now
                                               #   so multi-tenancy is config, not redesign)
    allowed_catalog_sources: tuple[str, ...]   # read-authorized + explicitly requested
    catalog_snapshot_ids: tuple[str, ...]
```

The planner never searches, binds, or bridges outside this set.

**Preserve alternatives, not one effective failure (#11 #14 #15):**

```python
@dataclass(frozen=True)
class CrossCatalogPlanningResultV1:
    recipe_id: str; target_entity: str
    selected_plan_id: str | None
    candidate_plans: tuple[CrossCatalogCandidatePlanV1, ...]
    effective_status: CrossCatalogPlanStatus
    diagnostics: tuple[PlannerDiagnosticV1, ...]
    search_truncated: bool
    versions: PlannerInputVersionSetV1

@dataclass(frozen=True)
class CrossCatalogCandidatePlanV1:
    plan_id: str
    ingredient_bindings: tuple[IngredientBindingV1, ...]
    semantic_path: tuple[EntityRelationshipRefV1, ...]
    physical_segments: tuple[PlanPathSegmentV1, ...]     # #11 — the auditable physical join plan
    bridge_refs: tuple[BridgeRefV1, ...]
    aggregation_plan: AggregationPlanV1                  # #12
    temporal_plan: TemporalPlanV1                        # #12
    participating_catalogs: tuple[str, ...]
    provenance: tuple[SourceObjectRefV1, ...]
    preference_tier: int; bridge_count: int
    structural_status: CrossCatalogPlanStatus            # #19 — STRUCTURAL only
    safety_decision: StageDecisionV1                     # #19 — separate stage
    resolution_status: ResolutionStatus                  # #18
    reason_codes: tuple[str, ...]
```

Precedence is **candidate-local first, global only when no VALID candidate remains (#15)** — a conflict on an *unused* alternative never invalidates a valid plan. If any candidate is VALID, the result is VALID (highest-preference selected) with rejected alternatives retained as diagnostics; else the effective status is the strongest failure by precedence `RELATIONSHIP_CONFLICT > UNSANCTIONED_BRIDGE > MISSING_REALIZATION > AMBIGUOUS > UNKNOWN`, with every candidate's failure retained — so the review can see "sanction bridge A" is the actionable route.

**Physical path segments (#11):**

```python
class PlanPathSegmentV1:
    segment_type: Literal["catalog_realization", "entity_bridge", "semantic_rollup"]
    catalog_source: str | None; realization_ref: str | None; bridge_ref: str | None
    semantic_relationship_ref: str
    from_entity: str; to_entity: str
```

An auditor can reconstruct which physical join realizes each semantic hop, where a catalog boundary is crossed, which bridge authorizes it, and which ingredient depends on which segment.

**Compiled aggregation + temporal declarations (#12):** the plan pins `AggregationPlanV1` (`measure_need_role, function, source_grain, target_grain, group_entity, window`) and `TemporalPlanV1` (`event_time_binding, as_of_binding, window, availability_semantics`) — declared, never executed.

**Bounded, deterministic search (#13):** hard bounds (`max candidate bindings/need · max catalogs/plan · max bridges/plan · max physical paths/binding · max plans/recipe · max search nodes`); deterministic pruning order (exact-concept match → authoritative metadata → fewer catalogs → fewer bridges → shorter path → stable ids); on hitting a bound, `search_truncated=True` (never a pretend-complete result). Measured in shadow.

**Precise ambiguity (#16):** distinct reason codes `AMBIGUOUS_SEMANTIC_PATH · AMBIGUOUS_PHYSICAL_REALIZATION · AMBIGUOUS_INGREDIENT_BINDING · AMBIGUOUS_BRIDGE · AMBIGUOUS_EQUIVALENT_PLAN`. Deterministically-preferred alternatives (single-catalog over one-bridge) are **not** ambiguous.

**Preference (decision 4):** single-catalog → one governed bridge → multiple governed bridges; compute all tiers, select highest-preference VALID, record tier + bridge count (measure multi-bridge need); multi-bridge-live is a 3C/3D call.

**Freshness integration contract (#18):** a plan's `participating_catalogs` feed the existing drift guard via an explicit `fact_dependencies_from_binding_plan(plan)` / `resolve_plan_freshness(plan, snapshots)`; resolvability is distinct from structural validity:

```python
class ResolutionStatus(StrEnum):
    RESOLVABLE = "resolvable"
    BLOCKED_BY_STALE_CATALOG = "blocked_by_stale_catalog"
    FRESHNESS_UNKNOWN = "freshness_unknown"
    NOT_EVALUATED = "not_evaluated"
```

Shadow proves: every participating catalog is included, no non-participating one is, staleness is per-catalog, and unknown freshness never becomes `RESOLVABLE`.

**Safety staging (#19):** per-column universal-safety eligibility filters candidate columns **before** search (reuse `_safe_to_bind`); a plan carries `structural_status` (STRUCTURALLY_VALID) *and* a separate `safety_decision`; both are persisted; plan-level PIT/temporal validation runs after assembly. `VALID` never conflates "structurally valid" with "safe."

**Status enum:**

```python
class CrossCatalogPlanStatus(StrEnum):
    STRUCTURALLY_VALID = "structurally_valid"
    AMBIGUOUS = "ambiguous"
    RELATIONSHIP_CONFLICT = "relationship_conflict"
    UNSANCTIONED_BRIDGE = "unsanctioned_bridge"
    MISSING_REALIZATION = "missing_realization"
    SEARCH_LIMIT_REACHED = "search_limit_reached"
    UNKNOWN = "unknown"
```

**Replay envelope + idempotency (#20):**

```python
@dataclass(frozen=True)
class PlannerInputVersionSetV1:
    planner_version: str; graph_version: str; graph_fingerprint: str
    recipe_registry_version: str; need_metadata_version: str; concept_registry_version: str
    realization_derivation_version: str; bridge_snapshot_version: str
    catalog_snapshot_ids: tuple[str, ...]; catalog_scope_fingerprint: str
# idempotency: (generation_run_id, recipe_id, planner_input_hash) -> the existing record, never a duplicate
```

---

## 3B.4 — Shadow persistence + evaluation (#20 + objective gates)

- **Append-only, idempotent shadow store** (migration `0977`+) behind `FEATUREGEN_INTENT_CROSS_CATALOG_SHADOW` (default off, log-only): persists `CrossCatalogPlanningResultV1` with the full `PlannerInputVersionSetV1`; a repeated activity with the same `planner_input_hash` returns the existing record. No disposition/ranking change.
- **Expert gold set** as a **governed evaluation-data module** — versioned separately, unable to affect runtime planning, containing no sensitive production catalog metadata (a fixture/eval-only module, not production planning code).
- **Privileged review report** (`GET /admin/...` or an eval CLI) surfacing plans, candidate alternatives, diagnostics, and the bridge proposals awaiting sanction.
- **Eval module** computing binding recall, incorrect-path rate, ambiguity-detection (by reason code), unnecessary-bridge use, provenance completeness, freshness-participant completeness, bridge-proposal precision, realization-conflict rate, missing-metadata rate, override rate, tier distribution, stale/freshness-unknown rate, and search-limit rate.
- **Objective exit gates to 3C** (all required): zero false-`STRUCTURALLY_VALID` on designated governed/high-risk cases · zero unauthorized-catalog usage · zero unsanctioned bridge classified VALID-capable · 100% participating-catalog provenance completeness · 100% freshness-dependency completeness · 100% deterministic replay for pinned inputs · 100% ambiguity detection on the ambiguity subset · binding recall ≥ agreed threshold · incorrect-path rate ≤ agreed threshold · search-limit rate ≤ agreed threshold — plus a **recorded human review** of the gold set + surfaced bridge proposals.

---

## What 3B does NOT do (deferred to 3C / later)

Enforcement (turning non-VALID statuses into real rejections; flipping the deterministic lens on for multi-catalog runs; the disposition mapping); the review UIs (bridge ratification, realization-conflict, cross-catalog Gate #1); the multi-bridge-live decision; and anything row-level (the declared-metadata boundary is permanent).

## Named 3C decisions to record now

- **Enforcement fold-in** — status → disposition mapping; the flag that flips the deterministic lens on for entity-scoped multi-catalog runs.
- **Bridge ratification UX** — the event-sourced sanction, exposed with an authority/approval model (may reuse the deferred policy system's authority machinery).
- **Multi-bridge live** — offer tier-3 plans or cap at one bridge for v1.
- **`AMBIGUOUS` presentation + consumer handling** — how Gate #1 disambiguates a multi-plan; `ranking.py`/`contract.py` must handle the reserved `EntityCompatibility.AMBIGUOUS` when the planner surfaces multi-path plans live.
- **Catalog snapshot model** — realizations/bridges/plans reference `catalog_snapshot_ids`; 3C must decide how snapshots are minted + retained if they don't yet exist.

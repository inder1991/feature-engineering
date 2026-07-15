# Phase 3B.3b — Cross-Catalog Assembly: Governed Source→Target Physical Paths (Design)

> **Status:** Design — ready for planning after the user's spec review. Second planner increment of Phase 3B.3. Extends the 3B.3a vocabulary + frozen scope; adds the governed cross-catalog physical-path assembler (tier-1 fully-realized / tier-2 one-bridge / tier-3 multi-bridge). Shadow / behaviour-neutral / no migration (the durable store is 3B.4).
> **Builds on:** 3B.3a (planner core, contracts, `evaluate_binding_safety`, bounded/deterministic/candidate-local-first, frozen `CatalogScopeV1`), 3B.1 (`source_entity`/`source_entity_need_role`, `RESOLVED_NEED_METADATA` join roles), 3A (`resolve_entity_compatibility → EntitySemanticPathV1`), 3B.2A (`derive_catalog_realizations → CatalogEntityRelationshipV1`), 3B.2B (`active_bridges → ActiveBridgeV1`), 3B.3.0 (bridge freshness).
> **Convention:** `@dataclass(frozen=True, slots=True)`; lowercase `snake_case` `StrEnum`; canonical `BindingPlanV1` constructor (fields are computed, not static defaults). Extends the 3B.3a contracts additively — one vocabulary, no cross-catalog subtype.

## The model

A **cross-catalog plan = source-grain ingredient bindings + a governed physical roll-up path from the recipe's source entity to the confirmed target entity.** 3B.3a bound ingredients per catalog but **never evaluated a roll-up path**; 3B.3b enriches those source-catalog ingredient candidates into fully-realized tier-1 / tier-2 / tier-3 plans.

Worked example (acceptance): *"avg transaction value per customer over 90 days"* — ingredients (`transaction_amount`, `transaction_timestamp`) bind in `transactions` at source grain `transaction`; the roll-up `transaction → account → customer` is realized physically, crossing `transactions → accounts` at entity `account` through a governed bridge, then realizing `account → customer` in the accounts catalog.

## Invariants (bind every part of 3B.3b)

1. **Bounded FRONTIER search, never a greedy walk.** The assembler is a bounded state-space search (uniform-cost / breadth-first over bridge count), **not** "realize in the current catalog if possible, else cross." A locally-valid realization may dead-end on the next hop while another realization — or an earlier governed crossing — yields the only complete path. The search expands *all* deterministically-ordered permitted transitions from each state, within bounds.
2. **Exact physical-position continuity.** The physical position is `(entity_id, catalog_source, object_ref)` — not just the catalog. A realization is usable only when its source object is *continuous* with the current position (`(entity, catalog, object_ref)` equal); a bridge is usable only when one of its endpoints *exactly* matches the current position. This is what makes the output an **executable** physical join path, not a bag of individually-plausible relationships.
3. **Governed-bridge-only, fail-closed.** Crossings use *only* `active_bridges` (VERIFIED, in-scope); the permissive `find_cross_catalog_path` adjacency is never touched. A needed crossing with no verified bridge → `unsanctioned_bridge` (explicit, preserved as evidence), never a silent skip. A hop with no realization in scope → `missing_realization`. Both are fail-closed.
4. **Tier (structural) ≠ resolution completeness.** `PlanTier` is *purely* the bridge count. A separate `path_resolution_status` records whether the source→target roll-up was evaluated + completed. A zero-bridge realized plan is **new 3B.3b work**, distinct from a 3B.3a ingredient-only binding.
5. **Multi-grain ingredients rejected up-front.** A recipe whose required ingredients don't all resolve to the single declared source entity is rejected *before* path assembly with a capability code (`unsupported_multi_grain_ingredients`) — distinct from `missing_realization` (which is a governed-graph gap, not a planner-capability gap).
6. **Whole-tier completion before deeper expansion.** Enumerate *all* bounded k-bridge paths across *all* surviving source bindings before deciding whether to expand to k+1 bridges — never stop at the first valid path in iteration order. Prefer fewest bridges.
7. **Equal valid paths = resolved-with-ambiguity, not rejection.** A tie is a *selection* ambiguity: preserve one deterministic selected candidate + the equal-rank alternatives + the tie-break key; the result is `resolved_with_ambiguity`. (Whether 3C blocks ambiguous plans is a later policy call.)
8. **Frozen-scope confinement.** The search never introduces a catalog absent from the frozen `CatalogScopeV1`; `missing_realization`/`unsanctioned_bridge` are computed *within* the authorized set and never reveal inaccessible catalogs.
9. **Bounded + deterministic + candidate-local-first + declared-metadata-only** (inherited from 3B.3a): every expansion stage bounded (incl. a frontier-state bound), truncation recorded; a stable total order; a rejected alternative never downgrades a valid plan; no data plane.
10. **Shadow / behaviour-neutral / no migration.** Computed + logged on the same entity-scoped route path; changes no response, disposition, ranking, or live path. Per-recipe savepoint isolation (from 3B.3a) still applies.

---

## The frontier-search assembler

### Eligibility gate (before any path assembly)

For a recipe + confirmed `target_entity`:
- resolve the recipe's **single source entity** from `source_entity` / the `SOURCE_ENTITY_KEY` need via `RESOLVED_NEED_METADATA`. Require *exactly one* unambiguous source-entity declaration.
- every REQUIRED ingredient must resolve (through `RESOLVED_NEED_METADATA`) to that source entity, or be explicitly *entity-neutral* (a MEASURE/TIME need with no entity grain). If any required ingredient declares a *different* entity grain → the candidate is preserved as **`unsupported_multi_grain_ingredients`** and no path assembly is attempted. **The source entity is never inferred from whichever catalog happened to bind.**
- the semantic-path source must equal the resolved source entity; the destination the confirmed target entity.

If `source_entity == target_entity` → `resolve_entity_compatibility` returns EXACT → the roll-up path is empty (a fully-realized tier-1 plan with only the source segment). Otherwise the semantic path drives the search. `AMBIGUOUS` (≥2 *semantic* paths) is carried as distinct evidence (`ambiguous_semantic_path`, reserved) and each semantic path is searched; `UNKNOWN` → no reachable target → `source_to_target_rejected` with `missing_realization` at the graph level.

### Search state + transitions

Per source-binding candidate + semantic path, run a bounded frontier search. A state is:

```
(semantic_hop_index, current_entity_id, current_catalog_source, current_object_ref,
 selected_segments, bridge_count, participating_catalogs, used_bridge_fact_keys)
```

The initial state is the source-binding's physical position `(source_entity, source_catalog, source_object_ref)` at hop 0. From each state, expand **all** deterministically-ordered permitted transitions within bounds:

1. **Realize the next semantic hop in place** — an in-scope `CatalogEntityRelationshipV1` whose `(from_object_grain, to_object_grain)` matches the hop's `(from_entity, to_entity)` **and** whose `from_key_ref`/source object is *continuous* with the current position (same catalog + object). Emits `semantic_rollup(E1→E2)` + `intra_catalog_realization` segments; advances to the hop's `to` object; `semantic_hop_index += 1`.
2. **Traverse a governed active bridge at the current entity** — an `ActiveBridgeV1` with `entity_id == current_entity_id` where one endpoint *exactly* matches `(current_catalog_source, current_object_ref)`; cross to the *other* endpoint (endpoints are UNORDERED/symmetric — normalize). Emits a `governed_bridge` segment; `bridge_count += 1`; `used_bridge_fact_keys += fact_key`; entity unchanged, physical position moves to the other catalog/object. (Then the same or next hop can realize from the new position.)
3. **Dead-end** — no permitted transition continues the branch → preserve it as a rejected candidate with the precise reason (`missing_realization` or `unsanctioned_bridge`).

A state is **complete** when `semantic_hop_index == len(hops)` and `current_entity_id == target_entity` — a `source_to_target_resolved` path.

**Cycle prevention + bounds:** the visited key is `(current_entity_id, current_catalog_source, current_object_ref, frozenset(used_bridge_fact_keys))`; **the same bridge fact never appears twice** in one plan. Chained bridges (A→B→C at one entity) are allowed (the search + bounds support them). Bounds: `MAX_BRIDGES_PER_PLAN`, `MAX_REALIZATIONS_PER_HOP`, `MAX_PHYSICAL_PATHS_PER_BINDING`, and a **frontier bound** `MAX_STATES_EXPANDED_PER_BINDING` — on any hit, record the matching truncation flag; a result blocked only by truncation is `bounded_out`, never pretend-complete.

### Layered tier search (whole-tier completion)

```
1. Enumerate ALL bounded 0-bridge complete paths across ALL surviving source bindings.
2. If ≥1 valid: rank + preserve the tier-1 candidates; do NOT expand tier-2.
3. Else enumerate ALL bounded 1-bridge complete paths (across all bindings). If ≥1 valid: rank, stop.
4. Else continue to k=2..MAX_BRIDGES_PER_PLAN.
5. "Deeper tiers not explored because a lower valid tier existed" is RESULT METADATA, not a rejected candidate.
```

### Ranking precedence (deterministic total order)

```
1. candidate validity/safety      (resolved > partial/rejected; safe only)
2. bridge_count                    (a valid 0-bridge beats a valid 1-bridge, even with a weaker ingredient score)
3. ingredient-binding rank         (the 3B.3a binding-quality/completeness order)
4. semantic-path rank              (shorter / more-specific semantic path)
5. physical-realization rank       (realization authority: approved_join > declared_join)
6. canonical structural tie-break  (participating_catalogs, first object_ref, plan_id)
```

`≥2` complete paths tying on the entire key except the id → `resolved_with_ambiguity` + `ambiguous_equal_cross_catalog_paths`; one deterministic selected + the equal-rank alternatives preserved.

### Path-segment grammar (validated; invalid sequences cannot be built)

At a physical representation of `E1`, each semantic hop `E1 → E2` is:

```
[zero or more governed_bridge segments at E1]   # cross catalogs, entity unchanged
semantic_rollup(E1 → E2)                        # explanatory marker
intra_catalog_realization(E1/object → E2/object)  # the physical join evidence; advances the entity
```

Validated: each `semantic_rollup` matches exactly one semantic hop and is followed by its realization; `governed_bridge` segments preserve the entity and change the physical catalog/object; realizations advance the entity; segment continuity holds end-to-end; the final physical entity equals the confirmed target. `semantic_rollup` is explanatory (not independently executable); the `intra_catalog_realization` + `governed_bridge` segments carry the physical evidence — all three retained for audit.

---

## Contract additions (extend the 3B.3a backbone additively)

```python
class PlanTier(StrEnum):                    # PURELY structural (bridge count)
    tier_1_single_catalog = "tier_1_single_catalog"   # bridge_count == 0
    tier_2_one_bridge = "tier_2_one_bridge"           # bridge_count == 1
    tier_3_multi_bridge = "tier_3_multi_bridge"       # bridge_count >= 2

class PathResolutionStatus(StrEnum):        # SEPARATE from tier — was the roll-up evaluated + completed?
    ingredient_binding_only = "ingredient_binding_only"      # 3B.3a-style: bound, roll-up not evaluated
    source_to_target_resolved = "source_to_target_resolved"  # a complete governed physical path to target
    source_to_target_rejected = "source_to_target_rejected"  # roll-up attempted, no complete path in scope

class CandidateRole(StrEnum):               # for ambiguity + preservation
    selected = "selected"
    equal_rank_alternative = "equal_rank_alternative"
    lower_rank_alternative = "lower_rank_alternative"
    rejected = "rejected"
```

`BindingPlanV1` gains (validated additive fields; a canonical constructor populates them — `participating_catalogs` cannot be a static default since it depends on `catalog_source`):

```python
    participating_catalogs: tuple[str, ...]     # unique, ordered by first traversal; [0] == catalog_source
    bridge_count: int                            # == count of governed_bridge segments
    path_resolution_status: PathResolutionStatus
    candidate_role: CandidateRole
```

Structural validation (asserted at construction): `participating_catalogs[0] == catalog_source`; no duplicates; `bridge_count == #governed_bridge segments`; `tier == tier_from_bridge_count(bridge_count)`; every segment's `catalog_source ∈ participating_catalogs`; the path-segment grammar holds; the final physical entity `== target_entity` for a `source_to_target_resolved` plan.

**Serialization note (not byte-identical):** these additive fields change `BindingPlanV1`'s serialized shape → plan-hash / canonicalization may change. **Bump the canonicalization version** (a `PLAN_CONTRACT_VERSION`), and recompute `plan_id` over the new canonical content — do not attempt to preserve old bytes implicitly. Existing 3B.3a plan-shape tests are updated to the new fields (a tier-1 plan now also carries `participating_catalogs=(catalog_source,)`, `bridge_count=0`, `path_resolution_status` per whether the roll-up was evaluated).

`PlanResolutionStatus` gains `resolved_with_ambiguity = "resolved_with_ambiguity"`.

`ReasonCode` — the reserved codes go live + one new capability code:

```python
    # newly EMITTED (reserved in 3B.3a):
    unsanctioned_bridge, missing_realization, ambiguous_equal_cross_catalog_paths
    # NEW:
    unsupported_multi_grain_ingredients = "unsupported_multi_grain_ingredients"
    ambiguous_semantic_path = "ambiguous_semantic_path"   # >=2 semantic roll-up paths (distinct from physical)
    bounded_out_max_bridges = "bounded_out_max_bridges"
    bounded_out_max_realizations_per_hop = "bounded_out_max_realizations_per_hop"
    bounded_out_max_frontier_states = "bounded_out_max_frontier_states"
```

Bumps `REASON_CODE_REGISTRY_VERSION`.

Bound constants (`contracts.py`):

```python
MAX_BRIDGES_PER_PLAN = 2                       # tier-3 = 2 bridges for the shadow first cut (measured; multi-bridge-live is 3C/3D)
MAX_REALIZATIONS_PER_HOP = 4
MAX_PHYSICAL_PATHS_PER_BINDING = 16
MAX_STATES_EXPANDED_PER_BINDING = 512          # the frontier bound
```

`BoundingMetricsV1` gains: `realizations_truncated`, `bridge_transitions_truncated`, `frontier_states_truncated`, `deeper_tiers_not_explored: bool` (metadata — a lower valid tier existed), plus counts (`total_states_expanded`, `total_bridge_transitions_explored`).

### Reason-code semantics (precise)

- **`missing_realization`** — emitted only when *no* applicable realization exists **within the frozen authorized scope** for the semantic hop from any reachable physical state. Evidence: `semantic_hop_index, from_entity, to_entity, current_catalog_source, current_object_ref, catalogs_considered`. Never inspects inaccessible catalogs to distinguish "globally-exists-but-unauthorized" from "does-not-exist."
- **`unsanctioned_bridge`** — one or more candidate realizations exist in *another in-scope* catalog, but no VERIFIED active-bridge path connects the current physical representation of the current entity to those realizations within `MAX_BRIDGES_PER_PLAN`. Preserve the inaccessible realization as rejection evidence + name the missing governed transition; **do not fabricate a bridge segment.**
- **`ambiguous_equal_cross_catalog_paths`** — a *selection* ambiguity, not a safety reject; result is `resolved_with_ambiguity`, one deterministic selected + equal-rank alternatives + the tie-break key.

### Bridge traversal invariants

A bridge is usable only when: it is active + VERIFIED; **both** endpoint catalogs ∈ frozen `CatalogScopeV1`; it is visible under the same frozen read scope; the current `(entity_id, catalog_source, object_ref)` *exactly* matches one endpoint; and its state is covered by the replay envelope. Endpoints are **UNORDERED/symmetric** (per 3B.2B) — normalize; do not depend on `left`/`right` storage. A bridge **never introduces a catalog absent from the frozen set**. The same bridge fact never appears twice in a plan; chained bridges are cycle-prevented by the visited-state key.

### Replay envelope additions

3B.3b depends on more mutable state, so `PlannerReplayEnvelopeV1` pins the additional inputs (a replay is not `strong` — and stays `conditional` — if bridge visibility or semantic compatibility is recomputed from current state):

```python
    bridge_projection_version: str            # BRIDGE_DERIVATION_VERSION (already present) — now load-bearing
    active_bridge_fact_keys: tuple[str, ...]   # the sanctioned bridges visible in the frozen scope at plan time
    plan_contract_version: str                 # the canonicalization/plan-hash version (bumped)
```

(`graph_version`, `realization_derivation_version`, `need_metadata_version`, `catalog_scope` with its policy versions are already in the 3B.3a envelope.)

---

## Mandatory adversarial tests

1. **Acceptance path** — `transaction → account → customer` with one bridge → a `tier_2_one_bridge`, `source_to_target_resolved` plan whose segments match the grammar.
2. **Zero-bridge roll-up** — a single-catalog `transaction → customer` roll-up → a fully-realized `tier_1`, `source_to_target_resolved` plan (proving 3B.3b creates realized tier-1, which 3B.3a did not).
3. **Non-greedy dead-end** — a current-catalog realization that dead-ends on the next hop while a bridge-first path completes → the assembler finds the complete path (proves it is not greedy).
4. **Wrong-object_ref bridge** — a bridge with the right `entity_id` but an endpoint object_ref ≠ the current position → rejected (not traversed).
5. **Reverse-orientation bridge** — the current position matches the bridge's *right* endpoint → still traversable (symmetric normalization).
6. **`unsanctioned_bridge`** — a realization exists in another in-scope catalog but no verified bridge connects → the code is emitted, the realization preserved as evidence, no bridge segment fabricated.
7. **`missing_realization`** — no realization for a hop in any in-scope catalog → the code + evidence; no inaccessible-catalog leakage.
8. **Ambiguity** — two equally-preferred complete paths → `resolved_with_ambiguity`, deterministic selected + equal-rank alternatives + tie-break key.
9. **Tier-2 prevents tier-3** — a valid one-bridge plan exists → no two-bridge expansion; `deeper_tiers_not_explored=True`.
10. **Multiple tier-2 across bindings** — two source bindings each yielding a one-bridge path → both enumerated before tier termination (whole-tier completion).
11. **Multi-grain reject** — a recipe with a required ingredient at a different entity grain → `unsupported_multi_grain_ingredients` before any path assembly.
12. **Bridge cycle** — a bridge topology that could loop → bounded termination, no repeated bridge fact.
13. **Determinism** — inputs (realizations, bridges, bindings) shuffled repeatedly → identical candidate ordering + selected plan.
14. **Out-of-scope catalog / bridge** — a catalog or bridge absent from the frozen scope → cannot participate and cannot leak into evidence.

Plus the 3B.3a behaviour-neutral proof (full suite green; the route change is still the same log-only savepoint-isolated block).

## Internal task shape (for the plan)

- **B1** — contract additions (`contracts.py`): `PlanTier` tiers, `PathResolutionStatus`, `CandidateRole`, `BindingPlanV1` additive fields + the canonical constructor + structural validation, `resolved_with_ambiguity`, the new reason codes, the bound constants, `BoundingMetricsV1` + `PlannerReplayEnvelopeV1` additions, the `PLAN_CONTRACT_VERSION` bump. Update 3B.3a plan-shape tests to the new fields.
- **B2** — the eligibility gate + source-entity resolution (`assembly.py` or extend `plan.py`): multi-grain rejection, single-source-entity resolution, the semantic-path fetch (`resolve_entity_compatibility`).
- **B3** — the frontier-search assembler (`assembly.py`): the state, the two transition kinds (realize-in-place, governed-bridge), exact object-ref continuity, cycle prevention, the frontier + physical-path + realizations-per-hop + bridges bounds, the segment grammar + validation.
- **B4** — the layered tier search + ranking + ambiguity classification + candidate-local-first result classification (extend `plan.py`/`order.py`): whole-tier completion, the ranking precedence, `resolved_with_ambiguity`, `deeper_tiers_not_explored`.
- **B5** — wire the assembler into `plan_bindings`/`run_shadow_planner` (still log-only, per-recipe savepoint-isolated), the replay-envelope additions, the behaviour-neutral proof, the 14 adversarial tests.

## What 3B.3b does NOT do (deferred)

Multi-grain / multi-branch ingredient assembly (independent physical positions per ingredient, join-order selection, cross-branch compatibility) → 3B.3c/3D. Aggregation + temporal *declarations* + freshness *resolvability* + safety *staging* + `strong` replay → 3B.3c. The durable append-only store + migration + eval gates → 3B.4. Enforcement / disposition change / live traversal / multi-bridge-live → 3C. True catalog snapshots (vs the watermark stamp) → 3C. Hard entity reject (`INCOMPATIBLE`) → 3D.

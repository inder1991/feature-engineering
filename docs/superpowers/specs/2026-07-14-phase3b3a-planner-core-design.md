# Phase 3B.3a — Cross-Catalog Binding Planner: Core + Single-Catalog Tier (Design)

> **Status:** Design — ready for planning after the user's spec review. First increment of Phase 3B.3 (the bounded deterministic cross-catalog binding planner). Establishes the contract / reason-code / resolution-status **vocabulary backbone** reused by 3B.3b (cross-catalog assembly), 3B.3c (declarations + resolvability + safety staging + replay), 3B.4 (shadow store + eval), and 3C (enforcement).
> **Parent:** `docs/superpowers/specs/2026-07-11-phase3b-cross-catalog-binding-design.md` (§3B.3). **Builds on:** 3B.1 `RESOLVED_NEED_METADATA`, 3A entity graph, 3B.2A realizations, 3B.2B `active_bridges`, 3B.3.0 bridge freshness wiring.
> **Convention:** contracts are `@dataclass(frozen=True, slots=True)`; enum values are lowercase `snake_case` `StrEnum` (matching the taxonomy contracts). NOT pydantic; NOT mixed casing.

## Vocabulary supersession (normative)

The 3B.3a contracts in this spec **supersede** the provisional `CrossCatalogCandidatePlanV1`, `CrossCatalogPlanningResultV1`, `PlanPathSegmentV1`, `CrossCatalogPlanStatus`, and `PlannerInputVersionSetV1` names from the parent 3B design. **Those names must not be introduced into production code.** The canonical vocabulary is: `BindingPlanV1`, `IngredientBindingV1`, `BindingPathSegmentV1`, `BindingPlanningResultV1`, `CatalogScopeV1`, `CatalogStateStampV1`, `PlannerReplayEnvelopeV1`, `ReplayStrength`, `PlanResolutionStatus`, `ReasonCode`. No aliases or compatibility shims — the platform does not yet persist the old names.

## What 3B.3a is (and is not)

**Is:** for an entity-scoped run (`build_considered_set`'s `catalog_source is None` branch), for each applicable recipe + the confirmed `target_entity`, over a **frozen** authorized catalog set, a new planner-owned module tries to bind the recipe's ingredients **within a single catalog** (tier-1), preserving every eligible candidate up to explicit bounds, ranking deterministically, classifying with a compact status + multi-scope reason codes, and **logging** the result in shadow. It also records a **differential** against the existing live single-catalog binder.

**Is NOT (deferred):** bridges / cross-catalog physical paths / semantic roll-ups (3B.3b); aggregation + temporal + freshness *resolvability* + safety *staging* + the replay-strength *elevation* to STRONG (3B.3c); the durable append-only shadow store + migration `0990` + evaluation gates (3B.4); any enforcement / disposition change / replacement of the live paths (3C). Tier-1 only: no bridge, no roll-up edge, no cross-catalog transition.

## Invariants (bind every part of 3B.3a)

1. **Shadow / behaviour-neutral / no flag needed to stay dormant.** The planner computes + logs on the entity-scoped branch that today produces no deterministic candidates; it **never alters** candidate generation, disposition, ranking, or the existing single-catalog / LLM paths. The full suite is byte-identical except the new planner's own tests.
2. **Authorization is resolved OUTSIDE the planner.** Dependency flow: *read-scope/authorization resolver → immutable `CatalogScopeV1` → planner*. The planner receives resolved catalog ids + state stamps as input and **never queries all catalogs and applies role logic internally**. This keeps authorization policy out of planning code and makes replay possible.
3. **Scope is frozen at run start.** Catalog discovery is resolved **once** and is **not repeated** during candidate enumeration, retries, or per-recipe planning. A catalog becoming readable / unreadable / updated mid-run does not change the run's considered set.
4. **Safety-gate floor.** The planner may add *stricter* eligibility, but must **never accept a binding the existing safety gate rejects**. Safety is consumed through a stable planner-facing boundary (`evaluate_binding_safety`), not the private `_safe_to_bind` directly.
5. **Candidate-local-first precedence (formal).** A conflict, rejection, or incompleteness associated only with an **unselected** candidate must not downgrade or invalidate a completed plan that does not depend on it. A plan's status is derived **exclusively** from its selected ingredient bindings and selected path segments — except for **result-level** conditions (truncation, planner failure, input-integrity failure). Recursively: an unused column never invalidates an ingredient binding; an unused ingredient candidate never invalidates a plan; an unused catalog never invalidates the planning result; a **global input-integrity failure** may invalidate the whole result.
6. **Bounded + deterministic.** Every enumeration stage has an explicit bound; on hitting it, truncation is **recorded** (a metric + a reason), never a pretend-complete result. Ordering is a total, stable key — never incidental DB order.
7. **Lifecycle status ≠ reason codes.** `resolution_status` is a compact terminal lifecycle classification; `ReasonCode`s explain *how* and attach at candidate / ingredient / catalog / plan / result scope. Detailed vocabulary evolves independently of the status state machine.
8. **Tier-1 structural closure.** Every 3B.3a `BindingPlanV1` has all bindings in one catalog, no bridge id, no roll-up edge, no cross-catalog transition, `tier = tier_1_single_catalog`, a single `direct_catalog` path segment, and an unambiguously derivable catalog id. 3B.3a must not implement any part of 3B.3b through loosely-typed path structures.
9. **Declared-metadata-only / NO data plane** (permanent). A plan is a definition; nothing is executed or row-inspected.

---

## Architecture

New package `src/featuregen/overlay/upload/planner/` (leaf; consumed by nothing live). Five conceptually distinct stages so rejection reasons stay attributable to their scope:

```
resolve scope (OUTSIDE planner)      → CatalogScopeV1 (frozen)
      │
      ▼
discover ingredient candidates       → per (recipe need, catalog): candidate columns
      → evaluate candidate-local eligibility  (role / grain / concept / SAFETY)   → IngredientCandidateV1[]
      → enumerate compatible combinations      (bounded product over needs, single catalog)
      → evaluate plan-local constraints        (all-required-needs-bound; tier-1 structural closure)
      → rank + bound plans                     (deterministic order; preserve up to bounds)
      → classify result                        (candidate-local-first precedence → status + reason codes)
      → differential vs ground_template        (shadow validation signal)
      → LOG (log-only; the durable store is 3B.4)
```

### Module layout (each file one responsibility)

| File | Responsibility |
|---|---|
| `planner/contracts.py` | All frozen-dataclass contracts + the enums (`PlanResolutionStatus`, `ReasonCode`, `PlanTier`, `BindingSafety`, `ReplayStrength`, `CatalogStateStampKind`, `GroundTemplateDiffOutcome`) + `PLANNER_VERSION` + `REASON_CODE_REGISTRY_VERSION` + the bound constants. **This is A1 — the backbone.** |
| `planner/scope.py` | `resolve_catalog_scope(conn, *, roles, target_entity, requested_sources=None, now) -> CatalogScopeV1` — the authorization/read-scope resolver that produces the FROZEN scope (the ONE place role logic + watermark stamping happen). Not called by the planner core; called by the entry point, passed in. |
| `planner/safety.py` | `evaluate_binding_safety(col) -> BindingSafety` — the stable planner-facing safety boundary; initially delegates to `_safe_to_bind`, with parity tests. |
| `planner/candidates.py` | **A2** — `discover_ingredient_candidates(conn, recipe_needs, catalog_source, scope) -> tuple[IngredientCandidateV1, ...]` + candidate-local eligibility (role/grain/concept/safety), each rejection carrying reason codes. |
| `planner/enumerate.py` | **A3** — bounded combination of per-need candidates into single-catalog `BindingPlanV1`s + plan-local constraint checks; records bounding metrics. |
| `planner/order.py` | **A4** — the deterministic total ordering + preference tiering + ambiguity detection. |
| `planner/plan.py` | The orchestrator: `plan_bindings(conn, *, recipe_id, needs, target_entity, scope, now) -> BindingPlanningResultV1` (single recipe) + result classification (candidate-local-first precedence) + the `ground_template` differential. |
| `planner/shadow.py` | **A5** — `run_shadow_planner(conn, *, eligible_recipe_ids, target_entity, roles, now)` invoked from the `catalog_source is None` branch of `build_considered_set`; iterates recipes, resolves scope once, logs results. Log-only. |

---

## Contracts

### Replay + catalog state stamps

```python
class ReplayStrength(StrEnum):
    """What can ACTUALLY be reproduced — not merely whether stamps were recorded."""
    strong = "strong"            # inputs, policy versions, catalog states, ordering, and bounds all pinned + reconstructable
    conditional = "conditional"  # inputs pinned, but >=1 external/mutable dependency (e.g. the mutable catalog graph under a watermark-only stamp) cannot be reconstructed exactly
    audit_only = "audit_only"    # explainable from persisted evidence; exact re-execution not guaranteed
    none = "none"                # required replay evidence missing

# 3B.3a plans are ALWAYS `conditional`: catalog state is a drift-watermark stamp (not a materialized
# snapshot), so the mutable graph cannot be exactly reconstructed, AND enumeration reads mutable
# graph_node state. `strong` requires 3C materialized-snapshot semantics. The planner NEVER claims
# `strong` while authorization was resolved from current roles or enumeration read unstamped mutable state.

class CatalogStateStampKind(StrEnum):
    drift_watermark = "drift_watermark"

@dataclass(frozen=True, slots=True)
class CatalogStateStampV1:
    """A replay MARKER, not a materialized catalog snapshot. Supports drift comparison + audit
    correlation, NOT exact graph reconstruction. True snapshot minting is deferred to 3C."""
    catalog_source: str
    head_seq: int                 # overlay_drift_watermark.head_seq (monotonic global_seq)
    last_completed_at: str        # ISO-8601 of overlay_drift_watermark.last_completed_at
    stamp_kind: CatalogStateStampKind = CatalogStateStampKind.drift_watermark
```

### Catalog scope (frozen, resolved outside the planner)

```python
class CatalogOmissionReason(StrEnum):
    no_usable_state_stamp = "no_usable_state_stamp"       # no drift watermark -> cannot stamp -> cannot replay
    catalog_consideration_bound = "catalog_consideration_bound"  # dropped by MAX_AUTHORIZED_CATALOGS_CONSIDERED

@dataclass(frozen=True, slots=True)
class OmittedCatalogV1:
    catalog_source: str
    reason: CatalogOmissionReason

@dataclass(frozen=True, slots=True)
class CatalogScopeV1:
    """The authorized, frozen search boundary. Resolved ONCE (scope.py) before planning; the planner
    core treats it as immutable input. Authorization is NOT reproducible from catalog watermarks alone,
    so the scope also pins the policy versions + the resolution boundary."""
    scope_id: str                                    # deterministic sha256 over the scope content (the frozen-scope identity)
    authorized_catalog_sources: tuple[str, ...]      # deterministically ordered (sorted); read-authorized + (3C) request-narrowed
    catalog_state_stamps: tuple[CatalogStateStampV1, ...]   # one per authorized catalog (same order)
    omitted_catalog_sources: tuple[OmittedCatalogV1, ...]   # authorized-but-unstamped / bound-dropped, with a reason
    read_scope_policy_version: str                   # the sensitivity/read-scope policy version in force
    role_resolution_version: str                     # identity/role-resolution version, or "unknown"
    resolved_at: str                                 # ISO-8601 boundary at which the scope was resolved
    catalog_consideration_truncated: bool            # the MAX_AUTHORIZED_CATALOGS bound was hit
```

`resolve_catalog_scope` (scope.py): reads the distinct `catalog_source`s in `graph_node` the run's `roles` can read (existing read-scope/sensitivity filter), sorts them deterministically, stamps each from `overlay_drift_watermark` (`head_seq`, `last_completed_at`); a catalog with **no** watermark is **omitted** with `no_usable_state_stamp` (it cannot be replay-stamped); applies `MAX_AUTHORIZED_CATALOGS_CONSIDERED` (dropping the deterministic tail, `catalog_consideration_truncated=True`, each dropped catalog recorded omitted); computes `scope_id` = sha256 over the ordered stamps + policy versions + `resolved_at`. **The planner core never calls this** — the entry point resolves it once and passes it in.

### Ingredient candidate + binding

```python
class BindingSafety(StrEnum):
    safe = "safe"
    unsafe = "unsafe"
    not_evaluated = "not_evaluated"

@dataclass(frozen=True, slots=True)
class IngredientCandidateV1:
    """One candidate column for one recipe need in one catalog, with its candidate-local eligibility
    verdict. Preserved (accepted OR rejected) up to the per-need bound."""
    recipe_id: str
    need_role: str                    # the RESOLVED_NEED_METADATA key (a need is identified by its role)
    concept: str                      # ResolvedNeedMetadataV1.concept
    required_grains: tuple[str, ...]  # allowed_source_grains ((),= unconstrained)
    join_role: str                    # JoinRole
    temporal_role: str                # TemporalRole
    catalog_source: str
    object_ref: str                   # the candidate column's object_ref (public.<table>.<column>)
    actual_source_grain: str | None   # entity of the bound object's is_grain column — the ACTUAL grain, DISTINCT from the concept's entity_link and the need's required grains
    binding_quality: BindingQuality   # the deterministic quality tier (below)
    eligible: bool
    safety: BindingSafety
    reason_codes: tuple[ReasonCode, ...]   # why eligible / why rejected (candidate-scope)

class BindingQuality(StrEnum):
    """Deterministic candidate quality tier (drives ordering; higher = better). Concept-name match is the
    strongest tier-1 signal; entity/grain fit refine it. 3B.3b adds authority-derived tiers for realized hops."""
    exact_concept = "exact_concept"
    grain_and_role_fit = "grain_and_role_fit"
    entity_tagged = "entity_tagged"
    weak = "weak"

@dataclass(frozen=True, slots=True)
class IngredientBindingV1:
    """A SELECTED ingredient candidate in a completed plan. Same fields as the candidate, minus the
    eligibility bookkeeping (a binding is by definition eligible + safe)."""
    recipe_id: str
    need_role: str
    concept: str
    required_grains: tuple[str, ...]
    join_role: str
    temporal_role: str
    bound_catalog_source: str
    bound_object_ref: str
    actual_source_grain: str | None
    binding_quality: BindingQuality
    safety: BindingSafety             # always `safe` in a completed plan (invariant #4)
    reason_codes: tuple[ReasonCode, ...]
```

### Path segment (tier-1 = a single direct segment)

```python
class SegmentKind(StrEnum):
    direct_catalog = "direct_catalog"          # tier-1: all ingredients already in this catalog; no traversal
    # RESERVED for 3B.3b (must not be produced in 3B.3a):
    intra_catalog_realization = "intra_catalog_realization"
    governed_bridge = "governed_bridge"
    semantic_rollup = "semantic_rollup"

@dataclass(frozen=True, slots=True)
class BindingPathSegmentV1:
    segment_kind: SegmentKind
    catalog_source: str
    from_entity: str | None = None
    to_entity: str | None = None
    realization_ref: str | None = None    # 3B.3b
    bridge_fact_key: str | None = None     # 3B.3b
    cardinality: str | None = None
    direction: str | None = None
    reason_codes: tuple[ReasonCode, ...] = ()
```

### The plan + the result

```python
class PlanTier(StrEnum):
    tier_1_single_catalog = "tier_1_single_catalog"
    # tier_2_one_bridge / tier_3_multi_bridge RESERVED for 3B.3b

class PlanResolutionStatus(StrEnum):
    """Compact terminal LIFECYCLE classification of a plan or a planning result. Detailed 'why' lives in
    reason codes, NOT here (invariant #7)."""
    resolved = "resolved"                     # a completed, selected, safe single-catalog plan exists
    partially_resolved = "partially_resolved" # some but not all required needs bound (candidate preserved for audit)
    unresolved = "unresolved"                 # no completed plan; see primary_reason_code
    safety_rejected = "safety_rejected"       # the only completable plan(s) required an unsafe binding
    not_applicable = "not_applicable"         # no authorized catalog / none contains the target entity
    bounded_out = "bounded_out"               # enumeration truncated before a completed plan could be confirmed
    internal_error = "internal_error"         # planner failure isolated to this recipe/catalog

@dataclass(frozen=True, slots=True)
class BindingPlanV1:
    plan_id: str                              # deterministic sha256 over (recipe_id, catalog, ordered bound object_refs, tier, planner_version)
    recipe_id: str
    target_entity: str | None
    tier: PlanTier                            # tier_1_single_catalog in 3B.3a
    catalog_source: str                       # the single catalog (unambiguously derivable — invariant #8)
    ingredient_bindings: tuple[IngredientBindingV1, ...]
    path_segments: tuple[BindingPathSegmentV1, ...]   # exactly one direct_catalog segment in 3B.3a
    resolution_status: PlanResolutionStatus
    primary_reason_code: ReasonCode | None    # the headline reason (a specific code, kept SEPARATE from the status)
    reason_codes: tuple[ReasonCode, ...]      # plan-scope reasons
    safety: BindingSafety                     # the plan's safety decision (structural completeness is the status; safety is separate)
    preference_rank: int                      # 0 = most-preferred among this result's candidates
    preference_reasons: tuple[str, ...]       # the ordering keys that placed it (audit of the deterministic sort)

@dataclass(frozen=True, slots=True)
class BoundingMetricsV1:
    """Truncation observability — counts retained even when the discarded candidates themselves are not."""
    candidate_columns_truncated: bool
    combinations_truncated: bool
    plans_truncated: bool
    catalog_consideration_truncated: bool     # mirrors CatalogScopeV1 (recorded here for the result-local view)
    total_candidate_columns_considered: int
    total_combinations_explored: int
    total_plans_preserved: int

class GroundTemplateDiffOutcome(StrEnum):
    live_binding_present_and_ranked_first = "live_binding_present_and_ranked_first"
    live_binding_present_but_ranked_lower = "live_binding_present_but_ranked_lower"
    live_binding_absent_due_to_new_constraint = "live_binding_absent_due_to_new_constraint"
    live_binding_absent_unexpectedly = "live_binding_absent_unexpectedly"
    live_path_had_no_binding = "live_path_had_no_binding"
    not_compared = "not_compared"             # the live binder needs a single catalog_source; on an entity-only run, per-catalog only

@dataclass(frozen=True, slots=True)
class GroundTemplateDiffV1:
    outcome: GroundTemplateDiffOutcome
    live_bound_object_refs: tuple[str, ...]
    planner_matched_plan_id: str | None

@dataclass(frozen=True, slots=True)
class PlannerReplayEnvelopeV1:
    """The full input-version envelope for ONE planning unit (run × recipe × frozen scope). Lives on the
    RESULT (shared by all its candidate plans), NOT duplicated per plan. `planner_input_hash` is the
    idempotency key the 3B.4 store dedupes on."""
    planner_version: str
    reason_code_registry_version: str
    applicability_mapping_version: str
    recipe_registry_version: str
    need_metadata_version: str                # NEED_METADATA_VERSION
    graph_version: str                        # entity-graph GRAPH_VERSION
    realization_derivation_version: str       # REALIZATION_DERIVATION_VERSION
    bridge_derivation_version: str            # BRIDGE_DERIVATION_VERSION (recorded even in tier-1 for a stable envelope shape)
    concept_registry_version: str
    catalog_scope: CatalogScopeV1             # the frozen scope (carries the state stamps + policy versions)
    replay_strength: ReplayStrength           # `conditional` in 3B.3a
    planner_input_hash: str                   # sha256 over the material inputs (versions + scope_id + recipe_id + target_entity); EXCLUDES wall-clock / logging-only fields

@dataclass(frozen=True, slots=True)
class BindingPlanningResultV1:
    run_id: str | None                        # generation_run_id (shadow may synthesize)
    recipe_id: str
    target_entity: str | None
    catalog_scope_id: str
    selected_plan_id: str | None              # the highest-preference `resolved` plan, else None
    candidate_plans: tuple[BindingPlanV1, ...]  # accepted + rejected + partial + bounded-out — ALL preserved (up to bounds)
    result_status: PlanResolutionStatus       # by candidate-local-first precedence: `resolved` if any candidate is resolved
    primary_reason_code: ReasonCode | None
    reason_codes: tuple[ReasonCode, ...]       # result-scope
    bounding: BoundingMetricsV1
    ground_template_diff: GroundTemplateDiffV1
    replay_envelope: PlannerReplayEnvelopeV1
```

### Reason-code registry (multi-scope; separate from status)

```python
class ReasonCode(StrEnum):
    # selection (plan/result scope)
    selected_best_single_catalog = "selected_best_single_catalog"
    ambiguous_multiple_equal_plans = "ambiguous_multiple_equal_plans"
    # candidate / ingredient scope
    no_role_compatible_column = "no_role_compatible_column"
    concept_mismatch = "concept_mismatch"
    grain_incompatible = "grain_incompatible"
    binding_safety_rejected = "binding_safety_rejected"
    # plan scope
    missing_required_need = "missing_required_need"
    # catalog / result scope
    catalog_missing_target_entity = "catalog_missing_target_entity"
    no_authorized_catalog = "no_authorized_catalog"
    catalog_omitted_no_state_stamp = "catalog_omitted_no_state_stamp"
    # bounding (result scope)
    bounded_out_max_candidate_columns = "bounded_out_max_candidate_columns"
    bounded_out_max_combinations = "bounded_out_max_combinations"
    bounded_out_max_plans = "bounded_out_max_plans"
    bounded_out_max_catalogs = "bounded_out_max_catalogs"
    # failure
    planner_internal_error = "planner_internal_error"
    # RESERVED for 3B.3c (declared here so the registry version is stable; NOT emitted in 3B.3a):
    missing_required_aggregation = "missing_required_aggregation"
    missing_temporal_declaration = "missing_temporal_declaration"
    freshness_requirement_unsatisfied = "freshness_requirement_unsatisfied"
    # RESERVED for 3B.3b:
    ambiguous_equal_cross_catalog_paths = "ambiguous_equal_cross_catalog_paths"
    unsanctioned_bridge = "unsanctioned_bridge"
    missing_realization = "missing_realization"
```

`REASON_CODE_REGISTRY_VERSION` bumps whenever a code's meaning changes (not on additive reserves). Reserved codes are declared now so 3B.3b/c extend the vocabulary without a registry-shape break.

---

## Bounded, deterministic enumeration

**Bounds (module constants in `contracts.py`; conservative defaults, tunable):**

```python
MAX_CANDIDATE_COLUMNS_PER_NEED_PER_CATALOG = 8
MAX_PARTIAL_COMBINATIONS                    = 256   # partial products explored per (recipe, catalog)
MAX_PLANS_PER_RECIPE                        = 32    # completed plans preserved per recipe (across catalogs)
MAX_AUTHORIZED_CATALOGS_CONSIDERED          = 16
```

**Deterministic total ordering** (candidates within a need, and completed plans within a result) — a stable key, never incidental DB order:

```
plans:      (tier, -binding_completeness, -aggregate_binding_quality, safety_rank,
             -grain_fit, catalog_source, first_bound_object_ref, recipe_id, plan_id)
candidates: (-binding_quality_rank, -grain_fit, safety_rank, object_ref)
```

Truncation happens **after** ordering (drop the deterministic tail), so the same inputs always keep the same head and record the same bounded-out counts. Any truncation sets the matching `BoundingMetricsV1` flag + emits the matching `bounded_out_*` reason code; a result whose *only* obstruction to a completed plan is truncation is `bounded_out`, never `unresolved` (honest, not pretend-complete).

**Ambiguity (tier-1):** if ≥2 completed plans tie on the entire ordering key, they are equally preferred → the result keeps both, `selected_plan_id` is the lowest `plan_id` (deterministic), and `ambiguous_multiple_equal_plans` is emitted. (Deterministically-separable alternatives are NOT ambiguous.)

---

## Result classification (candidate-local-first precedence)

1. Compute all completed + rejected + partial + bounded-out candidate plans (preserve all, up to bounds).
2. If **any** candidate plan is a completed, safe, single-catalog plan → `result_status = resolved`, `selected_plan_id` = its highest-preference member; rejected/partial/bounded alternatives are retained as candidates with their own reason codes and **do not** downgrade the result (invariant #5).
3. Else the result status is the honest reason no plan completed, by result-scope precedence: `safety_rejected` (a completable plan existed but required an unsafe binding) > `bounded_out` (truncation blocked confirmation) > `partially_resolved` (some required needs bound, some not) > `unresolved` (no candidate reached completion) > `not_applicable` (no authorized catalog / none contains the target entity). `primary_reason_code` carries the specific code.
4. A **result-level** condition — `internal_error` (planner failure, isolated per recipe/catalog) or an input-integrity failure — overrides candidate-local status.

---

## Scope-outcome distinctions (do not collapse to "no plan")

- **No authorized catalog** → `not_applicable`, `no_authorized_catalog`.
- **Authorized catalogs exist, none contains the target entity** → `not_applicable`, `catalog_missing_target_entity`.
- **Catalogs authorized but omitted (no state stamp / bound)** → surfaced in `CatalogScopeV1.omitted_catalog_sources`; if that omission left no usable catalog, `catalog_omitted_no_state_stamp` is emitted.
- **Target entity present but no ingredient candidates found** → `unresolved`, `no_role_compatible_column`.

These imply different operator actions, so they stay distinct.

---

## Safety boundary

```python
def evaluate_binding_safety(col) -> BindingSafety:
    """The stable planner-facing safety boundary. Initially delegates to templates._safe_to_bind
    (leakage_anchor + blocked sensitivities). Parity tests assert identical accept/reject for the same
    inputs. Invariant: the planner may add STRICTER eligibility, but a binding `_safe_to_bind` rejects is
    ALWAYS `unsafe` here — the planner can never accept it."""
```

Parity tests enumerate representative columns (a leakage anchor, a blocked-sensitivity column, an ordinary column, an untagged column, an unknown concept) and assert `evaluate_binding_safety` matches `_safe_to_bind`'s boolean.

---

## Shadow entry point (A5)

`build_considered_set` (contract/gate1.py) currently **skips deterministic grounding when `catalog_source is None`** (the entity-scoped / cross-catalog run). 3B.3a adds — on exactly that branch, only when a confirmed `target_entity` is present — a call to `run_shadow_planner(...)`:

```
single catalog_source          → existing grounding path (UNCHANGED)
catalog_source is None
  + confirmed target_entity     → resolve_catalog_scope() ONCE → for each eligible recipe:
                                     plan_bindings(...) → LOG the BindingPlanningResultV1
                                   (candidate generation, dispositions, ranking: UNCHANGED)
```

**Shadow contract:** the planner result is **computed + logged**, never used to alter the considered set. It answers *"could the deterministic planner have produced a single-catalog binding plan for this recipe under this target entity, and why / why not?"* — not *"should we use it?"*. A planner exception is caught, logged as `internal_error` for that recipe, and does not touch the response.

**Differential vs `ground_template`:** for a recipe the live binder can ground on a specific catalog, `plan_bindings` records whether the planner's candidate set contains the live-selected binding, as a `GroundTemplateDiffV1`. On an entity-only run the live binder needs one catalog, so the differential is computed **per authorized catalog** where the live binder can run (else `not_compared`). This turns behaviour-neutrality into a measured signal.

---

## Persistence / replay (3B.3a scope vs 3B.4)

3B.3a is **log-only** — it emits `BindingPlanningResultV1` to the logger; it adds **no migration and no table** (keeps 3B.3a behaviour-neutral). It **defines** the replay envelope + the idempotency key (`planner_input_hash`) + the version pins so the 3B.4 durable append-only store (migration `0990`) can satisfy these invariants by construction:

- retrying the same planning unit (same `planner_input_hash`) is idempotent → the store returns the existing record;
- a partial write can never appear as a completed result (the store writes one immutable row per unit);
- planner + reason-code registry versions are pinned in the envelope;
- ordering keys are reproducible (deterministic sort) and `preference_reasons` records them;
- bounded-out counts are retained (`BoundingMetricsV1`) even when discarded candidates are not;
- planner errors are isolated per recipe/catalog (`internal_error`);
- shadow persistence failure never alters live grounding (shadow is downstream + wrapped).

The natural 3B.4 uniqueness key: `(run_id, target_entity, recipe_id, planner_version, scope_id)` — i.e. `planner_input_hash`. **Open scope question for review:** whether to pull a minimal durable log into 3B.3a or keep the store entirely in 3B.4. Recommendation: keep the store in 3B.4 (this spec's default); 3B.3a stays log-only.

---

## Internal task shape (for the plan)

- **A1** — `contracts.py`: all contracts + enums + `PLANNER_VERSION`/`REASON_CODE_REGISTRY_VERSION` + bound constants + `evaluate_binding_safety` boundary (+ parity tests). The backbone; nothing depends on later tasks.
- **A2** — `candidates.py`: `discover_ingredient_candidates` + candidate-local eligibility (role/grain/concept/safety), consuming `RESOLVED_NEED_METADATA`, reusing read-scope column loading, per-need bound + truncation.
- **A3** — `enumerate.py`: bounded single-catalog combination into `BindingPlanV1`s + plan-local constraints (all-required-needs-bound, tier-1 structural closure) + `BoundingMetricsV1`.
- **A4** — `order.py`: deterministic total ordering + preference ranks/reasons + ambiguity detection.
- **A5** — `scope.py` + `plan.py` + `shadow.py`: scope resolution (frozen), result classification (candidate-local-first), the `ground_template` differential, and the log-only entry into `build_considered_set`.

Split A after A2 into 3B.3a1 (A1–A2) / 3B.3a2 (A3–A5) ONLY if it exceeds one plan; try one plan first.

## Testing focus

- **Contract + registry:** every enum value round-trips; reserved codes present; `planner_input_hash` stable + excludes wall-clock; `scope_id` deterministic.
- **Candidate-local-first precedence (adversarial):** a rejected/unsafe/bounded-out *alternative* never downgrades a `resolved` result; the recursive cases (unused column / candidate / catalog).
- **Bounded + deterministic:** exceeding each bound sets the flag + reason + `bounded_out` status; the same inputs produce byte-identical ordering + head after truncation.
- **Safety floor parity:** `evaluate_binding_safety` == `_safe_to_bind` on the representative set; a `_safe_to_bind`-rejected column is never in a completed plan.
- **Tier-1 structural closure:** every emitted plan is one catalog / no bridge / no roll-up / single `direct_catalog` segment.
- **Scope outcomes:** the four empty/partial distinctions map to the right status + reason.
- **Shadow neutrality:** on the `catalog_source is None` branch the response (considered set, dispositions, ranking) is byte-identical with and without the planner; a planner exception is contained.
- **Differential:** for a recipe groundable by `ground_template`, the diff outcome is correct.

## What 3B.3a does NOT do (deferred)

Bridges / cross-catalog physical paths / semantic roll-ups (3B.3b); aggregation + temporal + freshness *resolvability* + safety *staging* + `strong` replay (3B.3c); the durable shadow store + migration `0990` + eval gates (3B.4); enforcement / disposition change / live-path replacement (3C). No `INCOMPATIBLE`-style hard entity reject (3D).

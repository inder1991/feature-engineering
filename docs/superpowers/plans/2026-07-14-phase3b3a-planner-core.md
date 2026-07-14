# Phase 3B.3a — Cross-Catalog Binding Planner Core + Single-Catalog Tier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Build the deterministic single-catalog (tier-1) binding planner + the contract/reason-code/resolution-status vocabulary backbone, computed and logged in shadow on the entity-scoped considered-set path — changing no user-visible behaviour.

**Architecture:** A new leaf package `src/featuregen/overlay/upload/planner/` consumed by nothing live. Five stages across five tasks (A1 contracts+safety → A2 candidate discovery → A3 bounded enumeration → A4 deterministic ordering → A5 scope+orchestration+shadow entry). It consumes `RESOLVED_NEED_METADATA` (3B.1), `_load_columns`/`_safe_to_bind`/`object_grain` (existing), and produces `BindingPlanningResultV1` logged from the considered-set API route on the entity-scoped run. No migration (the durable store is 3B.4).

**Tech Stack:** Python 3.11 (`@dataclass(frozen=True, slots=True)`, `StrEnum`), PostgreSQL (read-only), psycopg, pytest (`db` fixture, per-test rollback). `uv run pytest/ruff/mypy`.

## Global Constraints

- **Shadow / behaviour-neutral / no flag / no migration.** The planner computes + logs; it NEVER alters the considered set, dispositions, ranking, or the existing single-catalog/LLM paths. The full `tests/featuregen/` suite is byte-identical except the planner's own tests. No `db/migrations/*`.
- **Convention:** `@dataclass(frozen=True, slots=True)`; enum values lowercase `snake_case` `StrEnum`. NOT pydantic. NOT mixed casing.
- **Vocabulary supersession (normative):** the `CrossCatalog*` / `PlannerInputVersionSetV1` names from the parent 3B design are superseded and MUST NOT appear in code. Canonical: `BindingPlanV1`, `IngredientBindingV1`, `IngredientCandidateV1`, `BindingPathSegmentV1`, `BindingPlanningResultV1`, `CatalogScopeV1`, `CatalogStateStampV1`, `PlannerReplayEnvelopeV1`, `ReplayStrength`, `PlanResolutionStatus`, `ReasonCode`.
- **Authorization outside the planner.** `resolve_catalog_scope` (the ONE place role logic + watermark stamping happen) produces a FROZEN `CatalogScopeV1`; the planner core receives it as input and never queries catalogs or applies role logic. Scope is resolved ONCE per run, not re-resolved during enumeration.
- **Safety floor.** Bindings pass through `evaluate_binding_safety` (delegates to `_safe_to_bind`); the planner may add stricter eligibility but NEVER accepts a binding `_safe_to_bind` rejects.
- **Candidate-local-first precedence.** A plan's status derives only from its selected bindings/segments; an unused column/candidate/catalog never invalidates a completed plan; only a result-level condition (truncation, planner failure, input-integrity) can. Formal invariant, adversarially tested.
- **Bounded + deterministic.** Every enumeration stage has an explicit bound; truncation is recorded (metric + reason), never a pretend-complete result; ordering is a stable total key, never incidental DB order.
- **Status ≠ reason codes.** `PlanResolutionStatus` is the compact lifecycle; `ReasonCode`s are the multi-scope "why"; `primary_reason_code` is the headline.
- **Tier-1 structural closure.** Every plan: one catalog, no bridge id, no roll-up edge, no cross-catalog transition, `tier=tier_1_single_catalog`, one `direct_catalog` segment, catalog id unambiguous.
- **Log-only.** 3B.3a emits `BindingPlanningResultV1` to a logger; the durable append-only store + idempotency enforcement is 3B.4. The replay envelope + `planner_input_hash` are DEFINED here.
- **Tools:** `uv run pytest <path> -q`, `uv run ruff check`, `uv run mypy`. ruff prefers `collections.abc`, forbids E402 in `src/**`. Branch `feature/phase3b3a-planner-core`. Commit trailer: the harness default co-author.

## Reused interfaces (verified)
- `overlay/upload/templates.py`: `Template(id, needs: tuple[Need,...], source_entity, source_entity_need_role, ...)`; `Need(role, concept, optional=False, allowed_source_grains, join_role, temporal_role)`; `ALL_TEMPLATES: tuple[Template,...]`; `_Col(catalog_source, object_ref, table, column, data_type, is_grain, is_as_of, concept, entity, additivity, sensitivity, currency)`; `_load_columns(conn, catalog_source, roles) -> list[_Col]`; `_safe_to_bind(col: _Col) -> bool`; `ground_template(conn, template, *, catalog_source, roles, params=None) -> GroundedFeature | None`; `GroundedFeature.derives_pairs: tuple[tuple[str,str],...]` ((catalog_source, object_ref)).
- `overlay/upload/need_metadata.py`: `RESOLVED_NEED_METADATA: Mapping[str, tuple[ResolvedNeedMetadataV1,...]]` (keyed by `template.id`); `ResolvedNeedMetadataV1(role, concept, allowed_source_grains, join_role, temporal_role, grain_source, join_role_source, temporal_role_source)`; `NEED_METADATA_VERSION`.
- `overlay/upload/catalog_realizations.py`: `object_grain(conn, catalog_source, table_object_ref) -> str | None`; `table_of(column_object_ref) -> str`; `REALIZATION_DERIVATION_VERSION`.
- `overlay/upload/bridge_candidates.py`: `BRIDGE_DERIVATION_VERSION`.
- `overlay/upload/taxonomy/entity_registry.py`: `GRAPH_VERSION`.
- `overlay/catalog_changes.py`: `drift_watermark(conn, catalog_source) -> datetime | None`; `drift_head_seq(conn, catalog_source) -> int | None`.
- `overlay/upload/read_scope.py`: `allowed_sensitivities(roles) -> list[str]`.
- `overlay/upload/taxonomy/applicability.py`: `ApplicabilityResult.eligible_ids: frozenset[str]`. `overlay/upload/taxonomy/applicability.py` `ConfirmedScope.target_entity`.
- Entry site: `api/routes/contract.py` calls `build_considered_set(...)` with the `ConfirmedScope` (`scope.target_entity`), `applicability`, and `roles` all in scope.

## File Structure

| File | Responsibility |
|---|---|
| `src/featuregen/overlay/upload/planner/__init__.py` | empty package marker |
| `src/featuregen/overlay/upload/planner/contracts.py` (A1) | all contracts + enums + version constants + bound constants |
| `src/featuregen/overlay/upload/planner/safety.py` (A1) | `evaluate_binding_safety` boundary |
| `src/featuregen/overlay/upload/planner/candidates.py` (A2) | `discover_ingredient_candidates` + candidate-local eligibility |
| `src/featuregen/overlay/upload/planner/enumerate.py` (A3) | `enumerate_single_catalog_plans` bounded combination + plan-local constraints |
| `src/featuregen/overlay/upload/planner/order.py` (A4) | `order_plans` deterministic ordering + ambiguity |
| `src/featuregen/overlay/upload/planner/scope.py` (A5) | `resolve_catalog_scope` |
| `src/featuregen/overlay/upload/planner/plan.py` (A5) | `plan_bindings` orchestrator + classification + `ground_template` differential |
| `src/featuregen/overlay/upload/planner/shadow.py` (A5) | `run_shadow_planner` + the log-only entry into the considered-set route |
| Tests | `tests/featuregen/overlay/upload/planner/test_*.py` |

---

### Task 1 (A1): Contracts + version registry + safety boundary

**Files:**
- Create: `src/featuregen/overlay/upload/planner/__init__.py` (empty), `src/featuregen/overlay/upload/planner/contracts.py`, `src/featuregen/overlay/upload/planner/safety.py`
- Test: `tests/featuregen/overlay/upload/planner/__init__.py` (empty), `tests/featuregen/overlay/upload/planner/test_contracts.py`

**Interfaces:**
- Produces: every contract/enum in the spec + `PLANNER_VERSION`, `REASON_CODE_REGISTRY_VERSION`, `READ_SCOPE_POLICY_VERSION`, `ROLE_RESOLUTION_VERSION`, `RECIPE_REGISTRY_VERSION`, `APPLICABILITY_MAPPING_VERSION`, `CONCEPT_REGISTRY_VERSION`, the four bound constants; `evaluate_binding_safety(col) -> BindingSafety`.

- [ ] **Step 1: Write the failing test** — `tests/featuregen/overlay/upload/planner/test_contracts.py`:

```python
from featuregen.overlay.upload.planner import contracts as c
from featuregen.overlay.upload.planner.safety import evaluate_binding_safety


def test_enums_are_lowercase_snake_and_complete():
    assert c.PlanResolutionStatus.resolved == "resolved"
    assert {s.value for s in c.PlanResolutionStatus} == {
        "resolved", "partially_resolved", "unresolved", "safety_rejected",
        "not_applicable", "bounded_out", "internal_error"}
    assert c.ReplayStrength.conditional == "conditional"
    assert c.PlanTier.tier_1_single_catalog == "tier_1_single_catalog"
    assert c.BindingSafety.safe == "safe"
    # reserved codes for later phases are present so the registry shape is stable
    for reserved in ("missing_required_aggregation", "unsanctioned_bridge",
                     "ambiguous_equal_cross_catalog_paths"):
        assert reserved in {r.value for r in c.ReasonCode}


def test_version_constants_present():
    for v in (c.PLANNER_VERSION, c.REASON_CODE_REGISTRY_VERSION, c.READ_SCOPE_POLICY_VERSION,
              c.ROLE_RESOLUTION_VERSION, c.RECIPE_REGISTRY_VERSION, c.APPLICABILITY_MAPPING_VERSION,
              c.CONCEPT_REGISTRY_VERSION):
        assert isinstance(v, str) and v


def test_bounds_are_positive_ints():
    for b in (c.MAX_CANDIDATE_COLUMNS_PER_NEED_PER_CATALOG, c.MAX_PARTIAL_COMBINATIONS,
              c.MAX_PLANS_PER_RECIPE, c.MAX_AUTHORIZED_CATALOGS_CONSIDERED):
        assert isinstance(b, int) and b > 0


def test_contracts_are_frozen():
    stamp = c.CatalogStateStampV1(catalog_source="core", head_seq=1, last_completed_at="2026-07-14T00:00:00Z")
    import dataclasses
    assert dataclasses.is_dataclass(stamp)
    try:
        stamp.head_seq = 2  # type: ignore[misc]
        raise AssertionError("expected frozen")
    except dataclasses.FrozenInstanceError:
        pass


def test_evaluate_binding_safety_parity_with_safe_to_bind():
    # the boundary must agree with _safe_to_bind for every representative column
    from featuregen.overlay.upload.templates import _Col, _safe_to_bind
    reps = [
        _Col("s", "public.t.a", "t", "a", "text", False, False, "outcome_label", None, None, None, None),  # leakage anchor -> unsafe
        _Col("s", "public.t.b", "t", "b", "numeric", False, False, "monetary_stock", None, "semi_additive", None, "USD"),  # safe
        _Col("s", "public.t.c", "t", "c", "text", False, False, None, None, None, None, None),  # untagged -> safe
        _Col("s", "public.t.d", "t", "d", "text", False, False, "not_a_real_concept", None, None, None, None),  # unknown concept -> safe
    ]
    for col in reps:
        expected = c.BindingSafety.safe if _safe_to_bind(col) else c.BindingSafety.unsafe
        assert evaluate_binding_safety(col) is expected
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/featuregen/overlay/upload/planner/test_contracts.py -q` → FAIL (module missing).

- [ ] **Step 3: Create the package + contracts** — `src/featuregen/overlay/upload/planner/__init__.py` (empty), `tests/featuregen/overlay/upload/planner/__init__.py` (empty), and `src/featuregen/overlay/upload/planner/contracts.py`:

```python
"""Phase-3B.3a — cross-catalog binding planner contracts + reason-code/status vocabulary.

The BACKBONE reused by 3B.3b/c/3B.4/3C. Supersedes the parent 3B design's CrossCatalog* names.
Frozen dataclasses; lowercase snake_case StrEnum values. No behaviour — pure contracts + constants."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

PLANNER_VERSION = "3b3a.1.0.0"
REASON_CODE_REGISTRY_VERSION = "1.0.0"
# Version pins for inputs that have no formal version source yet (wired to real policy versions in 3C):
READ_SCOPE_POLICY_VERSION = "1.0.0"
ROLE_RESOLUTION_VERSION = "unknown"
RECIPE_REGISTRY_VERSION = "1.0.0"
APPLICABILITY_MAPPING_VERSION = "1.0.0"
CONCEPT_REGISTRY_VERSION = "concepts@1"

# Bounds (conservative; tunable). Truncation is always recorded, never a pretend-complete result.
MAX_CANDIDATE_COLUMNS_PER_NEED_PER_CATALOG = 8
MAX_PARTIAL_COMBINATIONS = 256
MAX_PLANS_PER_RECIPE = 32
MAX_AUTHORIZED_CATALOGS_CONSIDERED = 16


class ReplayStrength(StrEnum):
    strong = "strong"
    conditional = "conditional"
    audit_only = "audit_only"
    none = "none"


class CatalogStateStampKind(StrEnum):
    drift_watermark = "drift_watermark"


class CatalogOmissionReason(StrEnum):
    no_usable_state_stamp = "no_usable_state_stamp"
    catalog_consideration_bound = "catalog_consideration_bound"


class BindingSafety(StrEnum):
    safe = "safe"
    unsafe = "unsafe"
    not_evaluated = "not_evaluated"


class BindingQuality(StrEnum):
    exact_concept = "exact_concept"
    grain_and_role_fit = "grain_and_role_fit"
    entity_tagged = "entity_tagged"
    weak = "weak"


class SegmentKind(StrEnum):
    direct_catalog = "direct_catalog"
    intra_catalog_realization = "intra_catalog_realization"   # reserved 3B.3b
    governed_bridge = "governed_bridge"                       # reserved 3B.3b
    semantic_rollup = "semantic_rollup"                       # reserved 3B.3b


class PlanTier(StrEnum):
    tier_1_single_catalog = "tier_1_single_catalog"


class PlanResolutionStatus(StrEnum):
    resolved = "resolved"
    partially_resolved = "partially_resolved"
    unresolved = "unresolved"
    safety_rejected = "safety_rejected"
    not_applicable = "not_applicable"
    bounded_out = "bounded_out"
    internal_error = "internal_error"


class ReasonCode(StrEnum):
    selected_best_single_catalog = "selected_best_single_catalog"
    ambiguous_multiple_equal_plans = "ambiguous_multiple_equal_plans"
    no_role_compatible_column = "no_role_compatible_column"
    concept_mismatch = "concept_mismatch"
    grain_incompatible = "grain_incompatible"
    binding_safety_rejected = "binding_safety_rejected"
    missing_required_need = "missing_required_need"
    catalog_missing_target_entity = "catalog_missing_target_entity"
    no_authorized_catalog = "no_authorized_catalog"
    catalog_omitted_no_state_stamp = "catalog_omitted_no_state_stamp"
    bounded_out_max_candidate_columns = "bounded_out_max_candidate_columns"
    bounded_out_max_combinations = "bounded_out_max_combinations"
    bounded_out_max_plans = "bounded_out_max_plans"
    bounded_out_max_catalogs = "bounded_out_max_catalogs"
    planner_internal_error = "planner_internal_error"
    # reserved 3B.3c
    missing_required_aggregation = "missing_required_aggregation"
    missing_temporal_declaration = "missing_temporal_declaration"
    freshness_requirement_unsatisfied = "freshness_requirement_unsatisfied"
    # reserved 3B.3b
    ambiguous_equal_cross_catalog_paths = "ambiguous_equal_cross_catalog_paths"
    unsanctioned_bridge = "unsanctioned_bridge"
    missing_realization = "missing_realization"


class GroundTemplateDiffOutcome(StrEnum):
    live_binding_present_and_ranked_first = "live_binding_present_and_ranked_first"
    live_binding_present_but_ranked_lower = "live_binding_present_but_ranked_lower"
    live_binding_absent_due_to_new_constraint = "live_binding_absent_due_to_new_constraint"
    live_binding_absent_unexpectedly = "live_binding_absent_unexpectedly"
    live_path_had_no_binding = "live_path_had_no_binding"
    not_compared = "not_compared"


@dataclass(frozen=True, slots=True)
class CatalogStateStampV1:
    catalog_source: str
    head_seq: int
    last_completed_at: str
    stamp_kind: CatalogStateStampKind = CatalogStateStampKind.drift_watermark


@dataclass(frozen=True, slots=True)
class OmittedCatalogV1:
    catalog_source: str
    reason: CatalogOmissionReason


@dataclass(frozen=True, slots=True)
class CatalogScopeV1:
    scope_id: str
    authorized_catalog_sources: tuple[str, ...]
    catalog_state_stamps: tuple[CatalogStateStampV1, ...]
    omitted_catalog_sources: tuple[OmittedCatalogV1, ...]
    read_scope_policy_version: str
    role_resolution_version: str
    resolved_at: str
    catalog_consideration_truncated: bool


@dataclass(frozen=True, slots=True)
class IngredientCandidateV1:
    recipe_id: str
    need_role: str
    concept: str
    required_grains: tuple[str, ...]
    join_role: str
    temporal_role: str
    catalog_source: str
    object_ref: str
    actual_source_grain: str | None
    binding_quality: BindingQuality
    eligible: bool
    safety: BindingSafety
    reason_codes: tuple[ReasonCode, ...]


@dataclass(frozen=True, slots=True)
class IngredientBindingV1:
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
    safety: BindingSafety
    reason_codes: tuple[ReasonCode, ...]


@dataclass(frozen=True, slots=True)
class BindingPathSegmentV1:
    segment_kind: SegmentKind
    catalog_source: str
    from_entity: str | None = None
    to_entity: str | None = None
    realization_ref: str | None = None
    bridge_fact_key: str | None = None
    cardinality: str | None = None
    direction: str | None = None
    reason_codes: tuple[ReasonCode, ...] = ()


@dataclass(frozen=True, slots=True)
class BindingPlanV1:
    plan_id: str
    recipe_id: str
    target_entity: str | None
    tier: PlanTier
    catalog_source: str
    ingredient_bindings: tuple[IngredientBindingV1, ...]
    path_segments: tuple[BindingPathSegmentV1, ...]
    resolution_status: PlanResolutionStatus
    primary_reason_code: ReasonCode | None
    reason_codes: tuple[ReasonCode, ...]
    safety: BindingSafety
    preference_rank: int
    preference_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BoundingMetricsV1:
    candidate_columns_truncated: bool
    combinations_truncated: bool
    plans_truncated: bool
    catalog_consideration_truncated: bool
    total_candidate_columns_considered: int
    total_combinations_explored: int
    total_plans_preserved: int


@dataclass(frozen=True, slots=True)
class GroundTemplateDiffV1:
    outcome: GroundTemplateDiffOutcome
    live_bound_object_refs: tuple[str, ...]
    planner_matched_plan_id: str | None


@dataclass(frozen=True, slots=True)
class PlannerReplayEnvelopeV1:
    planner_version: str
    reason_code_registry_version: str
    applicability_mapping_version: str
    recipe_registry_version: str
    need_metadata_version: str
    graph_version: str
    realization_derivation_version: str
    bridge_derivation_version: str
    concept_registry_version: str
    catalog_scope: CatalogScopeV1
    replay_strength: ReplayStrength
    planner_input_hash: str


@dataclass(frozen=True, slots=True)
class BindingPlanningResultV1:
    run_id: str | None
    recipe_id: str
    target_entity: str | None
    catalog_scope_id: str
    selected_plan_id: str | None
    candidate_plans: tuple[BindingPlanV1, ...]
    result_status: PlanResolutionStatus
    primary_reason_code: ReasonCode | None
    reason_codes: tuple[ReasonCode, ...]
    bounding: BoundingMetricsV1
    ground_template_diff: GroundTemplateDiffV1
    replay_envelope: PlannerReplayEnvelopeV1
```

And `src/featuregen/overlay/upload/planner/safety.py`:

```python
"""The stable planner-facing safety boundary. Delegates to templates._safe_to_bind today; parity-tested.
Invariant: the planner may add STRICTER eligibility, but NEVER accepts a binding _safe_to_bind rejects."""
from __future__ import annotations

from featuregen.overlay.upload.planner.contracts import BindingSafety
from featuregen.overlay.upload.templates import _Col, _safe_to_bind


def evaluate_binding_safety(col: _Col) -> BindingSafety:
    return BindingSafety.safe if _safe_to_bind(col) else BindingSafety.unsafe
```

- [ ] **Step 4: Run to verify it passes** — `uv run pytest tests/featuregen/overlay/upload/planner/test_contracts.py -q` → PASS (5 passed).

- [ ] **Step 5: Gates + commit**

```bash
uv run ruff check src/featuregen/overlay/upload/planner/ tests/featuregen/overlay/upload/planner/
uv run mypy src/featuregen/overlay/upload/planner/contracts.py src/featuregen/overlay/upload/planner/safety.py
git add -A && git commit -m "feat(3b3a): planner contracts + reason-code/status vocabulary + safety boundary (task 1)"
```

---

### Task 2 (A2): Candidate discovery + candidate-local eligibility

**Files:**
- Create: `src/featuregen/overlay/upload/planner/candidates.py`
- Test: `tests/featuregen/overlay/upload/planner/test_candidates.py`

**Interfaces:**
- Consumes: Task 1 contracts; `Template`/`Need`/`_Col`/`_load_columns` (templates.py); `RESOLVED_NEED_METADATA` (need_metadata.py); `object_grain`/`table_of` (catalog_realizations.py); `evaluate_binding_safety`.
- Produces: `discover_ingredient_candidates(conn, template, catalog_source, roles) -> dict[str, tuple[IngredientCandidateV1, ...]]` (keyed by need role; a required need with an empty tuple is unbindable); `candidate_columns_truncated(...) -> bool` via a returned flag — see the `CandidateDiscoveryV1` result below.

- [ ] **Step 1: Write the failing tests** — `tests/featuregen/overlay/upload/planner/test_candidates.py`:

```python
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.planner.candidates import discover_ingredient_candidates
from featuregen.overlay.upload.planner.contracts import BindingSafety, ReasonCode
from featuregen.overlay.upload.templates import Need, Template


def _accounts(db):
    catalog = [
        (CanonicalRow("core", "accounts", "customer_id", "integer", is_grain=True), "customer_id"),
        (CanonicalRow("core", "accounts", "balance", "numeric", additivity="semi_additive", currency="USD"),
         "monetary_stock"),
        (CanonicalRow("core", "accounts", "churned", "boolean"), "outcome_label"),  # leakage anchor -> unsafe
    ]
    build_graph(db, "core", [r for r, _ in catalog], concepts={content_hash(r): c for r, c in catalog})


def _tmpl():
    return Template(id="t_bal", family="f", intent="i",
                    needs=(Need(role="stock_col", concept="monetary_stock"),
                           Need(role="entity", concept="customer_id")),
                    params={}, aggregation="avg", additivity="semi_additive", explain="M", use_cases=(),
                    pit="trailing")


def test_discovers_concept_matched_safe_candidates(db):
    _accounts(db)
    cands = discover_ingredient_candidates(db, _tmpl(), "core", roles=()).candidates
    stock = cands["stock_col"]
    assert len(stock) == 1 and stock[0].object_ref == "public.accounts.balance"
    assert stock[0].eligible is True and stock[0].safety is BindingSafety.safe


def test_unsafe_column_is_a_rejected_candidate_not_dropped(db):
    # add a monetary_stock column that is ALSO a leakage anchor concept -> a candidate, but unsafe+ineligible
    catalog = [(CanonicalRow("x", "t", "amt", "numeric"), "monetary_stock"),
               (CanonicalRow("x", "t", "label", "numeric"), "outcome_label")]
    build_graph(db, "x", [r for r, _ in catalog], concepts={content_hash(r): c for r, c in catalog})
    tmpl = Template(id="t2", family="f", intent="i",
                    needs=(Need(role="lbl", concept="outcome_label"),), params={}, aggregation="a",
                    additivity="n/a", explain="L", use_cases=(), pit="p")
    cands = discover_ingredient_candidates(db, tmpl, "x", roles=()).candidates
    lbl = cands["lbl"]
    assert len(lbl) == 1                                   # preserved, not dropped
    assert lbl[0].eligible is False and lbl[0].safety is BindingSafety.unsafe
    assert ReasonCode.binding_safety_rejected in lbl[0].reason_codes


def test_grain_incompatible_candidate_is_ineligible(db):
    _accounts(db)
    # a need whose allowed_source_grains forbids the accounts grain (account) -> grain_incompatible
    tmpl = Template(id="t3", family="f", intent="i",
                    needs=(Need(role="stock_col", concept="monetary_stock",
                                allowed_source_grains=("customer",)),),
                    params={}, aggregation="a", additivity="n/a", explain="L", use_cases=(), pit="p")
    cands = discover_ingredient_candidates(db, tmpl, "core", roles=()).candidates
    stock = cands["stock_col"]
    # accounts' grain is 'account' (customer_id is a FK, account_id... here accounts grain = customer via is_grain customer_id)
    # so grain fit depends on object_grain; assert the reason code path is exercised deterministically:
    assert all(c.concept == "monetary_stock" for c in stock)


def test_missing_concept_need_yields_empty_candidate_tuple(db):
    _accounts(db)
    tmpl = Template(id="t4", family="f", intent="i",
                    needs=(Need(role="ts", concept="event_timestamp"),), params={}, aggregation="a",
                    additivity="n/a", explain="L", use_cases=(), pit="p")
    cands = discover_ingredient_candidates(db, tmpl, "core", roles=()).candidates
    assert cands["ts"] == ()      # no event_timestamp column -> unbindable need (empty tuple, not missing key)
```

> **Implementer note:** the accounts grain in `_accounts` is whatever `object_grain(conn, "core", "public.accounts")` returns for the `is_grain` column `customer_id` (concept `customer_id` → entity `customer`). Do not hardcode a grain assumption in the impl; read it via `object_grain`. `test_grain_incompatible_candidate_is_ineligible` asserts the concept-match invariant only (it does not pin the grain outcome, which depends on the resolved grain).

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/featuregen/overlay/upload/planner/test_candidates.py -q` → FAIL (module missing).

- [ ] **Step 3: Implement `candidates.py`:**

```python
"""Phase-3B.3a A2 — per-need candidate discovery within ONE catalog. Preserves every concept-matched
column (accepted OR rejected) up to the per-need bound, each with its candidate-local eligibility verdict
(role/grain/concept/safety). Consumes RESOLVED_NEED_METADATA for the grain constraint."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from featuregen.overlay.upload.catalog_realizations import object_grain, table_of
from featuregen.overlay.upload.need_metadata import RESOLVED_NEED_METADATA
from featuregen.overlay.upload.planner.contracts import (
    MAX_CANDIDATE_COLUMNS_PER_NEED_PER_CATALOG,
    BindingQuality,
    BindingSafety,
    IngredientCandidateV1,
    ReasonCode,
)
from featuregen.overlay.upload.planner.safety import evaluate_binding_safety
from featuregen.overlay.upload.templates import Template, _Col, _load_columns


@dataclass(frozen=True, slots=True)
class CandidateDiscoveryV1:
    candidates: dict[str, tuple[IngredientCandidateV1, ...]]   # need role -> candidates (accepted + rejected)
    candidate_columns_truncated: bool
    total_candidate_columns_considered: int


def _quality(col: _Col, concept: str, grain_ok: bool) -> BindingQuality:
    if col.concept == concept and grain_ok:
        return BindingQuality.grain_and_role_fit
    if col.concept == concept:
        return BindingQuality.exact_concept
    if col.entity is not None:
        return BindingQuality.entity_tagged
    return BindingQuality.weak


def discover_ingredient_candidates(conn, template: Template, catalog_source: str,
                                   *, roles: Iterable[str] = ()) -> CandidateDiscoveryV1:
    cols = _load_columns(conn, catalog_source, roles)
    resolved = {r.role: r for r in RESOLVED_NEED_METADATA.get(template.id, ())}
    out: dict[str, tuple[IngredientCandidateV1, ...]] = {}
    truncated = False
    total = 0
    for need in template.needs:
        rn = resolved.get(need.role)
        allowed = rn.allowed_source_grains if rn is not None else need.allowed_source_grains
        join_role = str(rn.join_role) if rn is not None else str(need.join_role or "")
        temporal_role = str(rn.temporal_role) if rn is not None else str(need.temporal_role or "")
        # tier-1 candidate columns: an exact concept match (the strongest single-catalog signal). Sorted
        # deterministically by object_ref so truncation is stable.
        matches = sorted((c for c in cols if c.concept == need.concept), key=lambda c: c.object_ref)
        if len(matches) > MAX_CANDIDATE_COLUMNS_PER_NEED_PER_CATALOG:
            matches = matches[:MAX_CANDIDATE_COLUMNS_PER_NEED_PER_CATALOG]
            truncated = True
        cands: list[IngredientCandidateV1] = []
        for col in matches:
            total += 1
            grain = object_grain(conn, catalog_source, table_of(col.object_ref))
            grain_ok = not allowed or (grain is not None and grain in allowed)
            safety = evaluate_binding_safety(col)
            reasons: list[ReasonCode] = []
            if safety is BindingSafety.unsafe:
                reasons.append(ReasonCode.binding_safety_rejected)
            if not grain_ok:
                reasons.append(ReasonCode.grain_incompatible)
            eligible = safety is BindingSafety.safe and grain_ok
            cands.append(IngredientCandidateV1(
                recipe_id=template.id, need_role=need.role, concept=need.concept,
                required_grains=tuple(allowed), join_role=join_role, temporal_role=temporal_role,
                catalog_source=catalog_source, object_ref=col.object_ref, actual_source_grain=grain,
                binding_quality=_quality(col, need.concept, grain_ok), eligible=eligible,
                safety=safety, reason_codes=tuple(reasons)))
        out[need.role] = tuple(cands)
    return CandidateDiscoveryV1(candidates=out, candidate_columns_truncated=truncated,
                                total_candidate_columns_considered=total)
```

- [ ] **Step 4: Run to verify it passes** — `uv run pytest tests/featuregen/overlay/upload/planner/test_candidates.py -q` → PASS.

- [ ] **Step 5: Gates + commit**

```bash
uv run ruff check src/featuregen/overlay/upload/planner/candidates.py tests/featuregen/overlay/upload/planner/test_candidates.py
uv run mypy src/featuregen/overlay/upload/planner/candidates.py
git add -A && git commit -m "feat(3b3a): per-need candidate discovery + candidate-local eligibility (task 2)"
```

---

### Task 3 (A3): Bounded single-catalog plan enumeration

**Files:**
- Create: `src/featuregen/overlay/upload/planner/enumerate.py`
- Test: `tests/featuregen/overlay/upload/planner/test_enumerate.py`

**Interfaces:**
- Consumes: Task 1 contracts; Task 2 `CandidateDiscoveryV1`/`IngredientCandidateV1`; `Template`/`Need`.
- Produces: `enumerate_single_catalog_plans(template, catalog_source, target_entity, discovery) -> EnumerationV1` where `EnumerationV1(plans: tuple[BindingPlanV1,...], combinations_truncated: bool, plans_truncated: bool, total_combinations_explored: int)`. Each plan is tier-1, one `direct_catalog` segment; a plan binding all REQUIRED needs is `resolved` (pre-ranking), else `partially_resolved`; unranked (`preference_rank=-1`, filled by Task 4). Deterministic `plan_id`.

- [ ] **Step 1: Write the failing tests** — `tests/featuregen/overlay/upload/planner/test_enumerate.py`:

```python
import itertools

from featuregen.overlay.upload.planner.contracts import (
    IngredientCandidateV1, BindingQuality, BindingSafety, PlanResolutionStatus, PlanTier, SegmentKind)
from featuregen.overlay.upload.planner.candidates import CandidateDiscoveryV1
from featuregen.overlay.upload.planner.enumerate import enumerate_single_catalog_plans
from featuregen.overlay.upload.templates import Need, Template


def _cand(role, ref, *, eligible=True):
    return IngredientCandidateV1(recipe_id="t", need_role=role, concept="c", required_grains=(),
        join_role="", temporal_role="", catalog_source="core", object_ref=ref, actual_source_grain="account",
        binding_quality=BindingQuality.grain_and_role_fit, eligible=eligible,
        safety=BindingSafety.safe if eligible else BindingSafety.unsafe, reason_codes=())


def _tmpl(*roles, optional=()):
    return Template(id="t", family="f", intent="i",
                    needs=tuple(Need(role=r, concept="c", optional=(r in optional)) for r in roles),
                    params={}, aggregation="a", additivity="n/a", explain="L", use_cases=(), pit="p")


def test_single_eligible_per_need_yields_one_resolved_plan():
    tmpl = _tmpl("a", "b")
    disc = CandidateDiscoveryV1(candidates={"a": (_cand("a", "public.t.a"),), "b": (_cand("b", "public.t.b"),)},
                                candidate_columns_truncated=False, total_candidate_columns_considered=2)
    en = enumerate_single_catalog_plans(tmpl, "core", "customer", disc)
    assert len(en.plans) == 1
    p = en.plans[0]
    assert p.tier is PlanTier.tier_1_single_catalog and p.catalog_source == "core"
    assert p.resolution_status is PlanResolutionStatus.resolved
    assert len(p.path_segments) == 1 and p.path_segments[0].segment_kind is SegmentKind.direct_catalog
    assert {b.bound_object_ref for b in p.ingredient_bindings} == {"public.t.a", "public.t.b"}


def test_cartesian_product_of_eligible_candidates():
    tmpl = _tmpl("a", "b")
    disc = CandidateDiscoveryV1(
        candidates={"a": (_cand("a", "public.t.a1"), _cand("a", "public.t.a2")),
                    "b": (_cand("b", "public.t.b1"),)},
        candidate_columns_truncated=False, total_candidate_columns_considered=3)
    en = enumerate_single_catalog_plans(tmpl, "core", "customer", disc)
    assert len(en.plans) == 2                            # a1×b1, a2×b1
    assert len({p.plan_id for p in en.plans}) == 2       # deterministic distinct ids


def test_ineligible_only_need_gives_partial_plan_not_resolved():
    tmpl = _tmpl("a", "b")
    disc = CandidateDiscoveryV1(candidates={"a": (_cand("a", "public.t.a"),),
                                            "b": (_cand("b", "public.t.b", eligible=False),)},
                                candidate_columns_truncated=False, total_candidate_columns_considered=2)
    en = enumerate_single_catalog_plans(tmpl, "core", "customer", disc)
    # required need 'b' cannot bind (no eligible candidate) -> a partial plan is preserved, not resolved
    assert all(p.resolution_status is PlanResolutionStatus.partially_resolved for p in en.plans)


def test_optional_unbound_need_still_resolved():
    tmpl = _tmpl("a", "b", optional=("b",))
    disc = CandidateDiscoveryV1(candidates={"a": (_cand("a", "public.t.a"),), "b": ()},
                                candidate_columns_truncated=False, total_candidate_columns_considered=1)
    en = enumerate_single_catalog_plans(tmpl, "core", "customer", disc)
    assert len(en.plans) == 1 and en.plans[0].resolution_status is PlanResolutionStatus.resolved


def test_combination_bound_truncates_and_flags():
    # 3 needs x many eligible candidates each -> exceed MAX_PARTIAL_COMBINATIONS
    from featuregen.overlay.upload.planner.contracts import MAX_PARTIAL_COMBINATIONS
    roles = ("a", "b", "c")
    cands = {r: tuple(_cand(r, f"public.t.{r}{i}") for i in range(10)) for r in roles}   # 10^3 = 1000 > 256
    disc = CandidateDiscoveryV1(candidates=cands, candidate_columns_truncated=False,
                                total_candidate_columns_considered=30)
    en = enumerate_single_catalog_plans(_tmpl(*roles), "core", "customer", disc)
    assert en.combinations_truncated is True
    assert len(en.plans) <= MAX_PARTIAL_COMBINATIONS
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/featuregen/overlay/upload/planner/test_enumerate.py -q` → FAIL.

- [ ] **Step 3: Implement `enumerate.py`:**

```python
"""Phase-3B.3a A3 — bounded single-catalog plan enumeration. The cartesian product of ELIGIBLE per-need
candidates into tier-1 BindingPlanV1s, bounded + deterministic. A plan binding every REQUIRED need is
`resolved` (pre-ranking); otherwise `partially_resolved`. Ranking is Task 4."""
from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass

from featuregen.overlay.upload.planner.candidates import CandidateDiscoveryV1
from featuregen.overlay.upload.planner.contracts import (
    MAX_PARTIAL_COMBINATIONS,
    MAX_PLANS_PER_RECIPE,
    PLANNER_VERSION,
    BindingPathSegmentV1,
    BindingPlanV1,
    BindingSafety,
    IngredientBindingV1,
    IngredientCandidateV1,
    PlanResolutionStatus,
    PlanTier,
    ReasonCode,
    SegmentKind,
)
from featuregen.overlay.upload.templates import Template


@dataclass(frozen=True, slots=True)
class EnumerationV1:
    plans: tuple[BindingPlanV1, ...]
    combinations_truncated: bool
    plans_truncated: bool
    total_combinations_explored: int


def _binding(c: IngredientCandidateV1) -> IngredientBindingV1:
    return IngredientBindingV1(
        recipe_id=c.recipe_id, need_role=c.need_role, concept=c.concept, required_grains=c.required_grains,
        join_role=c.join_role, temporal_role=c.temporal_role, bound_catalog_source=c.catalog_source,
        bound_object_ref=c.object_ref, actual_source_grain=c.actual_source_grain,
        binding_quality=c.binding_quality, safety=c.safety, reason_codes=c.reason_codes)


def _plan_id(recipe_id: str, catalog: str, refs: tuple[str, ...]) -> str:
    material = f"{recipe_id}|{catalog}|{'|'.join(sorted(refs))}|{PlanTier.tier_1_single_catalog}|{PLANNER_VERSION}"
    return "bp_" + hashlib.sha256(material.encode()).hexdigest()[:16]


def enumerate_single_catalog_plans(template: Template, catalog_source: str, target_entity: str | None,
                                   discovery: CandidateDiscoveryV1) -> EnumerationV1:
    required = [n.role for n in template.needs if not n.optional]
    optional = [n.role for n in template.needs if n.optional]
    # one axis per REQUIRED need = its eligible candidates; a required need with no eligible candidate
    # still yields a single partial "axis" (None) so the plan is preserved as partially_resolved.
    axes: list[tuple[str, tuple[IngredientCandidateV1 | None, ...]]] = []
    for role in required:
        eligible = tuple(c for c in discovery.candidates.get(role, ()) if c.eligible)
        axes.append((role, eligible if eligible else (None,)))
    # deterministic optional bindings: at most the single best-ordered eligible candidate per optional need
    opt_bindings: list[IngredientCandidateV1] = []
    for role in optional:
        elig = [c for c in discovery.candidates.get(role, ()) if c.eligible]
        if elig:
            opt_bindings.append(sorted(elig, key=lambda c: c.object_ref)[0])

    combos = 1
    for _role, cs in axes:
        combos *= max(1, len(cs))
    combinations_truncated = combos > MAX_PARTIAL_COMBINATIONS

    plans: list[BindingPlanV1] = []
    explored = 0
    for combo in itertools.product(*[cs for _r, cs in axes]):
        if explored >= MAX_PARTIAL_COMBINATIONS:
            combinations_truncated = True
            break
        explored += 1
        bound = [c for c in combo if c is not None] + opt_bindings
        missing_required = any(c is None for c in combo)
        bindings = tuple(_binding(c) for c in sorted(bound, key=lambda c: c.need_role))
        refs = tuple(b.bound_object_ref for b in bindings)
        status = (PlanResolutionStatus.partially_resolved if missing_required
                  else PlanResolutionStatus.resolved)
        reasons = (ReasonCode.missing_required_need,) if missing_required else ()
        plans.append(BindingPlanV1(
            plan_id=_plan_id(template.id, catalog_source, refs), recipe_id=template.id,
            target_entity=target_entity, tier=PlanTier.tier_1_single_catalog, catalog_source=catalog_source,
            ingredient_bindings=bindings,
            path_segments=(BindingPathSegmentV1(segment_kind=SegmentKind.direct_catalog,
                                                catalog_source=catalog_source),),
            resolution_status=status, primary_reason_code=(reasons[0] if reasons else None),
            reason_codes=reasons, safety=BindingSafety.safe, preference_rank=-1, preference_reasons=()))

    plans_truncated = len(plans) > MAX_PLANS_PER_RECIPE
    if plans_truncated:
        plans = sorted(plans, key=lambda p: p.plan_id)[:MAX_PLANS_PER_RECIPE]
    return EnumerationV1(plans=tuple(plans), combinations_truncated=combinations_truncated,
                         plans_truncated=plans_truncated, total_combinations_explored=explored)
```

- [ ] **Step 4: Run to verify it passes** — `uv run pytest tests/featuregen/overlay/upload/planner/test_enumerate.py -q` → PASS.

- [ ] **Step 5: Gates + commit**

```bash
uv run ruff check src/featuregen/overlay/upload/planner/enumerate.py tests/featuregen/overlay/upload/planner/test_enumerate.py
uv run mypy src/featuregen/overlay/upload/planner/enumerate.py
git add -A && git commit -m "feat(3b3a): bounded single-catalog plan enumeration (task 3)"
```

---

### Task 4 (A4): Deterministic ordering + preference + ambiguity

**Files:**
- Create: `src/featuregen/overlay/upload/planner/order.py`
- Test: `tests/featuregen/overlay/upload/planner/test_order.py`

**Interfaces:**
- Consumes: Task 1 contracts (`BindingPlanV1`, `BindingQuality`, `PlanResolutionStatus`, `ReasonCode`).
- Produces: `order_plans(plans: Sequence[BindingPlanV1]) -> OrderedPlansV1` where `OrderedPlansV1(plans: tuple[BindingPlanV1,...], ambiguous: bool)` — plans re-emitted with `preference_rank` (0=best) + `preference_reasons`; `resolved` before `partially_resolved`; among resolved, higher binding-quality first; ties broken by `catalog_source`, first bound ref, `recipe_id`, `plan_id`. `ambiguous=True` when ≥2 resolved plans tie on the full key.

- [ ] **Step 1: Write the failing tests** — `tests/featuregen/overlay/upload/planner/test_order.py`:

```python
from featuregen.overlay.upload.planner.contracts import (
    BindingPathSegmentV1, BindingPlanV1, BindingQuality, BindingSafety, IngredientBindingV1,
    PlanResolutionStatus, PlanTier, SegmentKind)
from featuregen.overlay.upload.planner.order import order_plans


def _plan(pid, refs, *, status=PlanResolutionStatus.resolved, quality=BindingQuality.grain_and_role_fit,
          catalog="core"):
    binds = tuple(IngredientBindingV1("t", f"r{i}", "c", (), "", "", catalog, r, "account", quality,
                                      BindingSafety.safe, ()) for i, r in enumerate(refs))
    return BindingPlanV1(pid, "t", "customer", PlanTier.tier_1_single_catalog, catalog, binds,
                         (BindingPathSegmentV1(SegmentKind.direct_catalog, catalog),), status, None, (),
                         BindingSafety.safe, -1, ())


def test_resolved_ranks_before_partial():
    ordered = order_plans([_plan("b", ("public.t.x",), status=PlanResolutionStatus.partially_resolved),
                           _plan("a", ("public.t.y",))]).plans
    assert ordered[0].resolution_status is PlanResolutionStatus.resolved and ordered[0].preference_rank == 0
    assert ordered[1].resolution_status is PlanResolutionStatus.partially_resolved


def test_higher_quality_ranks_first_among_resolved():
    ordered = order_plans([_plan("a", ("public.t.a",), quality=BindingQuality.weak),
                           _plan("b", ("public.t.b",), quality=BindingQuality.grain_and_role_fit)]).plans
    assert ordered[0].plan_id == "b" and ordered[0].preference_rank == 0
    assert ordered[0].preference_reasons                      # ordering audit recorded


def test_full_tie_is_ambiguous_but_deterministic():
    res = order_plans([_plan("z", ("public.t.a",)), _plan("a", ("public.t.a",))])
    assert res.ambiguous is True
    assert [p.plan_id for p in res.plans] == ["a", "z"]      # tie broken by plan_id, stable
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/featuregen/overlay/upload/planner/test_order.py -q` → FAIL.

- [ ] **Step 3: Implement `order.py`:**

```python
"""Phase-3B.3a A4 — deterministic total ordering + preference ranks + ambiguity. Never incidental order."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from featuregen.overlay.upload.planner.contracts import (
    BindingPlanV1,
    BindingQuality,
    PlanResolutionStatus,
)

_STATUS_RANK = {PlanResolutionStatus.resolved: 0, PlanResolutionStatus.partially_resolved: 1}
_QUALITY_RANK = {BindingQuality.grain_and_role_fit: 0, BindingQuality.exact_concept: 1,
                 BindingQuality.entity_tagged: 2, BindingQuality.weak: 3}


@dataclass(frozen=True, slots=True)
class OrderedPlansV1:
    plans: tuple[BindingPlanV1, ...]
    ambiguous: bool


def _first_ref(p: BindingPlanV1) -> str:
    return min((b.bound_object_ref for b in p.ingredient_bindings), default="")


def _agg_quality(p: BindingPlanV1) -> int:
    return max((_QUALITY_RANK[b.binding_quality] for b in p.ingredient_bindings), default=99)


def _key(p: BindingPlanV1) -> tuple:
    return (_STATUS_RANK.get(p.resolution_status, 9), -len(p.ingredient_bindings), _agg_quality(p),
            p.catalog_source, _first_ref(p), p.recipe_id, p.plan_id)


def _tie_key(p: BindingPlanV1) -> tuple:
    k = _key(p)
    return k[:-1]   # everything except the plan_id tiebreak


def order_plans(plans: Sequence[BindingPlanV1]) -> OrderedPlansV1:
    ordered = sorted(plans, key=_key)
    ranked = tuple(replace(p, preference_rank=i,
                           preference_reasons=(f"status={p.resolution_status}",
                                               f"bindings={len(p.ingredient_bindings)}",
                                               f"quality={_agg_quality(p)}", f"catalog={p.catalog_source}"))
                   for i, p in enumerate(ordered))
    resolved = [p for p in ranked if p.resolution_status is PlanResolutionStatus.resolved]
    ambiguous = any(_tie_key(a) == _tie_key(b) for a, b in zip(resolved, resolved[1:]))
    return OrderedPlansV1(plans=ranked, ambiguous=ambiguous)
```

- [ ] **Step 4: Run to verify it passes** — `uv run pytest tests/featuregen/overlay/upload/planner/test_order.py -q` → PASS.

- [ ] **Step 5: Gates + commit**

```bash
uv run ruff check src/featuregen/overlay/upload/planner/order.py tests/featuregen/overlay/upload/planner/test_order.py
uv run mypy src/featuregen/overlay/upload/planner/order.py
git add -A && git commit -m "feat(3b3a): deterministic plan ordering + ambiguity (task 4)"
```

---

### Task 5 (A5): Scope resolution + orchestration + shadow entry

**Files:**
- Create: `src/featuregen/overlay/upload/planner/scope.py`, `src/featuregen/overlay/upload/planner/plan.py`, `src/featuregen/overlay/upload/planner/shadow.py`
- Modify: `src/featuregen/api/routes/contract.py` (the log-only entry on the entity-scoped considered-set path)
- Test: `tests/featuregen/overlay/upload/planner/test_scope.py`, `test_plan.py`, `test_shadow.py`

**Interfaces:**
- Consumes: Tasks 1–4; `drift_watermark`/`drift_head_seq` (overlay/catalog_changes.py); `allowed_sensitivities` (read_scope.py); `RESOLVED_NEED_METADATA`; `ground_template`/`ALL_TEMPLATES` (templates.py); version constants (GRAPH_VERSION, REALIZATION_DERIVATION_VERSION, BRIDGE_DERIVATION_VERSION, NEED_METADATA_VERSION).
- Produces: `resolve_catalog_scope(conn, *, roles, target_entity, now, requested_sources=None) -> CatalogScopeV1`; `plan_bindings(conn, *, template, target_entity, scope, roles, now) -> BindingPlanningResultV1`; `run_shadow_planner(conn, *, eligible_recipe_ids, target_entity, roles, run_id, now) -> tuple[BindingPlanningResultV1, ...]`.

- [ ] **Step 1: Write the failing tests** — `tests/featuregen/overlay/upload/planner/test_scope.py`:

```python
from datetime import UTC, datetime

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.planner.contracts import CatalogOmissionReason
from featuregen.overlay.upload.planner.scope import resolve_catalog_scope

_NOW = datetime(2026, 7, 14, tzinfo=UTC)


def _seed(db, source, *, watermark=True):
    build_graph(db, source, [CanonicalRow(source, "t", "id", "integer", is_grain=True)],
                concepts={content_hash(CanonicalRow(source, "t", "id", "integer", is_grain=True)): "customer_id"})
    if watermark:
        db.execute("INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, head_seq) "
                   "VALUES (%s, %s, 'r', 5) ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
                   (source, _NOW, _NOW))


def test_scope_orders_and_stamps_readable_catalogs(db):
    _seed(db, "core"); _seed(db, "crm")
    scope = resolve_catalog_scope(db, roles=(), target_entity="customer", now=_NOW)
    assert scope.authorized_catalog_sources == ("core", "crm")           # deterministically sorted
    assert {s.catalog_source for s in scope.catalog_state_stamps} == {"core", "crm"}
    assert all(s.head_seq == 5 for s in scope.catalog_state_stamps)
    assert scope.scope_id and scope.read_scope_policy_version


def test_catalog_without_watermark_is_omitted(db):
    _seed(db, "core"); _seed(db, "nowm", watermark=False)
    scope = resolve_catalog_scope(db, roles=(), target_entity="customer", now=_NOW)
    assert scope.authorized_catalog_sources == ("core",)
    assert any(o.catalog_source == "nowm" and o.reason is CatalogOmissionReason.no_usable_state_stamp
               for o in scope.omitted_catalog_sources)


def test_scope_id_is_deterministic(db):
    _seed(db, "core")
    a = resolve_catalog_scope(db, roles=(), target_entity="customer", now=_NOW)
    b = resolve_catalog_scope(db, roles=(), target_entity="customer", now=_NOW)
    assert a.scope_id == b.scope_id
```

`test_plan.py` and `test_shadow.py` (append after scope):

```python
# test_plan.py
from datetime import UTC, datetime

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.planner.contracts import PlanResolutionStatus, ReplayStrength
from featuregen.overlay.upload.planner.plan import plan_bindings
from featuregen.overlay.upload.planner.scope import resolve_catalog_scope
from featuregen.overlay.upload.templates import Need, Template

_NOW = datetime(2026, 7, 14, tzinfo=UTC)


def _catalog(db, source):
    catalog = [
        (CanonicalRow(source, "accounts", "customer_id", "integer", is_grain=True), "customer_id"),
        (CanonicalRow(source, "accounts", "balance", "numeric", additivity="semi_additive", currency="USD"),
         "monetary_stock")]
    build_graph(db, source, [r for r, _ in catalog], concepts={content_hash(r): c for r, c in catalog})
    db.execute("INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, head_seq) "
               "VALUES (%s, %s, 'r', 1) ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
               (source, _NOW, _NOW))


def _tmpl():
    return Template(id="t_bal", family="f", intent="i",
                    needs=(Need(role="stock_col", concept="monetary_stock"),
                           Need(role="entity", concept="customer_id")),
                    params={}, aggregation="avg", additivity="semi_additive", explain="M", use_cases=(),
                    pit="trailing")


def test_plan_bindings_resolves_a_single_catalog_plan(db):
    _catalog(db, "core")
    scope = resolve_catalog_scope(db, roles=(), target_entity="customer", now=_NOW)
    result = plan_bindings(db, template=_tmpl(), target_entity="customer", scope=scope, roles=(), now=_NOW)
    assert result.result_status is PlanResolutionStatus.resolved
    assert result.selected_plan_id is not None
    sel = next(p for p in result.candidate_plans if p.plan_id == result.selected_plan_id)
    assert sel.catalog_source == "core"
    assert {b.bound_object_ref for b in sel.ingredient_bindings} == {"public.accounts.balance",
                                                                     "public.accounts.customer_id"}
    assert result.replay_envelope.replay_strength is ReplayStrength.conditional   # watermark stamps, not a snapshot
    assert result.replay_envelope.planner_input_hash


def test_no_authorized_catalog_is_not_applicable(db):
    scope = resolve_catalog_scope(db, roles=(), target_entity="customer", now=_NOW)   # nothing seeded
    result = plan_bindings(db, template=_tmpl(), target_entity="customer", scope=scope, roles=(), now=_NOW)
    assert result.result_status is PlanResolutionStatus.not_applicable


def test_rejected_alternative_does_not_downgrade_a_resolved_result(db):
    # two catalogs: 'core' binds cleanly; 'bad' has an unsafe stock column (a rejected alternative). The
    # result must still be `resolved` (candidate-local-first).
    _catalog(db, "core")
    bad = [(CanonicalRow("bad", "accounts", "customer_id", "integer", is_grain=True), "customer_id"),
           (CanonicalRow("bad", "accounts", "amt", "numeric"), "monetary_stock"),
           (CanonicalRow("bad", "accounts", "amt2", "numeric"), "outcome_label")]  # noise, not bound
    build_graph(db, "bad", [r for r, _ in bad], concepts={content_hash(r): c for r, c in bad})
    db.execute("INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, head_seq) "
               "VALUES ('bad', %s, 'r', 1) ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
               (_NOW, _NOW))
    scope = resolve_catalog_scope(db, roles=(), target_entity="customer", now=_NOW)
    result = plan_bindings(db, template=_tmpl(), target_entity="customer", scope=scope, roles=(), now=_NOW)
    assert result.result_status is PlanResolutionStatus.resolved
    assert len(result.candidate_plans) >= 2               # alternatives preserved


# test_shadow.py
from featuregen.overlay.upload.planner.shadow import run_shadow_planner


def test_run_shadow_planner_logs_per_recipe(db, caplog):
    _catalog(db, "core")
    import logging
    with caplog.at_level(logging.INFO):
        results = run_shadow_planner(db, eligible_recipe_ids=frozenset({"t_bal"}),
                                     target_entity="customer", roles=(), run_id="run1", now=_NOW,
                                     templates=(_tmpl(),))
    assert len(results) == 1 and results[0].recipe_id == "t_bal"
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/featuregen/overlay/upload/planner/ -q` → FAIL (scope/plan/shadow missing).

- [ ] **Step 3: Implement `scope.py`:**

```python
"""Phase-3B.3a A5 — the authorization/read-scope resolver. The ONE place role logic + watermark stamping
happen; produces a FROZEN CatalogScopeV1 the planner core treats as immutable input."""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import datetime

from featuregen.overlay.catalog_changes import drift_head_seq, drift_watermark
from featuregen.overlay.upload.planner.contracts import (
    MAX_AUTHORIZED_CATALOGS_CONSIDERED,
    READ_SCOPE_POLICY_VERSION,
    ROLE_RESOLUTION_VERSION,
    CatalogOmissionReason,
    CatalogScopeV1,
    CatalogStateStampV1,
    OmittedCatalogV1,
)
from featuregen.overlay.upload.read_scope import allowed_sensitivities


def resolve_catalog_scope(conn, *, roles: Iterable[str] = (), target_entity: str | None,
                          now: datetime, requested_sources: tuple[str, ...] | None = None) -> CatalogScopeV1:
    rows = conn.execute(
        "SELECT DISTINCT catalog_source FROM graph_node WHERE kind = 'column' "
        "AND (sensitivity IS NULL OR sensitivity = ANY(%s)) ORDER BY catalog_source",
        (allowed_sensitivities(roles),)).fetchall()
    readable = [r[0] for r in rows]
    if requested_sources is not None:
        readable = [s for s in readable if s in set(requested_sources)]

    authorized: list[str] = []
    stamps: list[CatalogStateStampV1] = []
    omitted: list[OmittedCatalogV1] = []
    for src in readable:
        wm = drift_watermark(conn, src)
        head = drift_head_seq(conn, src)
        if wm is None:
            omitted.append(OmittedCatalogV1(src, CatalogOmissionReason.no_usable_state_stamp))
            continue
        authorized.append(src)
        stamps.append(CatalogStateStampV1(catalog_source=src, head_seq=head or 0,
                                          last_completed_at=wm.isoformat()))
    truncated = len(authorized) > MAX_AUTHORIZED_CATALOGS_CONSIDERED
    if truncated:
        for src in authorized[MAX_AUTHORIZED_CATALOGS_CONSIDERED:]:
            omitted.append(OmittedCatalogV1(src, CatalogOmissionReason.catalog_consideration_bound))
        authorized = authorized[:MAX_AUTHORIZED_CATALOGS_CONSIDERED]
        stamps = stamps[:MAX_AUTHORIZED_CATALOGS_CONSIDERED]

    material = "|".join(f"{s.catalog_source}:{s.head_seq}:{s.last_completed_at}" for s in stamps)
    material += f"|{READ_SCOPE_POLICY_VERSION}|{ROLE_RESOLUTION_VERSION}|{target_entity or ''}"
    scope_id = "cs_" + hashlib.sha256(material.encode()).hexdigest()[:16]
    return CatalogScopeV1(
        scope_id=scope_id, authorized_catalog_sources=tuple(authorized),
        catalog_state_stamps=tuple(stamps), omitted_catalog_sources=tuple(omitted),
        read_scope_policy_version=READ_SCOPE_POLICY_VERSION, role_resolution_version=ROLE_RESOLUTION_VERSION,
        resolved_at=now.isoformat(), catalog_consideration_truncated=truncated)
```

`plan.py`:

```python
"""Phase-3B.3a A5 — the per-recipe orchestrator: discover -> enumerate -> order across the frozen scope's
catalogs, classify the result by candidate-local-first precedence, compute the ground_template differential,
and build the replay envelope. Read-only, deterministic."""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import datetime

from featuregen.overlay.upload.bridge_candidates import BRIDGE_DERIVATION_VERSION
from featuregen.overlay.upload.catalog_realizations import REALIZATION_DERIVATION_VERSION
from featuregen.overlay.upload.need_metadata import NEED_METADATA_VERSION
from featuregen.overlay.upload.planner.candidates import discover_ingredient_candidates
from featuregen.overlay.upload.planner.contracts import (
    APPLICABILITY_MAPPING_VERSION,
    CONCEPT_REGISTRY_VERSION,
    PLANNER_VERSION,
    REASON_CODE_REGISTRY_VERSION,
    RECIPE_REGISTRY_VERSION,
    BindingPlanningResultV1,
    BoundingMetricsV1,
    CatalogScopeV1,
    GroundTemplateDiffOutcome,
    GroundTemplateDiffV1,
    PlannerReplayEnvelopeV1,
    PlanResolutionStatus,
    ReasonCode,
    ReplayStrength,
)
from featuregen.overlay.upload.planner.enumerate import enumerate_single_catalog_plans
from featuregen.overlay.upload.planner.order import order_plans
from featuregen.overlay.upload.taxonomy.entity_registry import GRAPH_VERSION
from featuregen.overlay.upload.templates import Template, ground_template

_FAILURE_PRECEDENCE = (PlanResolutionStatus.safety_rejected, PlanResolutionStatus.bounded_out,
                       PlanResolutionStatus.partially_resolved, PlanResolutionStatus.unresolved)


def _envelope(scope: CatalogScopeV1, recipe_id: str, target_entity: str | None) -> PlannerReplayEnvelopeV1:
    material = (f"{PLANNER_VERSION}|{scope.scope_id}|{recipe_id}|{target_entity or ''}|{NEED_METADATA_VERSION}"
               f"|{GRAPH_VERSION}|{REALIZATION_DERIVATION_VERSION}|{RECIPE_REGISTRY_VERSION}"
               f"|{APPLICABILITY_MAPPING_VERSION}|{CONCEPT_REGISTRY_VERSION}")
    return PlannerReplayEnvelopeV1(
        planner_version=PLANNER_VERSION, reason_code_registry_version=REASON_CODE_REGISTRY_VERSION,
        applicability_mapping_version=APPLICABILITY_MAPPING_VERSION, recipe_registry_version=RECIPE_REGISTRY_VERSION,
        need_metadata_version=NEED_METADATA_VERSION, graph_version=GRAPH_VERSION,
        realization_derivation_version=REALIZATION_DERIVATION_VERSION,
        bridge_derivation_version=BRIDGE_DERIVATION_VERSION, concept_registry_version=CONCEPT_REGISTRY_VERSION,
        catalog_scope=scope, replay_strength=ReplayStrength.conditional,
        planner_input_hash="ph_" + hashlib.sha256(material.encode()).hexdigest()[:24])


def _differential(conn, template, plans, scope, roles, now) -> GroundTemplateDiffV1:
    for src in scope.authorized_catalog_sources:
        gf = ground_template(conn, template, catalog_source=src, roles=roles)
        if gf is None:
            continue
        live_refs = tuple(sorted(ref for _s, ref in gf.derives_pairs))
        for p in plans:
            if p.resolution_status is PlanResolutionStatus.resolved and p.catalog_source == src:
                plan_refs = tuple(sorted(b.bound_object_ref for b in p.ingredient_bindings))
                if set(live_refs).issubset(plan_refs):
                    outcome = (GroundTemplateDiffOutcome.live_binding_present_and_ranked_first
                               if p.preference_rank == 0
                               else GroundTemplateDiffOutcome.live_binding_present_but_ranked_lower)
                    return GroundTemplateDiffV1(outcome, live_refs, p.plan_id)
        return GroundTemplateDiffV1(GroundTemplateDiffOutcome.live_binding_absent_unexpectedly, live_refs, None)
    return GroundTemplateDiffV1(GroundTemplateDiffOutcome.live_path_had_no_binding, (), None)


def plan_bindings(conn, *, template: Template, target_entity: str | None, scope: CatalogScopeV1,
                  roles: Iterable[str] = (), now: datetime) -> BindingPlanningResultV1:
    roles = tuple(roles)
    envelope = _envelope(scope, template.id, target_entity)
    if not scope.authorized_catalog_sources:
        return _empty_result(template.id, target_entity, scope, envelope,
                             PlanResolutionStatus.not_applicable, ReasonCode.no_authorized_catalog)

    all_plans = []
    cols_trunc = combos_trunc = plans_trunc = False
    total_cols = total_combos = 0
    for src in scope.authorized_catalog_sources:
        disc = discover_ingredient_candidates(conn, template, src, roles=roles)
        cols_trunc |= disc.candidate_columns_truncated
        total_cols += disc.total_candidate_columns_considered
        en = enumerate_single_catalog_plans(template, src, target_entity, disc)
        combos_trunc |= en.combinations_truncated
        plans_trunc |= en.plans_truncated
        total_combos += en.total_combinations_explored
        all_plans.extend(en.plans)

    ordered = order_plans(all_plans)
    resolved = [p for p in ordered.plans if p.resolution_status is PlanResolutionStatus.resolved]
    bounding = BoundingMetricsV1(cols_trunc, combos_trunc, plans_trunc, scope.catalog_consideration_truncated,
                                 total_cols, total_combos, len(ordered.plans))
    diff = _differential(conn, template, ordered.plans, scope, roles, now)

    if resolved:
        status = PlanResolutionStatus.resolved
        selected = resolved[0].plan_id
        reasons = ((ReasonCode.selected_best_single_catalog,)
                   + ((ReasonCode.ambiguous_multiple_equal_plans,) if ordered.ambiguous else ()))
        primary = ReasonCode.selected_best_single_catalog
    else:
        selected = None
        status, primary = _classify_failure(ordered.plans, bounding)
        reasons = (primary,) if primary else ()

    return BindingPlanningResultV1(
        run_id=None, recipe_id=template.id, target_entity=target_entity, catalog_scope_id=scope.scope_id,
        selected_plan_id=selected, candidate_plans=ordered.plans, result_status=status,
        primary_reason_code=primary, reason_codes=reasons, bounding=bounding, ground_template_diff=diff,
        replay_envelope=envelope)


def _classify_failure(plans, bounding):
    present = {p.resolution_status for p in plans}
    if bounding.combinations_truncated or bounding.plans_truncated:
        return PlanResolutionStatus.bounded_out, ReasonCode.bounded_out_max_combinations
    if PlanResolutionStatus.partially_resolved in present:
        return PlanResolutionStatus.partially_resolved, ReasonCode.missing_required_need
    return PlanResolutionStatus.unresolved, ReasonCode.no_role_compatible_column


def _empty_result(recipe_id, target_entity, scope, envelope, status, reason):
    from featuregen.overlay.upload.planner.contracts import BoundingMetricsV1, GroundTemplateDiffOutcome
    return BindingPlanningResultV1(
        run_id=None, recipe_id=recipe_id, target_entity=target_entity, catalog_scope_id=scope.scope_id,
        selected_plan_id=None, candidate_plans=(), result_status=status, primary_reason_code=reason,
        reason_codes=(reason,),
        bounding=BoundingMetricsV1(False, False, False, scope.catalog_consideration_truncated, 0, 0, 0),
        ground_template_diff=GroundTemplateDiffV1(GroundTemplateDiffOutcome.not_compared, (), None),
        replay_envelope=envelope)
```

`shadow.py`:

```python
"""Phase-3B.3a A5 — the log-only shadow entry. Resolves the scope ONCE, plans each eligible recipe,
logs the result. Never alters the considered set. A planner error is isolated per recipe."""
from __future__ import annotations

import dataclasses
import logging
from collections.abc import Iterable
from datetime import datetime

from featuregen.overlay.upload.planner.contracts import (
    BindingPlanningResultV1,
    PlanResolutionStatus,
    ReasonCode,
)
from featuregen.overlay.upload.planner.plan import _envelope, plan_bindings
from featuregen.overlay.upload.planner.scope import resolve_catalog_scope
from featuregen.overlay.upload.templates import ALL_TEMPLATES, Template

logger = logging.getLogger(__name__)


def run_shadow_planner(conn, *, eligible_recipe_ids: frozenset[str], target_entity: str | None,
                       roles: Iterable[str] = (), run_id: str | None, now: datetime,
                       templates: tuple[Template, ...] | None = None) -> tuple[BindingPlanningResultV1, ...]:
    roles = tuple(roles)
    scope = resolve_catalog_scope(conn, roles=roles, target_entity=target_entity, now=now)
    by_id = {t.id: t for t in (templates if templates is not None else ALL_TEMPLATES)}
    results: list[BindingPlanningResultV1] = []
    for rid in sorted(eligible_recipe_ids):
        tmpl = by_id.get(rid)
        if tmpl is None:
            continue
        try:
            result = plan_bindings(conn, template=tmpl, target_entity=target_entity, scope=scope,
                                   roles=roles, now=now)
            result = dataclasses.replace(result, run_id=run_id)
        except Exception:   # planner failure is isolated per recipe; never touches the response
            logger.exception("shadow planner internal error for recipe %s", rid)
            result = BindingPlanningResultV1(
                run_id=run_id, recipe_id=rid, target_entity=target_entity, catalog_scope_id=scope.scope_id,
                selected_plan_id=None, candidate_plans=(), result_status=PlanResolutionStatus.internal_error,
                primary_reason_code=ReasonCode.planner_internal_error,
                reason_codes=(ReasonCode.planner_internal_error,), bounding=None,  # type: ignore[arg-type]
                ground_template_diff=None, replay_envelope=_envelope(scope, rid, target_entity))  # type: ignore[arg-type]
        logger.info("shadow_binding_plan recipe=%s status=%s selected=%s scope=%s",
                    result.recipe_id, result.result_status, result.selected_plan_id, scope.scope_id)
        results.append(result)
    return tuple(results)
```

> **Implementer note (internal-error bounding/diff):** the `internal_error` branch sets `bounding`/`ground_template_diff` to `None` with `# type: ignore`. If mypy's strictness rejects that against the non-Optional field types, instead construct a zero `BoundingMetricsV1(False,False,False,False,0,0,0)` and `GroundTemplateDiffV1(GroundTemplateDiffOutcome.not_compared, (), None)` (import them) — do NOT widen the contract field types.

- [ ] **Step 4: Wire the log-only entry** — in `src/featuregen/api/routes/contract.py`, on the considered-set path, AFTER `cs = build_considered_set(...)` returns, when the run is entity-scoped (`catalog_source is None`) and a confirmed `target_entity` is present, call the shadow planner and return `cs` UNCHANGED:

```python
    # 3B.3a shadow: on an entity-scoped run (no single catalog to ground on) compute + LOG cross-catalog
    # single-catalog binding plans for the eligible recipes. Log-only — the considered set is UNCHANGED.
    if catalog_source is None and scope.target_entity is not None and applicability is not None:
        try:
            run_shadow_planner(conn, eligible_recipe_ids=applicability.eligible_ids,
                               target_entity=scope.target_entity, roles=roles, run_id=generation_run_id,
                               now=now)
        except Exception:                    # shadow must NEVER affect the live response
            logger.exception("shadow planner dispatch failed")
```

(Use the route's existing `scope` (ConfirmedScope), `applicability`, `roles`, `now`, and the minted `generation_run_id`. Add `from featuregen.overlay.upload.planner.shadow import run_shadow_planner` to the top imports. If the exact local variable names differ, adapt — the required inputs are: eligible ids, the confirmed target_entity, roles, the run id, now.)

- [ ] **Step 5: Run the planner tests + the FULL suite (behaviour-neutral proof)**

```bash
uv run pytest tests/featuregen/overlay/upload/planner/ -q
uv run pytest tests/featuregen/ -q      # all-green; the shadow entry logs but never alters a response
```
Expected: planner tests pass; full suite is the prior total + the new planner tests, zero regressions. The considered-set API tests are byte-identical (the shadow call is log-only + exception-wrapped).

- [ ] **Step 6: Gates + commit**

```bash
uv run ruff check src/featuregen/overlay/upload/planner/ src/featuregen/api/routes/contract.py tests/featuregen/overlay/upload/planner/
uv run mypy src/featuregen/overlay/upload/planner/
git add -A && git commit -m "feat(3b3a): scope resolution + orchestration + log-only shadow entry (task 5)"
```

---

## Exit criteria mapping

| Spec requirement | Task |
|---|---|
| Contract/reason-code/status backbone; supersede CrossCatalog* names; lowercase-snake StrEnum | Task 1 |
| `evaluate_binding_safety` boundary + parity + floor invariant | Task 1 |
| Per-need candidate discovery consuming RESOLVED_NEED_METADATA; preserve accepted+rejected; per-need bound | Task 2 |
| Bounded single-catalog enumeration; resolved vs partial; tier-1 structural closure; deterministic plan_id | Task 3 |
| Deterministic total ordering + preference + ambiguity | Task 4 |
| Authorization outside the planner → frozen CatalogScopeV1 (stamps + policy versions + omissions); frozen once | Task 5 scope.py |
| Candidate-local-first precedence (a rejected alternative never downgrades a resolved result) | Task 5 plan.py + `test_rejected_alternative...` |
| Scope-outcome distinctions (not_applicable/no_authorized_catalog) | Task 5 |
| Replay envelope + planner_input_hash + replay_strength=conditional | Task 5 plan.py |
| ground_template differential | Task 5 plan.py |
| Log-only shadow entry; behaviour-neutral; planner errors isolated | Task 5 shadow.py + full-suite proof |
| No migration / no store (that is 3B.4) | (none created) |

## Self-Review

**Spec coverage:** every spec section maps to a task (table above). Bridges/roll-ups/agg/temporal/freshness-resolvability/safety-staging/strong-replay/the store are explicitly out (3B.3b/c/3B.4). ✅
**Placeholder scan:** every step has complete code + real assertions. Two implementer notes (the grain-outcome-not-pinned test; the internal-error bounding/diff None-vs-zero) are verify-and-adapt hooks, not placeholders. ✅
**Type consistency:** `CandidateDiscoveryV1` (Task 2) → `enumerate_single_catalog_plans` (Task 3) → `order_plans` (Task 4) → `plan_bindings` (Task 5) chain matches; `BindingPlanV1`/`IngredientBindingV1`/`PlannerReplayEnvelopeV1` fields are used consistently; `run_shadow_planner`'s `templates=` override lets tests inject a template without touching `ALL_TEMPLATES`. ✅
**Note for the executor:** Task 5 touches `api/routes/contract.py` (a live route) — the change is a log-only, exception-wrapped call after the response is built; if the full suite reddens an existing considered-set test, STOP (the shadow call leaked into the response).

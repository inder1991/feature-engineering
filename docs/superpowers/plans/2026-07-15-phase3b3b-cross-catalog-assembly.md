# Phase 3B.3b — Cross-Catalog Assembly (Governed Source→Target Physical Paths) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax. **B3 (physical transitions) + B4 (frontier search) are the algorithmic core — expect the deepest review scrutiny there.**

**Goal:** Extend the 3B.3a planner from single-catalog ingredient binding to a governed cross-catalog **source→target physical path** — a bounded frontier search over exact physical positions that realizes each semantic roll-up hop by an intra-catalog realization (R) or a cross-catalog roll-up bridge (B), fail-closed, in shadow.

**Architecture:** New `planner/assembly.py` (the physics + the frontier search) consumed by an extended `planner/plan.py`. Consumes `resolve_entity_compatibility → EntitySemanticPathV1` (3A), `derive_catalog_realizations → CatalogEntityRelationshipV1` (3B.2A), `active_bridges → ActiveBridgeV1` (3B.2B), `object_grain`/`key_entity`/`table_of`. Log-only, behaviour-neutral, no migration (store is 3B.4). Extends the 3B.3a contracts additively.

**Tech Stack:** Python 3.11 (frozen dataclasses, StrEnum), PostgreSQL (read-only), pytest (`db` fixture). `uv run pytest/ruff/mypy`.

## Global Constraints (from the spec — every task's requirements include these)

- **Shadow / behaviour-neutral / no migration.** Computed + logged on the same entity-scoped route path (already savepoint-isolated); no response/disposition/ranking/live-path change; full `tests/featuregen/` suite green except the planner's own tests.
- **Bounded FRONTIER search, never greedy.** State-space search (BFS over bridge count) — expand *all* deterministically-ordered permitted transitions per state; a locally-valid realization may dead-end while another path completes.
- **Exact physical-position continuity.** Position = `(entity, catalog, table_object_ref)`. A realizer/bridge is usable only when its source table == the current table (+ catalog); a bridge endpoint's *table* must equal the current table. Output is an executable path.
- **Hop realized by (R) intra-catalog realization OR (B) cross-catalog roll-up bridge.** (R): a `CatalogEntityRelationshipV1` in the current catalog, `from_object_ref == current_table`, grains == the hop. (B): the current table has an `E2`-entity FK column; a VERIFIED bridge at `E2` links it to an `E2`-grain table in another catalog. A **reposition** bridge (same entity, cross catalog) does not advance a hop.
- **Governed-bridge-only, fail-closed.** Crossings use only `active_bridges` (VERIFIED, in-scope); `find_cross_catalog_path` untouched. Needed-but-missing crossing → `unsanctioned_bridge`; no realizer → `missing_realization`; both scoped to the frozen `CatalogScopeV1`, never revealing inaccessible catalogs; never fabricate a bridge segment.
- **Tier (bridge count) ≠ `path_resolution_status`.** `tier_1`(0)/`tier_2`(1)/`tier_3`(≥2). Zero-bridge roll-up is NEW work.
- **Multi-grain ingredients rejected up-front** with `unsupported_multi_grain_ingredients`.
- **Whole-tier completion before deeper expansion; equal paths = `resolved_with_ambiguity`; candidate-local-first; bounded + deterministic** (frontier bound + truncation recorded); a canonical `BindingPlanV1` constructor + a bumped `PLAN_CONTRACT_VERSION`; symmetric-bridge normalization + cycle prevention (same bridge fact never twice).
- **Ranking precedence:** validity/safety → bridge_count → ingredient-binding rank → semantic-path rank → physical-realization rank (authority) → canonical tie-break.
- **Convention:** frozen dataclasses; lowercase-snake `StrEnum`. ruff `collections.abc`, E402 top-of-file. Branch `feature/phase3b3b-cross-catalog-assembly`; harness default commit trailer.

## Reused interfaces (verified)
- 3A: `from featuregen.overlay.upload.taxonomy.entity_graph import resolve_entity_compatibility, ENTITY_GRAPH`; `resolve_entity_compatibility(source, target, ENTITY_GRAPH) -> EntityCompatibilityResultV1(status, source_entity, target_entity, paths: tuple[EntitySemanticPathV1,...], reason_codes, graph_version, paths_truncated)`; `EntityCompatibility` (exact/derivable/ambiguous/unknown); `EntitySemanticPathV1(hops: tuple[EntityRelationshipRefV1,...])`; `EntityRelationshipRefV1(relationship_id, relationship_version, from_entity, to_entity, cardinality, aggregation_required, aggregation_strategy)`.
- 3B.2A: `from featuregen.overlay.upload.catalog_realizations import derive_catalog_realizations, object_grain, table_of`; `CatalogEntityRelationshipV1(realization_id, relationship_id, catalog_source, from_object_ref, from_object_grain, to_object_ref, to_object_grain, from_key_ref, from_key_entity, to_key_ref, to_key_entity, declared_cardinality, authority, status, reversed_authoring)`; `derive_catalog_realizations(conn, catalog_source).realizations`.
- 3B.2B: `from featuregen.overlay.upload.bridge_projection import active_bridges`; `ActiveBridgeV1(fact_key, entity_id, left_catalog_source, left_object_ref, right_catalog_source, right_object_ref)` (endpoints are identifier COLUMN refs; UNORDERED).
- 3B.3a: `planner/contracts.py`, `planner/plan.py` (`plan_bindings`, `_envelope`, `_differential`), `planner/order.py`; `discover_ingredient_candidates`; `object_grain(conn, catalog, table_ref) -> str|None`; `key_entity(conn, catalog, column_ref) -> str|None`.
- Column enumeration (verified schema): `SELECT object_ref, is_grain, concept FROM graph_node WHERE catalog_source=%s AND table_name=%s AND kind='column'`; a column's entity via `key_entity(conn, catalog_source, column_object_ref) -> str|None`; a table's grain via `object_grain(conn, catalog_source, table_object_ref) -> str|None`; `table_of(column_object_ref) -> str`. The `is_grain` flag identifies the grain-key column (used by the reposition transition).

## File Structure

| File | Responsibility |
|---|---|
| `planner/contracts.py` (MODIFY) — B1 | additive contract fields + enums + bounds + version bump |
| `planner/assembly.py` (CREATE) — B2/B3/B4 | eligibility + source resolution + the transition physics + the bounded frontier + tier search |
| `planner/plan.py` (MODIFY) — B5 | enrich tier-1 bindings into realized plans; classification |
| Tests | `tests/featuregen/overlay/upload/planner/test_assembly.py` + updates to `test_contracts.py`/`test_plan.py` |

---

### Task B1: Contract additions + version bump

**Files:** Modify `src/featuregen/overlay/upload/planner/contracts.py`; Test `tests/featuregen/overlay/upload/planner/test_contracts.py` (append) + update `test_plan.py`/`test_enumerate.py` fixtures for the new `BindingPlanV1` fields.

**Interfaces produced:** `PlanTier.tier_2_one_bridge`/`tier_3_multi_bridge`; `PathResolutionStatus`; `CandidateRole`; `BindingPlanV1` gains `participating_catalogs`/`bridge_count`/`path_resolution_status`/`candidate_role`; `PlanResolutionStatus.resolved_with_ambiguity`; new `ReasonCode`s; bounds; `BoundingMetricsV1`/`PlannerReplayEnvelopeV1` additions; `PLAN_CONTRACT_VERSION`; `tier_from_bridge_count(n)`; a canonical `make_binding_plan(...)` constructor that validates + computes derived fields.

- [ ] **Step 1: Write failing tests** — append to `test_contracts.py`:

```python
def test_new_enum_members():
    assert c.PlanTier.tier_2_one_bridge == "tier_2_one_bridge"
    assert c.PlanTier.tier_3_multi_bridge == "tier_3_multi_bridge"
    assert {s.value for s in c.PathResolutionStatus} == {
        "ingredient_binding_only", "source_to_target_resolved", "source_to_target_rejected"}
    assert c.PlanResolutionStatus.resolved_with_ambiguity == "resolved_with_ambiguity"
    for r in ("unsupported_multi_grain_ingredients", "ambiguous_semantic_path",
              "bounded_out_max_bridges", "bounded_out_max_frontier_states"):
        assert r in {x.value for x in c.ReasonCode}


def test_tier_from_bridge_count():
    assert c.tier_from_bridge_count(0) is c.PlanTier.tier_1_single_catalog
    assert c.tier_from_bridge_count(1) is c.PlanTier.tier_2_one_bridge
    assert c.tier_from_bridge_count(3) is c.PlanTier.tier_3_multi_bridge


def test_make_binding_plan_validates_and_derives():
    seg = c.BindingPathSegmentV1(c.SegmentKind.direct_catalog, "core")
    plan = c.make_binding_plan(
        recipe_id="t", target_entity="customer", catalog_source="core",
        ingredient_bindings=(), path_segments=(seg,),
        resolution_status=c.PlanResolutionStatus.resolved,
        path_resolution_status=c.PathResolutionStatus.source_to_target_resolved,
        primary_reason_code=None, reason_codes=(), safety=c.BindingSafety.safe,
        preference_rank=0, preference_reasons=(), candidate_role=c.CandidateRole.selected)
    assert plan.participating_catalogs == ("core",) and plan.bridge_count == 0
    assert plan.tier is c.PlanTier.tier_1_single_catalog
    assert len(plan.plan_id) > 3


def test_make_binding_plan_rejects_bad_participation():
    import pytest
    bridge = c.BindingPathSegmentV1(c.SegmentKind.governed_bridge, "other", from_entity="account",
                                    to_entity="account", bridge_fact_key="b1")
    with pytest.raises(ValueError):
        # a governed_bridge to catalog 'other' whose catalog isn't first / participation mismatch
        c.make_binding_plan(recipe_id="t", target_entity="c", catalog_source="core",
                            ingredient_bindings=(), path_segments=(bridge,),
                            resolution_status=c.PlanResolutionStatus.resolved,
                            path_resolution_status=c.PathResolutionStatus.source_to_target_resolved,
                            primary_reason_code=None, reason_codes=(), safety=c.BindingSafety.safe,
                            preference_rank=0, preference_reasons=(), candidate_role=c.CandidateRole.selected)
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/featuregen/overlay/upload/planner/test_contracts.py -q` → FAIL.

- [ ] **Step 3: Implement** — in `contracts.py`:
  - Extend `PlanTier` with `tier_2_one_bridge = "tier_2_one_bridge"`, `tier_3_multi_bridge = "tier_3_multi_bridge"`.
  - Add `class PathResolutionStatus(StrEnum): ingredient_binding_only / source_to_target_resolved / source_to_target_rejected`.
  - Add `class CandidateRole(StrEnum): selected / equal_rank_alternative / lower_rank_alternative / rejected`.
  - Add to `PlanResolutionStatus`: `resolved_with_ambiguity = "resolved_with_ambiguity"`.
  - Add to `ReasonCode`: `unsupported_multi_grain_ingredients`, `ambiguous_semantic_path`, `bounded_out_max_bridges`, `bounded_out_max_realizations_per_hop`, `bounded_out_max_frontier_states` (and the already-reserved `missing_realization`/`unsanctioned_bridge`/`ambiguous_equal_cross_catalog_paths` are now live). Bump `REASON_CODE_REGISTRY_VERSION = "1.1.0"`.
  - Add `PLAN_CONTRACT_VERSION = "3b3b.1.0.0"`; bounds `MAX_BRIDGES_PER_PLAN = 2`, `MAX_REALIZATIONS_PER_HOP = 4`, `MAX_PHYSICAL_PATHS_PER_BINDING = 16`, `MAX_STATES_EXPANDED_PER_BINDING = 512`.
  - Extend `BindingPlanV1` with fields (after the existing ones): `participating_catalogs: tuple[str, ...]`, `bridge_count: int`, `path_resolution_status: PathResolutionStatus`, `candidate_role: CandidateRole`.
  - Extend `BoundingMetricsV1` with: `realizations_truncated: bool`, `bridge_transitions_truncated: bool`, `frontier_states_truncated: bool`, `deeper_tiers_not_explored: bool`, `total_states_expanded: int`, `total_bridge_transitions_explored: int` (append; update the 3B.3a `BoundingMetricsV1(...)` construction sites in `plan.py` to pass the new args — default `False`/`0`).
  - Extend `PlannerReplayEnvelopeV1` with: `active_bridge_fact_keys: tuple[str, ...]`, `plan_contract_version: str` (`bridge_derivation_version` already present).
  - Add helpers:

```python
def tier_from_bridge_count(n: int) -> PlanTier:
    if n == 0:
        return PlanTier.tier_1_single_catalog
    if n == 1:
        return PlanTier.tier_2_one_bridge
    return PlanTier.tier_3_multi_bridge


def make_binding_plan(*, recipe_id, target_entity, catalog_source, ingredient_bindings,
                      path_segments, resolution_status, path_resolution_status, primary_reason_code,
                      reason_codes, safety, preference_rank, preference_reasons, candidate_role) -> BindingPlanV1:
    """The ONE canonical constructor: derives participating_catalogs (ordered by first traversal, dedup,
    catalog_source first), bridge_count, tier, and a plan_id over the canonical content + PLAN_CONTRACT_VERSION;
    validates the structural invariants. participating_catalogs cannot be a static default (it depends on
    catalog_source + segments), which is why this constructor exists."""
    participating: list[str] = [catalog_source]
    for seg in path_segments:
        if seg.catalog_source not in participating:
            participating.append(seg.catalog_source)
    bridge_count = sum(1 for s in path_segments if s.segment_kind is SegmentKind.governed_bridge)
    tier = tier_from_bridge_count(bridge_count)
    # structural validation (fail closed on a malformed plan)
    if participating[0] != catalog_source:
        raise ValueError("participating_catalogs[0] must be catalog_source")
    if len(set(participating)) != len(participating):
        raise ValueError("participating_catalogs has duplicates")
    for s in path_segments:
        if s.catalog_source not in participating:
            raise ValueError(f"segment catalog {s.catalog_source!r} not in participating_catalogs")
    refs = tuple(sorted(b.bound_object_ref for b in ingredient_bindings))
    material = (f"{recipe_id}|{catalog_source}|{'|'.join(refs)}|{tier}|"
               f"{'>'.join(f'{s.segment_kind}:{s.catalog_source}:{s.realization_ref or s.bridge_fact_key or \"\"}' for s in path_segments)}"
               f"|{PLANNER_VERSION}|{PLAN_CONTRACT_VERSION}")
    plan_id = "bp_" + hashlib.sha256(material.encode()).hexdigest()[:16]
    return BindingPlanV1(
        plan_id=plan_id, recipe_id=recipe_id, target_entity=target_entity, tier=tier,
        catalog_source=catalog_source, ingredient_bindings=ingredient_bindings, path_segments=path_segments,
        resolution_status=resolution_status, primary_reason_code=primary_reason_code, reason_codes=reason_codes,
        safety=safety, preference_rank=preference_rank, preference_reasons=preference_reasons,
        participating_catalogs=tuple(participating), bridge_count=bridge_count,
        path_resolution_status=path_resolution_status, candidate_role=candidate_role)
```
  (Add `import hashlib` if not present.)

- [ ] **Step 4: Update the 3B.3a construction sites** — in `enumerate.py`, replace the direct `BindingPlanV1(...)` construction (currently at `enumerate.py:88`) with `make_binding_plan(...)`, passing `path_resolution_status=PathResolutionStatus.ingredient_binding_only` and `candidate_role=CandidateRole.rejected` (default; the ranker resets it) — a 3B.3a tier-1 ingredient binding is `ingredient_binding_only` until B5's assembler enriches it. **Delete the now-superseded `_plan_id` helper (`enumerate.py:44`)** — `make_binding_plan` is the single plan-id authority (the old helper hardcoded `tier_1` and omitted segments + `PLAN_CONTRACT_VERSION`, so leaving it would create two divergent derivations). Update `order.py`'s `replace(...)` to keep the new fields. Update `test_enumerate.py`/`test_plan.py` assertions for the new fields (a tier-1 plan now also has `participating_catalogs=(catalog,)`, `bridge_count=0`, `path_resolution_status=ingredient_binding_only`) — plan_ids change (expected: `PLAN_CONTRACT_VERSION` bump), so update any hardcoded plan_id fixtures.

- [ ] **Step 5: Run + gates + commit**

```bash
uv run pytest tests/featuregen/overlay/upload/planner/ -q
uv run ruff check src/featuregen/overlay/upload/planner/contracts.py tests/featuregen/overlay/upload/planner/
uv run mypy src/featuregen/overlay/upload/planner/contracts.py
git add -A && git commit -m "feat(3b3b): planner contract additions + canonical make_binding_plan + version bump (task b1)"
```

---

### Task B2: Eligibility gate + source-entity resolution + semantic path

**Files:** Create `src/featuregen/overlay/upload/planner/assembly.py`; Test `tests/featuregen/overlay/upload/planner/test_assembly.py`.

**Interfaces produced:** `resolve_source_entity(template) -> str | None`; `ingredient_eligibility(template) -> EligibilityV1(eligible: bool, source_entity: str | None, reason: ReasonCode | None)`; `semantic_rollup_paths(source_entity, target_entity) -> tuple[tuple[EntitySemanticPathV1, ...], EntityCompatibility]`.

- [ ] **Step 1: Write failing tests** — `test_assembly.py`:

```python
# NOTE: fixtures use REAL concepts/entities verified against the registry + graph:
#   transaction_id -> entity_link "transaction"; customer_id -> "customer"; monetary_flow is entity-neutral.
#   "transaction"->"account" is DERIVABLE and "account"->"account" is EXACT in ENTITY_GRAPH (38 entities).
#   EntityCompatibility members are UPPERCASE (EXACT/DERIVABLE/AMBIGUOUS/UNKNOWN).
from featuregen.overlay.upload.planner.assembly import ingredient_eligibility, semantic_rollup_paths
from featuregen.overlay.upload.planner.contracts import ReasonCode
from featuregen.overlay.upload.taxonomy.entity_relationships import EntityCompatibility
from featuregen.overlay.upload.templates import Need, Template


def _tmpl(needs, *, source_entity_need_role=None):
    return Template(id="t3b3b", family="f", intent="i", needs=needs, params={}, aggregation="sum",
                    additivity="additive", explain="M", use_cases=(), pit="trailing",
                    source_entity_need_role=source_entity_need_role)


def test_single_source_entity_eligible():
    # a lone transaction-grain key -> source resolves to 'transaction'; nothing gates
    t = _tmpl((Need(role="txn", concept="transaction_id"),))
    e = ingredient_eligibility(t)
    assert e.eligible is True and e.source_entity == "transaction"


def test_multi_grain_ingredient_rejected():
    # source anchored on the transaction key; a REQUIRED customer-grain need is a second grain -> rejected
    t = _tmpl((Need(role="txn", concept="transaction_id"), Need(role="cust", concept="customer_id")),
              source_entity_need_role="txn")
    e = ingredient_eligibility(t)
    assert e.eligible is False and e.reason is ReasonCode.unsupported_multi_grain_ingredients


def test_no_single_source_grain_is_skipped_not_rejected():
    # an entity-neutral measure-only recipe has NO SOURCE_ENTITY_KEY -> skipped, NOT a rejection
    t = _tmpl((Need(role="amt", concept="monetary_flow"),))
    e = ingredient_eligibility(t)
    assert e.eligible is False and e.reason is None


def test_semantic_rollup_paths_derivable():
    paths, status = semantic_rollup_paths("transaction", "account")
    assert status is EntityCompatibility.DERIVABLE
    assert paths and all(p.hops[0].from_entity == "transaction" for p in paths)


def test_exact_source_equals_target_is_empty_path():
    paths, status = semantic_rollup_paths("account", "account")
    assert status is EntityCompatibility.EXACT and paths == ()
```

- [ ] **Step 2: Run to verify it fails** — FAIL (module missing).

- [ ] **Step 3: Implement** the top of `assembly.py`:

```python
"""Phase-3B.3b — cross-catalog assembly: eligibility, source-entity resolution, semantic paths, the
physical-transition physics, and the bounded frontier search. Read-only, deterministic."""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.overlay.upload.binding_roles import JoinRole
from featuregen.overlay.upload.need_metadata import ResolvedNeedMetadataV1, derive_need_metadata
from featuregen.overlay.upload.planner.contracts import ReasonCode
from featuregen.overlay.upload.taxonomy.entity_graph import ENTITY_GRAPH, resolve_entity_compatibility
from featuregen.overlay.upload.taxonomy.entity_relationships import EntityCompatibility, EntitySemanticPathV1
from featuregen.overlay.upload.templates import Template


@dataclass(frozen=True, slots=True)
class EligibilityV1:
    eligible: bool
    source_entity: str | None
    reason: ReasonCode | None


def _resolved(template: Template) -> tuple[ResolvedNeedMetadataV1, ...]:
    """The GOVERNED per-need resolution (3B.1) — reuse it; never re-derive source grain from concepts here.
    ``derive_need_metadata`` is the pure function behind the ``RESOLVED_NEED_METADATA`` corpus registry and
    raises ``ValueError`` on an ambiguous anchor (the caller treats that as not-eligible)."""
    return derive_need_metadata(template)


def resolve_source_entity(template: Template) -> str | None:
    """The recipe's single source-grain entity, from the GOVERNED 3B.1 resolution: the sole need resolved to
    ``JoinRole.SOURCE_ENTITY_KEY`` and its single ``allowed_source_grain``. 0-or-many source keys, a source key
    with 0-or-many grains, or an ambiguous anchor -> None (never guessed from whichever catalog bound)."""
    try:
        metas = _resolved(template)
    except ValueError:
        return None
    sources = [m for m in metas if m.join_role is JoinRole.SOURCE_ENTITY_KEY]
    if len(sources) != 1:
        return None
    grains = sources[0].allowed_source_grains
    return grains[0] if len(grains) == 1 else None


def ingredient_eligibility(template: Template) -> EligibilityV1:
    """3B.3b handles SOURCE-GRAIN ingredients only. A recipe with no single governed source grain is SKIPPED
    (eligible=False, reason=None — not a rejection; it stays an ingredient-binding-only tier-1 candidate). A
    REQUIRED need governed to a single grain DIFFERENT from the source (a second entity that would need its own
    roll-up, e.g. a resolved ``INTERMEDIATE_ENTITY_KEY``) -> unsupported_multi_grain_ingredients. Optional needs
    and entity-neutral MEASURE/TIME needs (unconstrained grains) never gate."""
    source = resolve_source_entity(template)
    if source is None:
        return EligibilityV1(False, None, None)
    by_role = {m.role: m for m in _resolved(template)}
    for need in template.needs:
        if need.optional:
            continue
        m = by_role.get(need.role)
        if m is None:
            continue
        grains = m.allowed_source_grains
        if len(grains) == 1 and grains[0] != source:
            return EligibilityV1(False, source, ReasonCode.unsupported_multi_grain_ingredients)
    return EligibilityV1(True, source, None)


def semantic_rollup_paths(source_entity: str, target_entity: str
                          ) -> tuple[tuple[EntitySemanticPathV1, ...], EntityCompatibility]:
    """The governed roll-up paths source->target. EXACT (source==target) -> (); DERIVABLE -> one path;
    AMBIGUOUS -> >=2; UNKNOWN -> ()."""
    res = resolve_entity_compatibility(source_entity, target_entity, ENTITY_GRAPH)
    return res.paths, res.status
```

- [ ] **Step 4: Run to pass; gates; commit**

```bash
uv run pytest tests/featuregen/overlay/upload/planner/test_assembly.py -q
uv run ruff check src/featuregen/overlay/upload/planner/assembly.py; uv run mypy src/featuregen/overlay/upload/planner/assembly.py
git add -A && git commit -m "feat(3b3b): eligibility gate + source-entity resolution + semantic paths (task b2)"
```

---

### Task B3: Physical-transition physics (R / roll-up bridge B / reposition)

**Files:** Modify `assembly.py` (append); Test `test_assembly.py` (append). DB-backed.

**Interfaces produced:** `_Position(entity, catalog, table_ref)`; `realize_in_place(conn, pos, hop, scope) -> tuple[_Realization, ...]` (R candidates); `rollup_bridges(conn, pos, hop, scope) -> tuple[_BridgeMove, ...]` (B candidates); `reposition_bridges(conn, pos, scope) -> tuple[_BridgeMove, ...]`. Each returns the next `_Position` + the `BindingPathSegmentV1` to emit + (for bridges) the `fact_key`.

- [ ] **Step 1: Write failing DB tests** (build a two-catalog acceptance fixture; assert each transition finds the right move; assert continuity + exactness). *[The brief carries the full fixture + the R/B/reposition assertions — see the spec's mandatory tests #1/#3/#4/#5.]*

- [ ] **Step 2: Run RED.**

- [ ] **Step 3: Implement** the transition helpers in `assembly.py` (deterministically ordered; exact continuity):
  - `realize_in_place`: `for r in derive_catalog_realizations(conn, pos.catalog).realizations: if r.from_object_ref == pos.table_ref and r.from_object_grain == hop.from_entity and r.to_object_grain == hop.to_entity: yield a move to _Position(hop.to_entity, pos.catalog, r.to_object_ref)` emitting `semantic_rollup` + `intra_catalog_realization(realization_ref=r.realization_id)`. Sorted by `(r.authority, r.realization_id)` (approved_join before declared_join).
  - `rollup_bridges`: the current table's columns whose `key_entity == hop.to_entity` (the E2 FK); for each such column `k`, find a VERIFIED `active_bridge` with `entity_id == hop.to_entity` and an endpoint `== (pos.catalog, k)`; the *other* endpoint `(cat2, k2)` where `object_grain(conn, cat2, table_of(k2)) == hop.to_entity` (k2's table is E2-grain) → a move to `_Position(hop.to_entity, cat2, table_of(k2))` emitting `semantic_rollup` + `governed_bridge(bridge_fact_key, from_entity=hop.from_entity, to_entity=hop.to_entity)`. Normalize left/right (symmetric). Only bridges whose BOTH endpoint catalogs ∈ `scope.authorized_catalog_sources`.
  - `reposition_bridges`: the current table's grain-key column (the column whose `key_entity == pos.entity` and `is_grain`); a VERIFIED bridge at `pos.entity` with an endpoint `== (pos.catalog, grain_key)`, other endpoint `(cat2, k2)` where `object_grain(conn, cat2, table_of(k2)) == pos.entity` (same grain) → a move to `_Position(pos.entity, cat2, table_of(k2))` emitting `governed_bridge(from_entity=pos.entity, to_entity=pos.entity)` (reposition; entity unchanged). Scope-confined + symmetric-normalized.
  - Each helper is read-only, deterministic (sorted), and returns `()` when nothing matches.

- [ ] **Step 4: Run GREEN; gates; commit** (`feat(3b3b): physical transitions — realization / roll-up bridge / reposition (task b3)`).

---

### Task B4: Bounded frontier search + layered tiers + ranking + ambiguity

**Files:** Modify `assembly.py` (append); Test `test_assembly.py` (append — the adversarial cases: non-greedy dead-end, cycle, whole-tier, ambiguity, bounds).

**Interfaces produced:** `assemble_paths(conn, *, source_position, semantic_path, scope, ingredient_bindings, template, target_entity) -> AssemblyV1(complete: tuple[BindingPlanV1,...], rejected: tuple[BindingPlanV1,...], bounding fields)` — a bounded BFS over states, per (source binding × semantic path); plus `rank_and_classify(...)` for the layered tiers + `resolved_with_ambiguity`.

- [ ] **Step 1: Write failing adversarial tests** (spec tests #3 non-greedy, #6 unsanctioned, #7 missing, #8 ambiguity, #12 cycle, #9/#10 tiers). *[full fixtures in the brief]*

- [ ] **Step 2–4:** Implement the frontier search:
  - `_State = (hop_index, position, segments, bridge_count, participating, used_bridge_fact_keys)`; visited key `(position.entity, position.catalog, position.table_ref, frozenset(used_bridge_fact_keys))`; **same bridge fact never reused** (guard on `fact_key in used_bridge_fact_keys`).
  - BFS: from each state expand all transitions in deterministic order — realize-in-place (R) + roll-up bridges (B) advance the hop; reposition bridges do not (only when the current hop can't yet realize but a same-entity crossing might unlock it); enforce `MAX_BRIDGES_PER_PLAN`, `MAX_REALIZATIONS_PER_HOP`, `MAX_PHYSICAL_PATHS_PER_BINDING`, `MAX_STATES_EXPANDED_PER_BINDING` (record each truncation flag). A state with `hop_index == len(hops)` and `entity == target` → a complete path → `make_binding_plan(...)` with `source_to_target_resolved`. A state that can expand no transition → a rejected candidate with `missing_realization` (no R/B) or `unsanctioned_bridge` (a realizer exists in another in-scope catalog but no verified bridge reaches it) + evidence.
  - **Layered tiers:** `rank_and_classify` groups complete plans by `bridge_count`; if any bridge_count-0 plans exist, keep them + set `deeper_tiers_not_explored=True` for higher tiers (do not expand — controlled at the caller: run the search with `max_bridges=0` first, then `=1`, ... only if none complete). Rank by the precedence (validity → bridge_count → ingredient-binding rank (reuse `order.py`) → semantic-path rank (shorter) → realization authority → canonical tie-break). Full-key ties (except plan_id) → `resolved_with_ambiguity`, one `selected` + `equal_rank_alternative`s.

- [ ] **Step 5: gates + commit** (`feat(3b3b): bounded frontier search + layered tiers + ranking + ambiguity (task b4)`).

---

### Task B5: Wire into `plan_bindings` + replay + behaviour-neutral proof + acceptance tests

**Files:** Modify `planner/plan.py`; Test `test_plan.py`/`test_assembly.py` (the acceptance path #1, zero-bridge #2, reposition #2b, multi-grain #11, determinism #13, out-of-scope #14).

- [ ] **Step 1–3:** In `plan_bindings`, after building the 3B.3a tier-1 ingredient candidates per catalog: run `ingredient_eligibility` (a multi-grain reject short-circuits to a `unsupported_multi_grain_ingredients` result before assembly); for each surviving source-binding, resolve the semantic paths and run the layered `assemble_paths` search; classify the result (`resolved` / `resolved_with_ambiguity` / `source_to_target_rejected` via `missing_realization`/`unsanctioned_bridge` / `bounded_out` / `not_applicable`) candidate-local-first (a rejected path never downgrades a resolved result). Extend `_envelope` to pin `active_bridge_fact_keys` (from `active_bridges` filtered to the scope) + `plan_contract_version=PLAN_CONTRACT_VERSION`. The tier-1 ingredient-only candidates whose roll-up wasn't evaluated stay `ingredient_binding_only`.

- [ ] **Step 4: Behaviour-neutral proof** — `uv run pytest tests/featuregen/ -q` → prior total + new planner tests, zero new failures; the route path is unchanged (still the same log-only, savepoint-isolated block). The considered-set API tests are byte-identical.

- [ ] **Step 5: gates + commit** (`feat(3b3b): assembler wired into plan_bindings + replay + acceptance tests (task b5)`).

---

## Exit criteria mapping

| Spec requirement | Task |
|---|---|
| Contract additions + canonical constructor + version bump | B1 |
| Multi-grain reject up-front; single-source resolution; semantic paths | B2 |
| (R)/(B)/reposition physics with exact continuity + governed-bridge-only + scope confinement | B3 |
| Bounded frontier search (non-greedy), cycle prevention, frontier bound | B4 |
| Whole-tier completion; ranking precedence; `resolved_with_ambiguity`; candidate-local-first | B4 + B5 |
| `missing_realization`/`unsanctioned_bridge` scoped, evidence, no fabricated segment | B3 + B4 |
| Wire log-only into `plan_bindings`; replay additions; behaviour-neutral | B5 |
| The 14 adversarial tests | B3/B4/B5 |
| No migration / no store (3B.4) | (none) |

## Self-Review

**Spec coverage:** every spec section maps to a task. The corrected hop-realization model (R vs roll-up bridge B vs reposition) is B3's three helpers + the grammar in `make_binding_plan`/segments. ✅
**Placeholder scan:** B1/B2 carry complete code; B3/B4 carry the exact algorithm + signatures + the mandatory adversarial tests as the behavioural contract (the frontier search is genuinely algorithmic — the implementer writes the driver against the tests, and B3/B4 get the deepest review). This is a deliberate, flagged choice, not a placeholder. ✅
**Type consistency:** `make_binding_plan` is the ONE plan constructor (B1); B3's transitions emit `BindingPathSegmentV1`s consumed by B4's states; B4's complete states → `make_binding_plan`; B5 classifies + wires. `BoundingMetricsV1`/`PlannerReplayEnvelopeV1` additions are threaded B1→B5. ✅
**Executor note:** B1 touches the 3B.3a construction sites (`enumerate.py`/`order.py`) + their tests — a mechanical migration to `make_binding_plan` with the new fields; if a 3B.3a planner test reddens, it's the new-field migration, not a logic change. B3/B4 are the algorithmic core; the frontier search must be **bounded-first** (never greedy) — the non-greedy dead-end test (#3) is the gate.

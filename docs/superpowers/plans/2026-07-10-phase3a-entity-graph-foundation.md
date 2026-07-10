# Phase 3A — Entity & Grain Graph Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hardcoded 5-entry `_ENTITY_ROLLUP` map with a curated, versioned, **acyclic** global semantic entity-relationship graph, and rewire `entity_compatibility` to traverse it — **regression-equivalently** (same outputs for every existing pair) — while locking stable, *validated* (but inactive) contracts for the later cross-catalog edge classes.

**Architecture:** A new pure, DB-free module trio under `taxonomy/`: `entity_relationships.py` (frozen-dataclass contracts + enums + per-contract validators), `entity_registry.py` (the curated `ENTITY_RELATIONSHIPS_V1` seed = the 5 roll-ups + `GRAPH_VERSION`), and `entity_graph.py` (an immutable, cycle-rejecting graph builder that carries the closed entity vocabulary + a bounded `resolve_entity_compatibility` traversal returning a path-bearing result). `ranking_signals.entity_compatibility` becomes a thin adapter over the resolver; the old map is deleted. Only `EntityRelationshipDefinitionV1` is an *active* graph edge — the other three contracts are defined, validated, and feasibility-tested against the **real** production `JoinEdge`/`EntityBridge` shapes, but never populated or traversed.

**Tech Stack:** Python 3.11, `@dataclass(frozen=True, slots=True)`, `StrEnum`, `uv run pytest`, `uv run ruff check`, `uv run mypy`. No pydantic (the taxonomy package uses frozen dataclasses). No DB, no migration.

## Global Constraints

- **Behaviour-neutral, no flag.** For every entity pair in the *entire* `known_entities()` vocabulary, the new graph-backed result must equal the old `_ENTITY_ROLLUP` result. Ranking order, rank reasons, grain-warning responses, and the considered-set/ranking API response are **semantically/schema-identical** (parsed-JSON equal, not byte-equal) — no new key (e.g. `graph_version`) leaks to any external response.
- **Seed = EXACTLY the five roll-ups:** `account→customer`, `card_account→customer`, `transaction→account`, `facility→obligor`, `policy→customer`. Acyclic, each source out-degree ≤1, so `AMBIGUOUS` is provably unreachable from the seed. Do NOT add a sixth relationship in 3A.
- **`AMBIGUOUS` is reserved capability:** enum member + traversal support, exercised by synthetic multi-path fixtures ONLY.
- **Only `EntityRelationshipDefinitionV1` is active.** The other three contracts are defined, **structurally validated**, and feasibility-tested, but never built into the graph or traversed.
- **No hard reject.** Do NOT add `EntityCompatibility.INCOMPATIBLE` (deferred to 3D).
- **Delete the old map** (`_ENTITY_ROLLUP`, `_rolls_up_to`) — no fallback.
- **No DB migration, no governance UI.** The registry is in-code.
- **Closed entity vocabulary:** every relationship endpoint (and every bridge/proposal entity) must be in `known_entities()`; the resolver returns `UNKNOWN` for out-of-vocabulary entities (never `EXACT`).
- **The curated semantic graph is acyclic and forward-only:** the builder rejects cycles, non-`FORWARD` active edges, and duplicate semantic edges. Cycle detection must accept a converging DAG (a shared descendant is not a back edge).
- **Active-only validation:** the builder validates + indexes only `ACTIVE` definitions; deprecated definitions are archived, neither validated nor traversed (documented, deliberate).
- **Production graphs come only from `build_entity_graph`.** Direct `EntityGraph(...)` construction bypasses all invariants and is test-only (a `_unsafe_graph_for_test` helper).
- **Immutable for a process lifetime:** the graph + entity vocabulary are built once at import from the in-code registries and rebuilt only on redeploy — no runtime hot-reload.
- Commit messages end with: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Branch `feature/phase3-cross-catalog` is already checked out.

## File Structure

- **Create** `src/featuregen/overlay/upload/taxonomy/entity_relationships.py` — enums (`EntityCompatibility` +`AMBIGUOUS`; `RelationshipType`, `Cardinality`, `TraversalDirection`, `AggregationStrategy`, `RelationshipStatus`, `RelationshipProposalStatus`, `GraphEdgeAuthority`), the four edge contracts (typed authorities), the result types (`EntityRelationshipRefV1`, `EntitySemanticPathV1`, `EntityCompatibilityResultV1`), and four validators. Depends only on `dimensions.known_entities`. [3A.1]
- **Create** `src/featuregen/overlay/upload/taxonomy/entity_registry.py` — `GRAPH_VERSION` + `ENTITY_RELATIONSHIPS_V1`. [3A.2]
- **Create** `src/featuregen/overlay/upload/taxonomy/entity_graph.py` — `EntityGraph` (carries `known_entities`), `build_entity_graph` (cycle/duplicate/direction-rejecting), the singleton `ENTITY_GRAPH`, bounded `resolve_entity_compatibility`. [3A.3, 3A.4]
- **Modify** `src/featuregen/overlay/upload/taxonomy/ranking_signals.py` — delete `_ENTITY_ROLLUP`/`_rolls_up_to` + the local `EntityCompatibility`; import + re-export `EntityCompatibility`; rewire `entity_compatibility`. [3A.5]
- **Create** tests: `test_entity_relationships.py` [3A.1], `test_entity_graph.py` [3A.3/3A.4], `test_entity_compatibility_regression.py` [3A.5], `test_entity_contract_feasibility.py` [3A.6] under `tests/featuregen/overlay/upload/taxonomy/`.

Import DAG (no cycles): `entity_relationships` ← `entity_registry` ← `entity_graph` ← `ranking_signals`.

---

### Task 1 (3A.1): Entity graph contracts + validators

**Files:**
- Create: `src/featuregen/overlay/upload/taxonomy/entity_relationships.py`
- Test: `tests/featuregen/overlay/upload/taxonomy/test_entity_relationships.py`

**Interfaces:**
- Consumes: `from featuregen.overlay.upload.taxonomy.dimensions import known_entities`.
- Produces: the enums above; the dataclasses `EntityRelationshipDefinitionV1`, `CatalogEntityRelationshipV1`, `EntityBridgeV1`, `EntityRelationshipProposalV1`, `EntityRelationshipRefV1`, `EntitySemanticPathV1`, `EntityCompatibilityResultV1`; and `validate_relationship_definition(defn, *, known)`, `validate_catalog_relationship(real, *, known)`, `validate_entity_bridge(bridge, *, known)`, `validate_relationship_proposal(prop, *, known)` (all raise `ValueError`).

- [ ] **Step 1: Write the failing tests**

Create `tests/featuregen/overlay/upload/taxonomy/test_entity_relationships.py`:

```python
"""Phase-3A Task 3A.1 — the entity-relationship contracts + structural validators.

Only EntityRelationshipDefinitionV1 is an active graph edge in 3A; the other three contracts are
defined + STRUCTURALLY validated here (no global-registry cross-check — that is 3B) so 3B builds against
stable, self-consistent types."""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.taxonomy.dimensions import known_entities
from featuregen.overlay.upload.taxonomy.entity_relationships import (
    AggregationStrategy,
    Cardinality,
    CatalogEntityRelationshipV1,
    EntityBridgeV1,
    EntityCompatibility,
    EntityRelationshipDefinitionV1,
    EntityRelationshipProposalV1,
    GraphEdgeAuthority,
    RelationshipProposalStatus,
    RelationshipStatus,
    RelationshipType,
    TraversalDirection,
    validate_catalog_relationship,
    validate_entity_bridge,
    validate_relationship_definition,
    validate_relationship_proposal,
)

KNOWN = known_entities()


def _defn(**overrides) -> EntityRelationshipDefinitionV1:
    base = dict(
        relationship_id="account_to_customer", from_entity="account", to_entity="customer",
        relationship_type=RelationshipType.ROLLUP, cardinality=Cardinality.MANY_TO_ONE,
        traversal_direction=TraversalDirection.FORWARD, aggregation_required=True,
        aggregation_strategy=AggregationStrategy.RECIPE_DECLARED, status=RelationshipStatus.ACTIVE,
        version="1.0.0")
    base.update(overrides)
    return EntityRelationshipDefinitionV1(**base)


def test_ambiguous_member_present_incompatible_absent():
    assert EntityCompatibility.AMBIGUOUS.value == "ambiguous"
    assert not hasattr(EntityCompatibility, "INCOMPATIBLE")   # hard reject deferred to 3D


def test_valid_definition_passes():
    validate_relationship_definition(_defn(), known=KNOWN)


def test_dangling_endpoint_rejected():
    with pytest.raises(ValueError, match="unknown entity"):
        validate_relationship_definition(_defn(to_entity="not_an_entity"), known=KNOWN)


def test_all_self_edges_rejected():
    # No identity exception: the EXACT short-circuit handles entity identity; a self roll-up is redundant.
    with pytest.raises(ValueError, match="self-relationship"):
        validate_relationship_definition(_defn(from_entity="customer", to_entity="customer"), known=KNOWN)
    with pytest.raises(ValueError, match="self-relationship"):
        validate_relationship_definition(
            _defn(from_entity="customer", to_entity="customer",
                  relationship_type=RelationshipType.IDENTITY), known=KNOWN)


def test_rollup_must_be_forward():
    with pytest.raises(ValueError, match="forward"):
        validate_relationship_definition(_defn(traversal_direction=TraversalDirection.BOTH), known=KNOWN)


def test_aggregation_required_must_have_strategy():
    with pytest.raises(ValueError, match="aggregation"):
        validate_relationship_definition(
            _defn(aggregation_required=True, aggregation_strategy=AggregationStrategy.NOT_APPLICABLE),
            known=KNOWN)
    with pytest.raises(ValueError, match="aggregation"):
        validate_relationship_definition(
            _defn(aggregation_required=False, aggregation_strategy=AggregationStrategy.RECIPE_DECLARED),
            known=KNOWN)


def test_invalid_version_rejected():
    with pytest.raises(ValueError, match="version"):
        validate_relationship_definition(_defn(version="v1"), known=KNOWN)


def _catalog(**overrides) -> CatalogEntityRelationshipV1:
    base = dict(
        realization_id="core_accounts:accounts.account_id->accounts.customer_id",
        relationship_id="account_to_customer", catalog_source="core_accounts",
        from_object_ref="accounts.account_id", to_object_ref="accounts.customer_id",
        resolved_from_entity="account", resolved_to_entity="customer",
        declared_cardinality=Cardinality.MANY_TO_ONE, adapter_id="core_banking_adapter",
        authority=GraphEdgeAuthority.CATALOG_DECLARED, status=RelationshipStatus.ACTIVE)
    base.update(overrides)
    return CatalogEntityRelationshipV1(**base)


def test_catalog_relationship_validation():
    validate_catalog_relationship(_catalog(), known=KNOWN)
    with pytest.raises(ValueError, match="empty"):
        validate_catalog_relationship(_catalog(catalog_source=""), known=KNOWN)
    with pytest.raises(ValueError, match="empty"):        # whitespace-only is empty
        validate_catalog_relationship(_catalog(adapter_id="   "), known=KNOWN)
    with pytest.raises(ValueError, match="identical"):
        validate_catalog_relationship(_catalog(to_object_ref="accounts.account_id"), known=KNOWN)
    with pytest.raises(ValueError, match="unknown entity"):
        validate_catalog_relationship(_catalog(resolved_to_entity="not_an_entity"), known=KNOWN)
    with pytest.raises(ValueError, match="authority"):
        validate_catalog_relationship(_catalog(authority=GraphEdgeAuthority.ENTITY_BRIDGE), known=KNOWN)


def _bridge(**overrides) -> EntityBridgeV1:
    base = dict(
        bridge_id="b1", entity_id="account", left_catalog_source="payments",
        left_object_ref="transactions.account_id", right_catalog_source="core_accounts",
        right_object_ref="accounts.account_id", authority=GraphEdgeAuthority.ENTITY_BRIDGE,
        status=RelationshipStatus.ACTIVE)
    base.update(overrides)
    return EntityBridgeV1(**base)


def test_entity_bridge_validation():
    validate_entity_bridge(_bridge(), known=KNOWN)
    with pytest.raises(ValueError, match="unknown entity"):
        validate_entity_bridge(_bridge(entity_id="not_an_entity"), known=KNOWN)
    with pytest.raises(ValueError, match="distinct catalog"):
        validate_entity_bridge(_bridge(right_catalog_source="payments"), known=KNOWN)
    with pytest.raises(ValueError, match="authority"):
        validate_entity_bridge(_bridge(authority=GraphEdgeAuthority.CATALOG_DECLARED), known=KNOWN)


def _proposal(**overrides) -> EntityRelationshipProposalV1:
    base = dict(
        proposal_id="p1", proposed_from_entity="account", proposed_to_entity="customer",
        proposed_cardinality=Cardinality.MANY_TO_ONE, evidence_refs=("edge:1",),
        source_catalog="core_accounts", inferred_by="join_inspector",
        status=RelationshipProposalStatus.PENDING)
    base.update(overrides)
    return EntityRelationshipProposalV1(**base)


def test_relationship_proposal_validation():
    validate_relationship_proposal(_proposal(), known=KNOWN)
    with pytest.raises(ValueError, match="unknown entity"):
        validate_relationship_proposal(_proposal(proposed_to_entity="not_an_entity"), known=KNOWN)
    with pytest.raises(ValueError, match="self-relationship proposal"):
        validate_relationship_proposal(_proposal(proposed_to_entity="account"), known=KNOWN)
    with pytest.raises(ValueError, match="evidence"):
        validate_relationship_proposal(_proposal(evidence_refs=()), known=KNOWN)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/featuregen/overlay/upload/taxonomy/test_entity_relationships.py -q`
Expected: FAIL — `ModuleNotFoundError: ... entity_relationships`.

- [ ] **Step 3: Write the implementation**

Create `src/featuregen/overlay/upload/taxonomy/entity_relationships.py`:

```python
"""Phase-3A — entity-relationship contracts, enums, result types, and structural validators.

The GLOBAL semantic relationship (:class:`EntityRelationshipDefinitionV1`) is the ONLY edge class the
3A graph traverses. The catalog-realization / entity-bridge / proposal contracts are defined + validated
here so Phase 3B builds against stable, self-consistent types — 3A never populates or traverses them.
Validators are pure and structural (a bridge's entity is known, a realization's refs differ, …); the
cross-check of a realization against the global registry is a Phase-3B concern.

``EntityCompatibility`` lives here (not in ``ranking_signals``) so the graph resolver and the ranking
adapter both import it without a cycle; ``ranking_signals`` re-exports it for its existing callers."""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def is_semver(value: str) -> bool:
    """Shared N.N.N check (used by the definition validator AND the graph builder)."""
    return bool(_SEMVER.match(value))


class EntityCompatibility(StrEnum):
    """Soft grain fit of a recipe to a confirmed ``target_entity`` (Phase-2B semantics, now graph-backed).
    ``EXACT`` grain == target; ``DERIVABLE`` a single roll-up path reaches it; ``AMBIGUOUS`` several
    distinct paths do (reserved — the 3A seed forest never emits it); ``UNKNOWN`` no target / no path /
    out-of-vocabulary. No ``INCOMPATIBLE`` — a hard entity reject is deferred to Phase 3D."""

    EXACT = "exact"
    DERIVABLE = "derivable"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


class RelationshipType(StrEnum):
    ROLLUP = "rollup"
    PARENT_CHILD = "parent_child"
    OWNERSHIP = "ownership"
    MEMBERSHIP = "membership"
    IDENTITY = "identity"       # reserved for bridge/realization use; NOT a valid global self-edge


class Cardinality(StrEnum):
    ONE_TO_ONE = "one_to_one"
    ONE_TO_MANY = "one_to_many"
    MANY_TO_ONE = "many_to_one"
    MANY_TO_MANY = "many_to_many"


class TraversalDirection(StrEnum):
    FORWARD = "forward"
    REVERSE = "reverse"          # reserved for 3B; a non-FORWARD ACTIVE edge is rejected in 3A
    BOTH = "both"               # reserved for 3B


class AggregationStrategy(StrEnum):
    """WHO/whether an aggregation is declared for a roll-up. 3A carries the strategy only — the actual
    function (avg/sum/window) is a Phase-3B recipe concern, so the relationship never over-declares a
    measure-specific aggregation."""

    NOT_APPLICABLE = "not_applicable"    # a non-aggregating relationship (e.g. 1:1)
    RECIPE_DECLARED = "recipe_declared"  # aggregation required; the recipe declares the function (3B)


class RelationshipStatus(StrEnum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"


class RelationshipProposalStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class GraphEdgeAuthority(StrEnum):
    GLOBAL_ENTITY_MODEL = "global_entity_model"
    CATALOG_DECLARED = "catalog_declared"
    ENTITY_BRIDGE = "entity_bridge"


@dataclass(frozen=True, slots=True)
class EntityRelationshipDefinitionV1:
    """A GLOBAL semantic entity relationship — the only edge class the 3A graph traverses. Answers 'is
    grain ``from_entity`` semantically derivable into ``to_entity``?' and states WHETHER a roll-up needs
    aggregation (``aggregation_required``) and who declares it (``aggregation_strategy``). It carries NO
    physical column mapping and NO measure-specific aggregation list."""

    relationship_id: str
    from_entity: str
    to_entity: str
    relationship_type: RelationshipType
    cardinality: Cardinality
    traversal_direction: TraversalDirection
    aggregation_required: bool
    aggregation_strategy: AggregationStrategy
    status: RelationshipStatus
    version: str


@dataclass(frozen=True, slots=True)
class CatalogEntityRelationshipV1:
    """CONTRACT ONLY in 3A. How one catalog physically realizes a global relationship: the two object
    refs AND the entity each endpoint resolved to (persisted so Phase 3B can diagnose a realization-vs-
    global entity conflict — e.g. 'this upload resolved the to-endpoint as account, but the global
    relationship expects customer' — without re-querying endpoint metadata). Cross-checked against the
    global model in Phase 3B; 3A validates it structurally only."""

    realization_id: str
    relationship_id: str
    catalog_source: str
    from_object_ref: str
    to_object_ref: str
    resolved_from_entity: str
    resolved_to_entity: str
    declared_cardinality: Cardinality
    adapter_id: str
    authority: GraphEdgeAuthority = GraphEdgeAuthority.CATALOG_DECLARED
    status: RelationshipStatus = RelationshipStatus.ACTIVE


@dataclass(frozen=True, slots=True)
class EntityBridgeV1:
    """CONTRACT ONLY in 3A. A sanctioned cross-catalog identity link: two catalog-local representations
    of the SAME entity. Bridge IDENTITY is UNORDERED — ``(A:x ↔ B:y)`` and ``(B:y ↔ A:x)`` denote the
    same bridge; Phase 3B canonicalizes endpoints for duplicate detection. Governed activation is Phase
    3B (today bridges are computed permissively)."""

    bridge_id: str
    entity_id: str
    left_catalog_source: str
    left_object_ref: str
    right_catalog_source: str
    right_object_ref: str
    authority: GraphEdgeAuthority = GraphEdgeAuthority.ENTITY_BRIDGE
    status: RelationshipStatus = RelationshipStatus.ACTIVE


@dataclass(frozen=True, slots=True)
class EntityRelationshipProposalV1:
    """CONTRACT ONLY in 3A, and NEVER traversed. A metadata-derived candidate — evidence, not truth.
    ``ACCEPTED`` status does NOT make it traversable; promotion is a Phase-3B governance step."""

    proposal_id: str
    proposed_from_entity: str
    proposed_to_entity: str
    proposed_cardinality: Cardinality
    evidence_refs: tuple[str, ...]
    source_catalog: str
    inferred_by: str
    status: RelationshipProposalStatus


@dataclass(frozen=True, slots=True)
class EntityRelationshipRefV1:
    """One hop in a resolved semantic path: the relationship (with the version traversed) + its roll-up
    aggregation semantics."""

    relationship_id: str
    relationship_version: str
    from_entity: str
    to_entity: str
    cardinality: Cardinality
    aggregation_required: bool
    aggregation_strategy: AggregationStrategy


@dataclass(frozen=True, slots=True)
class EntitySemanticPathV1:
    hops: tuple[EntityRelationshipRefV1, ...]


@dataclass(frozen=True, slots=True)
class EntityCompatibilityResultV1:
    """``paths`` is ``()`` for EXACT/UNKNOWN, one for DERIVABLE, ≥2 for AMBIGUOUS. ``paths_truncated`` is
    True when path enumeration hit its bound (≥2 paths — enough to classify). ``graph_version`` stamps the
    registry composition the result came from."""

    status: EntityCompatibility
    source_entity: str
    target_entity: str
    paths: tuple[EntitySemanticPathV1, ...]
    reason_codes: tuple[str, ...]
    graph_version: str
    paths_truncated: bool = False


def _nonempty(**fields: str) -> None:
    for name, value in fields.items():
        if not value or not value.strip():
            raise ValueError(f"empty {name}")


def validate_relationship_definition(
    defn: EntityRelationshipDefinitionV1, *, known: frozenset[str]) -> None:
    """Structural guard over ONE global semantic definition. Raises ``ValueError`` on: an endpoint outside
    the closed vocabulary; ANY self-edge (the EXACT short-circuit handles identity — a self roll-up is
    redundant); a non-``FORWARD`` traversal direction (3A supports forward semantic edges only); an
    aggregation_required/strategy mismatch; a non-semver ``version``. Duplicate ids/edges and cycles are
    graph-build concerns."""
    _nonempty(relationship_id=defn.relationship_id, from_entity=defn.from_entity,
              to_entity=defn.to_entity, version=defn.version)
    if defn.from_entity not in known:
        raise ValueError(f"unknown entity: {defn.from_entity!r}")
    if defn.to_entity not in known:
        raise ValueError(f"unknown entity: {defn.to_entity!r}")
    if defn.from_entity == defn.to_entity:
        raise ValueError(f"self-relationship not allowed: {defn.from_entity!r}")
    if defn.traversal_direction is not TraversalDirection.FORWARD:
        raise ValueError("only FORWARD active semantic edges are supported in 3A")
    required = defn.aggregation_required
    applicable = defn.aggregation_strategy is not AggregationStrategy.NOT_APPLICABLE
    if required != applicable:
        raise ValueError("aggregation_required must match a non-NOT_APPLICABLE aggregation_strategy")
    if not is_semver(defn.version):
        raise ValueError(f"invalid version: {defn.version!r} (expected N.N.N)")


def validate_catalog_relationship(real: CatalogEntityRelationshipV1, *, known: frozenset[str]) -> None:
    """Structural guard: non-empty fields, distinct object refs, both resolved endpoints in the closed
    vocabulary, fixed authority. It does NOT cross-check the resolved entities against the global
    relationship's endpoints — that (and the realization↔global conflict decision) is Phase 3B."""
    _nonempty(realization_id=real.realization_id, relationship_id=real.relationship_id,
              catalog_source=real.catalog_source, adapter_id=real.adapter_id,
              from_object_ref=real.from_object_ref, to_object_ref=real.to_object_ref,
              resolved_from_entity=real.resolved_from_entity,
              resolved_to_entity=real.resolved_to_entity)
    if real.from_object_ref == real.to_object_ref:
        raise ValueError("catalog realization endpoints are identical")
    if real.resolved_from_entity not in known:
        raise ValueError(f"unknown entity: {real.resolved_from_entity!r}")
    if real.resolved_to_entity not in known:
        raise ValueError(f"unknown entity: {real.resolved_to_entity!r}")
    if real.authority is not GraphEdgeAuthority.CATALOG_DECLARED:
        raise ValueError("catalog realization authority must be CATALOG_DECLARED")


def validate_entity_bridge(bridge: EntityBridgeV1, *, known: frozenset[str]) -> None:
    _nonempty(bridge_id=bridge.bridge_id, entity_id=bridge.entity_id,
              left_catalog_source=bridge.left_catalog_source,
              right_catalog_source=bridge.right_catalog_source,
              left_object_ref=bridge.left_object_ref, right_object_ref=bridge.right_object_ref)
    if bridge.entity_id not in known:
        raise ValueError(f"unknown entity: {bridge.entity_id!r}")
    if bridge.left_catalog_source == bridge.right_catalog_source:
        raise ValueError("a bridge must span two distinct catalog sources")
    if bridge.authority is not GraphEdgeAuthority.ENTITY_BRIDGE:
        raise ValueError("bridge authority must be ENTITY_BRIDGE")


def validate_relationship_proposal(
    prop: EntityRelationshipProposalV1, *, known: frozenset[str]) -> None:
    _nonempty(proposal_id=prop.proposal_id, source_catalog=prop.source_catalog,
              inferred_by=prop.inferred_by, proposed_from_entity=prop.proposed_from_entity,
              proposed_to_entity=prop.proposed_to_entity)
    if prop.proposed_from_entity not in known:
        raise ValueError(f"unknown entity: {prop.proposed_from_entity!r}")
    if prop.proposed_to_entity not in known:
        raise ValueError(f"unknown entity: {prop.proposed_to_entity!r}")
    if prop.proposed_from_entity == prop.proposed_to_entity:
        raise ValueError("self-relationship proposal is not allowed; use an entity-bridge proposal")
    if not prop.evidence_refs:
        raise ValueError("a proposal needs at least one evidence ref")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/featuregen/overlay/upload/taxonomy/test_entity_relationships.py -q`
Expected: PASS (11 tests).

- [ ] **Step 5: Lint + type-check + commit**

```bash
uv run ruff check src/featuregen/overlay/upload/taxonomy/entity_relationships.py tests/featuregen/overlay/upload/taxonomy/test_entity_relationships.py
uv run mypy src/featuregen/overlay/upload/taxonomy/entity_relationships.py
git add src/featuregen/overlay/upload/taxonomy/entity_relationships.py tests/featuregen/overlay/upload/taxonomy/test_entity_relationships.py
git commit -m "feat(3a): entity-relationship contracts + structural validators (task 3A.1)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2 (3A.2): Curated semantic registry

**Files:**
- Create: `src/featuregen/overlay/upload/taxonomy/entity_registry.py`
- Test: append to `tests/featuregen/overlay/upload/taxonomy/test_entity_relationships.py`

**Interfaces:**
- Produces: `GRAPH_VERSION: str` (`"1.0.0"`), `ENTITY_RELATIONSHIPS_V1: tuple[EntityRelationshipDefinitionV1, ...]` (5 defs).

- [ ] **Step 1: Write the failing test**

Append to `tests/featuregen/overlay/upload/taxonomy/test_entity_relationships.py`:

```python
from collections import Counter

from featuregen.overlay.upload.taxonomy.entity_registry import (
    ENTITY_RELATIONSHIPS_V1,
    GRAPH_VERSION,
)


def test_registry_is_exactly_the_five_seed_rollups_and_valid():
    edges = {(d.from_entity, d.to_entity) for d in ENTITY_RELATIONSHIPS_V1}
    assert edges == {
        ("account", "customer"), ("card_account", "customer"), ("transaction", "account"),
        ("facility", "obligor"), ("policy", "customer")}
    for d in ENTITY_RELATIONSHIPS_V1:
        validate_relationship_definition(d, known=KNOWN)
        assert d.aggregation_required is True
        assert d.aggregation_strategy is AggregationStrategy.RECIPE_DECLARED  # never a blanket agg list
    assert GRAPH_VERSION == "1.0.0"


def test_registry_out_degree_at_most_one():
    # Out-degree <=1 prevents branching; acyclicity is enforced by the builder (Task 3).
    out_degree = Counter(
        d.from_entity for d in ENTITY_RELATIONSHIPS_V1 if d.status is RelationshipStatus.ACTIVE)
    assert all(n <= 1 for n in out_degree.values())


def test_registry_relationship_ids_unique():
    ids = [d.relationship_id for d in ENTITY_RELATIONSHIPS_V1]
    assert len(ids) == len(set(ids))
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/taxonomy/test_entity_relationships.py -q`
Expected: FAIL — `ModuleNotFoundError: ... entity_registry`.

- [ ] **Step 3: Write the implementation**

Create `src/featuregen/overlay/upload/taxonomy/entity_registry.py`:

```python
"""Phase-3A — the curated, versioned GLOBAL semantic entity-relationship registry.

Seeded with EXACTLY the five roll-ups Phase-2B's ``_ENTITY_ROLLUP`` expressed — acyclic, each source
out-degree <=1, so the graph is regression-equivalent and never emits ``AMBIGUOUS``. Each roll-up
requires aggregation whose function the RECIPE declares in Phase 3B (``RECIPE_DECLARED``) — the
relationship never over-declares a measure-specific aggregation list. In-code + version-controlled; no
DB in 3A. New relationships that could create a second path for an existing pair are a Phase-3D concern.

These five definitions encode the semantic ASSUMPTIONS already embedded in ``_ENTITY_ROLLUP``; they are
compatibility-preserving DEFAULTS, not proof that every catalog physically realizes the relationship
with the declared ``MANY_TO_ONE`` cardinality. Real-world exceptions (joint accounts, multi-policyholder
policies, facilities with several obligors) are catalog-realization concerns that Phase 3B validates and
FAILS CLOSED on — 3A does not assert these cardinalities as universal banking truths.

NOTE (deferred, Phase-3B/growth): a content fingerprint (sha256 over canonicalized definitions) paired
with ``GRAPH_VERSION`` would catch a definition change made without bumping the version. Omitted for a
five-entry seed; add it when the registry grows."""
from __future__ import annotations

from featuregen.overlay.upload.taxonomy.entity_relationships import (
    AggregationStrategy,
    Cardinality,
    EntityRelationshipDefinitionV1,
    RelationshipStatus,
    RelationshipType,
    TraversalDirection,
)

GRAPH_VERSION = "1.0.0"


def _rollup(relationship_id: str, from_entity: str, to_entity: str) -> EntityRelationshipDefinitionV1:
    return EntityRelationshipDefinitionV1(
        relationship_id=relationship_id, from_entity=from_entity, to_entity=to_entity,
        relationship_type=RelationshipType.ROLLUP, cardinality=Cardinality.MANY_TO_ONE,
        traversal_direction=TraversalDirection.FORWARD, aggregation_required=True,
        aggregation_strategy=AggregationStrategy.RECIPE_DECLARED, status=RelationshipStatus.ACTIVE,
        version="1.0.0")


ENTITY_RELATIONSHIPS_V1: tuple[EntityRelationshipDefinitionV1, ...] = (
    _rollup("account_to_customer", "account", "customer"),
    _rollup("card_account_to_customer", "card_account", "customer"),
    _rollup("transaction_to_account", "transaction", "account"),
    _rollup("facility_to_obligor", "facility", "obligor"),
    _rollup("policy_to_customer", "policy", "customer"),
)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/featuregen/overlay/upload/taxonomy/test_entity_relationships.py -q`
Expected: PASS (14 tests). If an endpoint is not in `KNOWN`, stop and report (do not fabricate a concept) — every seed endpoint is expected to already be a known entity.

- [ ] **Step 5: Lint + type-check + commit**

```bash
uv run ruff check src/featuregen/overlay/upload/taxonomy/entity_registry.py
uv run mypy src/featuregen/overlay/upload/taxonomy/entity_registry.py
git add src/featuregen/overlay/upload/taxonomy/entity_registry.py tests/featuregen/overlay/upload/taxonomy/test_entity_relationships.py
git commit -m "feat(3a): curated global entity-relationship registry — 5 seed rollups (task 3A.2)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3 (3A.3): Semantic graph builder (cycle/duplicate/direction-rejecting)

**Files:**
- Create: `src/featuregen/overlay/upload/taxonomy/entity_graph.py`
- Test: `tests/featuregen/overlay/upload/taxonomy/test_entity_graph.py`

**Interfaces:**
- Produces: `EntityGraph` (frozen; `.version: str`, `.known_entities: frozenset[str]`, `.outgoing(entity) -> tuple[EntityRelationshipDefinitionV1, ...]`), `build_entity_graph(defs, *, version, known) -> EntityGraph`.

- [ ] **Step 1: Write the failing tests**

Create `tests/featuregen/overlay/upload/taxonomy/test_entity_graph.py`:

```python
"""Phase-3A Tasks 3A.3/3A.4 — the immutable, cycle-rejecting graph builder + bounded traversal."""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.taxonomy.dimensions import known_entities
from featuregen.overlay.upload.taxonomy.entity_relationships import (
    AggregationStrategy,
    Cardinality,
    EntityRelationshipDefinitionV1,
    RelationshipStatus,
    RelationshipType,
    TraversalDirection,
)
from featuregen.overlay.upload.taxonomy.entity_graph import build_entity_graph

KNOWN = known_entities()


def _e(rid, a, b, *, status=RelationshipStatus.ACTIVE,
       direction=TraversalDirection.FORWARD) -> EntityRelationshipDefinitionV1:
    return EntityRelationshipDefinitionV1(
        relationship_id=rid, from_entity=a, to_entity=b, relationship_type=RelationshipType.ROLLUP,
        cardinality=Cardinality.MANY_TO_ONE, traversal_direction=direction, aggregation_required=True,
        aggregation_strategy=AggregationStrategy.RECIPE_DECLARED, status=status, version="1.0.0")


def test_build_indexes_active_outgoing_edges_and_carries_vocab():
    g = build_entity_graph(
        (_e("t_a", "transaction", "account"), _e("a_c", "account", "customer")),
        version="1.0.0", known=KNOWN)
    assert g.version == "1.0.0"
    assert g.known_entities == KNOWN
    assert [d.relationship_id for d in g.outgoing("transaction")] == ["t_a"]
    assert g.outgoing("customer") == ()


def test_inactive_edges_excluded():
    g = build_entity_graph(
        (_e("a_c", "account", "customer", status=RelationshipStatus.DEPRECATED),),
        version="1.0.0", known=KNOWN)
    assert g.outgoing("account") == ()


def test_outgoing_sorted_by_relationship_id():
    g = build_entity_graph(
        (_e("z2", "transaction", "account"), _e("a2", "transaction", "obligor")),
        version="1.0.0", known=KNOWN)
    assert [d.relationship_id for d in g.outgoing("transaction")] == ["a2", "z2"]


def test_converging_dag_is_not_treated_as_cycle():
    # transaction -> account -> customer AND transaction -> card_account -> customer share the descendant
    # `customer`; a converging DAG is acyclic and MUST build (the AMBIGUOUS test depends on this).
    g = build_entity_graph(
        (_e("t_a", "transaction", "account"), _e("a_c", "account", "customer"),
         _e("t_ca", "transaction", "card_account"), _e("ca_c", "card_account", "customer")),
        version="1.0.0", known=KNOWN)
    assert [d.relationship_id for d in g.outgoing("transaction")] == ["t_a", "t_ca"]


def test_duplicate_relationship_id_rejected():
    with pytest.raises(ValueError, match="duplicate.*id"):
        build_entity_graph(
            (_e("dup", "account", "customer"), _e("dup", "transaction", "account")),
            version="1.0.0", known=KNOWN)


def test_duplicate_semantic_edge_rejected():
    # same (from, to, type, direction) with different ids -> a duplicate declaration, NOT ambiguity
    with pytest.raises(ValueError, match="duplicate semantic edge"):
        build_entity_graph(
            (_e("a_c_v1", "account", "customer"), _e("a_c_dupe", "account", "customer")),
            version="1.0.0", known=KNOWN)


def test_semantic_cycle_rejected():
    with pytest.raises(ValueError, match="cycle"):
        build_entity_graph(
            (_e("a_c", "account", "customer"), _e("c_a", "customer", "account")),
            version="1.0.0", known=KNOWN)


def test_non_forward_active_edge_rejected():
    with pytest.raises(ValueError, match="forward"):
        build_entity_graph(
            (_e("bad", "account", "customer", direction=TraversalDirection.BOTH),),
            version="1.0.0", known=KNOWN)


def test_builder_validates_endpoints():
    with pytest.raises(ValueError, match="unknown entity"):
        build_entity_graph((_e("bad", "account", "not_an_entity"),), version="1.0.0", known=KNOWN)


def test_invalid_graph_version_rejected():
    with pytest.raises(ValueError, match="version"):
        build_entity_graph((_e("a_c", "account", "customer"),), version="v1", known=KNOWN)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/taxonomy/test_entity_graph.py -q`
Expected: FAIL — `ModuleNotFoundError: ... entity_graph`.

- [ ] **Step 3: Write the implementation**

Create `src/featuregen/overlay/upload/taxonomy/entity_graph.py` (builder only; traversal in Task 4):

```python
"""Phase-3A — the immutable global semantic entity graph + bounded compatibility traversal.

Built ONCE from the curated registry. Only active FORWARD :class:`EntityRelationshipDefinitionV1` edges
are indexed. The builder rejects invalid definitions, duplicate ids, duplicate semantic edges, non-
FORWARD active edges, and directed CYCLES (a semantic cycle is a contradictory grain model, not merely a
traversal hazard). Outgoing edges are stored sorted by ``relationship_id`` so traversal is deterministic.
The closed entity vocabulary is carried on the graph so the resolver can fail out-of-vocab entities to
UNKNOWN (never EXACT)."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from featuregen.overlay.upload.taxonomy.entity_relationships import (
    EntityRelationshipDefinitionV1,
    RelationshipStatus,
    TraversalDirection,
    is_semver,
    validate_relationship_definition,
)


@dataclass(frozen=True, slots=True)
class EntityGraph:
    """An immutable adjacency of active semantic relationships + the closed entity vocabulary it was built
    over. ``outgoing(entity)`` returns the entity's active outgoing edges, sorted by ``relationship_id``.
    PRODUCTION GRAPHS MUST come from :func:`build_entity_graph` — direct construction bypasses every
    invariant (cycle/duplicate/direction/vocabulary) and is test-only."""

    version: str
    known_entities: frozenset[str]
    _adjacency: Mapping[str, tuple[EntityRelationshipDefinitionV1, ...]]

    def outgoing(self, entity: str) -> tuple[EntityRelationshipDefinitionV1, ...]:
        return self._adjacency.get(entity, ())


def _reject_cycles(adjacency: Mapping[str, tuple[EntityRelationshipDefinitionV1, ...]]) -> None:
    """Raise ``ValueError`` on any directed cycle among active edges. Recursive three-colour DFS: a node
    on the ACTIVE recursion path (``visiting``) reached again is a back edge → a cycle; a fully-explored
    node (``visited``) reached again is a shared descendant of a converging DAG → NOT a cycle. The curated
    graph is tiny, so recursion depth is trivial."""
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise ValueError(f"semantic cycle through {node!r}")
        if node in visited:
            return
        visiting.add(node)
        for edge in adjacency.get(node, ()):
            visit(edge.to_entity)
        visiting.discard(node)
        visited.add(node)

    for node in sorted(adjacency):
        visit(node)


def build_entity_graph(
    defs: tuple[EntityRelationshipDefinitionV1, ...], *, version: str, known: frozenset[str],
) -> EntityGraph:
    """Validate + index only ACTIVE definitions (deprecated ones are archived, neither validated nor
    traversed — a deliberate active-only model). Reject duplicate active ids, duplicate active semantic
    edges ``(from, to, type, direction)``, non-FORWARD active edges, and directed cycles; index by
    ``from_entity`` (sorted). Fails fast at import for the seed."""
    if not is_semver(version):
        raise ValueError(f"invalid graph version: {version!r} (expected N.N.N)")
    seen_ids: set[str] = set()
    seen_semantic: set[tuple[str, str, str, str]] = set()
    by_source: dict[str, list[EntityRelationshipDefinitionV1]] = {}
    for d in defs:
        if d.status is not RelationshipStatus.ACTIVE:
            continue                                    # archived; not validated or indexed in 3A
        validate_relationship_definition(d, known=known)
        if d.traversal_direction is not TraversalDirection.FORWARD:
            raise ValueError(f"only FORWARD active edges supported in 3A: {d.relationship_id!r}")
        if d.relationship_id in seen_ids:
            raise ValueError(f"duplicate active relationship id: {d.relationship_id!r}")
        seen_ids.add(d.relationship_id)
        key = (d.from_entity, d.to_entity, d.relationship_type.value, d.traversal_direction.value)
        if key in seen_semantic:
            raise ValueError(f"duplicate semantic edge: {d.from_entity!r}->{d.to_entity!r}")
        seen_semantic.add(key)
        by_source.setdefault(d.from_entity, []).append(d)
    adjacency = {
        src: tuple(sorted(edges, key=lambda e: e.relationship_id))
        for src, edges in by_source.items()}
    _reject_cycles(adjacency)
    return EntityGraph(
        version=version, known_entities=known, _adjacency=MappingProxyType(adjacency))
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/featuregen/overlay/upload/taxonomy/test_entity_graph.py -q`
Expected: PASS (9 tests).

- [ ] **Step 5: Lint + type-check + commit**

```bash
uv run ruff check src/featuregen/overlay/upload/taxonomy/entity_graph.py tests/featuregen/overlay/upload/taxonomy/test_entity_graph.py
uv run mypy src/featuregen/overlay/upload/taxonomy/entity_graph.py
git add src/featuregen/overlay/upload/taxonomy/entity_graph.py tests/featuregen/overlay/upload/taxonomy/test_entity_graph.py
git commit -m "feat(3a): cycle/duplicate/direction-rejecting semantic graph builder (task 3A.3)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4 (3A.4): Bounded compatibility traversal

**Files:**
- Modify: `src/featuregen/overlay/upload/taxonomy/entity_graph.py` (add traversal + `ENTITY_GRAPH`)
- Test: append to `tests/featuregen/overlay/upload/taxonomy/test_entity_graph.py`

**Interfaces:**
- Produces: `resolve_entity_compatibility(source, target, graph) -> EntityCompatibilityResultV1` (path bound is the internal `_MAX_COMPATIBILITY_PATHS`, not a parameter); module singleton `ENTITY_GRAPH: EntityGraph`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/featuregen/overlay/upload/taxonomy/test_entity_graph.py`:

```python
from featuregen.overlay.upload.taxonomy.entity_graph import (
    ENTITY_GRAPH,
    resolve_entity_compatibility,
)
from featuregen.overlay.upload.taxonomy.entity_relationships import EntityCompatibility


def test_unknown_entities_never_exact():
    # THE closed-vocab guard: two identical out-of-vocab strings must NOT be EXACT.
    r = resolve_entity_compatibility("not_an_entity", "not_an_entity", ENTITY_GRAPH)
    assert r.status is EntityCompatibility.UNKNOWN
    assert "unknown_source_entity" in r.reason_codes
    assert resolve_entity_compatibility("account", "not_an_entity", ENTITY_GRAPH).reason_codes \
        == ("unknown_target_entity",)
    assert resolve_entity_compatibility("", "", ENTITY_GRAPH).status is EntityCompatibility.UNKNOWN


def test_exact_when_known_source_equals_target():
    r = resolve_entity_compatibility("customer", "customer", ENTITY_GRAPH)
    assert r.status is EntityCompatibility.EXACT
    assert r.paths == () and r.graph_version == ENTITY_GRAPH.version


def test_derivable_direct_and_transitive():
    direct = resolve_entity_compatibility("account", "customer", ENTITY_GRAPH)
    assert direct.status is EntityCompatibility.DERIVABLE
    assert [h.relationship_id for h in direct.paths[0].hops] == ["account_to_customer"]
    assert direct.paths[0].hops[0].relationship_version == "1.0.0"
    assert direct.paths_truncated is False
    trans = resolve_entity_compatibility("transaction", "customer", ENTITY_GRAPH)
    assert [h.to_entity for h in trans.paths[0].hops] == ["account", "customer"]


def test_unknown_when_no_path():
    assert resolve_entity_compatibility("customer", "account", ENTITY_GRAPH).status \
        is EntityCompatibility.UNKNOWN


def test_seed_never_emits_ambiguous():
    ents = ("customer", "account", "card_account", "transaction", "facility", "obligor", "policy")
    for s in ents:
        for t in ents:
            assert resolve_entity_compatibility(s, t, ENTITY_GRAPH).status \
                is not EntityCompatibility.AMBIGUOUS


def test_ambiguous_two_paths_is_not_truncated():
    g = build_entity_graph(
        (_e("t_a", "transaction", "account"), _e("a_c", "account", "customer"),
         _e("t_ca", "transaction", "card_account"), _e("ca_c", "card_account", "customer")),
        version="1.0.0", known=KNOWN)
    r = resolve_entity_compatibility("transaction", "customer", g)
    assert r.status is EntityCompatibility.AMBIGUOUS
    assert len(r.paths) == 2 and r.paths_truncated is False   # exactly two — nothing truncated


def test_ambiguous_three_paths_is_truncated():
    g = build_entity_graph(
        (_e("t_a", "transaction", "account"), _e("a_c", "account", "customer"),
         _e("t_ca", "transaction", "card_account"), _e("ca_c", "card_account", "customer"),
         _e("t_p", "transaction", "policy"), _e("p_c", "policy", "customer")),
        version="1.0.0", known=KNOWN)
    r = resolve_entity_compatibility("transaction", "customer", g)
    assert r.status is EntityCompatibility.AMBIGUOUS
    assert len(r.paths) == 2 and r.paths_truncated is True    # visible capped; a third path exists


def _unsafe_graph_for_test(edges: dict) -> object:
    # Direct construction bypasses build_entity_graph invariants — TEST-ONLY, to exercise traversal
    # defense against a malformed graph the builder would have rejected. Production graphs come only
    # from build_entity_graph.
    from types import MappingProxyType

    from featuregen.overlay.upload.taxonomy.entity_graph import EntityGraph
    return EntityGraph(version="1.0.0", known_entities=KNOWN, _adjacency=MappingProxyType(edges))


def test_traversal_visited_guard_defends_a_malformed_cyclic_graph():
    cyclic = _unsafe_graph_for_test({
        "account": (_e("a_c", "account", "customer"),),
        "customer": (_e("c_a", "customer", "account"),)})
    assert resolve_entity_compatibility("account", "customer", cyclic).status \
        is EntityCompatibility.DERIVABLE
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/taxonomy/test_entity_graph.py -q`
Expected: FAIL — `ImportError: cannot import name 'ENTITY_GRAPH' / 'resolve_entity_compatibility'`.

- [ ] **Step 3: Write the implementation**

Append to `src/featuregen/overlay/upload/taxonomy/entity_graph.py`:

```python
from featuregen.overlay.upload.taxonomy.dimensions import known_entities
from featuregen.overlay.upload.taxonomy.entity_registry import (
    ENTITY_RELATIONSHIPS_V1,
    GRAPH_VERSION,
)
from featuregen.overlay.upload.taxonomy.entity_relationships import (
    EntityCompatibility,
    EntityCompatibilityResultV1,
    EntityRelationshipRefV1,
    EntitySemanticPathV1,
)


def _ref(d: EntityRelationshipDefinitionV1) -> EntityRelationshipRefV1:
    return EntityRelationshipRefV1(
        relationship_id=d.relationship_id, relationship_version=d.version, from_entity=d.from_entity,
        to_entity=d.to_entity, cardinality=d.cardinality, aggregation_required=d.aggregation_required,
        aggregation_strategy=d.aggregation_strategy)


# Enough paths to classify DERIVABLE vs AMBIGUOUS. Not a public knob — no consumer configures it.
_MAX_COMPATIBILITY_PATHS = 2


def _bounded_simple_paths(
    graph: EntityGraph, source: str, target: str, *, limit: int,
) -> list[tuple[EntityRelationshipDefinitionV1, ...]]:
    """Up to ``limit`` simple directed paths ``source → target`` over active forward edges. Cycle-safe via
    a visited set (defense in depth — the builder already rejects cycles); deterministic because outgoing
    edges are pre-sorted. Stops once ``limit`` paths are found."""
    results: list[tuple[EntityRelationshipDefinitionV1, ...]] = []

    def _walk(node: str, path: tuple[EntityRelationshipDefinitionV1, ...], visited: frozenset[str]) -> None:
        if len(results) >= limit:
            return
        if node == target:
            results.append(path)
            return
        for edge in graph.outgoing(node):
            if len(results) >= limit:
                return
            nxt = edge.to_entity
            if nxt in visited:
                continue
            _walk(nxt, (*path, edge), visited | {nxt})

    _walk(source, (), frozenset({source}))
    return results


def _path_identity(path: tuple[EntityRelationshipDefinitionV1, ...]) -> tuple[str, ...]:
    """Two paths are DISTINCT iff their ordered relationship-ids differ. (With simple-path enumeration +
    the builder's duplicate-edge rejection this is already 1:1 with the edge sequence; the dedup is an
    explicit, defensive statement of the equivalence contract.)"""
    return tuple(edge.relationship_id for edge in path)


def resolve_entity_compatibility(
    source: str, target: str, graph: EntityGraph) -> EntityCompatibilityResultV1:
    """Graph-backed grain compatibility. Out-of-vocabulary ``source``/``target`` → UNKNOWN (NEVER EXACT).
    ``source == target`` (both known) → EXACT; exactly one directed path → DERIVABLE; ≥2 distinct paths →
    AMBIGUOUS (surfaced, never a shortest-path pick); no path → UNKNOWN. Enumeration is bounded to
    ``_MAX_COMPATIBILITY_PATHS + 1`` so ``paths_truncated`` reports whether MORE than the visible paths
    exist (exactly two paths → not truncated; three-plus → truncated). Never raises."""
    def _unknown(*codes: str) -> EntityCompatibilityResultV1:
        return EntityCompatibilityResultV1(
            status=EntityCompatibility.UNKNOWN, source_entity=source, target_entity=target,
            paths=(), reason_codes=codes, graph_version=graph.version)

    if source not in graph.known_entities:
        return _unknown("unknown_source_entity")
    if target not in graph.known_entities:
        return _unknown("unknown_target_entity")
    if source == target:
        return EntityCompatibilityResultV1(
            status=EntityCompatibility.EXACT, source_entity=source, target_entity=target,
            paths=(), reason_codes=(), graph_version=graph.version)
    raw = _bounded_simple_paths(graph, source, target, limit=_MAX_COMPATIBILITY_PATHS + 1)
    seen: set[tuple[str, ...]] = set()
    distinct: list[tuple[EntityRelationshipDefinitionV1, ...]] = []
    for p in raw:
        ident = _path_identity(p)
        if ident in seen:
            continue
        seen.add(ident)
        distinct.append(p)
    truncated = len(distinct) > _MAX_COMPATIBILITY_PATHS
    visible = distinct[:_MAX_COMPATIBILITY_PATHS]
    paths = tuple(EntitySemanticPathV1(hops=tuple(_ref(e) for e in p)) for p in visible)
    if not paths:
        return _unknown("no_entity_path")
    if len(paths) == 1:
        return EntityCompatibilityResultV1(
            status=EntityCompatibility.DERIVABLE, source_entity=source, target_entity=target,
            paths=paths, reason_codes=(), graph_version=graph.version, paths_truncated=False)
    return EntityCompatibilityResultV1(
        status=EntityCompatibility.AMBIGUOUS, source_entity=source, target_entity=target,
        paths=paths, reason_codes=("multiple_entity_paths",), graph_version=graph.version,
        paths_truncated=truncated)


# Built ONCE at import from the curated registry — the single active graph in 3A. Fails fast if the
# registry is malformed (cycle / duplicate / unknown endpoint / bad version).
ENTITY_GRAPH: EntityGraph = build_entity_graph(
    ENTITY_RELATIONSHIPS_V1, version=GRAPH_VERSION, known=known_entities())
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/featuregen/overlay/upload/taxonomy/test_entity_graph.py -q`
Expected: PASS (all builder + traversal tests green).

Also verify the module singleton builds OUTSIDE pytest (catches an import-time registry defect):

Run: `uv run python -c "from featuregen.overlay.upload.taxonomy.entity_graph import ENTITY_GRAPH; print(ENTITY_GRAPH.version)"`
Expected: prints `1.0.0`.

- [ ] **Step 5: Lint + type-check + commit**

```bash
uv run ruff check src/featuregen/overlay/upload/taxonomy/entity_graph.py tests/featuregen/overlay/upload/taxonomy/test_entity_graph.py
uv run mypy src/featuregen/overlay/upload/taxonomy/entity_graph.py
git add src/featuregen/overlay/upload/taxonomy/entity_graph.py tests/featuregen/overlay/upload/taxonomy/test_entity_graph.py
git commit -m "feat(3a): bounded compatibility traversal, closed-vocab guard (task 3A.4)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5 (3A.5): Rewire `entity_compatibility` (regression-equivalent) + delete the old map

**Files:**
- Modify: `src/featuregen/overlay/upload/taxonomy/ranking_signals.py`
- Create: `tests/featuregen/overlay/upload/taxonomy/test_entity_compatibility_regression.py`

**Interfaces:**
- Consumes: `resolve_entity_compatibility`, `ENTITY_GRAPH`, `EntityCompatibility`, `_grain_entity` (unchanged).
- Produces: `entity_compatibility(t: Template, target_entity: str | None = None) -> EntityCompatibility` (unchanged external contract); `EntityCompatibility` re-exported from `ranking_signals`.

- [ ] **Step 1: Write the characterization test** (a regression oracle over the FULL vocabulary; it exercises Task-4 code, so it passes immediately — its job is to lock equivalence, not to fail red first)

Create `tests/featuregen/overlay/upload/taxonomy/test_entity_compatibility_regression.py`:

```python
"""Phase-3A Task 3A.5 — THE load-bearing characterization test: the graph resolver reproduces the deleted
_ENTITY_ROLLUP map EXACTLY, for EVERY pair in the full known_entities() vocabulary (not just the seven in
the roll-ups). EXPECTED is computed from the OLD map's semantics, frozen here since the map is deleted."""
from __future__ import annotations

from featuregen.overlay.upload.taxonomy.dimensions import known_entities
from featuregen.overlay.upload.taxonomy.entity_graph import ENTITY_GRAPH, resolve_entity_compatibility
from featuregen.overlay.upload.taxonomy.entity_relationships import EntityCompatibility

_OLD_ROLLUP = {
    "account": "customer", "card_account": "customer", "transaction": "account",
    "facility": "obligor", "policy": "customer"}
_ENTITIES = tuple(sorted(known_entities()))


def _old_status(source: str, target: str) -> EntityCompatibility:
    if source == target:
        return EntityCompatibility.EXACT
    seen: set[str] = set()
    cur: str | None = source
    while cur is not None and cur not in seen:
        seen.add(cur)
        cur = _OLD_ROLLUP.get(cur)
        if cur == target:
            return EntityCompatibility.DERIVABLE
    return EntityCompatibility.UNKNOWN


def test_graph_reproduces_old_rollup_for_every_known_entity_pair():
    for source in _ENTITIES:
        for target in _ENTITIES:
            assert resolve_entity_compatibility(source, target, ENTITY_GRAPH).status \
                == _old_status(source, target), f"{source}->{target}"


def test_no_pair_produces_ambiguous():
    for source in _ENTITIES:
        for target in _ENTITIES:
            assert resolve_entity_compatibility(source, target, ENTITY_GRAPH).status \
                is not EntityCompatibility.AMBIGUOUS
```

- [ ] **Step 2: Run to verify it passes (characterization)**

Run: `uv run pytest tests/featuregen/overlay/upload/taxonomy/test_entity_compatibility_regression.py -q`
Expected: PASS. (It guards the resolver; the *rewire* is validated by the existing `test_ranking_signals.py` staying green after Step 3.)

- [ ] **Step 3: Rewire `ranking_signals.py`**

(a) Add to the import block (after `from ... .templates import GroundedFeature, Template`):

```python
from featuregen.overlay.upload.taxonomy.entity_graph import (
    ENTITY_GRAPH,
    resolve_entity_compatibility,
)
from featuregen.overlay.upload.taxonomy.entity_relationships import EntityCompatibility
```

(b) DELETE the local `class EntityCompatibility(StrEnum): ...` block (now imported + re-exported).

(c) DELETE `_ENTITY_ROLLUP`, `_rolls_up_to`, and their comments. KEEP `_grain_entity` unchanged.

(d) Replace the body of `entity_compatibility`:

```python
def entity_compatibility(t: Template, target_entity: str | None = None) -> EntityCompatibility:
    """The SOFT grain fit of the recipe to a confirmed ``target_entity`` — a grain/groundability signal
    (a low rank tie-break + an ``entity_grain_mismatch`` warning on ``DERIVABLE``), NEVER an
    applicability reject. Phase-3A: the grain relationship is resolved by the governed entity graph
    (:func:`resolve_entity_compatibility` over :data:`ENTITY_GRAPH`) instead of a hardcoded map — the
    seed is regression-equivalent, so outputs match the old map exactly. ``target_entity is None`` or a recipe
    with no derivable grain → ``UNKNOWN``."""
    if target_entity is None:
        return EntityCompatibility.UNKNOWN
    source = _grain_entity(t)
    if source is None:
        return EntityCompatibility.UNKNOWN
    return resolve_entity_compatibility(source, target_entity, ENTITY_GRAPH).status
```

- [ ] **Step 4: Prove regression-equivalent — run the full existing ranking/route suites + a no-leak guard**

Run: `uv run pytest tests/featuregen/overlay/upload/taxonomy/test_ranking_signals.py tests/featuregen/overlay/upload/taxonomy/test_entity_compatibility_regression.py tests/featuregen/api/test_contract_ranked.py -q`
Expected: PASS — every existing `test_ranking_signals.py` entity-compatibility test green UNCHANGED (adapter output unchanged), the regression green, route-ranking green. If any existing test fails, the rewire changed behaviour — stop and diagnose (do NOT edit the existing tests to match).

Then add a no-leak guard to `test_entity_compatibility_regression.py` (asserts 3A added no graph provenance to the route-facing signal bundle — the real leak surface, and non-vacuous):

```python
from dataclasses import fields

from featuregen.overlay.upload.taxonomy.ranking import RankSignals   # existing (Task A2)
from featuregen.overlay.upload.taxonomy.ranking_signals import (
    EntityCompatibility as _ReExported,
    entity_compatibility,
)


def test_rank_signals_gained_no_graph_metadata():
    # The route serializes RankSignals-derived fields; 3A must not leak graph provenance into them.
    names = {f.name for f in fields(RankSignals)}
    assert "graph_version" not in names
    assert "paths_truncated" not in names
    assert "paths" not in names


def test_adapter_still_returns_the_bare_enum_reexported_from_ranking_signals():
    # entity_compatibility returns the EntityCompatibility MEMBER (not EntityCompatibilityResultV1),
    # and EntityCompatibility is still importable from ranking_signals (re-export preserved).
    from featuregen.overlay.upload.templates import Need, Template
    t = Template(id="t", family="f", intent="i", needs=(Need("entity", "customer_id"),),
                 params={}, aggregation="avg", additivity="additive")
    assert entity_compatibility(t, target_entity="customer") is _ReExported.EXACT
```

Run: `uv run pytest tests/featuregen/overlay/upload/taxonomy/test_entity_compatibility_regression.py -q`
Expected: PASS. (If `Template`'s required fields differ, mirror the factory in `test_ranking_signals.py` — the assertion that matters is the bare-enum return + no graph field in `RankSignals`.)

Then add a **route-level** neutrality scan to `tests/featuregen/api/test_contract_ranked.py` — a recursive scan of a REAL scoped considered-set response proves no graph metadata reaches the wire (stronger than the dataclass-field check). Reuse the file's existing scoped-call setup (the same catalog upload + `POST /contract/considered-set` with a `confirmed_scope` carrying a `target_entity` that yields a `DERIVABLE` grain — e.g. an account-grain recipe under `target_entity="customer"`, exactly the B3 `entity_grain_mismatch` case already exercised in this file):

```python
def _all_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _all_keys(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _all_keys(item)


def test_scoped_ranking_response_leaks_no_entity_graph_metadata(client):
    # Mirror an existing scoped-ranking test in this file that produces a DERIVABLE target_entity, then
    # assert the serialized response carries NONE of the 3A graph fields anywhere in its tree.
    response = _scoped_considered_set_with_target_entity(client, target_entity="customer")  # existing helper/pattern
    keys = set(_all_keys(response))
    assert not (keys & {"graph_version", "paths", "paths_truncated", "relationship_version"})
```

Run: `uv run pytest tests/featuregen/api/test_contract_ranked.py -q`
Expected: PASS. (Wire `_scoped_considered_set_with_target_entity` to the file's existing scoped-call helper — the assertion is what's load-bearing: no graph field appears in the response JSON.)

- [ ] **Step 5: Lint + type-check + commit**

```bash
uv run ruff check src/featuregen/overlay/upload/taxonomy/ranking_signals.py tests/featuregen/overlay/upload/taxonomy/test_entity_compatibility_regression.py
uv run mypy src/featuregen/overlay/upload/taxonomy/ranking_signals.py
git add src/featuregen/overlay/upload/taxonomy/ranking_signals.py tests/featuregen/overlay/upload/taxonomy/test_entity_compatibility_regression.py tests/featuregen/api/test_contract_ranked.py
git commit -m "feat(3a): rewire entity_compatibility onto the graph, delete _ENTITY_ROLLUP (task 3A.5)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6 (3A.6): Feasibility spike over the REAL production types (tests only)

**Files:**
- Create: `tests/featuregen/overlay/upload/taxonomy/test_entity_contract_feasibility.py`

**Interfaces:**
- Consumes: the Task-1 contracts + validators; the REAL `featuregen.overlay.upload.graph.JoinEdge` and `featuregen.overlay.upload.entity.EntityBridge` (both plain frozen dataclasses, constructible with no DB).
- Produces: nothing in production — an in-test transform proving the contracts carry the real upload shapes. NO production graph change (3B promotes the transforms).

- [ ] **Step 1: Write the feasibility tests (real types, not stand-ins)**

Create `tests/featuregen/overlay/upload/taxonomy/test_entity_contract_feasibility.py`:

```python
"""Phase-3A Task 3A.6 — feasibility spike (TESTS ONLY). Prove the not-yet-active contracts can represent
REAL production upload shapes (graph.JoinEdge / entity.EntityBridge) BEFORE 3B commits to them. The
transforms live here, not in production: 3A never populates or traverses catalog realizations or bridges.
Using the real types (not stand-ins) is the point — a field rename in production must fail this test."""
from __future__ import annotations

from featuregen.overlay.upload.entity import EntityBridge          # real production type
from featuregen.overlay.upload.graph import JoinEdge               # real production type
from featuregen.overlay.upload.taxonomy.entity_relationships import (
    Cardinality,
    CatalogEntityRelationshipV1,
    EntityBridgeV1,
    GraphEdgeAuthority,
    RelationshipStatus,
    validate_catalog_relationship,
    validate_entity_bridge,
)
from featuregen.overlay.upload.taxonomy.dimensions import known_entities

_CARDINALITY = {"N:1": Cardinality.MANY_TO_ONE, "1:1": Cardinality.ONE_TO_ONE,
                "1:N": Cardinality.ONE_TO_MANY, "N:N": Cardinality.MANY_TO_MANY}


def catalog_relationship_from_join_edge(
    edge: JoinEdge, *, catalog_source: str, relationship_id: str, adapter_id: str,
    from_entity: str, to_entity: str,
) -> CatalogEntityRelationshipV1:
    """A real WITHIN-catalog JoinEdge → a CatalogEntityRelationshipV1 (physical realization). Endpoint→
    entity resolution (``from_entity``/``to_entity``) + binding to a global relationship_id is 3B's job;
    here they are SUPPLIED to prove the CONTRACT carries the join's physical facts + resolved entities
    from the REAL JoinEdge type."""
    return CatalogEntityRelationshipV1(
        realization_id=f"{catalog_source}:{edge.from_ref}->{edge.to_ref}",
        relationship_id=relationship_id, catalog_source=catalog_source,
        from_object_ref=edge.from_ref, to_object_ref=edge.to_ref,
        resolved_from_entity=from_entity, resolved_to_entity=to_entity,
        declared_cardinality=_CARDINALITY[edge.cardinality or "N:1"], adapter_id=adapter_id,
        authority=GraphEdgeAuthority.CATALOG_DECLARED, status=RelationshipStatus.ACTIVE)


def bridge_v1_from_entity_bridge(
    bridge: EntityBridge, *, left_catalog: str, right_catalog: str, bridge_id: str,
) -> EntityBridgeV1:
    return EntityBridgeV1(
        bridge_id=bridge_id, entity_id=bridge.entity, left_catalog_source=left_catalog,
        left_object_ref=bridge.from_ref, right_catalog_source=right_catalog,
        right_object_ref=bridge.to_ref, authority=GraphEdgeAuthority.ENTITY_BRIDGE,
        status=RelationshipStatus.ACTIVE)


def test_real_join_edges_map_to_valid_catalog_realizations():
    # Within-catalog roll-up realizations (a cross-catalog SAME-entity join is a bridge, tested below).
    cases = [
        (JoinEdge(from_ref="accounts.account_id", to_ref="accounts.customer_id",
                  cardinality="N:1", resolved=True), "account", "customer"),
        (JoinEdge(from_ref="cards.card_account_id", to_ref="cards.customer_id",
                  cardinality="N:1", resolved=True), "card_account", "customer"),
        (JoinEdge(from_ref="facilities.facility_id", to_ref="facilities.obligor_id",
                  cardinality="N:1", resolved=True), "facility", "obligor"),
    ]
    for i, (edge, from_entity, to_entity) in enumerate(cases):
        real = catalog_relationship_from_join_edge(
            edge, catalog_source="core", relationship_id=f"rel_{i}", adapter_id="core_adapter",
            from_entity=from_entity, to_entity=to_entity)
        validate_catalog_relationship(real, known=known_entities())   # the contract is self-consistent
        assert real.from_object_ref == edge.from_ref
        assert real.resolved_from_entity == from_entity and real.resolved_to_entity == to_entity
        assert real.declared_cardinality is Cardinality.MANY_TO_ONE


def test_real_entity_bridge_maps_to_valid_bridge_v1():
    b = EntityBridge(entity="account", from_ref="transactions.account_id",
                     to_ref="accounts.account_id")
    v1 = bridge_v1_from_entity_bridge(b, left_catalog="payments", right_catalog="core", bridge_id="b1")
    validate_entity_bridge(v1, known=known_entities())
    assert v1.entity_id == "account"
    assert v1.left_object_ref == "transactions.account_id"
    assert v1.authority is GraphEdgeAuthority.ENTITY_BRIDGE
```

- [ ] **Step 2: Run to verify it passes**

Run: `uv run pytest tests/featuregen/overlay/upload/taxonomy/test_entity_contract_feasibility.py -q`
Expected: PASS (2 tests). If the real `JoinEdge`/`EntityBridge` field names differ from `from_ref`/`to_ref`/`cardinality`/`entity`, the test fails HONESTLY — fix the transform to the real fields (that discovery is the whole point of this task); do NOT fall back to stand-in types.

- [ ] **Step 3: Confirm no production surface changed**

Run: `git status --short` — expected: only the new test file untracked; no `src/` file modified by this task.

- [ ] **Step 4: Full-suite regression + gates + commit**

```bash
uv run pytest tests/featuregen/overlay/ tests/featuregen/api/ -q
uv run ruff check src tests
uv run mypy src/featuregen/overlay/upload/taxonomy/
git add tests/featuregen/overlay/upload/taxonomy/test_entity_contract_feasibility.py
git commit -m "test(3a): contract feasibility spike over REAL join/bridge types (task 3A.6)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Exit criteria mapping (verify before finishing the phase)

| # | Exit criterion (spec) | Where satisfied |
|---|---|---|
| 1 | Every hardcoded relation in the registry | Task 2 `test_registry_is_exactly_the_five_seed_rollups_and_valid` |
| 2 | Every current compatibility fixture returns the same result | Task 5 regression over ALL `known_entities()` + existing `test_ranking_signals.py` green |
| 3 | Ranking + grain-warning responses unchanged | Task 5 Step 4 (`test_contract_ranked.py` green + no-leak guard) |
| 4 | Old map removed, not a fallback | Task 5 Step 3(c) deletes `_ENTITY_ROLLUP`/`_rolls_up_to` |
| 5 | Traversal cycle-safe + deterministic | Task 3 `test_semantic_cycle_rejected` (builder) + Task 4 `test_traversal_visited_guard...` (traversal) + sorted adjacency |
| 6 | AMBIGUOUS synthetic-only | Task 4 `test_seed_never_emits_ambiguous` + `test_ambiguous_two_paths_is_not_truncated` + `test_ambiguous_three_paths_is_truncated`; Task 5 `test_no_pair_produces_ambiguous` |
| 7 | Aggregation metadata preserved in paths | Task 4 `test_derivable_direct_and_transitive` (asserts `relationship_version`; ref carries `aggregation_required`/`aggregation_strategy`) |
| 8 | Graph version stable + observable | Task 4 (`result.graph_version`), Task 3 `test_invalid_graph_version_rejected` |
| 9 | Catalog joins representable by `CatalogEntityRelationshipV1` | Task 6 `test_real_join_edges_map_to_valid_catalog_realizations` (REAL `JoinEdge`) |
| 10 | Bridge data representable by `EntityBridgeV1` | Task 6 `test_real_entity_bridge_maps_to_valid_bridge_v1` (REAL `EntityBridge`) |
| 11 | Realizations + bridges NOT active traversal inputs | Task 3 builder ignores them; Task 6 transforms live in tests only |
| 12 | No migration / governance UI | No `db/migrations/*` created anywhere in this plan |

## Self-review notes

- **Behaviour-neutrality proven three ways:** the full-vocabulary resolver regression (`old == new` over every `known_entities()` pair), the untouched existing `test_ranking_signals.py`/`test_contract_ranked.py`, and a **route-level recursive scan** of a real scoped response confirming no graph key (`graph_version`/`paths`/`paths_truncated`/`relationship_version`) reaches the wire. The invariant is *semantic/schema equality* (parsed JSON), not byte equality.
- **Converging DAGs build:** `_reject_cycles` is a recursive three-colour DFS that distinguishes a back edge (node on the active path) from a shared descendant (fully-visited) — `test_converging_dag_is_not_treated_as_cycle` guards it, and the AMBIGUOUS tests depend on it.
- **`paths_truncated` is honest:** enumeration goes to `_MAX_COMPATIBILITY_PATHS + 1`, so exactly-two paths → `False`, three-plus → `True`.
- **Active-only validation:** deprecated definitions are archived (never validated or indexed) — a deliberate, documented model.
- **`AMBIGUOUS` additive-safety:** `entity_compatibility` feeds only the ranker reason stream (`is UNKNOWN`) and `signal_warnings` (`is DERIVABLE`) — no exhaustive `match`; the seed (acyclic, out-degree ≤1) never emits it.
- **Closed-vocabulary invariant** is enforced at the resolver (unknown source/target → UNKNOWN before the EXACT short-circuit), not just at the registry.
- **Curated graph correctness:** cycles, non-FORWARD active edges, duplicate ids, and duplicate semantic edges are all rejected at build; traversal keeps an independent visited-guard for a hand-built malformed graph.
- **No import cycle:** `EntityCompatibility` in `entity_relationships`; `entity_graph` imports it; `ranking_signals` imports from `entity_graph`/`entity_relationships` and re-exports the enum.
- **`MappingProxyType` note:** it is a `Mapping`, so assignment to the `Mapping[str, tuple[...]]` field type-checks; if a mypy version objects, wrap with `typing.cast(Mapping[...], MappingProxyType(...))`.

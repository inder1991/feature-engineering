# Phase 3A — Entity & Grain Graph Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hardcoded 5-entry `_ENTITY_ROLLUP` map with a curated, versioned global semantic entity-relationship graph, and rewire `entity_compatibility` to traverse it — byte-identically — while locking stable (but inactive) contracts for the later cross-catalog edge classes.

**Architecture:** A new pure, DB-free module trio under `taxonomy/`: `entity_relationships.py` (frozen-dataclass contracts + enums + per-definition validation), `entity_registry.py` (the curated `ENTITY_RELATIONSHIPS_V1` seed = the 5 existing roll-ups + `GRAPH_VERSION`), and `entity_graph.py` (an immutable built graph + `resolve_entity_compatibility` traversal returning a path-bearing result). `ranking_signals.entity_compatibility` becomes a thin adapter over the resolver; the old map is deleted, not kept as a fallback. Only `EntityRelationshipDefinitionV1` is an *active* graph edge — the other three contracts are defined, validated, and feasibility-tested but never populated or traversed in 3A.

**Tech Stack:** Python 3.11, `@dataclass(frozen=True, slots=True)`, `StrEnum`, `uv run pytest`, `uv run ruff check`, `uv run mypy`. No pydantic (the taxonomy package uses frozen dataclasses — the spec's `BaseModel` snippets were illustrative). No DB, no migration.

## Global Constraints

- **Behaviour-neutral, no flag.** For every currently-supported `(source, target)` entity pair, the new graph-backed result must equal the old `_ENTITY_ROLLUP` result. `EXACT`/`DERIVABLE`/`UNKNOWN` outputs, ranking order, and grain-warning responses are byte-identical. This is the load-bearing acceptance criterion.
- **Seed = EXACTLY the five roll-ups, nothing more:** `account→customer`, `card_account→customer`, `transaction→account`, `facility→obligor`, `policy→customer`. These form a forest (each source has ≤1 outgoing edge) so `AMBIGUOUS` is provably unreachable from the seed. Do NOT add any sixth relationship in 3A.
- **`AMBIGUOUS` is reserved capability:** add the enum member + traversal support, exercise it with synthetic multi-path fixtures ONLY; the production seed never emits it.
- **Only `EntityRelationshipDefinitionV1` is active.** `CatalogEntityRelationshipV1`, `EntityBridgeV1`, `EntityRelationshipProposalV1` are contracts only — defined, validated, feasibility-tested, but never built into the graph or traversed.
- **No hard reject.** Do NOT add `EntityCompatibility.INCOMPATIBLE` (deferred to 3D). The existing test `test_entity_compatibility_has_no_incompatible_member` must stay green.
- **Delete the old map** (`_ENTITY_ROLLUP`, `_rolls_up_to`) — no dead fallback path.
- **No DB migration, no governance UI.** The registry is in-code, like the taxonomy.
- **Entity vocabulary is closed:** every relationship endpoint must be in `known_entities()` (from `dimensions.py`, the distinct `Concept.entity_link` values).
- Commit messages end with: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Branch `feature/phase3-cross-catalog` is already checked out.

## File Structure

- **Create** `src/featuregen/overlay/upload/taxonomy/entity_relationships.py` — enums (`EntityCompatibility` moved here + gains `AMBIGUOUS`; `RelationshipType`, `Cardinality`, `TraversalDirection`, `AggregationType`, `RelationshipStatus`, `GraphEdgeAuthority`), the four edge contracts, the result types (`EntityRelationshipRefV1`, `EntitySemanticPathV1`, `EntityCompatibilityResultV1`), and `validate_relationship_definition`. Pure; depends only on `dimensions.known_entities`. [3A.1]
- **Create** `src/featuregen/overlay/upload/taxonomy/entity_registry.py` — `GRAPH_VERSION` + `ENTITY_RELATIONSHIPS_V1` (the 5 seeded defs). [3A.2]
- **Create** `src/featuregen/overlay/upload/taxonomy/entity_graph.py` — `EntityGraph`, `build_entity_graph`, the module singleton `ENTITY_GRAPH`, and `resolve_entity_compatibility`. [3A.3, 3A.4]
- **Modify** `src/featuregen/overlay/upload/taxonomy/ranking_signals.py` — delete `_ENTITY_ROLLUP`/`_rolls_up_to` + the local `EntityCompatibility` class; import `EntityCompatibility` (re-export) + `ENTITY_GRAPH` + `resolve_entity_compatibility`; rewire `entity_compatibility`. [3A.5]
- **Create** `tests/featuregen/overlay/upload/taxonomy/test_entity_relationships.py` [3A.1]
- **Create** `tests/featuregen/overlay/upload/taxonomy/test_entity_graph.py` [3A.3, 3A.4]
- **Create** `tests/featuregen/overlay/upload/taxonomy/test_entity_compatibility_regression.py` [3A.5]
- **Create** `tests/featuregen/overlay/upload/taxonomy/test_entity_contract_feasibility.py` [3A.6]

Import DAG (no cycles): `entity_relationships` ← `entity_registry` ← `entity_graph` ← `ranking_signals`. `entity_relationships` depends only on `dimensions`.

---

### Task 1 (3A.1): Entity graph contracts + validation

**Files:**
- Create: `src/featuregen/overlay/upload/taxonomy/entity_relationships.py`
- Test: `tests/featuregen/overlay/upload/taxonomy/test_entity_relationships.py`

**Interfaces:**
- Consumes: `from featuregen.overlay.upload.taxonomy.dimensions import known_entities` (`() -> frozenset[str]`).
- Produces: the enums `EntityCompatibility` (`EXACT`/`DERIVABLE`/`AMBIGUOUS`/`UNKNOWN`), `RelationshipType`, `Cardinality`, `TraversalDirection`, `AggregationType`, `RelationshipStatus`, `GraphEdgeAuthority`; the dataclasses `EntityRelationshipDefinitionV1`, `CatalogEntityRelationshipV1`, `EntityBridgeV1`, `EntityRelationshipProposalV1`, `EntityRelationshipRefV1`, `EntitySemanticPathV1`, `EntityCompatibilityResultV1`; and `validate_relationship_definition(defn, *, known: frozenset[str]) -> None` (raises `ValueError`).

- [ ] **Step 1: Write the failing tests**

Create `tests/featuregen/overlay/upload/taxonomy/test_entity_relationships.py`:

```python
"""Phase-3A Task 3A.1 — the entity-relationship contracts + per-definition validation.

Only EntityRelationshipDefinitionV1 is an active graph edge in 3A; the other three contracts are
defined + validated here so 3B builds against stable types. Validation is a pure guard over a single
definition — duplicate-id and graph-shape checks live in the builder (Task 3)."""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.taxonomy.dimensions import known_entities
from featuregen.overlay.upload.taxonomy.entity_relationships import (
    AggregationType,
    Cardinality,
    CatalogEntityRelationshipV1,
    EntityBridgeV1,
    EntityCompatibility,
    EntityRelationshipDefinitionV1,
    EntityRelationshipProposalV1,
    GraphEdgeAuthority,
    RelationshipStatus,
    RelationshipType,
    TraversalDirection,
    validate_relationship_definition,
)

KNOWN = known_entities()


def _defn(**overrides) -> EntityRelationshipDefinitionV1:
    base = dict(
        relationship_id="account_to_customer",
        from_entity="account",
        to_entity="customer",
        relationship_type=RelationshipType.ROLLUP,
        cardinality=Cardinality.MANY_TO_ONE,
        traversal_direction=TraversalDirection.FORWARD,
        aggregation_required=True,
        allowed_aggregations=(AggregationType.SUM, AggregationType.COUNT, AggregationType.AVERAGE),
        status=RelationshipStatus.ACTIVE,
        version="1.0.0",
    )
    base.update(overrides)
    return EntityRelationshipDefinitionV1(**base)


def test_ambiguous_is_a_member_but_incompatible_is_not():
    assert EntityCompatibility.AMBIGUOUS.value == "ambiguous"
    assert not hasattr(EntityCompatibility, "INCOMPATIBLE")   # hard reject deferred to 3D


def test_valid_definition_passes():
    validate_relationship_definition(_defn(), known=KNOWN)     # no raise


def test_dangling_endpoint_rejected():
    with pytest.raises(ValueError, match="unknown entity"):
        validate_relationship_definition(_defn(to_entity="not_an_entity"), known=KNOWN)


def test_self_relationship_rejected_unless_identity():
    with pytest.raises(ValueError, match="self-relationship"):
        validate_relationship_definition(
            _defn(from_entity="customer", to_entity="customer"), known=KNOWN)
    # an identity self-edge is allowed
    validate_relationship_definition(
        _defn(relationship_id="customer_identity", from_entity="customer", to_entity="customer",
              relationship_type=RelationshipType.IDENTITY, aggregation_required=False,
              allowed_aggregations=()),
        known=KNOWN)


def test_rollup_must_be_forward():
    with pytest.raises(ValueError, match="reverse"):
        validate_relationship_definition(
            _defn(traversal_direction=TraversalDirection.BOTH), known=KNOWN)


def test_aggregation_required_without_allowed_is_rejected():
    with pytest.raises(ValueError, match="aggregation"):
        validate_relationship_definition(
            _defn(aggregation_required=True, allowed_aggregations=()), known=KNOWN)


def test_invalid_version_rejected():
    with pytest.raises(ValueError, match="version"):
        validate_relationship_definition(_defn(version="v1"), known=KNOWN)


def test_inactive_contracts_are_constructible():
    # The three not-yet-active contracts exist and validate structurally (used by 3B).
    real = CatalogEntityRelationshipV1(
        realization_id="core_accounts_account_customer", relationship_id="account_to_customer",
        catalog_source="core_accounts", from_object_ref="accounts.account_id",
        to_object_ref="accounts.customer_id", declared_cardinality=Cardinality.MANY_TO_ONE,
        adapter_id="core_banking_adapter", authority="catalog_declared",
        status=RelationshipStatus.ACTIVE)
    assert real.authority == GraphEdgeAuthority.CATALOG_DECLARED.value
    bridge = EntityBridgeV1(
        bridge_id="b1", entity_id="account", left_catalog_source="payments",
        left_object_ref="transactions.account_id", right_catalog_source="core_accounts",
        right_object_ref="accounts.account_id", authority="entity_bridge",
        status=RelationshipStatus.ACTIVE)
    assert bridge.entity_id == "account"
    prop = EntityRelationshipProposalV1(
        proposal_id="p1", proposed_from_entity="account", proposed_to_entity="customer",
        proposed_cardinality=Cardinality.MANY_TO_ONE, evidence_refs=("edge:1",),
        source_catalog="core_accounts", inferred_by="join_inspector", status="pending")
    assert prop.status == "pending"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/featuregen/overlay/upload/taxonomy/test_entity_relationships.py -q`
Expected: FAIL — `ModuleNotFoundError: featuregen.overlay.upload.taxonomy.entity_relationships`.

- [ ] **Step 3: Write the implementation**

Create `src/featuregen/overlay/upload/taxonomy/entity_relationships.py`:

```python
"""Phase-3A — entity-relationship contracts, enums, result types, and per-definition validation.

The GLOBAL semantic relationship (:class:`EntityRelationshipDefinitionV1`) is the ONLY edge class the
3A graph traverses. The catalog-realization / entity-bridge / proposal contracts are defined here so
Phase 3B builds against stable, versioned types — but 3A never populates or traverses them. Validation
is a pure guard over ONE definition; duplicate-id and graph-shape checks live in the graph builder.

``EntityCompatibility`` lives here (not in ``ranking_signals``) so the graph resolver and the ranking
adapter can both import it without a cycle; ``ranking_signals`` re-exports it for its existing callers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


class EntityCompatibility(StrEnum):
    """Soft grain fit of a recipe to a confirmed ``target_entity`` (Phase-2B semantics, now graph-backed).
    ``EXACT`` grain == target; ``DERIVABLE`` a single roll-up path reaches the target; ``AMBIGUOUS``
    several distinct paths do (reserved — the 3A seed forest never emits it); ``UNKNOWN`` no target / no
    path. There is deliberately **no** ``INCOMPATIBLE`` — a hard entity reject is deferred to Phase 3D."""

    EXACT = "exact"
    DERIVABLE = "derivable"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


class RelationshipType(StrEnum):
    ROLLUP = "rollup"
    PARENT_CHILD = "parent_child"
    OWNERSHIP = "ownership"
    MEMBERSHIP = "membership"
    IDENTITY = "identity"


class Cardinality(StrEnum):
    ONE_TO_ONE = "one_to_one"
    ONE_TO_MANY = "one_to_many"
    MANY_TO_ONE = "many_to_one"
    MANY_TO_MANY = "many_to_many"


class TraversalDirection(StrEnum):
    FORWARD = "forward"
    REVERSE = "reverse"
    BOTH = "both"


class AggregationType(StrEnum):
    SUM = "sum"
    COUNT = "count"
    AVERAGE = "average"
    MIN = "min"
    MAX = "max"


class RelationshipStatus(StrEnum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"


class GraphEdgeAuthority(StrEnum):
    GLOBAL_ENTITY_MODEL = "global_entity_model"
    CATALOG_DECLARED = "catalog_declared"
    ENTITY_BRIDGE = "entity_bridge"


@dataclass(frozen=True, slots=True)
class EntityRelationshipDefinitionV1:
    """A GLOBAL semantic entity relationship — the only edge class the 3A graph traverses. It answers
    'is grain ``from_entity`` semantically derivable into ``to_entity``?' and carries the aggregation the
    roll-up requires (metadata for 3B). It contains NO physical column mapping — that is a catalog
    realization (:class:`CatalogEntityRelationshipV1`)."""

    relationship_id: str
    from_entity: str
    to_entity: str
    relationship_type: RelationshipType
    cardinality: Cardinality
    traversal_direction: TraversalDirection
    aggregation_required: bool
    allowed_aggregations: tuple[AggregationType, ...]
    status: RelationshipStatus
    version: str


@dataclass(frozen=True, slots=True)
class CatalogEntityRelationshipV1:
    """CONTRACT ONLY in 3A. How one catalog physically realizes a global relationship (its two object
    refs). References a global ``relationship_id``; lower authority than the global model; scoped to its
    catalog. Populated + validated against the global model in Phase 3B."""

    realization_id: str
    relationship_id: str
    catalog_source: str
    from_object_ref: str
    to_object_ref: str
    declared_cardinality: Cardinality
    adapter_id: str
    authority: str = GraphEdgeAuthority.CATALOG_DECLARED.value
    status: RelationshipStatus = RelationshipStatus.ACTIVE


@dataclass(frozen=True, slots=True)
class EntityBridgeV1:
    """CONTRACT ONLY in 3A. A sanctioned cross-catalog identity link: two catalog-local representations
    of the SAME entity. It asserts identity, never a new roll-up. Governed activation is Phase 3B (today
    bridges are computed permissively on the fly — see ``entity.cross_join_via_entity``)."""

    bridge_id: str
    entity_id: str
    left_catalog_source: str
    left_object_ref: str
    right_catalog_source: str
    right_object_ref: str
    authority: str = GraphEdgeAuthority.ENTITY_BRIDGE.value
    status: RelationshipStatus = RelationshipStatus.ACTIVE


@dataclass(frozen=True, slots=True)
class EntityRelationshipProposalV1:
    """CONTRACT ONLY in 3A, and NEVER traversed by the active graph. A metadata-derived candidate
    relationship — evidence, not truth. Promotion (mapping to a global relationship or ratification as a
    catalog-scoped edge) is a governance step in Phase 3B."""

    proposal_id: str
    proposed_from_entity: str
    proposed_to_entity: str
    proposed_cardinality: Cardinality
    evidence_refs: tuple[str, ...]
    source_catalog: str
    inferred_by: str
    status: str   # pending | accepted | rejected


@dataclass(frozen=True, slots=True)
class EntityRelationshipRefV1:
    """One hop in a resolved semantic path: the relationship + the aggregation its roll-up requires."""

    relationship_id: str
    from_entity: str
    to_entity: str
    cardinality: Cardinality
    aggregation_required: bool
    allowed_aggregations: tuple[AggregationType, ...]


@dataclass(frozen=True, slots=True)
class EntitySemanticPathV1:
    """One distinct directed roll-up chain ``source → … → target``, hops in order."""

    hops: tuple[EntityRelationshipRefV1, ...]


@dataclass(frozen=True, slots=True)
class EntityCompatibilityResultV1:
    """The full traversal result. ``paths`` is ``()`` for EXACT/UNKNOWN, one path for DERIVABLE, all for
    AMBIGUOUS. ``graph_version`` stamps the graph the result came from (provenance for rank/warnings)."""

    status: EntityCompatibility
    source_entity: str
    target_entity: str
    paths: tuple[EntitySemanticPathV1, ...]
    reason_codes: tuple[str, ...]
    graph_version: str


def validate_relationship_definition(
    defn: EntityRelationshipDefinitionV1, *, known: frozenset[str]) -> None:
    """Pure per-definition guard. Raises ``ValueError`` on: an endpoint outside the closed entity
    vocabulary; a self-relationship on a non-identity type; a ROLLUP that is not FORWARD-only
    ('reverse' roll-up is unsupported — a roll-up is inherently child→parent); an aggregation-required
    relationship with no allowed aggregation; a non-semver ``version``. Duplicate active ids are a
    graph-build concern, not a per-definition one."""
    if defn.from_entity not in known:
        raise ValueError(f"unknown entity: {defn.from_entity!r}")
    if defn.to_entity not in known:
        raise ValueError(f"unknown entity: {defn.to_entity!r}")
    if defn.from_entity == defn.to_entity and defn.relationship_type is not RelationshipType.IDENTITY:
        raise ValueError(f"self-relationship not allowed for {defn.relationship_type.value!r}")
    if defn.relationship_type is RelationshipType.ROLLUP \
            and defn.traversal_direction is not TraversalDirection.FORWARD:
        raise ValueError("a rollup must be FORWARD-only (reverse roll-up is unsupported)")
    if defn.aggregation_required and not defn.allowed_aggregations:
        raise ValueError("aggregation_required with no allowed_aggregations")
    if not _SEMVER.match(defn.version):
        raise ValueError(f"invalid version: {defn.version!r} (expected N.N.N)")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/featuregen/overlay/upload/taxonomy/test_entity_relationships.py -q`
Expected: PASS (8 tests).

- [ ] **Step 5: Lint + type-check + commit**

```bash
uv run ruff check src/featuregen/overlay/upload/taxonomy/entity_relationships.py tests/featuregen/overlay/upload/taxonomy/test_entity_relationships.py
uv run mypy src/featuregen/overlay/upload/taxonomy/entity_relationships.py
git add src/featuregen/overlay/upload/taxonomy/entity_relationships.py tests/featuregen/overlay/upload/taxonomy/test_entity_relationships.py
git commit -m "feat(3a): entity-relationship contracts + per-definition validation (task 3A.1)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2 (3A.2): Curated semantic registry

**Files:**
- Create: `src/featuregen/overlay/upload/taxonomy/entity_registry.py`
- Test: append to `tests/featuregen/overlay/upload/taxonomy/test_entity_relationships.py`

**Interfaces:**
- Consumes: everything from Task 1; `known_entities()`.
- Produces: `GRAPH_VERSION: str` (`"1.0.0"`), `ENTITY_RELATIONSHIPS_V1: tuple[EntityRelationshipDefinitionV1, ...]` (the 5 seeded roll-ups).

- [ ] **Step 1: Write the failing test**

Append to `tests/featuregen/overlay/upload/taxonomy/test_entity_relationships.py`:

```python
from featuregen.overlay.upload.taxonomy.entity_registry import (
    ENTITY_RELATIONSHIPS_V1,
    GRAPH_VERSION,
)


def test_registry_is_the_five_seed_rollups_and_valid():
    edges = {(d.from_entity, d.to_entity) for d in ENTITY_RELATIONSHIPS_V1}
    assert edges == {
        ("account", "customer"), ("card_account", "customer"), ("transaction", "account"),
        ("facility", "obligor"), ("policy", "customer")}
    # every def is individually valid + every endpoint is in the closed vocabulary
    for d in ENTITY_RELATIONSHIPS_V1:
        validate_relationship_definition(d, known=KNOWN)
    assert GRAPH_VERSION == "1.0.0"


def test_registry_is_a_forest_so_ambiguous_is_unreachable():
    # Each source has AT MOST ONE outgoing active edge -> no pair has two paths -> no AMBIGUOUS.
    from collections import Counter
    out_degree = Counter(
        d.from_entity for d in ENTITY_RELATIONSHIPS_V1 if d.status is RelationshipStatus.ACTIVE)
    assert all(n == 1 for n in out_degree.values())


def test_registry_relationship_ids_are_unique():
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

The single source of truth for entity-grain derivability. Seeded with EXACTLY the five roll-ups that
Phase-2B's hardcoded ``_ENTITY_ROLLUP`` expressed — a forest (each source has one parent), so the graph
built from it is regression-equivalent and never emits ``AMBIGUOUS``. In-code + version-controlled, like
the use-case taxonomy; no DB persistence in 3A. Adding relationships that could create a second path for
an existing pair is a Phase-3D concern, not 3A."""
from __future__ import annotations

from featuregen.overlay.upload.taxonomy.entity_relationships import (
    AggregationType,
    Cardinality,
    EntityRelationshipDefinitionV1,
    RelationshipStatus,
    RelationshipType,
    TraversalDirection,
)

GRAPH_VERSION = "1.0.0"

_ROLLUP_AGGS = (AggregationType.SUM, AggregationType.COUNT, AggregationType.AVERAGE,
                AggregationType.MIN, AggregationType.MAX)


def _rollup(relationship_id: str, from_entity: str, to_entity: str) -> EntityRelationshipDefinitionV1:
    return EntityRelationshipDefinitionV1(
        relationship_id=relationship_id, from_entity=from_entity, to_entity=to_entity,
        relationship_type=RelationshipType.ROLLUP, cardinality=Cardinality.MANY_TO_ONE,
        traversal_direction=TraversalDirection.FORWARD, aggregation_required=True,
        allowed_aggregations=_ROLLUP_AGGS, status=RelationshipStatus.ACTIVE, version="1.0.0")


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
Expected: PASS (11 tests). If `test_registry_is_the_five_seed_rollups_and_valid` fails on an endpoint not in `KNOWN`, the entity is missing from the concept registry's `entity_link` values — stop and report (do not add a fake concept); every seed endpoint (`customer`/`account`/`card_account`/`transaction`/`facility`/`obligor`/`policy`) is expected to already be a known entity.

- [ ] **Step 5: Lint + type-check + commit**

```bash
uv run ruff check src/featuregen/overlay/upload/taxonomy/entity_registry.py
uv run mypy src/featuregen/overlay/upload/taxonomy/entity_registry.py
git add src/featuregen/overlay/upload/taxonomy/entity_registry.py tests/featuregen/overlay/upload/taxonomy/test_entity_relationships.py
git commit -m "feat(3a): curated global entity-relationship registry — 5 seed rollups (task 3A.2)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3 (3A.3): Semantic graph builder

**Files:**
- Create: `src/featuregen/overlay/upload/taxonomy/entity_graph.py`
- Test: `tests/featuregen/overlay/upload/taxonomy/test_entity_graph.py`

**Interfaces:**
- Consumes: Task 1 contracts; `known_entities()`.
- Produces: `EntityGraph` (frozen; `.version: str`, `.outgoing(entity: str) -> tuple[EntityRelationshipDefinitionV1, ...]`), `build_entity_graph(defs, *, version, known) -> EntityGraph`.

- [ ] **Step 1: Write the failing tests**

Create `tests/featuregen/overlay/upload/taxonomy/test_entity_graph.py`:

```python
"""Phase-3A Tasks 3A.3/3A.4 — the immutable semantic graph builder + compatibility traversal."""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.taxonomy.dimensions import known_entities
from featuregen.overlay.upload.taxonomy.entity_relationships import (
    AggregationType,
    Cardinality,
    EntityRelationshipDefinitionV1,
    RelationshipStatus,
    RelationshipType,
    TraversalDirection,
)
from featuregen.overlay.upload.taxonomy.entity_graph import build_entity_graph

KNOWN = known_entities()


def _e(rid, a, b, *, status=RelationshipStatus.ACTIVE) -> EntityRelationshipDefinitionV1:
    return EntityRelationshipDefinitionV1(
        relationship_id=rid, from_entity=a, to_entity=b, relationship_type=RelationshipType.ROLLUP,
        cardinality=Cardinality.MANY_TO_ONE, traversal_direction=TraversalDirection.FORWARD,
        aggregation_required=True, allowed_aggregations=(AggregationType.SUM,), status=status,
        version="1.0.0")


def test_build_indexes_active_outgoing_edges():
    g = build_entity_graph(
        (_e("t_a", "transaction", "account"), _e("a_c", "account", "customer")),
        version="1.0.0", known=KNOWN)
    assert g.version == "1.0.0"
    assert [d.relationship_id for d in g.outgoing("transaction")] == ["t_a"]
    assert [d.relationship_id for d in g.outgoing("account")] == ["a_c"]
    assert g.outgoing("customer") == ()          # sink


def test_inactive_edges_excluded():
    g = build_entity_graph(
        (_e("a_c", "account", "customer", status=RelationshipStatus.DEPRECATED),),
        version="1.0.0", known=KNOWN)
    assert g.outgoing("account") == ()


def test_outgoing_is_sorted_deterministically():
    # two active edges from the same source arrive out of id order -> stored sorted by relationship_id
    g = build_entity_graph(
        (_e("z_edge", "account", "customer"), _e("a_edge", "account", "obligor")),
        version="1.0.0", known=KNOWN)
    assert [d.relationship_id for d in g.outgoing("account")] == ["a_edge", "z_edge"]


def test_duplicate_active_relationship_id_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        build_entity_graph(
            (_e("dup", "account", "customer"), _e("dup", "transaction", "account")),
            version="1.0.0", known=KNOWN)


def test_builder_validates_each_definition():
    with pytest.raises(ValueError, match="unknown entity"):
        build_entity_graph((_e("bad", "account", "not_an_entity"),), version="1.0.0", known=KNOWN)


def test_graph_tolerates_a_cycle_at_build_time():
    # A cyclic graph must BUILD (traversal handles the cycle) — building never recurses.
    g = build_entity_graph(
        (_e("a_b", "account", "customer"), _e("b_a", "customer", "account")),
        version="1.0.0", known=KNOWN)
    assert [d.relationship_id for d in g.outgoing("account")] == ["a_b"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/taxonomy/test_entity_graph.py -q`
Expected: FAIL — `ModuleNotFoundError: ... entity_graph`.

- [ ] **Step 3: Write the implementation**

Create `src/featuregen/overlay/upload/taxonomy/entity_graph.py` (traversal added in Task 4 — this task ships only the builder + `EntityGraph`):

```python
"""Phase-3A — the immutable global semantic entity graph + compatibility traversal.

Built ONCE from the curated registry (Task 3A.2). Only active :class:`EntityRelationshipDefinitionV1`
edges are indexed; catalog realizations and bridges are NOT inputs in 3A. Outgoing edges are stored
sorted by ``relationship_id`` so traversal (and its path enumeration) is deterministic."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from featuregen.overlay.upload.taxonomy.entity_relationships import (
    EntityRelationshipDefinitionV1,
    RelationshipStatus,
    validate_relationship_definition,
)


@dataclass(frozen=True, slots=True)
class EntityGraph:
    """An immutable adjacency of active semantic relationships. ``outgoing(entity)`` returns the entity's
    active outgoing edges, sorted by ``relationship_id`` (deterministic)."""

    version: str
    _adjacency: Mapping[str, tuple[EntityRelationshipDefinitionV1, ...]]

    def outgoing(self, entity: str) -> tuple[EntityRelationshipDefinitionV1, ...]:
        return self._adjacency.get(entity, ())


def build_entity_graph(
    defs: tuple[EntityRelationshipDefinitionV1, ...], *, version: str, known: frozenset[str],
) -> EntityGraph:
    """Validate every definition, reject duplicate ACTIVE relationship ids, and index the active edges by
    ``from_entity`` (sorted by ``relationship_id``). Inactive (deprecated) edges are excluded. Building is
    non-recursive, so a cyclic input builds fine — traversal is where cycles are guarded."""
    seen_ids: set[str] = set()
    by_source: dict[str, list[EntityRelationshipDefinitionV1]] = {}
    for d in defs:
        validate_relationship_definition(d, known=known)
        if d.status is not RelationshipStatus.ACTIVE:
            continue
        if d.relationship_id in seen_ids:
            raise ValueError(f"duplicate active relationship_id: {d.relationship_id!r}")
        seen_ids.add(d.relationship_id)
        by_source.setdefault(d.from_entity, []).append(d)
    adjacency = {
        src: tuple(sorted(edges, key=lambda e: e.relationship_id))
        for src, edges in by_source.items()}
    return EntityGraph(version=version, _adjacency=MappingProxyType(adjacency))
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/featuregen/overlay/upload/taxonomy/test_entity_graph.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Lint + type-check + commit**

```bash
uv run ruff check src/featuregen/overlay/upload/taxonomy/entity_graph.py tests/featuregen/overlay/upload/taxonomy/test_entity_graph.py
uv run mypy src/featuregen/overlay/upload/taxonomy/entity_graph.py
git add src/featuregen/overlay/upload/taxonomy/entity_graph.py tests/featuregen/overlay/upload/taxonomy/test_entity_graph.py
git commit -m "feat(3a): immutable semantic graph builder (task 3A.3)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4 (3A.4): Compatibility traversal

**Files:**
- Modify: `src/featuregen/overlay/upload/taxonomy/entity_graph.py` (add traversal + the `ENTITY_GRAPH` singleton)
- Test: append to `tests/featuregen/overlay/upload/taxonomy/test_entity_graph.py`

**Interfaces:**
- Consumes: `EntityGraph`, the contracts, the registry (`ENTITY_RELATIONSHIPS_V1`, `GRAPH_VERSION`).
- Produces: `resolve_entity_compatibility(source: str, target: str, graph: EntityGraph) -> EntityCompatibilityResultV1`; module singleton `ENTITY_GRAPH: EntityGraph` built from the registry.

- [ ] **Step 1: Write the failing tests**

Append to `tests/featuregen/overlay/upload/taxonomy/test_entity_graph.py`:

```python
from featuregen.overlay.upload.taxonomy.entity_graph import (
    ENTITY_GRAPH,
    resolve_entity_compatibility,
)
from featuregen.overlay.upload.taxonomy.entity_relationships import EntityCompatibility


def test_exact_when_source_equals_target():
    r = resolve_entity_compatibility("customer", "customer", ENTITY_GRAPH)
    assert r.status is EntityCompatibility.EXACT
    assert r.paths == ()
    assert r.graph_version == ENTITY_GRAPH.version


def test_derivable_single_direct_path():
    r = resolve_entity_compatibility("account", "customer", ENTITY_GRAPH)
    assert r.status is EntityCompatibility.DERIVABLE
    assert len(r.paths) == 1
    assert [h.relationship_id for h in r.paths[0].hops] == ["account_to_customer"]
    # the roll-up's aggregation metadata rides the path (for 3B)
    assert r.paths[0].hops[0].aggregation_required is True


def test_derivable_transitive_path():
    r = resolve_entity_compatibility("transaction", "customer", ENTITY_GRAPH)
    assert r.status is EntityCompatibility.DERIVABLE
    assert [h.to_entity for h in r.paths[0].hops] == ["account", "customer"]


def test_unknown_when_no_path():
    # customer does not roll up to account (forest is directional child->parent)
    assert resolve_entity_compatibility("customer", "account", ENTITY_GRAPH).status \
        is EntityCompatibility.UNKNOWN


def test_seed_graph_never_emits_ambiguous():
    entities = ("customer", "account", "card_account", "transaction", "facility", "obligor", "policy")
    for s in entities:
        for t in entities:
            assert resolve_entity_compatibility(s, t, ENTITY_GRAPH).status \
                is not EntityCompatibility.AMBIGUOUS


def test_ambiguous_on_synthetic_two_path_graph():
    # transaction -> account -> customer AND transaction -> card_account -> customer: two distinct paths.
    g = build_entity_graph(
        (_e("t_a", "transaction", "account"), _e("a_c", "account", "customer"),
         _e("t_ca", "transaction", "card_account"), _e("ca_c", "card_account", "customer")),
        version="synthetic", known=KNOWN)
    r = resolve_entity_compatibility("transaction", "customer", g)
    assert r.status is EntityCompatibility.AMBIGUOUS
    assert len(r.paths) == 2           # both surfaced, never an arbitrary winner


def test_traversal_is_cycle_safe():
    g = build_entity_graph(
        (_e("a_b", "account", "customer"), _e("b_a", "customer", "account")),
        version="cyc", known=KNOWN)
    # terminates, and finds the one simple path account -> customer
    r = resolve_entity_compatibility("account", "customer", g)
    assert r.status is EntityCompatibility.DERIVABLE
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
        relationship_id=d.relationship_id, from_entity=d.from_entity, to_entity=d.to_entity,
        cardinality=d.cardinality, aggregation_required=d.aggregation_required,
        allowed_aggregations=d.allowed_aggregations)


def _all_simple_paths(
    graph: EntityGraph, source: str, target: str,
) -> list[tuple[EntityRelationshipDefinitionV1, ...]]:
    """Every distinct simple directed path ``source → target`` over active forward edges. Cycle-safe via a
    visited set; deterministic because outgoing edges are pre-sorted by ``relationship_id``."""
    results: list[tuple[EntityRelationshipDefinitionV1, ...]] = []

    def _walk(node: str, path: tuple[EntityRelationshipDefinitionV1, ...], visited: frozenset[str]) -> None:
        if node == target:
            results.append(path)
            return
        for edge in graph.outgoing(node):
            nxt = edge.to_entity
            if nxt in visited:
                continue
            _walk(nxt, (*path, edge), visited | {nxt})

    _walk(source, (), frozenset({source}))
    return results


def resolve_entity_compatibility(
    source: str, target: str, graph: EntityGraph) -> EntityCompatibilityResultV1:
    """Graph-backed grain compatibility. ``source == target`` → EXACT; exactly one directed path →
    DERIVABLE; several distinct paths → AMBIGUOUS (both surfaced, never a shortest-path pick); no path →
    UNKNOWN. Never raises; totally defined over any two entity strings."""
    if source == target:
        return EntityCompatibilityResultV1(
            status=EntityCompatibility.EXACT, source_entity=source, target_entity=target,
            paths=(), reason_codes=(), graph_version=graph.version)
    raw = _all_simple_paths(graph, source, target)
    paths = tuple(EntitySemanticPathV1(hops=tuple(_ref(e) for e in p)) for p in raw)
    if len(paths) == 0:
        return EntityCompatibilityResultV1(
            status=EntityCompatibility.UNKNOWN, source_entity=source, target_entity=target,
            paths=(), reason_codes=("no_entity_path",), graph_version=graph.version)
    if len(paths) == 1:
        return EntityCompatibilityResultV1(
            status=EntityCompatibility.DERIVABLE, source_entity=source, target_entity=target,
            paths=paths, reason_codes=(), graph_version=graph.version)
    return EntityCompatibilityResultV1(
        status=EntityCompatibility.AMBIGUOUS, source_entity=source, target_entity=target,
        paths=paths, reason_codes=("multiple_entity_paths",), graph_version=graph.version)


# Built ONCE at import from the curated registry — the single active graph in 3A.
ENTITY_GRAPH: EntityGraph = build_entity_graph(
    ENTITY_RELATIONSHIPS_V1, version=GRAPH_VERSION, known=known_entities())
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/featuregen/overlay/upload/taxonomy/test_entity_graph.py -q`
Expected: PASS (13 tests).

- [ ] **Step 5: Lint + type-check + commit**

```bash
uv run ruff check src/featuregen/overlay/upload/taxonomy/entity_graph.py tests/featuregen/overlay/upload/taxonomy/test_entity_graph.py
uv run mypy src/featuregen/overlay/upload/taxonomy/entity_graph.py
git add src/featuregen/overlay/upload/taxonomy/entity_graph.py tests/featuregen/overlay/upload/taxonomy/test_entity_graph.py
git commit -m "feat(3a): compatibility traversal — EXACT/DERIVABLE/AMBIGUOUS/UNKNOWN (task 3A.4)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5 (3A.5): Rewire `entity_compatibility` (byte-identical) + delete the old map

**Files:**
- Modify: `src/featuregen/overlay/upload/taxonomy/ranking_signals.py` (lines 191–262 region: remove the local `EntityCompatibility` class, `_ENTITY_ROLLUP`, `_rolls_up_to`; re-import `EntityCompatibility`; rewire `entity_compatibility`)
- Create: `tests/featuregen/overlay/upload/taxonomy/test_entity_compatibility_regression.py`

**Interfaces:**
- Consumes: `resolve_entity_compatibility`, `ENTITY_GRAPH`, `EntityCompatibility` (from `entity_relationships`), `_grain_entity` (unchanged, stays in `ranking_signals`).
- Produces: `entity_compatibility(t: Template, target_entity: str | None = None) -> EntityCompatibility` (unchanged external signature + enum return); `EntityCompatibility` remains importable from `ranking_signals` (re-export).

- [ ] **Step 1: Write the failing regression test**

Create `tests/featuregen/overlay/upload/taxonomy/test_entity_compatibility_regression.py`:

```python
"""Phase-3A Task 3A.5 — THE load-bearing test: the graph resolver reproduces the deleted _ENTITY_ROLLUP
map exactly, for every entity pair the old map could produce. EXPECTED is computed from the OLD map's
semantics (frozen here since the map itself is being deleted)."""
from __future__ import annotations

from featuregen.overlay.upload.taxonomy.entity_graph import ENTITY_GRAPH, resolve_entity_compatibility
from featuregen.overlay.upload.taxonomy.entity_relationships import EntityCompatibility

# The deleted map (frozen for the regression oracle only).
_OLD_ROLLUP = {
    "account": "customer", "card_account": "customer", "transaction": "account",
    "facility": "obligor", "policy": "customer"}
_ENTITIES = ("customer", "account", "card_account", "transaction", "facility", "obligor", "policy")


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


def test_graph_reproduces_old_rollup_for_every_pair():
    for source in _ENTITIES:
        for target in _ENTITIES:
            assert resolve_entity_compatibility(source, target, ENTITY_GRAPH).status \
                == _old_status(source, target), f"{source}->{target}"


def test_graph_never_produces_ambiguous_or_incompatible_for_seed_pairs():
    for source in _ENTITIES:
        for target in _ENTITIES:
            status = resolve_entity_compatibility(source, target, ENTITY_GRAPH).status
            assert status in (
                EntityCompatibility.EXACT, EntityCompatibility.DERIVABLE, EntityCompatibility.UNKNOWN)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/taxonomy/test_entity_compatibility_regression.py -q`
Expected: PASS already (it exercises Task-4 code directly). This test guards the resolver; the *rewire* is validated by the existing `test_ranking_signals.py` staying green after Step 3. Proceed to Step 3.

- [ ] **Step 3: Rewire `ranking_signals.py`**

In `src/featuregen/overlay/upload/taxonomy/ranking_signals.py`:

(a) Add to the import block near the top (after the existing `from ... .templates import GroundedFeature, Template`):

```python
from featuregen.overlay.upload.taxonomy.entity_graph import (
    ENTITY_GRAPH,
    resolve_entity_compatibility,
)
from featuregen.overlay.upload.taxonomy.entity_relationships import EntityCompatibility
```

(b) DELETE the local `class EntityCompatibility(StrEnum): ...` block (the enum now lives in `entity_relationships`; it is re-exported by the import above so every existing `from ...ranking_signals import EntityCompatibility` keeps working).

(c) DELETE `_ENTITY_ROLLUP`, `_rolls_up_to`, and their comments. KEEP `_grain_entity` unchanged.

(d) Replace the body of `entity_compatibility` with the adapter (keep the exact signature and docstring intent):

```python
def entity_compatibility(t: Template, target_entity: str | None = None) -> EntityCompatibility:
    """The SOFT grain fit of the recipe to a confirmed ``target_entity`` — a grain/groundability signal
    (a low rank tie-break + an ``entity_grain_mismatch`` warning on ``DERIVABLE``), NEVER an
    applicability reject. Phase-3A: the grain relationship is resolved by the governed entity graph
    (:func:`resolve_entity_compatibility` over :data:`ENTITY_GRAPH`) instead of a hardcoded map — the
    seed is regression-equivalent, so outputs are byte-identical. ``target_entity is None`` or a recipe
    with no derivable grain → ``UNKNOWN`` (the axis is a no-op in ranking)."""
    if target_entity is None:
        return EntityCompatibility.UNKNOWN
    source = _grain_entity(t)
    if source is None:
        return EntityCompatibility.UNKNOWN
    return resolve_entity_compatibility(source, target_entity, ENTITY_GRAPH).status
```

- [ ] **Step 4: Run the regression + the FULL existing ranking/route suites (byte-identical proof)**

Run: `uv run pytest tests/featuregen/overlay/upload/taxonomy/test_ranking_signals.py tests/featuregen/overlay/upload/taxonomy/test_entity_compatibility_regression.py tests/featuregen/api/test_contract_ranked.py -q`
Expected: PASS — all existing `test_ranking_signals.py` entity-compatibility tests green unchanged (adapter is byte-identical), the regression test green, and the route-ranking tests green (ranking output unchanged). If any `test_ranking_signals.py` test fails, the rewire changed behaviour — stop and diagnose (do NOT edit the existing tests to match).

- [ ] **Step 5: Lint + type-check + commit**

```bash
uv run ruff check src/featuregen/overlay/upload/taxonomy/ranking_signals.py tests/featuregen/overlay/upload/taxonomy/test_entity_compatibility_regression.py
uv run mypy src/featuregen/overlay/upload/taxonomy/ranking_signals.py
git add src/featuregen/overlay/upload/taxonomy/ranking_signals.py tests/featuregen/overlay/upload/taxonomy/test_entity_compatibility_regression.py
git commit -m "feat(3a): rewire entity_compatibility onto the graph, delete _ENTITY_ROLLUP (task 3A.5)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6 (3A.6): Future-contract feasibility spike (tests only)

**Files:**
- Create: `tests/featuregen/overlay/upload/taxonomy/test_entity_contract_feasibility.py`

**Interfaces:**
- Consumes: `CatalogEntityRelationshipV1`, `EntityBridgeV1`, `Cardinality`, `RelationshipStatus` (Task 1); the existing `JoinEdge` (`graph.py`) and `EntityBridge` (`entity.py`) shapes.
- Produces: nothing in production — a pure, in-test transform proving the *contracts* can carry real upload metadata. NO production graph change; the transforms live in the test file (they are promoted to production in 3B, where the planner consumes them).

- [ ] **Step 1: Write the feasibility tests**

Create `tests/featuregen/overlay/upload/taxonomy/test_entity_contract_feasibility.py`:

```python
"""Phase-3A Task 3A.6 — feasibility spike (TESTS ONLY). Prove the not-yet-active contracts can represent
real upload metadata BEFORE 3B commits to them. The transforms live here, not in production: 3A never
populates or traverses catalog realizations or bridges. (3B promotes these to production code.)"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.overlay.upload.taxonomy.entity_relationships import (
    Cardinality,
    CatalogEntityRelationshipV1,
    EntityBridgeV1,
    GraphEdgeAuthority,
    RelationshipStatus,
)


# Minimal stand-ins mirroring the real shapes (graph.JoinEdge / entity.EntityBridge) so the spike does
# not need a DB. The field names match the production dataclasses.
@dataclass(frozen=True)
class _JoinEdge:
    from_ref: str
    to_ref: str
    cardinality: str | None
    resolved: bool


@dataclass(frozen=True)
class _EntityBridge:
    entity: str
    from_ref: str
    to_ref: str


_CARDINALITY = {"N:1": Cardinality.MANY_TO_ONE, "1:1": Cardinality.ONE_TO_ONE,
                "1:N": Cardinality.ONE_TO_MANY, "N:N": Cardinality.MANY_TO_MANY}


def catalog_relationship_from_join_edge(
    edge: _JoinEdge, *, catalog_source: str, relationship_id: str, adapter_id: str,
) -> CatalogEntityRelationshipV1:
    """A single per-catalog join edge → a CatalogEntityRelationshipV1 (physical realization). The
    endpoint→entity resolution + binding to a global relationship_id is the caller's job in 3B; here we
    prove the CONTRACT can carry the join's physical facts."""
    return CatalogEntityRelationshipV1(
        realization_id=f"{catalog_source}:{edge.from_ref}->{edge.to_ref}",
        relationship_id=relationship_id, catalog_source=catalog_source,
        from_object_ref=edge.from_ref, to_object_ref=edge.to_ref,
        declared_cardinality=_CARDINALITY[edge.cardinality or "N:1"], adapter_id=adapter_id,
        authority=GraphEdgeAuthority.CATALOG_DECLARED.value, status=RelationshipStatus.ACTIVE)


def bridge_v1_from_entity_bridge(
    bridge: _EntityBridge, *, left_catalog: str, right_catalog: str, bridge_id: str,
) -> EntityBridgeV1:
    return EntityBridgeV1(
        bridge_id=bridge_id, entity_id=bridge.entity, left_catalog_source=left_catalog,
        left_object_ref=bridge.from_ref, right_catalog_source=right_catalog,
        right_object_ref=bridge.to_ref, authority=GraphEdgeAuthority.ENTITY_BRIDGE.value,
        status=RelationshipStatus.ACTIVE)


def test_representative_join_edges_map_to_catalog_realizations():
    cases = [
        _JoinEdge("transactions.account_id", "accounts.account_id", "N:1", True),
        _JoinEdge("accounts.customer_id", "customer_master.customer_id", "N:1", True),
        _JoinEdge("facilities.borrower_id", "borrowers.borrower_id", "N:1", True),
    ]
    for i, edge in enumerate(cases):
        real = catalog_relationship_from_join_edge(
            edge, catalog_source="core", relationship_id=f"rel_{i}", adapter_id="core_adapter")
        assert real.from_object_ref == edge.from_ref
        assert real.declared_cardinality is Cardinality.MANY_TO_ONE
        assert real.authority == "catalog_declared"


def test_existing_bridge_shape_maps_to_bridge_v1():
    b = _EntityBridge(entity="account", from_ref="transactions.account_id",
                      to_ref="accounts.account_id")
    v1 = bridge_v1_from_entity_bridge(b, left_catalog="payments", right_catalog="core", bridge_id="b1")
    assert v1.entity_id == "account"
    assert v1.left_object_ref == "transactions.account_id"
    assert v1.right_object_ref == "accounts.account_id"
    assert v1.authority == "entity_bridge"
```

- [ ] **Step 2: Run to verify it passes**

Run: `uv run pytest tests/featuregen/overlay/upload/taxonomy/test_entity_contract_feasibility.py -q`
Expected: PASS (2 tests). (These exercise Task-1 contracts only; they should pass immediately.)

- [ ] **Step 3: Confirm no production surface changed**

Run: `git status --short` — expected: only the new test file is untracked; no `src/` file modified by this task.

- [ ] **Step 4: Full-suite regression + gates + commit**

```bash
uv run pytest tests/featuregen/overlay/ tests/featuregen/api/ -q          # nothing regressed
uv run ruff check src tests
uv run mypy src/featuregen/overlay/upload/taxonomy/
git add tests/featuregen/overlay/upload/taxonomy/test_entity_contract_feasibility.py
git commit -m "test(3a): contract feasibility spike over real join/bridge shapes (task 3A.6)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Exit criteria mapping (verify before finishing the phase)

| # | Exit criterion (spec) | Where satisfied |
|---|---|---|
| 1 | Every hardcoded relation exists in the registry | Task 2 `test_registry_is_the_five_seed_rollups_and_valid` |
| 2 | Every current compatibility fixture returns the same result | Task 5 regression + existing `test_ranking_signals.py` green |
| 3 | Ranking + grain-warning responses unchanged | Task 5 Step 4 (`test_contract_ranked.py` green) |
| 4 | Old map removed, not a fallback | Task 5 Step 3(c) deletes `_ENTITY_ROLLUP`/`_rolls_up_to` |
| 5 | Traversal cycle-safe + deterministic | Task 3 `test_graph_tolerates_a_cycle`, Task 4 `test_traversal_is_cycle_safe`, sorted adjacency |
| 6 | AMBIGUOUS synthetic-only, never from the seed | Task 4 `test_seed_graph_never_emits_ambiguous` + `test_ambiguous_on_synthetic_two_path_graph` |
| 7 | Aggregation metadata preserved in paths | Task 4 `test_derivable_single_direct_path` (asserts `aggregation_required`) |
| 8 | Graph version stable + observable | Task 4 (`result.graph_version`), `GRAPH_VERSION` |
| 9 | Catalog joins representable by `CatalogEntityRelationshipV1` | Task 6 `test_representative_join_edges_map_to_catalog_realizations` |
| 10 | Bridge data representable by `EntityBridgeV1` | Task 6 `test_existing_bridge_shape_maps_to_bridge_v1` |
| 11 | Realizations + bridges NOT active traversal inputs | Task 3 builder ignores them; Task 6 transforms live in tests only |
| 12 | No migration / governance UI | No `db/migrations/*` created anywhere in this plan |

## Self-review notes

- **Behaviour-neutrality is proven two ways:** the direct resolver-vs-old-map regression (Task 5) AND the untouched existing `test_ranking_signals.py` + `test_contract_ranked.py` suites staying green.
- **`AMBIGUOUS` additive-safety:** `entity_compatibility` feeds only the ranker's reason stream (`is UNKNOWN`) and `signal_warnings` (`is DERIVABLE`) — no exhaustive `match`, so the new enum member needs no downstream arm; the seed never emits it.
- **No import cycle:** `EntityCompatibility` lives in `entity_relationships`; `entity_graph` imports it; `ranking_signals` imports from `entity_graph`/`entity_relationships` and re-exports the enum for its existing callers.

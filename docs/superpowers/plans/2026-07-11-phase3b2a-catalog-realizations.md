# Phase 3B.2A — Catalog Realization Derivation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Derive, deterministically from a catalog's declared joins, the *physical realizations* of the global entity relationships — each carrying the object-grain hop it realizes (not the join-key entity), an authority stamp, and a cardinality-conflict / unmapped-local disposition — so the (later 3B.3) cross-catalog planner can traverse real catalog metadata.

**Architecture:** A pure derivation over the existing `graph_node`/`graph_edge` catalog tables (no new table, no migration). The load-bearing distinction: a join `accounts.customer_id → customer_master.customer_id` has both endpoint columns at entity `customer` (the **join key**), but realizes the semantic hop `account → customer` (the **object grains** — each = the `entity_link` of its table's `is_grain` column). The 3A-defined-but-inactive `CatalogEntityRelationshipV1` contract is extended to carry both. Behaviour-neutral: nothing consumes realizations until 3B.3.

**Tech Stack:** Python 3.11, `@dataclass(frozen=True, slots=True)`, `StrEnum`, Postgres (read-only over `graph_node`/`graph_edge`), `uv run pytest`/`ruff`/`mypy`.

## Global Constraints

- **Behaviour-neutral, no flag.** Nothing consumes realizations until the 3B.3 planner. The existing grounding path is untouched; the overlay + api suites stay green.
- **Object grain ≠ join-key entity ≠ required grain.** The object grain of a table = the `entity_link` of the concept of its `is_grain` column (governed, lowercase). The join-key entity = the `entity_link` of the *join column's* concept. NEVER treat a join-key column's entity as the object grain.
- **Authority is stamped, never assumed.** `RealizationAuthority ∈ {approved_join, declared_join, inferred_join}`. Existing single-catalog grounding legitimately uses **declared** joins; 3B.2A stamps `DECLARED_JOIN` and does NOT change what's VALID-capable (that enforcement is 3C).
- **Fail closed on conflict.** A join's declared cardinality contradicting the global relationship's → **`RELATIONSHIP_CONFLICT`**, surfaced, **never silently overridden**.
- **Unmapped grain pair → catalog-local + proposal.** A join whose `(from_object_grain, to_object_grain)` has no global relationship is a `catalog_local_relationship`: intra-catalog-only, **not** cross-catalog-traversable, plus an `EntityRelationshipProposalV1`.
- **Reads existing tables, NO new table / NO DB migration.** Realizations are derived on demand from `graph_node`/`graph_edge`. A composite immutable fingerprint (catalog schema fingerprint + global-graph fingerprint + concept-registry version + realization-derivation version) is *computed and attached* for replay/caching — persistence is deferred to 3B.3.
- **LLM proposes / deterministic code disposes** — pure code over declared metadata. **Declared-metadata-only** — no row-level join quality.
- Commit messages end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Branch `feature/phase3b2a-catalog-realizations` is already checked out.

## Codebase facts (verified)

- `graph_node(catalog_source, object_ref, kind ['table'|'column'], table_name, column_name, is_grain, is_as_of, concept, entity, ...)`; `object_ref` = `public.<table>.<column>` (column) or `public.<table>` (table); `_SCHEMA = "public"`.
- `graph_edge(catalog_source, kind, from_ref, to_ref, cardinality)`; join edges are `kind='joins'`, `from_ref`/`to_ref` are **column** object_refs, `cardinality ∈ {"N:1","1:1","1:N"}`.
- `from featuregen.overlay.upload.concepts import concept` → `Concept | None`; `Concept.entity_link: str | None` is the governed (lowercase) entity.
- 3A contracts (`taxonomy/entity_relationships.py`): `Cardinality` (`one_to_one`/`one_to_many`/`many_to_one`/`many_to_many`), `RelationshipStatus`, `EntityRelationshipProposalV1`, `validate_relationship_proposal`; `CatalogEntityRelationshipV1` is defined-but-**inactive** (nothing populates it — safe to change its fields). `entity_registry.ENTITY_RELATIONSHIPS_V1` holds the 5 global roll-ups (`many_to_one`). `dimensions.known_entities()` is the closed vocabulary.

## File Structure

- **Modify** `src/featuregen/overlay/upload/taxonomy/entity_relationships.py` — extend `CatalogEntityRelationshipV1` (object grains + key entities, drop `resolved_from/to_entity` + `adapter_id`), add `RealizationAuthority`, update `validate_catalog_relationship`. [Task 1]
- **Modify** `tests/featuregen/overlay/upload/taxonomy/test_entity_relationships.py` + `tests/featuregen/overlay/upload/taxonomy/test_entity_contract_feasibility.py` — update the constructions to the new contract. [Task 1]
- **Modify** `src/featuregen/overlay/upload/taxonomy/entity_registry.py` — add `global_relationship_for(from_entity, to_entity) -> EntityRelationshipDefinitionV1 | None`. [Task 2]
- **Create** `src/featuregen/overlay/upload/catalog_realizations.py` — `CARDINALITY_TOKENS`, `normalize_realization`, the endpoint-resolution helpers, `derive_catalog_realizations`, `CatalogRealizationResult`, `realization_fingerprint`. [Tasks 2, 3, 4]
- **Create** tests `tests/featuregen/overlay/upload/test_catalog_realizations.py`. [Tasks 2, 3, 4]

Import DAG: `entity_relationships`/`entity_registry`/`concepts`/`graph` ← `catalog_realizations`. No cycles.

---

### Task 1: Extend the realization contract + `RealizationAuthority`

**Files:**
- Modify: `src/featuregen/overlay/upload/taxonomy/entity_relationships.py`
- Test: `tests/featuregen/overlay/upload/taxonomy/test_entity_relationships.py`, `tests/featuregen/overlay/upload/taxonomy/test_entity_contract_feasibility.py`

**Interfaces:**
- Produces: `RealizationAuthority` (`APPROVED_JOIN`/`DECLARED_JOIN`/`INFERRED_JOIN`); the extended `CatalogEntityRelationshipV1` (fields below); `validate_catalog_relationship(real, *, known)`.

- [ ] **Step 1: Write the failing tests** — replace the existing `CatalogEntityRelationshipV1` construction + validation test in `test_entity_relationships.py`. Find the `_catalog(...)` helper and `test_catalog_relationship_validation` and replace them with:

```python
def _catalog(**overrides) -> CatalogEntityRelationshipV1:
    base = dict(
        realization_id="core:public.accounts->public.customer_master",
        relationship_id="account_to_customer", catalog_source="core",
        from_object_ref="public.accounts", from_object_grain="account",
        to_object_ref="public.customer_master", to_object_grain="customer",
        from_key_ref="public.accounts.customer_id", from_key_entity="customer",
        to_key_ref="public.customer_master.customer_id", to_key_entity="customer",
        declared_cardinality=Cardinality.MANY_TO_ONE,
        authority=RealizationAuthority.DECLARED_JOIN, status=RelationshipStatus.ACTIVE)
    base.update(overrides)
    return CatalogEntityRelationshipV1(**base)


def test_catalog_relationship_validation():
    validate_catalog_relationship(_catalog(), known=KNOWN)
    with pytest.raises(ValueError, match="empty"):
        validate_catalog_relationship(_catalog(catalog_source=""), known=KNOWN)
    with pytest.raises(ValueError, match="empty"):
        validate_catalog_relationship(_catalog(from_key_ref="  "), known=KNOWN)
    with pytest.raises(ValueError, match="unknown entity"):
        validate_catalog_relationship(_catalog(to_object_grain="not_an_entity"), known=KNOWN)
    with pytest.raises(ValueError, match="unknown entity"):
        validate_catalog_relationship(_catalog(from_key_entity="not_an_entity"), known=KNOWN)
    with pytest.raises(ValueError, match="identical"):
        validate_catalog_relationship(_catalog(to_object_ref="public.accounts"), known=KNOWN)
```

Add `RealizationAuthority` to the import block of that test file.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/taxonomy/test_entity_relationships.py -q`
Expected: FAIL — `ImportError: cannot import name 'RealizationAuthority'` / `TypeError` on the new fields.

- [ ] **Step 3: Modify `entity_relationships.py`**

(a) Add the enum (near `GraphEdgeAuthority`):

```python
class RealizationAuthority(StrEnum):
    """The authority behind a catalog realization. ``APPROVED_JOIN`` = an attested approved_join fact;
    ``DECLARED_JOIN`` = an uploaded ``graph_edge`` join (what existing single-catalog grounding uses);
    ``INFERRED_JOIN`` = metadata-inferred. Stamped in 3B; which levels are VALID-capable is enforced in
    3C — 3B never blocks on it."""

    APPROVED_JOIN = "approved_join"
    DECLARED_JOIN = "declared_join"
    INFERRED_JOIN = "inferred_join"
```

(b) Replace the `CatalogEntityRelationshipV1` dataclass with the object-grain-aware contract:

```python
@dataclass(frozen=True, slots=True)
class CatalogEntityRelationshipV1:
    """How one catalog physically realizes a global relationship. The semantic hop it realizes is
    ``from_object_grain -> to_object_grain`` (each = the entity of its table's is_grain column), realized
    by the join KEY (``from_key_ref``/``to_key_ref`` + their entities). Object grain and join-key entity
    are DISTINCT (a join on ``customer_id`` can realize ``account -> customer``). Derived from declared
    joins in Phase 3B.2A; nothing populated it in 3A (safe to extend)."""

    realization_id: str
    relationship_id: str
    catalog_source: str
    from_object_ref: str
    from_object_grain: str
    to_object_ref: str
    to_object_grain: str
    from_key_ref: str
    from_key_entity: str
    to_key_ref: str
    to_key_entity: str
    declared_cardinality: Cardinality
    authority: RealizationAuthority = RealizationAuthority.DECLARED_JOIN
    status: RelationshipStatus = RelationshipStatus.ACTIVE
```

(c) Replace `validate_catalog_relationship` with:

```python
def validate_catalog_relationship(real: CatalogEntityRelationshipV1, *, known: frozenset[str]) -> None:
    """Structural guard: non-empty refs, distinct object endpoints, and every resolved entity (both
    object grains + both key entities) in the closed vocabulary. It does NOT cross-check the realization
    against the global relationship (that is the derivation's job in 3B.2A)."""
    _nonempty(realization_id=real.realization_id, relationship_id=real.relationship_id,
              catalog_source=real.catalog_source, from_object_ref=real.from_object_ref,
              to_object_ref=real.to_object_ref, from_key_ref=real.from_key_ref,
              to_key_ref=real.to_key_ref)
    if real.from_object_ref == real.to_object_ref:
        raise ValueError("catalog realization object endpoints are identical")
    for label, ent in (("from_object_grain", real.from_object_grain),
                       ("to_object_grain", real.to_object_grain),
                       ("from_key_entity", real.from_key_entity),
                       ("to_key_entity", real.to_key_entity)):
        if ent not in known:
            raise ValueError(f"unknown entity ({label}): {ent!r}")
```

- [ ] **Step 4: Fix the 3A feasibility spike test**

`tests/featuregen/overlay/upload/taxonomy/test_entity_contract_feasibility.py` constructs `CatalogEntityRelationshipV1` with the OLD fields via `catalog_relationship_from_join_edge`. That in-test transform is SUPERSEDED by the real derivation this plan builds (Task 4). Delete `catalog_relationship_from_join_edge` and its test `test_real_join_edges_map_to_valid_catalog_realizations` from that file (leave the `EntityBridgeV1` half — that's 3B.2B). Add a one-line module note: "catalog-realization derivation moved to production in Phase 3B.2A (`catalog_realizations.py`)."

- [ ] **Step 5: Run tests + gates + commit**

```bash
uv run pytest tests/featuregen/overlay/upload/taxonomy/test_entity_relationships.py tests/featuregen/overlay/upload/taxonomy/test_entity_contract_feasibility.py -q
uv run ruff check src/featuregen/overlay/upload/taxonomy/entity_relationships.py tests/featuregen/overlay/upload/taxonomy/test_entity_relationships.py tests/featuregen/overlay/upload/taxonomy/test_entity_contract_feasibility.py
uv run mypy src/featuregen/overlay/upload/taxonomy/entity_relationships.py
git add -A && git commit -m "feat(3b2a): object-grain-aware realization contract + RealizationAuthority (task 1)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Cardinality mapping + orientation normalization + global lookup

**Files:**
- Modify: `src/featuregen/overlay/upload/taxonomy/entity_registry.py` (add `global_relationship_for`)
- Create: `src/featuregen/overlay/upload/catalog_realizations.py`
- Test: `tests/featuregen/overlay/upload/test_catalog_realizations.py`

**Interfaces:**
- Consumes: `CatalogEntityRelationshipV1`, `RealizationAuthority`, `Cardinality`, `RelationshipStatus` (Task 1); `EntityRelationshipDefinitionV1`, `ENTITY_RELATIONSHIPS_V1`.
- Produces: `entity_registry.global_relationship_for(from_entity, to_entity) -> EntityRelationshipDefinitionV1 | None`; `catalog_realizations.CARDINALITY_TOKENS`, `cardinality_from_token(token) -> Cardinality`, `invert_cardinality(c) -> Cardinality`, `NormalizedRealization` (namedtuple/dataclass), `normalize_realization(...)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/featuregen/overlay/upload/test_catalog_realizations.py`:

```python
"""Phase-3B.2A — deterministic catalog-realization derivation over declared joins."""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.catalog_realizations import (
    NormalizedRealization,
    cardinality_from_token,
    invert_cardinality,
    normalize_realization,
)
from featuregen.overlay.upload.taxonomy.entity_registry import global_relationship_for
from featuregen.overlay.upload.taxonomy.entity_relationships import Cardinality


def test_cardinality_token_mapping():
    assert cardinality_from_token("N:1") is Cardinality.MANY_TO_ONE
    assert cardinality_from_token("1:N") is Cardinality.ONE_TO_MANY
    assert cardinality_from_token("1:1") is Cardinality.ONE_TO_ONE
    assert cardinality_from_token(None) is Cardinality.MANY_TO_ONE   # unstated -> the common FK default
    with pytest.raises(ValueError, match="unknown cardinality"):
        cardinality_from_token("weird")


def test_invert_cardinality():
    assert invert_cardinality(Cardinality.MANY_TO_ONE) is Cardinality.ONE_TO_MANY
    assert invert_cardinality(Cardinality.ONE_TO_MANY) is Cardinality.MANY_TO_ONE
    assert invert_cardinality(Cardinality.ONE_TO_ONE) is Cardinality.ONE_TO_ONE


def test_global_relationship_lookup():
    rel = global_relationship_for("account", "customer")
    assert rel is not None and rel.relationship_id == "account_to_customer"
    assert global_relationship_for("customer", "account") is None      # not a declared global direction


def test_normalize_forward_orientation_binds():
    # account-grain -> customer-grain join, declared N:1, matches global account->customer (many_to_one)
    rel = global_relationship_for("account", "customer")
    out = normalize_realization(
        from_object_grain="account", to_object_grain="customer",
        declared=Cardinality.MANY_TO_ONE, global_rel=rel)
    assert out == NormalizedRealization(
        relationship_id="account_to_customer", declared_cardinality=Cardinality.MANY_TO_ONE,
        conflict=False, reversed_authoring=False)


def test_normalize_reverse_orientation_inverts_cardinality():
    # the SAME account->customer relationship, but the join was authored customer-grain -> account-grain
    # with 1:N; normalization detects the reverse orientation and inverts the cardinality to compare.
    rel = global_relationship_for("account", "customer")
    out = normalize_realization(
        from_object_grain="customer", to_object_grain="account",
        declared=Cardinality.ONE_TO_MANY, global_rel=rel)
    assert out.relationship_id == "account_to_customer" and out.reversed_authoring is True
    assert out.conflict is False


def test_normalize_cardinality_conflict_fails_closed():
    # account->customer global is many_to_one; a join declaring many_to_many contradicts it
    rel = global_relationship_for("account", "customer")
    out = normalize_realization(
        from_object_grain="account", to_object_grain="customer",
        declared=Cardinality.MANY_TO_MANY, global_rel=rel)
    assert out.conflict is True and out.relationship_id == "account_to_customer"


def test_normalize_no_global_relationship_is_local():
    out = normalize_realization(
        from_object_grain="account", to_object_grain="account",
        declared=Cardinality.ONE_TO_ONE, global_rel=None)
    assert out is None      # unmapped grain pair -> caller records a catalog_local_relationship + proposal
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/test_catalog_realizations.py -q`
Expected: FAIL — `ModuleNotFoundError: ... catalog_realizations` / `ImportError: global_relationship_for`.

- [ ] **Step 3: Implement the lookup + normalization**

(a) Append to `entity_registry.py`:

```python
def global_relationship_for(
    from_entity: str, to_entity: str) -> EntityRelationshipDefinitionV1 | None:
    """The active global relationship for a directed grain pair, or None. Directed: ``account->customer``
    is a relationship; ``customer->account`` is not (the reverse must be handled by the caller)."""
    for d in ENTITY_RELATIONSHIPS_V1:
        if d.status is RelationshipStatus.ACTIVE \
                and d.from_entity == from_entity and d.to_entity == to_entity:
            return d
    return None
```

(add `RelationshipStatus` to `entity_registry.py`'s imports if not present.)

(b) Create `src/featuregen/overlay/upload/catalog_realizations.py`:

```python
"""Phase-3B.2A — derive a catalog's physical realizations of the global entity relationships from its
declared joins. Pure, deterministic, read-only over ``graph_node``/``graph_edge``. The semantic hop a
join realizes is its OBJECT-GRAIN pair (each = the entity of the table's is_grain column), NOT the
join-key entity. Behaviour-neutral: nothing consumes this until the 3B.3 planner."""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.overlay.upload.taxonomy.entity_relationships import (
    Cardinality,
    EntityRelationshipDefinitionV1,
)

REALIZATION_DERIVATION_VERSION = "1.0.0"

# The upload cardinality tokens (canonical.py) -> the governed Cardinality. Unstated -> MANY_TO_ONE
# (the overwhelmingly common FK direction). N:N is not a valid upload token.
CARDINALITY_TOKENS: dict[str, Cardinality] = {
    "N:1": Cardinality.MANY_TO_ONE,
    "1:N": Cardinality.ONE_TO_MANY,
    "1:1": Cardinality.ONE_TO_ONE,
}


def cardinality_from_token(token: str | None) -> Cardinality:
    if token is None or token == "":
        return Cardinality.MANY_TO_ONE
    try:
        return CARDINALITY_TOKENS[token]
    except KeyError:
        raise ValueError(f"unknown cardinality token: {token!r}") from None


def invert_cardinality(c: Cardinality) -> Cardinality:
    """The cardinality read from the opposite direction (endpoints swapped)."""
    if c is Cardinality.MANY_TO_ONE:
        return Cardinality.ONE_TO_MANY
    if c is Cardinality.ONE_TO_MANY:
        return Cardinality.MANY_TO_ONE
    return c   # one_to_one and many_to_many are symmetric


@dataclass(frozen=True, slots=True)
class NormalizedRealization:
    """The result of orienting a declared join against a global relationship: the bound relationship id,
    the declared cardinality (inverted if the join was authored in reverse), whether it was reverse-
    authored, and whether the cardinality conflicts with the global model."""
    relationship_id: str
    declared_cardinality: Cardinality
    conflict: bool
    reversed_authoring: bool


def normalize_realization(
    *, from_object_grain: str, to_object_grain: str, declared: Cardinality,
    global_rel: EntityRelationshipDefinitionV1 | None) -> NormalizedRealization | None:
    """Orient a declared join (grains ``from -> to``, cardinality ``declared``) against ``global_rel``.
    Returns None when there is no global relationship (caller records a catalog_local_relationship +
    proposal). Otherwise binds the relationship and reports whether the join was reverse-authored (so its
    cardinality is inverted to compare) and whether the (oriented) cardinality CONFLICTS with the global
    model (fail closed — surfaced, never silently overridden)."""
    if global_rel is None:
        return None
    if (from_object_grain, to_object_grain) == (global_rel.from_entity, global_rel.to_entity):
        oriented, reversed_ = declared, False
    else:
        # reverse orientation: the join was authored to->from; invert its cardinality to compare
        oriented, reversed_ = invert_cardinality(declared), True
    return NormalizedRealization(
        relationship_id=global_rel.relationship_id, declared_cardinality=oriented,
        conflict=oriented is not global_rel.cardinality, reversed_authoring=reversed_)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/featuregen/overlay/upload/test_catalog_realizations.py -q`
Expected: PASS (7 tests).

- [ ] **Step 5: Gates + commit**

```bash
uv run ruff check src/featuregen/overlay/upload/catalog_realizations.py src/featuregen/overlay/upload/taxonomy/entity_registry.py tests/featuregen/overlay/upload/test_catalog_realizations.py
uv run mypy src/featuregen/overlay/upload/catalog_realizations.py src/featuregen/overlay/upload/taxonomy/entity_registry.py
git add -A && git commit -m "feat(3b2a): cardinality mapping + orientation normalization + global lookup (task 2)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Endpoint resolution (object grain + join-key entity) from the catalog graph

**Files:**
- Modify: `src/featuregen/overlay/upload/catalog_realizations.py`
- Test: `tests/featuregen/overlay/upload/test_catalog_realizations.py`

**Interfaces:**
- Consumes: `concept` (`concepts.py`); `graph_node`.
- Produces: `object_grain(conn, catalog_source, table_object_ref) -> str | None` (the entity of the table's is_grain column's concept); `key_entity(conn, catalog_source, column_object_ref) -> str | None` (the join-key column's `concept.entity_link`); `table_of(column_object_ref) -> str` (strip the column segment).

- [ ] **Step 1: Write the failing tests** (DB-backed — append to `test_catalog_realizations.py`)

```python
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.catalog_realizations import (
    key_entity,
    object_grain,
    table_of,
)
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph


def _accounts_customer_catalog(conn) -> None:
    # accounts: grain = account (account_id is_grain), plus a customer_id FK column; customer_master:
    # grain = customer. A join accounts.customer_id -> customer_master.customer_id (N:1).
    catalog = [
        (CanonicalRow("core", "accounts", "account_id", "integer", is_grain=True), "account_id"),
        (CanonicalRow("core", "accounts", "customer_id", "integer",
                      joins_to="customer_master.customer_id", cardinality="N:1"), "customer_id"),
        (CanonicalRow("core", "customer_master", "customer_id", "integer", is_grain=True), "customer_id"),
        (CanonicalRow("core", "customer_master", "segment", "text"), "categorical"),
    ]
    rows = [r for r, _ in catalog]
    concepts = {content_hash(r): c for r, c in catalog}
    build_graph(conn, "core", rows, concepts=concepts)


def test_table_of_strips_column():
    assert table_of("public.accounts.customer_id") == "public.accounts"


def test_object_grain_is_the_grain_column_entity(db):
    _accounts_customer_catalog(db)
    # accounts' grain column is account_id -> entity account; customer_master's is customer_id -> customer
    assert object_grain(db, "core", "public.accounts") == "account"
    assert object_grain(db, "core", "public.customer_master") == "customer"


def test_key_entity_is_the_join_column_concept_entity(db):
    _accounts_customer_catalog(db)
    # the join key column accounts.customer_id has concept customer_id -> entity customer (NOT account)
    assert key_entity(db, "core", "public.accounts.customer_id") == "customer"


def test_object_grain_none_when_no_grain_column(db):
    from featuregen.overlay.upload.canonical import CanonicalRow as CR
    rows = [CR("x", "t", "c", "text")]
    build_graph(db, "x", rows, concepts={content_hash(rows[0]): "categorical"})
    assert object_grain(db, "x", "public.t") is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/test_catalog_realizations.py -q`
Expected: FAIL — `ImportError: cannot import name 'object_grain'/'key_entity'/'table_of'`.

- [ ] **Step 3: Implement the resolution helpers** (append to `catalog_realizations.py`)

```python
from featuregen.overlay.upload.concepts import concept

_SCHEMA = "public"


def table_of(column_object_ref: str) -> str:
    """The table object_ref of a column object_ref: ``public.accounts.customer_id`` -> ``public.accounts``."""
    return column_object_ref.rsplit(".", 1)[0]


def _entity_of_concept(concept_name: str | None) -> str | None:
    if not concept_name:
        return None
    c = concept(concept_name)
    return c.entity_link if c is not None else None


def object_grain(conn, catalog_source: str, table_object_ref: str) -> str | None:
    """The OBJECT GRAIN of a table: the ``entity_link`` of the concept of the table's ``is_grain`` column.
    ``None`` when the table has no grain column or its grain concept links no entity. This is the table's
    grain — NOT a join-key column's entity."""
    row = conn.execute(
        "SELECT concept FROM graph_node WHERE catalog_source = %s AND kind = 'column' "
        "AND table_name = %s AND is_grain = true "
        "AND object_ref LIKE %s ORDER BY object_ref LIMIT 1",
        (catalog_source, table_object_ref.rsplit(".", 1)[-1], table_object_ref + ".%")).fetchone()
    return _entity_of_concept(row[0]) if row is not None else None


def key_entity(conn, catalog_source: str, column_object_ref: str) -> str | None:
    """The join-KEY entity of a column: its concept's ``entity_link`` (governed). ``None`` when the
    column has no concept or its concept links no entity."""
    row = conn.execute(
        "SELECT concept FROM graph_node WHERE catalog_source = %s AND object_ref = %s AND kind = 'column'",
        (catalog_source, column_object_ref)).fetchone()
    return _entity_of_concept(row[0]) if row is not None else None
```

(Note: `object_grain` matches the grain column both by `table_name` and by an `object_ref LIKE table.%` guard so a table name that is a prefix of another cannot cross-match; `_SCHEMA` is retained for symmetry with `graph.py` even though `table_of` derives the table ref directly.)

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/featuregen/overlay/upload/test_catalog_realizations.py -q`
Expected: PASS (all Task 2 + 4 new). If `object_grain` returns None where a grain is expected, check the catalog fixture actually sets `is_grain=True` on the grain column and that its concept has an `entity_link`.

- [ ] **Step 5: Gates + commit**

```bash
uv run ruff check src/featuregen/overlay/upload/catalog_realizations.py tests/featuregen/overlay/upload/test_catalog_realizations.py
uv run mypy src/featuregen/overlay/upload/catalog_realizations.py
git add -A && git commit -m "feat(3b2a): object-grain + join-key-entity resolution from the catalog graph (task 3)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: The realization derivation + fingerprint

**Files:**
- Modify: `src/featuregen/overlay/upload/catalog_realizations.py`
- Test: `tests/featuregen/overlay/upload/test_catalog_realizations.py`

**Interfaces:**
- Consumes: everything above; `EntityRelationshipProposalV1`, `RelationshipProposalStatus`, `GRAPH_VERSION` (`entity_registry.py`). The `realization_id`/`proposal_id` are deterministic (`{catalog_source}:{from_key}->{to_key}`) — no id minting, so the derivation is pure + replay-stable.
- Produces: `CatalogRealizationResult` (buckets: `realizations`, `conflicts`, `local_relationships`, `proposals`, `fingerprint`); `derive_catalog_realizations(conn, catalog_source) -> CatalogRealizationResult`; `realization_fingerprint(conn, catalog_source) -> str`.

- [ ] **Step 1: Write the failing tests** (append to `test_catalog_realizations.py`)

```python
from featuregen.overlay.upload.catalog_realizations import (
    REALIZATION_DERIVATION_VERSION,
    derive_catalog_realizations,
)
from featuregen.overlay.upload.taxonomy.entity_relationships import RealizationAuthority


def test_derive_binds_the_accounts_customer_realization(db):
    _accounts_customer_catalog(db)
    result = derive_catalog_realizations(db, "core")
    # the account->customer hop is realized by the customer_id join key
    assert len(result.realizations) == 1
    r = result.realizations[0]
    assert r.relationship_id == "account_to_customer"
    assert (r.from_object_grain, r.to_object_grain) == ("account", "customer")   # object grains
    assert (r.from_key_entity, r.to_key_entity) == ("customer", "customer")      # join-KEY entity
    assert r.from_object_ref == "public.accounts" and r.to_object_ref == "public.customer_master"
    assert r.declared_cardinality is Cardinality.MANY_TO_ONE
    assert r.authority is RealizationAuthority.DECLARED_JOIN
    assert result.conflicts == () and result.local_relationships == ()


def test_cardinality_conflict_is_surfaced_not_bound(db):
    # same catalog but the join declares 1:1 (contradicts global account->customer many_to_one)
    catalog = [
        (CanonicalRow("c2", "accounts", "account_id", "integer", is_grain=True), "account_id"),
        (CanonicalRow("c2", "accounts", "customer_id", "integer",
                      joins_to="customer_master.customer_id", cardinality="1:1"), "customer_id"),
        (CanonicalRow("c2", "customer_master", "customer_id", "integer", is_grain=True), "customer_id"),
    ]
    rows = [r for r, _ in catalog]
    build_graph(db, "c2", rows, concepts={content_hash(r): c for r, c in catalog})
    result = derive_catalog_realizations(db, "c2")
    assert result.realizations == ()                       # NOT bound as valid
    assert len(result.conflicts) == 1
    assert result.conflicts[0].relationship_id == "account_to_customer"


def test_unmapped_grain_pair_is_local_plus_proposal(db):
    # a join whose grain pair has NO global relationship -> catalog_local + a proposal
    catalog = [
        (CanonicalRow("c3", "widgets", "widget_id", "integer", is_grain=True), "product_id"),
        (CanonicalRow("c3", "widgets", "merchant_id", "integer",
                      joins_to="merchants.merchant_id", cardinality="N:1"), "merchant_id"),
        (CanonicalRow("c3", "merchants", "merchant_id", "integer", is_grain=True), "merchant_id"),
    ]
    rows = [r for r, _ in catalog]
    build_graph(db, "c3", rows, concepts={content_hash(r): c for r, c in catalog})
    result = derive_catalog_realizations(db, "c3")
    assert result.realizations == () and result.conflicts == ()
    assert len(result.local_relationships) == 1 and len(result.proposals) == 1
    assert result.proposals[0].proposed_from_entity == "product"      # widgets grain
    assert result.proposals[0].proposed_to_entity == "merchant"


def test_fingerprint_is_stable_and_composite(db):
    _accounts_customer_catalog(db)
    fp1 = derive_catalog_realizations(db, "core").fingerprint
    fp2 = derive_catalog_realizations(db, "core").fingerprint
    assert fp1 == fp2 and len(fp1) == 64                              # sha256 hex, deterministic
    assert REALIZATION_DERIVATION_VERSION == "1.0.0"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/test_catalog_realizations.py -q`
Expected: FAIL — `ImportError: cannot import name 'derive_catalog_realizations'`.

- [ ] **Step 3: Implement the derivation** (append to `catalog_realizations.py`)

```python
import hashlib
import json

from featuregen.overlay.upload.taxonomy.entity_registry import (
    GRAPH_VERSION,
    global_relationship_for,
)
from featuregen.overlay.upload.taxonomy.entity_relationships import (
    CatalogEntityRelationshipV1,
    EntityRelationshipProposalV1,
    RealizationAuthority,
    RelationshipProposalStatus,
    RelationshipStatus,
)

CONCEPT_REGISTRY_FOR_REALIZATION = "concepts@1"   # a version tag for the concept vocabulary


@dataclass(frozen=True, slots=True)
class CatalogRealizationResult:
    catalog_source: str
    realizations: tuple[CatalogEntityRelationshipV1, ...]        # bound to a global relationship, VALID
    conflicts: tuple[CatalogEntityRelationshipV1, ...]           # cardinality conflict (fail-closed)
    local_relationships: tuple[CatalogEntityRelationshipV1, ...]  # unmapped grain pair, intra-catalog-only
    proposals: tuple[EntityRelationshipProposalV1, ...]          # governance proposals for the unmapped
    fingerprint: str


def _catalog_schema_fingerprint(conn, catalog_source: str) -> str:
    """A deterministic hash of the catalog's schema-relevant metadata (columns + grain/entity/concept +
    join edges) — so the derivation's cache key changes iff the catalog's declared structure changes."""
    nodes = conn.execute(
        "SELECT object_ref, kind, table_name, is_grain, concept FROM graph_node "
        "WHERE catalog_source = %s ORDER BY object_ref", (catalog_source,)).fetchall()
    edges = conn.execute(
        "SELECT from_ref, to_ref, cardinality FROM graph_edge "
        "WHERE catalog_source = %s AND kind = 'joins' ORDER BY from_ref, to_ref", (catalog_source,)).fetchall()
    blob = json.dumps({"nodes": [list(n) for n in nodes], "edges": [list(e) for e in edges]},
                      sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def realization_fingerprint(conn, catalog_source: str) -> str:
    """The composite immutable key — catalog schema fingerprint + global-graph version + concept-registry
    version + derivation version — NOT the mutable catalog_source name alone."""
    parts = "|".join((_catalog_schema_fingerprint(conn, catalog_source), GRAPH_VERSION,
                      CONCEPT_REGISTRY_FOR_REALIZATION, REALIZATION_DERIVATION_VERSION))
    return hashlib.sha256(parts.encode()).hexdigest()


def _join_edges(conn, catalog_source: str):
    """Intra-catalog declared join edges (both endpoints in THIS catalog — a cross-source target is a
    3B.2B bridge concern, not a realization)."""
    return conn.execute(
        "SELECT e.from_ref, e.to_ref, e.cardinality FROM graph_edge e "
        "WHERE e.catalog_source = %s AND e.kind = 'joins' "
        "  AND EXISTS(SELECT 1 FROM graph_node n WHERE n.catalog_source = e.catalog_source "
        "             AND n.object_ref = e.to_ref) "
        "ORDER BY e.from_ref, e.to_ref", (catalog_source,)).fetchall()


def derive_catalog_realizations(conn, catalog_source: str) -> CatalogRealizationResult:
    """Derive this catalog's physical realizations from its declared joins. Deterministic, read-only.
    Each intra-catalog join whose object-grain pair matches a global relationship becomes a bound
    realization (a cardinality contradiction -> a conflict bucket); an unmapped grain pair -> a
    catalog-local relationship + a governance proposal. Object grain = the table's is_grain column
    entity, distinct from the join-key entity."""
    realizations: list[CatalogEntityRelationshipV1] = []
    conflicts: list[CatalogEntityRelationshipV1] = []
    local: list[CatalogEntityRelationshipV1] = []
    proposals: list[EntityRelationshipProposalV1] = []

    for from_key, to_key, card_token in _join_edges(conn, catalog_source):
        from_table, to_table = table_of(from_key), table_of(to_key)
        fg, tg = object_grain(conn, catalog_source, from_table), object_grain(conn, catalog_source, to_table)
        fke, tke = key_entity(conn, catalog_source, from_key), key_entity(conn, catalog_source, to_key)
        if None in (fg, tg, fke, tke):
            continue                                            # unresolvable grain/key -> not derivable
        declared = cardinality_from_token(card_token)
        # try forward, then reverse orientation against the global model
        norm = normalize_realization(from_object_grain=fg, to_object_grain=tg,
                                     declared=declared, global_rel=global_relationship_for(fg, tg)) \
            or normalize_realization(from_object_grain=fg, to_object_grain=tg,
                                     declared=declared, global_rel=global_relationship_for(tg, fg))
        rid = f"{catalog_source}:{from_key}->{to_key}"
        rel = CatalogEntityRelationshipV1(
            realization_id=rid, relationship_id=(norm.relationship_id if norm else ""),
            catalog_source=catalog_source,
            from_object_ref=from_table, from_object_grain=fg, to_object_ref=to_table, to_object_grain=tg,
            from_key_ref=from_key, from_key_entity=fke, to_key_ref=to_key, to_key_entity=tke,
            declared_cardinality=(norm.declared_cardinality if norm else declared),
            authority=RealizationAuthority.DECLARED_JOIN, status=RelationshipStatus.ACTIVE)
        if norm is None:
            local.append(rel)
            proposals.append(EntityRelationshipProposalV1(
                proposal_id=f"prop:{rid}", proposed_from_entity=fg, proposed_to_entity=tg,
                proposed_cardinality=declared, evidence_refs=(from_key, to_key),
                source_catalog=catalog_source, inferred_by="catalog_realization_derivation@1",
                status=RelationshipProposalStatus.PENDING))
        elif norm.conflict:
            conflicts.append(rel)
        else:
            realizations.append(rel)

    return CatalogRealizationResult(
        catalog_source=catalog_source, realizations=tuple(realizations), conflicts=tuple(conflicts),
        local_relationships=tuple(local), proposals=tuple(proposals),
        fingerprint=realization_fingerprint(conn, catalog_source))
```

- [ ] **Step 4: Run the derivation tests + the FULL overlay/api suites (behaviour-neutral proof)**

```bash
uv run pytest tests/featuregen/overlay/upload/test_catalog_realizations.py -q
uv run pytest tests/featuregen/overlay/ tests/featuregen/api/ -q      # nothing regressed; realizations dormant
```
Expected: PASS. The realization derivation is consumed by nothing, so the whole suite is byte-identical.

- [ ] **Step 5: Gates + commit**

```bash
uv run ruff check src/featuregen/overlay/upload/catalog_realizations.py tests/featuregen/overlay/upload/test_catalog_realizations.py
uv run mypy src/featuregen/overlay/upload/catalog_realizations.py
git add -A && git commit -m "feat(3b2a): derive catalog realizations from declared joins (task 4)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Exit criteria mapping

| Spec requirement (3B.2A) | Where satisfied |
|---|---|
| Object grain ≠ join-key entity; realization carries both | Task 1 contract + Task 4 `test_derive_binds...` (grains vs key entities) |
| `RealizationAuthority` (approved/declared/inferred), declared stamped | Task 1 enum + Task 4 `authority is DECLARED_JOIN` |
| Direction/cardinality normalization (forward/reverse, invert, reject) | Task 2 `normalize_realization` + reverse/conflict tests |
| Derive realizations from `graph_edge` joins, bind to a global relationship | Task 4 `derive_catalog_realizations` + `test_derive_binds...` |
| Cardinality conflict → `RELATIONSHIP_CONFLICT` fail-closed | Task 4 `test_cardinality_conflict_is_surfaced_not_bound` |
| Unmapped grain pair → catalog-local + proposal | Task 4 `test_unmapped_grain_pair_is_local_plus_proposal` |
| Composite immutable fingerprint (schema + graph + concept + derivation versions) | Task 4 `realization_fingerprint` + `test_fingerprint_is_stable_and_composite` |
| Behaviour-neutral, no flag, no migration | Task 4 Step 4 (full suite green); no `db/migrations/*` created |

## Self-review notes

- **Behaviour-neutral:** realizations are consumed by nothing until 3B.3; the full overlay+api suite proves it (Task 4).
- **Object-grain vs key-entity is proven** by `test_derive_binds...`: the join is on `customer_id` (key entity customer) but realizes `account → customer` (object grains) — the load-bearing distinction, asserted directly.
- **No migration:** everything reads existing `graph_node`/`graph_edge`; the fingerprint is computed, not stored.
- **Fail-closed conflict** and **unmapped→local+proposal** are distinct buckets, never silently dropped or bound.

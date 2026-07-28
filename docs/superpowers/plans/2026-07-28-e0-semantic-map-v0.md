# E0 — Semantic Map v0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user filter and facet the existing cross-catalog search by business concept, and
make the concept hierarchy safe to traverse.

**Architecture:** No new read model, no new service. `search()` is already cross-catalog, fresh-only
and read-scoped, and already returns `n.concept` in its hit projection; `_COLUMN_FACETS` is a
generic `{facet name: graph_node column}` map that drives filtering, the `IS NULL` bucket and facet
counting. E0 adds one entry to that map and one validation rule to the concept registry. Everything
else is existing machinery.

**Parent:** `2026-07-27-e3-e5-cross-catalog-ontology-program.md`, Phase E0.

**Tech Stack:** Python 3.12, psycopg, pytest; React/TypeScript + vitest for the screen.

## Global Constraints

- **Registry validation lands before descendant traversal.** `concepts._validate_registry` runs at
  import and currently checks duplicate names, `is_a` resolvability and the `CONCEPTS`/registry
  mirror only. Descendant expansion over an unvalidated graph can loop; Task 1 precedes Task 3.
- **Read scope and freshness are never facets.** They are AND-ed always
  (`search.py:95-96`). Adding a facet must not change that.
- **No new read model.** E0 does not introduce a service, cursor, freshness policy or property
  contract. Those are E5.1's, on the Foundation service.
- **Measured baseline is a deliverable, not a nice-to-have.** Task 4 publishes M1 and M6; the
  programme's stop rule depends on them.
- Concept values are a closed registry vocabulary, so the facet needs no free-text sanitisation —
  but the filter value is still a bound parameter, never interpolated.

---

### Task 1: Reject self-parenting and `is_a` cycles in the concept registry

**Files:**
- Modify: `src/featuregen/overlay/upload/concepts.py` (`_validate_registry`, ~line 856)
- Test: `tests/featuregen/overlay/upload/test_concepts_registry.py`

**Interfaces:**
- Consumes: `_ALL` (the concept list), `CONCEPT_REGISTRY`, `Concept.is_a`
- Produces: nothing new — `_validate_registry` keeps raising `ValueError` at import

- [ ] **Step 1: Write the failing tests**

```python
def test_a_self_parenting_concept_is_rejected():
    """`is_a` pointing at itself is a one-node cycle. Descendant expansion over it never terminates,
    and the registry is validated at IMPORT precisely so a bad edit cannot reach a query."""
    bad = (Concept("loop_a", "categorical", is_a="loop_a"),)
    with pytest.raises(ValueError, match="cycle"):
        _validate_is_a_acyclic(bad)


def test_a_two_node_cycle_is_rejected():
    bad = (Concept("loop_a", "categorical", is_a="loop_b"),
           Concept("loop_b", "categorical", is_a="loop_a"))
    with pytest.raises(ValueError, match="cycle"):
        _validate_is_a_acyclic(bad)


def test_a_deep_acyclic_chain_is_accepted():
    """The control: a legitimate 4-deep hierarchy must not trip the cycle check."""
    ok = (Concept("l0", "categorical"),
          Concept("l1", "categorical", is_a="l0"),
          Concept("l2", "categorical", is_a="l1"),
          Concept("l3", "categorical", is_a="l2"))
    _validate_is_a_acyclic(ok)          # does not raise


def test_the_shipped_registry_is_acyclic():
    """The registry as shipped must pass. This is the assertion that makes the rule real rather
    than a rule only fixtures obey."""
    _validate_is_a_acyclic(tuple(CONCEPT_REGISTRY.values()))
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_concepts_registry.py -q`
Expected: FAIL — `cannot import name '_validate_is_a_acyclic'`

- [ ] **Step 3: Implement**

```python
def _validate_is_a_acyclic(concepts: tuple[Concept, ...]) -> None:
    """Refuse a self-parent or any `is_a` cycle.

    Split out of `_validate_registry` so it is directly testable against a synthetic bad registry:
    the shipped one is (and must stay) acyclic, so a cycle check that could only be exercised
    through the real registry could never be proven to fire.

    Iterative colour-marking rather than recursion — a malformed registry is exactly the input that
    would blow a recursive walk's stack before reaching the raise.
    """
    parent = {c.name: c.is_a for c in concepts if c.is_a}
    state: dict[str, int] = {}          # 1 = on the current path, 2 = settled
    for start in parent:
        if state.get(start):
            continue
        path: list[str] = []
        node: str | None = start
        while node is not None and not state.get(node):
            state[node] = 1
            path.append(node)
            node = parent.get(node)
        if node is not None and state.get(node) == 1:
            cycle = path[path.index(node):] if node in path else [node]
            raise ValueError(f"concept is_a cycle: {' -> '.join([*cycle, node])}")
        for seen in path:
            state[seen] = 2
```

Then call it from `_validate_registry`, after the existing resolvability loop:

```python
    _validate_is_a_acyclic(tuple(_ALL))
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_concepts_registry.py -q`
Expected: PASS (4 tests)

- [ ] **Step 5: Prove the check can fail**

Temporarily add `Concept("x", "categorical", is_a="x")` to `_ALL`, confirm the import raises, then
revert. Record the observed message in the task report.

- [ ] **Step 6: Commit**

```bash
git add src/featuregen/overlay/upload/concepts.py tests/featuregen/overlay/upload/test_concepts_registry.py
git commit -m "fix(concepts): reject self-parenting and is_a cycles before descendant traversal"
```

---

### Task 2: Add `concept` as a search facet

**Files:**
- Modify: `src/featuregen/overlay/upload/search.py` (`_COLUMN_FACETS`, line 15)
- Test: `tests/featuregen/overlay/upload/test_search_concept_facet.py`

**Interfaces:**
- Consumes: `_COLUMN_FACETS` — a generic `{facet name: graph_node column}` map already driving
  filtering, the `(none)` NULL bucket and facet counting. No other change is needed.
- Produces: `facets["concept"]` in `SearchResult.facets`; `concept=` accepted as a filter.

- [ ] **Step 1: Write the failing test**

```python
def test_concept_is_a_filterable_faceted_dimension(db):
    """Outcome item 1 of the programme: find every readable column that MEANS a thing, across
    catalogs. `search()` is already cross-catalog, fresh-only and read-scoped and already returns
    `n.concept`; it simply was not filterable."""
    _two_catalogs(db)                      # core.balance + cards.balance, both concept=balance
    r = search(db, "", now=_NOW, facets={"concept": ["balance"]})
    assert {h.catalog_source for h in r.hits} == {"core", "cards"}
    assert all(h.concept == "balance" for h in r.hits)


def test_the_concept_facet_reports_buckets_with_counts(db):
    _two_catalogs(db)
    buckets = {b.value: b.count for b in search(db, "", now=_NOW).facets["concept"]}
    assert buckets["balance"] == 2


def test_an_unconcepted_column_lands_in_the_none_bucket(db):
    """A column with no concept must be reachable and countable, not invisible — the empty state is
    the curator's to-do list."""
    _two_catalogs(db, include_unconcepted=True)
    buckets = {b.value: b.count for b in search(db, "", now=_NOW).facets["concept"]}
    assert buckets["(none)"] >= 1
    r = search(db, "", now=_NOW, facets={"concept": ["(none)"]})
    assert all(h.concept is None for h in r.hits)


def test_the_concept_facet_does_not_bypass_read_scope(db):
    """Read scope is AND-ed always and is never a facet. Filtering by a concept must not reveal a
    column the caller cannot see."""
    _restricted_balance(db)                # concept=balance, governed floor = restricted
    assert search(db, "", now=_NOW, facets={"concept": ["balance"]}, roles=()).hits == []
    assert search(db, "", now=_NOW, facets={"concept": ["balance"]},
                  roles={"restricted_reader"}).hits != []
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_search_concept_facet.py -q`
Expected: FAIL — `KeyError: 'concept'` on the facets dict.

- [ ] **Step 3: Implement**

```python
_COLUMN_FACETS: dict[str, str] = {
    "source": "catalog_source",
    "concept": "concept",
    "domain": "domain",
    "sensitivity": "sensitivity",
    "additivity": "additivity",
    "entity": "entity",
    "kind": "kind",
}
```

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_search_concept_facet.py -q`
Expected: PASS (4 tests)

- [ ] **Step 5: Run the existing search suite unchanged**

Run: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_search.py tests/featuregen/overlay/upload/test_read_scope.py -q`
Expected: PASS, no edits to those files. A new facet must not alter existing results.

- [ ] **Step 6: Commit**

```bash
git add src/featuregen/overlay/upload/search.py tests/featuregen/overlay/upload/test_search_concept_facet.py
git commit -m "feat(search): make concept a filterable, faceted dimension"
```

---

### Task 3: Optional descendant expansion on the concept filter

**Files:**
- Modify: `src/featuregen/overlay/upload/search.py` (filter construction, ~line 109)
- Modify: `src/featuregen/overlay/upload/concepts.py` (add `descendants_of`)
- Test: `tests/featuregen/overlay/upload/test_search_concept_facet.py` (extend)

**Interfaces:**
- Consumes: `_validate_is_a_acyclic` from Task 1 — expansion is only safe because the graph is
  proven acyclic at import.
- Produces: `descendants_of(name) -> tuple[str, ...]` (the concept itself plus every transitive
  `is_a` child, sorted); `search(..., include_descendants=False)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_descendants_are_not_included_by_default(db):
    """`include_descendants=false` never widens silently — the default must be exact match."""
    _hierarchy(db)                         # balance <- current_balance <- available_balance
    r = search(db, "", now=_NOW, facets={"concept": ["balance"]})
    assert {h.concept for h in r.hits} == {"balance"}


def test_descendants_are_included_on_request(db):
    _hierarchy(db)
    r = search(db, "", now=_NOW, facets={"concept": ["balance"]}, include_descendants=True)
    assert {h.concept for h in r.hits} == {"balance", "current_balance", "available_balance"}


def test_descendants_of_is_deterministic_and_includes_itself():
    assert descendants_of("balance")[0] == "balance"
    assert descendants_of("balance") == tuple(sorted(descendants_of("balance")))


def test_expansion_still_applies_read_scope(db):
    """Widening by hierarchy must not widen by authorization."""
    _hierarchy(db, restrict="available_balance")
    r = search(db, "", now=_NOW, facets={"concept": ["balance"]}, include_descendants=True, roles=())
    assert "available_balance" not in {h.concept for h in r.hits}
```

- [ ] **Step 2: Run to verify they fail**

Expected: FAIL — `search() got an unexpected keyword argument 'include_descendants'`

- [ ] **Step 3: Implement**

```python
def descendants_of(name: str) -> tuple[str, ...]:
    """`name` plus every transitive `is_a` descendant, sorted.

    Terminates because `_validate_is_a_acyclic` proved the graph acyclic at import (Task 1). Sorted
    so the generated SQL parameter is stable and two identical queries hash identically.
    """
    children: dict[str, list[str]] = {}
    for c in CONCEPT_REGISTRY.values():
        if c.is_a:
            children.setdefault(c.is_a, []).append(c.name)
    out, stack = {name}, [name]
    while stack:
        for child in children.get(stack.pop(), ()):
            if child not in out:
                out.add(child)
                stack.append(child)
    return tuple(sorted(out))
```

In `search()`, when building the `concept` predicate and `include_descendants` is set, expand each
requested value through `descendants_of` before binding. The `(none)` token is NOT expanded — it
selects `IS NULL` and has no hierarchy.

- [ ] **Step 4: Run to verify they pass**

Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(search): optional is_a descendant expansion on the concept filter"
```

---

### Task 4: Publish the M1 and M6 baselines

**Files:**
- Create: `scripts/measure_concept_coverage.py`
- Test: `tests/featuregen/overlay/upload/test_concept_coverage_measure.py`

**Interfaces:**
- Produces: `measure_concept_coverage(conn) -> dict` with `by_source`, `total_columns`,
  `with_concept`, `distinct_concepts`, `distinct_concept_source_pairs`.

The programme's stop rule reads these. Publishing them is the deliverable — a number, not a claim.

- [ ] **Step 1: Write the failing test**

```python
def test_the_measure_reports_coverage_per_source(db):
    _two_catalogs(db, include_unconcepted=True)
    m = measure_concept_coverage(db)
    assert m["total_columns"] >= 3
    assert m["with_concept"] < m["total_columns"]        # the unconcepted column is counted honestly
    assert m["distinct_concept_source_pairs"] >= 2       # M6: reachable in one cross-catalog query
    assert set(m["by_source"]) == {"core", "cards"}
```

- [ ] **Step 2: Run to verify it fails.** Expected: import error.

- [ ] **Step 3: Implement** — one read-only query grouping `graph_node` by `catalog_source` over
`kind='column'`, counting rows and non-null `concept`, plus a `count(DISTINCT (concept,
catalog_source))`. No read-scope filter: this is an operator measure of the catalog, not a user read,
and it returns counts only — never a column name.

- [ ] **Step 4: Run to verify it passes.**

- [ ] **Step 5: Run against the deployed catalog and record the numbers** in the task report and in
`docs/architecture/2026-07-28-verified-interfaces-cross-catalog.md` §8. The last recorded values were
M1 = 120/126 across 33 concepts, M6 = 33, M7 = 1 source.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "chore(measure): publish concept-coverage baselines M1/M6"
```

---

### Task 5: Surface the concept facet in the search screen

**Files:**
- Modify: `frontend/src/screens/SearchScreen.tsx`
- Test: `frontend/src/screens/SearchScreen.test.tsx`

**Interfaces:**
- Consumes: `facets.concept` from the search response; `concept` accepted as a filter param.
- `hit.concept` is already rendered (`SearchScreen.tsx:435`) and the search box placeholder already
  reads "Column, table, or concept" — the screen anticipated this.

- [ ] **Step 1: Write the failing tests**

```tsx
it("offers concept as a filter with counts", async () => { /* renders facets.concept buckets */ });
it("shows the (none) bucket so uncurated columns are reachable", async () => { /* ... */ });
it("keeps the descendant toggle off by default", async () => { /* exact match unless asked */ });
```

- [ ] **Step 2: Run to verify they fail.**

Run: `npx vitest run src/screens/SearchScreen.test.tsx`

> The full vitest suite hangs on worker-start in this environment; run the changed file directly and
> let CI run the suite.

- [ ] **Step 3: Implement** — render `facets.concept` with the existing facet component, plus a
descendant toggle wired to `include_descendants`. The empty state must say *"no columns carry this
concept"*, never *"no results"*: on this surface an empty facet is a curation to-do, and the
distinction is the point of the screen.

- [ ] **Step 4: Run to verify they pass.**

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(search-ui): concept filter, counts and descendant toggle"
```

---

## Acceptance (Phase E0)

- One concept filter returns matching readable columns from two catalogs.
- `include_descendants=false` never widens silently; `true` follows only validated `is_a` edges.
- A self-parent and a two-node cycle fail registry validation; the shipped registry passes.
- A restricted column is absent from hits, counts and facets without the role.
- A stale source is absent (inherited from `search()`, not re-implemented).
- **M1 and M6 are published**, per source.

## Out of scope

Per-field authority envelopes, schema-preserving identity, source entitlement, an explicit freshness
policy, node-closed pagination and signed cursors. Those are E5.1, on the Foundation service, and
E5.1 supersedes this facet when it lands.

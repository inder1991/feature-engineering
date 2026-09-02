# Derived Target Labels — Contract and Registry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A typed, content-hashed, catalog-validated representation of a derived training label, stored in a registry that mirrors the feature registry, so a label can be authored once and reused across models.

**Architecture:** Two closed rule shapes (`state_change`, `event_window`) over one header, refused at construction like `RecipeDefinitionV2` and `ModelFeatureSpecV1`. A pure-Python contract module with no DB or LLM dependency, then a migration and a store. Catalog resolution is a separate DB-backed validator so the contract stays unit-testable.

**Tech Stack:** Python 3.11, frozen slots dataclasses, `psycopg`, pytest with the repo's `db` fixture, raw SQL migrations applied lexically by the ledger (`schema_migrations`).

**Spec:** `docs/superpowers/specs/2026-09-01-derived-target-labels-design.md`

## Global Constraints

- **Scope is SPECIFY, NOT EXECUTE.** Nothing in this plan runs a rule, materialises labels, or reads catalog *data*. It reads catalog *metadata* only (`graph_node`). Spec §4.
- **Names:** `^tgt_[a-z0-9_]{1,123}$` — the `tgt_` prefix is the owner's decision, the remainder matches the existing feature-name rule `^[a-z][a-z0-9_]{0,127}$`. Spec §5.
- **`direction` is always `"forward"`.** A backward rule is a feature; it is refused, never corrected. Spec §7.5.
- **`label_type` and `operator`/`threshold` are exclusive:** `binary` requires both; `count` and `amount` forbid both. Spec §7.1.
- **Migration number 1142.** 1130–1139 are reserved by the cross-catalog serving program (see the reservation note in `1141_draft_plan_binding_rechecks_its_pins.sql`), 1140 and 1141 are allocated. **1142 is the next free number and this plan allocates it.** Migration files are checksummed and immutable once applied anywhere.
- **Every ref is scoped by a catalog.** `graph_node.object_ref` is only `public.{table}.{column}` — the catalog is a separate column, so a bare ref does not identify a column. `_column_meta` scopes to an exact `(catalog_source, object_ref)` pair and cites finding **M3** for it: *"a same-named column in another catalog cannot contaminate the reading"*. Carried per **side** (`anchor_catalog` on the header, `event_catalog` on the event shape), not per ref. Spec §7.1.
- **Content hash:** `hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()`, matching `model_feature_revision_hash`.
- **The verification ladder stops at `DESIGN-CHECKED`.** This plan must never write `DATA-CHECKED` or above — the platform does not execute the rule and cannot know the class balance. Spec §10.

## Deviation from the spec, stated

Spec §6 lists **four** tables, mirroring the feature registry: `target`, `target_definition`,
`target_derives_from`, `target_consumer`. This plan creates **three**, folding `target` into
`target_definition`.

The feature registry separates the two because a feature keeps one name across many revisions
(`feature_active_revision` points at the current one). A label does not work that way here: spec §5
rules that a different window is a *different label with a different name* — `tgt_npe_60d` and
`tgt_npe_90d` are two labels, not two revisions of one. With name and definition 1:1, a second
table holds nothing but a join.

**This forecloses one thing, deliberately:** spec §12.2 asks who may change a definition other
models are trained against, and anticipates an active-revision pointer. That question is still open.
Splitting the table is a migration away and is cheaper than building revision semantics before the
governance rule that gives them meaning is decided. If §12.2 resolves toward revisions under a
stable name, the split happens then.

---

### Task 1: The header — identity, window, and the label-type pairing

**Files:**
- Create: `src/featuregen/overlay/upload/target_contract.py`
- Test: `tests/featuregen/overlay/upload/test_target_contract.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `TargetContractError`, `TargetHeaderV1`, `LABEL_TYPES`, `OPERATORS`, `DIRECTIONS`.

- [ ] **Step 1: Write the failing test**

```python
"""The derived-label contract: a label is a RULE, refused at construction when malformed."""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.target_contract import (
    TargetContractError,
    TargetHeaderV1,
)


def _header(**over) -> TargetHeaderV1:
    base = dict(name="tgt_npe_90d", entity="customer", anchor_catalog="cib",
                grain_ref="public.bo_cib_customer.cust_num",
                as_of_ref="public.bo_cib_customer.business_dt",
                window_days=90, label_type="binary", operator=">=", threshold=1.0)
    return TargetHeaderV1(**{**base, **over})


def test_a_well_formed_binary_header_is_accepted():
    h = _header()
    assert (h.name, h.window_days, h.direction) == ("tgt_npe_90d", 90, "forward")


def test_the_name_must_carry_the_tgt_prefix():
    """The prefix is the owner's decision and it is what makes a label recognisable in a
    registry it shares with nothing else."""
    with pytest.raises(TargetContractError, match="name"):
        _header(name="npe_90d")


def test_direction_is_always_forward_and_a_backward_rule_is_REFUSED():
    """A rule that reads backward from the as-of date is a FEATURE. Correcting it silently
    would hide the confusion; the refusal is the point."""
    with pytest.raises(TargetContractError, match="forward"):
        _header(direction="backward")


def test_a_binary_label_REQUIRES_operator_and_threshold():
    with pytest.raises(TargetContractError, match="binary"):
        _header(operator=None, threshold=None)


def test_a_count_label_FORBIDS_operator_and_threshold():
    """`count` measures; it does not threshold. Carrying both is the field pair most likely to
    be filled in inconsistently, so it is checked rather than trusted."""
    with pytest.raises(TargetContractError, match="count"):
        _header(label_type="count", operator=">=", threshold=1.0)


def test_a_count_label_without_a_threshold_is_accepted():
    assert _header(label_type="count", operator=None, threshold=None).label_type == "count"


def test_the_window_must_be_positive():
    with pytest.raises(TargetContractError, match="window_days"):
        _header(window_days=0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/test_target_contract.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'featuregen.overlay.upload.target_contract'`

- [ ] **Step 3: Write minimal implementation**

```python
"""Derived target labels — the rule contract.

A training label is normally CONSTRUCTED ("no transaction for 90 days"), not stored. The platform
had two half-representations and no bridge: intake required a `target_ref` resolving to a real
column, and the model contract held `target_definition` as prose "in reviewed words". Neither is
executable. See docs/superpowers/specs/2026-09-01-derived-target-labels-design.md.

The one inverted property that gives this its own lane: a FEATURE must never read forward of the
as-of date; a LABEL must read exactly forward of it. `direction` + `window_days` are that
declaration, and they are the governed object.

Pure Python: no DB, no LLM. Catalog resolution lives in `target_catalog_check`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

LABEL_TYPES = ("binary", "count", "amount")
OPERATORS = ("==", "!=", ">=", "<=", ">", "<")
#: Only forward. A backward rule is a feature — see the module docstring.
DIRECTIONS = ("forward",)

#: `tgt_` is the owner's decision; the tail matches the existing feature-name rule.
_NAME_RE = re.compile(r"^tgt_[a-z0-9_]{1,123}$")


class TargetContractError(ValueError):
    """An invalid rule — refused at construction, exactly like RecipeDefinitionV2."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise TargetContractError(message)


@dataclass(frozen=True, slots=True)
class TargetHeaderV1:
    """What every label declares regardless of shape."""

    name: str
    entity: str
    #: Which catalog `grain_ref` and `as_of_ref` live in. A bare ref does not identify a column
    #: (M3) — `graph_node.object_ref` omits the catalog entirely.
    anchor_catalog: str
    grain_ref: str
    as_of_ref: str
    window_days: int
    label_type: str
    direction: str = "forward"
    operator: str | None = None
    threshold: float | None = None

    def __post_init__(self) -> None:
        _require(bool(_NAME_RE.match(self.name)),
                 f"name {self.name!r} must match {_NAME_RE.pattern}")
        _require(bool(self.entity.strip()), "entity is mandatory")
        _require(bool(self.anchor_catalog.strip()),
                 "anchor_catalog is mandatory — a ref without a catalog does not identify a "
                 "column (M3)")
        _require(bool(self.grain_ref.strip()), "grain_ref is mandatory")
        _require(bool(self.as_of_ref.strip()),
                 "as_of_ref is mandatory — a label without an anchor date cannot be computed")
        _require(self.window_days > 0, "window_days must be positive")
        _require(self.direction in DIRECTIONS,
                 f"direction must be forward, got {self.direction!r} — a rule reading backward "
                 "from the as-of date is a FEATURE, not a label")
        _require(self.label_type in LABEL_TYPES,
                 f"label_type {self.label_type!r} not in {LABEL_TYPES}")
        thresholded = self.operator is not None or self.threshold is not None
        if self.label_type == "binary":
            _require(self.operator is not None and self.threshold is not None,
                     "a binary label REQUIRES operator and threshold")
            # Reported separately: "requires an operator" misdirects when one was supplied and is
            # simply not a recognised comparison.
            _require(self.operator in OPERATORS,
                     f"operator {self.operator!r} not in {OPERATORS}")
        else:
            _require(not thresholded,
                     f"a {self.label_type} label FORBIDS operator/threshold — it measures, "
                     "it does not threshold")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/featuregen/overlay/upload/test_target_contract.py -v`
Expected: PASS — 7 tests.

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/target_contract.py tests/featuregen/overlay/upload/test_target_contract.py
git commit -m "feat(target): the label header — forward-only, with the label_type/threshold pairing checked"
```

---

### Task 2: The `state_change` shape

**Files:**
- Modify: `src/featuregen/overlay/upload/target_contract.py`
- Test: `tests/featuregen/overlay/upload/test_target_contract.py`

**Interfaces:**
- Consumes: `TargetHeaderV1`, `TargetContractError`, `_require` from Task 1.
- Produces: `StateChangeRuleV1(header, column_ref, from_values, to_values, at_least_once, population_filter)`, `STATE_POPULATIONS`.

- [ ] **Step 1: Write the failing test**

Append to `tests/featuregen/overlay/upload/test_target_contract.py`:

```python
from featuregen.overlay.upload.target_contract import StateChangeRuleV1


def _state(**over) -> StateChangeRuleV1:
    base = dict(header=_header(),
                column_ref="public.bo_cib_customer.cust_perf_nonperf_flg",
                from_values=("Performing",), to_values=("Non-performing",))
    return StateChangeRuleV1(**{**base, **over})


def test_a_well_formed_state_change_rule_is_accepted():
    r = _state()
    assert r.column_ref.endswith("cust_perf_nonperf_flg")
    assert r.population_filter == "from_values"


def test_a_value_in_BOTH_from_and_to_is_incoherent():
    """If Performing is both the starting state and the outcome, the rule asks whether a
    customer changed from a state to itself. Silently always-0; caught here instead."""
    with pytest.raises(TargetContractError, match="both"):
        _state(from_values=("Performing",), to_values=("Performing", "Non-performing"))


def test_from_and_to_values_are_both_mandatory():
    """Empty values are how a label becomes silently always-0 — nothing matches."""
    with pytest.raises(TargetContractError, match="from_values"):
        _state(from_values=())


def test_a_state_change_label_must_be_binary():
    """A state either changed or it did not; counting a change is a different rule shape."""
    with pytest.raises(TargetContractError, match="binary"):
        _state(header=_header(label_type="count", operator=None, threshold=None))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/test_target_contract.py -k state -v`
Expected: FAIL — `ImportError: cannot import name 'StateChangeRuleV1'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/featuregen/overlay/upload/target_contract.py`:

```python
#: How the population is bounded for a state-change rule. `from_values` excludes rows that
#: ALREADY have the outcome at the as-of date — a customer already non-performing on 1 January is
#: not a candidate. Omitting this is the most common way to build a silently broken label.
STATE_POPULATIONS = ("from_values", "all")


@dataclass(frozen=True, slots=True)
class StateChangeRuleV1:
    """A column's value at the as-of date, compared against its value inside the window.

    Requires an append-only snapshot source: if the source rewrites a row in place rather than
    appending a new as-of, "the value in January" silently returns a later one. `header.as_of_ref`
    records which column carries that assumption.
    """

    header: TargetHeaderV1
    column_ref: str
    from_values: tuple[str, ...]
    to_values: tuple[str, ...]
    at_least_once: bool = True
    population_filter: str = "from_values"

    shape: str = "state_change"

    def __post_init__(self) -> None:
        _require(bool(self.column_ref.strip()), "column_ref is mandatory")
        _require(bool(self.from_values), "from_values is mandatory")
        _require(bool(self.to_values), "to_values is mandatory")
        overlap = set(self.from_values) & set(self.to_values)
        _require(not overlap,
                 f"{sorted(overlap)!r} appear in both from_values and to_values — a change from "
                 "a state to itself is not an outcome")
        _require(self.population_filter in STATE_POPULATIONS,
                 f"population_filter {self.population_filter!r} not in {STATE_POPULATIONS}")
        _require(self.header.label_type == "binary",
                 "a state_change label must be binary — a state changed or it did not")
        _require(self.column_ref != self.header.as_of_ref,
                 "column_ref and as_of_ref are the same column — the rule would compare a date "
                 "against itself and observe nothing")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/featuregen/overlay/upload/test_target_contract.py -v`
Expected: PASS — 11 tests.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(target): the state_change shape, with from/to overlap refused"
```

---

### Task 3: The `event_window` shape and its population filter

**Files:**
- Modify: `src/featuregen/overlay/upload/target_contract.py`
- Test: `tests/featuregen/overlay/upload/test_target_contract.py`

**Interfaces:**
- Consumes: Task 1 and Task 2 symbols.
- Produces: `EventWindowRuleV1(header, event_table, event_date_ref, join_left, join_right, event_filter, aggregate, measure_ref, population_lookback_days, population_having)`, `AGGREGATES`, `EVENT_POPULATIONS`.

- [ ] **Step 1: Write the failing test**

Append to the same test file:

```python
from featuregen.overlay.upload.target_contract import EventWindowRuleV1


def _event(**over) -> EventWindowRuleV1:
    base = dict(header=_header(name="tgt_fx_new_60d", window_days=60),
                event_catalog="ftr",
                event_table="public.comp_financial_tran_repos_dly",
                event_date_ref="public.comp_financial_tran_repos_dly.pstd_date",
                join_left="public.bo_cib_customer.cust_num",
                join_right="public.comp_financial_tran_repos_dly.cif_id",
                event_filter="tran_crncy <> counter_party_tran_crncy",
                aggregate="count")
    return EventWindowRuleV1(**{**base, **over})


def test_a_well_formed_event_window_rule_is_accepted():
    assert _event().aggregate == "count"


def test_the_default_population_is_the_WHOLE_population():
    """Explicit, because it is the degenerate choice: on "who will do FX" it means customers
    already trading FX dominate and the model restates last month."""
    assert (_event().population_having, _event().population_lookback_days) == ("any", 0)


def test_excluding_prior_activity_REQUIRES_a_lookback():
    """"Who will START doing FX" is meaningless without saying how far back "not currently" looks."""
    with pytest.raises(TargetContractError, match="lookback"):
        _event(population_having="none", population_lookback_days=0)


def test_a_new_to_activity_population_is_accepted():
    r = _event(population_having="none", population_lookback_days=180)
    assert r.population_lookback_days == 180


def test_a_sum_aggregate_REQUIRES_a_measure():
    with pytest.raises(TargetContractError, match="measure_ref"):
        _event(header=_header(name="tgt_fx_volume_60d", label_type="amount",
                              operator=None, threshold=None),
               aggregate="sum")


def test_a_count_aggregate_FORBIDS_a_measure():
    with pytest.raises(TargetContractError, match="measure_ref"):
        _event(measure_ref="public.comp_financial_tran_repos_dly.tran_amt_aed")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/test_target_contract.py -k event -v`
Expected: FAIL — `ImportError: cannot import name 'EventWindowRuleV1'`

- [ ] **Step 3: Write minimal implementation**

Append to `target_contract.py`:

```python
AGGREGATES = ("count", "sum")
#: `none` restricts the population to rows with NO matching event in the lookback before the as-of
#: date ("who will START"); `any` is the whole population ("who will do it at all"). The difference
#: is which question is being asked, and `any` silently yields the degenerate label.
EVENT_POPULATIONS = ("any", "none")


@dataclass(frozen=True, slots=True)
class EventWindowRuleV1:
    """Rows in a second table, inside the window, joined to the grain.

    Cross-catalog by construction — anchored in one catalog, counting events in another. The join
    is DECLARED in a reviewed definition rather than decided by a planner at request time, so this
    needs none of the live cross-catalog machinery.
    """

    header: TargetHeaderV1
    #: The catalog the EVENT side lives in — routinely different from `header.anchor_catalog`,
    #: which is what makes this shape cross-catalog and why it must be stated.
    event_catalog: str
    event_table: str
    event_date_ref: str
    join_left: str
    join_right: str
    aggregate: str
    event_filter: str | None = None
    measure_ref: str | None = None
    population_lookback_days: int = 0
    population_having: str = "any"

    shape: str = "event_window"

    def __post_init__(self) -> None:
        for field_name in ("event_catalog", "event_table", "event_date_ref",
                           "join_left", "join_right"):
            _require(bool(getattr(self, field_name).strip()), f"{field_name} is mandatory")
        _require(self.aggregate in AGGREGATES,
                 f"aggregate {self.aggregate!r} not in {AGGREGATES}")
        if self.aggregate == "sum":
            _require(self.measure_ref is not None and bool(self.measure_ref.strip()),
                     "a sum aggregate REQUIRES measure_ref — there is nothing to add up")
        else:
            _require(self.measure_ref is None,
                     "a count aggregate FORBIDS measure_ref — it counts rows, not values")
        _require(self.population_having in EVENT_POPULATIONS,
                 f"population_having {self.population_having!r} not in {EVENT_POPULATIONS}")
        if self.population_having == "none":
            _require(self.population_lookback_days > 0,
                     "excluding prior activity REQUIRES a positive population lookback — "
                     "'not currently doing this' is meaningless without a period")
        _require(self.population_lookback_days >= 0,
                 "population_lookback_days cannot be negative")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/featuregen/overlay/upload/test_target_contract.py -v`
Expected: PASS — 17 tests.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(target): the event_window shape with the population filter that makes 'who will START' expressible"
```

---

### Task 4: Refs read, and the content hash

**Files:**
- Modify: `src/featuregen/overlay/upload/target_contract.py`
- Test: `tests/featuregen/overlay/upload/test_target_contract.py`

**Interfaces:**
- Consumes: `StateChangeRuleV1`, `EventWindowRuleV1`.
- Produces: `refs_read(rule) -> tuple[str, ...]`, `canonical_target(rule) -> dict`, `target_content_hash(rule) -> str`.

- [ ] **Step 1: Write the failing test**

```python
from featuregen.overlay.upload.target_contract import (
    canonical_target, refs_read, target_content_hash,
)


def test_refs_read_names_every_column_the_rule_touches():
    """This is the lineage answer — "tran_crncy is being retired, which labels break?" — NOT a
    leakage blocklist. A feature reading the same columns BACKWARD is the method, not a leak."""
    assert refs_read(_event()) == (
        ("cib", "public.bo_cib_customer.business_dt"),
        ("cib", "public.bo_cib_customer.cust_num"),
        ("ftr", "public.comp_financial_tran_repos_dly.cif_id"),
        ("ftr", "public.comp_financial_tran_repos_dly.pstd_date"),
    )


def test_refs_read_keeps_the_two_SIDES_of_a_join_apart():
    """`join_left` is on the anchor and `join_right` on the event side. Collapsing them to bare
    refs is exactly the M3 defect `_column_meta` exists to avoid."""
    assert ("cib", "public.bo_cib_customer.cust_num") in refs_read(_event())
    assert ("ftr", "public.comp_financial_tran_repos_dly.cif_id") in refs_read(_event())


def test_refs_read_includes_the_measure_when_there_is_one():
    r = _event(header=_header(name="tgt_fx_volume_60d", label_type="amount",
                              operator=None, threshold=None),
               aggregate="sum",
               measure_ref="public.comp_financial_tran_repos_dly.tran_amt_aed")
    assert ("ftr", "public.comp_financial_tran_repos_dly.tran_amt_aed") in refs_read(r)


def test_refs_read_for_a_state_change_includes_the_watched_column():
    assert ("cib", "public.bo_cib_customer.cust_perf_nonperf_flg") in refs_read(_state())


def test_the_content_hash_is_stable_for_an_identical_rule():
    """Content-addressing is what makes an identical rule authored twice ONE row, and any edit a
    new definition rather than a mutation of one other models are already trained against."""
    assert target_content_hash(_state()) == target_content_hash(_state())


def test_changing_the_window_changes_the_hash():
    other = _state(header=_header(window_days=60))
    assert target_content_hash(_state()) != target_content_hash(other)


def test_the_canonical_body_carries_the_shape():
    assert canonical_target(_state())["shape"] == "state_change"
    assert canonical_target(_event())["shape"] == "event_window"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/test_target_contract.py -k "refs_read or hash or canonical" -v`
Expected: FAIL — `ImportError: cannot import name 'refs_read'`

- [ ] **Step 3: Write minimal implementation**

Add to the top of `target_contract.py`:

```python
import hashlib
import json
from dataclasses import asdict
```

Append:

```python
TargetRuleV1 = StateChangeRuleV1 | EventWindowRuleV1


def refs_read(rule: TargetRuleV1) -> tuple[tuple[str, str], ...]:
    """Every catalog ref the rule reads, sorted and deduped.

    For LINEAGE and IMPACT — "this column is being retired, which labels break?" — and NOT a
    leakage blocklist. A feature reading the same columns BACKWARD from the as-of date is the
    method (past FX predicting future FX); blocking on column overlap would delete the use case.
    The leakage control is temporal and already exists. Spec §8, §9.
    """
    anchor = rule.header.anchor_catalog
    refs = {(anchor, rule.header.grain_ref), (anchor, rule.header.as_of_ref)}
    if isinstance(rule, StateChangeRuleV1):
        refs.add((anchor, rule.column_ref))
    else:
        # join_left is on the ANCHOR side, join_right on the EVENT side — the whole point of the
        # pair. Collapsing them to bare refs is the M3 defect.
        refs.add((anchor, rule.join_left))
        refs.update({(rule.event_catalog, rule.event_date_ref),
                     (rule.event_catalog, rule.join_right)})
        if rule.measure_ref is not None:
            refs.add((rule.event_catalog, rule.measure_ref))
    return tuple(sorted(refs))


def canonical_target(rule: TargetRuleV1) -> dict:
    """The rule as a plain, ordered dict — the body the content hash is taken over."""
    # `asdict` recurses into the nested header dataclass already — no second pass needed.
    return asdict(rule)


def target_content_hash(rule: TargetRuleV1) -> str:
    """Identity by content, matching `model_feature_revision_hash`."""
    body = json.dumps(canonical_target(rule), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/featuregen/overlay/upload/test_target_contract.py -v`
Expected: PASS — 23 tests.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(target): refs_read for lineage, and content-addressed rule identity"
```

---

### Task 5: The registry migration and store

**Files:**
- Create: `src/featuregen/db/migrations/1142_target_label_registry.sql`
- Create: `src/featuregen/overlay/upload/target_store.py`
- Test: `tests/featuregen/overlay/upload/test_target_store.py`

**Interfaces:**
- Consumes: `TargetRuleV1`, `refs_read`, `target_content_hash`, `canonical_target`.
- Produces: `register_target(conn, rule, *, description, registered_by) -> str` (returns `definition_id`); `target_by_name(conn, entity, name) -> dict | None`; `targets_for_entity(conn, entity) -> list[dict]`; `TargetNameTaken`.

- [ ] **Step 1: Write the failing test**

```python
"""The target registry — content-addressed, reusable across models, mirroring the feature registry."""
from __future__ import annotations

from featuregen.overlay.upload.target_contract import (
    EventWindowRuleV1, StateChangeRuleV1, TargetHeaderV1,
)
import pytest

from featuregen.overlay.upload.target_store import (
    TargetNameTaken, register_target, target_by_name, targets_for_entity,
)


def _rule(name="tgt_npe_90d", window=90) -> StateChangeRuleV1:
    return StateChangeRuleV1(
        header=TargetHeaderV1(name=name, entity="customer",
                              grain_ref="public.bo_cib_customer.cust_num",
                              as_of_ref="public.bo_cib_customer.business_dt",
                              window_days=window, label_type="binary",
                              operator=">=", threshold=1.0),
        column_ref="public.bo_cib_customer.cust_perf_nonperf_flg",
        from_values=("Performing",), to_values=("Non-performing",))


def test_registering_a_rule_stores_it_and_its_lineage(db):
    register_target(db, _rule(), description="credit deterioration", registered_by="user:tester")
    row = target_by_name(db, "customer", "tgt_npe_90d")
    assert row["entity"] == "customer"
    assert row["verification"] == "DESIGN-CHECKED"
    assert ("cib", "public.bo_cib_customer.cust_perf_nonperf_flg") in row["derives_from"]


def test_the_ladder_never_starts_above_DESIGN_CHECKED(db):
    """The platform does not execute the rule, so it cannot know the class balance or whether the
    rule matched anything at all. DATA-CHECKED is unreachable here BY CONSTRUCTION. Spec §10."""
    register_target(db, _rule(), description="d", registered_by="user:tester")
    assert target_by_name(db, "customer", "tgt_npe_90d")["verification"] == "DESIGN-CHECKED"


def test_registering_the_SAME_rule_twice_is_one_definition(db):
    """Content-addressing: an identical rule authored twice is one row, not two."""
    first = register_target(db, _rule(), description="d", registered_by="a")
    second = register_target(db, _rule(), description="d", registered_by="b")
    assert first == second


def test_reusing_a_NAME_for_a_different_rule_is_a_typed_refusal(db):
    """Someone iterating on a definition will hit this, so it must not surface as a raw
    IntegrityError from the (entity, name) index. The refusal names the definition in the way."""
    register_target(db, _rule(), description="d", registered_by="a")
    changed = _rule()
    changed = StateChangeRuleV1(header=changed.header, column_ref=changed.column_ref,
                                from_values=("Performing",), to_values=("Watchlist",))
    with pytest.raises(TargetNameTaken, match="tgt_npe_90d"):
        register_target(db, changed, description="d", registered_by="a")


def test_a_different_window_is_a_DIFFERENT_label(db):
    """`tgt_npe_60d` and `tgt_npe_90d` are two labels, both governed — a different window is not
    a variant of one rule."""
    register_target(db, _rule(), description="d", registered_by="a")
    register_target(db, _rule(name="tgt_npe_60d", window=60), description="d", registered_by="a")
    assert {t["name"] for t in targets_for_entity(db, "customer")} == {
        "tgt_npe_90d", "tgt_npe_60d"}


def test_search_is_scoped_to_the_entity(db):
    register_target(db, _rule(), description="d", registered_by="a")
    assert targets_for_entity(db, "account") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/test_target_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'featuregen.overlay.upload.target_store'`

- [ ] **Step 3: Write the migration**

Create `src/featuregen/db/migrations/1142_target_label_registry.sql`:

```sql
-- src/featuregen/db/migrations/1142_target_label_registry.sql
--
-- Derived target labels (spec 2026-09-01). A training label is normally CONSTRUCTED, not stored:
-- the deployed catalogs carry 237 columns, 100% concept coverage, and ZERO in the outcome family,
-- because the labels these users need were never going to be sitting there.
--
-- RESERVATION. 1130-1139 are the cross-catalog serving program's block and 1140/1141 were
-- allocated on 2026-08-29; 1142 is allocated here, on 2026-09-01, for this registry. Migration
-- files apply lexically and are checksummed by the ledger — immutable once applied anywhere.
--
-- Mirrors the feature registry (`feature` / `feature_definition` / `feature_derives_from` /
-- `feature_consumer`) because the owner asked for reuse across models "similar to the feature
-- registry", and that pattern already carries every property a label needs.

CREATE TABLE IF NOT EXISTS target_definition (
    definition_id   text PRIMARY KEY,
    name            text NOT NULL,
    entity          text NOT NULL,
    shape           text NOT NULL,
    window_days     integer NOT NULL,
    label_type      text NOT NULL,
    rule            jsonb NOT NULL,
    content_hash    text NOT NULL UNIQUE,
    description     text NOT NULL DEFAULT '',
    -- The ladder STOPS here under specify-not-execute: the platform never sees the labels the
    -- rule produces, so it cannot know the class balance or whether the rule matched nothing.
    -- DATA-CHECKED and above require the executing pipeline to report back (spec §10).
    verification    text NOT NULL DEFAULT 'DESIGN-CHECKED',
    registered_by   text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT target_definition_name_chk  CHECK (name ~ '^tgt_[a-z0-9_]{1,123}$'),
    CONSTRAINT target_definition_shape_chk CHECK (shape IN ('state_change', 'event_window')),
    CONSTRAINT target_definition_type_chk  CHECK (label_type IN ('binary', 'count', 'amount')),
    CONSTRAINT target_definition_window_chk CHECK (window_days > 0),
    -- DELIBERATELY NARROWER than the feature ladder, which also has DATA-CHECKED and
    -- USEFULNESS-CHECKED. Under specify-not-execute those rungs are unreachable, and a CHECK is a
    -- stronger guarantee than a convention nobody re-reads. CONSEQUENCE, accepted: admitting them
    -- later costs a migration — which is the right price for making the claim impossible to write
    -- by accident in the meantime.
    CONSTRAINT target_definition_verification_chk
        CHECK (verification IN ('UNVERIFIED', 'DESIGN-CHECKED'))
);

-- One name per entity, exactly as `feature_definition_name_per_entity`.
CREATE UNIQUE INDEX IF NOT EXISTS target_definition_name_per_entity
    ON target_definition (entity, name);

-- Lineage, for IMPACT ("this column is retired — which labels break?"), never a leakage blocklist.
CREATE TABLE IF NOT EXISTS target_derives_from (
    definition_id  text NOT NULL REFERENCES target_definition(definition_id) ON DELETE CASCADE,
    -- The catalog is part of the identity. `object_ref` is only `public.{table}.{column}`, so a
    -- bare ref does not name a column (M3) — the same reason `_column_meta` scopes to the pair.
    catalog_source text NOT NULL,
    object_ref     text NOT NULL,
    PRIMARY KEY (definition_id, catalog_source, object_ref)
);

CREATE TABLE IF NOT EXISTS target_consumer (
    definition_id text NOT NULL REFERENCES target_definition(definition_id) ON DELETE CASCADE,
    consumer_ref  text NOT NULL,
    recorded_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (definition_id, consumer_ref)
);

CREATE INDEX IF NOT EXISTS target_definition_entity_idx ON target_definition (entity);
```

- [ ] **Step 4: Write the store**

Create `src/featuregen/overlay/upload/target_store.py`:

```python
"""The target registry — register a rule, read it back, search by entity.

Content-addressed: an identical rule registered twice is ONE definition. That is what makes a
label reusable across models rather than re-invented per run, which is the difference between
"what does this bank mean by non-performing?" having one answer and having three.
"""
from __future__ import annotations

import json
import uuid

from featuregen.overlay.upload.target_contract import (
    TargetRuleV1,
    canonical_target,
    refs_read,
    target_content_hash,
)


class TargetNameTaken(ValueError):
    """This entity already has a label of this name, with a DIFFERENT rule. Raised rather than
    letting the `(entity, name)` unique index surface as a raw IntegrityError — someone iterating
    on a definition meets this routinely, and a typed refusal can name what is in the way."""


def register_target(conn, rule: TargetRuleV1, *, description: str, registered_by: str) -> str:
    """Persist a rule and its lineage; return the definition id.

    Idempotent on content: re-registering an identical rule returns the existing id rather than
    minting a second row. Verification is DESIGN-CHECKED and never higher — see the migration.
    """
    content_hash = target_content_hash(rule)
    existing = conn.execute(
        "SELECT definition_id FROM target_definition WHERE content_hash = %s",
        (content_hash,)).fetchone()
    if existing is not None:
        return existing[0]

    header = rule.header
    taken = conn.execute(
        "SELECT definition_id FROM target_definition WHERE entity = %s AND name = %s",
        (header.entity, header.name)).fetchone()
    if taken is not None:
        raise TargetNameTaken(
            f"{header.name} already exists for entity {header.entity} as {taken[0]} with a "
            "different rule — a changed rule is a new label, so give it its own name")

    definition_id = f"tdef_{uuid.uuid4().hex[:16]}"
    conn.execute(
        "INSERT INTO target_definition (definition_id, name, entity, shape, window_days,"
        " label_type, rule, content_hash, description, registered_by)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (definition_id, header.name, header.entity, rule.shape, header.window_days,
         header.label_type, json.dumps(canonical_target(rule)), content_hash,
         description, registered_by))
    for catalog_source, object_ref in refs_read(rule):
        conn.execute(
            "INSERT INTO target_derives_from (definition_id, catalog_source, object_ref)"
            " VALUES (%s, %s, %s)",
            (definition_id, catalog_source, object_ref))
    return definition_id


def _row(conn, definition_id: str, name: str, entity: str, shape: str, window_days: int,
         label_type: str, rule, verification: str, description: str) -> dict:
    derives = [(r[0], r[1]) for r in conn.execute(
        "SELECT catalog_source, object_ref FROM target_derives_from WHERE definition_id = %s"
        " ORDER BY catalog_source, object_ref", (definition_id,)).fetchall()]
    return {"definition_id": definition_id, "name": name, "entity": entity, "shape": shape,
            "window_days": window_days, "label_type": label_type, "rule": rule,
            "verification": verification, "description": description, "derives_from": derives}


_SELECT = ("SELECT definition_id, name, entity, shape, window_days, label_type, rule,"
           " verification, description FROM target_definition")


def target_by_name(conn, entity: str, name: str) -> dict | None:
    """Entity-scoped, because the unique index is `(entity, name)`. Looking up by name alone
    would return an arbitrary row once two entities both have a `tgt_churned_90d`."""
    row = conn.execute(f"{_SELECT} WHERE entity = %s AND name = %s", (entity, name)).fetchone()
    return None if row is None else _row(conn, *row)


def targets_for_entity(conn, entity: str) -> list[dict]:
    """Every label registered for this entity — the reuse surface. Ordered by name so a listing
    is stable."""
    rows = conn.execute(f"{_SELECT} WHERE entity = %s ORDER BY name", (entity,)).fetchall()
    return [_row(conn, *row) for row in rows]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/featuregen/overlay/upload/test_target_store.py -v`
Expected: PASS — 5 tests.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat(target): migration 1142 and the content-addressed target registry"
```

---

### Task 6: Catalog resolution — the checks that need the catalog

**Files:**
- Create: `src/featuregen/overlay/upload/target_catalog_check.py`
- Test: `tests/featuregen/overlay/upload/test_target_catalog_check.py`

**Interfaces:**
- Consumes: `TargetRuleV1`, `refs_read`.
- Produces: `check_target_against_catalog(conn, rule, *, roles) -> tuple[str, ...]` — a tuple of refusal reasons, empty when the rule resolves.

- [ ] **Step 1: Write the failing test**

```python
"""Catalog resolution: the checks that cannot be made without the catalog, kept OUT of the
contract so the contract stays a pure unit."""
from __future__ import annotations

from tests.featuregen.overlay.upload.test_templates import SOURCE

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.target_catalog_check import check_target_against_catalog
from featuregen.overlay.upload.target_contract import StateChangeRuleV1, TargetHeaderV1

_GRAIN = "public.customers.cust_num"
_ASOF = "public.customers.business_dt"
_FLAG = "public.customers.perf_flg"


def _catalog(db):
    rows = [
        (CanonicalRow(SOURCE, "customers", "cust_num", "integer", is_grain=True,
                      entity="Customer"), "customer_id"),
        (CanonicalRow(SOURCE, "customers", "business_dt", "timestamp", as_of=True), "as_of_date"),
        (CanonicalRow(SOURCE, "customers", "perf_flg", "text"), "npe_flag"),
    ]
    build_graph(db, SOURCE, [r for r, _ in rows],
                concepts={content_hash(r): c for r, c in rows})


def _rule(**over) -> StateChangeRuleV1:
    base = dict(column_ref=_FLAG, from_values=("P",), to_values=("N",))
    header = TargetHeaderV1(name="tgt_npe_90d", entity="customer", anchor_catalog=SOURCE,
                            grain_ref=_GRAIN, as_of_ref=_ASOF, window_days=90,
                            label_type="binary", operator=">=", threshold=1.0)
    return StateChangeRuleV1(header=header, **{**base, **over})


def test_a_rule_whose_refs_all_resolve_is_accepted(db):
    _catalog(db)
    assert check_target_against_catalog(db, _rule(), roles=("data_owner",)) == ()


def test_an_UNRESOLVABLE_ref_is_refused_and_named(db):
    """An invented ref is rejected, never repaired — a rule pointing at a column that is not
    there computes nothing, silently."""
    _catalog(db)
    reasons = check_target_against_catalog(
        db, _rule(column_ref="public.customers.does_not_exist"), roles=("data_owner",))
    assert any("does_not_exist" in r for r in reasons)


def test_a_ref_the_caller_CANNOT_READ_is_refused(db):
    """Read-scope holds here exactly as it does everywhere else: a label must not be definable
    over a column its author cannot see."""
    _catalog(db)
    db.execute("UPDATE graph_node SET sensitivity = 'pii' WHERE object_ref = %s", (_FLAG,))
    reasons = check_target_against_catalog(db, _rule(), roles=())
    assert any("perf_flg" in r for r in reasons)


def test_a_ref_that_exists_in_ANOTHER_catalog_does_not_resolve_here(db):
    """M3, guarded. A rule declaring `anchor_catalog` must not be satisfied by a same-named column
    sitting in a different catalog — that is the defect `_column_meta` is pair-scoped to avoid."""
    _catalog(db)
    elsewhere = StateChangeRuleV1(
        header=TargetHeaderV1(name="tgt_npe_90d", entity="customer",
                              anchor_catalog="a_catalog_that_does_not_hold_these",
                              grain_ref=_GRAIN, as_of_ref=_ASOF, window_days=90,
                              label_type="binary", operator=">=", threshold=1.0),
        column_ref=_FLAG, from_values=("P",), to_values=("N",))
    reasons = check_target_against_catalog(db, elsewhere, roles=("data_owner",))
    assert len(reasons) == 3, "all three refs are absent from that catalog"


def test_the_as_of_ref_must_actually_be_an_as_of_column(db):
    """`as_of_ref` carries the append-only assumption the whole state_change shape rests on.
    Pointing it at an ordinary column makes the rule quietly wrong rather than refused."""
    _catalog(db)
    bad = StateChangeRuleV1(
        header=TargetHeaderV1(name="tgt_npe_90d", entity="customer", anchor_catalog=SOURCE,
                              grain_ref=_GRAIN, as_of_ref=_ASOF, window_days=90,
                              label_type="binary", operator=">=", threshold=1.0),
        column_ref=_GRAIN, from_values=("P",), to_values=("N",))
    # `cust_num` is not an as-of column, so pointing the ANCHOR at it must be refused. (The
    # contract already refuses as_of_ref == column_ref, so the two checks do not overlap.)
    bad_anchor = StateChangeRuleV1(
        header=TargetHeaderV1(name="tgt_npe_90d", entity="customer", anchor_catalog=SOURCE,
                              grain_ref=_GRAIN, as_of_ref=_GRAIN, window_days=90,
                              label_type="binary", operator=">=", threshold=1.0),
        column_ref=_FLAG, from_values=("P",), to_values=("N",))
    reasons = check_target_against_catalog(db, bad_anchor, roles=("data_owner",))
    assert any("as_of" in r for r in reasons)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/test_target_catalog_check.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'featuregen.overlay.upload.target_catalog_check'`

- [ ] **Step 3: Write minimal implementation**

```python
"""Catalog resolution for a target rule.

Deliberately separate from `target_contract`: the contract is a pure unit testable without a
database, and these are the checks that genuinely need one. Returns REASONS rather than raising,
because the authoring conversation shows them all at once rather than one per round trip.
"""
from __future__ import annotations

from collections.abc import Iterable

from featuregen.overlay.upload.read_scope import allowed_sensitivities
from featuregen.overlay.upload.target_contract import TargetRuleV1, refs_read


def check_target_against_catalog(conn, rule: TargetRuleV1, *,
                                 roles: Iterable[str] = ()) -> tuple[str, ...]:
    """Every reason this rule cannot be registered against this catalog; empty when it resolves."""
    reasons: list[str] = []
    wanted = refs_read(rule)
    # Matched on the (catalog_source, object_ref) PAIR, never on the ref alone. `object_ref` is
    # only `public.{table}.{column}`, so a bare match lets a same-named column in another catalog
    # answer for this one — the M3 defect `_column_meta` is scoped to avoid.
    rows = conn.execute(
        "SELECT catalog_source, object_ref, is_as_of FROM graph_node WHERE kind = 'column'"
        " AND (catalog_source, object_ref) = ANY(%s) AND visible_requires <@ %s",
        (list(wanted), allowed_sensitivities(roles))).fetchall()
    visible = {(catalog, ref): is_as_of for catalog, ref, is_as_of in rows}

    for pair in wanted:
        if pair not in visible:
            catalog, ref = pair
            reasons.append(
                f"{ref} does not resolve to a readable column in catalog {catalog}")

    as_of = (rule.header.anchor_catalog, rule.header.as_of_ref)
    if as_of in visible and not visible[as_of]:
        reasons.append(
            f"as_of_ref {as_of[1]} is not an as-of column — the label's anchor date must be one, "
            "or the window is measured from something that does not move with time")
    return tuple(reasons)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/featuregen/overlay/upload/test_target_catalog_check.py -v`
Expected: PASS — 4 tests.

- [ ] **Step 5: Run the full suite and both linters**

```bash
uv run pytest -q -p no:randomly
uv run ruff check src/ tests/
```

Expected: the suite green with 39 more tests than the pre-plan baseline; ruff unchanged from baseline (56 pre-existing errors — do not "fix" them here).

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat(target): catalog resolution — refs resolve, read-scope holds, as_of is really an as-of"
```

---

### Task 7: The spec's own worked examples must construct

**Files:**
- Test: `tests/featuregen/overlay/upload/test_target_spec_examples.py`

**Interfaces:**
- Consumes: everything from Tasks 1–5. Produces nothing.

**Why this task exists.** Spec §7.4 names four labels the design claims are expressible. Nothing so
far proves any of them can actually be built — the unit tests all use one hand-rolled fixture, which
proves the contract accepts *itself*. This is the test that catches a contract which cannot express
what its own spec advertises, and it is the one a reviewer should insist on.

- [ ] **Step 1: Write the failing test**

```python
"""Spec §7.4's four worked examples, constructed. A contract that cannot build the labels its own
design advertises is the failure this file exists to catch."""
from __future__ import annotations

from featuregen.overlay.upload.target_contract import (
    EventWindowRuleV1, StateChangeRuleV1, TargetHeaderV1, refs_read,
)
from featuregen.overlay.upload.target_store import register_target, targets_for_entity

CIB_GRAIN = "public.bo_cib_customer.cust_num"
CIB_ASOF = "public.bo_cib_customer.business_dt"
FTR_TABLE = "public.comp_financial_tran_repos_dly"


def _header(name: str, window: int, label_type: str = "binary") -> TargetHeaderV1:
    thresholded = label_type == "binary"
    return TargetHeaderV1(
        name=name, entity="customer", anchor_catalog="cib",
        grain_ref=CIB_GRAIN, as_of_ref=CIB_ASOF, window_days=window,
        label_type=label_type,
        operator=">=" if thresholded else None,
        threshold=1.0 if thresholded else None)


def _tgt_npe_90d() -> StateChangeRuleV1:
    # Values are ILLUSTRATIVE: nothing profiles this varchar(20), which is why the authoring
    # conversation asks rather than guesses (spec §11).
    return StateChangeRuleV1(
        header=_header("tgt_npe_90d", 90),
        column_ref="public.bo_cib_customer.cust_perf_nonperf_flg",
        from_values=("Performing",), to_values=("Non-performing",))


def _tgt_restricted_90d() -> StateChangeRuleV1:
    return StateChangeRuleV1(
        header=_header("tgt_restricted_90d", 90),
        column_ref="public.bo_cib_customer.cust_susp_flg",
        from_values=("N",), to_values=("Y",))


def _tgt_churned_90d() -> EventWindowRuleV1:
    """Zero rows in the window — "no transaction for 90 days", the churn definition the spec opens
    with and the one nothing in the platform could express."""
    return EventWindowRuleV1(
        header=_header("tgt_churned_90d", 90), event_catalog="ftr", event_table=FTR_TABLE,
        event_date_ref=f"{FTR_TABLE}.pstd_date", join_left=CIB_GRAIN,
        join_right=f"{FTR_TABLE}.cif_id", aggregate="count")


def _tgt_fx_active_90d() -> EventWindowRuleV1:
    return EventWindowRuleV1(
        header=_header("tgt_fx_active_90d", 90), event_catalog="ftr", event_table=FTR_TABLE,
        event_date_ref=f"{FTR_TABLE}.pstd_date", join_left=CIB_GRAIN,
        join_right=f"{FTR_TABLE}.cif_id",
        event_filter="tran_crncy <> 'AED'", aggregate="count")


def test_all_four_spec_examples_construct():
    for rule in (_tgt_npe_90d(), _tgt_restricted_90d(),
                 _tgt_churned_90d(), _tgt_fx_active_90d()):
        assert rule.header.direction == "forward"


def test_the_churn_label_needs_a_zero_threshold_to_mean_NO_activity():
    """"Churned" is the ABSENCE of events. `count == 0` — the one example where the threshold is
    not `>= 1`, and the one most likely to be written the wrong way round."""
    rule = EventWindowRuleV1(
        header=TargetHeaderV1(
            name="tgt_churned_90d", entity="customer", anchor_catalog="cib",
            grain_ref=CIB_GRAIN, as_of_ref=CIB_ASOF, window_days=90,
            label_type="binary", operator="==", threshold=0.0),
        event_catalog="ftr", event_table=FTR_TABLE,
        event_date_ref=f"{FTR_TABLE}.pstd_date", join_left=CIB_GRAIN,
        join_right=f"{FTR_TABLE}.cif_id", aggregate="count")
    assert (rule.header.operator, rule.header.threshold) == ("==", 0.0)


def test_a_cross_catalog_example_reads_BOTH_catalogs():
    """The event shape spans catalogs by construction — anchored in `cib`, counting in `ftr`."""
    catalogs = {catalog for catalog, _ in refs_read(_tgt_churned_90d())}
    assert catalogs == {"cib", "ftr"}


def test_the_four_examples_register_and_are_all_findable_for_the_entity(db):
    for rule in (_tgt_npe_90d(), _tgt_restricted_90d(),
                 _tgt_churned_90d(), _tgt_fx_active_90d()):
        register_target(db, rule, description="spec example", registered_by="user:tester")
    assert {t["name"] for t in targets_for_entity(db, "customer")} == {
        "tgt_npe_90d", "tgt_restricted_90d", "tgt_churned_90d", "tgt_fx_active_90d"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/test_target_spec_examples.py -v`
Expected: FAIL before Tasks 1–5 exist; after them it should PASS. **If any example cannot be
constructed, that is a contract defect, not a test defect — fix the contract.**

- [ ] **Step 3: Run the full suite and the linter**

```bash
uv run pytest -q -p no:randomly
uv run ruff check src/ tests/
```

Expected: green; ruff unchanged from the pre-plan baseline (56 pre-existing errors — do not "fix"
them here).

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "test(target): the spec's four worked examples construct and register"
```

---

## What this plan deliberately does NOT build

Each is a real requirement of the spec, deferred to a second plan that depends on this one.

- **The conversational authoring flow** (spec §7.5) — the dialogue, the registry-reuse nudge, the transcript-as-provenance. It needs an LLM seam and a dialogue state machine, and it is the larger half.
- **Join-entity agreement** — that `join_left` and `join_right` share an entity. `graph_node.entity` is populated on only 8 of `cib`'s 111 columns and 15 of `ftr`'s 126, so the check would abstain more often than it fires. It belongs with the conversation, which can ask.
- **The API and UI surface.**
- **`target_consumer` writes** — the table exists; nothing links a run to a label until the authoring flow does.
- **Anything that executes a rule.** Spec §4.

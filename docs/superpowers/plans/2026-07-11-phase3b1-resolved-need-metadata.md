# Phase 3B.1 — Resolved Recipe Binding Metadata Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every recipe need three governed binding facts — an allowed-source-grain *constraint*, a join role, and a temporal role — derived once into an immutable, versioned resolved view, so the (later) cross-catalog planner reads stable metadata that replays exactly.

**Architecture:** Two new optional fields' worth of contract on `Need`/`Template` (leaf enums in a new `binding_roles.py`), plus a pure derivation module `need_metadata.py` that resolves each need from GOVERNED metadata — `concept.entity_link` for the grain constraint, `concept.pit_role` for the temporal role, and the template's *explicit* anchor for the join role (never a column name, never a need's tuple position). The resolved registry is computed once at import over all 153 recipes; nothing consumes it yet (3B.3 does), so 3B.1 is behaviour-neutral with no flag.

**Tech Stack:** Python 3.11, `@dataclass(frozen=True, slots=True)`, `StrEnum`, `uv run pytest`/`ruff`/`mypy`. No DB, no migration.

## Global Constraints

- **Behaviour-neutral, no flag.** Nothing reads the new fields until the 3B.3 planner. The existing grounding path (`ground_template`) and all existing template suites must stay green, unchanged.
- **Grain is a CONSTRAINT, not an assertion.** The need declares `allowed_source_grains` (`()` = unconstrained); the *actual* grain is derived later (3B.3) from the bound object. `concept.entity_link` is the join-KEY entity, never treated as the object's actual grain.
- **Anchor NEVER from tuple position.** The join role's `SOURCE_ENTITY_KEY` comes from the template's explicit `source_entity_need_role`, or from the single entity-linked need when unambiguous — never "the first entity need."
- **Temporal from GOVERNED metadata, never name-sniffing.** `temporal_role` derives from `Concept.pit_role` (the existing per-concept temporal semantics), via a fixed map. No column/concept name inspection.
- **Reject ambiguous anchors.** A recipe with >1 *distinct* entity-linked need and no explicit `source_entity_need_role` is rejected (fail-fast at import).
- **Resolve once, version it.** `RESOLVED_NEED_METADATA` is computed once at import and immutable; `NEED_METADATA_VERSION = "1.0.0"`. No runtime rederivation.
- **No per-need `target_grain`** (that's the confirmed scope's `target_entity`). **No per-need `unit`/`currency`** (the existing gauntlet handles mixed units/currency).
- Commit messages end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Branch `feature/phase3b-cross-catalog-binding` is already checked out.

## File Structure

- **Create** `src/featuregen/overlay/upload/binding_roles.py` — the two leaf enums `JoinRole`, `TemporalRole` (no deps). [Task 1]
- **Modify** `src/featuregen/overlay/upload/templates.py` — `Need` gains `allowed_source_grains`/`join_role`/`temporal_role`; `Template` gains `source_entity`/`source_entity_need_role`; import the enums. [Task 2]
- **Create** `src/featuregen/overlay/upload/need_metadata.py` — `ResolvedNeedMetadataV1`, `validate_template_anchor`, `derive_need_metadata`, the `pit_role`→`TemporalRole` map, the resolved registry + version + report. [Tasks 3, 4]
- **Create** tests `tests/featuregen/overlay/upload/test_binding_roles.py` [Task 1], `test_need_metadata.py` [Tasks 3, 4]; extend an existing templates test for the contract fields [Task 2].

Import DAG (no cycles): `binding_roles` (leaf) ← `templates` ← `need_metadata`; `concepts` (leaf) ← `need_metadata`.

---

### Task 1: Governed join/temporal role enums

**Files:**
- Create: `src/featuregen/overlay/upload/binding_roles.py`
- Test: `tests/featuregen/overlay/upload/test_binding_roles.py`

**Interfaces:**
- Produces: `JoinRole` (`SOURCE_ENTITY_KEY`/`TARGET_ENTITY_KEY`/`INTERMEDIATE_ENTITY_KEY`/`MEASURE`/`TIME`), `TemporalRole` (`NONE`/`EVENT_TIME`/`AS_OF_TIME`/`INGESTION_TIME`/`VALID_FROM`/`VALID_TO`).

- [ ] **Step 1: Write the failing test**

Create `tests/featuregen/overlay/upload/test_binding_roles.py`:

```python
"""Phase-3B.1 Task 1 — the governed join/temporal role vocabularies (leaf enums)."""
from __future__ import annotations

from featuregen.overlay.upload.binding_roles import JoinRole, TemporalRole


def test_join_role_members():
    assert {r.value for r in JoinRole} == {
        "source_entity_key", "target_entity_key", "intermediate_entity_key", "measure", "time"}


def test_temporal_role_members():
    assert {r.value for r in TemporalRole} == {
        "none", "event_time", "as_of_time", "ingestion_time", "valid_from", "valid_to"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/test_binding_roles.py -q`
Expected: FAIL — `ModuleNotFoundError: ... binding_roles`.

- [ ] **Step 3: Write the implementation**

Create `src/featuregen/overlay/upload/binding_roles.py`:

```python
"""Phase-3B.1 — the governed vocabularies for a recipe need's cross-catalog binding role.

Leaf module (no deps) so ``templates.Need`` can type its fields on these without an import cycle. The
role of a need is GOVERNED metadata, never inferred from a column name or a need's tuple position."""
from __future__ import annotations

from enum import StrEnum


class JoinRole(StrEnum):
    """What a need contributes to a cross-catalog join. ``SOURCE_ENTITY_KEY`` fixes the recipe's source
    grain; ``TARGET_ENTITY_KEY`` is the grain the plan rolls up to (the confirmed scope's target — not a
    need at authoring time, reserved); ``INTERMEDIATE_ENTITY_KEY`` is a hop key from an intermediate
    catalog; ``MEASURE`` is a value carried/aggregated to the target grain; ``TIME`` is a timestamp for
    the window / as-of."""

    SOURCE_ENTITY_KEY = "source_entity_key"
    TARGET_ENTITY_KEY = "target_entity_key"
    INTERMEDIATE_ENTITY_KEY = "intermediate_entity_key"
    MEASURE = "measure"
    TIME = "time"


class TemporalRole(StrEnum):
    """A need's temporal semantics, derived from the concept's governed ``pit_role``. ``VALID_TO`` has no
    current ``pit_role`` source (reserved for a future bitemporal concept)."""

    NONE = "none"
    EVENT_TIME = "event_time"
    AS_OF_TIME = "as_of_time"
    INGESTION_TIME = "ingestion_time"
    VALID_FROM = "valid_from"
    VALID_TO = "valid_to"
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/featuregen/overlay/upload/test_binding_roles.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Lint + type-check + commit**

```bash
uv run ruff check src/featuregen/overlay/upload/binding_roles.py tests/featuregen/overlay/upload/test_binding_roles.py
uv run mypy src/featuregen/overlay/upload/binding_roles.py
git add src/featuregen/overlay/upload/binding_roles.py tests/featuregen/overlay/upload/test_binding_roles.py
git commit -m "feat(3b1): governed join/temporal role enums (task 1)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: The contract fields on `Need` and `Template`

**Files:**
- Modify: `src/featuregen/overlay/upload/templates.py` (the `Need` dataclass ~line 49, the `Template` dataclass ~line 57, and the import block)
- Test: `tests/featuregen/overlay/upload/test_binding_roles.py` (append)

**Interfaces:**
- Consumes: `JoinRole`, `TemporalRole` (Task 1).
- Produces: `Need.allowed_source_grains: tuple[str, ...]`, `Need.join_role: JoinRole | None`, `Need.temporal_role: TemporalRole | None`; `Template.source_entity: str | None`, `Template.source_entity_need_role: str | None`. All default to keep the existing positional constructors unchanged.

- [ ] **Step 1: Write the failing test**

Append to `tests/featuregen/overlay/upload/test_binding_roles.py`:

```python
from featuregen.overlay.upload.templates import ALL_TEMPLATES, Need, Template


def test_existing_need_construction_unchanged():
    # the pre-3B.1 positional constructor still works and the new fields default empty/None
    n = Need("entity", "customer_id")
    assert n.allowed_source_grains == ()
    assert n.join_role is None and n.temporal_role is None


def test_need_accepts_explicit_binding_overrides():
    n = Need("stock", "monetary_stock", allowed_source_grains=("account",), join_role=JoinRole.MEASURE)
    assert n.allowed_source_grains == ("account",)
    assert n.join_role is JoinRole.MEASURE


def test_template_gains_anchor_fields_defaulting_none():
    t = ALL_TEMPLATES[0]
    assert t.source_entity is None or isinstance(t.source_entity, str)
    assert t.source_entity_need_role is None or isinstance(t.source_entity_need_role, str)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/test_binding_roles.py -q`
Expected: FAIL — `TypeError: Need.__init__() got an unexpected keyword argument 'allowed_source_grains'` (and the `source_entity` attribute missing).

- [ ] **Step 3: Modify `templates.py`**

(a) Add to the import block near the top of `templates.py`:

```python
from featuregen.overlay.upload.binding_roles import JoinRole, TemporalRole
```

(b) Replace the `Need` dataclass body — keep the three existing fields, add three optional ones:

```python
@dataclass(frozen=True, slots=True)
class Need:
    """One binding slot of a template — a required (or optional) concept the grounding engine must find a
    column for. ``concept`` is a NAME that must exist in ``CONCEPT_REGISTRY`` (validated at import)."""
    role: str            # binding slot, e.g. "stock_col", "asof", "entity", "flow_col", "event_ts"
    concept: str         # required concept NAME (must exist in CONCEPT_REGISTRY)
    optional: bool = False
    # ── 3B.1 cross-catalog binding metadata (optional; need_metadata derives the unset ones) ──
    allowed_source_grains: tuple[str, ...] = ()   # acceptable source grains; () = unconstrained
    join_role: JoinRole | None = None             # explicit override; None -> derived (NEVER tuple position)
    temporal_role: TemporalRole | None = None     # explicit override; None -> derived from concept.pit_role
```

(c) In the `Template` dataclass, add two fields at the END of the field list (after `notes`), so the core positional schema is unchanged:

```python
    # ── 3B.1 explicit source anchor (optional; needed only when >1 distinct entity-linked need) ──
    source_entity: str | None = None            # the recipe's source grain entity (derived when unambiguous)
    source_entity_need_role: str | None = None   # which need carries the source key
```

- [ ] **Step 4: Run the new test + the FULL existing template suites (behaviour-neutral proof)**

Run: `uv run pytest tests/featuregen/overlay/upload/test_binding_roles.py tests/featuregen/overlay/upload/test_templates.py tests/featuregen/overlay/upload/test_templates_core3.py tests/featuregen/overlay/upload/test_templates_credit.py tests/featuregen/overlay/upload/test_templates_crime.py tests/featuregen/overlay/upload/test_templates_growth_trade.py tests/featuregen/overlay/upload/test_templates_markets.py tests/featuregen/overlay/upload/test_templates_specialist.py -q`
Expected: PASS — the new fields are additive; every existing template test stays green unchanged. If any existing test fails, the field additions were not purely additive — stop and diagnose (do NOT edit the existing tests).

- [ ] **Step 5: Lint + type-check + commit**

```bash
uv run ruff check src/featuregen/overlay/upload/templates.py tests/featuregen/overlay/upload/test_binding_roles.py
uv run mypy src/featuregen/overlay/upload/templates.py
git add src/featuregen/overlay/upload/templates.py tests/featuregen/overlay/upload/test_binding_roles.py
git commit -m "feat(3b1): additive Need/Template cross-catalog binding fields (task 2)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Derivation + anchor validation

**Files:**
- Create: `src/featuregen/overlay/upload/need_metadata.py`
- Test: `tests/featuregen/overlay/upload/test_need_metadata.py`

**Interfaces:**
- Consumes: `JoinRole`/`TemporalRole` (Task 1); `Need`/`Template` (Task 2); `from featuregen.overlay.upload.concepts import concept` (`(name) -> Concept | None`, where `Concept` has `.entity_link: str | None` and `.pit_role: str`).
- Produces: `ResolvedNeedMetadataV1`; `validate_template_anchor(template) -> None` (raises `ValueError`); `derive_need_metadata(template) -> tuple[ResolvedNeedMetadataV1, ...]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/featuregen/overlay/upload/test_need_metadata.py`:

```python
"""Phase-3B.1 Tasks 3/4 — derive governed per-need binding metadata (grain constraint / join role /
temporal role) from concept.entity_link, concept.pit_role, and the EXPLICIT template anchor."""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.binding_roles import JoinRole, TemporalRole
from featuregen.overlay.upload.need_metadata import (
    derive_need_metadata,
    validate_template_anchor,
)
from featuregen.overlay.upload.templates import Need, Template


def _t(needs, **over) -> Template:
    base = dict(id="t", family="f", intent="i", needs=tuple(needs), params={}, aggregation="avg",
                additivity="additive", explain="M", use_cases=(), pit="trailing window")
    base.update(over)
    return Template(**base)


def test_identifier_need_grain_constrained_and_is_source_anchor():
    # a single entity-linked need is the unambiguous source anchor; its grain is constrained to its entity
    t = _t([Need("entity", "customer_id")])
    (m,) = derive_need_metadata(t)
    assert m.allowed_source_grains == ("customer",)          # from concept.entity_link (customer_id -> customer)
    assert m.join_role is JoinRole.SOURCE_ENTITY_KEY
    assert m.grain_source == "concept_registry"


def test_measure_need_is_grain_unconstrained_and_measure_role():
    # a non-identifier measure (monetary_stock has no entity_link) -> unconstrained grain, MEASURE role
    t = _t([Need("entity", "customer_id"), Need("stock", "monetary_stock")])
    metas = {m.role: m for m in derive_need_metadata(t)}
    assert metas["stock"].allowed_source_grains == ()         # unconstrained; actual grain derived at bind time
    assert metas["stock"].join_role is JoinRole.MEASURE


def test_temporal_role_from_pit_role_not_name():
    # temporal role comes from concept.pit_role (governed), never the column/concept name.
    # event_timestamp has pit_role 'event'; customer_id has pit_role 'none'.
    t = _t([Need("entity", "customer_id"), Need("event_ts", "event_timestamp")])
    metas = {m.role: m for m in derive_need_metadata(t)}
    assert metas["event_ts"].temporal_role is TemporalRole.EVENT_TIME
    assert metas["event_ts"].join_role is JoinRole.TIME
    assert metas["entity"].temporal_role is TemporalRole.NONE


def test_multi_distinct_entity_without_anchor_is_rejected():
    t = _t([Need("cust", "customer_id"), Need("acct", "account_id")])   # two distinct entity keys, no anchor
    with pytest.raises(ValueError, match="distinct entity keys"):
        validate_template_anchor(t)


def test_explicit_anchor_resolves_multi_entity_recipe():
    t = _t([Need("cust", "customer_id"), Need("acct", "account_id")],
           source_entity_need_role="acct")
    metas = {m.role: m for m in derive_need_metadata(t)}          # no raise
    assert metas["acct"].join_role is JoinRole.SOURCE_ENTITY_KEY
    assert metas["cust"].join_role is JoinRole.INTERMEDIATE_ENTITY_KEY


def test_anchor_role_must_name_an_entity_need():
    t = _t([Need("cust", "customer_id"), Need("acct", "account_id")],
           source_entity_need_role="balance")                    # not an entity-linked need
    with pytest.raises(ValueError, match="not an entity-linked need"):
        validate_template_anchor(t)


def test_explicit_field_overrides_win():
    t = _t([Need("entity", "customer_id",
                 allowed_source_grains=("account",), temporal_role=TemporalRole.AS_OF_TIME)])
    (m,) = derive_need_metadata(t)
    assert m.allowed_source_grains == ("account",) and m.grain_source == "explicit_recipe"
    assert m.temporal_role is TemporalRole.AS_OF_TIME and m.temporal_role_source == "explicit_recipe"
```

(These concept names are real: `monetary_stock` is a `monetary` concept with no `entity_link`; `event_timestamp` has `pit_role="event"`; `customer_id`/`account_id` are identifiers with `entity_link`.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/test_need_metadata.py -q`
Expected: FAIL — `ModuleNotFoundError: ... need_metadata`.

- [ ] **Step 3: Write the implementation** (registry + report come in Task 4)

Create `src/featuregen/overlay/upload/need_metadata.py`:

```python
"""Phase-3B.1 — resolved, versioned per-need binding metadata.

Derives each recipe need's cross-catalog binding facts from GOVERNED metadata — the source-grain
CONSTRAINT (concept.entity_link), the temporal role (concept.pit_role), and the join role (the
template's EXPLICIT anchor) — never a column name or a need's tuple position. Resolved once + versioned
so a plan replays exactly. Behaviour-neutral: nothing consumes this until the 3B.3 planner."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from featuregen.overlay.upload.binding_roles import JoinRole, TemporalRole
from featuregen.overlay.upload.concepts import concept
from featuregen.overlay.upload.templates import Need, Template

NEED_METADATA_VERSION = "1.0.0"

# concept.pit_role -> the governed TemporalRole. pit_role IS the per-concept temporal semantics;
# 'maturity' is a business future-date (not a binding temporal anchor) -> NONE.
_PIT_ROLE_TO_TEMPORAL: dict[str, TemporalRole] = {
    "none": TemporalRole.NONE,
    "event": TemporalRole.EVENT_TIME,
    "as_of": TemporalRole.AS_OF_TIME,
    "system_time": TemporalRole.INGESTION_TIME,
    "effective": TemporalRole.VALID_FROM,
    "valid_time": TemporalRole.VALID_FROM,
    "maturity": TemporalRole.NONE,
}

DerivationSource = Literal["explicit_recipe", "concept_registry", "template_default"]


@dataclass(frozen=True, slots=True)
class ResolvedNeedMetadataV1:
    """One need's resolved binding metadata + where each field came from. Immutable; the planner reads it."""
    role: str
    concept: str
    allowed_source_grains: tuple[str, ...]
    join_role: JoinRole
    temporal_role: TemporalRole
    grain_source: DerivationSource
    join_role_source: DerivationSource
    temporal_role_source: DerivationSource


def _entity_of(need: Need) -> str | None:
    c = concept(need.concept)
    return c.entity_link if c is not None else None


def validate_template_anchor(template: Template) -> None:
    """Raise ``ValueError`` on an ambiguous source anchor: >1 DISTINCT entity-linked need and no explicit
    ``source_entity_need_role`` (0 or 1 distinct entity key is unambiguous). If the anchor role is set, it
    must name an entity-linked need."""
    entity_needs = [n for n in template.needs if _entity_of(n) is not None]
    distinct = {_entity_of(n) for n in entity_needs}
    if len(distinct) <= 1:
        return
    if template.source_entity_need_role is None:
        raise ValueError(
            f"template {template.id!r}: {len(distinct)} distinct entity keys "
            f"({sorted(str(e) for e in distinct)}) but no source_entity_need_role")
    if template.source_entity_need_role not in {n.role for n in entity_needs}:
        raise ValueError(
            f"template {template.id!r}: source_entity_need_role "
            f"{template.source_entity_need_role!r} is not an entity-linked need")


def _source_anchor_role(template: Template) -> str | None:
    """The need role carrying the source grain: the explicit override, else the single entity-linked need."""
    if template.source_entity_need_role is not None:
        return template.source_entity_need_role
    entity_needs = [n for n in template.needs if _entity_of(n) is not None]
    return entity_needs[0].role if len(entity_needs) == 1 else None


def _derive_one(template: Template, need: Need, anchor_role: str | None) -> ResolvedNeedMetadataV1:
    c = concept(need.concept)
    entity_link = c.entity_link if c is not None else None
    pit_role = c.pit_role if c is not None else "none"

    if need.allowed_source_grains:
        grains: tuple[str, ...] = need.allowed_source_grains
        grain_source: DerivationSource = "explicit_recipe"
    elif entity_link is not None:
        grains, grain_source = (entity_link,), "concept_registry"
    else:
        grains, grain_source = (), "template_default"

    if need.join_role is not None:
        join_role, jr_source = need.join_role, "explicit_recipe"
    elif need.role == anchor_role:
        join_role, jr_source = JoinRole.SOURCE_ENTITY_KEY, "template_default"
    elif entity_link is not None:
        join_role, jr_source = JoinRole.INTERMEDIATE_ENTITY_KEY, "concept_registry"
    elif pit_role != "none":
        join_role, jr_source = JoinRole.TIME, "concept_registry"
    else:
        join_role, jr_source = JoinRole.MEASURE, "template_default"

    if need.temporal_role is not None:
        temporal_role, tr_source = need.temporal_role, "explicit_recipe"
    else:
        temporal_role = _PIT_ROLE_TO_TEMPORAL.get(pit_role, TemporalRole.NONE)
        tr_source = "concept_registry"

    return ResolvedNeedMetadataV1(
        role=need.role, concept=need.concept, allowed_source_grains=grains,
        join_role=join_role, temporal_role=temporal_role,
        grain_source=grain_source, join_role_source=jr_source, temporal_role_source=tr_source)


def derive_need_metadata(template: Template) -> tuple[ResolvedNeedMetadataV1, ...]:
    """Resolve every need of a template. Raises (via ``validate_template_anchor``) on an ambiguous anchor."""
    validate_template_anchor(template)
    anchor = _source_anchor_role(template)
    return tuple(_derive_one(template, n, anchor) for n in template.needs)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/featuregen/overlay/upload/test_need_metadata.py -q`
Expected: PASS (7 tests).

- [ ] **Step 5: Lint + type-check + commit**

```bash
uv run ruff check src/featuregen/overlay/upload/need_metadata.py tests/featuregen/overlay/upload/test_need_metadata.py
uv run mypy src/featuregen/overlay/upload/need_metadata.py
git add src/featuregen/overlay/upload/need_metadata.py tests/featuregen/overlay/upload/test_need_metadata.py
git commit -m "feat(3b1): governed need-metadata derivation + anchor validation (task 3)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Resolved registry over all 153 recipes + report + anchor migration

**Files:**
- Modify: `src/featuregen/overlay/upload/need_metadata.py` (append the registry + report)
- Modify: `src/featuregen/overlay/upload/templates.py` (ONLY if a multi-distinct-entity recipe needs an explicit `source_entity_need_role` — see Step 3)
- Test: `tests/featuregen/overlay/upload/test_need_metadata.py` (append)

**Interfaces:**
- Consumes: `derive_need_metadata`, `ALL_TEMPLATES`.
- Produces: `RESOLVED_NEED_METADATA: dict[str, tuple[ResolvedNeedMetadataV1, ...]]` (computed once, keyed by template id), `derivation_report() -> tuple[dict, ...]`, `NEED_METADATA_VERSION`.

- [ ] **Step 1: Write the failing coverage test**

Append to `tests/featuregen/overlay/upload/test_need_metadata.py`:

```python
from featuregen.overlay.upload.need_metadata import (
    NEED_METADATA_VERSION,
    RESOLVED_NEED_METADATA,
    derivation_report,
)
from featuregen.overlay.upload.templates import ALL_TEMPLATES


def test_every_recipe_resolves_and_validates():
    # the load-bearing coverage gate: all 153 recipes have resolved metadata for every need.
    assert set(RESOLVED_NEED_METADATA) == {t.id for t in ALL_TEMPLATES}
    for t in ALL_TEMPLATES:
        assert len(RESOLVED_NEED_METADATA[t.id]) == len(t.needs)


def test_exactly_one_source_anchor_per_recipe_at_most():
    # a recipe with entity needs resolves at most one SOURCE_ENTITY_KEY (never several via tuple position).
    for tid, metas in RESOLVED_NEED_METADATA.items():
        anchors = [m for m in metas if m.join_role.value == "source_entity_key"]
        assert len(anchors) <= 1, f"{tid} has {len(anchors)} source anchors"


def test_derivation_report_covers_every_need_with_a_source():
    rows = derivation_report()
    assert len(rows) == sum(len(t.needs) for t in ALL_TEMPLATES)
    for row in rows:
        assert row["grain_source"] in ("explicit_recipe", "concept_registry", "template_default")
        assert row["join_role_source"] in ("explicit_recipe", "concept_registry", "template_default")
        assert row["temporal_role_source"] in ("explicit_recipe", "concept_registry", "template_default")


def test_version_stamped():
    assert NEED_METADATA_VERSION == "1.0.0"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/test_need_metadata.py -q`
Expected: FAIL — `ImportError: cannot import name 'RESOLVED_NEED_METADATA' / 'derivation_report'`. (Importing may instead raise `ValueError` at module load if a recipe has an ambiguous anchor — that is the signal for Step 3.)

- [ ] **Step 3: Append the registry + report; fix any ambiguous-anchor recipe**

Append to `src/featuregen/overlay/upload/need_metadata.py`:

```python
from featuregen.overlay.upload.templates import ALL_TEMPLATES

# Resolved ONCE at import over the whole recipe corpus — the immutable registry the 3B.3 planner reads.
# Fails FAST (at import) if any recipe's source anchor is ambiguous.
RESOLVED_NEED_METADATA: dict[str, tuple[ResolvedNeedMetadataV1, ...]] = {
    t.id: derive_need_metadata(t) for t in ALL_TEMPLATES}


def derivation_report() -> tuple[dict[str, object], ...]:
    """One row per (template, need): the resolved fields + where each came from — for inspection/audit."""
    return tuple(
        {"template": tid, "role": m.role, "concept": m.concept,
         "allowed_source_grains": m.allowed_source_grains,
         "join_role": m.join_role.value, "temporal_role": m.temporal_role.value,
         "grain_source": m.grain_source, "join_role_source": m.join_role_source,
         "temporal_role_source": m.temporal_role_source}
        for tid, metas in RESOLVED_NEED_METADATA.items() for m in metas)
```

Then run `uv run python -c "import featuregen.overlay.upload.need_metadata"`. It will raise `ValueError: template '<id>': ... distinct entity keys ... but no source_entity_need_role` for the **12 recipes that have >1 distinct entity-linked need**. Each of these 12 has an `entity`-role need that carries its source grain (the other entity need is a related/intermediate entity), so add `source_entity_need_role="entity"` to each of these templates in `templates.py`:

```
external_own_transfer_trend   fan_in_fan_out              crypto_offramp_exposure
prior_alert_recidivism        book_desk_concentration     custody_holding_dynamics
next_best_product_propensity  campaign_response_recency   clv_revenue_trajectory
household_relationship_value  invoice_finance_dynamics    guarantor_reliance
```

(Verified: each of the 12 has an `entity` need whose concept is an identifier — `customer_id`/`account_id`/`book_id`/`household_id`/`obligor_id` — so `"entity"` is the correct source anchor.) Re-run the import until clean; expect exactly these 12 edits, no more.

- [ ] **Step 4: Run the coverage tests + full overlay suite (behaviour-neutral proof)**

```bash
uv run pytest tests/featuregen/overlay/upload/test_need_metadata.py -q
uv run pytest tests/featuregen/overlay/ -q          # nothing regressed; the fields stay dormant
```
Expected: PASS. The overlay suite is byte-identical behaviour (the new metadata is consumed by nothing).

- [ ] **Step 5: Lint + type-check + commit**

```bash
uv run ruff check src/featuregen/overlay/upload/need_metadata.py tests/featuregen/overlay/upload/test_need_metadata.py src/featuregen/overlay/upload/templates.py
uv run mypy src/featuregen/overlay/upload/need_metadata.py
git add src/featuregen/overlay/upload/need_metadata.py tests/featuregen/overlay/upload/test_need_metadata.py src/featuregen/overlay/upload/templates.py
git commit -m "feat(3b1): resolved need-metadata registry over 153 recipes + report (task 4)

Explicit source_entity_need_role='entity' added to the 12 multi-distinct-entity recipes.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Exit criteria mapping

| Spec requirement (3B.1) | Where satisfied |
|---|---|
| `allowed_source_grains`/`join_role`/`temporal_role` on `Need`; `source_entity`/`source_entity_need_role` on `Template` | Task 2 |
| Governed `JoinRole` + `TemporalRole` enums | Task 1 |
| Grain a CONSTRAINT (not concept-entity-as-actual-grain) | Task 3 `test_measure_need_is_grain_unconstrained` + the `allowed_source_grains` derivation |
| Anchor NEVER from tuple position | Task 3 `test_multi_distinct_entity_without_anchor_is_rejected` + `_source_anchor_role` (explicit / single-need only) |
| Temporal from governed `pit_role`, no name-sniffing | Task 3 `test_temporal_role_from_pit_role_not_name` + `_PIT_ROLE_TO_TEMPORAL` |
| Reject ambiguous multi-entity anchor | Task 3 `validate_template_anchor` + Task 4 import-time fail-fast |
| Resolve ONCE, versioned, no runtime rederivation | Task 4 `RESOLVED_NEED_METADATA` (import-time) + `NEED_METADATA_VERSION` |
| Complete derivation report + explicit overrides | Task 4 `derivation_report` + Task 3 `test_explicit_field_overrides_win` |
| All 153 recipes derive + validate | Task 4 `test_every_recipe_resolves_and_validates` |
| Behaviour-neutral, no flag | Tasks 2 + 4 (existing template + overlay suites green unchanged) |
| No per-need `target_grain` / `unit` / `currency` | Not added anywhere (grep the diff) |

## Self-review notes

- **Behaviour-neutral** is proven by the untouched existing template suites (Task 2) + the full overlay suite (Task 4) staying green — the new fields default and nothing reads them.
- **No name-sniffing:** temporal role maps `concept.pit_role`; grain maps `concept.entity_link`; join anchor uses the explicit `source_entity_need_role` (or the single unambiguous entity need). No string/name inspection anywhere.
- **Replay-safe:** `RESOLVED_NEED_METADATA` is computed once at import, immutable, and version-stamped; the 3B.3 planner reads the resolved view, never rederives.
- **Import DAG:** `binding_roles` (leaf) ← `templates` ← `need_metadata`; `concepts` (leaf) ← `need_metadata`. No cycle.

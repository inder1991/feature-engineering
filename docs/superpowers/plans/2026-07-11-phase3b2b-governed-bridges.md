# Phase 3B.2B — Governed Entity Bridges Implementation Plan (v2 — on the overlay_fact spine)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Govern cross-catalog "same entity in two uploads" bridges by (1) discovering candidate bridges from declared identifier metadata and (2) putting them through the EXISTING `overlay_fact` governance engine as a new `entity_bridge` fact type — computed in shadow, consumed by nothing until 3B.3.

**Architecture:** This plan was **re-founded after a reconciliation against `origin/main`** (the Pass C / join-governance work landed a generic, append-only, four-eyes governance engine). Rather than build a parallel sanction system, 3B.2B **reuses that engine**: a bridge is a new governed `fact_type` whose lifecycle (proposed → confirmed → verified, reject, expire→reverify, stale) is the same append-only `overlay_fact` event stream every other fact uses. The only genuinely new code is: the **cross-catalog candidate scan**, a **bridge ref + value schema** (the existing `approved_join` ref *forbids* cross-catalog; a bridge *requires* it), and a **projection into a new `entity_bridge_edge` table** (cross-catalog, because `graph_edge` is intra-catalog-keyed). Every touch of a shared/live governance file is an **additive branch that only fires for the new fact type** — no command handler changes — so existing behaviour is byte-identical.

**Tech Stack:** Python 3.11 (frozen dataclasses, jsonschema value schemas), PostgreSQL (append-only `events`, projections), psycopg, pytest against the ephemeral per-test-rollback PG cluster. `uv run pytest/ruff/mypy`.

## Global Constraints

- **NO data plane.** A bridge is a governed *definition* of cross-catalog entity identity — never a computed row-level match.
- **Behaviour-neutral, no flag.** Nothing consumes bridges until 3B.3; the live permissive path (`find_cross_catalog_path` in `entity.py`, wired at `contract/author.py:88`) is **left untouched** — 3B.2B replaces it in 3C. Every shared-file edit is an **additive branch gated on `fact_type == "entity_bridge"` / `EntityBridgeRef`**; nothing existing produces those, so the full `tests/featuregen/` suite must stay green.
- **Reuse the `overlay_fact` engine, do NOT fork it.** `append_overlay_event`/`load_fact` (`overlay/store.py`), `fold_overlay_state` (`overlay/state.py`), `propose_fact`/`confirm_fact`/`reject_fact` (`overlay/commands.py`), four-eyes `proposer_ne_confirmer` (`overlay/authority.py`), and `write_evidence` (`overlay/evidence.py`) are used **as-is**. A bridge needs NO new event type and NO command-handler change.
- **Single-confirmer four-eyes for 3B.2B; two-owner dual sign-off is 3C.** `resolve_authority` for `entity_bridge` returns a NON-dual authority (a single governance/platform-admin confirmation, proposer≠confirmer). The dual-owner two-step (`_confirm_approved_join`) is NOT forked. Rationale: bridges are shadow (not live-traversable), so a single governed confirmation is a complete lifecycle; two-party-per-owner sign-off matters only when a bridge becomes live (3C).
- **Code/admin path only — NO HTTP routes, NO UI.** Per the spec, "3B provides the code/admin sanction path (no UI — that is 3C)." Propose/confirm/project are callable from code/tests; no `api/routes` changes.
- **Cross-catalog is REQUIRED for a bridge** (the mirror image of `approved_join`, which forbids it). Enforced in the write gate.
- **Next DB migration is `0989`** (the spec's "0977" and the earlier draft's "0986" are BOTH stale — migrations advanced to `0988_pass_c_candidate_evidence.sql`). Migration-ledger tests use relative counts (`>= 40`), so no test churn.
- **Bridges project into a NEW `entity_bridge_edge` table, not `graph_edge`.** `graph_edge` PK is `(catalog_source, kind, from_ref, to_ref)` — intra-catalog. A bridge spans two catalogs, so it needs a cross-catalog-capable projection table (which 3B.3 reads).
- **Discovery is deterministic + read-only + governed via concepts.** Candidate eligibility keys on `concept(name).group == "identifier"` + `entity_link` (NOT the free-text `graph_node.entity` tag), pairs identifier columns for the SAME entity across DISTINCT catalog sources with a COMPATIBLE type family. Read-scoped by `roles` (sensitivity), matching the existing entity primitives. Arbitrary shared-entity coincidence never proposes.
- **LLM proposes, deterministic code + humans dispose.** Discovery + candidates are deterministic code over declared metadata; a candidate is a `PROPOSED` fact, activated only by a human confirmation.
- **Tooling:** `uv run pytest <path> -q`, `uv run ruff check <paths>`, `uv run mypy <path>` from repo root. ruff prefers `collections.abc`, forbids E402 in `src/**` (top-of-file imports) but ignores it for `tests/**`. Commit messages end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

## Reused interfaces (verbatim signatures the tasks consume)

- `overlay/facts.py`: `FACT_VALUE_SCHEMAS: dict[str, dict]` (per-type value schema, `additionalProperties:false`), fact-type string constants (`GRAIN`, `APPROVED_JOIN`, …), `DATA_FACT_TYPES: frozenset`, `_CATALOG_OBJECT_REF_SCHEMA`.
- `overlay/identity.py`: `CatalogObjectRef(catalog_source, object_kind, schema, table, column=None)`; `fact_key(ref, fact_type, use_case=None) -> str` (sha256 of a canonical tuple); `_ref_from_payload(d) -> ref`; `join_write_error(ref, fact_type, value, use_case=None) -> str | None` (the write gate, called by propose/confirm/enter); helpers `_norm`, `_ref_tuple`, `_digest`; `proposal_fingerprint(value) -> str`.
- `overlay/state.py`: `fold_overlay_state(stream) -> OverlayState` (`.status` in `DRAFT|PARTIALLY_CONFIRMED|VERIFIED|REJECTED|STALE|REVERIFY`, `.value`, `.confirmed_event_id`). Generic — does NOT branch on fact_type.
- `overlay/store.py`: `load_fact(conn, fact_key) -> list[EventEnvelope]`; `append_overlay_event(...)` (used indirectly via commands).
- `overlay/authority.py`: `Authority(role, gate, subjects, governance_queue, dual=False)`; `resolve_authority(conn, adapter, ref, fact_type) -> Authority`.
- `overlay/commands.py`: `propose_fact`, `confirm_fact`, `reject_fact` (each `(conn, cmd: Command) -> CommandResult`). `Command(action, aggregate, aggregate_id, args, actor, idempotency_key, expected_version=None)` from `contracts/envelopes.py`.
- `overlay/evidence.py`: `write_evidence(conn, *, fact_key, table_snapshot_at, row_count, sample_size, profile_version, thresholds_used, metric_values, created_by, producer, strength) -> str`; enums `EvidenceProducer.STRUCTURAL_CONNECTOR`, `AssertionStrength.PROPOSED`.
- `overlay/projection.py` + `projections/runner.py`: `run_projection(conn, OverlayProjection())` (drain loop), `projection_lag(conn, "overlay")`.
- `overlay/_lifecycle.py`: `_cas_target(state) -> str` (the confirm CAS target event id).
- `overlay/upload/upload_catalog.py`: `ensure_upload_catalog_adapter()` (registers the upload adapter; `owner_of -> None` → governance queue).
- `overlay/upload/concepts.py`: `concept(name) -> Concept | None` (`Concept.group`, `Concept.entity_link`).
- `overlay/upload/read_scope.py`: `allowed_sensitivities(roles)`.
- `contracts/identity.py`: `identity_to_jsonb(actor)`.
- Test harness (from `tests/featuregen/overlay/upload/passc/conftest.py`): `SERVICE_ACTOR = _ENRICH_ACTOR` (proposer), `mint_test_identity(subject=..., role_claims=("platform-admin",))` (confirmer), the `passc_conn` pattern (`ensure_upload_catalog_adapter()` + the rolled-back `db`), a `_drain(conn)` = `while run_projection(conn, OverlayProjection()) >= 500: pass`.

## File Structure

| File | Responsibility |
|---|---|
| `src/featuregen/overlay/upload/bridge_candidates.py` (CREATE) | The cross-catalog candidate **scan** — `BridgeCandidateV1` + `derive_bridge_candidates`. |
| `src/featuregen/overlay/facts.py` (MODIFY) | Register the `entity_bridge` fact type + its value schema. |
| `src/featuregen/overlay/_types.py` (MODIFY) | Add `"entity_bridge"` to the `FactType` literal. |
| `src/featuregen/overlay/identity.py` (MODIFY) | `EntityBridgeRef` + `fact_key`/`_ref_from_payload` branches + the `_bridge_write_error` write-gate branch. |
| `src/featuregen/overlay/authority.py` (MODIFY) | `resolve_authority` `entity_bridge` branch (single-confirmer). |
| `src/featuregen/db/migrations/0989_entity_bridge_governance.sql` (CREATE) | `entity_bridge_candidate_evidence` ledger + `entity_bridge_edge` projection table. |
| `src/featuregen/overlay/upload/bridge_propose.py` (CREATE) | `propose_bridge` (evidence + `propose_fact`) + ledger stamp. |
| `src/featuregen/overlay/upload/bridge_projection.py` (CREATE) | `project_verified_bridge`, `demote_bridge_edges`, `active_bridges`. |
| Tests under `tests/featuregen/overlay/upload/` | `test_bridge_candidates.py`, `test_entity_bridge_fact.py`, `test_bridge_propose.py`, `test_bridge_projection.py`. |

Strict order: **1 discovery → 2 fact-type/ref/gate → 3 authority + propose + migration → 4 projection + full cycle.**

---

### Task 1: Cross-catalog bridge candidate discovery (read-only)

**Files:**
- Create: `src/featuregen/overlay/upload/bridge_candidates.py`
- Test: `tests/featuregen/overlay/upload/test_bridge_candidates.py`

**Interfaces:**
- Consumes: `concept()` (`Concept.group`/`entity_link`), `allowed_sensitivities(roles)`, `CatalogObjectRef` (`overlay/identity.py`), `graph_node` columns `catalog_source, object_ref, table_name, column_name, data_type, concept, is_grain, kind, sensitivity`.
- Produces: `BRIDGE_DERIVATION_VERSION = "1.0.0"`; `BridgeCandidateV1(candidate_id, entity_id, left_ref, right_ref, data_type_family, left_is_grain, right_is_grain)`; `derive_bridge_candidates(conn, *, roles=()) -> tuple[BridgeCandidateV1, ...]`.

- [ ] **Step 1: Write the failing tests** — create `tests/featuregen/overlay/upload/test_bridge_candidates.py`:

```python
from __future__ import annotations

from featuregen.overlay.upload.bridge_candidates import (
    BRIDGE_DERIVATION_VERSION,
    derive_bridge_candidates,
)
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph


def _load(db, source, rows_and_concepts):
    rows = [r for r, _ in rows_and_concepts]
    build_graph(db, source, rows, concepts={content_hash(r): c for r, c in rows_and_concepts})


def _two_catalog_customer(db):
    _load(db, "core", [
        (CanonicalRow("core", "customer_master", "customer_id", "integer", is_grain=True), "customer_id"),
        (CanonicalRow("core", "customer_master", "segment", "text"), "categorical"),
    ])
    _load(db, "crm", [
        (CanonicalRow("crm", "customers", "customer_id", "integer", is_grain=True), "customer_id"),
    ])


def test_derive_bridge_same_entity_distinct_catalogs(db):
    _two_catalog_customer(db)
    cands = derive_bridge_candidates(db)
    assert len(cands) == 1
    c = cands[0]
    assert c.entity_id == "customer"
    assert (c.left_ref.catalog_source, c.left_ref.table, c.left_ref.column) == ("core", "customer_master", "customer_id")
    assert (c.right_ref.catalog_source, c.right_ref.table, c.right_ref.column) == ("crm", "customers", "customer_id")
    assert c.left_ref.object_kind == "column" and c.left_ref.schema == "public"
    assert c.data_type_family == "integer"
    assert c.left_is_grain is True and c.right_is_grain is True
    assert len(c.candidate_id) == 16   # deterministic sha256[:16]


def test_same_catalog_pair_is_not_a_bridge(db):
    _load(db, "solo", [
        (CanonicalRow("solo", "customer_master", "customer_id", "integer", is_grain=True), "customer_id"),
        (CanonicalRow("solo", "accounts", "customer_id", "integer"), "customer_id"),
    ])
    assert derive_bridge_candidates(db) == ()


def test_different_entities_do_not_bridge(db):
    _load(db, "core", [
        (CanonicalRow("core", "customer_master", "customer_id", "integer", is_grain=True), "customer_id"),
    ])
    _load(db, "cards", [
        (CanonicalRow("cards", "accounts", "account_id", "integer", is_grain=True), "account_id"),
    ])
    assert derive_bridge_candidates(db) == ()


def test_incompatible_type_family_does_not_bridge(db):
    _load(db, "core", [
        (CanonicalRow("core", "customer_master", "customer_id", "integer", is_grain=True), "customer_id"),
    ])
    _load(db, "crm", [
        (CanonicalRow("crm", "customers", "customer_id", "text", is_grain=True), "customer_id"),
    ])
    assert derive_bridge_candidates(db) == ()


def test_deterministic_candidate_id_is_orientation_independent(db):
    _two_catalog_customer(db)
    id1 = derive_bridge_candidates(db)[0].candidate_id
    # rebuild in the other declaration order -> same unordered candidate id
    build_graph(db, "core", [], concepts={})   # clear
    build_graph(db, "crm", [], concepts={})
    _load(db, "crm", [
        (CanonicalRow("crm", "customers", "customer_id", "integer", is_grain=True), "customer_id"),
    ])
    _load(db, "core", [
        (CanonicalRow("core", "customer_master", "customer_id", "integer", is_grain=True), "customer_id"),
    ])
    assert derive_bridge_candidates(db)[0].candidate_id == id1


def test_version_pinned():
    assert BRIDGE_DERIVATION_VERSION == "1.0.0"
```

- [ ] **Step 2: Run to verify it fails** — Run: `uv run pytest tests/featuregen/overlay/upload/test_bridge_candidates.py -q` → FAIL: `ModuleNotFoundError: ... bridge_candidates`.

- [ ] **Step 3: Create `src/featuregen/overlay/upload/bridge_candidates.py`:**

```python
"""Phase-3B.2B — cross-catalog entity-bridge candidate discovery.

A bridge candidate links two catalog-local identifier columns that denote the SAME entity in DISTINCT
uploads (e.g. core.customer_master.customer_id <-> crm.customers.customer_id). Governed via the concept
registry (concept group='identifier' + entity_link), NEVER the free-text graph_node.entity tag. Read-only
and deterministic; a candidate becomes a governed fact only when proposed + confirmed (later tasks)."""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass

from featuregen.overlay.identity import CatalogObjectRef
from featuregen.overlay.upload.concepts import concept
from featuregen.overlay.upload.read_scope import allowed_sensitivities

BRIDGE_DERIVATION_VERSION = "1.0.0"

_TYPE_FAMILY = {
    "integer": "integer", "int": "integer", "int4": "integer", "int8": "integer",
    "bigint": "integer", "smallint": "integer", "serial": "integer", "bigserial": "integer",
    "text": "text", "varchar": "text", "character varying": "text", "char": "text",
    "character": "text", "string": "text",
    "uuid": "uuid",
}


def _type_family(data_type: str | None) -> str:
    return _TYPE_FAMILY.get((data_type or "").strip().lower(), "other")


@dataclass(frozen=True, slots=True)
class BridgeCandidateV1:
    candidate_id: str
    entity_id: str
    left_ref: CatalogObjectRef
    right_ref: CatalogObjectRef
    data_type_family: str
    left_is_grain: bool
    right_is_grain: bool


@dataclass(frozen=True, slots=True)
class _IdCol:
    catalog_source: str
    table_name: str
    column_name: str
    entity: str
    type_family: str
    is_grain: bool


def _identifier_columns(conn, *, roles: Iterable[str]) -> list[_IdCol]:
    rows = conn.execute(
        "SELECT catalog_source, table_name, column_name, data_type, concept, is_grain FROM graph_node "
        "WHERE kind = 'column' AND concept IS NOT NULL "
        "AND (sensitivity IS NULL OR sensitivity = ANY(%s)) "
        "ORDER BY catalog_source, object_ref",
        (allowed_sensitivities(roles),)).fetchall()
    out: list[_IdCol] = []
    for catalog_source, table_name, column_name, data_type, concept_name, is_grain in rows:
        c = concept(concept_name)
        if c is None or c.group != "identifier" or not c.entity_link:
            continue
        out.append(_IdCol(catalog_source=catalog_source, table_name=table_name, column_name=column_name,
                          entity=c.entity_link, type_family=_type_family(data_type), is_grain=bool(is_grain)))
    return out


def _col_ref(col: _IdCol) -> CatalogObjectRef:
    return CatalogObjectRef(catalog_source=col.catalog_source, object_kind="column", schema="public",
                            table=col.table_name, column=col.column_name)


def _candidate(entity: str, a: _IdCol, b: _IdCol) -> BridgeCandidateV1:
    left, right = sorted((a, b), key=lambda c: (c.catalog_source, c.table_name, c.column_name))
    material = (f"{entity}|{left.catalog_source}.{left.table_name}.{left.column_name}"
                f"|{right.catalog_source}.{right.table_name}.{right.column_name}|{BRIDGE_DERIVATION_VERSION}")
    candidate_id = hashlib.sha256(material.encode()).hexdigest()[:16]
    return BridgeCandidateV1(
        candidate_id=candidate_id, entity_id=entity, left_ref=_col_ref(left), right_ref=_col_ref(right),
        data_type_family=left.type_family, left_is_grain=left.is_grain, right_is_grain=right.is_grain)


def derive_bridge_candidates(conn, *, roles: Iterable[str] = ()) -> tuple[BridgeCandidateV1, ...]:
    """Candidate bridges from declared metadata: identifier concepts for the SAME entity_link, in DISTINCT
    catalog sources, with a COMPATIBLE type family. Deterministic (canonical unordered pair + sorted
    output). Read-only."""
    by_entity: dict[str, list[_IdCol]] = {}
    for col in _identifier_columns(conn, roles=roles):
        if col.type_family != "other":
            by_entity.setdefault(col.entity, []).append(col)
    cands: dict[str, BridgeCandidateV1] = {}
    for entity, group in by_entity.items():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if a.catalog_source == b.catalog_source or a.type_family != b.type_family:
                    continue
                c = _candidate(entity, a, b)
                cands[c.candidate_id] = c
    return tuple(cands[k] for k in sorted(cands))
```

- [ ] **Step 4: Run to verify it passes** — Run: `uv run pytest tests/featuregen/overlay/upload/test_bridge_candidates.py -q` → PASS (6 passed).

- [ ] **Step 5: Gates + commit**

```bash
uv run ruff check src/featuregen/overlay/upload/bridge_candidates.py tests/featuregen/overlay/upload/test_bridge_candidates.py
uv run mypy src/featuregen/overlay/upload/bridge_candidates.py
git add -A && git commit -m "feat(3b2b): cross-catalog entity-bridge candidate discovery (task 1)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: The `entity_bridge` fact type, ref, and write gate

**Files:**
- Modify: `src/featuregen/overlay/facts.py`, `src/featuregen/overlay/_types.py`, `src/featuregen/overlay/identity.py`
- Test: `tests/featuregen/overlay/upload/test_entity_bridge_fact.py`

**Interfaces:**
- Consumes: `FACT_VALUE_SCHEMAS`, `_CATALOG_OBJECT_REF_SCHEMA`, `DATA_FACT_TYPES` (facts.py); `CatalogObjectRef`, `fact_key`, `_ref_from_payload`, `join_write_error`, `_norm`, `_ref_tuple`, `_digest` (identity.py).
- Produces: `facts.ENTITY_BRIDGE = "entity_bridge"`; `FACT_VALUE_SCHEMAS["entity_bridge"]`; `identity.EntityBridgeRef(entity_id, left_ref, right_ref)`; `fact_key`/`_ref_from_payload`/`join_write_error` handle the bridge; unordered (symmetric) `fact_key`.

- [ ] **Step 1: Write the failing tests** — create `tests/featuregen/overlay/upload/test_entity_bridge_fact.py`:

```python
from __future__ import annotations

import pytest

from featuregen.overlay import facts
from featuregen.overlay.facts import FACT_VALUE_SCHEMAS, validate_fact_value
from featuregen.overlay.identity import CatalogObjectRef, EntityBridgeRef, fact_key, join_write_error


def _ref(left_source="core", right_source="crm") -> EntityBridgeRef:
    return EntityBridgeRef(
        entity_id="customer",
        left_ref=CatalogObjectRef(left_source, "column", "public", "customer_master", "customer_id"),
        right_ref=CatalogObjectRef(right_source, "column", "public", "customers", "customer_id"))


def _value(ref: EntityBridgeRef) -> dict:
    from dataclasses import asdict
    return {"entity_id": ref.entity_id, "left_ref": asdict(ref.left_ref), "right_ref": asdict(ref.right_ref)}


def test_entity_bridge_is_a_registered_data_fact_type():
    assert facts.ENTITY_BRIDGE == "entity_bridge"
    assert facts.ENTITY_BRIDGE in facts.DATA_FACT_TYPES
    assert facts.ENTITY_BRIDGE in FACT_VALUE_SCHEMAS


def test_value_schema_accepts_a_bridge_and_rejects_extras():
    ref = _ref()
    validate_fact_value("entity_bridge", _value(ref))   # no raise
    bad = _value(ref) | {"unexpected": 1}
    with pytest.raises(Exception):
        validate_fact_value("entity_bridge", bad)


def test_fact_key_is_symmetric():
    # swapping the two endpoints denotes the SAME bridge -> identical fact_key
    a = _ref()
    b = EntityBridgeRef(entity_id="customer", left_ref=a.right_ref, right_ref=a.left_ref)
    assert fact_key(a, "entity_bridge") == fact_key(b, "entity_bridge")


def test_write_gate_requires_cross_catalog():
    same = _ref(left_source="core", right_source="core")   # same catalog -> illegal for a bridge
    err = join_write_error(same, "entity_bridge", _value(same))
    assert err is not None and "distinct catalog" in err


def test_write_gate_passes_cross_catalog_and_matching_value():
    ref = _ref()
    assert join_write_error(ref, "entity_bridge", _value(ref)) is None


def test_write_gate_rejects_value_ref_mismatch():
    ref = _ref()
    other = _value(_ref(right_source="other"))   # value describes a different bridge than ref
    err = join_write_error(ref, "entity_bridge", other)
    assert err is not None and "does not match" in err
```

- [ ] **Step 2: Run to verify it fails** — Run: `uv run pytest tests/featuregen/overlay/upload/test_entity_bridge_fact.py -q` → FAIL: `ImportError: cannot import name 'EntityBridgeRef'`.

- [ ] **Step 3a: Register the fact type** — in `src/featuregen/overlay/facts.py`, add the constant next to the others, add it to `DATA_FACT_TYPES`, and add its value schema to `FACT_VALUE_SCHEMAS`:

```python
ENTITY_BRIDGE = "entity_bridge"
```
```python
DATA_FACT_TYPES = frozenset({AVAILABILITY_TIME, GRAIN, SCD_EFFECTIVE_DATING, APPROVED_JOIN, ENTITY_BRIDGE})
```
```python
    ENTITY_BRIDGE: {
        # A cross-catalog identity bridge: the SAME entity via an identifier column in two DISTINCT
        # catalogs. Symmetric in (left_ref, right_ref); cross-catalog is enforced in the write gate.
        "type": "object",
        "required": ["entity_id", "left_ref", "right_ref"],
        "properties": {
            "entity_id": {"type": "string"},
            "left_ref": _CATALOG_OBJECT_REF_SCHEMA,
            "right_ref": _CATALOG_OBJECT_REF_SCHEMA,
        },
        "additionalProperties": False,
    },
```

- [ ] **Step 3b: Add the FactType literal** — in `src/featuregen/overlay/_types.py`, add `"entity_bridge"` to the `FactType` `Literal[...]` alias (keep it in sync with `facts.py`).

- [ ] **Step 3c: Add the ref + fact_key + write gate** — in `src/featuregen/overlay/identity.py`:

Add the dataclass (next to `ApprovedJoinRef`):
```python
@dataclass(frozen=True, slots=True)
class EntityBridgeRef:
    """A cross-catalog entity bridge: the SAME entity_id via an identifier column in two DISTINCT
    catalogs. Bridge identity is UNORDERED — (left, right) and (right, left) denote the same bridge, so
    fact_key canonicalizes the endpoints."""
    entity_id: str
    left_ref: CatalogObjectRef
    right_ref: CatalogObjectRef
```

Add a branch at the TOP of `fact_key` (before the `ApprovedJoinRef` branch):
```python
    if isinstance(ref, EntityBridgeRef):
        endpoints = sorted([_ref_tuple(ref.left_ref), _ref_tuple(ref.right_ref)])
        canonical = {"kind": "bridge", "entity_id": _norm(ref.entity_id), "endpoints": endpoints,
                     "fact_type": _norm(fact_type), "use_case": _norm(use_case)}
        return _digest(canonical)
```

Add a branch to `_ref_from_payload` (BEFORE the `"column_pairs" in d` join branch, since a bridge payload has neither `column_pairs` nor a bare object ref):
```python
    if "entity_id" in d and "left_ref" in d and "right_ref" in d:
        return EntityBridgeRef(entity_id=d["entity_id"],
                               left_ref=CatalogObjectRef(**d["left_ref"]),
                               right_ref=CatalogObjectRef(**d["right_ref"]))
```

Add the write-gate branch — at the TOP of `join_write_error`, and a helper:
```python
def _bridge_write_error(ref, value) -> str | None:
    if not isinstance(ref, EntityBridgeRef):
        return "entity_bridge requires an EntityBridgeRef"
    if _norm(ref.left_ref.catalog_source) == _norm(ref.right_ref.catalog_source):
        return ("entity_bridge requires two distinct catalog sources "
                f"(left={ref.left_ref.catalog_source}, right={ref.right_ref.catalog_source})")
    value_ref = _ref_from_payload(value)
    if not isinstance(value_ref, EntityBridgeRef):
        return "entity_bridge proposed_value is not a bridge ref"
    if fact_key(value_ref, "entity_bridge") != fact_key(ref, "entity_bridge"):
        return "entity_bridge proposed_value does not match ref"
    return None
```
and, as the FIRST lines of `join_write_error(ref, fact_type, value, use_case=None)`:
```python
    if fact_type == "entity_bridge":
        return _bridge_write_error(ref, value)
```
(the existing `if fact_type != "approved_join": return None` and the rest stay unchanged — behaviour-neutral for every other fact type.)

- [ ] **Step 4: Run to verify it passes** — Run: `uv run pytest tests/featuregen/overlay/upload/test_entity_bridge_fact.py -q` → PASS (6 passed).

- [ ] **Step 5: Gates + commit**

```bash
uv run ruff check src/featuregen/overlay/facts.py src/featuregen/overlay/_types.py src/featuregen/overlay/identity.py tests/featuregen/overlay/upload/test_entity_bridge_fact.py
uv run mypy src/featuregen/overlay/facts.py src/featuregen/overlay/identity.py
git add -A && git commit -m "feat(3b2b): entity_bridge fact type + cross-catalog ref + write gate (task 2)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Authority resolution + propose path + migration 0989

**Files:**
- Modify: `src/featuregen/overlay/authority.py`
- Create: `src/featuregen/db/migrations/0989_entity_bridge_governance.sql`
- Create: `src/featuregen/overlay/upload/bridge_propose.py`
- Test: `tests/featuregen/overlay/upload/test_bridge_propose.py`

**Interfaces:**
- Consumes: Task 1 `BridgeCandidateV1`; Task 2 `EntityBridgeRef`/`fact_key`; `Authority`/`resolve_authority` (authority.py); `write_evidence` + enums (evidence.py); `propose_fact` + `Command` + `proposal_fingerprint`; `identity_to_jsonb`; `fold_overlay_state`/`load_fact`.
- Produces: `resolve_authority` handles `entity_bridge` (single-confirmer); tables `entity_bridge_candidate_evidence` + `entity_bridge_edge`; `propose_bridge(conn, candidate, *, actor, now=None) -> str` (returns `fact_key`).

- [ ] **Step 1: Write the migration** `src/featuregen/db/migrations/0989_entity_bridge_governance.sql`:

```sql
-- src/featuregen/db/migrations/0989_entity_bridge_governance.sql
-- Phase 3B.2B: governed cross-catalog entity bridges. The bridge LIFECYCLE rides the generic
-- overlay_fact event stream (fact_type='entity_bridge') — these tables are only the durable candidate
-- ledger + the VERIFIED projection. entity_bridge_edge is cross-catalog (graph_edge is intra-catalog-
-- keyed), and is what the 3B.3 planner reads. Additive-only; nothing consumes it until 3B.3.
CREATE TABLE IF NOT EXISTS entity_bridge_candidate_evidence (
    entity_id            text        NOT NULL,
    left_catalog_source  text        NOT NULL,
    left_object_ref      text        NOT NULL,
    right_catalog_source text        NOT NULL,
    right_object_ref     text        NOT NULL,
    candidate_id         text        NOT NULL,
    fact_key             text        NULL,
    proposed_event_id    text        NULL,
    data_type_family     text        NOT NULL,
    evidence_json        jsonb       NOT NULL DEFAULT '{}',
    derivation_version   text        NOT NULL,
    updated_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (entity_id, left_catalog_source, left_object_ref, right_catalog_source, right_object_ref),
    CONSTRAINT entity_bridge_candidate_distinct_sources
        CHECK (left_catalog_source <> right_catalog_source)
);

CREATE TABLE IF NOT EXISTS entity_bridge_edge (
    fact_key             text        PRIMARY KEY,
    entity_id            text        NOT NULL,
    left_catalog_source  text        NOT NULL,
    left_object_ref      text        NOT NULL,
    right_catalog_source text        NOT NULL,
    right_object_ref     text        NOT NULL,
    confirmed_event_id   text        NULL,
    status               text        NOT NULL,   -- 'VERIFIED'
    projected_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS entity_bridge_edge_entity_idx ON entity_bridge_edge (entity_id);
```

- [ ] **Step 2: Add the authority branch** — in `src/featuregen/overlay/authority.py`, import `EntityBridgeRef` and add a branch to `resolve_authority` (place it after the `approved_join` branch, before the final `CatalogObjectRef` else):

```python
    if fact_type == "entity_bridge":
        if not isinstance(ref, EntityBridgeRef):
            raise TypeError(
                f"entity_bridge authority requires an EntityBridgeRef, got {type(ref).__name__}")
        # 3B.2B shadow: a SINGLE governance confirmation (four-eyes: proposer != confirmer). Two-owner
        # dual sign-off is deferred to 3C (when a bridge becomes live-traversable). owner_of is consulted
        # only to collapse onto a shared owner when both catalogs happen to share one.
        left_owner = adapter.owner_of(ref.left_ref)
        right_owner = adapter.owner_of(ref.right_ref)
        if left_owner is not None and left_owner == right_owner:
            return Authority(role="data_owner", gate="OVERLAY_DATA_OWNER",
                             subjects=(left_owner,), governance_queue=False)
        return Authority(role="platform-admin", gate="OVERLAY_DATA_OWNER",
                         subjects=(), governance_queue=True)
```

- [ ] **Step 3: Write the failing tests** — create `tests/featuregen/overlay/upload/test_bridge_propose.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime

from featuregen.overlay.identity import fact_key
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.bridge_candidates import derive_bridge_candidates
from featuregen.overlay.upload.bridge_propose import propose_bridge
from featuregen.overlay.upload.enrich_llm import _ENRICH_ACTOR
from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter
from tests.featuregen.overlay.upload.test_bridge_candidates import _two_catalog_customer

_T0 = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def _propose(db) -> str:
    ensure_upload_catalog_adapter()
    _two_catalog_customer(db)
    cand = derive_bridge_candidates(db)[0]
    return propose_bridge(db, cand, actor=_ENRICH_ACTOR, now=_T0)


def test_propose_opens_a_draft_bridge_fact(db):
    key = _propose(db)
    state = fold_overlay_state(load_fact(db, key))
    assert state.status == "DRAFT"


def test_propose_stamps_the_candidate_ledger(db):
    key = _propose(db)
    row = db.execute(
        "SELECT entity_id, fact_key, proposed_event_id, data_type_family "
        "FROM entity_bridge_candidate_evidence "
        "WHERE left_catalog_source = 'core' AND right_catalog_source = 'crm'").fetchone()
    assert row is not None
    assert row[0] == "customer" and row[1] == key and row[2] is not None and row[3] == "integer"


def test_propose_opens_one_governance_gate_task(db):
    _propose(db)
    # single-confirmer -> exactly one open human task (platform-admin governance), not two
    n = db.execute("SELECT count(*) FROM human_tasks WHERE status = 'open'").fetchone()[0]
    assert n == 1
```

- [ ] **Step 4: Run to verify it fails** — Run: `uv run pytest tests/featuregen/overlay/upload/test_bridge_propose.py -q` → FAIL: `ModuleNotFoundError: ... bridge_propose`.

- [ ] **Step 5: Create `src/featuregen/overlay/upload/bridge_propose.py`:**

```python
"""Phase-3B.2B — propose a governed entity bridge onto the overlay_fact spine.

`propose_bridge` mirrors passc/propose.py::_propose_one: pre-mint an immutable evidence record, then
dispatch the generic `propose_fact` command with fact_type='entity_bridge'. The bridge lifecycle is
thereafter the standard overlay_fact stream (DRAFT -> ... -> VERIFIED). Also stamps the durable candidate
ledger with the resolved fact_key + proposed event id."""
from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime

from featuregen.contracts.envelopes import Command
from featuregen.contracts.identity import identity_to_jsonb
from featuregen.overlay.commands import propose_fact
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer, write_evidence
from featuregen.overlay.identity import EntityBridgeRef, fact_key, proposal_fingerprint
from featuregen.overlay.upload.bridge_candidates import BRIDGE_DERIVATION_VERSION, BridgeCandidateV1


def _object_ref_str(ref) -> str:
    return f"{ref.schema}.{ref.table}.{ref.column}"


def propose_bridge(conn, candidate: BridgeCandidateV1, *, actor, now=None) -> str:
    """Propose one bridge candidate as an entity_bridge fact. Returns the fact_key. Deterministic +
    append-only. The four-eyes gate holds because a human (not this service actor) later confirms."""
    ts = now if now is not None else datetime.now(UTC)
    ref = EntityBridgeRef(entity_id=candidate.entity_id, left_ref=candidate.left_ref,
                          right_ref=candidate.right_ref)
    key = fact_key(ref, "entity_bridge")
    evidence = {"entity_id": candidate.entity_id, "candidate_id": candidate.candidate_id,
                "data_type_family": candidate.data_type_family, "left_is_grain": candidate.left_is_grain,
                "right_is_grain": candidate.right_is_grain, "derivation_version": BRIDGE_DERIVATION_VERSION}
    evidence_ref = write_evidence(
        conn, fact_key=key, table_snapshot_at=ts, row_count=0, sample_size=0,
        profile_version=BRIDGE_DERIVATION_VERSION, thresholds_used={}, metric_values=evidence,
        created_by=identity_to_jsonb(actor),
        producer=EvidenceProducer.STRUCTURAL_CONNECTOR, strength=AssertionStrength.PROPOSED)
    value = {"entity_id": candidate.entity_id, "left_ref": asdict(candidate.left_ref),
             "right_ref": asdict(candidate.right_ref)}
    res = propose_fact(conn, Command(
        "propose_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "entity_bridge", "proposed_value": value,
         "evidence_ref": evidence_ref},
        actor, proposal_fingerprint(value)))
    if not res.accepted:
        raise RuntimeError(f"bridge proposal denied: {res.denied_reason}")
    proposed_event_id = res.produced_event_ids[0] if res.produced_event_ids else None
    conn.execute(
        "INSERT INTO entity_bridge_candidate_evidence ("
        "  entity_id, left_catalog_source, left_object_ref, right_catalog_source, right_object_ref,"
        "  candidate_id, fact_key, proposed_event_id, data_type_family, evidence_json, derivation_version,"
        "  updated_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (entity_id, left_catalog_source, left_object_ref, right_catalog_source,"
        "  right_object_ref) DO UPDATE SET fact_key = EXCLUDED.fact_key,"
        "  proposed_event_id = EXCLUDED.proposed_event_id, updated_at = EXCLUDED.updated_at",
        (candidate.entity_id, candidate.left_ref.catalog_source, _object_ref_str(candidate.left_ref),
         candidate.right_ref.catalog_source, _object_ref_str(candidate.right_ref), candidate.candidate_id,
         key, proposed_event_id, candidate.data_type_family,
         __import__("json").dumps(evidence), BRIDGE_DERIVATION_VERSION, ts))
    return key
```

> **Implementer note:** `CommandResult.produced_event_ids: tuple[str, ...]` (verified — the propose command produces the single `OVERLAY_FACT_PROPOSED` event, so `[0]` is its id; empty-guarded). Replace the inline `__import__("json")` with a top-of-file `import json` (E402 — src is not test-exempt).

- [ ] **Step 6: Run to verify it passes** — Run: `uv run pytest tests/featuregen/overlay/upload/test_bridge_propose.py -q` → PASS (3 passed).

- [ ] **Step 7: Gates + commit**

```bash
uv run ruff check src/featuregen/overlay/authority.py src/featuregen/overlay/upload/bridge_propose.py tests/featuregen/overlay/upload/test_bridge_propose.py
uv run mypy src/featuregen/overlay/authority.py src/featuregen/overlay/upload/bridge_propose.py
git add -A && git commit -m "feat(3b2b): entity_bridge authority + propose + candidate ledger (migration 0989) (task 3)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Confirm → project + demotion + full-cycle proof

**Files:**
- Create: `src/featuregen/overlay/upload/bridge_projection.py`
- Test: `tests/featuregen/overlay/upload/test_bridge_projection.py`

**Interfaces:**
- Consumes: Task 2 `EntityBridgeRef`/`fact_key`; `fold_overlay_state`/`load_fact`; `confirm_fact`/`reject_fact` + `Command` (used from the test to drive the human confirmation); `_cas_target`; the `entity_bridge_edge` table (Task 3).
- Produces: `project_verified_bridge(conn, ref, *, now) -> str` (`"projected"`/`"pending"`); `demote_bridge_edges(conn, fact_key) -> int`; `active_bridges(conn) -> tuple[ActiveBridgeV1, ...]`.

- [ ] **Step 1: Write the failing tests** — create `tests/featuregen/overlay/upload/test_bridge_projection.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime

from featuregen.contracts.envelopes import Command
from featuregen.overlay._lifecycle import _cas_target
from featuregen.overlay.commands import confirm_fact, reject_fact
from featuregen.overlay.identity import EntityBridgeRef, fact_key
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.bridge_candidates import derive_bridge_candidates
from featuregen.overlay.upload.bridge_projection import (
    active_bridges,
    demote_bridge_edges,
    project_verified_bridge,
)
from featuregen.overlay.upload.bridge_propose import propose_bridge
from featuregen.overlay.upload.enrich_llm import _ENRICH_ACTOR
from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter
from tests.featuregen._helpers import mint_test_identity
from tests.featuregen.overlay.upload.test_bridge_candidates import _two_catalog_customer

_NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def _ref(db) -> EntityBridgeRef:
    cand = derive_bridge_candidates(db)[0]
    return EntityBridgeRef(cand.entity_id, cand.left_ref, cand.right_ref)


def _propose_confirm(db) -> EntityBridgeRef:
    ensure_upload_catalog_adapter()
    _two_catalog_customer(db)
    ref = _ref(db)
    propose_bridge(db, derive_bridge_candidates(db)[0], actor=_ENRICH_ACTOR, now=_NOW)
    key = fact_key(ref, "entity_bridge")
    admin = mint_test_identity(subject="user:admin1", role_claims=("platform-admin",))
    target = _cas_target(fold_overlay_state(load_fact(db, key)))
    res = confirm_fact(db, Command(
        "confirm_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "entity_bridge", "use_case": None, "target_event_id": target},
        admin, f"confirm-{target}"))
    assert res.accepted, res.denied_reason
    return ref


def test_single_confirm_verifies_a_bridge(db):
    ref = _propose_confirm(db)
    # single-confirmer: ONE platform-admin confirmation reaches VERIFIED
    assert fold_overlay_state(load_fact(db, fact_key(ref, "entity_bridge"))).status == "VERIFIED"


def test_project_verified_bridge_writes_the_edge(db):
    ref = _propose_confirm(db)
    assert project_verified_bridge(db, ref, now=_NOW) == "projected"
    row = db.execute(
        "SELECT entity_id, left_catalog_source, right_catalog_source, status FROM entity_bridge_edge "
        "WHERE fact_key = %s", (fact_key(ref, "entity_bridge"),)).fetchone()
    assert row == ("customer", "core", "crm", "VERIFIED")
    active = active_bridges(db)
    assert len(active) == 1 and active[0].entity_id == "customer"


def test_unverified_bridge_does_not_project(db):
    ensure_upload_catalog_adapter()
    _two_catalog_customer(db)
    ref = _ref(db)
    propose_bridge(db, derive_bridge_candidates(db)[0], actor=_ENRICH_ACTOR, now=_NOW)   # DRAFT only
    assert project_verified_bridge(db, ref, now=_NOW) == "pending"
    assert active_bridges(db) == ()


def test_demote_removes_a_projected_bridge(db):
    ref = _propose_confirm(db)
    project_verified_bridge(db, ref, now=_NOW)
    assert demote_bridge_edges(db, fact_key(ref, "entity_bridge")) == 1
    assert active_bridges(db) == ()
```

- [ ] **Step 2: Run to verify it fails** — Run: `uv run pytest tests/featuregen/overlay/upload/test_bridge_projection.py -q` → FAIL: `ModuleNotFoundError: ... bridge_projection`.

- [ ] **Step 3: Create `src/featuregen/overlay/upload/bridge_projection.py`:**

```python
"""Phase-3B.2B — project a VERIFIED entity bridge into the cross-catalog entity_bridge_edge table.

The bridge's source of truth is the overlay_fact event stream; entity_bridge_edge is a derived projection
(the active cross-catalog set the 3B.3 planner reads, replacing the permissive find_cross_catalog_path
adjacency). State is read by folding the stream directly (no adapter/no drain needed — the fold is the
authoritative status). Demotion DELETEs the derived edge; it is always rebuildable from the stream."""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.overlay.identity import EntityBridgeRef, fact_key
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact


@dataclass(frozen=True, slots=True)
class ActiveBridgeV1:
    fact_key: str
    entity_id: str
    left_catalog_source: str
    left_object_ref: str
    right_catalog_source: str
    right_object_ref: str


def _obj_ref_str(d: dict) -> str:
    return f"{d['schema']}.{d['table']}.{d['column']}"


def project_verified_bridge(conn, ref: EntityBridgeRef, *, now) -> str:
    """Project the bridge iff its folded state is VERIFIED. Returns 'projected' or 'pending'. A non-VERIFIED
    bridge is demoted (any stale edge removed). Idempotent (DELETE-then-INSERT by fact_key)."""
    key = fact_key(ref, "entity_bridge")
    state = fold_overlay_state(load_fact(conn, key))
    if state.status != "VERIFIED" or not state.value:
        conn.execute("DELETE FROM entity_bridge_edge WHERE fact_key = %s", (key,))
        return "pending"
    v = state.value
    conn.execute("DELETE FROM entity_bridge_edge WHERE fact_key = %s", (key,))
    conn.execute(
        "INSERT INTO entity_bridge_edge (fact_key, entity_id, left_catalog_source, left_object_ref, "
        "  right_catalog_source, right_object_ref, confirmed_event_id, status, projected_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,'VERIFIED',%s)",
        (key, v["entity_id"], v["left_ref"]["catalog_source"], _obj_ref_str(v["left_ref"]),
         v["right_ref"]["catalog_source"], _obj_ref_str(v["right_ref"]), state.confirmed_event_id, now))
    return "projected"


def demote_bridge_edges(conn, fact_key_value: str) -> int:
    """Remove a projected bridge (on reject/expire/stale). Returns rows removed. The event stream retains
    the full audit; the projection is derived."""
    cur = conn.execute("DELETE FROM entity_bridge_edge WHERE fact_key = %s", (fact_key_value,))
    return cur.rowcount


def active_bridges(conn) -> tuple[ActiveBridgeV1, ...]:
    """The currently-projected VERIFIED bridges — the cross-catalog active set 3B.3 consumes. Deterministic
    (ordered)."""
    rows = conn.execute(
        "SELECT fact_key, entity_id, left_catalog_source, left_object_ref, right_catalog_source, "
        "  right_object_ref FROM entity_bridge_edge WHERE status = 'VERIFIED' "
        "ORDER BY entity_id, left_object_ref, right_object_ref").fetchall()
    return tuple(ActiveBridgeV1(*r) for r in rows)
```

- [ ] **Step 4: Run to verify it passes** — Run: `uv run pytest tests/featuregen/overlay/upload/test_bridge_projection.py -q` → PASS (4 passed).

- [ ] **Step 5: Behaviour-neutral proof (full suite)** — Run:

```bash
uv run pytest tests/featuregen/ -q
```
Expected: PASS. Existing counts are byte-identical **+19** (the new bridge tests: 6 + 6 + 3 + 4). Every shared-file change is a branch gated on the new fact type / ref, which nothing existing produces; the migration is additive. If a migration-ledger test appears, confirm it uses a relative floor (`>= 40`) — no update needed.

- [ ] **Step 6: Gates + commit**

```bash
uv run ruff check src/featuregen/overlay/upload/bridge_projection.py tests/featuregen/overlay/upload/test_bridge_projection.py
uv run mypy src/featuregen/overlay/upload/bridge_projection.py
git add -A && git commit -m "feat(3b2b): VERIFIED-bridge projection + demotion + active set (task 4)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Exit criteria mapping

| Spec requirement (3B.2B) | Where satisfied |
|---|---|
| Candidate eligibility — identifier concepts, same entity_link, key-like, compatible type, distinct sources, evidence (#8) | Task 1 `derive_bridge_candidates` + tests |
| Not every shared-entity pair (concept-governed, not the free-text tag) (#8) | Task 1 keys on `concept().group=="identifier"`+`entity_link`; `test_different_entities_do_not_bridge` |
| Bridge-specific contract distinct from the semantic proposal (#9) | Task 2 `EntityBridgeRef` + the `entity_bridge` value schema (a cross-catalog *identity* fact, not an `A→B` relationship or an `approved_join`) |
| Event-sourced sanction lifecycle: proposed→confirmed→verified, reject, expire/stale (#10) | REUSED overlay_fact spine — Task 3 `propose_bridge` (DRAFT) + Task 4 confirm→VERIFIED + demote; `fold_overlay_state` gives the projection |
| Active state is a projection, not a mutable flag (#10) | Task 4 `project_verified_bridge`/`active_bridges` fold the event stream; `entity_bridge_edge` is derived |
| Each event carries actor/authority + audit; append-only, no silent edit (#10) | REUSED append-only `overlay_fact` events + four-eyes `proposer_ne_confirmer` |
| Bound to catalog identity; drift → re-review (#10) | REUSED expiry/stale lifecycle (EXPIRED→REVERIFY / STALED→STALE) demotes the edge via `project_verified_bridge`/`demote_bridge_edges` |
| Cross-catalog required (bridge spans two uploads) | Task 2 `_bridge_write_error` requires distinct catalog sources |
| Old permissive path stays dormant; behaviour-neutral; no flag | `entity.py`/`find_cross_catalog_path` untouched; Task 4 Step 5 full-suite proof; additive migration `0989` |

## Deferred (recorded, not built here)

- **Two-owner dual sign-off → 3C.** 3B.2B uses single-confirmer four-eyes; per-owner two-party accountability lands when bridges go live (fork the `_confirm_approved_join` dual two-step then).
- **Live traversal → 3B.3.** The planner reads `active_bridges` (replacing `find_cross_catalog_path`'s ungoverned entity-tag adjacency); it should also filter by current fold status as a backstop.
- **Admin UI / HTTP routes → 3C.**
- **Namespace-compatibility gating** (`passc/namespace.py` `NamespaceCompatibility`) as a candidate filter → 3C refinement; 3B.2B records the type family only.
- **Bonus, orthogonal:** wire 3B.2A's dead `RealizationAuthority.APPROVED_JOIN` to read `graph_edge.approved_join_status='VERIFIED'` (the producer now exists); and add the `authority='operational' AND (approved_join_fact_key IS NULL OR approved_join_status='VERIFIED')` filter that `_join_edges` omitted. Track as a small separate 3B.2A follow-up.

## Self-Review

**1. Spec coverage:** #8 (Task 1), #9 (Task 2 contract), #10 (Tasks 3–4, realized by reusing the overlay_fact lifecycle). "Same workspace" remains deferred (no tenancy model); "event-sourced lifecycle" is now satisfied by the shared spine rather than bespoke tables — a deliberate re-founding vs the spec's original #10 wording (flag this when revising the spec). ✅

**2. Placeholder scan:** No TBD/TODO; every code + test block is complete. Two explicit *implementer notes* (the `CommandResult` event-id attribute name; the inline-`json` → top import) are verify-and-adapt hooks against live code, not placeholders. ✅

**3. Type consistency:** `EntityBridgeRef(entity_id, left_ref, right_ref)` defined in Task 2, consumed unchanged in Tasks 3–4. `fact_key(ref, "entity_bridge")` is the one identity used across propose/confirm/project. `BridgeCandidateV1` (Task 1) → `propose_bridge` (Task 3) → `project_verified_bridge` (Task 4) via the shared `EntityBridgeRef`. The value dict shape `{entity_id, left_ref, right_ref}` matches the Task 2 schema and is read identically in Task 4's projection. ✅

**Note for the executor:** this plan touches shared governance files (`facts.py`, `identity.py`, `authority.py`) — every change is an *additive branch on the new fact type*. If any task's full-suite run reddens an existing test, STOP: a non-additive change slipped in. The behaviour-neutral guarantee is the gate.
```

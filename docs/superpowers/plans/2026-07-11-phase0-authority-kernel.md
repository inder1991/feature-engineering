# Phase 0 — Authority Kernel (Extension) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add the six genuinely-new authority-kernel capabilities identified in the spec's §0 reuse map — an assertion-strength axis, object identity-status, a field-authority policy over (producer, strength), safety-override authority, a conflict-review lifecycle, and governed-`joins_to` wiring — as **extensions** of the existing overlay event-sourced fact substrate. No new enrichment; no parallel governance system.

**Architecture:** The overlay package is already a mature *propose → confirm → fold → resolve* substrate (`facts.py` events, `store.py`/`state.py` fold, `resolve.py`, `authority.py`, `confirmation_commands.py`, `expiry.py`). This plan does **not** rebuild any of that. It adds: columns on `overlay_evidence`; three small new modules (`object_identity.py`, `field_authority.py`, `conflict_review.py`, `safety_floor.py`); and one governed seam that routes a declared `joins_to` through the existing `approved_join`/`propose_fact` path instead of the ungoverned `graph_edge 'joins'` write. Readiness scope (spec contract 8) is deferred to Phase 2.

**Tech Stack:** Python 3.12, psycopg (raw SQL), pytest with a live-Postgres `db`/`conn` fixture (ephemeral PG auto-provisioned), `uv`. No frontend.

**Spec:** `docs/superpowers/specs/2026-07-11-evidence-authority-ingestion.md` (v4). §0 = the reuse map this plan builds to; §3.1 (strength/lifecycle), §2 (identity), §4 (policy/predicate/disqualifiers), §7 (safety floor), §10 (conflict lifecycle), §12.1 (`joins_to`).

## Global Constraints

- **Extension, not replacement.** Reuse `facts.py` events, `state.py` fold, `resolve.py`, `authority.py resolve_authority`, `confirmation_commands.py`, `expiry.py`, `identity.py` (`CatalogObjectRef`/`ApprovedJoinRef`/`fact_key`/`join_write_error`), `proposal_commands.propose_fact`. Do NOT create a second event log or a parallel confirmation flow. If a task seems to duplicate an existing capability, STOP and report — the reuse map says it exists.
- **provenance ≠ authority; confidence ≠ permission.** A `(producer, strength)` pair never gains authority by derivation (§3.2): a derived value's strength ≤ the min of its inputs' strengths.
- **Fail-closed safety.** Sensitivity floors may only be raised by evidence; a below-floor downgrade requires a `SafetyOverride` (governance authority + rationale + scope) — never a generic confirmation.
- **No-attach-when-ambiguous.** Evidence/proposals must not attach to a logical object whose identity status is `AMBIGUOUS`/`UNRESOLVED`.
- **Additive, default-off wiring.** The `joins_to` governance seam ships behind a flag (default off) so existing lineage is unchanged until cutover; the ungoverned edge is gated to display-only, never silently removed.
- **Conventions:** raw SQL via `conn.execute`; `from __future__ import annotations`; frozen slotted dataclasses like the existing overlay modules; ruff + mypy clean; TDD (failing test first); commit per task. Tests: `uv run pytest <path> -q`. Migrations are auto-discovered `.sql`, checksum-ledgered, applied once by the `conn`/`db` fixture.
- **Migration numbering:** highest on `main` at authoring time is `0977`. Use `0978`, `0979`, `0980` — **verify the next free slot on `main` before implementing** (parallel sessions advance it; collisions are silent add/adds).

---

## File Structure

- Modify `src/featuregen/overlay/evidence.py` + new migration `0978_evidence_strength.sql` — add the (producer, strength) axis + item-level linkage to the existing `overlay_evidence` record.
- Create `src/featuregen/overlay/object_identity.py` — `ObjectIdentityStatus` + `resolve_object_identity` + the no-attach guard. Wraps existing `CatalogAdapter`/`identity.py`.
- Create `src/featuregen/overlay/field_authority.py` — `AuthorityPredicate` tree + `evaluate`, `FieldPolicy` + `Disqualifier` + `resolve_field_authority` (two-output). Pure over an evidence set.
- Create `src/featuregen/overlay/safety_floor.py` + migration `0979_safety_override.sql` — sensitivity severity order, `GovernanceAuthority`, `SafetyOverride` record + table, `apply_sensitivity_floor`.
- Create `src/featuregen/overlay/conflict_review.py` + migration `0980_conflict_review.sql` — `conflict_fingerprint`, `ConflictState`, `conflict_review` table, open/ack/resolve/dismiss/stale/reopen commands.
- Modify `src/featuregen/overlay/upload/ingest.py` + `src/featuregen/overlay/upload/graph.py` — the governed-`joins_to` seam (declared join → `approved_join` proposal) behind a flag; gate the raw `graph_edge 'joins'` edge to display-only.
- Tests under `tests/featuregen/overlay/` mirroring the existing layout.

Locked interfaces (later tasks depend on these exact names):

```python
# evidence.py (extended)
class EvidenceProducer(StrEnum): SOURCE; STRUCTURAL_CONNECTOR; PARSER; LLM; PROFILER; TAXONOMY; HUMAN
class AssertionStrength(StrEnum): PROPOSED; SUPPORTED; ATTESTED; CONFIRMED
# write_evidence(... , producer, strength, producer_configuration_hash, producer_item_ref=None, evidence_spans=())

# object_identity.py
class ObjectIdentityStatus(StrEnum): EXACT; ALIASED; AMBIGUOUS; UNRESOLVED
@dataclass class ObjectBinding: logical_ref: CatalogObjectRef | None; status: ObjectIdentityStatus; candidates: tuple[str,...]
def resolve_object_identity(adapter, ref: CatalogObjectRef) -> ObjectBinding
def may_attach(binding: ObjectBinding) -> bool          # False for AMBIGUOUS/UNRESOLVED

# field_authority.py
class AuthorityPredicate: ...
@dataclass class HasEvidence(AuthorityPredicate): producer: EvidenceProducer; strength: AssertionStrength
@dataclass class AnyOf(AuthorityPredicate): conditions: tuple[AuthorityPredicate,...]
@dataclass class AllOf(AuthorityPredicate): conditions: tuple[AuthorityPredicate,...]
def evaluate(pred: AuthorityPredicate, active: frozenset[tuple[EvidenceProducer, AssertionStrength]]) -> bool
class Disqualifier(StrEnum): STALE_SELECTED_EVIDENCE; ACTIVE_HIGH_SEVERITY_CONFLICT; AMBIGUOUS_OBJECT_IDENTITY; MISSING_REQUIRED_SNAPSHOT; CONFIRMATION_PENDING_REVALIDATION
@dataclass class FieldPolicy: influence_max; display_rule; operational_rule; disqualifiers; resolution_mode
@dataclass class FieldResolution: display_value; load_bearing_value; unresolved_reason
def resolve_field_authority(evidence, policy, active_disqualifiers) -> FieldResolution

# safety_floor.py
class GovernanceAuthority(StrEnum): DATA_OWNER; SECURITY; PRIVACY; MODEL_RISK
SENSITIVITY_ORDER: tuple[str,...]                        # public<internal<confidential<restricted<prohibited
@dataclass class SafetyOverride: field; previous_floor; override_value; approved_by_authority; rationale; policy_reference; effective_until
def apply_sensitivity_floor(floor: str, proposals: list[str], override: SafetyOverride | None) -> str

# conflict_review.py
def conflict_fingerprint(logical_ref, field_name, competing_value_hashes, field_policy_version) -> str
class ConflictState(StrEnum): OPEN; ACKNOWLEDGED; RESOLVED; DISMISSED; STALE; REOPENED
def open_or_reopen_conflict(conn, *, fingerprint, ...) -> str
```

---

## Task 1: Assertion-strength axis on `overlay_evidence`

**Files:** Modify `src/featuregen/overlay/evidence.py`; Create `src/featuregen/db/migrations/0978_evidence_strength.sql`; Test `tests/featuregen/overlay/test_evidence_strength.py`.

**Interfaces:** Produces `EvidenceProducer`, `AssertionStrength`, extended `write_evidence`/`read_evidence`/`Evidence`.

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/overlay/test_evidence_strength.py
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer, read_evidence, write_evidence


def test_evidence_carries_producer_and_strength(db):
    eid = write_evidence(
        db, fact_key="fk1", table_snapshot_at=None, row_count=0, sample_size=0,
        profile_version="p1", thresholds_used={}, metric_values={}, created_by={"subject": "s"},
        producer=EvidenceProducer.LLM, strength=AssertionStrength.PROPOSED,
        producer_configuration_hash="cfg-abc", producer_item_ref="h1", evidence_spans=("balance",))
    ev = read_evidence(db, eid)
    assert ev.producer == "llm" and ev.strength == "proposed"
    assert ev.producer_configuration_hash == "cfg-abc" and ev.producer_item_ref == "h1"
    assert ev.evidence_spans == ["balance"]


def test_legacy_write_defaults_to_source_attested(db):
    # A caller that omits the new axis (existing profiler callers) still works, defaulting
    # to a governed producer/strength so existing behaviour is unchanged.
    eid = write_evidence(db, fact_key="fk2", table_snapshot_at=None, row_count=1, sample_size=1,
                         profile_version="p1", thresholds_used={}, metric_values={}, created_by={})
    ev = read_evidence(db, eid)
    assert ev.producer == "profiler" and ev.strength == "supported"
```

- [ ] **Step 2: Run test → FAIL** (`write_evidence() got an unexpected keyword argument 'producer'`).
Run: `uv run pytest tests/featuregen/overlay/test_evidence_strength.py -q`

- [ ] **Step 3a: Migration** `0978_evidence_strength.sql`:

```sql
-- Spec §3.1: an assertion-strength axis on the existing evidence record, so field authority can
-- reason over (producer, strength). Additive + defaulted so existing profiler rows/callers are
-- unchanged (they default to the governed producer=profiler / strength=supported).
ALTER TABLE overlay_evidence ADD COLUMN IF NOT EXISTS producer                    text NOT NULL DEFAULT 'profiler';
ALTER TABLE overlay_evidence ADD COLUMN IF NOT EXISTS strength                    text NOT NULL DEFAULT 'supported';
ALTER TABLE overlay_evidence ADD COLUMN IF NOT EXISTS producer_configuration_hash text NULL;
ALTER TABLE overlay_evidence ADD COLUMN IF NOT EXISTS producer_item_ref           text NULL;
ALTER TABLE overlay_evidence ADD COLUMN IF NOT EXISTS evidence_spans              jsonb NOT NULL DEFAULT '[]';
```

- [ ] **Step 3b: Extend `evidence.py`** — add the enums, the dataclass fields, and the `write_evidence`/`read_evidence` params:

```python
from enum import StrEnum

class EvidenceProducer(StrEnum):
    SOURCE = "source"; STRUCTURAL_CONNECTOR = "structural_connector"; PARSER = "parser"
    LLM = "llm"; PROFILER = "profiler"; TAXONOMY = "taxonomy"; HUMAN = "human"

class AssertionStrength(StrEnum):
    PROPOSED = "proposed"; SUPPORTED = "supported"; ATTESTED = "attested"; CONFIRMED = "confirmed"
```

Add to `Evidence` (frozen dataclass): `producer: str`, `strength: str`, `producer_configuration_hash: str | None`, `producer_item_ref: str | None`, `evidence_spans: list`. Extend `write_evidence(...)` with keyword-only `producer: EvidenceProducer = EvidenceProducer.PROFILER, strength: AssertionStrength = AssertionStrength.SUPPORTED, producer_configuration_hash: str | None = None, producer_item_ref: str | None = None, evidence_spans: tuple[str, ...] = ()` and include them in the INSERT (spans via `Jsonb(list(evidence_spans))`). Extend `read_evidence` to select/return them. Keep the defaults so existing callers (the profiler) are unchanged.

- [ ] **Step 4: Run test → PASS.**
- [ ] **Step 5: Commit** `feat(overlay): assertion-strength axis on evidence (spec §3.1)`.

---

## Task 2: Object identity status + no-attach guard

**Files:** Create `src/featuregen/overlay/object_identity.py`; Test `tests/featuregen/overlay/test_object_identity.py`.

**Interfaces:** Consumes `CatalogAdapter` (`overlay/catalog.py`), `CatalogObjectRef` (`overlay/identity.py`). Produces `ObjectIdentityStatus`, `ObjectBinding`, `resolve_object_identity`, `may_attach`.

- [ ] **Step 1: Failing test**

```python
# tests/featuregen/overlay/test_object_identity.py
from featuregen.overlay.identity import CatalogObjectRef
from featuregen.overlay.object_identity import (
    ObjectIdentityStatus, may_attach, resolve_object_identity)


class _FakeAdapter:
    def __init__(self, by_name): self._by_name = by_name   # display -> [native_id,...]
    def native_ids_for(self, ref): return self._by_name.get((ref.schema, ref.table, ref.column), [])


def _ref(c): return CatalogObjectRef("s", "column", "public", "accounts", c)


def test_exact_single_match_may_attach():
    b = resolve_object_identity(_FakeAdapter({("public","accounts","balance"): ["oid:1"]}), _ref("balance"))
    assert b.status == ObjectIdentityStatus.EXACT and may_attach(b)


def test_ambiguous_multi_match_blocks_attach():
    b = resolve_object_identity(_FakeAdapter({("public","accounts","amt"): ["oid:1","oid:2"]}), _ref("amt"))
    assert b.status == ObjectIdentityStatus.AMBIGUOUS and not may_attach(b)


def test_unresolved_no_match_blocks_attach():
    b = resolve_object_identity(_FakeAdapter({}), _ref("ghost"))
    assert b.status == ObjectIdentityStatus.UNRESOLVED and not may_attach(b)
```

- [ ] **Step 2: Run → FAIL** (module missing).

- [ ] **Step 3: Implement** `object_identity.py`:

```python
"""Spec §2: resolve a provider's native ref to a stable logical object, with an explicit status.
Evidence/proposals must NOT attach when identity is AMBIGUOUS/UNRESOLVED (else evidence from unrelated
physical objects merges). Wraps the existing CatalogAdapter's native-id lookup — it does NOT replace
identity.py's fact_key/ref model."""
from __future__ import annotations
from dataclasses import dataclass
from enum import StrEnum
from featuregen.overlay.identity import CatalogObjectRef

class ObjectIdentityStatus(StrEnum):
    EXACT = "exact"; ALIASED = "aliased"; AMBIGUOUS = "ambiguous"; UNRESOLVED = "unresolved"

@dataclass(frozen=True, slots=True)
class ObjectBinding:
    logical_ref: CatalogObjectRef | None
    status: ObjectIdentityStatus
    candidates: tuple[str, ...]

def resolve_object_identity(adapter, ref: CatalogObjectRef) -> ObjectBinding:
    native = tuple(adapter.native_ids_for(ref))
    if len(native) == 1:
        return ObjectBinding(ref, ObjectIdentityStatus.EXACT, native)
    if len(native) == 0:
        return ObjectBinding(None, ObjectIdentityStatus.UNRESOLVED, ())
    return ObjectBinding(None, ObjectIdentityStatus.AMBIGUOUS, native)

def may_attach(binding: ObjectBinding) -> bool:
    return binding.status in (ObjectIdentityStatus.EXACT, ObjectIdentityStatus.ALIASED)
```

(NOTE for the implementer: verify `CatalogAdapter`'s real method name for native-id lookup in `overlay/catalog.py` — the fake uses `native_ids_for`; if the real adapter exposes a different lookup (e.g. `owner_of`/`resolve`), adapt `resolve_object_identity` to the real method and update the fake to match. `ALIASED` is produced later when a confirmed rename mapping exists — leave the branch documented but exercised only by a mapping fixture.)

- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** `feat(overlay): object identity status + no-attach-when-ambiguous (spec §2)`.

---

## Task 3: Authority predicate tree + evaluator (pure)

**Files:** Create `src/featuregen/overlay/field_authority.py` (predicate half); Test `tests/featuregen/overlay/test_field_authority.py`.

**Interfaces:** Produces `AuthorityPredicate`, `HasEvidence`, `AnyOf`, `AllOf`, `evaluate`.

- [ ] **Step 1: Failing test**

```python
# tests/featuregen/overlay/test_field_authority.py
from featuregen.overlay.evidence import AssertionStrength as S, EvidenceProducer as P
from featuregen.overlay.field_authority import AllOf, AnyOf, HasEvidence, evaluate

STRUCT_ATTESTED = HasEvidence(P.STRUCTURAL_CONNECTOR, S.ATTESTED)
HUMAN_CONFIRMED = HasEvidence(P.HUMAN, S.CONFIRMED)


def test_any_of_satisfied_by_one():
    active = frozenset({(P.HUMAN, S.CONFIRMED)})
    assert evaluate(AnyOf((STRUCT_ATTESTED, HUMAN_CONFIRMED)), active)


def test_all_of_requires_every_condition():
    grain_review = AllOf((HasEvidence(P.LLM, S.PROPOSED), HasEvidence(P.PROFILER, S.SUPPORTED)))
    assert not evaluate(grain_review, frozenset({(P.LLM, S.PROPOSED)}))
    assert evaluate(grain_review, frozenset({(P.LLM, S.PROPOSED), (P.PROFILER, S.SUPPORTED)}))


def test_llm_proposed_never_satisfies_structural_rule():
    assert not evaluate(AnyOf((STRUCT_ATTESTED, HUMAN_CONFIRMED)), frozenset({(P.LLM, S.PROPOSED)}))
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** the predicate half of `field_authority.py`:

```python
"""Spec §4.1: the authority predicate language over (producer, strength). Pure — evaluated against the
set of ACTIVE evidence (producer, strength) pairs for a (logical_ref, field). This is the WHAT-authority
layer; it does NOT replace authority.py's WHO-confirms resolution."""
from __future__ import annotations
from dataclasses import dataclass
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer

class AuthorityPredicate: ...

@dataclass(frozen=True, slots=True)
class HasEvidence(AuthorityPredicate):
    producer: EvidenceProducer; strength: AssertionStrength

@dataclass(frozen=True, slots=True)
class AnyOf(AuthorityPredicate):
    conditions: tuple[AuthorityPredicate, ...]

@dataclass(frozen=True, slots=True)
class AllOf(AuthorityPredicate):
    conditions: tuple[AuthorityPredicate, ...]

def evaluate(pred: AuthorityPredicate, active: frozenset[tuple[EvidenceProducer, AssertionStrength]]) -> bool:
    if isinstance(pred, HasEvidence):
        return (pred.producer, pred.strength) in active
    if isinstance(pred, AnyOf):
        return any(evaluate(c, active) for c in pred.conditions)
    if isinstance(pred, AllOf):
        return all(evaluate(c, active) for c in pred.conditions)
    raise TypeError(f"unknown predicate {type(pred).__name__}")
```

- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** `feat(overlay): field authority predicate tree + evaluator (spec §4.1)`.

---

## Task 4: FieldPolicy + disqualifiers + two-output resolution (pure)

**Files:** Modify `src/featuregen/overlay/field_authority.py`; Test append to `test_field_authority.py`.

**Interfaces:** Consumes Task 3 `evaluate`. Produces `Disqualifier`, `InfluenceTier`, `ResolutionMode`, `FieldPolicy`, `FieldResolution`, `resolve_field_authority`.

- [ ] **Step 1: Failing test**

```python
# append to test_field_authority.py
from featuregen.overlay.field_authority import (
    Disqualifier, FieldPolicy, InfluenceTier, ResolutionMode, resolve_field_authority)

_POLICY = FieldPolicy(
    influence_max=InfluenceTier.OPERATIONAL,
    display_rule=HasEvidence(P.LLM, S.PROPOSED),
    operational_rule=AnyOf((HasEvidence(P.HUMAN, S.CONFIRMED),)),
    disqualifiers=(Disqualifier.AMBIGUOUS_OBJECT_IDENTITY, Disqualifier.STALE_SELECTED_EVIDENCE),
    resolution_mode=ResolutionMode.GENERIC_FIELD)


def test_display_shows_proposal_but_operational_unresolved():
    ev = [("llm", "proposed", "monetary_flow", "e1")]
    r = resolve_field_authority(ev, _POLICY, active_disqualifiers=frozenset())
    assert r.display_value == "monetary_flow" and r.load_bearing_value is None
    assert r.unresolved_reason == "authority_insufficient"


def test_human_confirmed_becomes_load_bearing():
    ev = [("llm","proposed","monetary_flow","e1"), ("human","confirmed","monetary_flow","e2")]
    r = resolve_field_authority(ev, _POLICY, active_disqualifiers=frozenset())
    assert r.load_bearing_value == "monetary_flow"


def test_disqualifier_blocks_even_when_rule_satisfied():
    ev = [("human","confirmed","monetary_flow","e2")]
    r = resolve_field_authority(ev, _POLICY, active_disqualifiers=frozenset({Disqualifier.STALE_SELECTED_EVIDENCE}))
    assert r.load_bearing_value is None and r.unresolved_reason == "disqualified:stale_selected_evidence"
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** — append to `field_authority.py`:

```python
from enum import StrEnum

class InfluenceTier(StrEnum):
    DISPLAY = "display"; RECOMMENDATION = "recommendation"; OPERATIONAL = "operational"

class ResolutionMode(StrEnum):
    GENERIC_FIELD = "generic_field"; SPECIALIZED_FACT = "specialized_fact"

class Disqualifier(StrEnum):
    STALE_SELECTED_EVIDENCE = "stale_selected_evidence"
    ACTIVE_HIGH_SEVERITY_CONFLICT = "active_high_severity_conflict"
    AMBIGUOUS_OBJECT_IDENTITY = "ambiguous_object_identity"
    MISSING_REQUIRED_SNAPSHOT = "missing_required_snapshot"
    CONFIRMATION_PENDING_REVALIDATION = "confirmation_pending_revalidation"

@dataclass(frozen=True, slots=True)
class FieldPolicy:
    influence_max: InfluenceTier
    display_rule: AuthorityPredicate
    operational_rule: AuthorityPredicate | None
    disqualifiers: tuple[Disqualifier, ...]
    resolution_mode: ResolutionMode

@dataclass(frozen=True, slots=True)
class FieldResolution:
    display_value: object | None
    load_bearing_value: object | None
    unresolved_reason: str | None

def _strongest(evidence: list[tuple[str, str, object, str]]) -> object | None:
    # evidence rows: (producer, strength, value, evidence_id); pick the value of the highest-strength row
    order = {"proposed": 0, "supported": 1, "attested": 2, "confirmed": 3}
    active = [e for e in evidence if e[1] in order]
    return max(active, key=lambda e: order[e[1]])[2] if active else None

def resolve_field_authority(evidence, policy: FieldPolicy,
                            active_disqualifiers: frozenset[Disqualifier]) -> FieldResolution:
    active_pairs = frozenset((e[0], e[1]) for e in evidence)
    display = _strongest(evidence) if evaluate(policy.display_rule, active_pairs) else None
    fired = active_disqualifiers & set(policy.disqualifiers)
    if fired:
        return FieldResolution(display, None, f"disqualified:{sorted(fired)[0]}")
    if policy.operational_rule is not None and evaluate(policy.operational_rule, active_pairs):
        return FieldResolution(display, _strongest(evidence), None)
    return FieldResolution(display, None, "authority_insufficient")
```

(NOTE: `resolve_field_authority` is the GENERIC_FIELD resolver. For `resolution_mode == SPECIALIZED_FACT`, load-bearing value comes exclusively from the specialized fact projection (`resolve_fact`), and this function returns only the display candidate — the specialized-fact wiring is Phase 2; here just honor the mode by never emitting a load-bearing value for it. Add a test asserting a SPECIALIZED_FACT policy yields `load_bearing_value is None` regardless of evidence.)

- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** `feat(overlay): FieldPolicy + disqualifiers + two-output field resolution (spec §4.2-4.4)`.

---

## Task 5: Safety-override authority + sensitivity floor

**Files:** Create `src/featuregen/overlay/safety_floor.py`; Migration `0979_safety_override.sql`; Test `tests/featuregen/overlay/test_safety_floor.py`.

**Interfaces:** Produces `GovernanceAuthority`, `SENSITIVITY_ORDER`, `SafetyOverride`, `apply_sensitivity_floor`, `record_safety_override`/`read_safety_override`.

- [ ] **Step 1: Failing test**

```python
# tests/featuregen/overlay/test_safety_floor.py
import pytest
from featuregen.overlay.safety_floor import (
    GovernanceAuthority, SafetyOverride, apply_sensitivity_floor)


def _ovr(val): return SafetyOverride(
    field="sensitivity", previous_floor="restricted", override_value=val,
    approved_by_authority=GovernanceAuthority.PRIVACY, rationale="tokenized", policy_reference="POL-1",
    effective_until=None)


def test_floor_holds_and_evidence_can_only_raise():
    # taxonomy floor restricted; an LLM/source proposal of a LOWER level cannot lower it
    assert apply_sensitivity_floor("restricted", ["public", "internal"], override=None) == "restricted"
    # a HIGHER proposal raises it
    assert apply_sensitivity_floor("internal", ["restricted"], override=None) == "restricted"


def test_below_floor_downgrade_requires_a_governed_override():
    with pytest.raises(PermissionError):
        # no override -> cannot go below floor even if a proposal says public
        apply_sensitivity_floor("restricted", ["public"], override=None, force_to="public")
    # with a governed override, the downgrade is permitted and audited by the caller
    assert apply_sensitivity_floor("restricted", ["public"], override=_ovr("internal"), force_to="internal") == "internal"
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3a: Migration** `0979_safety_override.sql`:

```sql
-- Spec §7: a governed below-floor sensitivity downgrade. Append-only, write-once — a downgrade is a
-- deliberate governance act requiring a specific authority + rationale + scope, never a generic confirm.
CREATE TABLE IF NOT EXISTS safety_override (
    override_id           text        PRIMARY KEY,
    fact_key              text        NOT NULL,
    field                 text        NOT NULL,
    previous_floor        text        NOT NULL,
    override_value        text        NOT NULL,
    approved_by_authority text        NOT NULL,   -- data_owner|security|privacy|model_risk
    rationale             text        NOT NULL,
    policy_reference      text        NOT NULL,
    effective_until       timestamptz NULL,
    created_by            jsonb       NOT NULL,
    created_at            timestamptz NOT NULL DEFAULT now()
);
```

- [ ] **Step 3b: Implement** `safety_floor.py`:

```python
"""Spec §7: sensitivity is a most-restrictive floor. Evidence may only RAISE it; a below-floor
downgrade requires a governed SafetyOverride (authority + rationale + scope). Distinct from the existing
compliance-gated policy_tag (a free-text basis) — this is a structured, authority-scoped downgrade."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

SENSITIVITY_ORDER: tuple[str, ...] = ("public", "internal", "confidential", "restricted", "prohibited")

class GovernanceAuthority(StrEnum):
    DATA_OWNER = "data_owner"; SECURITY = "security"; PRIVACY = "privacy"; MODEL_RISK = "model_risk"

@dataclass(frozen=True, slots=True)
class SafetyOverride:
    field: str; previous_floor: str; override_value: str
    approved_by_authority: GovernanceAuthority; rationale: str; policy_reference: str
    effective_until: datetime | None

def _rank(v: str) -> int:
    try: return SENSITIVITY_ORDER.index(v)
    except ValueError: return len(SENSITIVITY_ORDER)  # unknown -> most restrictive (fail closed)

def apply_sensitivity_floor(floor: str, proposals: list[str], *, override: SafetyOverride | None = None,
                            force_to: str | None = None) -> str:
    effective = max([floor, *proposals], key=_rank)          # evidence can only RAISE
    if force_to is None or _rank(force_to) >= _rank(effective):
        return effective
    # force_to is BELOW the effective floor -> only a governed override permits it
    if override is None or _rank(override.override_value) != _rank(force_to):
        raise PermissionError(f"below-floor downgrade to {force_to!r} requires a SafetyOverride")
    return force_to
```

Add `record_safety_override(conn, *, fact_key, override, created_by) -> str` (mint `sfo_` id, INSERT) and `read_safety_override(conn, override_id)`. Add a DB test for the round-trip.

- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** `feat(overlay): safety-override authority + sensitivity floor (spec §7)`.

---

## Task 6: Conflict-review lifecycle

**Files:** Create `src/featuregen/overlay/conflict_review.py`; Migration `0980_conflict_review.sql`; Test `tests/featuregen/overlay/test_conflict_review.py`.

**Interfaces:** Produces `conflict_fingerprint`, `ConflictState`, `open_or_reopen_conflict`, `transition_conflict`, `read_conflict`.

- [ ] **Step 1: Failing test**

```python
# tests/featuregen/overlay/test_conflict_review.py
from featuregen.overlay.conflict_review import (
    ConflictState, conflict_fingerprint, open_or_reopen_conflict, read_conflict, transition_conflict)


def _fp(): return conflict_fingerprint("public.accounts.balance", "sensitivity",
                                       ("hash_public", "hash_restricted"), "policy-v1")


def test_open_is_idempotent_on_fingerprint(db):
    a = open_or_reopen_conflict(db, fingerprint=_fp(), logical_ref="public.accounts.balance",
                                field_name="sensitivity", severity="high", competing_evidence_ids=("e1","e2"))
    b = open_or_reopen_conflict(db, fingerprint=_fp(), logical_ref="public.accounts.balance",
                                field_name="sensitivity", severity="high", competing_evidence_ids=("e1","e2"))
    assert a == b                                   # same fingerprint -> same conflict, not a duplicate
    assert read_conflict(db, a).state == ConflictState.OPEN


def test_resolved_then_same_fingerprint_reopens(db):
    cid = open_or_reopen_conflict(db, fingerprint=_fp(), logical_ref="r", field_name="sensitivity",
                                  severity="high", competing_evidence_ids=("e1",))
    transition_conflict(db, cid, ConflictState.RESOLVED, actor="u")
    again = open_or_reopen_conflict(db, fingerprint=_fp(), logical_ref="r", field_name="sensitivity",
                                    severity="high", competing_evidence_ids=("e1",))
    assert again == cid and read_conflict(db, cid).state == ConflictState.REOPENED
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3a: Migration** `0980_conflict_review.sql`:

```sql
-- Spec §10: a conflict record with a STABLE fingerprint so a re-upload updates/reopens rather than
-- duplicating. Distinct from quarantine (validation rows) and STALE/REVERIFY (per-fact re-verify).
CREATE TABLE IF NOT EXISTS conflict_review (
    conflict_id           text        PRIMARY KEY,
    fingerprint           text        NOT NULL UNIQUE,      -- reopen key
    logical_ref           text        NOT NULL,
    field_name            text        NOT NULL,
    severity              text        NOT NULL,
    competing_evidence_ids jsonb      NOT NULL DEFAULT '[]',
    state                 text        NOT NULL,             -- open|acknowledged|resolved|dismissed|stale|reopened
    owner                 text        NULL,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now()
);
```

- [ ] **Step 3b: Implement** `conflict_review.py`: `conflict_fingerprint(...)` = `sha256(json([logical_ref, field_name, sorted(competing_value_hashes), field_policy_version]))`; `ConflictState(StrEnum)`; `open_or_reopen_conflict` = `INSERT ... ON CONFLICT (fingerprint) DO UPDATE SET state = CASE WHEN conflict_review.state IN ('resolved','dismissed','stale') THEN 'reopened' ELSE conflict_review.state END, updated_at = now() RETURNING conflict_id` (mint `cfl_` id on first insert; on conflict return the existing id — use `ON CONFLICT ... DO UPDATE ... RETURNING conflict_id` which returns the row's id either way; the minted id is only used on a true insert); `transition_conflict(conn, conflict_id, new_state, *, actor)`; `read_conflict`. Provide a `now` seam consistent with the codebase (do not call `datetime.now` inline in a way tests can't control — accept an optional `now`, matching the overlay modules' pattern).

- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** `feat(overlay): conflict-review lifecycle with stable fingerprint (spec §10)`.

---

## Task 7: Governed `joins_to` seam (declared join → approved_join proposal), flag-gated

**Files:** Modify `src/featuregen/overlay/upload/ingest.py`, `src/featuregen/overlay/upload/graph.py`; Test `tests/featuregen/overlay/upload/test_governed_joins.py`.

**Interfaces:** Consumes `ApprovedJoinRef`/`ColumnPair`/`CatalogObjectRef` (`identity.py`), `join_write_error`, `propose_fact` (`proposal_commands.py`), the `approved_join` fact schema (`facts.py`). Produces `governed_join_proposal(row) -> ApprovedJoinRef | None` + a flag `OVERLAY_GOVERNED_JOINS`.

**Interfaces (consumes):** `build_graph`'s current ungoverned write at `graph.py:70-76` (and `112-117`).

- [ ] **Step 1: Failing test**

```python
# tests/featuregen/overlay/upload/test_governed_joins.py
# When OVERLAY_GOVERNED_JOINS=1, a declared joins_to yields an approved_join PROPOSAL (governed path),
# not just an ungoverned graph_edge. Default OFF preserves today's behaviour.
from featuregen.overlay.upload.graph import governed_join_proposal
from featuregen.overlay.upload.canonical import CanonicalRow


def test_declared_join_builds_an_approved_join_ref():
    row = CanonicalRow("deposits", "transactions", "account_id", "integer",
                       joins_to="accounts.id", cardinality="N:1")
    ref = governed_join_proposal(row)
    assert ref is not None
    assert ref.from_ref.table == "transactions" and ref.to_ref.table == "accounts"
    assert ref.cardinality == "N:1" and ref.column_pairs[0].from_col == "account_id"


def test_no_join_yields_none():
    assert governed_join_proposal(CanonicalRow("d", "t", "c", "text")) is None
```

- [ ] **Step 2: Run → FAIL** (`governed_join_proposal` undefined).

- [ ] **Step 3a: Add `governed_join_proposal`** to `graph.py` — pure builder from a `CanonicalRow`'s declared `joins_to` (`"table.column"`) into an `ApprovedJoinRef`:

```python
def governed_join_proposal(r):
    if not r.joins_to:
        return None
    to_table, _, to_col = r.joins_to.partition(".")
    if not to_col:
        return None
    frm = CatalogObjectRef(r.source, "column", _SCHEMA, r.table, r.column)
    to = CatalogObjectRef(r.source, "column", _SCHEMA, to_table, to_col)
    return ApprovedJoinRef(from_ref=frm, to_ref=to,
                           column_pairs=(ColumnPair(from_col=r.column, to_col=to_col),),
                           cardinality=(r.cardinality or "N:1"))
```

(Import `ApprovedJoinRef`, `ColumnPair`, `CatalogObjectRef` from `overlay.identity`. `_SCHEMA` already exists in graph.py.)

- [ ] **Step 3b: Flag-gated governed routing in ingest.** In `ingest.py`, after `build_graph`, when `os.environ.get("OVERLAY_GOVERNED_JOINS") == "1"`, for each `vr.good` row with a declared join, build `governed_join_proposal(row)` and submit it via the existing `propose_fact` command path (fact_type `approved_join`), guarded by `join_write_error` (reuse — do NOT reimplement). Advisory/fail-soft: a proposal failure logs and never aborts the upload. Default (flag unset) = today's behaviour, unchanged.

- [ ] **Step 3c: Gate the raw edge to display-only.** In `graph.py`, when the governed flag is on, keep writing the `graph_edge 'joins'` row (search/lineage still show it) but the plan's follow-on (Phase 3) makes feature-use read the governed `approved_join` projection, not the raw edge. Add a code comment + a `docs` note recording the **retirement deadline** (end of Phase 3): the raw edge becomes display-only, feature-use goes through `approved_join`.

- [ ] **Step 4: Run → PASS** (plus an ingest-level test with the flag on asserting a proposal is recorded and the upload still succeeds; use the existing `test_ingest_slice.py` scaffolding + a catalog adapter as `test_join_confirmation`/`propose_fact` tests do — inspect those for the fixture pattern).

- [ ] **Step 5: Commit** `feat(overlay): governed joins_to seam via approved_join proposal, flag-gated (spec §12.1)`.

---

## Self-Review

**Spec coverage (§0 new surface):** (a) strength axis → Task 1; (b) identity status → Task 2; (c) field-authority policy + disqualifiers → Tasks 3-4; (d) safety override → Task 5; (e) conflict lifecycle → Task 6; (f) governed joins_to → Task 7. Readiness scope (contract 8) intentionally **deferred to Phase 2** (noted in spec §0/§17). Assertion-strength *propagation* (§3.2) is a resolver rule exercised once evidence carries strength (Task 1) + the policy evaluator (Tasks 3-4) — a dedicated propagation helper is a small Phase-1 addition when taxonomy-derived evidence is first written; noted, not built here (no producer emits derived evidence yet in Phase 0).

**Reuse guardrails:** No task creates a second event log or confirmation flow. Task 7 explicitly reuses `propose_fact` + `join_write_error` + `approved_join` rather than a new join authority. Tasks 3-4 add the WHAT-authority layer *alongside* `resolve_authority` (WHO-confirms), not replacing it. If an implementer finds an existing module already provides a task's capability, that's a STOP-and-report (the reuse map may be more complete than assumed).

**Verification-before-build flags for the implementer:** (1) confirm the next free migration slot on `main` (0978+ may be taken); (2) confirm `CatalogAdapter`'s real native-id lookup method (Task 2 fake assumes `native_ids_for`); (3) confirm `propose_fact`'s `Command`/`current_catalog_adapter()` construction from the existing `test_join_confirmation`/`proposal_commands` tests before wiring Task 7; (4) the `now` seam convention in overlay modules (Task 6) — match `expiry.py`/`confirmation_commands.py`.

**Type consistency:** `EvidenceProducer`/`AssertionStrength` defined in Task 1 are imported by Tasks 3-4 and 7's evidence; `AuthorityPredicate`/`evaluate` from Task 3 are consumed by Task 4's `resolve_field_authority`; `ObjectIdentityStatus` (Task 2) feeds the `AMBIGUOUS_OBJECT_IDENTITY` disqualifier (Task 4). Migration numbers 0978/0979/0980 are distinct.

## Execution Handoff

After saving, two execution options: **(1) Subagent-Driven (recommended)** — fresh subagent per task + two-stage review; **(2) Inline Execution** — batched with checkpoints. NOTE: Tasks 2 and 7 carry real "verify the existing signature first" risk (catalog adapter method; propose_fact command construction) — an implementer should read the named existing tests before those tasks rather than trust the fakes verbatim.

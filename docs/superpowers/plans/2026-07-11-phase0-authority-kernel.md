# Phase 0 — Authority Kernel (Extension) Implementation Plan (v2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add the authority-kernel capabilities from the spec's §0 reuse map — assertion-strength+lifecycle axis, object identity-status (with logical/provider wrappers), a **field-specific** authority policy + resolver, safety-override authority, a conflict-review lifecycle **with audit history**, **field-decision-event persistence**, and a governed-`joins_to` seam **with enforced display-only edges** — as **extensions** of the existing overlay fact substrate.

**v2 changes (folding in the plan review's 10 must-fixes + should-fixes):** Task 4's resolver is now **field-specific (ConflictStrategy)**, not global-strength; evidence is a **typed `FieldEvidenceView`**, not raw tuples; Task 1 adds an **evidence-lifecycle** column + a **caller audit** step; **Task 8 (field-decision persistence)** added so this is genuinely the kernel; Task 5's `SafetyOverride` gains scope/effective-time/authority-allowlist + unknown-sensitivity fail-closed; Task 6 gains a **transition-history** table + `competing_value_hashes`; Task 7 gains a **join parser with diagnostics** + a **consumer audit** + **enforced display-only** edge authority + ingestion-level tests; predicates reject **empty AnyOf/AllOf**; `influence_max` is **enforced**; `evidence_spans` is a **tuple** contract.

**Architecture:** Extends `facts.py`/`state.py`/`resolve.py`/`authority.py`/`confirmation_commands.py`/`expiry.py`/`identity.py`/`proposal_commands.py`. No parallel event log or confirmation flow. Readiness scope (spec contract 8) deferred to Phase 2.

**Tech Stack:** Python 3.12, psycopg raw SQL, pytest (live PG `db`/`conn` fixture), `uv`.

**Spec:** `docs/superpowers/specs/2026-07-11-evidence-authority-ingestion.md` (v4) — §0 reuse map, §3.1, §2, §4, §7, §10, §12.1.

## Global Constraints

- **Extension, not replacement.** Reuse the overlay substrate; no second event log/confirmation flow. If a task appears to duplicate an existing capability, STOP and report.
- **Field authority is field-specific, never one global ranking.** The resolver selects effective values through the field's `ConflictStrategy`, not by max-strength (spec v2's core fix).
- **provenance ≠ authority; confidence ≠ permission.** A derived value's strength ≤ min of its inputs' strengths.
- **Fail-closed safety.** Sensitivity is a most-restrictive floor; below-floor downgrade requires a governed `SafetyOverride` by a permitted authority; unknown sensitivity → `prohibited`, never persisted as effective.
- **No-attach-when-ambiguous.** Evidence must not attach when identity status is `AMBIGUOUS`/`UNRESOLVED`.
- **Type safety.** Producer/strength/lifecycle are the Task-1 enums everywhere (no raw strings in resolver inputs). Immutable contracts: `evidence_spans: tuple[str, ...]`.
- **Migrations forward-only; apply BEFORE deploying code.** Additive `NOT NULL DEFAULT` columns cover existing rows (no backfill); new code reading pre-migration DB fails, so migrate first. Verify the next free slot on `main` (0978+ may be taken).
- **Conventions:** raw SQL via `conn.execute`; `from __future__ import annotations`; frozen slotted dataclasses; a `now` seam (accept optional `now`, matching `expiry.py`/`confirmation_commands.py`); ruff + mypy clean; TDD; commit per task; `uv run pytest <path> -q`.

---

## File Structure

- Modify `overlay/evidence.py` + migration `0978_evidence_axes.sql` — producer + strength + **lifecycle** + config-hash + item-ref + spans.
- Create `overlay/object_identity.py` — `LogicalObjectRef`/`ProviderObjectRef` wrappers, `ObjectIdentityStatus`, `resolve_object_identity`, `may_attach`.
- Create `overlay/field_authority.py` — predicate tree (+ empty rejection), `FieldEvidenceView`, `ConflictStrategy`, `FieldPolicy`, `resolve_field_authority` (field-specific, influence-enforced).
- Create `overlay/safety_floor.py` + migration `0979_safety_override.sql` — floor + governed override with validation.
- Create `overlay/conflict_review.py` + migration `0980_conflict_review.sql` — record + **event history** + fingerprint (+ value hashes).
- Create `overlay/field_decision.py` + migration `0981_field_decision_event.sql` — append-only field-decision events.
- Modify `overlay/upload/ingest.py`, `overlay/upload/graph.py` + migration `0982_graph_edge_authority.sql` — `parse_join_ref`, governed seam, **display-only edge authority**.

Locked interfaces (dependencies across tasks):

```python
# evidence.py
class EvidenceProducer(StrEnum): SOURCE; STRUCTURAL_CONNECTOR; PARSER; LLM; PROFILER; TAXONOMY; HUMAN; LEGACY
class AssertionStrength(StrEnum): PROPOSED; SUPPORTED; ATTESTED; CONFIRMED
class EvidenceLifecycle(StrEnum): ACTIVE; STALE; REJECTED; SUPERSEDED
# object_identity.py
class ObjectIdentityStatus(StrEnum): EXACT; ALIASED; AMBIGUOUS; UNRESOLVED
@dataclass class LogicalObjectRef: logical_catalog_id; schema; table; column
@dataclass class ProviderObjectRef: provider_id; provider_snapshot_id; native_ref
@dataclass class ObjectBinding: logical_ref: LogicalObjectRef | None; status; candidates
# field_authority.py
class HasEvidence/AnyOf/AllOf(AuthorityPredicate)
def evaluate(pred, active: frozenset[tuple[EvidenceProducer, AssertionStrength]]) -> bool
@dataclass class FieldEvidenceView: producer: EvidenceProducer; strength: AssertionStrength; value; evidence_id
class ConflictStrategy(StrEnum): PREFER_CONFIRMED; MOST_RESTRICTIVE; UNION_CLASSES; UNRESOLVED_ON_CONFLICT
class InfluenceTier(StrEnum): DISPLAY; RECOMMENDATION; OPERATIONAL
class ResolutionMode(StrEnum): GENERIC_FIELD; SPECIALIZED_FACT
class Disqualifier(StrEnum): ...
@dataclass class FieldPolicy: influence_max; display_rule; operational_rule; disqualifiers; resolution_mode; conflict_strategy
@dataclass class FieldResolution: display_value; load_bearing_value; unresolved_reason
def resolve_field_authority(evidence: list[FieldEvidenceView], policy, active_disqualifiers) -> FieldResolution
# safety_floor.py
class GovernanceAuthority(StrEnum): DATA_OWNER; SECURITY; PRIVACY; MODEL_RISK
DOWNGRADE_AUTHORITIES: frozenset[GovernanceAuthority]
SENSITIVITY_ORDER: tuple[str, ...]
@dataclass class SafetyOverride: fact_key; field; previous_floor; override_value; approved_by_authority; rationale; policy_reference; effective_from; effective_until
def apply_sensitivity_floor(floor, proposals, *, override=None, force_to=None, now=None) -> str
# conflict_review.py
def conflict_fingerprint(logical_ref, field_name, competing_value_hashes, field_policy_version) -> str
class ConflictState(StrEnum): OPEN; ACKNOWLEDGED; RESOLVED; DISMISSED; STALE; REOPENED
# field_decision.py
def record_field_decision(conn, *, logical_ref, field_name, event_type, ...) -> str
# graph.py
def parse_join_ref(joins_to: str) -> ParsedJoinTarget   # .ok / .diagnostic
def governed_join_proposal(row) -> ApprovedJoinRef | None
```

---

## Task 1: Evidence axes — producer, strength, lifecycle (+ item linkage)

**Files:** Modify `overlay/evidence.py`; Create `0978_evidence_axes.sql`; Test `tests/featuregen/overlay/test_evidence_axes.py`.

- [ ] **Step 0 (caller audit — do FIRST):** `grep -rn "write_evidence(" src/ tests/`. Classify every caller. The default `producer=PROFILER, strength=SUPPORTED` is correct ONLY if all existing callers write profiling evidence. Record the finding in the report. If any caller writes source/human evidence, upgrade THAT caller explicitly in this task (Option A) rather than relying on the default; if unsure, use `producer=LEGACY` for the default and note the follow-up. Do not silently mis-authorize legacy rows.

- [ ] **Step 1: Failing test**

```python
# tests/featuregen/overlay/test_evidence_axes.py
from featuregen.overlay.evidence import (
    AssertionStrength, EvidenceLifecycle, EvidenceProducer, read_evidence, write_evidence)


def test_evidence_carries_producer_strength_lifecycle_and_linkage(db):
    eid = write_evidence(
        db, fact_key="fk1", table_snapshot_at=None, row_count=0, sample_size=0, profile_version="p1",
        thresholds_used={}, metric_values={}, created_by={"subject": "s"},
        producer=EvidenceProducer.LLM, strength=AssertionStrength.PROPOSED,
        lifecycle=EvidenceLifecycle.ACTIVE, producer_configuration_hash="cfg",
        producer_item_ref="h1", evidence_spans=("balance",))
    ev = read_evidence(db, eid)
    assert ev.producer == "llm" and ev.strength == "proposed" and ev.lifecycle == "active"
    assert ev.producer_configuration_hash == "cfg" and ev.producer_item_ref == "h1"
    assert ev.evidence_spans == ("balance",)          # TUPLE contract, not list


def test_legacy_write_defaults(db):
    ev = read_evidence(db, write_evidence(
        db, fact_key="fk2", table_snapshot_at=None, row_count=1, sample_size=1, profile_version="p1",
        thresholds_used={}, metric_values={}, created_by={}))
    assert ev.producer == "profiler" and ev.strength == "supported" and ev.lifecycle == "active"
```

- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3a: Migration** `0978_evidence_axes.sql` — `ALTER TABLE overlay_evidence ADD COLUMN IF NOT EXISTS` for `producer text NOT NULL DEFAULT 'profiler'`, `strength text NOT NULL DEFAULT 'supported'`, `lifecycle text NOT NULL DEFAULT 'active'`, `producer_configuration_hash text NULL`, `producer_item_ref text NULL`, `evidence_spans jsonb NOT NULL DEFAULT '[]'`.
- [ ] **Step 3b:** Add the three enums (incl. `EvidenceLifecycle`, and `LEGACY` on producer). Extend `Evidence` with `producer/strength/lifecycle/producer_configuration_hash/producer_item_ref/evidence_spans` (spans typed `tuple[str, ...]` — convert the JSON list to a tuple in `read_evidence`). Extend `write_evidence` with the keyword-only params (defaults per Step 0) and INSERT them (`Jsonb(list(evidence_spans))`).
- [ ] **Step 4: Run → PASS.**  **Step 5: Commit** `feat(overlay): evidence producer/strength/lifecycle axes (spec §3.1)`.

---

## Task 2: Object identity — logical/provider wrappers + status + no-attach guard

**Files:** Create `overlay/object_identity.py`; Test `tests/featuregen/overlay/test_object_identity.py`.

**Framing (honest):** This is a **compatibility layer** over the existing `CatalogObjectRef`/`CatalogAdapter`, introducing the spec's Layer-0 wrappers so later phases have the identity seam. It does NOT yet implement full alias/rename-mapping resolution — that is a documented follow-up.

- [ ] **Step 1: Failing test** (as v1, plus:)

```python
def test_logical_and_provider_wrappers_roundtrip():
    from featuregen.overlay.object_identity import LogicalObjectRef, ProviderObjectRef
    lr = LogicalObjectRef("cat1", "public", "accounts", "balance")
    pr = ProviderObjectRef("ftr_glossary", "snap1", "public.accounts.balance")
    assert lr.column == "balance" and pr.provider_id == "ftr_glossary"
```

- [ ] **Step 3: Implement** — add `LogicalObjectRef` + `ProviderObjectRef` frozen dataclasses; `ObjectIdentityStatus`; `ObjectBinding(logical_ref: LogicalObjectRef | None, status, candidates)`; `resolve_object_identity(adapter, ref) -> ObjectBinding` (1 native id → EXACT with a LogicalObjectRef built from the ref; 0 → UNRESOLVED; >1 → AMBIGUOUS); `may_attach`. **VERIFY** the adapter's real native-id method in `overlay/catalog.py` before finalizing (the fake uses `native_ids_for`). Document that `ALIASED` + rename-mapping resolution is a follow-up (not built here).
- [ ] **Steps 2/4/5** as standard. Commit `feat(overlay): object identity status + logical/provider wrappers (spec §2)`.

---

## Task 3: Authority predicate tree + evaluator (empty-safe)

**Files:** Create `overlay/field_authority.py` (predicate half); Test `tests/featuregen/overlay/test_field_authority.py`.

- [ ] **Step 1: Failing test** (v1 tests, plus:)

```python
import pytest
from featuregen.overlay.field_authority import AllOf, AnyOf

def test_empty_predicates_are_rejected():
    with pytest.raises(ValueError): AllOf(())    # all([]) == True would authorize everything
    with pytest.raises(ValueError): AnyOf(())
```

- [ ] **Step 3: Implement** `HasEvidence/AnyOf/AllOf` + `evaluate` as v1, but `AnyOf`/`AllOf` raise `ValueError` in `__post_init__` when `conditions` is empty.
- [ ] Standard steps. Commit `feat(overlay): authority predicate tree + evaluator, empty-safe (spec §4.1)`.

---

## Task 4: Field authority resolver — typed, field-specific, influence-enforced

**Files:** Modify `overlay/field_authority.py`; Test append.

**This is the load-bearing task.** It must NOT pick by global strength.

- [ ] **Step 1: Failing test**

```python
from featuregen.overlay.evidence import AssertionStrength as S, EvidenceProducer as P
from featuregen.overlay.field_authority import (
    AnyOf, ConflictStrategy, Disqualifier, FieldEvidenceView, FieldPolicy, HasEvidence,
    InfluenceTier, ResolutionMode, resolve_field_authority)

def _pol(**kw):
    base = dict(influence_max=InfluenceTier.OPERATIONAL, display_rule=HasEvidence(P.LLM, S.PROPOSED),
                operational_rule=AnyOf((HasEvidence(P.HUMAN, S.CONFIRMED),)),
                disqualifiers=(Disqualifier.STALE_SELECTED_EVIDENCE,),
                resolution_mode=ResolutionMode.GENERIC_FIELD,
                conflict_strategy=ConflictStrategy.PREFER_CONFIRMED)
    base.update(kw); return FieldPolicy(**base)

def _ev(p, s, v): return FieldEvidenceView(p, s, v, f"e-{v}")

def test_display_proposal_operational_unresolved():
    r = resolve_field_authority([_ev(P.LLM, S.PROPOSED, "monetary_flow")], _pol(), frozenset())
    assert r.display_value == "monetary_flow" and r.load_bearing_value is None

def test_prefer_confirmed_selects_confirmed_value_not_highest_only():
    ev = [_ev(P.LLM, S.PROPOSED, "monetary_flow"), _ev(P.HUMAN, S.CONFIRMED, "monetary_stock")]
    r = resolve_field_authority(ev, _pol(), frozenset())
    assert r.load_bearing_value == "monetary_stock"        # confirmed value, chosen by strategy

def test_unresolved_on_conflict_blocks_when_values_disagree():
    pol = _pol(conflict_strategy=ConflictStrategy.UNRESOLVED_ON_CONFLICT,
               operational_rule=AnyOf((HasEvidence(P.STRUCTURAL_CONNECTOR, S.ATTESTED),
                                       HasEvidence(P.HUMAN, S.CONFIRMED))))
    ev = [_ev(P.HUMAN, S.CONFIRMED, "account"), _ev(P.STRUCTURAL_CONNECTOR, S.ATTESTED, "transaction")]
    r = resolve_field_authority(ev, pol, frozenset())
    assert r.load_bearing_value is None and r.unresolved_reason == "conflict"

def test_influence_max_below_operational_never_load_bearing():
    r = resolve_field_authority([_ev(P.HUMAN, S.CONFIRMED, "x")],
                                _pol(influence_max=InfluenceTier.RECOMMENDATION), frozenset())
    assert r.load_bearing_value is None and r.unresolved_reason == "influence_not_operational"

def test_disqualifier_blocks_even_when_satisfied():
    r = resolve_field_authority([_ev(P.HUMAN, S.CONFIRMED, "x")], _pol(),
                                frozenset({Disqualifier.STALE_SELECTED_EVIDENCE}))
    assert r.load_bearing_value is None and r.unresolved_reason.startswith("disqualified:")

def test_specialized_fact_mode_never_load_bearing():
    r = resolve_field_authority([_ev(P.HUMAN, S.CONFIRMED, "grain")],
                                _pol(resolution_mode=ResolutionMode.SPECIALIZED_FACT), frozenset())
    assert r.load_bearing_value is None and r.unresolved_reason == "specialized_fact"
```

- [ ] **Step 3: Implement** — the resolver docstring MUST state: *evidence passed here is already lifecycle-filtered to ACTIVE; stale/rejected/superseded excluded or supplied as disqualifiers.* Logic order:
  1. `active_pairs = frozenset((e.producer, e.strength) for e in evidence)`.
  2. `display = _select(evidence, ConflictStrategy.PREFER_CONFIRMED)` if `evaluate(display_rule, active_pairs)` else None. (Display always uses a lenient strategy.)
  3. If `resolution_mode == SPECIALIZED_FACT`: return `FieldResolution(display, None, "specialized_fact")` (operational truth comes from the specialized fact projection, not here).
  4. If `influence_max != OPERATIONAL`: return `FieldResolution(display, None, "influence_not_operational")`.
  5. `fired = active_disqualifiers & set(policy.disqualifiers)`; if fired → `FieldResolution(display, None, f"disqualified:{sorted(fired)[0]}")`.
  6. If `operational_rule` is None or not `evaluate(...)` → `FieldResolution(display, None, "authority_insufficient")`.
  7. Otherwise `lb = _select(evidence, policy.conflict_strategy)`; if `lb is _CONFLICT` → `FieldResolution(display, None, "conflict")`; else `FieldResolution(display, lb, None)`.

  `_select(evidence, strategy)` implements the field-specific merge:
  - `PREFER_CONFIRMED`: among values, prefer the one backed by the highest strength (confirmed > attested > supported > proposed); ties on distinct values → treat as conflict.
  - `MOST_RESTRICTIVE`: for ordered fields (sensitivity) return the max by the field's severity order (the caller supplies the order; for Phase 0 accept an optional `severity_order` on the policy or delegate to `safety_floor`).
  - `UNION_CLASSES`: return the sorted union of all values (multi-valued fields like sensitivity_classes).
  - `UNRESOLVED_ON_CONFLICT`: if all active values are equal → that value; else the sentinel `_CONFLICT`.

- [ ] Standard steps. Commit `feat(overlay): field-specific authority resolver (typed, conflict-strategy, influence-enforced) (spec §4.2-4.4, §6.2)`.

---

## Task 5: Safety-override authority + sensitivity floor (validated)

**Files:** Create `overlay/safety_floor.py`; Migration `0979_safety_override.sql`; Test `tests/featuregen/overlay/test_safety_floor.py`.

- [ ] **Step 1: Failing test**

```python
import pytest
from datetime import UTC, datetime
from featuregen.overlay.safety_floor import (
    GovernanceAuthority, SafetyOverride, apply_sensitivity_floor)

def _ovr(val, auth=GovernanceAuthority.PRIVACY, until=None):
    return SafetyOverride(fact_key="fk", field="sensitivity", previous_floor="restricted",
        override_value=val, approved_by_authority=auth, rationale="tokenized",
        policy_reference="POL-1", effective_from=None, effective_until=until)

def test_floor_holds_and_evidence_can_only_raise():
    assert apply_sensitivity_floor("restricted", ["public", "internal"]) == "restricted"
    assert apply_sensitivity_floor("internal", ["restricted"]) == "restricted"

def test_unknown_sensitivity_is_prohibited_not_persisted_verbatim():
    assert apply_sensitivity_floor("internal", ["top_secret"]) == "prohibited"

def test_below_floor_downgrade_requires_permitted_authority():
    with pytest.raises(PermissionError):
        apply_sensitivity_floor("restricted", ["public"], override=None, force_to="public")
    with pytest.raises(PermissionError):  # DATA_OWNER not permitted to downgrade
        apply_sensitivity_floor("restricted", ["public"],
                                override=_ovr("internal", GovernanceAuthority.DATA_OWNER), force_to="internal")
    assert apply_sensitivity_floor("restricted", ["public"], override=_ovr("internal"),
                                   force_to="internal") == "internal"

def test_expired_override_is_rejected():
    past = datetime(2020, 1, 1, tzinfo=UTC)
    with pytest.raises(PermissionError):
        apply_sensitivity_floor("restricted", ["public"], override=_ovr("internal", until=past),
                                force_to="internal", now=datetime(2026, 7, 11, tzinfo=UTC))
```

- [ ] **Step 3:** `SENSITIVITY_ORDER`; `GovernanceAuthority`; `DOWNGRADE_AUTHORITIES = frozenset({PRIVACY, SECURITY})`; `SafetyOverride` (with `fact_key`, `effective_from/until`); `apply_sensitivity_floor(floor, proposals, *, override=None, force_to=None, now=None)` that: normalizes unknown values → `prohibited`; raises floor by evidence; on a below-floor `force_to`, requires an override whose `field=="sensitivity"`, `previous_floor==floor`, `override_value==force_to`, `approved_by_authority in DOWNGRADE_AUTHORITIES`, and is currently effective (`now` within `[effective_from, effective_until]`). Add `record_safety_override`/`read_safety_override` + a DB round-trip test. Migration `0979` = the `safety_override` table (write-once).
- [ ] Standard steps. Commit `feat(overlay): safety-override authority + validated sensitivity floor (spec §7)`.

---

## Task 6: Conflict-review lifecycle + audit history

**Files:** Create `overlay/conflict_review.py`; Migration `0980_conflict_review.sql`; Test `tests/featuregen/overlay/test_conflict_review.py`.

- [ ] **Step 1: Failing test** (v1 idempotency + reopen tests, plus:)

```python
def test_transitions_are_recorded_in_history(db):
    from featuregen.overlay.conflict_review import (
        ConflictState, conflict_events, open_or_reopen_conflict, transition_conflict)
    cid = open_or_reopen_conflict(db, fingerprint="fp", logical_ref="r", field_name="sensitivity",
        severity="high", competing_evidence_ids=("e1",), competing_value_hashes=("h1","h2"))
    transition_conflict(db, cid, ConflictState.ACKNOWLEDGED, actor="alice", reason="reviewing")
    transition_conflict(db, cid, ConflictState.RESOLVED, actor="bob", reason="tokenized")
    hist = conflict_events(db, cid)
    assert [(h.to_state, h.actor) for h in hist][-2:] == [("acknowledged","alice"),("resolved","bob")]
```

- [ ] **Step 3a: Migration** `0980_conflict_review.sql` — the `conflict_review` table (as v1) **plus `competing_value_hashes jsonb NOT NULL DEFAULT '[]'`**, and a child audit table:

```sql
CREATE TABLE IF NOT EXISTS conflict_review_event (
    event_id     text        PRIMARY KEY,
    conflict_id  text        NOT NULL REFERENCES conflict_review(conflict_id),
    from_state   text        NULL,
    to_state     text        NOT NULL,
    actor        text        NOT NULL,
    reason       text        NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);
```

- [ ] **Step 3b:** `conflict_fingerprint(...)` (over value hashes + policy version); `open_or_reopen_conflict(... , competing_value_hashes)` writing the value hashes and emitting an initial `conflict_review_event` (to_state OPEN/REOPENED); `transition_conflict(conn, cid, new_state, *, actor, reason=None, now=None)` updating `conflict_review.state` AND appending a `conflict_review_event`; `conflict_events(conn, cid)`; `read_conflict`.
- [ ] Standard steps. Commit `feat(overlay): conflict-review lifecycle with audit history + value-hash fingerprint (spec §10)`.

---

## Task 7: Governed `joins_to` seam — parser + display-only edge + ingestion tests

**Files:** Modify `overlay/upload/graph.py`, `overlay/upload/ingest.py`; Migration `0982_graph_edge_authority.sql`; Test `tests/featuregen/overlay/upload/test_governed_joins.py`.

- [ ] **Step 0 (consumer audit — do FIRST):** `grep -rn "'joins'|\"joins\"|kind = 'joins'|column_joins" src/featuregen/` to enumerate every reader of `graph_edge 'joins'`. Record in the report whether any **feature-construction / operational** code reads it (vs search/lineage display). This decides Step 3c's enforcement.

- [ ] **Step 1: Failing test**

```python
# tests/featuregen/overlay/upload/test_governed_joins.py
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.graph import governed_join_proposal, parse_join_ref


def test_parse_table_column_and_schema_qualified():
    assert parse_join_ref("accounts.id").ok and parse_join_ref("accounts.id").to_table == "accounts"
    q = parse_join_ref("public.accounts.id"); assert q.ok and q.to_table == "accounts" and q.to_col == "id"

def test_malformed_join_yields_diagnostic_not_silent_none():
    bad = parse_join_ref("accounts")            # no column
    assert not bad.ok and bad.diagnostic        # a reason, not a silent drop

def test_declared_join_builds_approved_join_ref():
    ref = governed_join_proposal(CanonicalRow("deposits","transactions","account_id","integer",
                                              joins_to="accounts.id", cardinality="N:1"))
    assert ref.from_ref.table == "transactions" and ref.to_ref.table == "accounts"
    assert ref.cardinality == "N:1" and ref.column_pairs[0].from_col == "account_id"
```

- [ ] **Step 3a:** `parse_join_ref(joins_to) -> ParsedJoinTarget` (frozen dataclass `ok: bool`, `to_table`, `to_col`, `diagnostic: str | None`): supports `table.column` AND `schema.table.column`; rejects empty table/column with a diagnostic; a malformed join returns `ok=False` with a reason (the caller raises a quarantine/review diagnostic — do NOT silently drop). `governed_join_proposal(row)` builds the `ApprovedJoinRef` from a well-formed parse (else None).
- [ ] **Step 3b:** In `ingest.py`, behind `os.environ.get("OVERLAY_GOVERNED_JOINS")=="1"`, route each declared join via the existing `propose_fact` (`approved_join`), guarded by `join_write_error` (reuse). Advisory/fail-soft. **VERIFY** `propose_fact`'s `Command`/`current_catalog_adapter()` construction against `test_join_confirmation`/`proposal_commands` tests before wiring. A malformed join → a quarantine diagnostic, not a crash.
- [ ] **Step 3c:** Migration `0982_graph_edge_authority.sql` = `ALTER TABLE graph_edge ADD COLUMN IF NOT EXISTS authority text NOT NULL DEFAULT 'operational'`. When the flag is on, the `'joins'` edge is written with `authority='display_only'`. **If Step 0 found operational consumers**, update those queries to `AND authority='operational'` so a display-only edge is not used for feature construction (a code comment is NOT sufficient — the review's point). Record the retirement deadline (end of Phase 3): raw `'joins'` becomes display-only unconditionally, feature-use reads the `approved_join` projection.
- [ ] **Step 4:** Tests: the pure builder/parser tests above, PLUS an **ingestion-level** test (using `test_ingest_slice.py` scaffolding + a catalog adapter as the join tests do): flag OFF → today's behaviour (operational edge, no proposal); flag ON → an `approved_join` proposal exists AND the edge is `display_only` AND the upload still succeeds.
- [ ] **Step 5: Commit** `feat(overlay): governed joins_to seam — parser+diagnostics, approved_join proposal, display-only edge (spec §12.1)`.

---

## Task 8: Field-decision-event persistence

**Files:** Create `overlay/field_decision.py`; Migration `0981_field_decision_event.sql`; Test `tests/featuregen/overlay/test_field_decision.py`.

Makes this genuinely the kernel (the review's issue 5): the resolver's outputs are persisted as append-only, replayable decisions.

- [ ] **Step 1: Failing test**

```python
# tests/featuregen/overlay/test_field_decision.py
from featuregen.overlay.field_decision import read_field_decisions, record_field_decision

def test_field_decision_is_append_only_and_replayable(db):
    e1 = record_field_decision(db, logical_ref="public.accounts.balance", field_name="concept",
        event_type="resolved", selected_evidence_ids=("e1",), evidence_set_hash="es1",
        display_value_hash="dh", load_bearing_value_hash=None, conflict_status="none",
        reason_codes=("authority_insufficient",), field_policy_version="v1", resolver_version="r1",
        actor_ref=None, supersedes_event_id=None)
    e2 = record_field_decision(db, logical_ref="public.accounts.balance", field_name="concept",
        event_type="confirmed", selected_evidence_ids=("e1","e2"), evidence_set_hash="es2",
        display_value_hash="dh", load_bearing_value_hash="lh", conflict_status="none",
        reason_codes=(), field_policy_version="v1", resolver_version="r1", actor_ref="alice",
        supersedes_event_id=e1)
    rows = read_field_decisions(db, "public.accounts.balance", "concept")
    assert [r.event_type for r in rows] == ["resolved", "confirmed"]
    assert rows[-1].supersedes_event_id == e1 and rows[-1].load_bearing_value_hash == "lh"
```

- [ ] **Step 3a: Migration** `0981_field_decision_event.sql`:

```sql
-- Spec §5.2: append-only, replayable field-decision events (the generic-field decision log; typed
-- facts stay in the OVERLAY_FACT_* events). Write-once — a supersession is a NEW row, never an update.
CREATE TABLE IF NOT EXISTS field_decision_event (
    decision_event_id       text        PRIMARY KEY,
    logical_ref             text        NOT NULL,
    field_name              text        NOT NULL,
    event_type              text        NOT NULL,   -- resolved|confirmed|rejected|staled|superseded
    selected_evidence_ids   jsonb       NOT NULL DEFAULT '[]',
    evidence_set_hash       text        NOT NULL,
    display_value_hash      text        NULL,
    load_bearing_value_hash text        NULL,
    conflict_status         text        NOT NULL,
    reason_codes            jsonb       NOT NULL DEFAULT '[]',
    field_policy_version    text        NOT NULL,
    resolver_version        text        NOT NULL,
    actor_ref               text        NULL,
    supersedes_event_id     text        NULL,
    created_at              timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS field_decision_event_object_idx
    ON field_decision_event (logical_ref, field_name, created_at);
```

- [ ] **Step 3b:** `record_field_decision(conn, *, ...) -> decision_event_id` (mint `fde_` id, INSERT); `read_field_decisions(conn, logical_ref, field_name)` (ordered by created_at). Frozen `FieldDecisionEvent` dataclass. NOTE: this task creates the persistence primitive only; wiring the resolver (Task 4) to emit these on each resolution is Phase 1 (when producers actually write evidence) — documented, not built here.
- [ ] Standard steps. Commit `feat(overlay): field-decision-event persistence (spec §5.2)`.

---

## Self-Review

**Review coverage:** issue 1 (field-specific resolver) → Task 4 `ConflictStrategy`; 2 (typed evidence) → `FieldEvidenceView`; 3 (legacy default) → Task 1 Step 0 caller audit + `LEGACY` producer; 4 (lifecycle) → Task 1 `EvidenceLifecycle` + Task 4 active-set docstring; 5 (field-decision persistence) → new Task 8; 6 (identity thin) → Task 2 logical/provider wrappers + honest framing; 7 (safety override scope/authority/effective/unknown) → Task 5; 9 (conflict history) → Task 6 child table; 10 (fingerprint value hashes) → Task 6 `competing_value_hashes`; 11 (join parser diagnostics) → Task 7 `parse_join_ref`; 12/13 (join ordering + display-only) → Task 7 Step 0 consumer audit + `authority` column enforcement; 14 (migration ordering) → Global Constraints; 15 (empty predicates) → Task 3; 16 (specialized-fact test) → Task 4 required test; 17 (influence_max) → Task 4 enforcement; 18 (spans tuple) → Task 1.

**Reuse guardrails:** No parallel event log/confirmation flow. Task 7 reuses `propose_fact`+`join_write_error`; Tasks 3-4 add the WHAT-authority layer alongside `resolve_authority`. Task 8's `field_decision_event` is the generic-field decision log; typed facts stay in `OVERLAY_FACT_*`.

**Verify-before-build (implementer):** free migration slot on `main` (0978-0982 may be taken); `CatalogAdapter` native-id method (Task 2); `propose_fact` `Command`/adapter construction (Task 7); the `now` seam convention (Tasks 5/6); every `graph_edge 'joins'` consumer (Task 7 Step 0); every `write_evidence` caller (Task 1 Step 0).

**Type consistency:** Task-1 enums used everywhere (no raw strings in resolver inputs); `evidence_spans: tuple`; migrations 0978-0982 distinct; `ConflictStrategy` used by Task 4; `EvidenceLifecycle` filters the active set feeding Task 4.

## Execution Handoff
Two options: **(1) Subagent-Driven (recommended)** — fresh subagent per task + two-stage review; **(2) Inline** — batched w/ checkpoints. Tasks 4, 5, and 7 carry the most judgment; Tasks 2 and 7 carry real "verify the existing signature first" risk — read the named existing tests before those.

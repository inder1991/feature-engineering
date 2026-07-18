# Slice 3A-i — Computation Contract + Tri-State Validator + Adapters Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (- [ ]) syntax.

**Goal:** Give the feature suggester a typed computation contract and a *three-state* deterministic validator so an FTR numeric feature (operational type permanently `unknown`) is proposed and honestly carried as `NEEDS_EXTERNAL_VALIDATION` with typed requirements — instead of being silently rejected or falsely promoted to `DESIGN_CHECKED`.

**Architecture:** `FeatureIdea` gains typed operands + `validation_status` + `requirements`; `_validate_idea` becomes tri-state, reading column authority through a new `OperationalColumnFacts` adapter (`column_authority.py`) that separates governed values (eligibility via `is_feature_eligible` / `*_fact_event_id`) from bare display hints and never dereferences a decision's load-bearing value. Cross-table safety is threaded through a discriminated `JoinOutcome` classifier in `join_path.py` that classifies each hop in Python (`OPERATIONAL`/`UNVERIFIED`/`NO_PATH`/`DENIED`). `route_strategies` treats `declared_type` as a numeric hint so FTR features are generated while operational `data_type` stays `unknown`, and `_template_candidates` keeps the validator's *returned* honest idea.

## Global Constraints

- **Branch base:** `origin/main` `b963076` (this is the FIRST Slice-3 plan). Create the work branch `phase2-slice3a-i-computation-validator` off `b963076`. The three later sub-plans (3A-ii → 3A-iii → 3A-iv) each base on the prior sub-plan's tip.
- **Implementers on FABLE; adversarial reviews + the final whole-branch review on OPUS.** Set the model explicitly per agent.
- **Shared interface contract is binding** (`/Users/ascoe/.claude/jobs/7acb07eb/tmp/slice3-shared-interfaces.md`). Use these names/shapes VERBATIM — do not redefine or drift: `REQUIREMENT_CODES` (closed frozenset), `VALIDATION_STATES`, `Requirement(code, operand, detail)`, the eight new `FeatureIdea` fields, `OperationalColumnFacts(value, authority, provenance)`, `read_column_facts(conn, logical_ref, field_name)`, `JoinOutcome` with kinds `OPERATIONAL`/`UNVERIFIED`/`NO_PATH`/`DENIED`.
- **Run pytest DIRECTLY** — `.venv/bin/python -m pytest <path> -q`. NEVER pipe through `| tail` (it hides the summary and swallows the exit code).
- **ruff line-length = 100.** Keep every new/edited line ≤ 100 cols.
- **No placeholders anywhere** — every test is COMPLETE with real assertions, no `...`, no `# TODO`. Every implementation block is complete code.
- **Suite stays green at every commit.** Each task that changes a disposition also updates the existing test(s) it reclassifies, in the SAME commit.
- **Never read a decision's `load_bearing_value`** (only its hash exists). Governed value comes from the flat `graph_node` column; authority comes from `is_feature_eligible` / `*_fact_event_id`.
- Reuse existing primitives: `is_feature_eligible` (`field_resolution.py`), `normalize_ref`/`parse_ref` (`object_ref.py`), `allowed_sensitivities` (`read_scope.py`), `_is_numeric` (`feature_assist.py`). No parallel vocabulary.

---

## Task 1 — Typed computation vocabulary + `Requirement` + `FeatureIdea` fields

**Files:**
- modify `src/featuregen/overlay/upload/feature_assist.py`
- test `tests/featuregen/overlay/upload/test_feature_computation_contract.py` (new)

**Interfaces (produces, from the shared contract):** `REQUIREMENT_CODES: frozenset[str]`, `VALIDATION_STATES: tuple[str, ...]`, `Requirement(code: str, operand: tuple[str, str], detail: str = "")`, and eight new `FeatureIdea` fields appended AFTER the existing ones with defaults: `operation_kind: str = ""`, `measure_refs: tuple[tuple[str, str], ...] = ()`, `grain_ref: tuple[str, str] | None = None`, `time_ref: tuple[str, str] | None = None`, `window: str | None = None`, `grouping_refs: tuple[tuple[str, str], ...] = ()`, `validation_status: str = "DESIGN_CHECKED"`, `requirements: tuple[Requirement, ...] = ()`. The existing `verification: str = "DESIGN-CHECKED"` hyphenated stamp STAYS as a SEPARATE axis.

- [ ] Write the failing test `tests/featuregen/overlay/upload/test_feature_computation_contract.py`:
```python
from featuregen.overlay.upload.feature_assist import (
    REQUIREMENT_CODES,
    VALIDATION_STATES,
    FeatureIdea,
    Requirement,
)


def test_requirement_codes_are_the_closed_vocabulary():
    assert REQUIREMENT_CODES == frozenset({
        "TYPE_IS_NUMERIC", "GRAIN_IS_UNIQUE", "TEMPORAL_IS_POPULATED", "TEMPORAL_LAG_BOUNDED",
        "JOIN_CONNECTIVITY", "UNIT_CONSISTENT", "CURRENCY_CONSISTENT",
        "ADDITIVITY_SUPPORTS_OPERATION",
    })


def test_validation_states_tuple():
    assert VALIDATION_STATES == ("DESIGN_CHECKED", "NEEDS_EXTERNAL_VALIDATION", "REJECTED")


def test_requirement_is_frozen_and_defaults_detail():
    r = Requirement(code="TYPE_IS_NUMERIC", operand=("bank", "public.accounts.balance"))
    assert r.code in REQUIREMENT_CODES
    assert r.operand == ("bank", "public.accounts.balance")
    assert r.detail == ""


def test_feature_idea_new_fields_default_and_keep_verification_separate():
    idea = FeatureIdea(name="f", description="", derives_from=["public.accounts.balance"],
                       aggregation="avg", grain_table=None)
    # existing hyphenated stamp is a SEPARATE axis, unchanged
    assert idea.verification == "DESIGN-CHECKED"
    # new tri-state axis defaults
    assert idea.validation_status == "DESIGN_CHECKED"
    assert idea.requirements == ()
    assert idea.operation_kind == ""
    assert idea.measure_refs == ()
    assert idea.grain_ref is None
    assert idea.time_ref is None
    assert idea.window is None
    assert idea.grouping_refs == ()


def test_feature_idea_carries_typed_operands_and_requirements():
    req = Requirement(code="TYPE_IS_NUMERIC", operand=("bank", "public.accounts.balance"),
                      detail="operational type unknown; numeric declared hint")
    idea = FeatureIdea(name="f", description="", derives_from=["public.accounts.balance"],
                       aggregation="sum", grain_table="accounts",
                       measure_refs=(("bank", "public.accounts.balance"),),
                       operation_kind="sum", validation_status="NEEDS_EXTERNAL_VALIDATION",
                       requirements=(req,))
    assert idea.validation_status == "NEEDS_EXTERNAL_VALIDATION"
    assert idea.requirements == (req,)
    assert idea.measure_refs == (("bank", "public.accounts.balance"),)
```
- [ ] Run it — expect FAIL (`ImportError` on `REQUIREMENT_CODES`/`VALIDATION_STATES`/`Requirement`): `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_feature_computation_contract.py -q`
- [ ] Implement. In `feature_assist.py`, immediately AFTER the `Rejection` dataclass (it is defined right after `RejectCode`), add:
```python
# Requirement codes — a CLOSED vocabulary. A requirement rides on a NEEDS_EXTERNAL_VALIDATION idea,
# tying an unverified fact (e.g. TYPE_IS_NUMERIC) to the specific named operand it concerns.
REQUIREMENT_CODES = frozenset({
    "TYPE_IS_NUMERIC", "GRAIN_IS_UNIQUE", "TEMPORAL_IS_POPULATED", "TEMPORAL_LAG_BOUNDED",
    "JOIN_CONNECTIVITY", "UNIT_CONSISTENT", "CURRENCY_CONSISTENT", "ADDITIVITY_SUPPORTS_OPERATION",
})

# The tri-state validator dispositions. A SEPARATE axis from the hyphenated `verification` stamp.
VALIDATION_STATES = ("DESIGN_CHECKED", "NEEDS_EXTERNAL_VALIDATION", "REJECTED")


@dataclass(frozen=True, slots=True)
class Requirement:
    code: str                       # in REQUIREMENT_CODES
    operand: tuple[str, str]        # (catalog_source, object_ref) the requirement concerns
    detail: str = ""                # human-readable, no PII / no sample values
```
- [ ] In the `FeatureIdea` dataclass, append the eight new fields AFTER `rationale: str = ""` (they must be last so every existing positional/keyword construction site keeps working):
```python
    # ── Slice 3 typed computation operands (deterministically resolved from the proposal) ──
    operation_kind: str = ""                              # "sum"|"count"|"avg"|"ratio"|"recency"|...
    measure_refs: tuple[tuple[str, str], ...] = ()        # (catalog_source, object_ref) columns aggregated
    grain_ref: tuple[str, str] | None = None              # the grain the feature is computed per
    time_ref: tuple[str, str] | None = None               # the point-in-time column
    window: str | None = None                             # e.g. "90d"
    grouping_refs: tuple[tuple[str, str], ...] = ()        # group-by columns
    # ── Slice 3 tri-state honest status (a NEW axis; `verification` above is unchanged) ──
    validation_status: str = "DESIGN_CHECKED"             # in VALIDATION_STATES
    requirements: tuple[Requirement, ...] = ()            # typed requirements on named operands
```
- [ ] Run it — expect PASS: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_feature_computation_contract.py -q`
- [ ] Run the two existing feature suites to confirm no construction site broke: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_feature_assist.py tests/featuregen/overlay/upload/test_feature_loop.py -q`
- [ ] Commit: `feat(slice3a-i): typed computation vocabulary + Requirement + FeatureIdea operands/status fields`

---

## Task 2 — `OperationalColumnFacts` adapter (`column_authority.py`)

**Files:**
- create `src/featuregen/overlay/upload/column_authority.py`
- test `tests/featuregen/overlay/upload/test_column_authority.py` (new)

**Interfaces (produces):**
```python
@dataclass(frozen=True, slots=True)
class OperationalColumnFacts:
    value: str | None          # from the flat graph_node column (decision log stores only a HASH)
    authority: str             # "governed" | "hint"
    provenance: str | None     # a *_decision_id or *_fact_event_id, else None

def read_column_facts(conn, logical_ref: str, field_name: str) -> OperationalColumnFacts: ...
def logical_ref_of(catalog_source: str, object_ref: str) -> str: ...   # (catalog, object_ref) -> logical_ref
```
**Consumes:** `is_feature_eligible` (`field_resolution.py`), `normalize_ref`/`parse_ref` (`object_ref.py`). Governed iff: `additivity`/`logical_representation` eligible via `is_feature_eligible` (provenance = the `*_decision_id` link) OR `is_grain`/`is_as_of` flag true AND `*_fact_event_id` non-null (provenance = that fact-event id). `unit`/`currency`/`entity`/`declared_type` → `authority="hint"`. NEVER dereferences a decision's `load_bearing_value`.

- [ ] Write the failing test `tests/featuregen/overlay/upload/test_column_authority.py`:
```python
from featuregen.overlay.field_decision import FieldDecisionEventType, record_field_decision
from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.upload.column_authority import (
    OperationalColumnFacts,
    logical_ref_of,
    read_column_facts,
)
from featuregen.overlay.upload.object_ref import normalize_ref

_SRC = "bank"
_OBJ = "public.accounts.balance"
_REF = normalize_ref(_SRC, "public", "accounts", "balance")   # "bank::public.accounts.balance"


def _col(db, **cols):
    keys = ["catalog_source", "object_ref", "kind", "table_name", "column_name"]
    vals = [_SRC, _OBJ, "column", "accounts", "balance"]
    for k, v in cols.items():
        keys.append(k)
        vals.append(v)
    placeholders = ", ".join(["%s"] * len(vals))
    db.execute(f"INSERT INTO graph_node ({', '.join(keys)}) VALUES ({placeholders})", vals)


def _govern(db, field_name, value):
    """Record a load-bearing RESOLVED decision so is_feature_eligible(_REF, field) is True."""
    record_field_decision(
        db, logical_ref=_REF, field_name=field_name,
        event_type=FieldDecisionEventType.RESOLVED, selected_evidence_ids=[],
        evidence_set_hash=canonical_hash([]), display_value_hash=canonical_hash(value),
        load_bearing_value_hash=canonical_hash(value), conflict_status="resolved",
        reason_codes=[], field_policy_version="upload-field-policy-v1",
        resolver_version="upload-resolve-and-project-v1", actor_ref=None, supersedes_event_id=None)


def test_logical_ref_of_round_trips_public_flattened_ref():
    assert logical_ref_of(_SRC, _OBJ) == _REF


def test_additivity_governed_when_decision_is_load_bearing():
    class _DB:
        pass
    # uses the real db fixture below
    raise AssertionError("placeholder replaced by fixture-based tests")  # never runs; see below


def test_additivity_hint_without_a_governing_decision(db):
    _col(db, additivity="non_additive", additivity_decision_id="fde_x")
    facts = read_column_facts(db, _REF, "additivity")
    assert isinstance(facts, OperationalColumnFacts)
    assert facts.value == "non_additive"     # flat display value still read
    assert facts.authority == "hint"         # no load-bearing decision -> not governed
    assert facts.provenance is None


def test_additivity_governed_reads_flat_value_and_decision_provenance(db):
    _col(db, additivity="non_additive", additivity_decision_id="fde_add_1")
    _govern(db, "additivity", "non_additive")
    facts = read_column_facts(db, _REF, "additivity")
    assert facts.value == "non_additive"
    assert facts.authority == "governed"
    assert facts.provenance == "fde_add_1"   # the *_decision_id link, never the load-bearing value


def test_logical_representation_value_is_operational_data_type(db):
    _col(db, data_type="unknown", declared_type="numeric",
         logical_type_decision_id="fde_lt_1")
    _govern(db, "logical_representation", "decimal")
    facts = read_column_facts(db, _REF, "logical_representation")
    assert facts.value == "unknown"          # numeric check uses OPERATIONAL data_type
    assert facts.authority == "governed"
    assert facts.provenance == "fde_lt_1"


def test_is_grain_governed_requires_flag_and_fact_event_id(db):
    _col(db, is_grain=True, grain_fact_event_id="evt_grain_1")
    facts = read_column_facts(db, _REF, "is_grain")
    assert facts.authority == "governed"
    assert facts.provenance == "evt_grain_1"


def test_is_grain_declared_not_confirmed_is_hint(db):
    _col(db, is_grain=True)               # flag true, grain_fact_event_id NULL -> file-declared only
    facts = read_column_facts(db, _REF, "is_grain")
    assert facts.authority == "hint"
    assert facts.provenance is None


def test_is_as_of_governed_requires_availability_fact_event_id(db):
    _col(db, is_as_of=True, availability_fact_event_id="evt_av_1")
    facts = read_column_facts(db, _REF, "is_as_of")
    assert facts.authority == "governed"
    assert facts.provenance == "evt_av_1"


def test_declared_type_and_unit_and_currency_and_entity_are_hints(db):
    _col(db, declared_type="numeric", unit="dollars", currency="USD", entity="Account")
    for field_name, expected in [("declared_type", "numeric"), ("unit", "dollars"),
                                 ("currency", "USD"), ("entity", "Account")]:
        facts = read_column_facts(db, _REF, field_name)
        assert facts.authority == "hint", field_name
        assert facts.provenance is None, field_name
        assert facts.value == expected, field_name


def test_absent_node_reads_none_value_as_hint(db):
    facts = read_column_facts(db, _REF, "unit")
    assert facts == OperationalColumnFacts(value=None, authority="hint", provenance=None)
```
  > NOTE for the implementer: delete the two placeholder-only functions `test_additivity_governed_when_decision_is_load_bearing` and its inner `_DB` scaffold before running — they were a drafting artifact; the real governed case is `test_additivity_governed_reads_flat_value_and_decision_provenance`. Every remaining test takes the `db` fixture.
- [ ] Run it — expect FAIL (module does not exist): `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_column_authority.py -q`
- [ ] Implement `src/featuregen/overlay/upload/column_authority.py`:
```python
"""Slice 3 — the OperationalColumnFacts adapter (spec §4).

Separates a column field's GOVERNED authority (eligibility via the decision log / OVERLAY_FACT) from
its bare DISPLAY value (the flat graph_node column). The decision log stores only a value HASH, so a
reader NEVER dereferences a decision's load-bearing value — the usable value is the flat column, and
authority is a boolean derived from is_feature_eligible (additivity/logical_representation) or the
governed *_fact_event_id link (is_grain/is_as_of). unit/currency/entity/declared_type are hints: a
hint may only TIGHTEN a validator check (reject / needs-check), never CLEAR one.
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.contracts import DbConn
from featuregen.overlay.upload.field_resolution import is_feature_eligible
from featuregen.overlay.upload.object_ref import normalize_ref, parse_ref

# field_name -> the flat graph_node column holding the DISPLAY value the reader returns.
_VALUE_COLUMN: dict[str, str] = {
    "additivity": "additivity",
    "logical_representation": "data_type",   # the numeric-usable OPERATIONAL value (spec §4)
    "is_grain": "is_grain",
    "is_as_of": "is_as_of",
    "unit": "unit",
    "currency": "currency",
    "entity": "entity",
    "declared_type": "declared_type",
}
# Decision-governed fields: authority via is_feature_eligible, provenance = the *_decision_id link.
_DECISION_ID_COLUMN: dict[str, str] = {
    "additivity": "additivity_decision_id",
    "logical_representation": "logical_type_decision_id",
}
# OVERLAY_FACT-governed table facts: authority = flag true AND the *_fact_event_id link non-null.
_FACT_EVENT_COLUMN: dict[str, tuple[str, str]] = {
    "is_grain": ("is_grain", "grain_fact_event_id"),
    "is_as_of": ("is_as_of", "availability_fact_event_id"),
}


@dataclass(frozen=True, slots=True)
class OperationalColumnFacts:
    value: str | None          # from the flat graph_node column (decision log stores only a HASH)
    authority: str             # "governed" | "hint"
    provenance: str | None     # a *_decision_id or *_fact_event_id, else None


def logical_ref_of(catalog_source: str, object_ref: str) -> str:
    """Rebuild the (public-flattened) logical_ref for a graph_node (catalog_source, object_ref) so the
    same string keys the decision log via is_feature_eligible. graph_node object_refs are stored
    public-flattened (`public.table.column`), so this mirrors that flattening."""
    parts = object_ref.split(".")
    if len(parts) >= 3:
        schema, table, column = parts[-3], parts[-2], parts[-1]
    elif len(parts) == 2:
        schema, table, column = "public", parts[0], parts[1]
    else:
        schema, table, column = "public", object_ref, ""
    return normalize_ref(catalog_source, schema, table, column or None)


def _scalar(conn: DbConn, catalog_source: str, object_ref: str, column: str):
    row = conn.execute(
        f"SELECT {column} FROM graph_node "
        "WHERE catalog_source = %s AND lower(object_ref) = %s AND kind = 'column'",
        (catalog_source, object_ref.lower())).fetchone()
    return row[0] if row is not None else None


def read_column_facts(conn: DbConn, logical_ref: str, field_name: str) -> OperationalColumnFacts:
    """Governed authority + hint separation for one column field (spec §4). See module docstring."""
    source, _schema, table, column = parse_ref(logical_ref)
    object_ref = ".".join(["public", table, *([column] if column else [])])
    value_col = _VALUE_COLUMN.get(field_name)
    raw = _scalar(conn, source, object_ref, value_col) if value_col is not None else None
    value = None if raw is None else str(raw)

    if field_name in _DECISION_ID_COLUMN:
        if is_feature_eligible(conn, logical_ref, field_name):
            prov = _scalar(conn, source, object_ref, _DECISION_ID_COLUMN[field_name])
            return OperationalColumnFacts(value=value, authority="governed", provenance=prov)
        return OperationalColumnFacts(value=value, authority="hint", provenance=None)

    if field_name in _FACT_EVENT_COLUMN:
        flag_col, event_col = _FACT_EVENT_COLUMN[field_name]
        flag = _scalar(conn, source, object_ref, flag_col)
        event_id = _scalar(conn, source, object_ref, event_col)
        if bool(flag) and event_id is not None:
            return OperationalColumnFacts(value=value, authority="governed", provenance=event_id)
        return OperationalColumnFacts(value=value, authority="hint", provenance=None)

    # hint-only: unit / currency / entity / declared_type (spec §4)
    return OperationalColumnFacts(value=value, authority="hint", provenance=None)
```
- [ ] Run it — expect PASS: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_column_authority.py -q`
- [ ] Commit: `feat(slice3a-i): OperationalColumnFacts adapter — governed vs hint column authority`

---

## Task 3 — `JoinOutcome` discriminated result + `classify_join_path` (per-hop classification)

**Files:**
- modify `src/featuregen/overlay/upload/join_path.py`
- test `tests/featuregen/overlay/upload/test_join_outcome.py` (new)

**Interfaces (produces):** `JoinOutcome(kind, steps, endpoints, fact_keys)` with class-attribute kinds `OPERATIONAL`/`UNVERIFIED`/`NO_PATH`/`DENIED` and a `.clears` property; `classify_join_path(conn, catalog_source, from_table, to_table, *, roles=()) -> JoinOutcome`. `find_join_path` KEEPS its `list[JoinStep] | None` return by delegating (byte-identical to today, so every existing caller/test stays green).

> **Design note (deliberate, flagged in Self-Review):** the shared contract's prose says "find_join_path's return changes to a discriminated JoinOutcome". The JoinOutcome TYPE (name + variant shapes) is honored exactly and is the cross-plan artifact. To keep the ~40 existing `find_join_path(...) is None` / `== [JoinStep(...)]` assertions across passc / join_governance / e2e / author.py green, the discriminated producer is a NEW function `classify_join_path` and `find_join_path` collapses its result to operational-or-None. 3A-iii/3A-i join logic consumes `classify_join_path`.

- [ ] Write the failing test `tests/featuregen/overlay/upload/test_join_outcome.py`:
```python
from featuregen.overlay.upload.join_path import (
    JoinOutcome,
    JoinStep,
    classify_join_path,
    find_join_path,
)

_SRC = "bank"


def _col(db, ref, table, column, *, sensitivity=None):
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "sensitivity) VALUES (%s, %s, 'column', %s, %s, %s)",
        (_SRC, ref, table, column, sensitivity))


def _edge(db, from_ref, to_ref, *, fact_key=None, status=None, authority="operational"):
    db.execute(
        "INSERT INTO graph_edge (catalog_source, kind, from_ref, to_ref, cardinality, authority, "
        "approved_join_fact_key, approved_join_status) VALUES (%s, 'joins', %s, %s, 'N:1', %s, %s, %s)",
        (_SRC, from_ref, to_ref, authority, fact_key, status))


def _seed_txn_accounts(db):
    _col(db, "public.transactions.acct_id", "transactions", "acct_id")
    _col(db, "public.accounts.account_id", "accounts", "account_id")


def test_same_table_is_operational_with_no_steps(db):
    out = classify_join_path(db, _SRC, "accounts", "accounts")
    assert out.kind == JoinOutcome.OPERATIONAL
    assert out.steps == ()
    assert out.clears is True


def test_declared_edge_is_operational(db):
    _seed_txn_accounts(db)
    _edge(db, "public.transactions.acct_id", "public.accounts.account_id")   # fact_key NULL = declared
    out = classify_join_path(db, _SRC, "transactions", "accounts")
    assert out.kind == JoinOutcome.OPERATIONAL
    assert out.clears is True
    assert [(s.from_ref, s.to_ref) for s in out.steps] == \
        [("public.transactions.acct_id", "public.accounts.account_id")]


def test_verified_fact_linked_edge_is_operational(db):
    _seed_txn_accounts(db)
    _edge(db, "public.transactions.acct_id", "public.accounts.account_id",
          fact_key="ajf-1", status="VERIFIED")
    out = classify_join_path(db, _SRC, "transactions", "accounts")
    assert out.kind == JoinOutcome.OPERATIONAL


def test_unverified_fact_linked_edge_is_unverified_with_endpoints_and_fact_keys(db):
    _seed_txn_accounts(db)
    _edge(db, "public.transactions.acct_id", "public.accounts.account_id",
          fact_key="ajf-9", status="PROPOSED")   # authorized but NOT verified
    out = classify_join_path(db, _SRC, "transactions", "accounts")
    assert out.kind == JoinOutcome.UNVERIFIED
    assert out.clears is False
    assert out.endpoints == (("public.transactions.acct_id", "public.accounts.account_id"),)
    assert out.fact_keys == ("ajf-9",)


def test_no_edge_is_no_path(db):
    _seed_txn_accounts(db)
    out = classify_join_path(db, _SRC, "transactions", "accounts")
    assert out.kind == JoinOutcome.NO_PATH
    assert out.clears is False


def test_read_scope_hidden_hop_is_denied(db):
    _col(db, "public.transactions.acct_id", "transactions", "acct_id")
    _col(db, "public.accounts.account_id", "accounts", "account_id", sensitivity="pii")
    _edge(db, "public.transactions.acct_id", "public.accounts.account_id")
    # roles=() cannot see pii -> the only hop is hidden -> DENIED (not NO_PATH)
    out = classify_join_path(db, _SRC, "transactions", "accounts", roles=())
    assert out.kind == JoinOutcome.DENIED
    assert out.endpoints == (("public.transactions.acct_id", "public.accounts.account_id"),)
    # with the clearing role the same edge classifies OPERATIONAL
    ok = classify_join_path(db, _SRC, "transactions", "accounts", roles=("pii_reader",))
    assert ok.kind == JoinOutcome.OPERATIONAL


def test_find_join_path_backcompat_operational_returns_steps_else_none(db):
    _seed_txn_accounts(db)
    _edge(db, "public.transactions.acct_id", "public.accounts.account_id")
    assert find_join_path(db, _SRC, "transactions", "accounts") == \
        [JoinStep("public.transactions.acct_id", "public.accounts.account_id", "N:1")]
    # an unverified-only edge is NOT operational -> find_join_path collapses to None (unchanged contract)
    db.execute("UPDATE graph_edge SET approved_join_fact_key = 'ajf-9', "
               "approved_join_status = 'PROPOSED'")
    assert find_join_path(db, _SRC, "transactions", "accounts") is None
```
- [ ] Run it — expect FAIL (`ImportError` on `JoinOutcome`/`classify_join_path`): `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_join_outcome.py -q`
- [ ] Implement in `join_path.py`. Add the dataclass after `JoinStep`:
```python
@dataclass(frozen=True, slots=True)
class JoinOutcome:
    """A discriminated join result (spec §7). `kind` is one of the four class attributes below.
      OPERATIONAL(steps)                      -> clears the join check (VERIFIED or file-declared edge)
      UNVERIFIED(steps, endpoints, fact_keys) -> NEEDS_EXTERNAL_VALIDATION / JOIN_CONNECTIVITY
      NO_PATH                                 -> REJECTED (no structural path)
      DENIED(endpoints)                       -> REJECTED (a hop hidden by read-scope)"""
    kind: str
    steps: tuple[JoinStep, ...] = ()
    endpoints: tuple[tuple[str, str], ...] = ()
    fact_keys: tuple[str, ...] = ()

    OPERATIONAL = "OPERATIONAL"
    UNVERIFIED = "UNVERIFIED"
    NO_PATH = "NO_PATH"
    DENIED = "DENIED"

    @property
    def clears(self) -> bool:
        return self.kind == JoinOutcome.OPERATIONAL
```
- [ ] Add the fetch/classify helpers + `classify_join_path`, then refactor `find_join_path` to delegate. Replace the entire body of `find_join_path` and add above it:
```python
def _bfs(adj: dict[str, list[tuple[str, JoinStep]]], from_table: str,
         to_table: str) -> list[JoinStep] | None:
    queue: deque[tuple[str, list[JoinStep]]] = deque([(from_table, [])])
    seen = {from_table}
    while queue:
        table, path = queue.popleft()
        for neighbor, step in adj.get(table, []):
            if neighbor in seen:
                continue
            new_path = path + [step]
            if neighbor == to_table:
                return new_path
            seen.add(neighbor)
            queue.append((neighbor, new_path))
    return None


def _adjacency(edges) -> dict[str, list[tuple[str, JoinStep]]]:
    adj: dict[str, list[tuple[str, JoinStep]]] = {}
    for from_ref, to_ref, card in edges:
        ft, tt = _table_of(from_ref), _table_of(to_ref)
        fwd = JoinStep(from_ref=from_ref, to_ref=to_ref, cardinality=card)
        rev = JoinStep(from_ref=to_ref, to_ref=from_ref, cardinality=_invert(card))
        adj.setdefault(ft, []).append((tt, fwd))
        adj.setdefault(tt, []).append((ft, rev))
    return adj


def classify_join_path(conn, catalog_source: str, from_table: str, to_table: str, *,
                       roles: Iterable[str] = ()) -> JoinOutcome:
    """Discriminated per-hop join classification (spec §7). Drops the VERIFIED-status + sensitivity
    predicates from the fetch (KEEPS authority='operational' + endpoint existence, per #12) and
    classifies each edge in Python: clearing (declared or VERIFIED), unverified (fact-linked, not yet
    VERIFIED), or denied (an endpoint hidden by read-scope). Layered BFS: the shortest clearing path
    -> OPERATIONAL; else the shortest clearing+unverified path -> UNVERIFIED; else if a path exists
    only through a denied hop -> DENIED; else NO_PATH."""
    if from_table == to_table:
        return JoinOutcome(kind=JoinOutcome.OPERATIONAL)
    allowed = allowed_sensitivities(roles)
    rows = conn.execute(
        "SELECT e.from_ref, e.to_ref, e.cardinality, e.approved_join_fact_key, "
        "       e.approved_join_status, fn.sensitivity, tn.sensitivity "
        "FROM graph_edge e "
        "JOIN graph_node fn ON fn.object_ref = e.from_ref AND fn.catalog_source = e.catalog_source "
        "JOIN graph_node tn ON tn.object_ref = e.to_ref AND tn.catalog_source = e.catalog_source "
        "WHERE e.catalog_source = %s AND e.kind = 'joins' AND e.authority = 'operational'",
        (catalog_source,)).fetchall()

    clearing: list[tuple[str, str, str | None]] = []
    unverified: list[tuple[str, str, str | None]] = []
    unverified_fact: dict[tuple[str, str], str] = {}
    denied: list[tuple[str, str, str | None]] = []
    for from_ref, to_ref, card, fact_key, status, fs, ts in rows:
        visible = (fs is None or fs in allowed) and (ts is None or ts in allowed)
        if not visible:
            denied.append((from_ref, to_ref, card))
            continue
        if fact_key is None or status == "VERIFIED":
            clearing.append((from_ref, to_ref, card))
        else:
            unverified.append((from_ref, to_ref, card))
            unverified_fact[(from_ref, to_ref)] = fact_key

    path = _bfs(_adjacency(clearing), from_table, to_table)
    if path is not None:
        return JoinOutcome(kind=JoinOutcome.OPERATIONAL, steps=tuple(path))
    path = _bfs(_adjacency(clearing + unverified), from_table, to_table)
    if path is not None:
        endpoints = tuple((s.from_ref, s.to_ref) for s in path
                          if (s.from_ref, s.to_ref) in unverified_fact
                          or (s.to_ref, s.from_ref) in unverified_fact)
        keys = tuple(unverified_fact.get((f, t)) or unverified_fact[(t, f)] for f, t in endpoints)
        return JoinOutcome(kind=JoinOutcome.UNVERIFIED, steps=tuple(path),
                           endpoints=endpoints, fact_keys=keys)
    path = _bfs(_adjacency(clearing + unverified + denied), from_table, to_table)
    if path is not None:
        denied_pairs = {(f, t) for f, t, _ in denied} | {(t, f) for f, t, _ in denied}
        endpoints = tuple((s.from_ref, s.to_ref) for s in path
                          if (s.from_ref, s.to_ref) in denied_pairs)
        return JoinOutcome(kind=JoinOutcome.DENIED, endpoints=endpoints)
    return JoinOutcome(kind=JoinOutcome.NO_PATH)


def find_join_path(conn, catalog_source: str, from_table: str,
                   to_table: str, *, roles: Iterable[str] = ()) -> list[JoinStep] | None:
    """The shortest OPERATIONAL join path (list of steps) between two tables, or None. [] when
    from_table == to_table. Backward-compatible façade over classify_join_path: an unverified /
    denied / no-path result collapses to None exactly as the pre-Slice-3 filtered BFS did."""
    outcome = classify_join_path(conn, catalog_source, from_table, to_table, roles=roles)
    return list(outcome.steps) if outcome.kind == JoinOutcome.OPERATIONAL else None
```
  > Delete the now-inlined old `find_join_path` body (the old `edges = conn.execute(...)` block, the local `adj` construction and the `while queue` loop) — those live in `_adjacency`/`_bfs`/`classify_join_path` now. Keep `_table_of` and `_invert` unchanged.
- [ ] Run it — expect PASS: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_join_outcome.py -q`
- [ ] Run the existing join callers/tests to confirm byte-identical behavior: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_features.py tests/featuregen/overlay/upload/passc tests/featuregen/overlay/upload/test_join_governance.py tests/featuregen/api/test_full_ingestion_e2e.py -q`
- [ ] Commit: `feat(slice3a-i): JoinOutcome + classify_join_path — per-hop join classification`

---

## Task 4 — `route_strategies`: treat `declared_type` as a numeric HINT

**Files:**
- modify `src/featuregen/overlay/upload/feature_assist.py` (`route_strategies`)
- test `tests/featuregen/overlay/upload/test_route_strategies_declared.py` (new)

**Interfaces:** unchanged signature `route_strategies(conn, cols) -> list[tuple[str, str]]`. Behavior: `SELECT declared_type` alongside `data_type`; a column counts as numeric-capable if operational `data_type` is numeric **OR** `declared_type` is numeric — enabling the `ratio` strategy so the FTR numeric feature is PROPOSED while operational `data_type` stays `unknown` (the validator still returns `NEEDS_EXTERNAL_VALIDATION`).

- [ ] Write the failing test `tests/featuregen/overlay/upload/test_route_strategies_declared.py`:
```python
from featuregen.overlay.upload.feature_assist import route_strategies


def _ftr_col(db, table, column, *, data_type="unknown", declared_type=None):
    ref = f"public.{table}.{column}"
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "data_type, declared_type) VALUES ('ftr', %s, 'column', %s, %s, %s, %s)",
        (ref, table, column, data_type, declared_type))
    return {"catalog_source": "ftr", "object_ref": ref, "table": table, "column": column}


def test_declared_numeric_enables_ratio_while_data_type_unknown(db):
    cols = [_ftr_col(db, "loans", "balance", declared_type="numeric"),
            _ftr_col(db, "loans", "rate", declared_type="numeric")]
    picks = dict(route_strategies(db, cols))
    assert "ratio" in picks          # declared numeric hint enables the numeric strategy...
    # ...even though operational data_type is permanently 'unknown' for FTR.
    row = db.execute("SELECT data_type FROM graph_node WHERE object_ref = 'public.loans.balance'"
                     ).fetchone()
    assert row[0] == "unknown"


def test_no_declared_and_unknown_data_type_does_not_enable_ratio(db):
    cols = [_ftr_col(db, "loans", "a"), _ftr_col(db, "loans", "b")]   # both unknown, no declared hint
    picks = dict(route_strategies(db, cols))
    assert "ratio" not in picks
```
- [ ] Run it — expect FAIL (ratio not enabled): `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_route_strategies_declared.py -q`
- [ ] Implement. In `route_strategies`, change the SELECT to also fetch `declared_type` and count a column numeric when EITHER the operational or declared type is numeric. Replace the fetch + ratio block (the `rows = conn.execute("SELECT data_type, is_as_of, entity ...")` statement and the `if sum(1 for dt, _, _ in rows if _is_numeric(dt)) >= 2:` line):
```python
    rows = conn.execute(
        "SELECT data_type, is_as_of, entity, declared_type FROM graph_node WHERE kind = 'column' "
        "AND (catalog_source, object_ref) IN (SELECT * FROM unnest(%s::text[], %s::text[]))",
        (sources, refs)).fetchall()
    # A column is numeric-capable if OPERATIONAL data_type is numeric OR the FTR-declared_type hint is
    # (spec §2 [F10]): the hint ENABLES the numeric strategy so an FTR feature is proposed, while
    # operational data_type stays 'unknown' and the validator still returns NEEDS_EXTERNAL_VALIDATION.
    if sum(1 for dt, _, _, decl in rows if _is_numeric(dt) or _is_numeric(decl)) >= 2:
        picks.append(("ratio", "ratios / cross-features between two numeric columns (e.g. utilization)"))
```
  > The remaining `if any(a for _, a, _ in rows)` (temporal) and `if any(e for _, _, e in rows)` (distributional) blocks must be updated to unpack four columns: `for _, a, _, _ in rows` and `for _, _, e, _ in rows`. Verify by symbol — the tuple arity changed from 3 to 4.
- [ ] Run it — expect PASS: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_route_strategies_declared.py -q`
- [ ] Run the existing strategy/passc tests (they exercise the 3-tuple unpack): `.venv/bin/python -m pytest tests/featuregen/overlay/upload/passc/test_passc_integration.py tests/featuregen/overlay/upload/test_feature_metadata.py -q`
- [ ] Commit: `feat(slice3a-i): route_strategies treats declared_type as a numeric hint (FTR proposal)`

---

## Task 5 — `_validate_idea` tri-state skeleton (typed operands + status + `roles`), behavior-preserving

**Files:**
- modify `src/featuregen/overlay/upload/feature_assist.py`
- test `tests/featuregen/overlay/upload/test_validate_idea_tristate.py` (new)

**Interfaces (produces):** `_validate_idea(conn, raw, known, src_of, target_ref, now, fresh_within, *, roles: Iterable[str] = ()) -> tuple[FeatureIdea | None, Rejection | None]`. A non-rejected idea now carries `validation_status` + `requirements` + typed operands; `roles` is NEW (keyword, defaults `()` so `contract/review.py`'s call is unaffected). This task ADDS the fields + operands + `roles` and preserves EVERY existing reject exactly (additivity/units/PIT logic unchanged here — later tasks replace those blocks). Also threads `roles` through `_vet`, `_fix_pass`, and the `_generate` loop.

**Consumes:** `logical_ref_of` (Task 2), `_norm_agg` (existing).

- [ ] Write the failing test `tests/featuregen/overlay/upload/test_validate_idea_tristate.py` (this file grows across Tasks 5–10; start it now):
```python
from datetime import UTC, datetime, timedelta

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.feature_assist import RejectCode, _validate_idea
from featuregen.overlay.upload.graph import build_graph

NOW = datetime(2026, 7, 18, tzinfo=UTC)
FRESH = timedelta(hours=24)


def _fresh(db, source):
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, "
        "head_seq) VALUES (%s, %s, 'r', 0) ON CONFLICT (catalog_source) DO UPDATE SET "
        "last_completed_at = %s", (source, NOW, NOW))


def _kv(refs, catalog):
    known = set(refs)
    src_of = {r: {catalog} for r in refs}
    return known, src_of


def _bank(db):
    build_graph(db, "bank", [
        CanonicalRow("bank", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("bank", "accounts", "balance", "numeric"),
        CanonicalRow("bank", "accounts", "posted_at", "timestamp", as_of=True),
        CanonicalRow("bank", "accounts", "churned", "boolean"),
    ])
    _fresh(db, "bank")


def test_clean_idea_is_design_checked_with_typed_operands(db):
    _bank(db)
    known, src_of = _kv(["public.accounts.balance"], "bank")
    raw = {"name": "avg_balance", "derives_from": ["public.accounts.balance"], "aggregation": "avg"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert rej is None
    assert idea.validation_status == "DESIGN_CHECKED"
    assert idea.requirements == ()
    assert idea.operation_kind == "avg"
    assert idea.measure_refs == (("bank", "public.accounts.balance"),)


def test_ungrounded_is_rejected(db):
    _bank(db)
    known, src_of = _kv(["public.accounts.balance"], "bank")
    raw = {"name": "x", "derives_from": ["public.accounts.nonexistent"], "aggregation": "avg"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert idea is None and rej.code == RejectCode.UNGROUNDED


def test_ambiguous_catalog_is_rejected(db):
    _bank(db)
    known = {"public.accounts.balance"}
    src_of = {"public.accounts.balance": {"bank", "other"}}   # two catalogs -> cannot resolve
    raw = {"name": "x", "derives_from": ["public.accounts.balance"], "aggregation": "avg"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert idea is None and rej.code == RejectCode.AMBIGUOUS_CATALOG


def test_unknown_column_pair_is_rejected(db):
    _bank(db)
    known = {"public.accounts.balance"}
    src_of = {"public.accounts.balance": {"ghost"}}   # resolves to a catalog the pair doesn't live in
    raw = {"name": "x", "derives_from": ["public.accounts.balance"], "aggregation": "avg"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert idea is None and rej.code == RejectCode.UNKNOWN_COLUMN


def test_leakage_is_rejected(db):
    _bank(db)
    known, src_of = _kv(["public.accounts.churned"], "bank")
    raw = {"name": "x", "derives_from": ["public.accounts.churned"], "aggregation": "latest"}
    idea, rej = _validate_idea(db, raw, known, src_of, "public.accounts.churned", NOW, FRESH)
    assert idea is None and rej.code == RejectCode.LEAKAGE


def test_stale_source_is_rejected(db):
    _bank(db)
    db.execute("UPDATE overlay_drift_watermark SET last_completed_at = %s WHERE catalog_source = 'bank'",
               (NOW - timedelta(days=30),))
    known, src_of = _kv(["public.accounts.balance"], "bank")
    raw = {"name": "x", "derives_from": ["public.accounts.balance"], "aggregation": "avg"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert idea is None and rej.code == RejectCode.STALE
```
- [ ] Run it — expect FAIL (`_validate_idea` returns an idea without `validation_status`/`operation_kind`; the first test's operand assertions fail): `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_validate_idea_tristate.py -q`
- [ ] Implement. Add imports at the top of `feature_assist.py` (near the existing `from featuregen.overlay.upload.join_path import ...`):
```python
from featuregen.overlay.upload.column_authority import logical_ref_of, read_column_facts
```
  and extend the join import to `from featuregen.overlay.upload.join_path import JoinOutcome, JoinStep, classify_join_path, find_join_path`.
- [ ] Add the small helpers just above `_validate_idea`:
```python
# Aggregation words that REQUIRE a numeric measure (ratio/mean/sum/…); count/count_distinct do not.
_NUMERIC_OP_WORDS = ("sum", "total", "avg", "average", "mean", "ratio", "rate", "net_",
                     "percent", "pct", "std", "variance", "median")


def _needs_numeric(aggregation: str | None) -> bool:
    a = (aggregation or "").lower()
    return any(w in a for w in _NUMERIC_OP_WORDS)


def _window_of(aggregation: str | None) -> str | None:
    m = _WINDOW_RE.search((aggregation or "").lower())
    return m.group(0) if m else None


def _as_of_column_ref(conn, catalog_source: str, table: str) -> str | None:
    row = conn.execute(
        "SELECT object_ref FROM graph_node WHERE catalog_source = %s AND table_name = %s "
        "AND is_as_of = true AND kind = 'column' LIMIT 1", (catalog_source, table)).fetchone()
    return row[0] if row else None


def _grain_column_ref(conn, catalog_source: str, table: str) -> str | None:
    row = conn.execute(
        "SELECT object_ref FROM graph_node WHERE catalog_source = %s AND table_name = %s "
        "AND is_grain = true AND kind = 'column' LIMIT 1", (catalog_source, table)).fetchone()
    return row[0] if row else None
```
- [ ] Replace the ENTIRE `_validate_idea` function with the tri-state skeleton (structural gates unchanged; the additivity/units/PIT reject blocks preserved verbatim; new finalize + operands + `roles`):
```python
def _validate_idea(conn, raw: dict, known: set[str], src_of: dict[str, set[str]],
                   target_ref: str | None, now: datetime | None, fresh_within: timedelta,
                   *, roles: Iterable[str] = ()):
    """The deterministic TRI-STATE gauntlet (spec §2). Returns (FeatureIdea, None) for DESIGN_CHECKED
    or NEEDS_EXTERNAL_VALIDATION — the returned idea carries validation_status + typed requirements +
    resolved operands — or (None, Rejection) for REJECTED (deterministically invalid / unauthorized).
    `roles` gates cross-table join authority (a read-scope-DENIED hop rejects). `src_of` maps
    object_ref -> the candidate catalog source(s), used to resolve each derive's catalog (B3)."""
    derives = [d for d in raw.get("derives_from", []) if d in known]
    if not derives:
        return None, Rejection(RejectCode.UNGROUNDED, "ungrounded")
    pairs: list[tuple[str, str]] = []
    for d in derives:
        srcs = src_of.get(d, set())
        if len(srcs) != 1:
            return None, Rejection(RejectCode.AMBIGUOUS_CATALOG, f"ambiguous catalog for {d}")
        pairs.append((next(iter(srcs)), d))
    meta = _column_meta(conn, pairs)
    for src, d in pairs:
        if d not in meta or meta[d]["catalog_source"] != src:
            return None, Rejection(RejectCode.UNKNOWN_COLUMN, f"unknown column {d} in catalog {src}")
    if target_ref and target_ref in derives:
        return None, Rejection(RejectCode.LEAKAGE, "leaks target")
    if now is not None:
        for src in {p[0] for p in pairs}:
            wm = drift_watermark(conn, src)
            if wm is None or wm < now - fresh_within:
                return None, Rejection(RejectCode.STALE, f"stale source: {src}")

    aggregation = raw.get("aggregation")
    grain_table = raw.get("grain_table")
    catalogs = {p[0] for p in pairs}
    requirements: list[Requirement] = []
    grain_operand: tuple[str, str] | None = None
    time_operand: tuple[str, str] | None = None

    # ── disposition: additivity (Task 7 REPLACES this block) ──
    if _is_additive_unsafe(aggregation):
        for d in derives:
            if meta.get(d, {}).get("additivity") in ("semi_additive", "non_additive"):
                return None, Rejection(RejectCode.ADDITIVITY, f"unsafe additive aggregation of {d}")

    # ── disposition: unit / currency (Task 9 AUGMENTS this block) ──
    units = {meta[d]["unit"] for d in derives if meta.get(d, {}).get("unit")}
    currencies = {meta[d]["currency"] for d in derives if meta.get(d, {}).get("currency")}
    if len(units) > 1:
        return None, Rejection(RejectCode.MIXED_UNITS,
                               f"mixed units {sorted(units)}; aggregation would be silently wrong")
    if len(currencies) > 1:
        return None, Rejection(RejectCode.MIXED_CURRENCY, f"mixed currencies {sorted(currencies)}")

    # ── disposition: point-in-time (Task 8 REPLACES this block) ──
    if _is_windowed(aggregation):
        for src, d in pairs:
            if d.count(".") >= 2 and not _table_has_as_of(conn, src, d.split(".")[-2]):
                return None, Rejection(RejectCode.NO_POINT_IN_TIME,
                                       f"no point-in-time basis for {d} (future-leakage risk)")

    # ── finalize (tri-state) ──
    status = "NEEDS_EXTERNAL_VALIDATION" if requirements else "DESIGN_CHECKED"
    return FeatureIdea(
        name=str(raw.get("name", "")), description=str(raw.get("description", "")),
        derives_from=derives, aggregation=aggregation, grain_table=grain_table,
        derives_pairs=tuple(pairs), rationale=str(raw.get("rationale", "")),
        operation_kind=_norm_agg(aggregation), measure_refs=tuple(pairs),
        grain_ref=grain_operand, time_ref=time_operand, window=_window_of(aggregation),
        grouping_refs=(), validation_status=status, requirements=tuple(requirements)), None
```
- [ ] Thread `roles` through `_vet` and the loop. Change `_vet`'s signature to end with `..., target_ref, now, fresh_within, *, roles: Iterable[str] = ()) -> FeatureIdea | None:` and its `_validate_idea(conn, raw, known, src_of, target_ref, now, fresh_within)` call to pass `roles=roles`. In `_generate`'s loop (the `_vet(conn, raw, known, src_of, registered, accepted, seen, avoid, target_ref, now, fresh_within)` call) add `roles=roles`. Add `roles: Iterable[str] = ()` to `_fix_pass`'s params and pass `roles=roles` both to its `_vet(...)` call and from `_generate`'s `_fix_pass(...)` call. In `refine_idea`, pass `roles=roles` to its `_validate_idea(...)` call.
  > `contract/review.py`'s `validate_minimum` call to `_validate_idea` stays WITHOUT roles (defaults `()`) — threading it there is 3A-ii's job. Verify all four in-repo `_validate_idea(` call sites still type-check.
- [ ] Run it — expect PASS: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_validate_idea_tristate.py -q`
- [ ] Run the feature + contract suites to confirm behavior-identical (no reclassification yet): `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_feature_assist.py tests/featuregen/overlay/upload/test_feature_loop.py tests/featuregen/overlay/upload/test_feature_assist_hitl.py tests/featuregen/overlay/upload/contract/test_review.py tests/featuregen/api/test_feature_assist.py -q`
- [ ] Commit: `feat(slice3a-i): _validate_idea tri-state skeleton — operands, status, roles threaded`

---

## Task 6 — Disposition: `TYPE_IS_NUMERIC` (declared numeric hint) / non-numeric → `REJECTED`

**Files:**
- modify `src/featuregen/overlay/upload/feature_assist.py` (`RejectCode` + `_validate_idea`)
- test append to `tests/featuregen/overlay/upload/test_validate_idea_tristate.py`

**Interfaces:** classification rows: `data_type='unknown'` + declared numeric → `NEEDS_EXTERNAL_VALIDATION` + `Requirement("TYPE_IS_NUMERIC", operand)`; declared **non-numeric** → `REJECTED` (`RejectCode.NON_NUMERIC`); operational `data_type` numeric → clears. Uses `read_column_facts(logical_ref, "logical_representation")` (operational value) + `read_column_facts(logical_ref, "declared_type")` (hint).

- [ ] Append failing tests to `test_validate_idea_tristate.py`:
```python
def _ftr_col(db, table, column, *, data_type="unknown", declared_type=None):
    ref = f"public.{table}.{column}"
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "data_type, declared_type) VALUES ('ftr', %s, 'column', %s, %s, %s, %s)",
        (ref, table, column, data_type, declared_type))
    _fresh(db, "ftr")
    return ref


def test_type_is_numeric_when_data_type_unknown_but_declared_numeric(db):
    ref = _ftr_col(db, "loans", "balance", data_type="unknown", declared_type="numeric")
    known, src_of = _kv([ref], "ftr")
    raw = {"name": "avg_balance", "derives_from": [ref], "aggregation": "avg"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert rej is None
    assert idea.validation_status == "NEEDS_EXTERNAL_VALIDATION"
    codes = [(r.code, r.operand) for r in idea.requirements]
    assert ("TYPE_IS_NUMERIC", ("ftr", ref)) in codes


def test_declared_non_numeric_is_rejected(db):
    ref = _ftr_col(db, "loans", "status", data_type="unknown", declared_type="varchar")
    known, src_of = _kv([ref], "ftr")
    raw = {"name": "avg_status", "derives_from": [ref], "aggregation": "avg"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert idea is None and rej.code == RejectCode.NON_NUMERIC


def test_operational_numeric_data_type_clears_type_check(db):
    ref = _ftr_col(db, "loans", "amt", data_type="numeric", declared_type=None)
    known, src_of = _kv([ref], "ftr")
    raw = {"name": "avg_amt", "derives_from": [ref], "aggregation": "avg"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert rej is None
    assert idea.validation_status == "DESIGN_CHECKED"
    assert all(r.code != "TYPE_IS_NUMERIC" for r in idea.requirements)
```
- [ ] Run — expect FAIL: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_validate_idea_tristate.py -q`
- [ ] Implement. Add `NON_NUMERIC = "NON_NUMERIC"` to `RejectCode`. In `_validate_idea`, insert this block immediately AFTER the `catalogs = {...}` / `requirements = []` / operand initialization and BEFORE the additivity block:
```python
    # ── disposition: numeric type (a numeric op's measure must be numeric; declared_type is a HINT
    #    that may only reject/needs-check, never clear — only operational data_type clears) ──
    if _needs_numeric(aggregation):
        for src, d in pairs:
            lref = logical_ref_of(src, d)
            if _is_numeric(read_column_facts(conn, lref, "logical_representation").value):
                continue
            declared = read_column_facts(conn, lref, "declared_type").value
            if declared and not _is_numeric(declared):
                return None, Rejection(RejectCode.NON_NUMERIC,
                                       f"declared type {declared!r} of {d} is not numeric")
            requirements.append(Requirement("TYPE_IS_NUMERIC", (src, d),
                                            "operational type unknown; numeric declared hint"))
```
- [ ] Run — expect PASS: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_validate_idea_tristate.py -q`
- [ ] Run the feature suites (real-typed columns clear numeric): `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_feature_loop.py tests/featuregen/api/test_feature_assist.py -q`
- [ ] Commit: `feat(slice3a-i): TYPE_IS_NUMERIC disposition — declared numeric hint / non-numeric reject`

---

## Task 7 — Disposition: `ADDITIVITY` (governed non-additive → `REJECTED`; unresolved → needs-check)

**Files:**
- modify `src/featuregen/overlay/upload/feature_assist.py` (`_validate_idea` additivity block)
- test append to `tests/featuregen/overlay/upload/test_validate_idea_tristate.py`
- modify `tests/featuregen/overlay/upload/test_feature_loop.py` (reconcile 2 reclassified tests)

**Interfaces:** a **governed** (`authority=="governed"`) semi/non-additive under an additive-unsafe op → `REJECTED` (`RejectCode.ADDITIVITY`); an **unresolved** additivity (not governed) → `NEEDS_EXTERNAL_VALIDATION` + `Requirement("ADDITIVITY_SUPPORTS_OPERATION", operand)`; governed-additive → clears. This REPLACES the old flat-column reject (spec [F6]).

- [ ] Append failing tests to `test_validate_idea_tristate.py`:
```python
from featuregen.overlay.field_decision import FieldDecisionEventType, record_field_decision
from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.upload.object_ref import normalize_ref


def _govern(db, catalog, ref, field_name, value):
    lref = normalize_ref(catalog, "public", ref.split(".")[-2], ref.split(".")[-1])
    record_field_decision(
        db, logical_ref=lref, field_name=field_name,
        event_type=FieldDecisionEventType.RESOLVED, selected_evidence_ids=[],
        evidence_set_hash=canonical_hash([]), display_value_hash=canonical_hash(value),
        load_bearing_value_hash=canonical_hash(value), conflict_status="resolved",
        reason_codes=[], field_policy_version="upload-field-policy-v1",
        resolver_version="upload-resolve-and-project-v1", actor_ref=None, supersedes_event_id=None)


def test_governed_non_additive_sum_is_rejected(db):
    build_graph(db, "bank", [
        CanonicalRow("bank", "accounts", "balance", "numeric", additivity="non_additive")])
    _fresh(db, "bank")
    _govern(db, "bank", "public.accounts.balance", "additivity", "non_additive")
    known, src_of = _kv(["public.accounts.balance"], "bank")
    raw = {"name": "sum_bal", "derives_from": ["public.accounts.balance"], "aggregation": "sum"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert idea is None and rej.code == RejectCode.ADDITIVITY


def test_unresolved_additivity_sum_needs_external_validation(db):
    build_graph(db, "bank", [
        CanonicalRow("bank", "accounts", "balance", "numeric", additivity="semi_additive")])
    _fresh(db, "bank")   # additivity is file-declared only -> NOT governed
    known, src_of = _kv(["public.accounts.balance"], "bank")
    raw = {"name": "sum_bal", "derives_from": ["public.accounts.balance"], "aggregation": "sum"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert rej is None
    assert idea.validation_status == "NEEDS_EXTERNAL_VALIDATION"
    assert any(r.code == "ADDITIVITY_SUPPORTS_OPERATION" for r in idea.requirements)
```
- [ ] Run — expect FAIL (unresolved case currently rejects on the flat column): `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_validate_idea_tristate.py -q`
- [ ] Implement. Replace the additivity block in `_validate_idea`:
```python
    # ── disposition: additivity — only a GOVERNED semi/non-additive rejects; an unresolved
    #    (file-declared / hint) additivity is honest needs-check (spec [F6]) ──
    if _is_additive_unsafe(aggregation):
        for src, d in pairs:
            facts = read_column_facts(conn, logical_ref_of(src, d), "additivity")
            if facts.authority == "governed" and facts.value in ("semi_additive", "non_additive"):
                return None, Rejection(RejectCode.ADDITIVITY, f"unsafe additive aggregation of {d}")
            if facts.authority != "governed":
                requirements.append(Requirement("ADDITIVITY_SUPPORTS_OPERATION", (src, d),
                                                "additivity not governed-confirmed"))
```
- [ ] Reconcile the two reclassified existing tests in `tests/featuregen/overlay/upload/test_feature_loop.py`. Add a governing helper near the top (after `_fresh_watermark`):
```python
def _govern_additivity(db, source, table, column, value):
    from featuregen.overlay.field_decision import FieldDecisionEventType, record_field_decision
    from featuregen.overlay.field_evidence import canonical_hash
    from featuregen.overlay.upload.object_ref import normalize_ref
    lref = normalize_ref(source, "public", table, column)
    record_field_decision(
        db, logical_ref=lref, field_name="additivity",
        event_type=FieldDecisionEventType.RESOLVED, selected_evidence_ids=[],
        evidence_set_hash=canonical_hash([]), display_value_hash=canonical_hash(value),
        load_bearing_value_hash=canonical_hash(value), conflict_status="resolved",
        reason_codes=[], field_policy_version="upload-field-policy-v1",
        resolver_version="upload-resolve-and-project-v1", actor_ref=None, supersedes_event_id=None)
```
  In `test_loop_rejects_leaky_and_unsafe_keeps_good`, after `_fresh_watermark(db, "bank", NOW)` add `_govern_additivity(db, "bank", "accounts", "balance", "non_additive")` so the "unsafe" SUM stays REJECTED (confirmed-non-additive) and the assertion `names == {"good"}` holds. In `test_gauntlet_catches_additive_unsafe_names_without_sum`, after `_fresh_watermark(db, "t", NOW)` add `_govern_additivity(db, "t", "accounts", "balance", "semi_additive")` so the `== []` (all rejected) assertion holds.
- [ ] Run both — expect PASS: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_validate_idea_tristate.py tests/featuregen/overlay/upload/test_feature_loop.py -q`
- [ ] Commit: `feat(slice3a-i): ADDITIVITY disposition — governed reject vs unresolved needs-check`

---

## Task 8 — Disposition: `GRAIN_IS_UNIQUE` + `TEMPORAL_IS_POPULATED`

**Files:**
- modify `src/featuregen/overlay/upload/feature_assist.py` (`_validate_idea` PIT block → temporal + grain)
- test append to `tests/featuregen/overlay/upload/test_validate_idea_tristate.py`

**Interfaces:** windowed feature whose as-of column is **declared-not-confirmed** → `NEEDS_EXTERNAL_VALIDATION` + `Requirement("TEMPORAL_IS_POPULATED", (catalog, as_of_ref))` (governed as-of clears; NO as-of column at all still → `REJECTED` `NO_POINT_IN_TIME`). Grain feature (`grain_table` set) whose grain column is **declared-not-confirmed** → `Requirement("GRAIN_IS_UNIQUE", (catalog, grain_ref))` (governed grain clears). Sets the `time_ref` / `grain_ref` typed operands. Uses `read_column_facts(..., "is_as_of"/"is_grain")` + `_as_of_column_ref`/`_grain_column_ref`.

- [ ] Append failing tests to `test_validate_idea_tristate.py`:
```python
def test_windowed_declared_as_of_needs_temporal(db):
    _bank(db)   # posted_at as_of=True but file-declared (no availability_fact_event_id)
    known, src_of = _kv(["public.accounts.balance"], "bank")
    raw = {"name": "avg_bal_90d", "derives_from": ["public.accounts.balance"],
           "aggregation": "avg_90d"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert rej is None
    assert idea.validation_status == "NEEDS_EXTERNAL_VALIDATION"
    temporal = [r for r in idea.requirements if r.code == "TEMPORAL_IS_POPULATED"]
    assert temporal and temporal[0].operand == ("bank", "public.accounts.posted_at")
    assert idea.time_ref == ("bank", "public.accounts.posted_at")


def test_governed_as_of_clears_temporal(db):
    _bank(db)
    db.execute("UPDATE graph_node SET availability_fact_event_id = 'evt_av' "
               "WHERE object_ref = 'public.accounts.posted_at'")
    known, src_of = _kv(["public.accounts.balance"], "bank")
    raw = {"name": "avg_bal_90d", "derives_from": ["public.accounts.balance"],
           "aggregation": "avg_90d"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert rej is None
    assert all(r.code != "TEMPORAL_IS_POPULATED" for r in idea.requirements)


def test_windowed_with_no_as_of_column_is_rejected(db):
    build_graph(db, "t", [
        CanonicalRow("t", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("t", "accounts", "balance", "numeric")])   # no as_of column at all
    _fresh(db, "t")
    known, src_of = _kv(["public.accounts.balance"], "t")
    raw = {"name": "avg_bal_90d", "derives_from": ["public.accounts.balance"],
           "aggregation": "avg_90d"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert idea is None and rej.code == RejectCode.NO_POINT_IN_TIME


def test_grain_declared_not_confirmed_needs_grain_is_unique(db):
    _bank(db)   # id is_grain=True but file-declared (no grain_fact_event_id)
    known, src_of = _kv(["public.accounts.balance"], "bank")
    raw = {"name": "cnt_per_account", "derives_from": ["public.accounts.balance"],
           "aggregation": "count", "grain_table": "accounts"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert rej is None
    grain = [r for r in idea.requirements if r.code == "GRAIN_IS_UNIQUE"]
    assert grain and grain[0].operand == ("bank", "public.accounts.id")
    assert idea.grain_ref == ("bank", "public.accounts.id")


def test_governed_grain_clears_grain_check(db):
    _bank(db)
    db.execute("UPDATE graph_node SET grain_fact_event_id = 'evt_g' "
               "WHERE object_ref = 'public.accounts.id'")
    known, src_of = _kv(["public.accounts.balance"], "bank")
    raw = {"name": "cnt_per_account", "derives_from": ["public.accounts.balance"],
           "aggregation": "count", "grain_table": "accounts"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert rej is None
    assert all(r.code != "GRAIN_IS_UNIQUE" for r in idea.requirements)
    assert idea.grain_ref == ("bank", "public.accounts.id")
```
- [ ] Run — expect FAIL: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_validate_idea_tristate.py -q`
- [ ] Implement. Replace the point-in-time block in `_validate_idea` with the temporal + grain dispositions:
```python
    # ── disposition: temporal — a windowed feature needs a governed-VERIFIED as-of column; a table
    #    with NO as-of column at all is still a hard reject (future-leakage risk) ──
    if _is_windowed(aggregation):
        for src, d in pairs:
            if d.count(".") >= 2:
                aref = _as_of_column_ref(conn, src, d.split(".")[-2])
                if aref is None:
                    return None, Rejection(RejectCode.NO_POINT_IN_TIME,
                                           f"no point-in-time basis for {d} (future-leakage risk)")
                facts = read_column_facts(conn, logical_ref_of(src, aref), "is_as_of")
                if facts.authority != "governed":
                    time_operand = (src, aref)
                    requirements.append(Requirement("TEMPORAL_IS_POPULATED", (src, aref),
                                                    "as-of column declared, not governed-verified"))

    # ── disposition: grain — a grain feature needs a governed-VERIFIED grain column ──
    if grain_table and len(catalogs) == 1:
        gcat = next(iter(catalogs))
        gref = _grain_column_ref(conn, gcat, grain_table)
        if gref is not None:
            grain_operand = (gcat, gref)
            if read_column_facts(conn, logical_ref_of(gcat, gref), "is_grain").authority != "governed":
                requirements.append(Requirement("GRAIN_IS_UNIQUE", (gcat, gref),
                                                "grain declared, not governed-verified"))
```
- [ ] Run — expect PASS: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_validate_idea_tristate.py -q`
- [ ] Run the feature suites (windowed features stay RETURNED, just needs-check; no removals): `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_feature_loop.py tests/featuregen/overlay/upload/test_feature_assist.py -q`
- [ ] Commit: `feat(slice3a-i): GRAIN_IS_UNIQUE + TEMPORAL_IS_POPULATED dispositions`

---

## Task 9 — Disposition: `UNIT_CONSISTENT` / `CURRENCY_CONSISTENT` (absent across a combining op)

**Files:**
- modify `src/featuregen/overlay/upload/feature_assist.py` (`_validate_idea` unit/currency block)
- test append to `tests/featuregen/overlay/upload/test_validate_idea_tristate.py`

**Interfaces:** two operands declaring **different** unit/currency → `REJECTED` (preserved: `MIXED_UNITS`/`MIXED_CURRENCY`); unit/currency **absent/unknown** across a **combining** op (≥2 measures) → `NEEDS_EXTERNAL_VALIDATION` + distinct `Requirement("UNIT_CONSISTENT"/"CURRENCY_CONSISTENT", ...)`. A single-measure op adds neither.

- [ ] Append failing tests to `test_validate_idea_tristate.py`:
```python
def test_absent_units_across_combining_op_needs_unit_and_currency_consistent(db):
    build_graph(db, "t", [
        CanonicalRow("t", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("t", "accounts", "a", "numeric"),   # no unit / currency
        CanonicalRow("t", "accounts", "b", "numeric")])
    _fresh(db, "t")
    known, src_of = _kv(["public.accounts.a", "public.accounts.b"], "t")
    raw = {"name": "ratio_ab", "derives_from": ["public.accounts.a", "public.accounts.b"],
           "aggregation": "ratio"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert rej is None
    codes = {r.code for r in idea.requirements}
    assert "UNIT_CONSISTENT" in codes and "CURRENCY_CONSISTENT" in codes


def test_mixed_units_still_hard_rejected(db):
    build_graph(db, "t", [
        CanonicalRow("t", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("t", "accounts", "a", "numeric", unit="dollars"),
        CanonicalRow("t", "accounts", "b", "numeric", unit="cents")])
    _fresh(db, "t")
    known, src_of = _kv(["public.accounts.a", "public.accounts.b"], "t")
    raw = {"name": "sum_ab", "derives_from": ["public.accounts.a", "public.accounts.b"],
           "aggregation": "avg"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert idea is None and rej.code == RejectCode.MIXED_UNITS


def test_single_measure_absent_unit_adds_no_requirement(db):
    build_graph(db, "t", [
        CanonicalRow("t", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("t", "accounts", "a", "numeric")])
    _fresh(db, "t")
    known, src_of = _kv(["public.accounts.a"], "t")
    raw = {"name": "avg_a", "derives_from": ["public.accounts.a"], "aggregation": "avg"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert rej is None
    assert all(r.code not in ("UNIT_CONSISTENT", "CURRENCY_CONSISTENT") for r in idea.requirements)
```
- [ ] Run — expect FAIL: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_validate_idea_tristate.py -q`
- [ ] Implement. In `_validate_idea`, AFTER the two `MIXED_UNITS`/`MIXED_CURRENCY` reject returns, append:
```python
    if len(pairs) >= 2:   # a COMBINING op: unknown scale/currency is a fact to verify, not a reject
        if not units:
            requirements.append(Requirement("UNIT_CONSISTENT", pairs[0],
                                            "units unknown across a combining op"))
        if not currencies:
            requirements.append(Requirement("CURRENCY_CONSISTENT", pairs[0],
                                            "currency unknown across a combining op"))
```
- [ ] Run — expect PASS: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_validate_idea_tristate.py -q`
- [ ] Run the mixed-units existing test: `.venv/bin/python -m pytest "tests/featuregen/overlay/upload/test_feature_loop.py::test_gauntlet_rejects_mixed_units" -q`
- [ ] Commit: `feat(slice3a-i): UNIT_CONSISTENT / CURRENCY_CONSISTENT needs-check for absent scale`

---

## Task 10 — Disposition: cross-table join (`JOIN_CONNECTIVITY` / `REJECTED`) via `classify_join_path`

**Files:**
- modify `src/featuregen/overlay/upload/feature_assist.py` (`RejectCode` + `_validate_idea`)
- test append to `tests/featuregen/overlay/upload/test_validate_idea_tristate.py`

**Interfaces:** for a feature whose `grain_table` differs from a measure's table (single catalog), `classify_join_path(conn, catalog, grain_table, measure_table, roles=roles)` maps: `OPERATIONAL` → clears; `UNVERIFIED` → `Requirement("JOIN_CONNECTIVITY", operand)`; `NO_PATH` → `REJECTED` (`RejectCode.NO_JOIN_PATH`); `DENIED` → `REJECTED` (`RejectCode.JOIN_DENIED`). `roles` reaches here from Task 5's threading.

- [ ] Append failing tests to `test_validate_idea_tristate.py`:
```python
def _two_table(db, *, fact_key=None, status=None, acct_sensitivity=None):
    db.execute("INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
               "data_type) VALUES ('bank', 'public.transactions.amount', 'column', 'transactions', "
               "'amount', 'numeric')")
    db.execute("INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name) "
               "VALUES ('bank', 'public.transactions.acct_id', 'column', 'transactions', 'acct_id')")
    db.execute("INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
               "is_grain, sensitivity) VALUES ('bank', 'public.accounts.account_id', 'column', "
               "'accounts', 'account_id', true, %s)", (acct_sensitivity,))
    db.execute("INSERT INTO graph_edge (catalog_source, kind, from_ref, to_ref, cardinality, "
               "authority, approved_join_fact_key, approved_join_status) VALUES ('bank', 'joins', "
               "'public.transactions.acct_id', 'public.accounts.account_id', 'N:1', 'operational', "
               "%s, %s)", (fact_key, status))
    _fresh(db, "bank")


def test_cross_table_operational_join_clears(db):
    _two_table(db)   # declared edge -> OPERATIONAL
    known, src_of = _kv(["public.transactions.amount"], "bank")
    raw = {"name": "sum_txn_per_acct", "derives_from": ["public.transactions.amount"],
           "aggregation": "count", "grain_table": "accounts"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH, roles=())
    assert rej is None
    assert all(r.code != "JOIN_CONNECTIVITY" for r in idea.requirements)


def test_cross_table_unverified_join_needs_join_connectivity(db):
    _two_table(db, fact_key="ajf-1", status="PROPOSED")   # authorized but unverified
    known, src_of = _kv(["public.transactions.amount"], "bank")
    raw = {"name": "sum_txn_per_acct", "derives_from": ["public.transactions.amount"],
           "aggregation": "count", "grain_table": "accounts"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH, roles=())
    assert rej is None
    assert any(r.code == "JOIN_CONNECTIVITY" for r in idea.requirements)


def test_cross_table_no_path_is_rejected(db):
    db.execute("INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
               "data_type) VALUES ('bank', 'public.transactions.amount', 'column', 'transactions', "
               "'amount', 'numeric')")
    db.execute("INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
               "is_grain) VALUES ('bank', 'public.accounts.account_id', 'column', 'accounts', "
               "'account_id', true)")
    _fresh(db, "bank")   # no join edge between transactions and accounts
    known, src_of = _kv(["public.transactions.amount"], "bank")
    raw = {"name": "sum_txn_per_acct", "derives_from": ["public.transactions.amount"],
           "aggregation": "count", "grain_table": "accounts"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH, roles=())
    assert idea is None and rej.code == RejectCode.NO_JOIN_PATH


def test_cross_table_read_scope_denied_hop_is_rejected(db):
    _two_table(db, acct_sensitivity="pii")   # the only hop's endpoint is pii-hidden for roles=()
    known, src_of = _kv(["public.transactions.amount"], "bank")
    raw = {"name": "sum_txn_per_acct", "derives_from": ["public.transactions.amount"],
           "aggregation": "count", "grain_table": "accounts"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH, roles=())
    assert idea is None and rej.code == RejectCode.JOIN_DENIED
```
- [ ] Run — expect FAIL: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_validate_idea_tristate.py -q`
- [ ] Implement. Add `NO_JOIN_PATH = "NO_JOIN_PATH"` and `JOIN_DENIED = "JOIN_DENIED"` to `RejectCode`. In `_validate_idea`, insert this block immediately BEFORE the `# ── finalize (tri-state) ──` block:
```python
    # ── disposition: cross-table join authority (spec §7). A measure in a different table than the
    #    grain needs a real path; UNVERIFIED -> JOIN_CONNECTIVITY, no-path / read-scope-denied -> reject ──
    if grain_table and len(catalogs) == 1:
        jcat = next(iter(catalogs))
        for src, d in pairs:
            if d.count(".") >= 2 and d.split(".")[-2] != grain_table:
                outcome = classify_join_path(conn, jcat, grain_table, d.split(".")[-2], roles=roles)
                if outcome.kind == JoinOutcome.NO_PATH:
                    return None, Rejection(RejectCode.NO_JOIN_PATH,
                                           f"no join path {grain_table} -> {d}")
                if outcome.kind == JoinOutcome.DENIED:
                    return None, Rejection(RejectCode.JOIN_DENIED,
                                           f"join {grain_table} -> {d} crosses a read-scope-denied hop")
                if outcome.kind == JoinOutcome.UNVERIFIED:
                    requirements.append(Requirement("JOIN_CONNECTIVITY", (src, d),
                                                    "join authorized but not verified"))
```
- [ ] Run — expect PASS: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_validate_idea_tristate.py -q`
- [ ] Run the full feature + contract + api feature suites to catch any cross-table reclassification: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_feature_assist.py tests/featuregen/overlay/upload/test_feature_loop.py tests/featuregen/overlay/upload/test_feature_assist_hitl.py tests/featuregen/overlay/upload/contract tests/featuregen/api/test_feature_assist.py -q`
- [ ] Commit: `feat(slice3a-i): cross-table join dispositions — JOIN_CONNECTIVITY / no-path / denied reject`

---

## Task 11 — `_template_candidates` keeps the validator's RETURNED honest idea

**Files:**
- modify `src/featuregen/overlay/upload/contract/gate1.py` (`_template_candidates`)
- test `tests/featuregen/overlay/upload/contract/test_template_status.py` (new)

**Interfaces:** `_template_candidates` (`gate1.py:~149`) currently appends the **pre-validation** `_idea_from_grounded` object, discarding the validator's returned idea (its status + requirements). Fix: append the validator's **returned** idea. Verify the exact line by symbol — `_, rej = _validate_idea(...)` followed by `if rej is None: ideas.append(idea)`.

- [ ] Write the failing test `tests/featuregen/overlay/upload/contract/test_template_status.py`. It monkeypatches the `ground_all` symbol imported INTO `gate1` (so `_template_candidates` runs its REAL convert → `_validate_idea` → append path) with a hand-built `GroundedFeature` bound to an FTR numeric column, plus a fully-valid minimal `Template` for the `by_id[gf.template_id]` lookup — this avoids the fragile real-grounding `needs` contract and tests exactly the [F9] line:
```python
from datetime import UTC, datetime

import featuregen.overlay.upload.contract.gate1 as gate1
from featuregen.overlay.upload.contract.gate1 import _template_candidates
from featuregen.overlay.upload.templates import GroundedFeature, Template

NOW = datetime(2026, 7, 18, tzinfo=UTC)

# A fully-valid minimal Template whose id matches the grounded feature below. needs=()/params={} are
# never exercised because ground_all is monkeypatched — this object only serves the by_id lookup +
# _idea_from_grounded(template.intent).
_TMPL = Template(id="sum_balance", family="balance_stock", intent="total balance per loan",
                 needs=(), params={}, aggregation="sum", additivity="additive", explain="M",
                 use_cases=(), pit="")

_GF = GroundedFeature(template_id="sum_balance", name="sum_balance", aggregation="sum",
                      grain_table=None, as_of_column=None,
                      derives_pairs=(("ftr", "public.loans.balance"),), params={})


def _ftr_numeric_graph(db):
    # An FTR-shaped column: operational data_type 'unknown' + a numeric declared_type hint. A numeric
    # aggregation over it must resolve to NEEDS_EXTERNAL_VALIDATION (TYPE_IS_NUMERIC).
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "data_type, declared_type) VALUES ('ftr', 'public.loans.balance', 'column', 'loans', "
        "'balance', 'unknown', 'numeric')")
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, "
        "head_seq) VALUES ('ftr', %s, 'r', 0) ON CONFLICT (catalog_source) DO UPDATE SET "
        "last_completed_at = %s", (NOW, NOW))


def test_template_candidate_carries_needs_external_validation_status(db, monkeypatch):
    _ftr_numeric_graph(db)
    monkeypatch.setattr(gate1, "ground_all", lambda *a, **k: [_GF])
    ideas, rejections, grounded_ids, rejected_ids, binding = _template_candidates(
        db, catalog_source="ftr", roles=(), target_ref=None, now=NOW, templates=(_TMPL,))
    assert ideas, "the grounded numeric template should survive as a needs-check candidate"
    idea = ideas[0]
    # [F9]: the APPENDED idea is the validator's RETURNED idea (status + requirements), not the
    # pre-validation DESIGN-CHECKED _idea_from_grounded object.
    assert idea.validation_status == "NEEDS_EXTERNAL_VALIDATION"
    assert any(r.code == "TYPE_IS_NUMERIC" for r in idea.requirements)
    assert "sum_balance" in grounded_ids
```
  > IMPLEMENTER: confirm `ground_all` is imported into `gate1` as a bare name (it is: `from ...templates import ALL_TEMPLATES, GroundedFeature, Template, ground_all`) so `monkeypatch.setattr(gate1, "ground_all", ...)` intercepts the call `_template_candidates` makes. Confirm the real `GroundedFeature`/`Template` field names before running (anchored on the shapes read from `templates.py`).
- [ ] Run it — expect FAIL (the appended idea is the pre-validation DESIGN-CHECKED object; `validation_status == "DESIGN_CHECKED"`): `.venv/bin/python -m pytest tests/featuregen/overlay/upload/contract/test_template_status.py -q`
- [ ] Implement. In `_template_candidates`, change the capture + append. Replace:
```python
        _, rej = _validate_idea(conn, raw, known, src_of, target_ref, now, fresh_within)
        if rej is None:
            ideas.append(idea)   # keep the converted idea (identical to the gauntlet's rebuild)
```
  with:
```python
        validated, rej = _validate_idea(conn, raw, known, src_of, target_ref, now, fresh_within,
                                        roles=roles)
        if rej is None:
            ideas.append(validated)   # [F9] keep the VALIDATOR's idea (carries status + requirements)
```
  > `validated` is never `None` when `rej is None` (the tri-state contract), so the append is safe. Threading `roles` here matches the read-scoped candidate universe `_template_candidates` already builds.
- [ ] Run it — expect PASS: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/contract/test_template_status.py -q`
- [ ] Run the gate1 suite (grounding/snapshot behavior must be unchanged for the surviving-idea flow): `.venv/bin/python -m pytest tests/featuregen/overlay/upload/contract/test_gate1.py tests/featuregen/overlay/upload/contract/test_gate1_scoped.py -q`
- [ ] Commit: `fix(slice3a-i): _template_candidates keeps the validator's returned honest idea [F9]`

---

## Final verification (before requesting the OPUS whole-branch review)

- [ ] Run every suite this branch touches, DIRECTLY (never `| tail`):
```
.venv/bin/python -m pytest \
  tests/featuregen/overlay/upload/test_feature_computation_contract.py \
  tests/featuregen/overlay/upload/test_column_authority.py \
  tests/featuregen/overlay/upload/test_join_outcome.py \
  tests/featuregen/overlay/upload/test_route_strategies_declared.py \
  tests/featuregen/overlay/upload/test_validate_idea_tristate.py \
  tests/featuregen/overlay/upload/test_feature_assist.py \
  tests/featuregen/overlay/upload/test_feature_loop.py \
  tests/featuregen/overlay/upload/test_feature_assist_hitl.py \
  tests/featuregen/overlay/upload/test_features.py \
  tests/featuregen/overlay/upload/passc \
  tests/featuregen/overlay/upload/test_join_governance.py \
  tests/featuregen/overlay/upload/contract \
  tests/featuregen/api/test_feature_assist.py \
  tests/featuregen/api/test_full_ingestion_e2e.py \
  tests/featuregen/api/test_governance_routes.py -q
```
- [ ] `.venv/bin/python -m ruff check src/featuregen/overlay/upload/column_authority.py src/featuregen/overlay/upload/join_path.py src/featuregen/overlay/upload/feature_assist.py src/featuregen/overlay/upload/contract/gate1.py`
- [ ] Request the OPUS whole-branch review.

---

## Self-Review

**Spec coverage (§1, §2, §4, §7 + FTR-routing + template fixes):**
- §1 typed computation contract → Task 1 (`operation_kind`/`measure_refs`/`grain_ref`/`time_ref`/`window`/`grouping_refs`) + operand population in Tasks 5/8.
- §2 three-state validator + corrected classification — every TABLE row has a concrete test: UNGROUNDED / AMBIGUOUS_CATALOG / UNKNOWN_COLUMN / LEAKAGE / STALE (Task 5); TYPE_IS_NUMERIC + declared-non-numeric REJECTED (Task 6); confirmed-non-additive REJECTED + unresolved ADDITIVITY_SUPPORTS_OPERATION (Task 7); GRAIN_IS_UNIQUE + TEMPORAL_IS_POPULATED + NO_POINT_IN_TIME reject (Task 8); MIXED_UNITS/CURRENCY REJECTED + absent UNIT_CONSISTENT/CURRENCY_CONSISTENT (Task 9); JOIN_CONNECTIVITY + no-path/denied REJECTED (Task 10). DESIGN_CHECKED clean path (Task 5).
- §2 [F10] FTR routing → Task 4 (`declared_type` numeric hint enables `ratio` while `data_type` stays `unknown`).
- §2 [F9] template fix → Task 11.
- §4 concrete authority readers → Task 2 (governed via `is_feature_eligible` / `*_fact_event_id`; hint for unit/currency/entity/declared_type; never reads a decision's load-bearing value; `logical_representation` value = operational `data_type`).
- §7 authorization threading + join outcomes → Task 3 (`JoinOutcome` + `classify_join_path`, per-hop Python classification, drops VERIFIED/sensitivity predicates) + `roles` threaded through `_validate_idea`/`_vet`/`_fix_pass`/`refine_idea`/`_template_candidates` (Tasks 5/10/11).
- Out of scope here (correctly deferred): §3 carry-through/persistence + `validate_minimum` requirements (3A-ii), §5 menu enrichment + egress adapter + §6 relevance (3A-iii), §8 v2 schemas/versioning/serializers/flag + §9 quality gate (3A-iv).

**Placeholder scan:** No `...`/`# TODO` in any test or implementation block. One drafting artifact is explicitly called out for deletion in Task 2 (the placeholder-only `test_additivity_governed_when_decision_is_load_bearing`); every real test uses concrete assertions. Task 11's `Template(...)` shape carries an explicit "confirm the real constructor" instruction since the templates module was not opened in full.

**Type consistency vs the shared contract:** `REQUIREMENT_CODES` (frozenset, exact 8 members), `VALIDATION_STATES` (exact tuple), `Requirement(code, operand, detail="")`, the eight `FeatureIdea` fields with the contract's exact defaults, `OperationalColumnFacts(value, authority, provenance)`, `read_column_facts(conn, logical_ref, field_name)`, `JoinOutcome` kinds `OPERATIONAL`/`UNVERIFIED`/`NO_PATH`/`DENIED` with `steps`/`endpoints`/`fact_keys` — all used verbatim. New `RejectCode` members (`NON_NUMERIC`/`NO_JOIN_PATH`/`JOIN_DENIED`) are the REJECTED-path vocabulary and are deliberately distinct from the closed `REQUIREMENT_CODES` (which is the NEEDS_EXTERNAL_VALIDATION vocabulary).

**Ambiguities / deviations flagged for the orchestrator:**
1. **`_validate_idea` signature.** The shared contract's illustrative signature `_validate_idea(conn, idea, *, target_ref=None, now=None, fresh_within=..., roles=())` OMITS the existing `known`/`src_of` grounding-resolution params. Those are load-bearing (catalog resolution, existence check, the MCV/refine call sites pass them), so this plan KEEPS them and appends `roles` as a keyword with default `()`. Net delta vs today = the `roles` addition + the tri-state return. If the orchestrator truly wants the abbreviated positional shape, a follow-up refactor of all four call sites is needed.
2. **`find_join_path` return type.** The contract prose says find_join_path's return "changes to JoinOutcome". Doing that literally breaks ~40 `is None` / `== [JoinStep(...)]` assertions across passc / join_governance / e2e and the 3B `author.py` cross-catalog caller. This plan honors the JoinOutcome TYPE exactly via a NEW producer `classify_join_path` and keeps `find_join_path` as a byte-identical `list|None` façade. If a literal signature change is required, add a dedicated migration task for those downstream callers/assertions (out of a foundation plan's safe blast radius).
3. **Non-public schema authority.** `read_column_facts` rebuilds the decision-log `logical_ref` as public-flattened (matching how `graph_node` stores object_refs). If a source used a non-`public` schema, `is_feature_eligible` may miss the schema-preserving decision and fall back to `authority="hint"` — conservative (never wrongly clears a check), but a real edge. All in-repo uploads use `public`; flagged in case a later slice needs true schema-preserving lookup.
4. **`_needs_numeric` / `_is_windowed` are naming-based** (inherited limitation the codebase already documents). The typed `operation_kind` operand is populated but the dispositions still key off the aggregation string; a future slice with a structured `operation_kind` from the LLM schema could tighten this.

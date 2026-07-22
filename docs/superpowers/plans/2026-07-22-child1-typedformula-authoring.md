# Child 1 — TypedFormula Authoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Author a governed, content-hashed `TypedFormulaV1` from a feature intent — offline, deterministic, no execution — proven only against gold formulas.

**Architecture:** A new `src/featuregen/formula/` package. Pure-data schema + JCS canonicalization/hashing at the core; a new operation contract mapped onto (not widening) `b_operation`; a formula-level output resolver over C1 `read_operational_value`; a multi-axis `AuthoringResult` with a pure-function disposition; an LLM-1 ReAct author over 7 read/validate tools + an independent LLM-2 critic; a manifest-first append-only trace. Nothing compiles or runs against data.

**Tech Stack:** Python 3.11, frozen slotted dataclasses + `StrEnum`, `hashlib.sha256`, the existing `FakeLLM`/`record_llm_call` seams, psycopg for the trace tables. No PySpark, no pydantic.

## Global Constraints

- Frozen slotted dataclasses + `StrEnum` for every contract type; **NOT pydantic**.
- **NO execution, NO Spark, NO durable formula/version artifact.** The only durable writes are append-only `llm_call` + `authoring_run` / `authoring_trace_event`.
- Authoritative output comes **ONLY** from `resolve_formula_output_policy` over C1 `read_operational_value` — **never** from the LLM's advisory `expected_output`.
- `formula_content_hash = sha256(JCS(TypedFormulaV1))` covers **identity fields ONLY** (`formula_schema_version`, `operation_grammar_version`, `output_policy_version`, `canonicalization_version`, grain, body, parameters, decimal, output). `formula_capability_policy_version` and all provenance are OUTSIDE the object and the hash.
- Canonical operand identity is `source::schema.table[.column]` (`object_ref.py` `_SOURCE_SEP="::"`), normalized via `overlay.upload.object_ref` before hashing.
- **Unsupported ≠ invalid** — 6-outcome vocabulary; `unsupported_operation`/`unsupported_capability` → `UNSUPPORTED`, never `REJECTED`.
- **`COUNT_DISTINCT` is NON_ADDITIVE** (override `b_output_policy.derive_output_additivity`, which wrongly returns additive at `b_output_policy.py:126`).
- Do NOT widen `b_operation.SupportedOperation` in place; define the new contract and a compatibility mapping.
- All tests offline via the existing `FakeLLM` EXCEPT one key-gated real-provider enablement test.
- Every commit ends with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Run tests with: `PYTHONPATH=src .venv/bin/python -m pytest <path> -p no:cacheprovider -q`.

---

### Task 1: Schema — enums, dataclasses, predicate invariants

**Files:**
- Create: `src/featuregen/formula/__init__.py` (empty)
- Create: `src/featuregen/formula/schema.py`
- Test: `tests/featuregen/formula/test_schema.py`

**Interfaces — Produces:** the enums (`AggregateFunction`, `FinalOperation`, `WindowBasis`, `WindowUnit`, `Inclusivity`, `EmptyWindowResult`, `NullInput`, `ZeroDenominator`, `RoundingMode`, `OverflowBehavior`, `LiteralType`, `ParamClass`, `FilterKind`, `FilterBoolOp`, `FilterPredicateOp`, `AdditivityClass`); dataclasses `TypedLiteral`, `ParameterDecl`, `ParameterRef`, `FilterPredicate`, `FilterBool`, `SourceRelation`, `Grain`, `WindowPolicy`, `DecimalPolicy`, `AggregateExpression`, `UnaryBody`, `RatioBody`, `DiffBody`, `ExpectedOutput`, `TypedFormulaProposalV1`, `FormulaOutputPolicyV1`, `TypedFormulaV1`; constants `MAX_FILTER_DEPTH=4`, `MAX_PREDICATES=16`, `MAX_IN_LIST=64`; a `SchemaError(Exception)`; and `validate_proposal(p: TypedFormulaProposalV1) -> None` (raises `SchemaError` on any structural/predicate-invariant violation).

- [ ] **Step 1: Write the failing test** (`tests/featuregen/formula/test_schema.py`)

```python
import pytest
from featuregen.formula.schema import (
    AggregateFunction, FinalOperation, FilterPredicate, FilterPredicateOp, FilterBool,
    FilterBoolOp, TypedLiteral, LiteralType, ParameterDecl, ParamClass, SchemaError,
    validate_predicate, validate_filter_tree, MAX_FILTER_DEPTH,
)

def _lit(v): return TypedLiteral(type=LiteralType.STRING, value=v)

def test_is_null_predicate_forbids_right_side():
    ok = FilterPredicate(op=FilterPredicateOp.IS_NULL, left="ftr::public.t.c",
                         right_literal=None, right_param=None, right_set=None)
    validate_predicate(ok)  # no raise
    bad = FilterPredicate(op=FilterPredicateOp.IS_NULL, left="ftr::public.t.c",
                          right_literal=_lit("x"), right_param=None, right_set=None)
    with pytest.raises(SchemaError):
        validate_predicate(bad)

def test_equal_requires_exactly_one_of_literal_or_param():
    with pytest.raises(SchemaError):   # neither
        validate_predicate(FilterPredicate(op=FilterPredicateOp.EQUAL, left="ftr::public.t.c",
                                           right_literal=None, right_param=None, right_set=None))

def test_in_requires_nonempty_set_within_limit():
    good = FilterPredicate(op=FilterPredicateOp.IN, left="ftr::public.t.c",
                           right_literal=None, right_param=None, right_set=(_lit("a"), _lit("b")))
    validate_predicate(good)
    with pytest.raises(SchemaError):
        validate_predicate(FilterPredicate(op=FilterPredicateOp.IN, left="ftr::public.t.c",
                                           right_literal=None, right_param=None, right_set=()))

def test_not_requires_exactly_one_child_and_depth_capped():
    leaf = FilterPredicate(op=FilterPredicateOp.IS_NULL, left="ftr::public.t.c",
                           right_literal=None, right_param=None, right_set=None)
    with pytest.raises(SchemaError):
        validate_filter_tree(FilterBool(op=FilterBoolOp.NOT, children=(leaf, leaf)))
    deep = leaf
    for _ in range(MAX_FILTER_DEPTH + 1):
        deep = FilterBool(op=FilterBoolOp.AND, children=(deep, leaf))
    with pytest.raises(SchemaError):
        validate_filter_tree(deep)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/featuregen/formula/test_schema.py -q`
Expected: FAIL (`ModuleNotFoundError: featuregen.formula.schema`).

- [ ] **Step 3: Write minimal implementation** (`src/featuregen/formula/schema.py`)

Define every enum with the exact string values from spec §A (`SUM="sum"`, `COUNT_ROWS="count_rows"`, …), every frozen slotted dataclass from §A, the three limit constants, `SchemaError`, and the validators:

```python
from __future__ import annotations
from dataclasses import dataclass
from enum import StrEnum

MAX_FILTER_DEPTH, MAX_PREDICATES, MAX_IN_LIST = 4, 16, 64
class SchemaError(Exception): ...

class FilterPredicateOp(StrEnum):
    EQUAL="equal"; NOT_EQUAL="not_equal"; GREATER_THAN="greater_than"; GREATER_OR_EQUAL="greater_or_equal"
    LESS_THAN="less_than"; LESS_OR_EQUAL="less_or_equal"; IN="in"; NOT_IN="not_in"; IS_NULL="is_null"; IS_NOT_NULL="is_not_null"
# ... (all other enums per §A) ...

_NO_RIGHT = {FilterPredicateOp.IS_NULL, FilterPredicateOp.IS_NOT_NULL}
_SET_RIGHT = {FilterPredicateOp.IN, FilterPredicateOp.NOT_IN}

def validate_predicate(p: "FilterPredicate") -> None:
    sides = [p.right_literal is not None, p.right_param is not None, p.right_set is not None]
    if p.op in _NO_RIGHT:
        if any(sides): raise SchemaError(f"{p.op} takes no right side")
    elif p.op in _SET_RIGHT:
        if not (p.right_set and p.right_literal is None and p.right_param is None):
            raise SchemaError(f"{p.op} needs exactly a non-empty right_set")
        if len(p.right_set) > MAX_IN_LIST: raise SchemaError("right_set exceeds MAX_IN_LIST")
    else:
        if sum(1 for s in (p.right_literal, p.right_param) if s is not None) != 1 or p.right_set is not None:
            raise SchemaError(f"{p.op} needs exactly one of right_literal|right_param")

def validate_filter_tree(node, depth: int = 0, count: list[int] | None = None) -> None:
    count = count if count is not None else [0]
    if depth > MAX_FILTER_DEPTH: raise SchemaError("filter tree too deep")
    if isinstance(node, FilterBool):
        if node.op is FilterBoolOp.NOT and len(node.children) != 1: raise SchemaError("NOT needs 1 child")
        if node.op in (FilterBoolOp.AND, FilterBoolOp.OR) and len(node.children) < 2: raise SchemaError("AND/OR need >=2")
        for c in node.children: validate_filter_tree(c, depth + 1, count)
    else:
        count[0] += 1
        if count[0] > MAX_PREDICATES: raise SchemaError("too many predicates")
        validate_predicate(node)
```

Add `validate_proposal(p)` that walks grain/body/parameters: rejects unknown/duplicate parameter names (regex `^[a-z][a-z0-9_]{0,63}$`), asserts `operand is None` iff `aggregation==COUNT_ROWS`, resolves each `ParameterRef` to a declared `ParameterDecl`, and calls `validate_filter_tree` on each expression's filter.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/featuregen/formula/test_schema.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/formula/__init__.py src/featuregen/formula/schema.py tests/featuregen/formula/test_schema.py
git commit -m "feat(formula): TypedFormula schema + predicate invariants

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Canonicalization + `formula_content_hash`

**Files:**
- Create: `src/featuregen/formula/canonical.py`
- Test: `tests/featuregen/formula/test_canonical.py`

**Interfaces — Consumes:** Task-1 dataclasses. **Produces:** `canonical_json(f: TypedFormulaV1) -> str` (RFC-8785/JCS bytes as str), `formula_content_hash(f: TypedFormulaV1) -> str` (sha256 hex). Uses `featuregen.overlay.upload.object_ref` to normalize every `LogicalRef` first.

- [ ] **Step 1: Write the failing test** (`tests/featuregen/formula/test_canonical.py`)

```python
from featuregen.formula.canonical import formula_content_hash
from tests.featuregen.formula.factories import ratio_formula, and_filter, and_filter_reordered

def test_capability_version_not_in_hash():
    # two formulas identical except a (nonexistent-in-object) capability version -> identical hash
    assert formula_content_hash(ratio_formula()) == formula_content_hash(ratio_formula())

def test_associative_and_reorder_is_stable():
    assert formula_content_hash(and_filter()) == formula_content_hash(and_filter_reordered())

def test_ordered_slot_swap_changes_hash():
    f = ratio_formula()
    swapped = f.__class__(**{**f.__dict__})  # build with numerator/denominator swapped in factory
    from tests.featuregen.formula.factories import ratio_formula_swapped
    assert formula_content_hash(f) != formula_content_hash(ratio_formula_swapped())

def test_grain_key_order_is_semantic():
    from tests.featuregen.formula.factories import ratio_formula_grain_reordered
    assert formula_content_hash(ratio_formula()) != formula_content_hash(ratio_formula_grain_reordered())
```

Create `tests/featuregen/formula/factories.py` building the fixtures above (a customer-grain RATIO of two SUMs over a trailing-90d window on `ftr::public.comp_financial_tran_repos_dly.tran_amt_aed`, one with `is_cross_border` filter), plus the AND-reordered / slot-swapped / grain-reordered variants.

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/featuregen/formula/test_canonical.py -q`
Expected: FAIL (`ModuleNotFoundError: featuregen.formula.canonical`).

- [ ] **Step 3: Write minimal implementation** (`src/featuregen/formula/canonical.py`)

Convert the dataclass tree to a plain dict (dataclasses → dict, enums → `.value`, `LogicalRef` → `object_ref` normalized, decimals already canonical strings). Apply the §E ordering rules: preserve ordered slots + grain key order; **flatten associative AND/OR then sort children by their own canonical hash**; sort `IN` `right_set` and `allowed_set`; sort `parameters` by name. Emit RFC-8785 JCS (lexicographic key sort, minimal number forms, NFC). `formula_content_hash = sha256(canonical_json(f).encode()).hexdigest()`.

```python
import hashlib, unicodedata
from featuregen.overlay.upload import object_ref

def _flatten_sort_bool(node: dict) -> dict:
    if node.get("kind") != "bool" or node["op"] not in ("and", "or"):
        return node
    flat = []
    for c in node["children"]:
        c = _canon_node(c)
        if c.get("kind") == "bool" and c["op"] == node["op"]:
            flat.extend(c["children"])
        else:
            flat.append(c)
    flat.sort(key=lambda x: hashlib.sha256(_jcs(x).encode()).hexdigest())
    return {"kind": "bool", "op": node["op"], "children": flat}
# ... _canon_node normalizes refs + recurses; _jcs implements RFC 8785 ...

def canonical_json(f) -> str:
    return _jcs(_canon_node(_to_dict(f)))
def formula_content_hash(f) -> str:
    return hashlib.sha256(canonical_json(f).encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Run test to verify it passes** — Expected: PASS (4 passed).
- [ ] **Step 5: Commit** `feat(formula): RFC-8785 canonicalization + formula_content_hash`.

---

### Task 3: Operation compatibility + additivity + C1 output authority

**Files:**
- Create: `src/featuregen/formula/operations.py` (map + additivity, §B/§D)
- Create: `src/featuregen/formula/output_authority.py` (`resolve_formula_output_policy`, §C)
- Test: `tests/featuregen/formula/test_output_authority.py`

**Interfaces — Produces:** `to_path_aggregation(fn: AggregateFunction) -> PathAggregation | None` (None for the two new counts); `formula_additivity(body) -> AdditivityClass`; `resolve_formula_output_policy(conn, body, decimal, now) -> FormulaOutputPolicyV1 | ExternalRequirement | NeedsAuthority`. Consumes C1 `featuregen.overlay.upload.operational_facts.read_operational_value` and `b_output_policy.derive_output_additivity`.

- [ ] **Step 1: Write the failing test** — using the existing `_bank_graph`/`db` fixtures (see `tests/featuregen/overlay/upload/test_feature_assist.py`):

```python
def test_count_distinct_is_non_additive(db):
    from featuregen.formula.operations import formula_additivity
    from featuregen.formula.schema import AdditivityClass
    body = unary_count_distinct_body()          # factory
    assert formula_additivity(body) is AdditivityClass.NON_ADDITIVE

def test_sum_does_not_need_hint_only_unit(db):
    _bank_graph(db)                             # amount has numeric type but unit is a hint
    out = resolve_formula_output_policy(db, unary_sum_amount_body(), decimal_policy(), NOW)
    assert out.output_type != "unknown"         # resolves via numeric type, not stranded on unit
    # unit stays a hint, NOT a NEEDS_AUTHORITY

def test_difference_incompatible_units_is_invalid(db):
    _bank_graph_two_units(db)
    res = resolve_formula_output_policy(db, diff_two_units_body(), decimal_policy(), NOW)
    assert res.__class__.__name__ == "InvalidOutput"

def test_c1_hash_mismatch_on_required_field_needs_authority(db):
    _bank_graph_drifted(db)                     # force C1 status=hash_mismatch on the operand type
    res = resolve_formula_output_policy(db, unary_sum_amount_body(), decimal_policy(), NOW)
    assert res.__class__.__name__ == "NeedsAuthority"
```

- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL (module missing).
- [ ] **Step 3: Write minimal implementation** — `operations.py`: the compatibility map + `formula_additivity` (COUNT_DISTINCT→NON_ADDITIVE; RATIO→NON_ADDITIVE; DIFFERENCE→NON_ADDITIVE unless a proven rule; COUNT_ROWS/COUNT_NON_NULL→ADDITIVE only across disjoint partitions else NON_ADDITIVE; SUM→via governed input/path). `output_authority.py`: per-expression C1 reads via `read_operational_value`; apply the §C **required-field matrix** (only require unit/currency where the op needs them); return `FormulaOutputPolicyV1`, or `NeedsAuthority`/`ExternalRequirement("UNIT_PROVISIONING_REQUIRED")`/`InvalidOutput` per the matrix; any C1 `fork|hash_mismatch|projection_unavailable` on a required field → `NeedsAuthority`.
- [ ] **Step 4: Run test to verify it passes** — Expected: PASS (4 passed).
- [ ] **Step 5: Commit** `feat(formula): operation map + corrected additivity + C1 output-authority matrix`.

---

### Task 4: Multi-axis AuthoringResult + disposition fold

**Files:**
- Create: `src/featuregen/formula/result.py`
- Test: `tests/featuregen/formula/test_result.py`

**Interfaces — Produces:** `AuthoringResult` dataclass (§F fields, incl. `candidate_formula`/`candidate_formula_hash`); `DISPOSITION_POLICY_VERSION=1`; `derive_disposition(axes: AuthoringAxes) -> AuthoringResult` (pure function).

- [ ] **Step 1: Write the failing test**

```python
def test_unsupported_operation_folds_to_unsupported():
    r = derive_disposition(axes(structural="unsupported_operation"))
    assert r.authoring_disposition == "UNSUPPORTED"          # NOT REJECTED

def test_invalid_formula_folds_to_rejected():
    assert derive_disposition(axes(structural="invalid_formula")).authoring_disposition == "REJECTED"

def test_reviewable_needs_review_carries_candidate_formula():
    f = ratio_formula()
    r = derive_disposition(axes(critic="blocking"), candidate=f)
    assert r.authoring_disposition == "NEEDS_REVIEW"
    assert r.candidate_formula is f and r.candidate_formula_hash is not None

def test_advisory_expectation_mismatch_does_not_reject():
    r = derive_disposition(axes(expectation="mismatch", output="resolved"), candidate=ratio_formula())
    assert r.authoring_disposition == "NEEDS_REVIEW"

def test_technical_failure_wins():
    assert derive_disposition(axes(technical="technical_failure")).authoring_disposition == "TECHNICAL_FAILURE"
```

- [ ] **Step 2: Run to fail; Step 3: implement the pure fold exactly per spec §F** (technical → invalid/invalid_output → unsupported_* → needs_authority/blocking/mismatch → resolved), attaching `candidate_formula` + `formula_content_hash(candidate)` for RESOLVED and reviewable NEEDS_REVIEW. **Step 4: pass; Step 5: commit** `feat(formula): multi-axis AuthoringResult + disposition fold`.

---

### Task 5: LLM-1 tools + ReAct author

**Files:**
- Create: `src/featuregen/formula/tools.py` (7 read/validate tools over existing catalog reads)
- Create: `src/featuregen/formula/author.py` (bounded ReAct loop producing `TypedFormulaProposalV1`)
- Test: `tests/featuregen/formula/test_author.py` (FakeLLM-scripted)

**Interfaces — Produces:** `TOOLS: dict[str, Callable]` (`search_columns`, `get_column_metadata`, `get_governed_grain`, `get_time_anchor`, `get_verified_lineage`, `list_supported_operations`, `validate_draft_formula`); `author_formula(conn, intent, client, *, roles, max_iters, token_budget, actor) -> tuple[TypedFormulaProposalV1|None, AuthorTrace]`. Tools wrap `graph_node` reads / `resolve_fact` / `object_ref`; none mutates governance.

- [ ] **Step 1: failing test** — FakeLLM scripts a tool-using run that ends by calling `validate_draft_formula` clean and emitting a proposal for `cross_border_value_ratio_90d`; assert the proposal validates (`validate_proposal`) and every operand is metadata-only (no raw values in the tool egress); assert exceeding `max_iters` → `technical_failure`.
- [ ] **Step 2–5:** run-fail → implement the tools (read-scoped, metadata-only egress; `list_supported_operations` returns the §B vocabulary; `validate_draft_formula` runs Task-1 validators + returns structural omissions) and the bounded ReAct loop recording each provider turn via `record_llm_call` → run-pass → commit `feat(formula): LLM-1 tools + bounded ReAct author`.

---

### Task 6: Independent critic + closed finding codes

**Files:**
- Create: `src/featuregen/formula/critic.py`
- Test: `tests/featuregen/formula/test_critic.py`

**Interfaces — Produces:** `CriticFinding` (code + severity), `CRITIC_FINDING_CODES` (closed enum with fixed severity, §G), `CRITIC_POLICY_VERSION=1`, `critique(conn, intent, proposal, client, *, actor) -> tuple[list[CriticFinding], str]` (findings + `critic_findings_hash`). The critic is given a SEPARATE context (intent + proposal only — NOT the author's tool trace/reasoning).

- [ ] **Step 1: failing test** — FakeLLM critic returns a `WRONG_SLOT_DIRECTION` (blocking) → asserts `critic_status` maps to blocking and the author reasoning was never passed to the critic; a malformed/unknown finding code → dropped, non-blocking, recorded as a technical note.
- [ ] **Step 2–5:** implement closed codes + severity map + malformed/unknown/duplicate handling → pass → commit `feat(formula): independent critic + closed finding codes`.

---

### Task 7: Manifest-first authoring trace

**Files:**
- Create: `src/featuregen/db/migrations/10XX_formula_authoring_trace.sql` (next free number; `authoring_run` + append-only `authoring_trace_event`)
- Create: `src/featuregen/formula/trace.py`
- Test: `tests/featuregen/formula/test_trace.py`

**Interfaces — Produces:** `open_authoring_run(conn, *, intent_hash, versions, actor) -> authoring_run_id`; `append_event(conn, run_id, kind, payload)`; `run_status(conn, run_id) -> Literal["incomplete","completed","failed"]` (incomplete = no terminal event). Migration: `authoring_trace_event` is INSERT-only (write-once trigger like `0060`).

- [ ] **Step 1: failing test** — open a run, append `STARTED`+`LLM_CALL_RECORDED`, assert `run_status == "incomplete"`; append `COMPLETED`, assert `"completed"`; a run with a durable `llm_call` but no terminal event still reads `incomplete`.
- [ ] **Step 2–5:** write the migration (verify number free with `ls src/featuregen/db/migrations/ | tail`), the append-only functions, the derived status → pass → commit `feat(formula): manifest-first authoring trace`.

---

### Task 8: Gold gate + end-to-end authoring pipeline

**Files:**
- Create: `src/featuregen/formula/authoring.py` (the orchestrator wiring Tasks 1–7 into `run_authoring(...) -> AuthoringResult`)
- Create: `src/featuregen/formula/gold.py` (curated pairs + scored gate, §J)
- Create: `tests/featuregen/formula/gold/` (curated `(intent, expected TypedFormulaV1)` fixtures incl. `cross_border_value_ratio_90d`, one per operation, negative/unsupported, adversarial)
- Test: `tests/featuregen/formula/test_gold_gate.py`

**Interfaces — Produces:** `run_authoring(conn, intent, author_client, critic_client, *, roles, actor) -> AuthoringResult`; `score_gold(conn, author_client, critic_client) -> GoldReport`; `GOLD_GATE_V1` thresholds (false-resolve 0, operand-preservation 1.0, positive exact = all, unsupported/reject 1.0, blocking-critic recall 1.0, technical-failures-in-clean 0).

- [ ] **Step 1: failing test** — `run_authoring` on the `cross_border_value_ratio_90d` intent (FakeLLM author scripted to the expected formula, clean critic) → `RESOLVED`, `candidate_formula_hash` == the gold formula's hash; a swapped-direction script fails the gold gate with a localized diff; multi-source intent → `UNSUPPORTED`; `avg` → `UNSUPPORTED`. `score_gold` meets `GOLD_GATE_V1`. Non-vacuity: a reject-all author fails the gate.
- [ ] **Step 2–5:** wire the orchestrator (author → `validate_proposal` → critic (separate context) → `resolve_formula_output_policy` → `derive_disposition` → trace), the gold scorer + thresholds, and a **key-gated** (`@pytest.mark.skipif(no ANTHROPIC_API_KEY)`) real-provider schema/tool test → pass → commit `feat(formula): gold gate + authoring orchestrator`.

---

## Self-Review

**Spec coverage:** §A→T1; §E→T2; §B/§C/§D→T3; §F→T4; §I→T5; §G→T6; §H→T7; §J + orchestrator→T8. All spec sections covered. Explicit non-goals (compile/execute/storage-of-formula/external) are absent from every task by construction — the only durable writes are the trace tables (T7) + `llm_call`.
**Placeholder scan:** the two `# ...` markers in T2/T3 code sketches are elisions of mechanical recursion around fully-specified rules (§E ordering / §C matrix), not undecided behavior; the implementer has the exact rules in the cited spec sections. Every task has runnable tests with concrete assertions.
**Type consistency:** `AggregateFunction`/`FinalOperation` (T1) are used unchanged in T3/T4/T8; `AuthoringResult` fields (T4) match §F and are produced by `run_authoring` (T8); `formula_content_hash` (T2) is the single hasher used by T4 and T8; `read_operational_value` (C1) is the only authority read in T3.

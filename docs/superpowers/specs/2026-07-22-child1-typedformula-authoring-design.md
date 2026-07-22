# Child Spec 1 — TypedFormula Authoring (shadow, no execution) — rev 2

**Parent:** `2026-07-22-feature-materialization-pipeline-design.md` (Program #1). **Findings:** #4, #13 (parent) + 15 Child-1 review findings, resolved inline as `[cN]`.

**Goal:** Publish the **normative, closed** `TypedFormulaProposalV1` / `TypedFormulaV1` schemas, their closed enums, exact canonicalization + `formula_content_hash`, a **new** formula operation contract + a **new** formula-level output resolver (the existing `b_operation`/`b_output_policy` do not cover this), and the offline authoring loop (author → structural-validate → independent critic → C1-authoritative output resolution → gold gate) with a **multi-axis result** and deterministic disposition. **No execution, no storage of a formula/version artifact** — only append-only `llm_call` + authoring-trace records.

**Verified adapter facts (drive this rev):** `SupportedOperation` is single-operand (`b_operation.py:54`); `OutputPolicyV1` = `{output_type, output_additivity, external_type_required}`, no unit/currency, per-operand (`b_output_policy.py:71`); C1 `read_operational_value` carries `resolved/not_operational/projection_unavailable/fork/hash_mismatch` (`operational_facts.py`) which `b_output_policy` does not fully consume. → Child-1 defines a **new** operation contract + a **new** C1-based formula resolver, with a compatibility mapping to the old ones. `[c2][c3][c4]`

---

## §A Normative schemas (frozen, slotted dataclasses; JSON is their canonical serialization)

Every object is closed: `additionalProperties: false`, unknown fields → `INVALID_FORMULA`. `LogicalRef = str` (canonical `source::schema.table.column`, normalized before hashing).

```python
# ---- enums (closed) --------------------------------------------------------
class FormulaOperation(StrEnum):           # §B vocabulary
    SUM=…; COUNT_ROWS=…; COUNT_NON_NULL=…; COUNT_DISTINCT=…; RATIO=…; DIFFERENCE=…
class WindowBasis(StrEnum):     TRAILING=…; CALENDAR_PERIOD=…
class WindowUnit(StrEnum):      DAY=…; WEEK=…; MONTH=…; QUARTER=…; YEAR=…
class Inclusivity(StrEnum):     INCLUSIVE=…; EXCLUSIVE=…
class EmptyWindowResult(StrEnum): NULL=…; ZERO=…; ERROR=…
class NullInput(StrEnum):       IGNORE=…; PROPAGATE=…; ZERO=…
class ZeroDenominator(StrEnum): NULL=…; ZERO=…; ERROR=…
class RoundingMode(StrEnum):    HALF_UP=…; HALF_EVEN=…; DOWN=…; UP=…; FLOOR=…; CEILING=…
class OverflowBehavior(StrEnum):ERROR=…; SATURATE=…
class LiteralType(StrEnum):     STRING=…; INTEGER=…; DECIMAL=…; BOOLEAN=…; DATE=…
class ParamClass(StrEnum):      SEMANTIC=…; OPERATIONAL=…
class FilterBoolOp(StrEnum):    AND=…; OR=…; NOT=…
class FilterPredicateOp(StrEnum): EQUAL=…; NOT_EQUAL=…; GREATER_THAN=…; GREATER_OR_EQUAL=…; \
                                  LESS_THAN=…; LESS_OR_EQUAL=…; IN=…; NOT_IN=…; IS_NULL=…; IS_NOT_NULL=…

# ---- leaves ----------------------------------------------------------------
@frozen class TypedLiteral:  type: LiteralType; value: str      # value ALWAYS a canonical string (decimals too)
@frozen class ParameterDecl: name: str; type: LiteralType; param_class: ParamClass; classification: str; \
                             nullable: bool; allowed_set: tuple[str,...] | None; \
                             allowed_min: str | None; allowed_max: str | None      # [c14]
@frozen class ParameterRef:  name: str                          # resolves to a declared ParameterDecl

# ---- filter AST (recursive union; leaves reference one LogicalRef + a literal/param) -------------
@frozen class FilterPredicate: op: FilterPredicateOp; left: LogicalRef; \
                               right_literal: TypedLiteral | None; right_param: ParameterRef | None; \
                               right_set: tuple[TypedLiteral,...] | None            # IN/NOT_IN only
@frozen class FilterBool:      op: FilterBoolOp; children: tuple["FilterNode",...]  # NOT → exactly 1 child
FilterNode = FilterPredicate | FilterBool
#   HARD LIMITS (schema constants): MAX_FILTER_DEPTH=4, MAX_PREDICATES=16, MAX_IN_LIST=64  [c1]

# ---- source + grain --------------------------------------------------------
@frozen class SourceRelation: catalog_source: str; logical_table: str  # the governed counted/aggregated rowset [c6]
@frozen class Grain:          entity: str; keys: tuple[LogicalRef,...]  # excludes business_dt (always implied)

# ---- windowed aggregate expression (an operand slot) -----------------------
@frozen class AggregateExpression:
    expression_id: str                     # UNIQUE within a formula; distinct expressions may share a LogicalRef [c5]
    aggregation: FormulaOperation          # one of SUM/COUNT_ROWS/COUNT_NON_NULL/COUNT_DISTINCT
    operand: LogicalRef | None             # None only for COUNT_ROWS
    source_relation: SourceRelation        # identity-bearing; required even for COUNT_ROWS [c6]
    filter: FilterNode | None
    window: "WindowPolicy"

@frozen class WindowPolicy:
    basis: WindowBasis; length: int; unit: WindowUnit
    start_inclusive: Inclusivity; end_inclusive: Inclusivity
    timezone: str                          # IANA tz for calendar/cutoff resolution
    empty_window: EmptyWindowResult; null_input: NullInput

@frozen class DecimalPolicy: precision: int; scale: int; rounding: RoundingMode; overflow: OverflowBehavior

# ---- the operation body (ordered slots) ------------------------------------
@frozen class UnaryOp:  operation: FormulaOperation; expr: AggregateExpression                 # SUM/COUNT_*
@frozen class RatioOp:  operation: FormulaOperation; numerator: AggregateExpression; \
                        denominator: AggregateExpression; zero_denominator: ZeroDenominator     # ordered [c5]
@frozen class DiffOp:   operation: FormulaOperation; minuend: AggregateExpression; \
                        subtrahend: AggregateExpression                                          # ordered
OperationBody = UnaryOp | RatioOp | DiffOp

# ---- the two top-level objects --------------------------------------------
@frozen class ExpectedOutput:    output_type: str | None; unit: str | None; currency: str | None  # advisory [c9]
@frozen class TypedFormulaProposalV1:                       # what LLM-1 authors
    formula_schema_version: int; operation_grammar_version: int; canonicalization_version: int
    grain: Grain; body: OperationBody; parameters: tuple[ParameterDecl,...]
    decimal: DecimalPolicy; expected_output: ExpectedOutput | None

@frozen class FormulaOutputPolicyV1:                        # NEW formula-level resolver output [c3]
    output_type: str; unit: str | None; currency: str | None
    output_additivity: AdditivityClass; external_type_required: bool
@frozen class TypedFormulaV1:                               # AUTHORITATIVE (identity object)
    formula_schema_version: int; operation_grammar_version: int
    output_policy_version: int; canonicalization_version: int   # IDENTITY-bearing versions ONLY [c7]
    grain: Grain; body: OperationBody; parameters: tuple[ParameterDecl,...]
    decimal: DecimalPolicy; output: FormulaOutputPolicyV1
    # NOTE: NO capability_policy_version, NO feature/contract/call ids, NO timestamps here. [c7]
```

## §B Operation vocabulary + compatibility matrix `[c2]`

New `FormulaOperation` contract; explicit mapping to `b_operation` (do NOT widen `SupportedOperation` in place):

| FormulaOperation | slots | b_operation compatibility |
|---|---|---|
| `SUM` | `expr(operand)` | maps → `PathAggregation.sum` |
| `COUNT_ROWS` | — (needs `source_relation`) | **new** — b_operation has no rows-count |
| `COUNT_NON_NULL` | `expr(operand)` | **new** — splits b_operation's generic `count` |
| `COUNT_DISTINCT` | `expr(operand)` | maps → `PathAggregation.count_distinct` |
| `RATIO` | `numerator`,`denominator` | **new** ordered op (b_operation *defers* ratio) |
| `DIFFERENCE` | `minuend`,`subtrahend` | **new** ordered op (b_operation *defers* difference) |

`min`/`max`/`avg`/`stddev` are **out** of Child-1; derived-temporal (`trend`/`velocity`/`growth`/`zscore`) are **out of vocabulary but shape-accommodated** at a later `operation_grammar_version`. A trailing-window `SUM` is in-vocabulary (window is a first-class `WindowPolicy`, not a windowed *op*).

## §C Output resolution — a NEW formula-level resolver over C1 `[c3][c4]`

`b_output_policy.OutputPolicyV1` is per-operand and lacks unit/currency; it cannot resolve a ratio/difference. Define:

```
resolve_formula_output_policy(conn, body, decimal_policy, now) -> FormulaOutputPolicyV1
```
- For each `AggregateExpression`, read **operand type/unit/currency/additivity via C1 `read_operational_value`** (NOT `read_column_facts`); any `not_operational | projection_unavailable | fork | hash_mismatch` → **fail closed → `NEEDS_AUTHORITY`**. `[c4]`
- Derive the **final** output: `RATIO` → dimensionless (unit/currency cancel iff numerator/denominator units match, else `NEEDS_AUTHORITY`); `DIFFERENCE`/`SUM` → operand unit/currency (mismatch across ordered slots → `INVALID_FORMULA`); additivity via the existing `derive_output_additivity` per expression then combined by operation. Reuse `resolve_output_policy` per operand where useful, but final type/unit/currency/additivity are derived here.
- The LLM's `expected_output` is **advisory only** and never sets authoritative fields (§F).

## §D Canonicalization + hashing (exact) `[c7][c13]`

Canonical JSON: UTF-8, **sorted object keys**, no insignificant whitespace, exact enum casing, decimals as canonical strings (never binary floats), `LogicalRef` normalized first, unknown fields rejected, **duplicate `expression_id` rejected** (repeated `LogicalRef` across distinct slots/expressions is allowed `[c5]`). **Order rules:** ordered slots (numerator/denominator, minuend/subtrahend) preserved; **commutative `AND`/`OR` children sorted** by canonical child hash before serialization `[c13]`; `parameters` sorted by `name`; `IN` sets sorted+deduplicated.

```
formula_content_hash = sha256(canonical_json(TypedFormulaV1))
```
Covers `TypedFormulaV1` material ONLY — identity-bearing versions included; **`formula_capability_policy_version` and all outcome/provenance are OUTSIDE** the object and the hash `[c7]`. A capability-policy bump therefore cannot change the hash; a grammar/operand/output-policy/canonicalization change does.

## §E Authoring result — multi-axis, not one verdict `[c9][c10]`

```python
@frozen class AuthoringResult:
    structural_status: Literal["ok","invalid_formula","unsupported_operation"]
    capability_status: Literal["ok","unsupported_capability"]      # multi-source off in v1
    output_status:     Literal["resolved","needs_authority","invalid_output"]
    expectation_status:Literal["match","mismatch","not_provided"]  # advisory expected_output vs resolved [c9]
    critic_status:     Literal["clean","advisory","blocking"]      # §G [c10]
    technical_status:  Literal["ok","technical_failure"]           # LLM/tool/infra — never a semantic verdict [c11]
    authoring_disposition: Literal["RESOLVED","NEEDS_REVIEW","REJECTED","UNSUPPORTED","TECHNICAL_FAILURE"]
    disposition_policy_version: int
```
`authoring_disposition` is a **pure function** of the axes (pinned by `disposition_policy_version`): any `technical_status=technical_failure` → `TECHNICAL_FAILURE`; `structural=invalid_formula|unsupported_operation` OR `output=invalid_output` → `REJECTED`; `capability=unsupported_capability` → `UNSUPPORTED`; `output=needs_authority` OR `critic=blocking` OR `expectation=mismatch` → `NEEDS_REVIEW`; else → `RESOLVED`. An **advisory** expectation mismatch never makes a valid authoritative formula `REJECTED` `[c9]`.

## §F Expected-output disposition `[c9]`

`NEEDS_AUTHORITY` (authoritative output unresolvable from C1) ≠ `EXPECTATION_MISMATCH` (authoritative output resolved but differs from the LLM's advisory expectation → `expectation_status=mismatch` → `NEEDS_REVIEW`) ≠ `INVALID_FORMULA` (the formula semantics are themselves invalid). The authoritative `output` always comes from §C, never the proposal.

## §G Independent critic → closed findings → disposition `[c10]`

Critic (separate context/tier, no LLM-1 reasoning/trace, structured findings only) emits from a **closed finding-code enum** (e.g. `MISSING_OPERAND`, `WRONG_SLOT_DIRECTION`, `FILTER_MISMATCH`, `WINDOW_MISMATCH`, `INTENT_UNMET`), each classified **blocking | advisory** by a `critic_policy_version`. A **blocking** finding sets `critic_status=blocking` → `NEEDS_REVIEW` and **prevents an auto-`RESOLVED`** — it does not itself mutate the formula. `critic_findings_hash` is recorded.

## §H LLM audit + trace — corrected `[c11][c12][c15]`

- **A ReAct author run is MANY provider calls**, each an immutable `llm_call` record. The trace carries `author_call_ids[]`, `critic_call_ids[]`, and **ordered steps** `{llm_call | tool_call | tool_result}`. `[c12]`
- Tool results stored as **canonical redacted result + its hash** (hash-only is allowed only when it references another immutable, retrievable artifact). `[c12]`
- **Replay is precisely scoped** `[c11]`: guaranteed = exact reconstruction of each original call, the stored raw output, deterministic **re-validation** of that stored output, and a new replay linked to the original. NOT guaranteed = re-authoring reproduces the same formula (LLMs are stochastic).
- **Storage boundary** `[c15]`: Child-1 writes NO durable formula/version artifact, but DOES write append-only `llm_call` + one authoring-trace per `authoring_run_id`. If a durable `llm_call` exists but the final trace write fails → the trace is marked `incomplete` for that `authoring_run_id` (append-only; never leaves an orphan pretending success); the run is `TECHNICAL_FAILURE`.

## §I LLM-1 tools (governed catalog-authoring API)

Seven read/validate-only tools (none approves/executes/mutates governance): `search_columns`, `get_column_metadata`, `get_governed_grain`, `get_time_anchor`, `get_verified_lineage`, `list_supported_operations`, `validate_draft_formula`. Read-scoped; metadata-only egress; tool results are data not instructions (prompt-injection); versions stamped (schema/grammar/model/prompt); bounded ReAct iterations + token/cost budget (exceed → `technical_failure`); coercion telemetry captured.

## §J Gold gate `[c13]`

`(intent, expected TypedFormulaV1)` pairs. Compare by **canonical structural equality** (`formula_content_hash` after the §D commutative-normalization; ordered slots preserved). The gate reports, per operation: **positive shapes** (≥1 each of SUM/COUNT_*/RATIO/DIFFERENCE), **negative/unsupported** cases (multi-source→UNSUPPORTED, avg→UNSUPPORTED_OPERATION, over-deep filter→INVALID), **critic-miss adversarial** cases, plus **false-resolve rate** and **operand-preservation rate** against pinned thresholds. B-Gate-1-style non-vacuity. Includes a **key-gated real-provider** schema/tool integration test (FakeLLM proves plumbing only).

## §K Testing

Canonicalization stability + hash invariance (capability bump ≠ hash change; grammar/operand/output change = hash change; commutative AND reorder = same hash; ordered-slot swap = different hash); duplicate `expression_id` rejected but shared `LogicalRef` across slots accepted (the ratio example); unknown field / over-limit filter → `INVALID_FORMULA`; multi-source → `UNSUPPORTED_CAPABILITY`; avg → `UNSUPPORTED_OPERATION`; C1 `hash_mismatch`/`fork`/`projection_unavailable` on an operand → `NEEDS_AUTHORITY` (not silent clear); advisory expectation mismatch → `NEEDS_REVIEW`, formula still authoritative; blocking critic finding → `NEEDS_REVIEW`, no auto-RESOLVED; param type-checks against its compared column/literal `[c14]`; multi-call ReAct trace records all `llm_call` ids + ordered steps; trace-write-failure → `incomplete` + `TECHNICAL_FAILURE`; gold gate thresholds per operation. Offline (FakeLLM) except the one key-gated provider test.

## §L Deliverable boundary

Output: an `AuthoringResult` + (when `RESOLVED`) an authoritative `TypedFormulaV1` + `formula_content_hash` + the `authoring_run_id` trace + gold score. **Not** frozen into `feature_versions` (Child #2), **not** compiled (#3), **not** executed (#5).

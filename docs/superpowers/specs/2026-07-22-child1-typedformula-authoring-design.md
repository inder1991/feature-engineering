# Child Spec 1 — TypedFormula Authoring (shadow, no execution) — rev 3

**Parent:** `2026-07-22-feature-materialization-pipeline-design.md` (Program #1). Findings from three review rounds resolved inline as `[cN]`.

**Goal:** Freeze the normative, closed `TypedFormulaProposalV1` / `TypedFormulaV1` contract (exact enums, discriminated JSON unions, predicate invariants, RFC-8785 canonicalization, path-derived hashing), an operation-specific output-authority matrix over C1, a new operation/additivity contract, and the offline author→structural-validate→independent-critic→C1-resolve→gold loop with a multi-axis result. **No execution; no durable formula/version artifact.**

**Verified adapter facts:** `SupportedOperation` is single-operand (`b_operation.py:54`); `OutputPolicyV1={output_type,output_additivity,external_type_required}` per-operand (`b_output_policy.py:71`); **C1 governs `unit`/`currency` as HINTS → `not_operational`** (`operational_facts.py:45`); **`derive_output_additivity` wrongly marks `count_distinct` additive** (`b_output_policy.py:126`). → Child-1 defines new contracts + an operation-specific authority matrix + a corrected additivity rule.

---

## §A Normative schemas (frozen slotted dataclasses; JSON = canonical serialization; every object `additionalProperties:false`)

```python
LogicalRef = str  # canonical "source::schema.table[.column]" (object_ref.py:23), normalized before hashing

# ---- enums (exact string values are the serialized form; casing fixed by canonicalization_version) ----
class AggregateFunction(StrEnum):   # [c3] the per-expression aggregate ONLY
    SUM="sum"; COUNT_ROWS="count_rows"; COUNT_NON_NULL="count_non_null"; COUNT_DISTINCT="count_distinct"
class FinalOperation(StrEnum):      # [c3] the formula body shape ONLY
    IDENTITY="identity"; RATIO="ratio"; DIFFERENCE="difference"
class WindowBasis(StrEnum):     TRAILING="trailing"; CALENDAR_PERIOD="calendar_period"
class WindowUnit(StrEnum):      DAY="day"; WEEK="week"; MONTH="month"; QUARTER="quarter"; YEAR="year"
class Inclusivity(StrEnum):     INCLUSIVE="inclusive"; EXCLUSIVE="exclusive"
class EmptyWindowResult(StrEnum): NULL="null"; ZERO="zero"; ERROR="error"
class NullInput(StrEnum):       IGNORE="ignore"; PROPAGATE="propagate"; ZERO="zero"
class ZeroDenominator(StrEnum): NULL="null"; ZERO="zero"; ERROR="error"
class RoundingMode(StrEnum):    HALF_UP="half_up"; HALF_EVEN="half_even"; DOWN="down"; UP="up"; FLOOR="floor"; CEILING="ceiling"
class OverflowBehavior(StrEnum):ERROR="error"; SATURATE="saturate"
class LiteralType(StrEnum):     STRING="string"; INTEGER="integer"; DECIMAL="decimal"; BOOLEAN="boolean"; DATE="date"
class ParamClass(StrEnum):      SEMANTIC="semantic"; OPERATIONAL="operational"
class FilterKind(StrEnum):      BOOL="bool"; PREDICATE="predicate"   # JSON discriminator [c9]
class FilterBoolOp(StrEnum):    AND="and"; OR="or"; NOT="not"
class FilterPredicateOp(StrEnum):
    EQUAL="equal"; NOT_EQUAL="not_equal"; GREATER_THAN="greater_than"; GREATER_OR_EQUAL="greater_or_equal"
    LESS_THAN="less_than"; LESS_OR_EQUAL="less_or_equal"; IN="in"; NOT_IN="not_in"; IS_NULL="is_null"; IS_NOT_NULL="is_not_null"
class AdditivityClass(StrEnum): ADDITIVE="additive"; NON_ADDITIVE="non_additive"; SEMI_ADDITIVE="semi_additive"

# ---- leaves ----
@frozen class TypedLiteral:  type: LiteralType; value: str                # value ALWAYS canonical string
@frozen class ParameterDecl: name: str; type: LiteralType; param_class: ParamClass; classification: str
                             nullable: bool; allowed_set: tuple[str,...] | None
                             allowed_min: str | None; allowed_max: str | None      # [c14]  name matches /^[a-z][a-z0-9_]{0,63}$/, UNIQUE
@frozen class ParameterRef:  name: str

# ---- filter AST (discriminated union on `kind`) [c9] ----
@frozen class FilterPredicate:   # kind="predicate"
    op: FilterPredicateOp; left: LogicalRef
    right_literal: TypedLiteral | None; right_param: ParameterRef | None; right_set: tuple[TypedLiteral,...] | None
@frozen class FilterBool:        # kind="bool"
    op: FilterBoolOp; children: tuple["FilterNode",...]
FilterNode = FilterPredicate | FilterBool   # serialized with an explicit "kind" field
# PREDICATE INVARIANTS (validator) [c9]:
#   IS_NULL/IS_NOT_NULL -> right_literal=right_param=right_set=None
#   IN/NOT_IN           -> exactly right_set (non-empty, ≤ MAX_IN_LIST), sorted+deduped
#   all other ops       -> exactly ONE of right_literal | right_param
#   NOT (bool)          -> exactly 1 child;  AND/OR -> ≥2 children
#   right_param.name must resolve to a declared ParameterDecl
# HARD LIMITS (schema constants): MAX_FILTER_DEPTH=4, MAX_PREDICATES=16, MAX_IN_LIST=64

# ---- source, grain, window ----
@frozen class SourceRelation: table_ref: LogicalRef   # a TABLE logical_ref (no .column); source is implicit in it [c8]
@frozen class Grain:          entity: str; keys: tuple[LogicalRef,...]   # ORDER IS SEMANTIC (§D); excludes business_dt
@frozen class WindowPolicy:
    event_time_ref: LogicalRef                          # [c1] the column ordering the window (identity-bearing)
    basis: WindowBasis; length: int; unit: WindowUnit
    start_inclusive: Inclusivity; end_inclusive: Inclusivity; timezone: str
    empty_window: EmptyWindowResult; null_input: NullInput
@frozen class DecimalPolicy: precision: int; scale: int; rounding: RoundingMode; overflow: OverflowBehavior

# ---- aggregate expression (an operand slot). NO expression_id — internal id is its canonical PATH [c4] ----
@frozen class AggregateExpression:
    aggregation: AggregateFunction                      # [c3] cannot be a final op
    operand: LogicalRef | None                          # None IFF aggregation==COUNT_ROWS [c9]
    source_relation: SourceRelation                     # required (incl. COUNT_ROWS) [c6]; every operand/filter/event_time_ref MUST be reachable from table_ref
    filter: FilterNode | None
    window: WindowPolicy

# ---- body: discriminated union on final_operation [c3] ----
@frozen class UnaryBody: final_operation=IDENTITY; expr: AggregateExpression
@frozen class RatioBody: final_operation=RATIO; numerator: AggregateExpression; denominator: AggregateExpression; zero_denominator: ZeroDenominator
@frozen class DiffBody:  final_operation=DIFFERENCE; minuend: AggregateExpression; subtrahend: AggregateExpression
FormulaBody = UnaryBody | RatioBody | DiffBody          # serialized with "final_operation" discriminator

# ---- top level ----
@frozen class ExpectedOutput: output_type: str | None; unit: str | None; currency: str | None   # advisory
@frozen class TypedFormulaProposalV1:
    formula_schema_version: int; operation_grammar_version: int; canonicalization_version: int
    grain: Grain; body: FormulaBody; parameters: tuple[ParameterDecl,...]; decimal: DecimalPolicy
    expected_output: ExpectedOutput | None
@frozen class FormulaOutputPolicyV1: output_type: str; unit: str|None; currency: str|None; output_additivity: AdditivityClass; external_type_required: bool
@frozen class TypedFormulaV1:                            # AUTHORITATIVE identity object
    formula_schema_version: int; operation_grammar_version: int; output_policy_version: int; canonicalization_version: int
    grain: Grain; body: FormulaBody; parameters: tuple[ParameterDecl,...]; decimal: DecimalPolicy; output: FormulaOutputPolicyV1
    # NO capability_policy_version, NO ids/timestamps/critic/provenance [c7]
```

Internal expression paths (for messages/diffs, NOT in the hash): `body.expr`, `body.numerator`, `body.denominator`, `body.minuend`, `body.subtrahend`. `[c4]`

## §B Operation → b_operation compatibility `[c2/round2]`
`SUM→PathAggregation.sum`; `COUNT_DISTINCT→count_distinct`; `COUNT_ROWS`/`COUNT_NON_NULL` are **new** (split of b_operation's generic `count`); `RATIO`/`DIFFERENCE` are **new** `FinalOperation`s (b_operation defers them). `min/max/avg/stddev` out; derived-temporal out-of-vocabulary. Do not widen `SupportedOperation` in place.

## §C Output authority — operation-specific required-field matrix over C1 `[c2]`
`resolve_formula_output_policy(conn, body, decimal, now) -> FormulaOutputPolicyV1`. Per-expression reads use C1 `read_operational_value`; **only the fields the operation actually needs are required** (unit/currency are C1 HINTS, so requiring them universally is wrong):

| op | required authority | output unit/currency |
|---|---|---|
| `COUNT_ROWS` | source_relation reachable + grain | dimensionless |
| `COUNT_NON_NULL` | operand existence | dimensionless |
| `COUNT_DISTINCT` | operand existence/type | dimensionless |
| `SUM` | numeric type (C1) + additivity | inherit operand unit/currency **iff the output contract requires them**, else carry as hint |
| `RATIO` | numeric both operands + units/currency **cancel** | dimensionless (mismatch that can't cancel → `NEEDS_AUTHORITY` or typed external requirement) |
| `DIFFERENCE` | numeric both + **exactly compatible** unit/currency | that unit/currency (incompatible → `INVALID_FORMULA`) |

When a *required* field is unavailable, emit a **typed external requirement** (e.g. `UNIT_PROVISIONING_REQUIRED`) rather than an indiscriminate `NEEDS_AUTHORITY`; a *hint-only* field never forces `NEEDS_AUTHORITY`. Any C1 `fork`/`hash_mismatch`/`projection_unavailable` on a required field → fail closed → `NEEDS_AUTHORITY`.

## §D Additivity — corrected `[c5]`
Override `derive_output_additivity`'s unsafe rule: `COUNT_DISTINCT` = **NON_ADDITIVE by default** (distinct(A∪B)≠distinct(A)+distinct(B) without proven disjointness). `RATIO` = non-additive. `DIFFERENCE` = non-additive unless a proven rule applies. `COUNT_ROWS`/`COUNT_NON_NULL` = additive only across **disjoint** row partitions (else non-additive). `SUM` = governed input/path additivity.

## §E Canonicalization + hashing — RFC 8785 (JCS) + rules `[c4][c10]`
Base: **RFC 8785 JSON Canonicalization Scheme** (UTF-8, lexicographic key sort, minimal number/string forms) with **NFC Unicode normalization**. Additional pinned rules: decimals/integers/dates/booleans as canonical strings (never floats); `LogicalRef` normalized (`_norm`) first; `IN` `right_set` and `allowed_set` sorted+deduplicated; **grain `keys` order is semantic (preserved)**; ordered slots (numerator/denominator, minuend/subtrahend) preserved; **associative `AND`/`OR` flattened then children sorted by canonical child hash** (not just immediate-children sort); `parameters` sorted by `name`; `NOT` never flattened; unknown fields + duplicate parameter names rejected. Internal expression paths are NOT serialized. `formula_content_hash = sha256(JCS(TypedFormulaV1))` — identity-versions in, **capability version + all provenance out** `[c7]`.

## §F Authoring result — multi-axis + corrected fold `[c6][c7]`
```python
@frozen class AuthoringResult:
    structural_status: Literal["ok","invalid_formula","unsupported_operation"]
    capability_status: Literal["ok","unsupported_capability"]
    output_status:     Literal["resolved","needs_authority","invalid_output","external_requirement"]
    expectation_status:Literal["match","mismatch","not_provided"]
    critic_status:     Literal["clean","advisory","blocking"]
    technical_status:  Literal["ok","technical_failure"]
    authoring_disposition: Literal["RESOLVED","NEEDS_REVIEW","UNSUPPORTED","REJECTED","TECHNICAL_FAILURE"]
    disposition_policy_version: int
    candidate_formula: TypedFormulaV1 | None      # [c7-round3] present for RESOLVED AND reviewable NEEDS_REVIEW
    candidate_formula_hash: str | None
```
Pure-function fold (`disposition_policy_version`): `technical_failure`→`TECHNICAL_FAILURE`; `invalid_formula` or `output=invalid_output`→`REJECTED`; **`unsupported_operation` or `capability=unsupported_capability`→`UNSUPPORTED`** `[c6]`; `output∈{needs_authority,external_requirement}` or `critic=blocking` or `expectation=mismatch`→`NEEDS_REVIEW` (with the candidate formula + hash `[c7-r3]`); else `RESOLVED`. "Authoritative" = deterministically resolved (Child 1); "approved/frozen" = Child 2.

## §G Independent critic — closed finding codes `[c11]`
Separate context/tier, no LLM-1 reasoning, structured findings only. **Closed v1 finding codes + fixed severity** (`critic_policy_version`): `MISSING_REQUIRED_OPERAND`(blocking), `WRONG_SLOT_DIRECTION`(blocking), `FILTER_INTENT_MISMATCH`(blocking), `WINDOW_INTENT_MISMATCH`(blocking), `EXTRA_UNJUSTIFIED_OPERAND`(advisory), `WEAK_PROXY`(advisory). Malformed / unknown-code / duplicate findings → the finding is dropped and a `technical_status` note recorded (a malformed critic never blocks or clears). Any blocking finding → `critic_status=blocking` (prevents auto-`RESOLVED`), never mutates the formula. `critic_findings_hash` recorded.

## §H Audit + trace — manifest-first event log `[c12][c15]`
Insert an **`authoring_run` manifest FIRST** (`authoring_run_id`, versions, intent hash). Then an append-only **`authoring_trace_event`** log: `STARTED → LLM_CALL_RECORDED | TOOL_CALLED | TOOL_RESULT_RECORDED | CRITIC_RECORDED → COMPLETED | FAILED`. The read model derives **incomplete** = a run with no terminal (`COMPLETED`/`FAILED`) event — this survives process death and durable `llm_call` rows outliving the request tx (no attempt to update a row that may not exist `[c12]`). Author/critic calls are many immutable `llm_call` records (`author_call_ids[]`, `critic_call_ids[]`); tool results stored as canonical **redacted** result + hash. Replay is scoped to reconstruction + re-validation of stored output + a linked new attempt — **not** re-authoring reproducing the same formula `[c11-r2]`.

## §I LLM-1 tools (governed catalog-authoring API)
Seven read/validate-only tools (`search_columns`, `get_column_metadata`, `get_governed_grain`, `get_time_anchor`, `get_verified_lineage`, `list_supported_operations`, `validate_draft_formula`); `get_time_anchor` now has a home (`WindowPolicy.event_time_ref`). Read-scoped; metadata-only egress; tool results are data not instructions; versions stamped; bounded iterations + token budget (exceed→`technical_failure`); coercion telemetry captured.

## §J Gold gate — pinned v1 thresholds `[c13]`
Curated `(intent, expected TypedFormulaV1)`. Compare by canonical structural equality (post-§E). **v1 gates (all must hold):** false-resolve rate = **0**; operand-preservation rate = **1.0**; required positive exact matches = **all** curated cases (≥1 each of SUM/COUNT_ROWS/COUNT_NON_NULL/COUNT_DISTINCT/RATIO/DIFFERENCE); unsupported/reject classifications = **1.0** (multi-source→UNSUPPORTED, avg→UNSUPPORTED_OPERATION, over-deep filter→INVALID); blocking-critic recall on curated adversarial cases = **1.0**; technical failures in the clean gold population = **0**. The **real-provider** schema/tool test is an **enablement gate** (key-gated), NOT a normal deterministic unit-test dependency; FakeLLM covers plumbing.

## §K Testing
Canonicalization: JCS+NFC stable; capability bump ≠ hash; grammar/operand/output/window/event_time change = hash; associative AND flatten+reorder = same hash; ordered-slot swap ≠ hash; grain-key reorder ≠ hash (order semantic). Schema: unknown field/over-limit filter/predicate-invariant violation → `INVALID_FORMULA`; body discriminator rejects `aggregation=RATIO`; `COUNT_ROWS` requires no operand + a `source_relation`; every ref reachable from `table_ref`. Authority: SUM(amount) resolves via the §C matrix (no false `NEEDS_AUTHORITY` from hint-only unit); DIFFERENCE of incompatible units → `INVALID_FORMULA`; RATIO non-cancelling units → external requirement; C1 `hash_mismatch`/`fork` on a required field → `NEEDS_AUTHORITY`. Additivity: COUNT_DISTINCT → non-additive. Disposition: `unsupported_operation`→`UNSUPPORTED` (not REJECTED); reviewable `NEEDS_REVIEW` returns `candidate_formula`+hash. Critic: blocking finding → no auto-RESOLVED; malformed finding → dropped, non-blocking. Trace: manifest-first; missing terminal event → incomplete; multi-call ReAct records all ids. Gold: thresholds per operation. Offline (FakeLLM) except the key-gated provider test.

## §L Deliverable boundary
Output = an `AuthoringResult` (+ `candidate_formula`/hash for RESOLVED or reviewable NEEDS_REVIEW) + the `authoring_run_id` trace + gold score. **Not** frozen into `feature_versions` (Child #2), **not** compiled (#3), **not** executed (#5).

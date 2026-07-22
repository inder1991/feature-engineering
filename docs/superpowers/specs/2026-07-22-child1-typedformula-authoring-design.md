# Child Spec 1 — TypedFormula Authoring (shadow, no execution)

**Parent:** `2026-07-22-feature-materialization-pipeline-design.md` (Program #1). **Carries findings:** #4 (formal grammar), #13 (LLM tool security/replay).

**Goal:** Define the formal, closed `TypedFormulaV1` grammar + exact canonicalization/hashing, and the offline authoring loop (LLM-1 author → deterministic structural validation → independent LLM-2 critic → deterministic output-policy resolution → gold gate) that turns a feature intent/recipe into an authoritative, content-hashed `TypedFormulaV1`. **No execution, no storage, no external surface** — the artifact + its audit trail are the only outputs.

**One line:** Author a durable, hashable formula *identity* the whole program pins to — proven only against gold formulas, never run against data.

## Scope / non-goals

**In scope:** `TypedFormulaProposalV1` and `TypedFormulaV1` schemas + closed grammar; the operation vocabulary; multi-source *representation* (capability-gated off); the boolean filter grammar; null/window/decimal identity semantics; the five versions; canonicalization + `formula_content_hash`; the six outcome codes; LLM-1 authoring (ReAct, 7 read/validate tools) as a governed catalog-authoring API; the independent critic; the gold gate; the append-only tool-trace audit.

**Explicitly deferred (other children):** compilation to planner/IR (#3), execution/materialization (#5), storage (#5), the external run protocol (#6), artifact→version→binding persistence + status-axis derivation (#2), temporal *computation* (#4 — this spec fixes temporal *declaration only*), profiling (#7).

---

## Global constraints

- **Two distinct objects.** The LLM authors `TypedFormulaProposalV1`; deterministic validation + `resolve_output_policy` produce the authoritative `TypedFormulaV1`. The LLM MAY propose output *expectations*; it MUST NOT declare authoritative output type/unit/currency/additivity — the resolver derives those and **rejects contradictions**. (Rule 1)
- **Operand identity = canonical `logical_ref` `source::schema.table.column`** (`object_ref.py:23`), normalized before hashing.
- **`formula_content_hash` covers the canonical `TypedFormulaV1` ONLY** — never feature ids, LLM-call ids, timestamps, critic findings, or deployment values (those live in the parent's other hashes). (Rule 3)
- **Unsupported ≠ invalid** — a valid formula the platform can't yet execute is not semantically wrong. (Rule 6)
- Grammar **extends** `b_operation` / `b_output_policy`; it does not replace them.

## §1 Versions (grammar vs capability are separate) — Rule 2

Five independently-bumped versions stamped on every proposal/formula/trace:
`formula_schema_version` (AST shape) · `operation_grammar_version` (operation/aggregation vocabulary) · `formula_capability_policy_version` (what execution is *enabled*, e.g. single- vs multi-source) · `output_policy_version` (`b_output_policy`) · `canonicalization_version`.

A new AST shape/operation bumps grammar; **enabling an already-modelled capability (multi-source) bumps only the capability policy — the `formula_content_hash` does not change** because the platform gained a capability.

## §2 Operation vocabulary (v1 — small, reviewed) — ordered slots

| operation | slots | notes |
|---|---|---|
| `SUM` | `operand` | numeric operand, over the window |
| `COUNT_ROWS` | — | count rows in scope (no operand) |
| `COUNT_NON_NULL` | `operand` | count rows where operand is non-null |
| `COUNT_DISTINCT` | `operand` | distinct non-null values of operand |
| `RATIO` | `numerator`, `denominator` | **ordered** — never inferred from operand order |
| `DIFFERENCE` | `minuend`, `subtrahend` | **ordered** |

No ambiguous generic `COUNT`. `avg`/`stddev` remain out (ungovernable, per `b_operation`). Derived temporal ops (`trend`, `velocity`, `growth`, `zscore`) are **out of v1 vocabulary** (deferred, consistent with `_DEFERRED_TIME_ALIASES`) — but the schema shape accommodates them at a later grammar version.

## §3 Operand model + multi-source (representable, capability-gated) — chosen: A

- Every operand carries an exact `logical_ref`. Operands **may** name different catalog sources — the **grammar** permits it.
- **Capability policy** decides executability: v1 policy allows one catalog source; a formula whose operands span sources validates structurally but yields **`UNSUPPORTED_CAPABILITY`** (not `INVALID_FORMULA`). Enabling multi-source later is a `formula_capability_policy_version` bump; the formula's meaning and `formula_content_hash` are unchanged.
- Duplicate operands are rejected in canonicalization (Rule 3).

## §4 Filter grammar (narrow, closed) — Rule 4

A boolean AST — no raw expression strings. Nodes: `and`, `or`, `not`; predicates: `equal`, `not_equal`, `greater_than`, `greater_or_equal`, `less_than`, `less_or_equal`, `in`, `not_in`, `is_null`, `is_not_null`. Leaves reference a `logical_ref` on one side and a typed literal or a declared `parameter` on the other. **Hard limits:** max tree depth, max predicate count, max `in`-list size (exact numbers pinned in the schema). Anything outside → `INVALID_FORMULA`.

## §5 Identity-bearing semantics (Rule 5 — same hash ⇒ same value)

Declared explicitly on every formula (two engines with the same hash MUST compute the same value):
- **window:** `{basis: trailing | calendar_period, length, unit, start_inclusive, end_inclusive}` — rolling duration vs calendar period is explicit.
- **empty-window result**, **null-input treatment**, **divide-by-zero behavior** (`RATIO.zero_denominator: null | zero | error`).
- **decimal:** precision, rounding mode, overflow behavior (canonical decimal strings, never binary floats).

These are part of the canonical form and therefore the hash — omitting any is an `INVALID_FORMULA`.

## §6 Output semantics — proposal vs authoritative (Rule 1)

`TypedFormulaProposalV1.expected_output` (optional, LLM's guess) is advisory. The authoritative `TypedFormulaV1.output` = `resolve_output_policy(...)` over the operands + operation → `{output_type, output_additivity, external_type_required, unit, currency}` from governed reads, fail-closed. A proposal whose `expected_output` contradicts the resolved output → the contradiction is a critic-visible finding and a deterministic `INVALID_FORMULA`/`NEEDS_AUTHORITY` (never silently overridden).

## §7 Outcome vocabulary (Rule 6)

`INVALID_FORMULA` (grammar/limit/semantics violation) · `UNSUPPORTED_OPERATION` (op not in this grammar version) · `UNSUPPORTED_CAPABILITY` (valid but execution disabled, e.g. multi-source) · `NEEDS_AUTHORITY` (an operand/output needs governance the reads can't clear) · `RESOLVED` (authoritative `TypedFormulaV1` produced) · `TECHNICAL_FAILURE` (LLM/tool/infra fault — distinct from a semantic verdict).

## §8 Authoring pipeline (no execution)

```
intent (free-form) OR recipe
  → LLM-1 AUTHOR (ReAct; §9 tools only)                → TypedFormulaProposalV1
  → deterministic STRUCTURAL validation (§2/§3/§4/§5)   → INVALID/UNSUPPORTED_* or a structurally-sound proposal
  → LLM-2 INDEPENDENT CRITIC (§10)                      → structured findings (no rewrite)
  → deterministic OUTPUT-POLICY resolution (§6)         → authoritative TypedFormulaV1 (+ NEEDS_AUTHORITY if unclear)
  → canonicalize + formula_content_hash (§ Rule 3)      → RESOLVED artifact (in-memory / shadow store)
  → GOLD GATE (§11)                                     → pass/fail score vs curated expected formula
```
Runs **offline**; produces no plan, no data, no external call. `RESOLVED` here means "authoritative formula authored + hashed", NOT "eligible to run" (that is the parent's `materialization_eligibility`, Child #2).

## §9 LLM-1 tool API — a governed catalog-authoring surface (Rule 7 / #13)

Seven **read/validate-only** tools; none approves, executes, or mutates governance:
`search_columns` · `get_column_metadata` · `get_governed_grain` · `get_time_anchor` · `get_verified_lineage` · `list_supported_operations` · `validate_draft_formula`.

Security/replay contract: **read-scoped** (respects the caller's roles); **metadata-only egress** (no raw data values leave to the model); **prompt-injection treatment** (tool results are data, never instructions); stamped **model/prompt/schema/grammar/capability versions**; **bounded ReAct iterations** + **token/cost budget** (exceed → `TECHNICAL_FAILURE`); **raw author/critic responses captured**; **coercion telemetry** (e.g. comma-string→list, per the free-form fixes); **deterministic replay inputs** (given the same tool results + versions, re-authoring is reproducible). The model iterates until `validate_draft_formula` reports no structural omissions.

## §10 Independent critic (LLM-2)

Genuinely independent or it is theatre: **separate prompt + context construction**; a **different model tier** where available (e.g. Opus author / Sonnet critic); **not shown LLM-1's reasoning or tool trace**; returns **structured findings only** (missing operand? numerator/denominator direction? filter matches intent? window matches the stated period?), **never a rewritten formula**; must compare **every** business requirement in the intent against the proposal's operands. Findings are hashed (`critic_findings_hash`) and routed by the deterministic layer (a finding cannot silently mutate the formula).

## §11 Gold gate

A curated set of `(intent, expected TypedFormulaV1)` pairs for the first features (starting with `cross_border_value_ratio_90d`). The authored formula is compared to the expected one by **canonical structural equality** (same `formula_content_hash` ⇒ pass; otherwise a typed diff of operation/operands/filter/window/output). Mirrors the existing B-Gate-1 gold-harness discipline: the gate is non-vacuous (a reject-all authorer fails) and the diff localizes the miss.

## §12 Audit / trace (extend, don't duplicate) — Rule 7

Author + critic calls use the existing `record_llm_call` (immutable, run-bucketed, durable on a fresh connection). Add an **append-only tool-trace artifact** correlating: `author_call_id`, `critic_call_id`, prompt/schema/model/grammar/capability versions, the **ordered tool calls + canonical tool results (or result hashes)**, `proposal_hash`, `critic_findings_hash`, `formula_content_hash`. This is the replay + audit record for one authoring.

## Testing

- **Grammar:** canonicalization is exact and stable (sorted keys, canonical decimals, enum casing, unknown-field + duplicate-operand rejection, `logical_ref` normalization) → identical `formula_content_hash` across re-serialization; a capability-policy bump does NOT change the hash; a grammar/operand change DOES.
- **Vocabulary/slots:** `RATIO`/`DIFFERENCE` require named ordered slots; swapping numerator/denominator changes the hash; `avg`/`trend` → `UNSUPPORTED_OPERATION`; multi-source operand → `UNSUPPORTED_CAPABILITY` (not `INVALID`).
- **Output authority:** an LLM proposal declaring a contradicting output type is not accepted; the resolved output comes from `resolve_output_policy`.
- **Filter limits:** over-deep / over-wide filters → `INVALID_FORMULA`.
- **Critic independence:** critic receives no LLM-1 reasoning; a planted intent/formula mismatch is caught as a structured finding.
- **Gold gate:** `cross_border_value_ratio_90d` authored formula matches the curated expected; a deliberately wrong direction (denominator as numerator) fails the gate with a localized diff.
- **Audit:** one authoring emits one tool-trace with all correlation fields + hashes.
- All tests are offline (no Spark, no DB writes beyond the shadow trace); LLM calls use the existing FakeLLM scripting for determinism.

## Deliverable boundary

The output of Child 1 is a `RESOLVED` authoritative `TypedFormulaV1` + its `formula_content_hash` + the tool-trace audit + a gold score. It is **not** frozen into `feature_versions` (Child #2), **not** compiled (Child #3), **not** executed (Child #5). This keeps the first slice safe and makes the identity contract everything else depends on real and testable in isolation.

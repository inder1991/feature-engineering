# Phase 1A — Shadow Recognizer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: subagent-driven-development. Steps use `- [ ]` checkboxes.

**Goal:** Build the LLM-only use-case recognizer + the applicability evaluator + a gold evaluation set + a metrics harness, so we can measure **false-narrowing** against the gold set **before** any filtering goes live. Recognition runs but never changes what grounds (shadow). Deferred to Phase 2: `modelling_context`-method and `entity_context` dimensions.

**Architecture:** The recognizer rides the existing `LLMClient` seam and `drive_structured_call` (which already provides the bounded repair/retry/fail-closed runtime contract). Output is validated against the closed taxonomy (`taxonomy/use_cases.py`). The applicability evaluator maps a recognised/confirmed scope → concrete in-scope recipe ids (this is what Phase-1B will pass to grounding; built now to measure recall). The eval harness is deterministic in CI (FakeLLM); the real-LLM gold run is a runnable command whose results gate Phase 1B.

**Tech stack:** Python 3.12; `featuregen.intake.llm` (`LLMClient`, `LLMRequest`, `drive_structured_call`, `FakeLLM`, `FakeResponse`, `SchemaValidationError`); `uv run pytest -q`, `ruff`, `mypy`.

## Global Constraints

- **Shadow = behaviour-neutral by default.** The `intent_recognition_shadow` flag defaults **off**; grounding, `gate1`, and `templates.py` are unchanged. When on, recognition only *logs* — it never filters.
- **Recognition never sees catalog columns.** Input is the **redacted** hypothesis + prediction goal only (`redacted_hypothesis`/`redacted_goal`), consistent with `redact_free_text`.
- **Closed taxonomy.** Every recognised id is validated against `USE_CASE_REGISTRY`; unknown ids are a validation failure → repair → fallback. The recognizer never invents ids.
- **Fail-open to `unscoped`.** Any technical failure (`STATUS_FAILED`, validation exhausted, timeout) → `TECHNICAL_FAILURE`/`UNSCOPED`, full grounding continues. The recognizer never raises to its caller and never blocks generation.
- **Two metrics, not one.** *Recognition accuracy* (did the LLM pick the right use-cases) AND *applicability recall* (after mapping+inheritance, are the expert-relevant recipes retained). **Applicability recall is the Phase-1B gate.**
- **Version quintet** stamped on every recognition result: `taxonomy_version`, `applicability_mapping_version`, `recognizer_model_id`, `prompt_version`, `recipe_registry_version`.

---

## Task 1: Recognition contracts + closed-taxonomy validator

**Files:**
- Create: `src/featuregen/overlay/upload/taxonomy/recognition.py`
- Test: `tests/featuregen/overlay/upload/taxonomy/test_recognition_contract.py`

**Interfaces — Produces:**
- `class RecognitionStatus(StrEnum): CLASSIFIED; AMBIGUOUS; UNSCOPED; TECHNICAL_FAILURE`
- `@dataclass(frozen=True) class UseCaseCandidate: use_case_id: str; relationship: Literal["primary","secondary"]; confidence: Literal["high","medium","low"]; evidence_spans: tuple[str,...]; rationale: str`
- `@dataclass(frozen=True) class RecognitionResult: status: RecognitionStatus; candidates: tuple[UseCaseCandidate,...]; ambiguity_note: str|None; taxonomy_version: str; recognizer_model_id: str; prompt_version: str`
- `def validate_recognition_output(output: Mapping) -> None` — raises `SchemaValidationError` when: `status` not a valid enum; a candidate `use_case_id` ∉ `USE_CASE_REGISTRY`; a **primary** candidate is not `selectable`; >1 primary; >2 secondary; >3 candidates total; `confidence` not in the band; a `CLASSIFIED`/`AMBIGUOUS` result with 0 candidates; an evidence span not a non-empty string.
- `def unscoped_result(reason, *, model_id, prompt_version, technical=False) -> RecognitionResult` (the fallback constructor).

- [ ] **Step 1: Failing test** — `validate_recognition_output` accepts a well-formed CLASSIFIED body (primary = a real selectable leaf) and raises `SchemaValidationError` for: unknown id, a non-selectable primary (`financial_crime`), two primaries, three secondaries, a `classified` body with no candidates.
- [ ] **Step 2: Run — expect fail.**
- [ ] **Step 3: Implement** the enum, dataclasses, `validate_recognition_output`, `unscoped_result`.
- [ ] **Step 4: Run — expect pass.**
- [ ] **Step 5: Gates + commit** `feat(recognizer): recognition contracts + closed-taxonomy validator (1A task 1)`.

---

## Task 2: The recognizer (LLM-only, fail-open)

**Files:**
- Create: `src/featuregen/overlay/upload/taxonomy/recognizer.py`
- Create: `src/featuregen/overlay/upload/taxonomy/recognizer_prompt.py`
- Test: `tests/featuregen/overlay/upload/taxonomy/test_recognizer.py`

**Interfaces — Consumes:** `LLMClient`, `LLMRequest`, `drive_structured_call`, `STATUS_FAILED`, Task-1 contracts. **Produces:**
- `RECOGNIZER_TASK = "use_case_recognition"`, `PROMPT_ID`, `PROMPT_VERSION`.
- `def build_recognition_prompt() -> str` (in `recognizer_prompt.py`) — assembles the closed taxonomy (selectable use-cases: id + display + a line of include/exclude boundary examples) + the classification rules (≤1 primary, ≤2 secondary, qualitative confidence, quote evidence spans, abstain to `unscoped` when nothing clearly applies, classify from intent NOT data) + the output schema shape.
- `def recognize(client, *, redacted_hypothesis, redacted_goal=None, model_id="claude-opus-4-8") -> RecognitionResult` — builds the `LLMRequest` (task=`RECOGNIZER_TASK`, inputs = `{"hypothesis": redacted_hypothesis, "prediction_goal": redacted_goal}` — NO columns), calls `drive_structured_call(client, request, validate_recognition_output)`, then maps: `STATUS_FAILED` → `unscoped_result(technical=True)` (status `TECHNICAL_FAILURE`); else read `output["status"]` → `CLASSIFIED`/`AMBIGUOUS`/`UNSCOPED`, building candidates. Never raises.

- [ ] **Step 1: Failing test** (FakeLLM-scripted on `RECOGNIZER_TASK`):
  - a well-formed CLASSIFIED output → `status == CLASSIFIED`, primary candidate present;
  - an output with an **unknown use_case id** → after repair budget, `recognize` returns `UNSCOPED`/`TECHNICAL_FAILURE` (never raises, never an invalid id);
  - `FakeResponse(provider_status=PROVIDER_REFUSAL)` → `TECHNICAL_FAILURE`, no candidates;
  - a `{"status":"unscoped","candidates":[]}` output → `UNSCOPED`.
- [ ] **Step 2: Run — expect fail.**
- [ ] **Step 3: Implement** the prompt builder + `recognize` + the outcome mapping.
- [ ] **Step 4: Run — expect pass.**
- [ ] **Step 5: Gates + commit** `feat(recognizer): LLM-only use-case recognizer, fail-open (1A task 2)`.

---

## Task 3: Applicability evaluator (recognised scope → in-scope recipes)

**Files:**
- Create: `src/featuregen/overlay/upload/taxonomy/applicability.py`
- Test: `tests/featuregen/overlay/upload/taxonomy/test_applicability.py`

**Interfaces — Produces:**
- `class ScopeExpansion(StrEnum): EXACT; INCLUDE_DESCENDANTS`
- `@dataclass(frozen=True) class ConfirmedScope: primary: str|None; secondary: tuple[str,...]=(); expansion: ScopeExpansion = ScopeExpansion.EXACT; unscoped: bool = False`
- `def scope_from_recognition(result) -> ConfirmedScope` (primary/secondary from the candidates; `unscoped=True` for UNSCOPED/TECHNICAL_FAILURE).
- `def in_scope_recipes(scope) -> tuple[set[str], set[str]]` — returns `(primary_scoped, supporting_scoped)` recipe-id sets. Rules: `unscoped` → ALL recipe ids (fail-open). Else a recipe is in scope when its `recipe_applicability` **primary** equals a confirmed use-case, OR (only under `INCLUDE_DESCENDANTS`) is a descendant of a confirmed use-case, OR the recipe lists the confirmed use-case among its secondaries (→ `supporting_scoped`, never capped). A recipe whose primary is a *descendant* of a confirmed leaf is NOT auto-included under `EXACT`.

- [ ] **Step 1: Failing test** — for `ConfirmedScope(primary="customer.relationship_attrition.churn")` under EXACT, the churn recipes are in scope and no credit/fraud recipe is; `unscoped=True` → all 153; under `INCLUDE_DESCENDANTS` with `primary="credit"`, the credit-leaf recipes are in scope; a recipe listing a confirmed use-case as *secondary* appears in `supporting_scoped`.
- [ ] **Step 2: Run — expect fail.**
- [ ] **Step 3: Implement** using `recipe_applicability` over `ALL_TEMPLATES` + `use_cases.descendants`.
- [ ] **Step 4: Run — expect pass.**
- [ ] **Step 5: Gates + commit** `feat(recognizer): applicability evaluator — scope to recipe ids (1A task 3)`.

---

## Task 4: Gold evaluation set

**Files:**
- Create: `tests/featuregen/overlay/upload/taxonomy/gold_recognition.py`
- Test: `tests/featuregen/overlay/upload/taxonomy/test_gold_set.py`

**Interfaces — Produces:** `GOLD: tuple[GoldCase,...]` where `@dataclass(frozen=True) class GoldCase: id: str; hypothesis: str; prediction_goal: str|None; expected_primary: str|None; permitted_secondary: tuple[str,...]; expected_relevant_recipes: tuple[str,...]; category: Literal["straightforward","synonym","ambiguous","unscoped","regulated","multi_use_case"]`.

- [ ] **Step 1: Author** ≥24 gold cases spanning: each major family at least once; ≥4 `synonym` cases (banking wording that isn't the leaf name); ≥3 `ambiguous`; ≥3 `unscoped` (exploratory / no target); ≥3 `regulated` (fair-lending / AML-scoped); ≥2 `multi_use_case`. Each `expected_primary` is a real selectable leaf (or `None` for unscoped); `expected_relevant_recipes` are real recipe ids (validate against `ALL_TEMPLATES`).
- [ ] **Step 2: Test** — every `expected_primary` (when set) ∈ `selectable_leaves()`; every `expected_relevant_recipes` id ∈ `{t.id for t in ALL_TEMPLATES}`; category coverage minimums met; the marker that this set is **authored, pending expert review** is present (a module docstring note).
- [ ] **Step 3: Gates + commit** `test(recognizer): gold evaluation set (1A task 4)`.

---

## Task 5: Evaluation harness + metrics

**Files:**
- Create: `src/featuregen/overlay/upload/taxonomy/recognition_eval.py`
- Test: `tests/featuregen/overlay/upload/taxonomy/test_recognition_eval.py`

**Interfaces — Produces:**
- `@dataclass(frozen=True) class EvalReport: primary_accuracy: float; top3_recall: float; applicability_recall: float; false_narrowing_count: int; false_narrowing_regulated: int; abstention_precision: float; stability: float; per_case: tuple[...]`
- `def evaluate(client, gold=GOLD) -> EvalReport` — for each case: `recognize(...)` → `scope_from_recognition` → `in_scope_recipes`; **false-narrowing** = an `expected_relevant_recipe` NOT in `(primary_scoped ∪ supporting_scoped)`; applicability_recall = retained / expected across all cases; abstention precision over `unscoped` cases; stability = fraction of cases whose scope is identical on a second `recognize` call.
- A runnable entry (`def main()` / `python -m …recognition_eval`) that prints the report — the **real-LLM shadow run** (uses `current_llm_client()`), whose results gate Phase 1B.

- [ ] **Step 1: Failing test** — with a FakeLLM scripted to return the **correct** recognition for a small gold subset, `evaluate` reports `false_narrowing_count == 0` and `applicability_recall == 1.0`; with a FakeLLM scripted to return an **over-narrow** scope (drops a relevant leaf), `evaluate` **detects** `false_narrowing_count > 0`. (This tests the harness math, not real-LLM quality.)
- [ ] **Step 2: Run — expect fail.**
- [ ] **Step 3: Implement** `evaluate` + `main`.
- [ ] **Step 4: Run — expect pass**, then the full overlay/contract/governance suite (behaviour-neutrality).
- [ ] **Step 5: Gates + commit** `feat(recognizer): eval harness + false-narrowing metrics (1A task 5)`.

---

## Task 6: In-flow shadow hook (flag-gated, log-only)

**Files:**
- Modify: `src/featuregen/overlay/upload/contract/gate1.py` (a single guarded call in `build_considered_set`)
- Test: `tests/featuregen/overlay/upload/taxonomy/test_shadow_hook.py`

- [ ] **Step 1: Failing test** — with the `intent_recognition_shadow` flag **on** and a FakeLLM scripted for both generation and recognition, `build_considered_set` returns the **same alternatives** as with the flag off (grounding unchanged), and a recognition record/log line is emitted. With the flag **off**, `recognize` is not called.
- [ ] **Step 2: Run — expect fail.**
- [ ] **Step 3: Implement** — a flag read (default off); when on, call `recognize(client, redacted_hypothesis=intent.redacted_hypothesis, redacted_goal=…)` and `log()` the proposed scope + `len(in_scope_recipes(...))` vs the actual grounded count. **Do not** use the result to filter. Persisting recognition to a DB table is Phase 1B.
- [ ] **Step 4: Run — expect pass** + full suite green.
- [ ] **Step 5: Gates + commit** `feat(recognizer): flag-gated in-flow shadow logging (1A task 6)`.

---

## Self-review
- Behaviour-neutral: only Task 6 touches existing code, flag-gated default-off and log-only; grounding output is asserted unchanged.
- Fail-open: Task 2 maps every failure to unscoped; Task 3 `unscoped → all recipes`; the recognizer never raises.
- Gate correctness: Task 5's harness computes **applicability recall / false-narrowing** (the real 1B gate), not just recognition accuracy, and is proven to *detect* narrowing.
- The real-LLM gold run + expert review of the gold set are the human gating activities before 1B — flagged, not automated.

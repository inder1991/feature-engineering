# The recognition repair seam — a correct answer must not be lost to a sloppy sibling

**Date:** 2026-08-15 · **Origin:** a live run on the kind sandbox, not a code review.

## 0. What happened, and what is verified

A user asked *"customers whose transaction activity suddenly accelerates are about to leave"* with the
goal *"predict churn in the next 90 days"*. The recognizer returned this, recorded verbatim in
`llm_call.raw_output` (run `grun_01M02SAZWKC6FJ4DPTY4WVCB12`, 13:20:17Z):

```json
{"status": "classified",
 "candidates": [
   {"use_case_id": "customer.relationship_attrition.churn", "relationship": "primary",
    "confidence": "high",
    "evidence_spans": ["predict churn in the next 90 days",
                       "customers whose transaction activity suddenly accelerates are about to leave"],
    "rationale": ""},
   {"use_case_id": "x", "relationship": "primary", "confidence": "high",
    "evidence_spans": ["x"], "rationale": "placeholder"}],
 "modelling_contexts": []}
```

**The first candidate is correct.** The second is transparently a placeholder — it says so. The
platform discarded BOTH, recorded `technical_failure`, and the screen told the user *"No use-case was
recognised for this objective"*. The run then served **917 unscoped candidates** (306 of 317 recipes)
instead of the churn-scoped set.

### 0.1 The verified defect chain

| # | Fact | Evidence |
|---|---|---|
| 1 | The registered output schema declares `use_case_id` as a bare string — the 88 ids are prose in the prompt only | `enrich_llm.py:1452` (`("use_case_recognition", 1)`), `"use_case_id": {"type": "string"}` |
| 2 | Sibling fields in the SAME schema DO carry enums (`status`, `relationship`, `confidence`) | same block |
| 3 | The inner seam accepts a semantic validator and drives bounded repair/retry from it | `intake/llm.py:403-406` (`validate_output: Callable[...]`), §9.2 taxonomy at `:188` |
| 4 | **The audited wrapper hardcodes the validator to schema-only, and exposes no parameter for a caller's own** | `enrich_llm.py:2099`: `drive_structured_call(dispatch_client, req, lambda output: reg.validate(schema_id, schema_version, output))`; signature at `:1997-2007` has no `validate_output` |
| 5 | So the recognizer validates AFTER the call returns, outside the repair loop, where the only verdict is reject-everything | `taxonomy/recognizer.py:187` `validate_recognition_output(output)` in a `try/except` → `unscoped_result(..., technical=True)` |
| 6 | Consequently the call logged `{"result": "ok"}` with `repair_attempts: []` — the model was never asked to fix its own output | `llm_call` row, 13:20:17Z |
| 7 | `validate_recognition_output`'s own docstring claims it is *"the `validate_output` callback `drive_structured_call` invokes"* — on this path nothing invokes it there | `taxonomy/recognition.py:8` |
| 8 | The taxonomy validator is all-or-nothing: the first bad candidate raises | `recognition.py:126` inside `_validate_candidate`, called per-candidate from `validate_recognition_output` |
| 9 | The same module already has the FORGIVING pattern for two sibling dimensions, deliberately and documented | `recognition.py:221` — *"Unlike `validate_recognition_output` — which fails the WHOLE recognition on a malformed core"*; `normalize_dimensions` |
| 10 | The UI reports absence, not loss | `WorkbenchScreen.tsx:2060` *"No use-case was recognised for this objective."* |
| 11 | The audited wrapper has ~29 call sites, incl. the formula seam which delegates to it precisely to avoid re-implementing repair | `formula/audited.py:99,141` |

**Not a cause:** billing (the call returned HTTP 200), the prompt (88 ids rendered, churn listed
first — `recognizer_prompt.py`, 8906 chars), or the model being unreachable. The model was also
sloppy in a second way — two candidates both `"primary"` where the rules allow one — which is a
second independent signal that entry #2 was junk.

**Deliberate-design caveat, stated so it is argued rather than assumed:** fail-closed on the CORE
classification is a considered choice (fact 9 documents the contrast). This plan does not overturn
it. It restores the step that was supposed to come first — letting the model repair — and only then
decides what to do with a still-invalid body.

## 0.2 The done-bar

> A recognition whose body carries one valid candidate and one invalid one ends as a CLASSIFIED
> scope over the valid candidate — after the model has been given its designed chance to repair —
> and any residue the platform dropped is named to the user, never reported as absence.

## 1. Tasks

### Task 1 — the audited wrapper stops swallowing the caller's validator (½ day)

**Modify:** `src/featuregen/overlay/upload/enrich_llm.py`
- `drive_audited_structured_call` gains `validate_semantics: Callable[[Mapping[str, Any]], None] | None = None`.
- `:2099` composes instead of replacing: registry schema first (unchanged, still the fail-closed
  floor), then the caller's validator when given. One lambda, both checks, so a semantic failure is
  a REPAIRABLE outcome exactly as a schema failure already is.
- `audited_structured_call` (the output-only view, `:1978`) passes it through.

**Why the default is `None`:** every one of the ~29 existing call sites keeps today's behaviour
byte-for-byte, including `formula/audited.py`, which delegates here specifically to inherit repair.

**Acceptance (tests):**
- `test_a_semantic_failure_is_repaired_not_returned` — a scripted client returns an invalid body then
  a valid one; the result is the valid body and `repair_attempts` records the round.
- `test_the_registry_schema_still_fails_closed_first` — a body failing the SCHEMA is unchanged in
  outcome and message, whether or not `validate_semantics` is supplied.
- `test_every_existing_call_site_is_byte_identical` — the wrapper without the new argument produces
  the identical `LLMRequest` and validator behaviour (AST/structural over call sites + a golden).

### Task 2 — the recognizer uses it (½ day)

**Modify:** `src/featuregen/overlay/upload/taxonomy/recognizer.py`
- Pass `validate_semantics=validate_recognition_output` at `:151`.
- Keep the post-call `validate_recognition_output` at `:187` as the belt-and-braces floor — a
  repair that never succeeded must still not yield a scope.
- **Correct the docstring at `recognition.py:8`** — it describes a wiring that did not exist.

**Acceptance (tests):**
- `test_the_padded_body_from_the_live_incident_now_recognises` — the EXACT `raw_output` above as
  turn 1, a clean single-candidate body as turn 2 → `CLASSIFIED` on
  `customer.relationship_attrition.churn`. This is the regression test for the incident.
- `test_a_body_that_stays_invalid_after_repair_still_fails_closed` — no scope is invented.
- `test_repair_is_recorded_on_the_llm_call` — `repair_attempts` is non-empty, so the audit shows
  what happened.

### Task 3 — partial validity, decided rather than inherited (1 day)

Task 1+2 fix the incident. Task 3 answers the question the incident raises: *when repair fails and
the body still has one good candidate and one bad one, is reject-everything right?*

**Modify:** `src/featuregen/overlay/upload/taxonomy/recognition.py`
- `validate_recognition_output` keeps its all-or-nothing contract (callers depend on it).
- **New** `partition_candidates(output) -> (kept, dropped)` beside `normalize_dimensions`, following
  that function's documented non-fatal idiom: structurally-valid, closed-taxonomy candidates are
  kept; each dropped one carries its reason code.
- The recognizer folds a post-repair body through it: candidates survive → `CLASSIFIED`/`AMBIGUOUS`
  with `warnings` naming every drop; none survive → today's `technical_failure`.

**The rule that keeps this honest:** a dropped candidate never becomes silence. `warnings` already
exists on `RecognitionResult` (`recognition.py:113`) and is already persisted
(`intent_recognition_attempt.warnings`).

**Acceptance (tests):**
- `test_one_valid_and_one_junk_candidate_yields_the_valid_scope_and_names_the_drop`
- `test_two_primaries_after_partition_is_still_a_refusal` — the cap is not laundered by dropping.
- `test_nothing_valid_survives_is_unchanged` — the existing failure path is untouched.

### Task 4 — the schema stops permitting `x` (½ day) — **schema version 2**

**Modify:** `enrich_llm.py:1452` — a NEW `("use_case_recognition", 2)` whose `use_case_id` carries
`"enum": [...]` built FROM `selectable_leaves()`/`USE_CASE_REGISTRY`, never hand-typed (the C1
precedent: an advertisement derived from the thing it describes). v1 stays byte-frozen.

**Open question for the operator, stated not assumed:** 88 enum members is a large constraint to put
on the wire, and the registry is versioned independently of the schema. If the taxonomy grows, the
schema must version with it. Recommend shipping Tasks 1-3 first and treating this as defence in
depth once they are proven.

**Acceptance (tests):**
- `test_the_enum_is_derived_from_the_registry_not_typed` (AST/structural).
- `test_v1_is_byte_frozen`.
- `test_an_invalid_id_now_fails_at_the_schema_layer` — and therefore repairs.

### Task 5 — the surface stops reporting loss as absence (½ day)

**Modify:** `frontend/src/screens/WorkbenchScreen.tsx:2060` and the payload behind it — when
recognition dropped candidates or exhausted repair, say so: *"one proposed objective was discarded as
invalid — showing all buildable recipes"*, never the bare *"No use-case was recognised"*.

**Acceptance (tests):** a Vitest case per state (recognised / nothing recognised / recognised-then-
discarded); the standing honest-absence law asserted on the third.

## 2. Risks

- **The wrapper is a governed seam with ~29 callers.** The additive default is what makes this safe;
  Task 1's byte-identity test is the guard, not the intention.
- **Repair costs a provider round-trip.** Bounded by the existing §9.2 taxonomy — this plan adds no
  new retry policy, it only lets an existing one see the failure.
- **Task 3 changes what a partially-invalid body means.** That is a governance-visible semantic
  change; it ships behind its own tests and is separable from 1+2.
- **Not addressed here:** why `claude-opus-4-8` padded the list at all. The cluster ran
  `claude-sonnet-5` on 2026-08-14 (4/4 classified) and `claude-opus-4-8` today (0/2). Model choice is
  the operator's; this plan makes the platform robust to either.

## 3. Sequencing

```
Task 1 ──► Task 2      (fixes the incident; smallest honest fix)
             │
             ├──► Task 3   (partial validity — the policy question)
             ├──► Task 4   (schema enum — defence in depth, operator call)
             └──► Task 5   (the surface tells the truth)
```

**Minimum honest fix: Tasks 1 + 2** (~1 day). Everything after is hardening.

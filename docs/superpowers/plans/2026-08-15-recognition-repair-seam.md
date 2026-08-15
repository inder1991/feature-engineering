# The recognition repair seam — a correct answer must not be lost to a sloppy sibling

**Date:** 2026-08-15 · **Origin:** a live run on the kind sandbox, not a code review.
**Revision 2** — rewritten after an adversarial review of revision 1 (`730c69a8`) found four
blockers, three of which made tasks as-written unbuildable. What that review corrected is marked
**[R2]** throughout, because a plan that hides its own defects teaches nobody.

## 0. What happened, and what is verified

A user asked *"customers whose transaction activity suddenly accelerates are about to leave"*, goal
*"predict churn in the next 90 days"*. The recognizer returned this, recorded verbatim in
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

The first candidate is correct. The second says `"placeholder"` in as many words. The platform
discarded BOTH, recorded `technical_failure`, told the user *"No use-case was recognised"*, and
served **917 unscoped candidates** (306 of 317 recipes).

### 0.1 The verified defect chain

| # | Fact | Evidence |
|---|---|---|
| 1 | The registered schema declares `use_case_id` as a bare string; the 88 ids are prose in the prompt only | `enrich_llm.py:1452` |
| 2 | Sibling fields in the SAME schema carry enums (`status`, `relationship`, `confidence`) | same block |
| 3 | The inner seam accepts a semantic validator and drives bounded repair from it | `intake/llm.py:403-406`, §9.2 taxonomy `:188` |
| 4 | **The audited wrapper hardcodes the validator to schema-only and exposes no parameter for the caller's own** | `enrich_llm.py:2099`; signature `:1997-2007` |
| 5 | The recognizer therefore validates AFTER the call, outside the repair loop | `taxonomy/recognizer.py:187` |
| 6 | The call logged `{"result": "ok"}` with `repair_attempts: []` — the model was never asked to fix its output | `llm_call` row 13:20:17Z |
| 7 | `validate_recognition_output`'s docstring claims it IS the callback the inner seam invokes | `recognition.py:8` |
| 8 | The taxonomy validator is all-or-nothing — the first bad candidate raises | `recognition.py:126` |
| 9 | The module already has the forgiving idiom for two sibling dimensions, deliberately | `recognition.py:221`, `normalize_dimensions` |
| 10 | The UI reports absence, not loss | `WorkbenchScreen.tsx:2060` |
| 11 | **[R2 — corrected]** The wrapper has **12 distinct caller modules**, not "~29". Revision 1 quoted a raw grep line count as a caller count | `grep -rl`, verified at `730c69a8` |

### 0.2 What revision 1 got wrong — verified at `730c69a8` **[R2]**

| # | Blocker | Evidence |
|---|---|---|
| B1 | **Task 3 was unbuildable.** After repair exhaustion the wrapper returns `output=None` on BOTH the discard and failed paths, so the recognizer has no body left to partition. Modifying `recognition.py` alone cannot work | `enrich_llm.py:2128`, `:2135`; `recognizer.py:172-184` |
| B2 | **Re-running the same objective returns a NEW http result over an OLD stored row.** Persistence is unique on `(intent_id, input_hash)`, and `input_hash` covers only redacted user input + redaction policy — not prompt, schema, taxonomy, validator or model. The endpoint calls the provider again, then `ON CONFLICT DO NOTHING` returns the OLD recognition id. After this fix ships, resubmitting the incident could display "Churn" while the returned id still points at the `technical_failure` row | `contract.py:1173-1202`, `scope_records.py:94-117,145-172,490-545` |
| B3 | **The release gate would certify a different contract than production runs.** The evaluator is pinned to `SCHEMA_VERSION = 1` and re-runs the OLD all-or-nothing validator over `llm_call.raw_output`, so a partially-recovered result scores as a technical failure | `recognition_release_eval.py:52-56,216-280,304-320` |
| B4 | **Revision 1 created schema v2 and never activated it.** `_OUTPUT_SCHEMA_VERSION = 1` is hardcoded; `register_enrichment_schemas()` uses a MUTABLE `register_schema` that overwrites an existing version, so a dynamically-derived enum could mean different things on different deployments | `recognizer.py:55`, `enrich_llm.py:1902-1918`, `documents/registry.py:29-49` |

**Measured, not assumed [R2]:** 88 selectable leaves / 123 taxonomy nodes; the projected v2 schema is
~3.6 KB and passes the repository's provider-compatibility check. **The enum is therefore mandatory
in this repair, not optional hardening** — revision 1 deferred it on a size concern that does not
exist.

**Not a cause:** billing (HTTP 200), the prompt (88 ids rendered, churn listed first), model
reachability. The model was independently sloppy in a second way — two `"primary"` candidates where
the rules allow one.

## 0.3 The done-bar **[R2 — corrected]**

Revision 1's done-bar required a valid sibling to survive *even when repair fails*, while its
sequencing called Tasks 1+2 the "minimum honest fix". Those contradict: 1+2 only help when the model
repairs successfully. Resolved by making partition part of the functional fix:

> A recognition whose body carries one valid candidate and one invalid one ends as a scope over the
> valid candidate — after the model has been given its designed chance to repair, and whether or not
> that repair succeeds — with every dropped candidate named by a closed reason code, and never
> reported to the user as absence.

**Stated plainly, as the review required:** after repair exhaustion this **does** overturn the
current all-or-nothing core policy, in the bounded way §4 freezes. Revision 1 claimed it did not.
That claim was wrong.

## 1. Tasks — corrected sequence **[R2]**

### Task 0 — `RecognitionContractV2`: request identity and provenance (2 days) — **migration**

Nothing else is safe until re-running an objective is honest.

**New:** `recognition_request_hash` over `input_content_hash` + prompt content/version + schema
content/version + taxonomy content/version + semantic-validator version + model and generation
controls. `input_content_hash` keeps its current meaning (user input only) — the two answer
different questions and neither may absorb the other.

**Modify:** idempotency becomes `(intent_id, recognition_request_hash)`. An existing request **loads
and returns the stored result with no second provider call** — which also removes today's silent
double-spend. `llm_call_ref` is stored ON the recognition attempt, so the served result and the audit
row are one fact.

**Migration:** additive column + index; the old unique key stays until backfilled rows carry the new
hash. Append-only rows are never overwritten (1060/1061 discipline).

**Acceptance:**
- `test_a_changed_prompt_or_model_is_a_new_request` — same user text, different contract → new row.
- `test_an_identical_request_returns_the_stored_result_without_calling_the_provider`.
- `test_the_returned_recognition_id_is_the_row_the_response_was_built_from` — the B2 incident,
  pinned: a re-run may never return an id whose stored status disagrees with the payload.
- Migration audit against a POPULATED legacy table (the standing lesson).

### Task 1 — immutable schema v2, activated (1 day)

**New:** `("use_case_recognition", 2)` — `use_case_id` carries `"enum": [...]` of the 88 leaves,
**frozen as generated, reviewed bytes**, not derived at import. Generation is a one-off tool whose
output is committed and pinned by hash; taxonomy growth requires v3. This is the only way the same
version means the same thing on every deployment given the mutable registry (B4).

**Also:** remove model-returnable `technical_failure` from the provider schema — it is an internal
platform outcome, never an LLM classification.

**Modify:** `_OUTPUT_SCHEMA_VERSION = 2`; the schema inventory test; the evaluator (Task 6).

**Acceptance:** `test_v1_is_byte_frozen`; `test_v2_bytes_match_their_pinned_hash`;
`test_the_leaf_list_equals_the_registry_at_generation_time` (drift fails CI, loudly, rather than
silently changing meaning); `test_an_invalid_id_fails_at_the_schema_layer`;
`test_technical_failure_is_not_a_provider_status`.

### Task 2 — governed validator composition, with a failure contract (1 day)

**Modify:** `drive_audited_structured_call` gains
`validate_semantics: Callable[[Mapping], None] | None = None`; `:2099` composes schema-first then
semantics. Default `None` keeps all 12 caller modules byte-identical.

**The failure contract [R2]:**
- Only a typed `SchemaValidationError` from the semantic validator enters repair.
- **Any other exception becomes an AUDITED technical failure** — it may not escape before
  `_record_llm_call_durable`, since the provider has already been called and billed.
- Repair instructions carry **closed, value-free codes** — `UNKNOWN_USE_CASE_ID`,
  `MULTIPLE_PRIMARY_CANDIDATES`, `DUPLICATE_CANDIDATE` — and **never reflect the raw invalid id**
  back into a prompt or the UI.

**Also [R2 / B1]:** the outcome-returning wrapper (never the output-only view) additionally returns
`failure_kind` and `last_schema_valid_semantic_invalid_output`, exposed **only** after audit linkage
succeeds, and **never** for schema-invalid, provider-failed, egress-blocked or audit-degraded
results. The output-only wrapper keeps returning `None` for every invalid result.

**Acceptance:** `test_a_semantic_failure_is_repaired_not_returned`;
`test_the_registry_schema_still_fails_closed_first`;
`test_a_validator_raising_an_unexpected_error_is_audited_not_escaped`;
`test_repair_prompts_never_contain_the_invalid_value`;
`test_the_invalid_body_is_exposed_only_on_the_semantic_arm`;
`test_all_twelve_existing_call_sites_are_byte_identical`.

### Task 3 — the recognizer uses it (½ day)

Pass `validate_semantics=validate_recognition_output`; keep the post-call check as the floor; correct
the false docstring at `recognition.py:8`.

**Acceptance:** `test_the_padded_body_from_the_live_incident_now_recognises` (the exact `raw_output`
above as turn 1, clean body as turn 2 → CLASSIFIED on `customer.relationship_attrition.churn`);
`test_a_body_that_stays_invalid_after_repair_reaches_task_4`; `test_repair_is_recorded_on_the_llm_call`.

### Task 4 — strict partial partition, semantics frozen (1½ days)

Consumes Task 2's exposed post-repair body (B1).

**The rule, frozen [R2]:**
- **Candidate-local defects may be dropped**: unknown id, non-leaf primary, bad confidence/relationship
  band, malformed evidence spans.
- **Aggregate defects refuse the whole result**: duplicate ids, more than three candidates, multiple
  primaries, status incompatible with candidates, malformed status.
- `classified` after partition still requires **exactly one** primary.
- **No promotion** of a secondary to primary, ever.
- Every drop carries a closed, value-free reason code.

So the live incident — two primaries — refuses on the aggregate rule *unless repair fixed it, which
is why Task 3 comes first*. The partition rescues the single-junk-sibling case; it does not launder a
cap violation.

**Acceptance:** one case per bullet above, plus
`test_the_only_primary_being_invalid_does_not_promote_a_secondary`;
`test_nothing_valid_survives_is_unchanged`; the normalized result and its drop codes persist.

### Task 5 — the API and UI say which of five things happened (1 day)

**[R2]** `warnings` may NOT carry candidate loss: it already holds dimension warnings
(`UNKNOWN_MODELLING_CONTEXT`, `UNKNOWN_TARGET_ENTITY`) that the UI renders as *"We couldn't map part
of what you described…"*, which would misdescribe a discarded candidate.

**New contract:**
```
recognition_quality:
  disposition: clean | repaired | partially_recovered | unscoped | technical_failure
  repair_attempts: int
  dropped_candidate_count: int
  drop_reason_codes: [...]
```
**Five distinct messages** — partial recovery says *"One invalid proposal was discarded; review the
remaining scope"* and **keeps the surviving scope** (revision 1's "showing all buildable recipes" was
wrong here); repair-exhausted offers broadening; genuine unscoped, ambiguous-without-primary, and
clean each say their own thing.

**Acceptance:** a Vitest case per disposition; the honest-absence law asserted on the two that are
not absence.

### Task 6 — evaluation and rollout (2 days)

**[R2]** Version the evaluator with the contract; evaluate the **exact served, normalized result**
linked by `llm_call_ref`, retaining raw output separately for audit. Re-run the 100-case real-provider
gate, applicability recall, false-narrowing, stability and cost gates, plus repair-rate — and replay
the live incident.

## 2. Risks

- **Task 0 is a persistence change on an append-only store.** Migration audited against populated
  data; old rows never rewritten.
- **Repair costs a provider round-trip**, bounded by the existing §9.2 taxonomy. Task 6 measures the
  rate; Task 0's true idempotency removes today's silent re-spend.
- **Task 4 overturns core all-or-nothing after repair exhaustion** — stated, bounded, tested.
- **Not addressed:** why `claude-opus-4-8` padded the list (`claude-sonnet-5` 4/4 on 2026-08-14;
  `claude-opus-4-8` 0/2 on 2026-08-15). Model choice is the operator's; this plan makes the platform
  robust to either.

## 3. Sequencing

```
Task 0 (contract + persistence) ──► Task 1 (frozen schema v2)
                                        │
                                        ▼
                       Task 2 (composition + failure contract + body exposure)
                                        │
                                        ▼
                       Task 3 (recognizer) ──► Task 4 (partition) ──► Task 5 (API/UI)
                                                                          │
                                                                          ▼
                                                                    Task 6 (gates)
```

**≈ 9 days.** There is no shorter honest fix: Tasks 2+3 alone leave the done-bar unmet whenever
repair fails, and shipping them without Task 0 means a re-run can show one answer while returning
another's id.

# Phase-2 Slice 3 — Honest Feature Generation (Half A) — Design (rev. 2)

**Date:** 2026-07-18
**Status:** Draft for user review — **rev. 2** makes the design implementation-ready per a 15-finding review (all confirmed against code via a 7-area fact-gather). Findings tagged `[Fn]`.
**Scope:** **Half A only** (generation-side + carrying the honest state through the governed contract flow to the persisted contract version). Half B (the external-attestation round-trip) is deferred to its own spec.
**Predecessor:** Phase-2 Slices 1 & 2 (merged to `origin/main` `b963076`). Ingestion now stores rich per-column info; Slice 3 makes the downstream feature-suggester use it and answer honestly.

## Where this sits (unchanged from rev. 1)

Ingestion builds the catalog; feature suggestion runs later and consumes it. Slice 3 lives in `feature_assist.py` + the governed contract flow (`contract/*.py`). The 3C.1 "gate" is a *different concern* (it gates the planner classifier over shadow telemetry; never touches `feature`/`FeatureIdea`) — Slice 3 does **not** build on it, and Half B must **not** depend on its (abandoned) signing code `[F14]`.

Slice 3 is large; it will **decompose into four implementation plans** at planning time:
- **3A-i — computation contract + tri-state validator** (typed operands, the three dispositions, corrected classifications, template + FTR-routing fixes),
- **3A-ii — carry the honest state end-to-end** (Gate #1 snapshot → `ContractDraft` → MCV → `confirm_contract` → persisted `contract` version),
- **3A-iii — menu enrichment + nested field-aware egress + deterministic relevance**,
- **3A-iv — versioning (v2 schemas, threaded versions, v1/v2 serializers, flag byte-identity) + the real-provider quality gate**.

## Problem (rev. 1, restated precisely)

`_menu` sends the LLM only `object_ref/table/column/concept/domain` (it drops even the `definition` that `_candidate_columns` selects). `_validate_idea` is **binary** (`(FeatureIdea, None)` | `(None, Rejection)`) — so an FTR numeric feature (operational type permanently `unknown`) is silently rejected forever, and there is no place to say "plausible, verify these facts."

---

## 1. The typed computation contract `[F1]`

`FeatureIdea` today is `name, description, derives_from: list[str], aggregation: str|None, grain_table: str|None, derives_pairs, verification, critic_note, rationale` — `derives_from` is one undifferentiated list, so **no slot ties a requirement (e.g. `TYPE_IS_NUMERIC`) to the specific measure column.** Add a bounded, typed operand structure:

```
operation_kind: str            # e.g. "sum" | "count" | "count_distinct" | "avg" | "ratio" | "recency"
measure_refs:  tuple[(catalog, object_ref), ...]   # the columns being aggregated
grain_ref:     (catalog, object_ref) | None        # the grain the feature is computed per
time_ref:      (catalog, object_ref) | None        # the point-in-time column
window:        str | None                          # e.g. "30d"
grouping_refs: tuple[(catalog, object_ref), ...]   # any group-by columns
```

Requirements attach to a **named operand** (e.g. `TYPE_IS_NUMERIC` on `measure_refs[0]`, `GRAIN_IS_UNIQUE` on `grain_ref`). The permissive v1 output schema stays; the LLM proposes, the deterministic layer (below) fills the typed operands by resolving the proposal against the catalog.

## 2. The three-state validator `[F1]` + corrected classification

`_validate_idea` gains a **third disposition**. It returns one of:
- `DESIGN_CHECKED` — structurally safe with the authority available now,
- `NEEDS_EXTERNAL_VALIDATION` — plausible, carrying typed **requirements** attached to named operands,
- `REJECTED` — deterministically invalid / provably wrong / unauthorized.

New `RejectCode`-adjacent vocabulary. **Requirement codes (closed):** `TYPE_IS_NUMERIC`, `GRAIN_IS_UNIQUE`, `TEMPORAL_IS_POPULATED`, `TEMPORAL_LAG_BOUNDED`, `JOIN_CONNECTIVITY`, `UNIT_CONSISTENT`, `CURRENCY_CONSISTENT`, `ADDITIVITY_SUPPORTS_OPERATION`.

**Classification (corrected per `[F6]`/`[F7]`/`[F8]`):**

| Situation | Disposition |
|---|---|
| `UNGROUNDED` / `AMBIGUOUS_CATALOG` / `UNKNOWN_COLUMN` (operand doesn't exist/resolve) | `REJECTED` |
| `LEAKAGE` (derives from the target) | `REJECTED` |
| `STALE` (catalog drift watermark already fails freshness — a stored, verifiable fact) | `REJECTED` |
| Numeric op on a measure whose **operational** `data_type='unknown'` but **declared_type is numeric** | `NEEDS_EXTERNAL_VALIDATION` → `TYPE_IS_NUMERIC` |
| Numeric op on a measure whose declared_type is **non-numeric** | `REJECTED` |
| Sum on a measure with a **confirmed** semi/non-additive `additivity` | `REJECTED` |
| Sum on a measure with **unresolved** `additivity` `[F6]` | `NEEDS_EXTERNAL_VALIDATION` → `ADDITIVITY_SUPPORTS_OPERATION` |
| Grain feature whose `grain_ref` is **proposed-not-confirmed** | `NEEDS_EXTERNAL_VALIDATION` → `GRAIN_IS_UNIQUE` |
| Windowed feature whose `time_ref` is **declared-not-confirmed** | `NEEDS_EXTERNAL_VALIDATION` → `TEMPORAL_IS_POPULATED` (+ `TEMPORAL_LAG_BOUNDED` if a lag basis applies) |
| Two operands declare **different** unit/currency (a positive contradiction) `[F7]` | `REJECTED` |
| Operand unit/currency **absent/unknown** across a combining op `[F7]` | `NEEDS_EXTERNAL_VALIDATION` → `UNIT_CONSISTENT` / `CURRENCY_CONSISTENT` (distinct codes) |
| Cross-table op whose join is authorized-but-unverified (concrete endpoints known) | `NEEDS_EXTERNAL_VALIDATION` → `JOIN_CONNECTIVITY` |
| Cross-table op with **no structural path** or a **read-scope-denied** hop `[F8]` | `REJECTED` |

**FTR routing fix `[F10]`:** `route_strategies` gates numeric/ratio strategies on `graph_node.data_type` (unknown for FTR) and **never reads `declared_type`** (which *is* persisted). It must also `SELECT declared_type` and treat it as a **numeric hint** — enabling the numeric strategy so the feature is *proposed* — while operational `data_type` stays `unknown`, so the validator still returns `NEEDS_EXTERNAL_VALIDATION`. Without this, FTR numeric features are never generated and never reach the honest state.

**Template fix `[F9]`:** `_template_candidates` (`gate1.py:149`) appends the **pre-validation** `_idea_from_grounded` object, discarding the validator's returned idea. It must append the validator's **returned** idea (carrying status + requirements) — otherwise the template half of the considered set loses the honest state.

## 3. Carry the honest state through the governed flow `[F2]`/`[F3]`

**Not just `/features/recommend`.** The real product path is Gate #1 considered-set → persisted snapshot → chosen feature → `ContractDraft` → MCV → `confirm_contract` → versioned `contract` row. The state must survive all of it. Concrete edits:

1. **Snapshot round-trip `[F2]`:** `_idea_json` (`gate1.py:262`) already serializes `verification/critic_note/rationale`; `_idea_from_json` (`gate1.py:310`) **omits** them, so they fall back to defaults (`DESIGN-CHECKED`) — the tri-state is lost right after Gate #1. Extend `_idea_from_json` to restore the new `validation_status` + typed `requirements` (and the existing fields) from the snapshot.
2. **`ContractDraft` `[F3]`:** it has no status/requirements field (`author.py:23`). Add `validation_status` + `requirements`, populated in `draft_contract`.
3. **MCV `[F3]`:** `validate_minimum` (`review.py:31`) collapses `_validate_idea` to `tuple[bool, list[str]]`, discarding the idea. Change it to carry the typed requirements + status forward, not a boolean.
4. **Confirmation `[F3]` (corrected — `validation_status` is a SEPARATE axis, not the `verification` writes).** `confirm_contract` (`govern.py:42`)'s three hardcoded `"DESIGN-CHECKED"` writes target the **hyphenated `verification` column**, which a CHECK constraint (migration `0973`) restricts to `UNVERIFIED/DESIGN-CHECKED/DATA-CHECKED/USEFULNESS-CHECKED` — writing an underscore `validation_status` value there would violate the CHECK *and* the cross-cutting invariant forbids repurposing `verification`. So: leave the `verification` writes as they are (a design-check is still *earned* on that axis), and additionally write the honest `validation_status` + `requirements` into the **new** `contract.validation_status`/`requirements` columns. **Persist the status from the confirm-time MCV re-run** (the authoritative check), not the stale draft value — a re-run that legitimately upgrades `NEEDS_EXTERNAL_VALIDATION`→`DESIGN_CHECKED` did so because a governed fact was confirmed (a real change, not a silent promotion), and a downgrade is caught.
5. **Persistence (the immutable versioned artifact):** add `contract.validation_status text` (+ CHECK in `VALIDATION_STATES`, the underscore vocab) and `contract.requirements jsonb` to the `contract` table (re-confirm = a new row, so history is preserved). This is a **new axis distinct from `verification`**.

**Do NOT reuse `governance/attributes.py` `VERIFICATION_STAMPS`** — that is the SP-0 `feature_versions` vocabulary, explicitly distinct from the overlay contract vocabulary and not on this path. `validation_status` is a new, separate axis from the existing hyphenated `verification` stamp.

## 4. `OperationalColumnFacts{value, authority, provenance}` — concrete readers `[F5]`

Corrected model: **the decision log stores only a value HASH, never the raw value** — so a reader gets *authority* (eligibility), and the *value* comes from the flat `graph_node` column. Per field:

- **`additivity`** — value = `graph_node.additivity`; authority = `is_feature_eligible(logical_ref, "additivity")` (latest non-retired decision with a `load_bearing_value_hash`); provenance = `additivity_decision_id`.
- **structural type (`logical_representation`)** — authority = `is_feature_eligible(..., "logical_representation")`; provenance = `logical_type_decision_id`; the usable value for a numeric check is `graph_node.data_type` (operational) vs `declared_type` (hint).
- **`is_grain` / `is_as_of`** — value = the flag; authority = flag **AND** `grain_fact_event_id` / `availability_fact_event_id` non-null (governed-VERIFIED vs file-declared); provenance = that fact-event id (the OVERLAY_FACT `confirmed_event_id`, **not** the decision log).
- **hint-only (no policy, no decision):** `unit`, `currency`, column-level `entity`, `declared_type` (`data_type`). A hint may only tighten (reject / needs-check), never clear a check.
- **`semantic_type` / `temporal_role`** are **decision-only** (governed authority via `is_feature_eligible`) but **unprojected** (no flat display column) — so they can *clear an authority check* but have no menu-sendable value; do not present them as menu values.

**No reader dereferences a decision's `load_bearing_value`** (only its hash exists). The rule stands: only a `governed` (eligible) value clears a check; `declared_type` never approves a numeric op.

## 5. Menu enrichment + the nested field-aware egress adapter `[F4]`

**The sample-safety gap is real.** The feature menu's top-level key is `columns` (not `column_profiles`), so `_redact_free_text_meta` — which only scans `_FREE_TEXT_META_KEYS` + `column_profiles[*].business_definition` — is a **no-op** on it. The only backstop, `assert_llm_safe`, scans PII patterns and fails the whole call closed but **does not strip sample clauses**. So raw definitions in the new menu would egress unsanitized.

**Fix:** a dedicated **nested field-aware egress adapter**, invoked inside `audited_structured_call` (`enrich_llm.py:475`, before `build_llm_inputs`/`assert_llm_safe`/dispatch, so every `_call_raw` path is covered). It traverses `columns[*]` and any per-table context block:
- **definition-kind** fields (`definition`, `semantic_terms`, `table_definition`) → `sanitize_definition` (sample-clause strip + fail-closed data-marker scan + PII redaction; list-prose per item), with the `{path, sanitizer_version, state, removed_count}` audit + PII spans reaching `llm_call.input_redaction` (as Slice 1 does for Pass B);
- **structural** fields (`object_ref/table/column/concept/domain`, refs, enums) → exact-key allowlist + length bound, never sample-stripped;
- **fail-closed shape gate:** any unclassified key anywhere blocks/excludes the item; a definition the sanitizer blanks → no dispatch + an audited `EGRESS_BLOCKED`.

**Menu content:** widen `_candidate_columns` to also select `data_type, declared_type, semantic_terms, entity, additivity, unit, currency, is_grain, is_as_of` + the `*_fact_event_id` links; stop `_menu` discarding; per column emit the fields wrapped by `OperationalColumnFacts` (`{value, authority}`), never a bare display value. **Per-table context** — one block per table (table definition, confirmed grain columns + as-of column requiring a non-null `*_fact_event_id`, primary entity tagged advisory) — assembled **only from the already-authorized candidate rows** (never a second unscoped query); if all columns of a table are read-scope-excluded, emit no context for it.

## 6. Deterministic relevance selection `[F13]`

**No LLM call** (`recognize()` is a provider call; `known_entities()` is only a vocabulary). Objective source, in priority:
1. **Governed route** — the already-cleaned `ConfirmedScope` (primary + secondary leaf ids, `target_entity`, `modelling_contexts`), available at `contract.py:312-342` *before* `build_considered_set`.
2. **Direct-assist route** — the explicit `entity` param + `objective` string (`assist.py` `RecommendIn`/`RefineIn`).
3. **Lexical fallback** (objective free-text only) — lowercase-tokenize the objective → normalized token set; score each candidate by shared-token count; stable order by `(-score, object_ref ascending)`.

**Mandatory set (always included):** confirmed grain columns, the as-of column, columns whose `entity` matches the objective entity. **One hard byte budget** on the assembled batch; select mandatory first, then by score until the budget, summarize the rest. **Overflow → `CONTEXT_TOO_LARGE`, do NOT chunk** `[F13]` — each `audited_structured_call` is one audited `llm_call`; chunking would need N calls + cross-chunk dedup, defeating the single fail-open audit, whereas relevance ordering already floats the highest-relevance items into the one bounded call. Log the dropped/summarized count.

## 7. Authorization threading + join outcomes `[F8]`

`find_join_path` (`join_path.py:38`) returns `list[JoinStep] | None`, and `None` **collapses** authorized-but-unverified, no-path, and read-scope-denied into one indistinguishable result (and a file-declared edge with a null fact key traverses as operational). Change its return to a **discriminated `JoinOutcome`**: `OPERATIONAL(steps)` (VERIFIED or declared) → clears; `UNVERIFIED(steps, endpoints, fact_keys)` → `NEEDS_EXTERNAL_VALIDATION`/`JOIN_CONNECTIVITY` with concrete endpoints; `NO_PATH` → `REJECTED`; `DENIED(hidden hop)` → `REJECTED`. Implementation: drop the `approved_join_status='VERIFIED'` and sensitivity predicates from the edge fetch; select `approved_join_fact_key`/`approved_join_status` + endpoint visibility per edge and **classify each hop in Python**. Thread the caller's `roles` through `_validate_idea`/`_vet`/refinement/contract MCV (they don't carry roles today) so a `DENIED` hop is distinguishable and rejects.

## 8. Versioning + flag byte-identity `[F11]`/`[F12]`

- **v2 output schemas:** register `("feature_ideas", 2)` (+ `feature_recipe`/`leakage`/`feature_set_rec` v2). v1 stays permissive; **semantic validation remains code-side** in `_validate_idea`.
- **Threaded versions:** add `prompt_version:int`/`schema_version:int` to `_call_raw` and thread to `audited_structured_call` (which already accepts them, defaulting 1); pass them at all **7** call sites (recommend/refine/recommend-set/recipe/leakage/feature-set) so `llm_call` records the real numeric version instead of a hardcoded 1 masked by a `…_v1` prompt_id string.
- **v1/v2 response serializers:** the assist routes return the shared `FeatureIdea` dataclass directly, so any new field silently leaks into the flag-OFF response and the considered-set snapshot. Add explicit v1/v2 serializers; **flag-OFF response + snapshot must be byte-identical to pre-Slice-3**, gated by the flag captured once at the route.
- **Flag:** a single env flag (`FEATUREGEN_FEATURE_CONTEXT`, default off) gates the whole enrichment (menu widening, tri-state emission, versioned shape).

## 9. Quality gate — concrete thresholds `[F15]`

Hermetic tests **plus** a **manually-run, versioned** real-provider evaluation (a key-gated test that skips is not itself the gate). Define: a curated gold set of **≥ 40** objective→expected-feature cases (versioned artifact under `tests/eval/`); a runnable command; and delivery bars — **zero** unsafe-accepted features (no `DESIGN_CHECKED` on an unverified numeric op); **zero** restricted/unsanitized outbound fields; grounded-acceptance **≥ baseline** (non-regression); **≥ 15% relative** lift in expert-relevance vs the thin menu; token/cost/latency regression **≤ 25%**; pinned model/settings; results written to a versioned report.

## Cross-cutting invariants (must hold)

- The deterministic validator is the sole safety authority; a hint never clears a check (only governed/eligible values do); `declared_type` never yields `DESIGN_CHECKED` for a numeric op.
- Sample-safety via the nested field-aware egress adapter (§5); read-scope preserved (all context from the authorized candidate set); flag-off byte-identical (§8).
- No governance regression; the gauntlet is strengthened, never bypassed; `validation_status` is a new axis, not a repurposing of the existing `verification` stamp.

## Out of scope (Half B — deferred to its own spec)

- An external execution platform consuming the requirements, verifying against real data, and returning a **signed attestation**. *(The future protocol may reuse the general "re-derive before trust" principle, but must NOT depend on 3C.1's `authority_sign_gate` or its abandoned signing design `[F14]`.)*
- Minting the next stamp (promoting a feature out of `NEEDS_EXTERNAL_VALIDATION` on a valid attestation); a per-feature attestation table; `USEFULNESS-CHECKED` (backtest-proven).

## Global constraints

- Change `feature_assist.py`, `contract/{gate1,author,review,govern}.py`, the assist routes, `join_path.py`, `enrich_llm.py` (the nested egress + v2 schemas), and add the `contract` columns migration. Reuse Slice-1's sanitizer primitives, the field-authority kernel + `is_feature_eligible`, `known_entities()`, and the OPERATIONAL/RECOMMENDATION ceiling — no parallel vocabulary, no unscoped query, no oversized dispatch, no chunking.
- Implementers on **Fable**, reviews on **Opus**.

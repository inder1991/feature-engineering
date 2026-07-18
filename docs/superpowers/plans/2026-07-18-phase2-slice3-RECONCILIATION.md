# Slice 3 — Cross-plan RECONCILIATION (BINDING for all four sub-plans)

The four Slice-3 plans (`3a-i` computation+validator, `3a-ii` persistence, `3a-iii` menu+egress+relevance, `3a-iv` versioning+eval) were authored in parallel against the shared interface contract. These decisions resolve every cross-plan seam + the ambiguities the authors flagged. They OVERRIDE any drift in an individual plan.

## Branch chain
`3a-i` off `origin/main` (`b963076`) → `3a-ii` off the `3a-i` tip → `3a-iii` off `3a-ii` → `3a-iv` off `3a-iii`. Each merges the prior sub-plan's result. Implementers **FABLE**, reviews **OPUS**.

## 1. `validation_status` is a SEPARATE axis from `verification` (spec §3.4, corrected)
The three hardcoded `"DESIGN-CHECKED"` writes in `confirm_contract` target the **hyphenated `verification` column**, CHECK-constrained by migration `0973` to `UNVERIFIED/DESIGN-CHECKED/DATA-CHECKED/USEFULNESS-CHECKED`. Do **not** write underscore `validation_status` values there. Instead: leave the `verification` writes intact (design-check still earned on that axis) **and** write `validation_status`/`requirements` into the **new** `contract.validation_status`/`contract.requirements` columns (3a-ii migration `1002`). `validation_status` CHECK = `VALIDATION_STATES` (underscore vocab). Never repurpose `verification`. (3a-ii already resolved this — it is BINDING for the whole slice.)

## 2. Confirm persists the CONFIRM-TIME re-run status, not the stale draft value
`confirm_contract` re-runs the MCV. Persist **that re-run's** `validation_status` + `requirements` to the new columns (not `draft.validation_status`). Rationale: a re-run that upgrades `NEEDS_EXTERNAL_VALIDATION`→`DESIGN_CHECKED` did so because a governed fact was confirmed between draft and confirm (a real change), and a downgrade (evidence staled) is caught. → 3a-ii Task 5 + the e2e assert `contract.validation_status` == the re-run result.

## 3. `find_join_path` keeps its `list|None` façade; the NEW producer is `classify_join_path`
3a-i honors the `JoinOutcome` TYPE via a new `classify_join_path(conn, ...) -> JoinOutcome`; `find_join_path` stays a byte-identical `list[JoinStep] | None` façade so its ~40 existing callers/assertions (passc/join_governance/e2e/3B `author.py`) don't break. `_validate_idea` uses `classify_join_path`. Do NOT change `find_join_path`'s return type.

## 4. `_validate_idea` signature
Keep the existing grounding params (`known`/`src_of`, used by MCV/refine/template call sites) and **append `roles=()`**; change only the return to the tri-state contract (returned `FeatureIdea` carries `validation_status`+`requirements`; `REJECTED` still returns `(None, Rejection)`). No positional-shape refactor of the 4 call sites.

## 5. The flag helper is defined ONCE (in 3a-iii) and reused (in 3a-iv)
`feature_context_enabled()` reads env `FEATUREGEN_FEATURE_CONTEXT` (default off). **3a-iii defines it** (first consumer — the menu). **3a-iv reuses it** (serializers + snapshot gate) — do NOT redefine. The env is immutable within a request, so reading it deep in the snapshot path (`_idea_json`, matching the existing `gate1._scoped_applicability_enabled()` idiom) is acceptable rather than threading a boolean through the Gate #1 chain.

## 6. Version a schema to v2 ONLY if its INPUT shape actually widens under the flag (3a-iv)
`feature_ideas` widens (the enriched menu) → v2 when flagged. For `leakage`/`recipe`/`feature_set` — only bump to v2 if that call's catalog_metadata shape actually changes under the flag; otherwise pass a literal `1`. The `feature_candidate_critique` site stays v1. Threading the version params through `_call_raw` to all 7 sites is required regardless (so the recorded numeric version is honest); the v2 VALUE is passed only where the shape changed.

## 7. The eval's cost/token bar must read the REAL usage keys (3a-iv)
The `≤25%` token/cost bar reads `llm_call.cost_metadata` — confirm the actual key names the Claude client records (e.g. `input_tokens`/`output_tokens`) against a real `llm_call` before trusting the bar; a wrong key reads 0 and passes vacuously. This is verified on the **keyed** eval run, not in CI (the eval is key-gated + manual).

## 8. Non-`public` schema authority (3a-i, accepted edge)
`read_column_facts` rebuilds the decision-log `logical_ref` public-flattened (matching how `graph_node` stores object_refs). A non-`public`-schema source could miss its schema-preserving decision and fall back to `authority="hint"` — **conservative** (never wrongly clears a check). All in-repo uploads use `public`; a schema-preserving reader is a later refinement, not Half A.

## 9. Operand-driven dispositions are a later tightening (3a-i, accepted)
The typed `operation_kind`/`measure_refs` operands are POPULATED, but the numeric/windowed dispositions still key off the aggregation-string heuristics (`_needs_numeric`/`_is_windowed`) inherited today. Half A stamps the operands + requirements correctly; making `operation_kind` (from a structured LLM schema) the disposition driver is a follow-on.

---

## REVIEW FIXES (BINDING — from the 5-agent plan review; apply during execution, OVERRIDE the plan text)

### CRITICAL
- **RF-C1 (3a-ii, confirm persists the RE-RUN status).** `confirm_contract` re-runs `validate_minimum` (→ `MinimumCheck`). Persist **that re-run's** `validation_status` + `requirements` into `contract.validation_status`/`contract.requirements` — NOT `draft.validation_status`/`draft.requirements`. The INSERT binds `check.validation_status` and `requirements_to_json(check.requirements)`. (Reconciliation #2 already decided this; the plan text does the opposite.)
- **RF-C2 (3a-ii, the e2e must be REAL).** Task 5's confirm test + Task 6 e2e must exercise a genuinely **operational-unknown** column: a `CanonicalRow(... type=UNKNOWN_TYPE ...)` whose glossary `declared_type` is numeric (the FTR case), so the confirm-time re-run itself produces `NEEDS_EXTERNAL_VALIDATION` → `TYPE_IS_NUMERIC`. Rework the `_bank` fixture accordingly (its current `balance` is `'numeric'` = operationally KNOWN, which never yields `TYPE_IS_NUMERIC`). Delete the hand-invented, self-contradictory detail string; assert `contract.validation_status == 'NEEDS_EXTERNAL_VALIDATION'` and `contract.requirements` == the re-run's requirements.
- **RF-C3 (flag helper, ONE public definition).** Define `feature_context_enabled()` — **public name**, in `feature_assist.py`, reading env `FEATUREGEN_FEATURE_CONTEXT` via `(os.environ.get("FEATUREGEN_FEATURE_CONTEXT","").strip().lower() in {"1","true","yes","on"})` — **once, in 3a-iii**. 3a-iv **imports and reuses** it (delete 3a-iv's `feature_context_enabled()` redefinition; delete 3a-iii's private `_feature_context_enabled` name). Every call site uses the one public helper.

### IMPORTANT
- **RF-I1 (3a-i, valid join status).** Join tests must use a value in the `graph_edge_approved_join_status_check` vocab: `DRAFT/PARTIALLY_CONFIRMED/VERIFIED/REJECTED/STALE/REVERIFY`. "Authorized-but-unverified" = a fact-linked edge (`approved_join_fact_key` non-null) with status **`'DRAFT'`** (or `PARTIALLY_CONFIRMED`). Never `'PROPOSED'` (not a folded status; violates the CHECK).
- **RF-I2 (3a-i).** Delete the placeholder test flagged in the plan before running.
- **RF-I3 (3a-ii, import order).** Do NOT import `MinimumCheck` at the top of the Task-1 test file — it is introduced in Task 4. Introduce that import with Task 4; earlier tasks import only symbols that exist at that task (or split per-task test files).
- **RF-I4 (3a-ii, FK).** Task 1 must seed a real `feature` row (satisfying `contract_feature_id_fk`, migration 0972) before inserting a `contract` row.
- **RF-I5 (3a-iii, byte budget).** The byte-budget test must compute the expected budget INCLUDING the per-table `table_context` grain block that `select_relevant_context` always adds for the mandatory grain column.
- **RF-I6 (3a-iii, egress adapter covers BOTH shapes — do NOT fail-close the draft path).** The nested `sanitize_feature_context` fires on every `audited_structured_call`, including `overlay.contract.draft` whose `columns` is `_column_defs(...)` (a different-but-safe dict shape). Extend the adapter's allowlist to cover BOTH the feature-menu shape AND the contract-draft column-def keys, sanitizing definition-kind fields in each; only a genuinely-unknown key blocks. This *improves* sample-safety on the draft path rather than fail-closing a legitimate call.
- **RF-I7 (3a-i, coerce bool).** `is_grain`/`is_as_of` are BOOLEAN flat columns; `read_column_facts` must return `value` as `str | None` (render the flag, e.g. `"true"`/`"false"`), so the egress wrapper (`_fact_wrapper_ok`, `str|None`) accepts it — otherwise every enriched grain/as-of column fail-closes the flag-ON dispatch.
- **RF-I8 (3a-iv, v2 per-site).** Pass a literal `1` at the `leakage_check` + `recommend_set` sites (their inputs do NOT widen under the flag); pass v2 only where the menu widens (`feature_ideas`; `recipe` only if its input widens). Per reconciliation #6.
- **RF-I9 (seam — this REVISES reconciliation #5's "snapshot gate").** Do **NOT** flag-gate `_idea_json` / the considered-set snapshot. The internal governed snapshot **always** serializes `validation_status`+`requirements` (additive; produced by the always-on validator), so 3a-ii's flag-OFF snapshot round-trip works. Flag-OFF **byte-identity applies to the `/features/recommend` RESPONSE + the outbound LLM request payload only**, not the internal snapshot. 3a-iv Task 4 becomes: extend `_idea_json` to always serialize the new fields; gate only the response serializer + the enriched menu.

### MINOR (per-task review + TDD will catch; noted)
gold-set/eval-fixture column mismatch (3a-iv); a ruff redefinition risk if RF-C3 not applied; the eval cost-key caveat (reconciliation #7). Not blocking.

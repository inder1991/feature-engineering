# E4a (revised) — Close the unit loop: AI proposes, human confirms, feature stays honest

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A feature whose unit isn't declared in the file is **still created and usable**, marked *"unit not confirmed — AI suggests X"*, and a human can confirm that suggestion in one action to clear it.

**Why this supersedes the earlier E4a/E4b scope.** Task 1's measurement showed confirming the AI's grain/as-of unblocks 9 features but lifts `DESIGN_CHECKED` by zero, because `UNIT_CONSISTENT` fires on operands that can never carry a unit — and because no one can supply a unit at all today (the file doesn't declare it, `unit` isn't human-editable, and the LLM is forbidden). This plan closes that loop the safe way.

**The safety insight that shrinks the work.** The gauntlet reads `unit`/`currency` from **`graph_node` only** (`_column_meta`, `feature_assist.py:532-544` — a plain SELECT). So if an AI-proposed unit **never projects into `graph_node.unit`**, it is *structurally impossible* for it to silently clear the check. We keep `_MEASURE_ANNOTATION.display_rule` as `_SOURCE_OR_HUMAN` (LLM excluded from resolution), let the LLM write `unit` **evidence** only, and surface it as a suggestion on the requirement. No guard needed — the hole is designed out.

## Global Constraints

- **The LLM must NEVER reach `graph_node.unit`/`.currency`.** `_MEASURE_ANNOTATION.display_rule` and `operational_rule` stay `_SOURCE_OR_HUMAN`. The LLM writes evidence; it never wins resolution. A test must prove an `llm/proposed` unit does **not** clear `UNIT_CONSISTENT`.
- **Never block a feature.** A missing/unconfirmed unit yields a *requirement* (`NEEDS_EXTERNAL_VALIDATION`), never a rejection. `MIXED_UNITS`/`MIXED_CURRENCY` hard rejects stay exactly as they are.
- **Narrow only on structural grounds.** Do NOT narrow by concept/analytical role — that would rest a safety gate on an AI guess.
- Reuse `_write_llm_field_evidence` + existing reconciliation; reuse `apply_field_correction`. Fork nothing.
- Test fixtures use a **non-generic source name**.
- Judge the suite by **failure-set delta** vs a fresh `origin/main` baseline.

---

### Task 1: Stop asking the meaningless questions (structural narrowing)

**Files:** Modify `src/featuregen/overlay/upload/feature_assist.py` (~`:702-713`); Test: extend `test_gating_confirm_lift.py` / `test_feature_assist.py`.

Two structural corrections to when `UNIT_CONSISTENT`/`CURRENCY_CONSISTENT` are minted:
1. **Count measures, not bound columns, for "is this combining?"** The gate is `if len(pairs) >= 2`, but `pairs` includes the grouping key and the time anchor — so `AVG(txn_amt) BY cif_id OVER 30d [as_of_dt]` (3 pairs) is treated as combining when it combines nothing. With a **single measure there is nothing to mix**: the result inherits that column's unit and cannot be corrupted. Gate on the count of measure operands instead.
2. **Skip the grain and time operands in the per-operand loop.** They are the `GROUP BY` key and the window boundary — never arithmetically combined, so an unknown unit on them cannot make an aggregation wrong. Both are already resolved in this function (`grain_operand`, `time_operand`).

- [ ] **Step 1: Failing tests** — (a) a single-measure windowed feature mints NO unit/currency requirement; (b) a genuine **two-measure** feature with an unknown unit on a MEASURE **still** mints `UNIT_CONSISTENT` for it *(the anti-silent-clear test — it must fail if anyone widens the exclusion)*; (c) the grain/time operands never appear as unit-requirement operands; (d) genuinely mixed units across measures still hard-reject `MIXED_UNITS`.
- [ ] **Step 2: Run → fail** for the right reasons.
- [ ] **Step 3: Implement** both corrections. Nothing else in the disposition changes.
- [ ] **Step 4: Run → pass**, plus `test_feature_assist.py`, the contract/gate suites, and `test_gating_confirm_lift.py` (which PINS the old counts — re-record the real numbers).
- [ ] **Step 5: Commit** — `fix(e4a): demand a unit only where units can mix`

**Report the new before/after `DESIGN_CHECKED` numbers.**

---

### Task 2: The LLM proposes `unit`/`currency` when the file doesn't declare them

**Files:** Modify `src/featuregen/overlay/upload/enrich.py` (+ `enrich_llm` schema, `enrich_config`, the ingest stage wiring); Test: new.

The LLM drafts a unit/currency for measure columns whose file declares none, written as `llm/proposed` **evidence** via the existing `_write_llm_field_evidence` + reconciliation. Mirror the `definition`/`domain`/`synonyms` stages exactly (client-present block, savepoint, `audited_*` call — sanitization is NOT automatic for a raw `client.call` — `_enrichment_outcome`/`record_stage`, `CANONICAL_STAGES`, the `skipped_no_client` loop, `enrich_config` defaults).

**Do NOT touch `_MEASURE_ANNOTATION`'s `display_rule`/`operational_rule`.** The LLM value must remain unable to win resolution or reach `graph_node.unit`.

- [ ] **Step 1: Failing tests** — an AI unit is written as `llm/proposed` `unit` evidence at the schema-preserving ref; **`graph_node.unit` is UNCHANGED** by it; and **`UNIT_CONSISTENT` still fires** for that operand *(the load-bearing safety test)*; a source-declared unit is never overwritten.
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** the drafter + writer + stage wiring.
- [ ] **Step 4: Run → pass** + the enrichment/ingest suites.
- [ ] **Step 5: Commit** — `feat(e4a): the LLM proposes a unit when the file declares none (evidence only)`

---

### Task 3: Surface the suggestion, and let a human confirm it

**Files:** Modify `field_policies.py` (`_MEASURE_ANNOTATION.human_editable`), `field_resolution.py` (`_DISPLAY_COLUMN` for unit/currency), a migration if a `*_decision_id` link is required, the requirement detail; Test: new.

Two halves:
- **Surface it.** The `UNIT_CONSISTENT` requirement should carry the AI's suggestion so the card reads *"unit not confirmed — AI suggests AED"* rather than a bare "unit unknown". Use the existing `Requirement.params` extension seam (sorted tuple, emitted only when non-empty, schema-versioned) — **and fix `api/feature_serialize.py` dropping `params`/`schema_version`**, or the suggestion never reaches the UI.
- **Let a human confirm it.** Set `human_editable=True` on `_MEASURE_ANNOTATION` (a real `FieldPolicy` field, default `False`) so `apply_field_correction` stops 403-ing, and add `unit`/`currency` to the resolver's `_DISPLAY_COLUMN` (+ `_DECISION_LINK_COLUMN` and a migration for `*_decision_id` if the projection requires it) so a **human-confirmed** value reaches `graph_node.unit` and the requirement clears.

⚠️ **PROJECTION-WIPE HAZARD — must be tested.** Giving `unit` a display projection makes the resolver authoritative over a column `build_graph` also populates. A ref whose only unit evidence is `llm/proposed` resolves `display_value=None` (LLM is excluded from the display rule) and could **NULL a real source-declared unit**. Technical CSVs write SOURCE unit evidence (safe); a glossary upload may set `graph_node.unit` with no backing evidence (**not** safe). **Required test: an AI unit proposal must not wipe a source-declared `graph_node.unit`.** If the resolver would wipe it, scope the projection so it cannot (e.g. only project when a source/human value resolves).

- [ ] **Step 1: Failing tests** — (a) the requirement carries the AI's suggested unit and it survives serialization to the API; (b) a human `confirm_existing` on the AI's unit evidence succeeds (no 403) and **clears** `UNIT_CONSISTENT`, moving the feature to `DESIGN_CHECKED`; (c) an `llm/proposed` unit alone still does **NOT** clear it; (d) **the projection-wipe test above.**
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run → pass** + the authority/resolution/policy suites (`_MEASURE_ANNOTATION` is load-bearing well beyond this feature — report their output explicitly) + the full-suite delta.
- [ ] **Step 5: Commit** — `feat(e4a): a human can confirm the AI's unit, clearing the requirement`

---

## Self-Review (author checklist — completed)
- **The silent clear is designed out, not guarded:** the LLM never enters `_MEASURE_ANNOTATION`'s display/operational rules, so it can never reach the column the gauntlet reads. Pinned by a test in both Task 2 and Task 3.
- **Narrowing is structural only** (measure count; grain/time exclusion) — never concept- or role-based.
- **Features are never blocked** — unconfirmed units yield a requirement, and the hard mixed-unit rejects are untouched.
- **The loop actually closes:** propose (T2) → surface (T3) → confirm (T3) → clear. T1 removes the noise that would otherwise bury it.
- The projection-wipe hazard is named with a required test rather than assumed away.

## Success criteria
- A feature with an undeclared unit is **created**, marked *"unit not confirmed — AI suggests X"*, and never blocked.
- An `llm/proposed` unit **never** clears `UNIT_CONSISTENT` and **never** reaches `graph_node.unit`.
- A human confirms the AI's suggestion in one action → the requirement clears → the feature reaches `DESIGN_CHECKED`.
- Single-measure features ask for no unit at all; genuine multi-measure ones still do.
- Mixed units across measures still hard-reject.
- Full-suite failure-set delta vs `origin/main`: **empty**.

## Deferred
`logical_representation` (its check reads the physical type; policy excludes even a human). Bulk/by-convention confirm. The P4 grounding-performance fix. Caching.

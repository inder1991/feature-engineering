# E4a — Gating Fields: measure, then open `additivity` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move features from `NEEDS_EXTERNAL_VALIDATION` to `DESIGN_CHECKED` by letting a human **confirm an AI proposal in one click** on the fields that actually gate a feature — and produce the program's first real number.

**Architecture:** Most of this already exists. Grounding verified: `OVERLAY_TABLE_SYNTH="1"` is already live in the deployed configmap; `table_synth` already proposes **grain** and **availability_time** as human-gated typed facts; the **"Grain & availability" tab already renders the queue** in `GovernanceReviewScreen`; `POST /governance/table-facts/{fact_key}/confirm` already projects a VERIFIED fact into `is_grain`/`is_as_of` + `*_fact_event_id`, which is exactly what C1 reads to clear `GRAIN_IS_UNIQUE` and `TEMPORAL_IS_POPULATED`. So E4a is **measure first, then one small build**.

**Tech Stack:** Python 3.12, psycopg, FastAPI, pytest; React/TypeScript + Vitest.

## Scope & Deferrals

- **IN:** (1) measure the existing chain end-to-end and produce the before/after number; (2) open `additivity` to the LLM as a proposal a human can confirm; (3) expose the CAS anchor so "accept the AI's value" is genuinely one click.
- **OUT — E4b (own slice):** `unit`/`currency`. They need a migration (`unit_decision_id`/`currency_decision_id`), resolver projection entries, a policy change, a writer, **and** — mandatorily in the same change — switching `_validate_idea`'s unit/currency reads from flat-presence to a governed/producer-aware read. Shipping the first three without the last creates a real **silent clear**. There is also a projection-wipe hazard (a glossary upload's `build_graph`-only unit would be NULLed by the first LLM proposal).
- **OUT — permanently:** `logical_representation`. Its check reads the **physical** `data_type`, and its policy excludes even a human confirm by design.
- **OUT — NFR:** the P4 grounding-performance fix, caching, dashboards, bulk confirm.

## Global Constraints

- **AI PROPOSES, A HUMAN DISPOSES.** Nothing here lets an AI value become operational on its own. `_BEHAVIOURAL.operational_rule` must stay `AnyOf(taxonomy/confirmed, source/attested, human/confirmed)` — **do NOT add an LLM leaf to it.** The human confirm is what makes the value load-bearing; that IS the design.
- **THREE FLAT-PRESENCE SURFACES ARE THE STANDING HAZARD** (verified): `UNIT_CONSISTENT`/`CURRENCY_CONSISTENT` clear on the mere presence of `graph_node.unit`/`.currency`, and `TYPE_IS_NUMERIC` clears on a bare `ov.value` rather than `status`. They are unreachable by the LLM today **only because no display projection exists for them.** Any change that adds one opens a silent clear in the same stroke. E4a must add none.
- **Confirming is not always unblocking.** A confirmed `semi_additive`/`non_additive` correctly turns the requirement into a **rejection**. The UI must not promise "confirm to unblock".
- Reuse the existing writer `enrich._write_llm_field_evidence` (field-parameterised) and the existing reconciliation; do not fork them.
- Judge the suite by **failure-set delta** against a fresh `origin/main` baseline, never raw counts.

## File Structure

- Create: `tests/featuregen/overlay/upload/test_gating_confirm_lift.py` — the measurement (Task 1).
- Modify: `src/featuregen/overlay/upload/field_policies.py` — `_BEHAVIOURAL` display rule + `human_editable` (Task 2).
- Modify: `src/featuregen/overlay/upload/enrich.py` — the `additivity` proposal writer (Task 2).
- Modify: `src/featuregen/api/routes/assets.py` (or wherever the field-decision read lives) — expose the CAS anchor (Task 3).

---

### Task 1: Measure the existing chain — the number this program has never had

**Files:** Create `tests/featuregen/overlay/upload/test_gating_confirm_lift.py`.

**Why first:** A1 was assumed to need building; grounding shows it is already live. So the honest first move is to *prove the chain works and quantify it* — not to build. This test is also the permanent regression guard for the whole E4 thesis.

**Interfaces:** Consumes `table_synth` (proposal), `table_fact_governance` / the confirm route (disposal), `feature_assist._validate_idea` (the gauntlet), `is_feature_eligible`.

- [ ] **Step 1: Write the failing test**

```python
def test_confirming_the_ai_grain_and_asof_moves_features_to_design_checked(db, ...):
    """The E4 thesis, end to end: AI proposes grain + availability; a human confirms; features that
    were NEEDS_EXTERNAL_VALIDATION become DESIGN_CHECKED. Fails if any link in the chain breaks."""
    # 1. ingest the FTR sample with OVERLAY_TABLE_SYNTH on -> AI proposes grain + availability facts
    # 2. BEFORE: count validation_status across the candidates for this table
    before = _status_counts(db, source, table)
    assert before["NEEDS_EXTERNAL_VALIDATION"] > 0        # the requirements really do fire
    # 3. a human CONFIRMS the queued proposals through the REAL governance path
    _confirm_table_facts(db, source, table, actor=admin)
    # 4. AFTER: the same count
    after = _status_counts(db, source, table)
    assert after["DESIGN_CHECKED"] > before["DESIGN_CHECKED"], (
        "confirming the AI's grain/as-of cleared no requirement — the E4 chain is broken")
```
Use a **non-generic source name** (a generic one collides in full-suite runs and the MF-6 guard holds an FTR upload). Drive the REAL propose→confirm→project path, not direct table writes.

- [ ] **Step 2: Run it.** Expected: it FAILS or passes with a small delta — either way **record the actual numbers** (before/after counts, and which requirement codes cleared). This is the deliverable.
- [ ] **Step 3: If the chain is broken, fix the break** (that is the real A1 work and it is only now defined). If it works, keep the test as the regression guard.
- [ ] **Step 4: Re-run to green.**
- [ ] **Step 5: Commit** — `test(e4a): pin the AI-propose -> human-confirm -> DESIGN_CHECKED chain`

**Report the number in the task report** — "N features moved from needs-review to clean" is the headline this program has been missing, and it decides whether E4b is worth funding.

---

### Task 2: Open `additivity` to the LLM (proposal only)

**Files:** Modify `field_policies.py`, `enrich.py`; Test: new/extended.

**The three edits** (all verified present): `_BEHAVIOURAL` currently excludes the LLM from **both** rules and defaults `human_editable=False`, so an AI proposal is invisible AND the generic correction route 403s.
1. Add `_LLM_PROPOSED` to `_BEHAVIOURAL.display_rule` — makes an AI proposal **visible**.
2. Set `human_editable=True` on `_BEHAVIOURAL` — makes it **confirmable** via `apply_field_correction`.
3. Add an `additivity` writer in `enrich.py` reusing `_write_llm_field_evidence` (+ the existing reconciliation), so the LLM can propose a value.

**Do NOT touch `_BEHAVIOURAL.operational_rule.** The display column, `_DECISION_LINK_COLUMN` entry and `additivity_decision_id` all already exist, and GATE-2 hash verification already protects the clear.

- [ ] **Step 1: Failing tests** — an AI-proposed `additivity` is **visible** (renders with its `llm/proposed` provenance) but is **NOT** operational (`is_feature_eligible` False, the requirement still fires); after a human `confirm_existing` on that evidence, it **becomes** operational and the requirement clears; a confirmed `non_additive` correctly **rejects** the feature rather than clearing it.
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement the three edits.**
- [ ] **Step 4: Run → pass**, plus the behavioural/authority suites (`test_field_policies`, `test_field_authority`, `test_column_authority`, the cascade tests) — an `_BEHAVIOURAL` change is load-bearing far beyond this feature, so report their output explicitly.
- [ ] **Step 5: Commit** — `feat(e4a): the LLM may PROPOSE additivity; only a human confirm makes it operational`

---

### Task 3: Make one-click confirm actually issuable

**Files:** Modify the asset/field read route; Test: route test.

**The gap:** `confirm_existing` **already** lets a human accept an AI proposal by `evidence_id` without retyping — and the four-eyes check explicitly permits `llm` as the third party. But `read_field_cas` (`field_correction.py:190-199`) **is wired to no route**, and asset-detail exposes evidence ids but not the `evidence_set_hash`, so a client cannot assemble the CAS triple a correction requires. Without this, "accept the AI's value" is not clickable.

- [ ] **Step 1: Failing test** — a read route returns, per field, the CAS anchor (`latest_decision_id`, `evidence_set_hash`, `policy_version`) alongside the candidate evidence ids, such that a subsequent `confirm_existing` with those values succeeds; and a stale anchor still 409s (the CAS guard must not be weakened).
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** — expose `read_field_cas` on the existing field/asset read surface. **Read-only; do not weaken the CAS or four-eyes checks.**
- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit** — `feat(e4a): expose the CAS anchor so an AI proposal is one-click confirmable`

---

## Self-Review (author checklist — completed)

- **Measurement leads**, because grounding showed A1 is already live — building first would have been building something that exists.
- **The safety line holds:** no LLM leaf on `_BEHAVIOURAL.operational_rule`; the human confirm is the only path to operational.
- **No new flat-presence surface** is created (the three known ones stay unreachable; E4b owns them, with the read-switch mandatory in the same change).
- **Honest about confirm≠unblock** (`non_additive` correctly rejects) — pinned by a test.
- Reuses the existing writer, reconciliation, correction path and governance queue; forks nothing.

## Success criteria
- **A real number:** N features move from `NEEDS_EXTERNAL_VALIDATION` to `DESIGN_CHECKED` after a human confirms AI proposals — measured before and after, on the sample.
- An AI-proposed `additivity` is **visible but not operational**; it becomes operational **only** on human confirm.
- A confirmed `non_additive` **rejects** the feature (confirming is not always unblocking).
- A human can accept an AI proposal in **one action**, without retyping the value.
- No requirement is ever cleared by an unconfirmed AI value (per-field test).
- Full-suite failure-set delta vs `origin/main`: **empty**.

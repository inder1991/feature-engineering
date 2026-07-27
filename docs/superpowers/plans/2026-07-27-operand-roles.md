# Operand Roles — ask for a unit only where a unit can exist

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop demanding a unit from a timestamp, an id or a status flag that merely rides along as a second operand — the **sole** remaining blocker between `DESIGN_CHECKED 5 of 10` and 10 of 10 on the FTR sample.

**Architecture:** The role of every bound operand is already declared by the template and already computed during grounding — it is simply dropped before the gauntlet sees it. This plan carries it through. **No new inference, no heuristic, no AI guess.**

## Why this is safe (and why the earlier shortcut was refused)

E4a narrowed the unit check structurally (grouping key + time anchor + single-measure). What remains is operands like `setl_stat`/`acct_id`/`txn_ts` bound as a *second* operand — so `len(measures) >= 2` trips and the check fires on a column that can never carry a unit.

The tempting fix was "skip anything whose **concept** isn't monetary". That was **refused and stays refused**: concepts are AI-proposed, so one wrong concept would wave a genuine dollars-vs-fils mismatch through — a safety gate resting on a guess.

This fix uses a different, trustworthy source: **the template author's own declaration.** `Need.role` (`templates.py:81`) is written by hand in the recipe library — `"stock_col"`, `"flow_col"`, `"asof"`, `"entity"`, `"event_ts"` — and `Need.join_role` maps to the `JoinRole` enum (`binding_roles.py:10-18`: `MEASURE`, `SOURCE_ENTITY_KEY`, `TIME`, …). Template-declared, human-authored, versioned in the repo. That is a legitimate basis for a safety decision; an AI-proposed concept is not.

## The data already exists — verified

- Grounding builds a role→column map: `bindings[need.role] = col` (`templates.py:485`) and `resolutions[need.role] = GroundedNeedResolution(role=need.role, …)` (`:486-487`).
- `GroundedNeedResolution` carries **`role`** *and* **`selected_object_ref`** (`templates.py:141-145`).
- `GroundedFeature.binding_resolutions` already carries those resolutions (`templates.py:606-608`).
- But `GroundedFeature.derives_pairs` (`:176`) is bare `(catalog_source, object_ref)` — the role is dropped there, and `_idea_from_grounded` (`contract/gate1.py:133-143`) builds the `FeatureIdea` from `derives_pairs` alone.

**So the whole change is: carry the role from `binding_resolutions` onto the idea, and let the unit check consult it.**

## Global Constraints

- **Template-declared roles ONLY.** Never infer a role from a concept, an analytical guess, a column name, or a data type. If a role is absent, **fall back to today's behaviour (ask)** — erring toward asking is the safe direction.
- **The LLM path must be unaffected.** `_validate_idea` is shared: LLM-proposed candidates have no template roles, so they must behave exactly as they do now. Prove it with a test.
- **Do not weaken E4a.** Keep the measure-count gate and the grain/time exclusion. Keep `MIXED_UNITS`/`MIXED_CURRENCY` scanning every derive. Keep the anti-silent-clear test green.
- **Do not touch** `_MEASURE_ANNOTATION`'s rules, the LLM's exclusion from resolution, or anything that would let an AI value reach `graph_node.unit`.
- Judge the suite by **failure-set delta** vs a fresh `origin/main` baseline (current: 4969 passed / 0 failed).

---

### Task 1: Carry the operand role onto the feature

**Files:** `src/featuregen/overlay/upload/templates.py` (expose the role→ref map), `contract/gate1.py` (`_idea_from_grounded`), `feature_assist.py` (`FeatureIdea` field). Test: new + existing.

**Interfaces:**
- `FeatureIdea` gains an optional `operand_roles: tuple[tuple[str, str], ...] = ()` — `(object_ref, role)` pairs, empty for LLM candidates. (A sorted tuple keeps the frozen dataclass hashable, matching `Requirement.params`' precedent.)
- `_idea_from_grounded` populates it from `gf.binding_resolutions` (`role` + `selected_object_ref`, skipping `None` refs).

- [ ] **Step 1: Failing tests** — a grounded template idea carries a role for each bound operand, matching the template's own `Need.role`; an LLM-proposed idea carries an **empty** map; the roles survive into the persisted snapshot / serialization if the idea is serialized (check `_idea_json` and `feature_serialize.py` — **note both have previously held lossy private copies of the wire shape; verify rather than assume**).
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement.** Additive only — no existing field changes meaning.
- [ ] **Step 4: Run → pass** + the contract/gate suites and `test_suggestions.py`.
- [ ] **Step 5: Commit** — `feat(roles): carry the template-declared operand role onto the feature`

---

### Task 2: Ask for a unit only where a unit can exist

**Files:** `feature_assist.py` (the unit/currency disposition, ~`:758-775` after E4a). Test: extend.

Use the role map to decide which operands are genuine **measures**:
- An operand whose declared role is a **measure** (e.g. `JoinRole.MEASURE`, or the template's own measure-ish `Need.role`s — derive it from the existing `binding_roles`/`need_metadata` mapping rather than hard-coding a string list, and say what you used) → **still asked** for its unit.
- An operand whose declared role is a key/time/other non-measure → **not asked** (it cannot carry a unit).
- **No role available** (the LLM path, or a template that declares none) → **behave exactly as today**: fall back to E4a's structural rule. Never silently skip on missing information.

Also re-evaluate the "is this combining?" gate with roles: it should count **measure** operands.

- [ ] **Step 1: Failing tests**
  - a feature whose second operand is a declared key/time/flag no longer mints `UNIT_CONSISTENT`/`CURRENCY_CONSISTENT` for it;
  - **ANTI-SILENT-CLEAR:** a feature with **two declared MEASURE operands**, one lacking a unit, **still** mints `UNIT_CONSISTENT` for that measure (must fail if anyone widens the exclusion);
  - an **LLM-proposed** candidate (no roles) behaves byte-identically to today;
  - a template declaring **no** roles falls back to E4a behaviour;
  - `MIXED_UNITS`/`MIXED_CURRENCY` still hard-reject.
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run → pass**, then **re-record `test_gating_confirm_lift.py`** (it pins the counts) and run `test_feature_assist.py`, the contract/gate suites, `test_ai_unit_proposal.py`.
- [ ] **Step 5: Commit** — `fix(roles): demand a unit only from declared measure operands`

**Report the new `DESIGN_CHECKED` number — this is the deliverable.**

---

## Self-Review (author checklist — completed)
- The role source is **template-authored**, never inferred — the one distinction that makes this safe where the concept-based shortcut was not.
- **Fail-open toward asking:** any missing role falls back to current behaviour; the check is never skipped on absent information.
- The **LLM path is provably unchanged** (its own test).
- E4a's guarantees are preserved: measure-count gate, grain/time exclusion, hard rejects, anti-silent-clear.
- The change is additive on `FeatureIdea`; the two known lossy serializers are called out for verification.

## Success criteria
- **`DESIGN_CHECKED` rises above 5 of 10** on the FTR sample (report the number; 10/10 only if every survivor's blocker really was a ride-along operand).
- A declared **measure** missing a unit is **still** asked — always.
- LLM-proposed candidates are byte-identical to today.
- Mixed units still hard-reject.
- Full-suite failure-set delta vs `origin/main`: **empty**.

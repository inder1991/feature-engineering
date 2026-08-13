# Semantic v1 UAT runbook — the end-to-end feature-generation walk

**Date:** 2026-08-13 · **Companion test:** `tests/featuregen/api/test_e2e_walkthrough.py`
(the same walk as API calls, green in the default suite — any regression that breaks this
path breaks the build).

**Scope of the claim this walk proves:** contract-authoring is ready for UAT;
materialization is **visibly unavailable** (a typed refusal with named next steps, never a
silent failure). Nothing in this walk executes a formula or touches warehouse data.

**Precondition:** the backend runs with `FEATUREGEN_SEMANTIC_PLANNING=semantic_v1` and a
catalog is ingested whose concepts are AI-proposed (the normal state after enrichment,
before any SME confirmation). The walk below uses a churn hypothesis; any objective with a
bound recipe works the same way.

---

## The walk

### 1. Generate — and read the honest blockers

*Workbench → type a hypothesis ("long-tenured customers churn when their balance drops"),
prediction goal ("predict churn"), catalog source → Generate candidate sets → confirm the
recognised scope.*

Expected:

- Candidate cards appear in two lists: **recommended** (bound, plannable) and **actionable**
  (undecided work — never hidden, never framed as failure).
- A bound recipe card (e.g. "Complaint count") shows **Save idea** available, but
  contract-creation blocked with two named reasons:
  - *confirm the AI-proposed concept(s) in the Governance screen's concept-confirmation
    queue* (`PROPOSED_METADATA_ONLY`), and
  - *record a recipe review at the current revision* (`RECIPE_REVIEW_NOT_CURRENT`).
- A recipe whose authored temporal contract cannot compile (e.g. "Tenure days" — no
  snapshot policy authored) appears under **rejected**, with the code
  `TEMPORAL_POLICY_UNRESOLVED`. This is recipe setup work, honestly named — not a bug in
  your catalog.

### 2. Clear each blocker through its real surface

**2a. Confirm the concepts (SME, one batch).**
*Governance → Concept confirmations → select the catalog source.* The queue groups proposed
concepts by how load-bearing they are. Confirm the batch (untick exceptions). Each
confirmation is one attributable decision; the funnel counter moves.

**2b. Review the recipe (three roles, at least two people).**
*Recipe review screen → the recipe → Approve* as `banking_sme`, `data_semantic_owner`, and
`formula_engineering`. A single person approving all three roles is rejected by design. The
review pins the exact recipe revision — if the definition changes later, every approval
stales automatically.

**2c. Confirm the unit of analysis (one click).**
*Workbench scope panel → "You're predicting per CUSTOMER (spine: accounts via
public.accounts.customer_id) — correct?"* → **Yes**. (Answering "No" offers only the
catalog's realistic spine-backed entities — a closed list. Skipping is allowed: the
confirmation is optional and absence never blocks; confirming it makes a wrong-grain
candidate an actionable `UOA_MISMATCH` instead of a silently-served card.)

### 3. Regenerate, draft, confirm

*Workbench → regenerate (same intent).* The hero card's contract-creation is now
**allowed**. Draft it, review the draft, confirm.

Expected:

- The feature registers as **governed** (`lifecycle_state='governed'`) — the only path that
  mints governed features.
- The contract carries its readiness honestly: `FORMULA_BLOCKED` — no executable formula
  exists yet, and the card never pretends otherwise.

### 4. Materialization is visibly unavailable

The card's materialization action is blocked with typed reasons
(`READINESS_NOT_MATERIALIZATION_READY`, `EXECUTION_AUTHORITY_UNEVALUATED`, …), each with a
named next step. There is no path that silently materializes.

### 5. Save an idea

Any card — including a conceptual LLM proposal that can never be a contract — can be saved
as an **idea**: browsable, labeled, never a model consumer's input.

### 6. Refine

*A card → Refine → type an instruction ("look at deposit activity only").* The engine
revises the meaning and re-binds the columns; the revised card is a preview, and governing
it requires a whole-round regenerate (the button says so).

### 7. Cross-check on Suggested Features

*Catalog → the table → Suggested Features.* The page consults the same engine over the same
frozen context: the recipe you just confirmed appears bound here too. The two surfaces
cannot disagree about a binding — by construction.

---

## What UAT sign-off means

Signing this walk off asserts: the funnel, review, UOA, generation, activation, draft,
confirm, idea, refine, and suggestion surfaces work end to end against a real catalog. It
does **not** assert anything about formula execution, materialization, or warehouse
correctness — those are behind later remediation phases (C2 execution authority, the
formula seam) and stay typed-refused until then.

# Recipe-to-code: reviewed formulas, LLM fallback, and one generation journey

**Date:** 2026-08-22 · amended 2026-08-22 to become a child plan
**Status:** implementation plan only; this document changes no runtime behaviour
**Branch:** `feature/asset-detail-reapply`

---

## ▲ This is a CHILD IMPLEMENTATION PLAN

**Parent:** [`2026-08-22-four-stage-gating-and-production-certification.md`](2026-08-22-four-stage-gating-and-production-certification.md)

> *The gating plan defines the traffic rules; this plan builds the road and the vehicle.*
> — the product owner, 2026-08-22

The owner's ruling, which is why this document was rewritten:

> *"The gating plan defines WHETHER each action is allowed; the recipe-to-code plan defines HOW a
> selected feature obtains a formula and generated code. The gating plan's deterministic lane,
> support for the 295 recipes, mixed-method artifacts and decisive journey tests already depend on
> the recipe-to-code plan, so finishing the gating plan first is impossible.
> **recipe-to-code-llm-fallback.md should become a CHILD IMPLEMENTATION PLAN of the amended gating
> architecture — not a separate plan executed afterward.**"*

| | |
|---|---|
| **The PARENT owns** | the six actions · **the one blocker matrix** · the exhaustive `(code, action)` disposition table · the shared decision service · the bypasses · the production boundary · method identity and per-member certificate binding · formula pinning and draft identity · the certification programmes · code-retrieval authorization |
| **THIS PLAN owns** | strategy resolution from evidence · the deterministic reviewed-blueprint lane · the LLM fallback lane · the durable code-generation coordinator · preview generation · the frontend journey · the reviewed-expectation growth programme |

**Three rules this document obeys, and a reader should hold it to:**

1. ▲ **It does not restate the parent.** Where a gate, a code disposition or a matrix cell is at
   issue, this document **references** the parent's section. Duplicated policy is how two routes
   come to give two answers.
2. ▲ **It contains no action matrix.** The previous revision's *"D4 — four actions, four
   decisions"* — four actions, no `PUBLISH_SANDBOX`, no `MATERIALIZE_PRODUCTION`, twelve rows of its
   own — is **DELETED**. See **parent §4**, which is now the only one. The rows that existed only
   here were folded into it and are marked `(child)` there.
3. ▲ **It does not build the decision service.** The previous revision's Phase 5 did. That phase is
   **dissolved** into parent steps 3, 7 and 8. This plan **consumes** `evaluate_action`.

**The execution order is parent §17's ten steps**, not the phase numbers this document used to
carry. The steps below are named for those. ▲ **Nothing here may begin until step 1 — this
amendment pair — is complete**, per the owner: *"neither should begin implementation until their
conflicting action matrices and the P0 findings are corrected."*

---

## 0. Outcome

After this plan, a user can submit a hypothesis, select recipe and LLM-origin recommendations, ask
for formulas and code, inspect the result, and run it in a sandbox — **without a dead end caused
merely by a recipe lacking a reviewed formula expectation.**

```text
Hypothesis
   |
   v
Semantic eligibility and recommendations
   |
   v
Human selects features  (selection spends nothing)
   |
   v
Resolve formula method per selected feature      <- server-side, from evidence, BEFORE authoring
   |-----------------------------------------|
   | reviewed V2 EXPECTATION-REGISTRY entry,  | anything else — including a merely DERIVED
   | current, and it binds                    | blueprint (parent §10.1)
   v                                          v
Deterministic instantiation                    Explicit LLM authoring
(zero provider calls)                          (cost shown and authorized before the request)
   |                                          |
   +--------------------+---------------------+
                        v
        Parse -> deterministic validation -> admission          <- BOTH routes, identically
                        v
            Compile -> render -> seal artifact
                        v
              Show formula and code           <- parent §4 GENERATE_PREVIEW, parent §13 read scope
                        v
              Execute in sandbox              <- parent §4 EXECUTE_SANDBOX
                        v
              Publish sandbox results         <- parent §4 PUBLISH_SANDBOX (a SEPARATE act)
                        v
        Production materialization, then publication
             (per-member method-matched certificates — parent §9, §10)
```

▲ **The user-facing promise is narrower than "all 317 recipes always generate code", and narrower
than the previous revision implied.** Per parent §0.2 fact 2: **295 is the count of POTENTIAL
LLM-FALLBACK RECIPES, not a set that becomes previewable.** Each of the 295 must still pass binding
completeness, grain, currency/reversal/status policy resolution and renderer dispatch before a
preview exists. **How many clear all four is NOT MEASURED**, and this plan does not claim it — the
measurement is parent Phase B's first output and is the only honest denominator for "did this work".

Missing data and ambiguous banking semantics remain honest blockers. Missing *review* alone does not.

---

## 1. Verified current state

### 1.1 Registry measurement

| Measurement | Count |
|---|---:|
| Recipes (`V2_RECIPES`) | 317 |
| Deterministic formula recipes | 298 |
| Conceptual patterns | 11 |
| Governed model outputs | 8 |
| `FORMULA_BLOCKED` | 295 |
| `FORMULA_AUTHORABLE` | 3 |
| `CONCEPTUAL_ONLY` | 19 |
| Reviewed expectation membership (`has_reviewed_expectation`) | 3 |
| **Structurally DERIVABLE V2 blueprints** | **90** |
| **REVIEWED V2 expectation-registry entries** | **1** — `posted_debit_amount` |

The 227 non-derivable definitions break down as: window not event-anchored 102 · multiple
operands/body unresolved 65 · not a deterministic formula 19 · aggregation undeclared 19 · output
policy underivable 6 · window unit unsupported 6 · no measure operand 4 · temporal definition
blocked 3 · parameter projection underivable 2 · grain key unresolved 1.

Those refusals are recipe-library improvement work. They are not all reasons to prevent an LLM from
proposing a candidate-specific formula when the actual selected columns supply the missing facts.

### 1.2 ▲ 90 DERIVABLE is not 90 REVIEWED — and the previous revision blurred them

**Verified.** `derive_blueprint_v2` (`recipe_formula_blueprint_derivation.py:152`) is **pure —
definition only**. It consults no review event, no registry and no human. The reviewed registry
`RECIPE_FORMULA_V2_EXPECTATIONS` (`recipe_formula_expectations_v2.py`) has **one entry**, and its
own docstring says why the two numbers must never be joined:

> *"A2 derives a blueprint for 90 of the 317 recipes, but* which *of those 90 a human has actually
> reviewed is a governance answer no derivation can supply, and no `recipe_review_event` row exists
> yet."*

`grep` across `deterministic_producer.py` and `replay_authoring_v2.py` for `has_reviewed`,
`recipe_review_event` or `review_validity` returns **nothing**: the deterministic lane has no review
check of its own. So a resolver written from `blueprint_derivable` would seal
`REVIEWED_RECIPE_BLUEPRINT` for up to **90** recipes no reviewer approved — into an **append-only**
provenance table (migration 1099) and an append-only method-identity table (parent §10). That is a
sealed method claim the evidence does not support, and it cannot be audited away afterwards.

▲ **The rule is parent §10.1's, and this plan implements it:** `REVIEWED_RECIPE_BLUEPRINT` requires
**reviewed-registry membership, at expectation generation V2, that binds**. Derivability is a
fourth, independent fact and grants nothing. A derived-but-unreviewed blueprint is a **proposal** —
it may be shown, it seeds the review queue (step 9), and it selects the **LLM** route with
`BLUEPRINT_DERIVED_NOT_REVIEWED` recorded.

### 1.3 The three "reviewed" recipes are not one homogeneous capability

`has_reviewed_expectation()` (`recipe_formula_expectations_v2.py:53`) unions two registries:

* `merchant_mcc_diversity` and `obligor_facility_count` — Formula **V1** count-distinct blueprints
  (`recipe_formula_expectations.py`);
* `posted_debit_amount` — the single Formula **V2** reviewed fixture.

Therefore "a reviewed expectation exists" cannot by itself select the V3 deterministic producer. The
resolver must return the **expectation generation** and whether it can produce V3 (parent §10 makes
that field part of `method_identity_hash`, so it is load-bearing rather than decorative).

▲ **CORRECTED: the two V1 entries have no live serving role to retire.** The previous revision said
*"delete their V1 serving role only after the new deterministic lane produces identical canonical
results."* **The V1 authoring lane is already retired** — there is no V1 materialization path to
switch off, and no canonical V1 result to compare against. The honest statement is the simpler one:
**the two V1 entries route to the LLM today and always will, until somebody authors, reviews and
pins genuine V2 fixtures for them.** Treating them as a live path to be migrated off invents a
migration that has no source side. They emit `REVIEWED_EXPECTATION_LEGACY_VERSION` (parent §5) as a
warning and select `LLM_AUTHORED`.

### 1.4 Machinery that already exists

* `formula_draft_worker.py` authors and critiques with the LLM, parses Formula V3, validates, admits.
* `deterministic_producer.py` turns a **bound** blueprint into a V3 proposal with no provider call
  (`proposal_from_bound_expectation`, `bypass_for`).
* `run_authoring_v2_replay(..., reviewed_blueprint=...)` (`replay_authoring_v2.py:486`) records the
  honest `REVIEW_BYPASSED` trace. ▲ **No production caller supplies that parameter** — `grep` for
  `reviewed_blueprint` in `formula_draft_worker.py` returns **0 hits**.
* `restore_formula_v3.py`, `generation_lane.py`, `generate_v2.py`, `seal_v2.py` — the build-set
  compile/render/seal path.
* ▲ **Migration 1099 is APPLIED to the live cluster (ledger 195), and so is its writer.**
  `derive_member_provenance` / `record_member_provenance` are called from `seal_v2` and reached from
  the real lane at `generation_lane.py:543`. The previous revision's *"the current worktree is
  finishing its writer"* and its Phase 0 item *"finish or separately shelve the 1099 provenance
  writer work"* are **STALE** — parent Phase A completed both at `364cd7fa`. **1099 is immutable:
  it carries a BEFORE UPDATE OR DELETE trigger that raises, so it can only ever be EXTENDED by a new
  append-only table** (parent §10).
* `FormulaDraftAction.tsx` already guarantees that ticking a checkbox calls no LLM.

### 1.5 The real gaps this plan closes

1. `formula_draft_worker.py` always uses the LLM and never supplies `reviewed_blueprint`.
2. `formula_drafts.py:269` hard-codes `"formula_source": "llm_authored"` instead of reading evidence.
3. `WorkItemOrigin.authors_from_reviewed_blueprint` (`authoring_work_item_store.py:49`) returns
   `origin is RECIPE`, and migration 1088's CHECK constraint enforces
   `origin = 'recipe' → reviewed_blueprint_revision IS NOT NULL`. **Origin is being used as proof of
   review.** That makes "recipe recommendation, LLM-authored formula" — the required fallback —
   *unrepresentable in the database*.
4. No production coordinator carries selected options through selection revision → formula → build
   set → authorization → generation. The frontend has per-row drafting and a downstream artifact
   screen and no bridge.
5. Recipe readiness mixes specification maturity, formula availability, engine support and
   production certification into one ladder. *(The half that AUTHORIZES is the parent's to remove —
   parent §6. The half that DESCRIBES is this plan's to re-word — step 3 below.)*

▲ Gaps the previous revision listed that are **the parent's, not this plan's**: the
`MATERIALIZATION_READY` equality (parent §6), the two routes not sharing one decision (parent §7,
§8.3), and gold sitting on readiness (parent Phase C / step 8). They are removed from this list so
nobody builds them twice.

---

## 2. Product and architecture decisions

Part of the implementation contract; not questions for individual tasks to re-litigate.
▲ **D4 and D8 of the previous revision are gone** — D4 was the competing matrix (now parent §4),
D8 was a restatement of parent §14. Numbering is preserved so existing references still resolve.

### D1 — Candidate origin and formula method are separate axes

```text
candidate origin:  recipe | llm_intent | user_definition       — where the IDEA came from
formula method:    REVIEWED_RECIPE_BLUEPRINT | LLM_AUTHORED    — how the FORMULA was produced
```

A recipe-origin feature may use either method. An LLM-origin feature may never claim a reviewed
recipe blueprint merely because it resembles a recipe.

▲ **Today every formula is `LLM_AUTHORED` whatever the origin** (§1.4, §1.5 items 1–3) — which is
also why parent §0.2 fact 4 records that **mixed-METHOD artifacts are not testable until step 4
lands**. A build combining a recipe-origin and an LLM-origin recommendation is mixed-*origin*; both
members are LLM-authored. This plan is what makes the parent's mixed-method journey test writable.

### D2 — Strategy selection is deterministic and server-owned

For one selected option:

```text
conceptual_pattern       -> NON_FORMULA
governed_model_output    -> MODEL_WORKFLOW
deterministic_formula:
    reviewed V2 registry entry, current, AND binds   -> REVIEWED_RECIPE_BLUEPRINT
    reviewed V2 registry entry, current, fails to bind -> BLOCKED  (REVIEWED_BLUEPRINT_NOT_EXECUTABLE)
    reviewed entry exists at generation V1           -> LLM_AUTHORED  + REVIEWED_EXPECTATION_LEGACY_VERSION
    blueprint DERIVABLE but not in the registry      -> LLM_AUTHORED  + BLUEPRINT_DERIVED_NOT_REVIEWED
    no reviewed entry                                -> LLM_AUTHORED  + LLM_AUTHORING_REQUIRED
```

▲ **Corrected from the previous revision**, which said *"current executable V2 blueprint exists +
binds → REVIEWED_RECIPE_BLUEPRINT"*. "Executable blueprint exists" reads as derivable, and
derivable is 90 (§1.2). **Registry membership is the gate.** Unknown expectation generations fail
closed; they never become V2 by default.

The client never chooses a more favourable method label. It may explicitly request an LLM retry
after a deterministic defect — **that is a new draft identity and a new user action**, and the
backend never silently changes method after a failure.

### D3 — A missing reviewed expectation is a route selector, not a code blocker

It causes `LLM_AUTHORED` and the UI warning "AI formula required". ▲ **Two different codes are
involved and the previous revision named the wrong one:**

| Code | Module | What it does |
|---|---|---|
| `"no_reviewed_formula_expectation"` (`BLOCKER_NO_REVIEWED_EXPECTATION`) | `recipe_readiness.py:37` | a readiness-**fold** string. Displays, sorts, explains. **Enforces nothing** |
| `R.FORMULA_NOT_REVIEWED` | `semantic_eligibility_reasons.py:93` | the **activation-policy** code emitted at `activation_policy.py:184`. **This is the refusal** |

Keeping the fold string fold-owned is correct **and changes nothing about the gate**. Re-homing
`R.FORMULA_NOT_REVIEWED` is **parent §6's** job, not this plan's — and parent §6 records that it
already sits in the evaluator disposition table as **`DROPPED`**.

### D4 — *(deleted — the action matrix is parent §4)*

The four-action matrix that stood here is gone. There is one matrix, in the parent. What this plan
consumes from it:

* `evaluate_action(conn, request, *, action_authorization_revision_id)` — parent §7. Discriminated
  request types, server-loaded evidence, **one decision PER MEMBER**, group verdict all-must-pass,
  warnings the UI must render, policy version, evidence hash.
* The `(code, action) → Disposition` table — parent §5.
* ▲ **Adding a blocker code is a three-part commit** — parent §5: the
  `semantic_eligibility_reasons` entry, an explicit `CARRIED`/`DROPPED` row with a written reason,
  and `test_evaluator_contracts.py`'s `assert len(emitted) == 16` literal, **all in the same
  commit**. Verified: that assertion is an **equality** over a regex scan of `activation_policy.py`'s
  source, and `ACTIVATION_BLOCKER_DISPOSITIONS` is 18 `CARRIED` / 4 `DROPPED` with **no default** —
  `carried_blockers` raises `KeyError` on an unknown code, which is a 500 out of the sandbox publish
  path. So a new production-certificate code either crashes sandbox publication or blocks it, and
  both violate the owner's ruling that gold must not gate sandbox.

The codes this plan contributes are listed in parent §5. Three the previous revision proposed were
**not accepted**, and the reason is worth carrying: `PRODUCTION_METHOD_CERTIFICATE_MISSING` /
`_STALE` / `PRODUCTION_RECIPE_REVIEW_NOT_CURRENT` embed the ACTION in the code name, and the action
is already a column.

### D5 — Selection never spends

Checkboxes update client state. The first paid action is an explicit "Prepare formulas and code"
confirmation stating how many selected features require LLM calls. Polling, reload and double-click
never create new provider work.

▲ **The durable half of that promise is broken today, and it is the parent's step 2 to fix**
(parent §11.1): `_authoring_config_hash()` returns a **constant** — it calls `getattr` on a `dict`,
so `model`, `max_tokens` and `prompt_id` all fall to their defaults on every deployment. The unique
index migration 1090 calls "THE MONEY GUARD" therefore cannot see a model change. **This plan must
not touch that composition.** Its `{formula_strategy, strategy_identity_hash}` facts are *folded in*
by parent step 2, together with `provider_contract_hash` and the corrected model/prompt read, as
**one** identity change — see §3.3 below.

### D6 — One backend coordinator owns the journey

The React client must not chain five write APIs and hope the browser stays open. A durable
`code_generation_job` owns selections, formula preparation, build-set creation, generation and the
terminal outcome. Existing formula and generation queues remain the workers; the coordinator joins
their durable states and advances only from recorded evidence.

### D7 — Mixed-method artifacts are first-class

One build set may contain a reviewed-blueprint member and an LLM-authored member. ▲ **Provenance,
method identity AND certificate binding are all PER MEMBER** — parent §10. The previous revision
said "record the exact certificate revision used on a production publication attempt", which is
per-**attempt**; a mixed artifact needs *several* certificates and one per attempt could only ever
be right about one member. Corrected: `production_attempt_member_certificate (attempt_id,
member_name, certificate_revision_id, method_identity_hash)`.

### D8 — *(deleted — see parent §14, "Certification is not feature approval")*

### D9 — No new rollout flags

The product is pre-live. Implement the canonical path, test it, deploy it to the branch-owned Kind
cluster, and delete the legacy path. Rollback is the previous image. Do not create dark gates that
let the two behaviours drift.

▲ **CORRECTED — no thin adapter.** The previous revision's opening promised *"keep the old
materialization API temporarily as a thin adapter, then delete it."* **Parent D2 rules: DELETE the
route, do not build an adapter**, and parent §8.3 explains why a thin adapter is a bypass rather
than a simplification: `POST /materialization-runs` compiles, renders, seals, runs L0 and **can
publish** — mapping it to one `GENERATE_PREVIEW` decision would let its later execution and
publication proceed under a decision made for a weaker act. **There is no "old API preview"
operation to map.** The migration surface is three test modules and one frontend run-sheet read.

---

## 3. Canonical contracts

### 3.1 Formula strategy

Create `src/featuregen/overlay/upload/formula_strategy.py`:

```python
class FormulaStrategy(StrEnum):
    REVIEWED_RECIPE_BLUEPRINT = "REVIEWED_RECIPE_BLUEPRINT"
    LLM_AUTHORED             = "LLM_AUTHORED"
    NON_FORMULA              = "NON_FORMULA"
    MODEL_WORKFLOW           = "MODEL_WORKFLOW"

@dataclass(frozen=True)
class FormulaStrategyFactsV1:
    candidate_origin: str
    computation_kind: str
    recipe_id: str | None
    recipe_revision_hash: str | None
    expectation_ref: str | None                 # NEVER the recipe id — 295 of 317 differ
    expectation_generation: str | None          # v1 | v2 | unknown  -> unknown fails closed
    reviewed_expectation_current: bool          # ▲ REGISTRY MEMBERSHIP. The gate (parent §10.1)
    blueprint_derivable: bool                   # ▲ informational ONLY. Grants nothing
    blueprint_bindable: bool
    semantic_inputs_ready: bool

@dataclass(frozen=True)
class FormulaStrategyDecisionV1:
    strategy: FormulaStrategy
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    strategy_identity_hash: str
```

▲ `expectation_ref` is the only key that may be looked up in the reviewed registry — the registry's
own docstring: *"The key is an EXPECTATION REF, never a recipe id. 295 of the 317 registry recipes
declare a ref that is not their own name."*

The pure selector chooses no columns and reads no database. A facts assembler reads the frozen
option, the current review/expectation revision, and the bound blueprint.

▲ **`blueprint_derivable` is carried so the UI can say "a blueprint could be derived — send it to
review", and for NO other purpose.** A selector that branches on it violates parent §10.1. Write the
table-driven test that proves the selector's output is unchanged when `blueprint_derivable` flips
and `reviewed_expectation_current` does not.

### 3.2 What this plan does NOT define

▲ The previous revision's §3.2 redefined the action decision, the blocker vocabulary and the
disposition of `gold_evaluation_unproven`. **All three are the parent's.**

* the action decision → **parent §7**
* the blocker vocabulary and every `(code, action)` disposition → **parent §5**
* gold's relocation off readiness → **parent step 8 / Phase C**, atomic with the production gate

What this plan supplies to the parent's fold, per member, as **facts** rather than verdicts:

```text
formula_strategy              formula_state              formula_schema_version
formula_validation_result     formula_draft_id           formula_content_hash
expectation_generation        reviewed_expectation_current
provider_contract_hash        author/critic dispatch counts
```

Every blocker still needs a closed code, a user sentence and a next action — that requirement stands;
the codes live in parent §5.

### 3.3 Durable authoring plan — and what it must NOT touch

Append-only `formula_draft_authoring_plan`, keyed one-to-one by `formula_draft_id`
(**migration 1104** — parent §17 reserves the range; both plans had independently claimed 1100):

```text
formula_draft_id            candidate_origin            formula_strategy
strategy_identity_hash      recipe_id?                  recipe_revision_hash?
expectation_ref?            expectation_generation?     reviewed_blueprint_revision?
reviewed_blueprint_hash?    provider_contract_hash?     planned_at
```

Database checks enforce:

* `REVIEWED_RECIPE_BLUEPRINT` names a recipe, expectation generation **v2**, a blueprint revision and
  hash, and **no** provider contract;
* `LLM_AUTHORED` names the frozen provider contract and **cannot** claim a reviewed blueprint;
* non-formula / model decisions never create a `formula_draft` row;
* the plan can never be updated or deleted.

#### ▲ P0 — the previous revision would have voided the money guard and every retirement

It said: *"continue using the existing `authoring_config_hash` column, but compute it as the hash of
`{formula_strategy, strategy_identity_hash}`."* **Refused.** Verified chain:

```
_authoring_config_hash()   formula_drafts.py:279   -> {model, max_tokens, prompt_id}
  -> formula_identity()    formula_draft_store.py:246
  -> formula_draft.formula_identity_hash
  -> CREATE UNIQUE INDEX formula_draft_identity    1090_formula_draft.sql:101  "THE MONEY GUARD"
```

▲ **Retirement is enforced ONLY by identity collision.** `request_draft`
(`formula_draft_store.py:335-367`) inserts `ON CONFLICT (formula_identity_hash) DO NOTHING` and
reads `formula_draft_retirement` **only when the INSERT loses**. Recompose the hash and **every**
insert succeeds, **no** retirement is consulted, and withdrawn formulas are re-authored and
re-billed. That is not a re-spend; it is a silent blanket un-retirement.

**The fix belongs to parent step 2 / §11.1, and is FOLD, never replace:**
`{provider_contract_hash, formula_strategy, strategy_identity_hash}` are **added** to the existing
model/prompt facts. ▲ **`blueprint_bindable` and `semantic_inputs_ready` stay OUT** — they are
measurements of the world, and a measurement that flaps re-mints the identity and **buys the same
answer twice**, which is the expense the index exists to prevent.

▲ **ANY composition change re-hashes every existing draft**, so parent §11.1 owns one of two
remedies before deploy, and states which: a one-time re-spend with **every existing retirement
re-asserted first**, or a backfill reproducing today's value. ▲ The backfill is unusually cheap here
because **today's value is a constant** — `_authoring_config_hash` calls `getattr` on a `dict`, so
all three fields fall to their defaults and every deployment produces the same hash. This plan does
not fix that and does not depend on it being unfixed; it supplies two of the facts and lets step 2
land them once.

### 3.4 Fix the generalized work-item model

**Migration 1105** separates `authoring_work_item.origin` from authoring strategy. Before writing
it, audit live row counts and values. Empty → replace the old constraint directly. Rows present →
preserve them and backfill only facts the old constraint logically guaranteed; anything unprovable
becomes legacy-undetermined and is **ineligible for sealing** (the same posture parent §10 takes
toward existing provenance rows: `METHOD_IDENTITY_UNRECORDED` is the honest answer, regeneration the
honest remedy).

`AuthoringWorkItemV1` carries:

```text
candidate_origin: recipe | llm_intent | user_definition
formula_strategy: REVIEWED_RECIPE_BLUEPRINT | LLM_AUTHORED
```

Remove `WorkItemOrigin.authors_from_reviewed_blueprint` and migration 1088's origin-keyed CHECK. The
new invariant is strategy-based: `reviewed_blueprint_revision` is present **exactly** for
`REVIEWED_RECIPE_BLUEPRINT`.

### 3.5 Durable journey aggregate

**Migration 1106**:

* `code_generation_job` — immutable request identity, considered revision, target reading,
  environment, requested action, requester, status, terminal details;
* `code_generation_job_member` — ordered option ids, selection revision, **`formula_draft_id` +
  `formula_content_hash`**, strategy, member state, blockers;
* `code_generation_job_event` — append-only stage events;
* links to build-set revision, **action authorization revision** (parent §0.1), generation request
  and sealed artifact.

▲ **The job's members carry the pinned formula pair because the BUILD SET does** — parent §11: today
`build_set_member` is `(revision_id, position, selection_revision_id)` and nothing else, and
`restore_formula_v3.py:90` resolves the formula with `ORDER BY d.updated_at DESC LIMIT 1`. A newer
draft landing between request and worker changes what the build MEANS while its `content_hash` stays
put. The coordinator hands the parent's pinned pair through; it does not re-resolve.

States, closed and monotone:

```text
REQUESTED  PLANNING_FORMULAS  AUTHORING  READY_TO_BUILD  GENERATING_PREVIEW
PREVIEW_READY  BLOCKED  FAILED  CANCELLED
```

`BLOCKED` is a product outcome; `FAILED` is a platform failure. The job is idempotent on its exact
request content, so a second click neither spends again nor creates a parallel build.

---

## 4. Implementation — mapped onto the parent's ten steps

▲ **The step numbers are parent §17's.** This plan's old phase numbers are gone; the mapping is
recorded in parent §17 so nothing is orphaned.

### Step 1 (SHARED) — amend both plans; one authoritative matrix

This revision and the parent's. Plus the baseline work the previous Phase 0 carried, minus its stale
item:

1. ~~Finish or shelve the 1099 provenance writer~~ — **DONE at `364cd7fa`; 1099 applied, ledger 195**
   (§1.4).
2. Record `git status`, full backend/frontend baselines, migration-ledger verification, and live row
   counts for `formula_draft`, `authoring_work_item`, `feature_selection_revision`,
   `build_set_revision`, `generation_request`, `sealed_artifact_v2`.
3. Characterization tests pinning **317 / 298 / 19 / 295 / 3 / 90 / 1** — running the real
   registry, fold and deriver, not a hand-built fixture. ▲ **The last two are separate numbers on
   purpose** (§1.2): 90 derivable, 1 reviewed. A test that pins only "90" is how they get conflated.
4. Route-call enumeration tests showing which old and new endpoints consult which policy today —
   the before-state proof for parent §8.3's decision-equivalence.
5. Freeze applied migrations; corrections get new files; ledger verification runs in CI.

**Acceptance:** no behavioural change; clean ledger; baselines recorded; the 90-vs-1 distinction is
pinned by a test.

### Step 2 (PARENT) — identity and authorization foundations

Not this plan's work. This plan **depends on all five** and must not duplicate any:
server-derived roles (parent §0.1) · formula-draft pinning (§11) · the money-guard composition
(§11.1, into which §3.3's two facts fold) · immutable action authorization (§0.1) · per-member
method identity (§10, §10.1).

### Step 3 (SHARED) — the shared action-decision service

**Parent** builds `evaluate_action`, the disposition table, and removes all three ladder blockers
from preview authorization (parent §6). **This plan's half:**

1. Keep `fold_definition_readiness` (`recipe_readiness.py:99`) for compatibility and reporting; stop
   treating its state as action authority. ▲ Named precisely: it is `fold_definition_readiness`
   feeding `fold_readiness`, and its consumers are `recipe_planning_lens.py:475`,
   `suggestion_contract.py:1632`, `recipe_registry_v2.py:168` and `taxonomy/coverage.py:32` — four
   surfaces to re-word, none of which gates an act.
2. Add a recipe **capability projection** beside it:

   ```text
   computation_kind
   reviewed_expectation: none | v1_legacy | v2_current | stale
   blueprint_derivation: derivable | refusal(code)        # informational (§3.1)
   suggested_formula_strategy
   ```

3. Expose those fields on the recipe/search/suggestion APIs, then deprecate the overloaded
   execution-readiness label.
4. Re-word: `FORMULA_BLOCKED + no_reviewed_formula_expectation` becomes **"AI formula required"** for
   a valid deterministic candidate. Conceptual and model items keep honest, separate calls to action.
5. Remove client filters and disabled buttons that treat `FORMULA_BLOCKED` as "cannot generate code".

**Acceptance:** all 295 remain honestly unreviewed and none is blocked from formula authoring by
that fact alone. **No recipe is promoted to reviewed by code.** ▲ And the acceptance does **not**
claim 295 previews — see §0.

### Step 4 (CHILD) — recipe formula routing

**4a — resolve and persist the strategy**

1. Implement the pure selector; table-driven tests over every combination, including the
   `blueprint_derivable`-flips-nothing case (§3.1).
2. Version-aware expectation resolver; **never union membership alone** (§1.3).
3. `posted_debit_amount` resolves to executable V2 reviewed blueprint.
4. The two V1 entries resolve to `v1_legacy` → `LLM_AUTHORED` (§1.3 — no V1 serving role to retire).
5. Derivable-but-unreviewed resolves to `LLM_AUTHORED` + `BLUEPRINT_DERIVED_NOT_REVIEWED` (§1.2).
6. Bind a reviewed blueprint against the **exact frozen candidate context**; a binding mismatch
   refuses deterministic authoring **by name**.
7. Persist `formula_draft_authoring_plan` in the same transaction as the draft request/outbox.
8. Re-read the stored plan in the worker; **never recompute strategy** after a review or the registry
   moves.

**4b — wire the deterministic lane**

1. Worker dispatch by the **stored** strategy.
2. `REVIEWED_RECIPE_BLUEPRINT` → load and verify the frozen blueprint revision and binding, then call
   `run_authoring_v2_replay(reviewed_blueprint=bound)`.
3. Assert **zero** author/critic provider dispatches and exactly one verified `REVIEW_BYPASSED`
   event. ▲ `bypass_for` names `blueprint_revision` and `expectation_hash` from the bound object —
   it does **not** check that anybody reviewed it, which is exactly why §1.2's registry gate must sit
   in front of it.
4. The proposal goes through the **same** V3 parser, output authority, admission, compiler and
   renderer as an LLM proposal.
5. Deterministic refusal → `REVIEWED_BLUEPRINT_NOT_EXECUTABLE`. **No silent LLM invocation.** An
   explicit "Try AI formula" action mints a new draft identity and shows the cost and provenance
   change.
6. Replace `formula_drafts.py:269`'s hard-coded `"llm_authored"` with the stored, verified method.

**4c — harden the LLM fallback**

1. Route deterministic candidates with no current reviewed entry to the LLM lane.
2. Build the authoring intent from the frozen hypothesis, recipe semantics, bound roles, selected
   parameters, grain, temporal contract, row selections, policies, target reading and catalog
   snapshot. Do not send irrelevant catalog columns.
3. Label recipe prose and LLM-enriched descriptions as context with **authority labels**;
   source-attested and human-confirmed facts outrank proposals. The LLM cannot promote its own
   metadata.
4. Reject physical refs outside the frozen read set, missing grains, cross-currency sums without a
   conversion policy, unsupported joins, future leakage, undeclared row-selection values.
5. Bounded author/critic loop. Automatic repair may fix schema/grammar; a missing **business**
   decision becomes `NEEDS_USER_INPUT`, not another guess.
6. Persist provider contract, dispatches, prompt/schema identities, result hash, critic evidence.
7. Provider absence blocks **only LLM-strategy members** (parent §4's per-member row). Reviewed
   deterministic members in the same set remain usable.

**Acceptance:** `posted_debit_amount` reaches READY with **zero provider calls** and its sealed
member proves `REVIEWED_RECIPE_BLUEPRINT` from trace evidence · an unreviewed recipe produces a valid
V3 proposal and preview artifact · the same candidate with a missing grain makes **zero** provider
calls and returns the grain blocker · a derivable-but-unreviewed recipe takes the **LLM** route and
its sealed member says `LLM_AUTHORED`.

### Step 5 (CHILD) — durable coordinator and preview generation

**5a — the coordinator**

1. `POST /code-generation-jobs/plan` — read-only cost/readiness preview. Returns ordered selected
   options, per-member strategy, deterministic-vs-LLM counts, estimated provider calls and token
   ceiling, required user decisions, and **the blockers and warnings from the parent's decision
   service** — not from a second implementation.
2. `POST /code-generation-jobs` — the one explicit write/spend action. Records the request,
   immutable selection revisions and authoring plans in one transaction, then enqueues.
3. The coordinator waits on **durable formula states**. It does not poll providers and does not hold
   a transaction open.
4. When every buildable member is READY, derive and validate the build declaration from confirmed
   facts: population spine, grain, cadence, availability promise, operand facts, policy
   realizations, empty-window behaviour, environment. Ask the user only for facts that cannot be
   derived.
5. Record the build set — ▲ **with the pinned `formula_draft_id` + `formula_content_hash` per member
   (parent §11)** — the action authorization revision (parent §0.1) and the generation request,
   preserving selected order.
6. The existing generation worker restores, admits, compiles, renders and seals.
7. Per-member failures are stored. "Continue with ready members" is allowed **only** after explicit
   user confirmation that creates a **new job identity**. Never silently drop a selected feature.
8. `GET /code-generation-jobs/{id}` plus an event/progress projection.
9. Cancellation stops not-yet-started provider calls and future stages. It **cannot** claim to cancel
   a provider call already in flight.

**5b — the frontend journey**

1. Keep per-row "Draft formula" for inspection without selection.
2. Replace the decision rail's readiness wording with per-selection methods:

   ```text
   1 reviewed formula · no AI cost
   2 AI formulas required · estimated cost and provider-call ceiling shown at confirmation
   1 needs a grain decision
   ```

3. "Prepare formulas and code" is the explicit action. Cost confirmation appears only when at least
   one member uses the LLM.
4. A generation workspace reached from the returned job id: Selected · Preparing formulas ·
   Validating · Generating code · Code ready · **Sandbox executed** · **Sandbox published** ·
   Production eligibility. ▲ **Seven stages, not six** — sandbox execution and sandbox publication
   are separate acts (parent §2, §3), and a UI that merges them re-creates the merge parent §4
   explicitly undid.
5. Per member: recommendation origin · formula method · exact formula and assumptions · bound
   tables/columns, grain, event time, window, filters, policies · validation findings · code files
   and lineage · **all six** action decisions from the server.
6. Plain-language badges: "Reviewed recipe formula" · "AI-authored from recipe" · "AI-proposed
   feature and formula" · "Sandbox ready — production certification pending".
   ▲ **A badge is never computed in the browser.** Parent §7: the client must not supply readiness,
   blocker codes, formula method, certificate identity or roles.
7. ▲ **Warnings must be DISPLAYED, not merely returned** — parent D3: *"A warning that is computed
   and dropped is worse than no warning."* Today `SuggestionCard.tsx:210` renders
   `gold_evaluation_unproven` as a **blocker** line; that copy becomes warning-shaped and does not
   disappear.
8. Direct clearing actions for missing grain, ambiguous event time, currency/reversal policy, stale
   metadata. After resolution, regenerate a **new considered revision** rather than mutating the
   frozen one.
9. Conceptual patterns show "Save idea / Specify computation"; governed model outputs show
   "Configure model". Neither shows a deterministic Generate Code button.
10. Reuse `FeatureExecutionScreen` after sealing, and add a real active-publication read before ever
    showing "Published".

**Acceptance:** browser closure and reload do not interrupt the job · duplicate submission creates no
duplicate selections, LLM calls, build set or generation request · the whole journey is possible with
keyboard and screen reader · every button label states what it writes or spends · **all** status and
blocker wording comes from the backend.

### Steps 6, 7, 8 (PARENT) — sandbox split, production boundary, gold relocation

Not this plan's work. The previous revision's Phase 5 proposed to build these and is **dissolved**
into parent steps 3, 7 and 8. This plan consumes the result and, in step 5b item 4, renders the
sandbox split honestly.

### Step 9 (SHARED) — the two evaluation programmes, and growing the corpus

**Parent** owns both programmes (§12, Phase E) — including the ▲ **unresolved product ruling**, §12
piece #4: what "the compiler produced the right thing" MEANS (byte-identical IR? equivalent results
within tolerance?). **This plan owns the supply side**, and it is **not a prerequisite for LLM
preview generation**:

1. A "Formula review" queue beside recipe review, showing the derived blueprint or its named
   derivation refusal.
2. Seed the **90** structurally derivable recipes as **proposed** blueprints — never reviewed ones
   (§1.2).
3. For the remaining deterministic recipes, let the LLM propose a semantic blueprint draft. It may
   accelerate authoring; it can never approve itself.
4. Reviewers confirm banking semantics, grain, event time, filters, row selections, window,
   aggregation, null/empty behaviour, currency and policy references against worked examples.
5. Store approved expectation revisions append-only with blueprint hash, recipe revision hash,
   approver roles, test vectors and status. Replace static source membership with a database-backed
   current-revision reader **only after** equivalence tests cover the existing three pins.
6. ▲ **The two V1 entries get genuine V2/V3 fixtures, or they stay on the LLM route.** There is no
   V1 serving role to delete afterwards (§1.3).
7. Promote a successful candidate-specific LLM formula only after abstracting physical refs back to
   semantic roles and proving it across more than one valid binding. **Never promote one customer's
   column names as the universal recipe.**

▲ **The cost of a single approval is parent §21's, and it is the real ceiling:** per clean case,
approved `recipe_review_event` rows from **every** role `required_reviewer_roles` names — at minimum
three for a `deterministic_formula`, more with privacy/risk/model-output refs — under the
multi-person rule (`ReviewValidityV1.single_identity_violation`). Nine more clean cases is **at
least 27 approval events plus nine reviewed fixtures**, not nine signatures.

**Acceptance:** adding an approved expectation requires no source edit but still requires the full
review/event/test-vector contract · revocation immediately stops new deterministic selection without
rewriting past artifacts.

### Step 10 (SHARED) — journey tests, then remove the legacy path

**Real-path tests, not source-inspection substitutes.** These join parent §19's decisive test and its
reintroduction table:

1. Reviewed `posted_debit_amount`: hypothesis → recommendation → selection → **zero LLM calls** → V3
   formula → code artifact → sandbox execution → sandbox publication.
2. Unreviewed deterministic recipe: same journey → LLM author+critic → code → sandbox with warning.
3. LLM-origin recommendation: LLM formula path, honest origin **and** method.
4. **Mixed-METHOD set**: reviewed + LLM-fallback sealed together, correct per-member provenance,
   **two different certificates required** (parent §10).
5. Gold absent: formula and code exist, sandbox allowed with warning, **production materialization**
   blocked — not only production publication (parent §3).
6. Current method certificate: production allowed only for members matching that method identity.
7. Stale / mismatched certificate: production refused.
8. Missing grain: blocked **before any provider call**.
9. Ambiguous event date: user decision required; no guessed date.
10. Currency, reversal, status and target-leakage negatives.
11. Provider unavailable: the deterministic member succeeds; the LLM member reports the provider
    blocker (parent §4's per-member row).
12. Deterministic producer defect: **no silent LLM fallback**.
13. Double-click / reload / cancellation / queue redelivery idempotency.
14. Retirement during multi-turn LLM authoring stops the next provider call.
15. Old and new route **decision** equivalence over the full six-action matrix (parent §8.3 — compare
    decisions, not routes).
16. Direct API permission and stale-snapshot attempts cannot bypass UI policy.
17. ▲ **Derivable-but-unreviewed**: the sealed member says `LLM_AUTHORED`, never
    `REVIEWED_RECIPE_BLUEPRINT`.
18. **Mutation tests** must fail under each reintroduced defect: gold on preview · origin-as-method ·
    missing provenance treated as pass · selection-triggered LLM call · silent member dropping ·
    **routing on `blueprint_derivable`** · **recomposing `authoring_config_hash` without
    `provider_contract_hash`**.

▲ Read pytest's summary line. `grep -c "^FAILED"` silently matches nothing against coloured output
and reports a false pass.

**Then the cutover:**

1. Build and test migrations on a scratch restore of the branch-owned database.
2. Back up before applying; verify the ledger before and after.
3. **Deploy backend workers before the frontend**, so every UI state has a server meaning.
4. Run the real CIB example through Kind/Postgres:

   ```text
   Hypothesis: customers with rapidly rising posted debit activity may require review
   Grain:      public.bo_cib_customer.cust_num (or the governed transaction-to-customer key)
   Reviewed member: posted_debit_amount where applicable
   LLM member:      debit amount growth, current 30d vs prior 30d
   ```

5. Verify generated Spark/Kedro files are visible, artifact hashes reproduce, sandbox results have
   one row per declared grain, and production stays blocked without a current certificate.
6. ▲ **Delete `POST /materialization-runs`** once the run sheet (`frontend/src/api.ts:4257`) and the
   three test modules (`test_materialization_runs.py`, `test_materialization_e2e.py`,
   `test_seam_walkthrough.py`) are migrated. **No adapter** (D9, parent D2/§8.3).
7. Keep the cluster branch-owned until its migration lineage is merged into main and the image
   carries the same migration set.

---

## 5. API response shape

One honest member record. ▲ **Six actions, because the parent defines six** — the previous revision
returned three and silently merged the sandbox acts:

```json
{
  "option_id": "opt_...",
  "feature_name": "posted_debit_amount_30d",
  "candidate_origin": "recipe",
  "recipe_id": "posted_debit_amount",
  "expectation_ref": "posted_debit_amount",
  "expectation_generation": "v2",
  "formula_strategy": "REVIEWED_RECIPE_BLUEPRINT",
  "formula_state": "READY",
  "formula_draft_id": "fd_...",
  "formula_content_hash": "...",
  "method_identity_hash": "...",
  "actions": {
    "author_formula":         {"allowed": true,  "blockers": [], "warnings": []},
    "generate_preview":       {"allowed": true,  "blockers": [], "warnings": []},
    "execute_sandbox":        {"allowed": true,  "blockers": [], "warnings": []},
    "publish_sandbox":        {"allowed": false, "blockers": [{
        "code": "VERIFICATION_NOT_CURRENT",
        "reason": "No passing sandbox verification covers this artifact yet.",
        "next_step": "Run the sandbox execution first."}], "warnings": []},
    "materialize_production": {"allowed": false, "blockers": [{
        "code": "METHOD_CERTIFICATE_MISSING",
        "reason": "The deterministic recipe compiler has not been certified for production.",
        "next_step": "Run Formula quality certification from Governance."}], "warnings": []},
    "publish_production":     {"allowed": false, "blockers": [{
        "code": "METHOD_CERTIFICATE_MISSING",
        "reason": "The deterministic recipe compiler has not been certified for production.",
        "next_step": "Run Formula quality certification from Governance."}], "warnings": []}
  },
  "policy_version": "...",
  "evidence_hash": "..."
}
```

An unreviewed recipe differs in exactly the facts that differ:

```json
{
  "candidate_origin": "recipe",
  "expectation_generation": null,
  "formula_strategy": "LLM_AUTHORED",
  "warnings": [{
    "code": "LLM_AUTHORING_REQUIRED",
    "reason": "This recipe has no reviewed executable formula; AI will propose one for these bound columns."
  }]
}
```

A **derivable but unreviewed** recipe differs again, and this is the distinction §1.2 exists for:

```json
{
  "formula_strategy": "LLM_AUTHORED",
  "warnings": [{
    "code": "BLUEPRINT_DERIVED_NOT_REVIEWED",
    "reason": "A blueprint can be derived from this recipe's definition, but no reviewer has approved it.",
    "next_step": "Send it to the formula review queue."
  }]
}
```

▲ The API never reports an LLM-authored recipe formula as "reviewed recipe formula", and never
reports a **derived** blueprint as a **reviewed** one.

---

## 6. Observability and operating controls

Record, without logging sensitive values:

* selected members by candidate origin and formula method;
* deterministic vs LLM authoring latency;
* provider calls / tokens / cost per job and per member;
* LLM parse, critic, admission and repair outcomes;
* blocker counts by **action** and code — six actions, so the counts are comparable across the gate;
* **percentage of unreviewed recipe candidates reaching preview and sandbox** — ▲ this is the number
  parent §0.2 fact 2 asks for, measured continuously rather than claimed once;
* method-certificate production refusals, split by materialization vs publication;
* **deterministic routes with non-zero provider calls** (alert: must remain **zero**);
* **`REVIEWED_RECIPE_BLUEPRINT` seals whose expectation ref is not in the reviewed registry**
  (alert: must remain **zero** — the §1.2 hole, watched at runtime);
* formula jobs with missing or contradictory member provenance (alert **and refuse**);
* queue age, retries, cancellations, retired-draft stops.

Operator views for stuck jobs and provider faults. Product blockers are not paged as incidents;
platform failures are.

---

## 7. Adversarial review of this plan

### "Let the LLM write all 295 formulas and call the problem solved"

Faster, unsafe, and it misstates the number. Some candidates lack a grain, a usable event time, a
currency policy or a non-duplicating join. The LLM path opens only after semantic eligibility and
never converts a missing governed fact into a guess. **295 is a candidate ceiling, not a forecast**
(§0).

### "Recipe origin proves the formula is reviewed"

False in the current lane: **every** formula draft uses the LLM (§1.4, §1.5). Worse, the database
currently *encodes* the confusion — migration 1088's CHECK ties `origin = 'recipe'` to a reviewed
blueprint revision. §3.4 separates them and the method is re-derived from durable run evidence at
sealing.

### "There are three reviewed expectations, so all three can take V3 deterministic authoring"

False: two are V1. The version-aware resolver stops a union-membership boolean laundering V1 review
into V3 execution authority (§1.3).

### ▲ "Ninety blueprints are derivable, so ninety recipes can take the deterministic path"

**The most dangerous attack in this list, because the code makes it easy.** `derive_blueprint_v2` is
pure and cheerful; nothing downstream asks whether a human agreed. Derivation is structure; review is
governance. §1.2, parent §10.1, and step 10 test 17.

### "Automatically fall back to the LLM if deterministic authoring fails"

That hides a broken reviewed blueprint, changes the cost and changes the production certificate. The
fallback is explicit and mints a new identity (D2).

### "Trigger formula generation as soon as the checkbox is selected"

Surprise spend, expensive exploration, hard to cancel. Selection is free; only the confirmed job
starts provider work (D5).

### ▲ "Recompute the draft identity from the new strategy facts"

The previous revision proposed exactly this. It would void the money guard **and silently un-retire
every withdrawn formula**, because retirement is checked only when the INSERT loses the unique index
(§3.3). Fold, never replace — and the folding belongs to parent step 2.

### "Put one authoring method on the artifact"

A mixed artifact carries both. Per-member provenance, per-member method identity and per-member
certificate binding are non-negotiable (D7, parent §10).

### "Keep gold on the readiness ladder until the UI is finished"

That preserves the dead end; removing it without the production gate removes protection instead of
relocating it. The two land atomically — **parent step 8**, and this plan does not touch it.

### "Let the browser orchestrate the APIs"

A closed tab or a failed request leaves partial selections, drafts and builds with no owner. The
durable coordinator owns progress and idempotency (D6).

### "Promote one successful physical formula into the global recipe registry"

That overfits one bank's schema and can embed an accidental debit-sign or date choice. Promotion
first abstracts to semantic roles and requires review plus multiple binding examples (step 9).

### "All formula-shaped LLM output is safe after JSON validation"

Schema validity is not banking correctness. Grain, time, target leakage, row selection, join
cardinality, status/reversal/currency rules, permissions, engine support and sandbox results remain
independent gates.

---

## 8. Definition of done

This **child** plan is complete when all of the following hold. ▲ Items that depend on the parent
are marked — they are not this plan's to claim, and it cannot finish without them.

* a valid recipe recommendation with **no reviewed expectation** produces an honestly labelled
  LLM-authored formula and inspectable code;
* a current reviewed **V2 registry entry** produces the formula deterministically with **zero
  provider calls**;
* a **derivable but unreviewed** recipe takes the LLM route, and no seal anywhere claims
  `REVIEWED_RECIPE_BLUEPRINT` for it;
* the two **V1** entries route to the LLM and no V1 serving path is invented in order to retire it;
* recipe **origin** and authoring **method** are stored separately everywhere, and origin no longer
  implies review in the schema;
* selecting features never spends, and duplicate submission never spends twice;
* missing grain / operands / policies block before unsafe authoring or execution;
* code preview and sandbox are not blocked by certification alone;
* conceptual and model recipes never advertise deterministic code generation;
* the frontend shows origin, method, formula, assumptions, code, blockers, next steps and **all six**
  action decisions, with warnings rendered rather than dropped;
* the reviewed-expectation programme can grow deterministic coverage without source edits or
  self-approval by the LLM;
* the full journey passes against Kind/Postgres with **a reviewed recipe and an unreviewed
  recipe/LLM fallback in ONE build set** — the mixed-**method** case, which parent §0.2 fact 4
  records is impossible until this plan lands;
* **(PARENT)** production materialization *and* publication are fail-closed on a current,
  method-matched certificate for **every member**;
* **(PARENT)** old and new APIs return the same decision for the same action, and the old route is
  deleted rather than adapted.

▲ **What "done" does NOT mean.** Production stays closed for every recipe until the reviewed corpus
grows past its single clean case — at least 27 approval events and nine reviewed fixtures (step 9,
parent §21) — and until §12's eight compiler-certification pieces exist, of which #4 is an
undelivered product ruling. That is the intended invariant, not a defect, and it is why preview must
never be gated on the thing that cannot yet happen.

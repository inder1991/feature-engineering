# Recipe-to-code: reviewed formulas, LLM fallback, and one generation journey

**Date:** 2026-08-22 · amended 2026-08-22 to become a child plan · amended again to fold the owner's
**third review** (five rulings)
**Status:** implementation plan only; this document changes no runtime behaviour
**Branch:** `feature/asset-detail-reapply`

▲ **What the third review changed HERE.** Four of the five rulings are the parent's to hold; this
document carries their consequences and nothing more:

| Ruling | Consequence in this plan |
|---|---|
| **1 — legacy pinning** | step 1 records the measurement that already exists (all four build tables **zero**) and adds the two row counts nobody has measured — `formula_draft`, `formula_draft_retirement`. Parent §11.0 owns the rule |
| **2 — two comparisons** | parent §12 piece #4 is **no longer an open ruling**. It costs this plan a bigger supply obligation: a compiler case needs an approved IR **and reviewed test data** (step 9) |
| **3 — the money guard** | §3.3's *"two remedies, pick one"* is **gone**. Retirement decouples from identity; this plan still supplies exactly two facts and still touches no composition (parent §11.1.1) |
| **5 — delete the legacy route** | ▲ **the parent explicitly supersedes this document's earlier "thin adapter" commitment.** Step 10's both-route equivalence test is replaced by route-absence and direct-queue-bypass |

### ▲ What the PRINCIPAL-ARCHITECT VERDICT changed here — revision four

The verdict of 2026-08-22 (against `3c52a9de`, suite green at 13,154) found **ten stop-ship items**
across the pair. Six are wholly the parent's. **Four land on this document, and one of them moves a
whole step:**

| Finding | Consequence in this plan |
|---|---|
| ▲ **P0-2 — the step 2 / step 4 identity CYCLE** | **§3.1's resolver and §3.3's `formula_draft_authoring_plan` are PROMOTED into step 2.** Parent identity V2 composes `formula_strategy` and `strategy_identity_hash`; those facts cannot be created by a step that waits for V2. **Migration 1104 moves with them.** Step 4 keeps the lanes, not the contract |
| ▲ **P0-9 — "Try AI formula" vs server-owned method** | D2's *"the client may explicitly request an LLM retry"* becomes a **durable server-authored `formula_method_override_revision`** (parent §11.3, migration 1108). The browser never sends a strategy; the resolver consumes the override as an input fact |
| ▲ **P0-10 — spend authorization is not designed** | D5's *"selection never spends"* gets its durable half: `llm_spend_authorization_revision` (parent §11.2), checked **before every provider call and every repair turn** — which is step 4c item 5's loop |
| ▲ **P0-1 — the sandbox lane does not exist** | steps 6–8 were *"not this plan's work"* on the assumption that `EXECUTE_SANDBOX` worked. **It does not** (parent §9.0). Step 10's cutover cannot delete the legacy handler until it is BUILT, and step 5b item 4's seven-stage UI describes two stages nothing currently performs |

▲ **And one measurement in this document was wrong, in the direction that matters.** §1.2 and step 9
repeat a docstring saying *"no `recipe_review_event` row exists yet"*. **Live: 996 approved rows,
covering all 317 recipes, zero single-identity violations — and ZERO carrying a
`formula_expectation_hash`** (parent §21). The rule is untouched; the scarcity is the opposite of
what this plan assumed. See §1.2 and step 9.

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
        Parse -> deterministic validation -> admission          <- BOTH METHODS, identically
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

▲ **CORRECTED — the last clause of that docstring is now FALSE, and the sentence around it is not.**
Live measurement, 2026-08-22: **996 approved `recipe_review_event` rows**, covering **all 317**
recipes, by 6 reviewers, with **zero** single-identity violations — and **zero** carrying a
`formula_expectation_hash` (parent §21). So the honest statement is *"there are no qualifying
formula-expectation approvals"*, never *"no review events exist"*. ▲ **The argument is untouched and
is exactly why this section exists:** those 996 approve **recipes**; not one of them says a human
looked at a **formula expectation**. Correct the docstring in step 2, and do not let the correction
be read as removing the rule it justifies.

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

▲ **P0-9 — and as written that sentence contradicted parent §7**, which rules that the client must
not supply formula method. Both intentions are right; the **mechanism** was missing. **The retry is a
durable, server-authored `formula_method_override_revision`** (parent §11.3, migration 1108): the
browser asks for an override and never sends `formula_strategy=LLM_AUTHORED`; the server **verifies
the deterministic refusal it names is recorded and current**, binds an actor, a reason, a spend
authorization (parent §11.2) and an expiry; and the resolver below consumes the revision as an
**input fact**. ▲ **The override changes the evidence, never the authority** — this selector remains
the only thing that decides a method.

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

▲ **And the owner's third review says something this plan must not paper over: the money promise and
the RETIREMENT promise are two different promises riding on one mechanism** (parent §11.1.1).
Retirement is enforced only when the unique-index INSERT *loses*, so "selection never spends" and
"a withdrawn formula is never rebought" are today the same code path — and fixing either moves both.
The parent decouples them; **the visible consequence in this plan is that a retired candidate must
refuse before the coordinator ever counts it as a costed member** (step 5a item 1).

▲ **P0-10 — and "an approved cost ceiling" had no owner anywhere.** This plan quotes one in step 5a,
the parent's §4 has a blocker row for its absence, and **no table, migration or contract defined it**.
A confirmation modal is a UI event: it authorizes nothing and survives nothing. **Parent §11.2's
`llm_spend_authorization_revision` (migration 1105) is the durable half of D5**, and the consequence
in this plan is specific: **step 4c's bounded author/critic loop and its automatic repair turns must
check and consume the authorization before EVERY provider call**, not once per job. A bounded loop
with an unbounded repair path is unbounded.

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
be right about one member. ▲ **CORRECTED AGAIN in revision five — that shape is stale twice over
(parent §10.3):** it omits `certificate_kind`, so it still permits only ONE certificate per member;
and it matches every kind against `method_identity_hash`, which is an **authoring** identity, so a
`COMPILER_RUNTIME` certificate would be matched against a subject it does not describe. The shape is
`production_attempt_member_certificate (attempt_id, member_name, certificate_kind,
certificate_revision_id, subject_identity_kind, subject_identity_hash)`. ▲ **And its parent —
`method_certificate_revision` — DOES NOT EXIST YET**: there is no certificate table in the schema at
all, only eval runs, cases and attempts (parent §10.3).

### D8 — *(deleted — see parent §14, "Certification is not feature approval")*

### D9 — No new rollout flags

The product is pre-live. Implement the canonical path, test it, deploy it to the branch-owned Kind
cluster, and delete the legacy path. Rollback is the previous image. Do not create dark gates that
let the two behaviours drift.

▲ **CORRECTED — no thin adapter, and the owner's third review says so EXPLICITLY.** The previous
revision's opening promised *"keep the old materialization API temporarily as a thin adapter, then
delete it."* **The parent supersedes that commitment**: DELETE the route and its execution entry
points, build no adapter, not even temporarily. Parent §8.3 explains why an adapter is a bypass
rather than a simplification — `POST /materialization-runs` compiles, renders, seals, runs L0 and
**can publish**, so mapping it to one `GENERATE_PREVIEW` decision would let its later execution and
publication proceed under a decision made for a weaker act. **There is no "old API preview"
operation to map**, and an adapter that orchestrated the stages properly would simply *be* the
canonical lane with a legacy entrance attached.

▲ **The deletion surface is bigger than this section used to say, and the extra item is the one that
matters:** parent §1 D2 enumerates it, and it includes **`enqueue_materialization` and the legacy
queue handler**. A route deleted while its producer survives is a renamed bypass — which is exactly
what step 10's direct-queue-bypass test exists to catch.

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

▲ **REVISION FIVE — `strategy_identity_hash` needs an EXACT payload, and it did not have one.** It is
folded into parent identity V2 (§11.1.1), so an under-specified payload is an under-specified draft
identity — and every later reading of "was this the same strategy?" inherits the ambiguity. The JCS
payload, versioned:

```
strategy_identity_version           -- so the composition can change without silence
resolver_policy_version
considered_revision_id · option_id
reviewed_expectation_revision · expectation_hash · expectation_generation
blueprint_revision · blueprint_content_hash · binding_hash        -- deterministic lane
review_validity_evidence                                          -- WHY it counted as reviewed
formula_method_override_revision_id                               -- parent 11.3, when present
provider_contract_hash                                            -- LLM lane
catalog_snapshot_hash · binding_plan_hash                         -- the frozen world it resolved in
```

▲ **`blueprint_derivable` and `blueprint_bindable` stay OUT** — measurements, per parent §11.1. And
`§3.3`'s `formula_draft_authoring_plan` must carry every field above, not a subset: the plan row is
what a worker re-reads instead of recomputing, so anything absent from it is a fact the worker will
have to derive again, differently.

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

▲ **P0-2 — THIS TABLE AND ITS RESOLVER MOVED TO STEP 2.** The verdict found a cycle: parent §11.1.1
composes identity V2 from `formula_strategy` and `strategy_identity_hash`, which this section
creates — while step 2 below forbids writing a plan row until step 2 finishes. **Step 2 needed step
4's facts; step 4 could not start until step 2 ended.**

> **RULED: promote, do not split.** §3.1's resolver and this table become **step 2** work, and
> **migration 1104 moves with them**. Parent identity **V2 activates at the END of step 2**, once
> these facts are persisted. *(The verdict's alternative — 2A / resolver / 2B with an atomic
> mid-programme activation — is recorded as the reversal path in parent §17. An identity that cannot
> be composed without strategy facts is telling you the strategy contract is foundation.)*

▲ **What stays at step 4 is the LANES, not the CONTRACT** — deterministic instantiation, the LLM
fallback, the worker dispatch. Step 4 then *reads* a plan that already exists rather than defining
the vocabulary the foundation depends on.

Append-only `formula_draft_authoring_plan`, keyed one-to-one by `formula_draft_id`
(**migration 1104 — written in STEP 2**; parent §17 re-reserves the whole range):

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

▲ **REVISION FIVE — the identity companion can disagree with the draft.** Parent §11.1.1's
`formula_draft_authoring_identity.config_hash` is not relationally tied to
`formula_draft.authoring_config_hash`, so a V2 companion row can be attached to a draft whose stored
config hash is something else — and the identity version then describes a draft it does not match.
**Add a composite FK (or an equivalent unique target on `formula_draft (formula_draft_id,
authoring_config_hash)`)** so the companion can only attach to the draft it actually describes. Same
argument as parent §11.0.1: worker-time checking is not a constraint.

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

▲ **RULED, third review — and NEITHER of the two remedies this section used to offer.** The owner
refused both the one-time re-spend and the hash-preserving backfill, because they answer the money
question while the question that bites is retirement. **Parent §11.1.1** is the design; the four
facts this plan must build against are:

1. **Retirement moves off the identity hash** onto a *retirement scope key* — the five identity
   fields that are not the authoring configuration — and is checked **before** the INSERT and
   therefore before any enqueue.
2. **Legacy drafts keep identity V1 explicitly**, constant and all, recorded as what it was: a
   record of the defect, never a claim about a historical model configuration.
3. **Identity V2 is the corrected composition**, into which this plan's two facts fold.
4. **No automatic re-spend.** A V2 request that finds only a legacy draft returns it as an auditable
   preview marked `LEGACY_CONFIG_UNPROVEN` and enqueues nothing; regeneration needs explicit
   approval and cost confirmation.

**This plan supplies `formula_strategy` and `strategy_identity_hash`, and nothing else.** It does not
compute the composition, does not write the tombstones, and must not "help" by correcting the
`getattr` in passing — parent §11.1.1's whole point is that this looks like a one-line fix and is
two governance rules.

### 3.4 Fix the generalized work-item model

**Migration 1107** (▲ renumbered — parent §17) separates `authoring_work_item.origin` from authoring
strategy. The audit this section demanded is **DONE: `authoring_work_item` is 0 rows** on the live
cluster (parent §0.3), so **the empty branch applies and the old constraint is replaced directly** —
no backfill, no legacy-undetermined rows, no sealing-eligibility question to answer.

▲ **Re-measure inside the migration's transaction anyway.** The rule governs, not the reading — and
if a row does appear, the "rows present" branch stands: preserve them, backfill only facts the old
constraint logically guaranteed, and mark anything unprovable legacy-undetermined and **ineligible
for sealing** (the same posture parent §10 takes toward existing provenance rows —
`METHOD_IDENTITY_UNRECORDED` is the honest answer, regeneration the honest remedy).

`AuthoringWorkItemV1` carries:

```text
candidate_origin: recipe | llm_intent | user_definition
formula_strategy: REVIEWED_RECIPE_BLUEPRINT | LLM_AUTHORED
```

Remove `WorkItemOrigin.authors_from_reviewed_blueprint` and migration 1088's origin-keyed CHECK. The
new invariant is strategy-based: `reviewed_blueprint_revision` is present **exactly** for
`REVIEWED_RECIPE_BLUEPRINT`.

### 3.5 Durable journey aggregate

**Migration 1109** (▲ renumbered — parent §17):

* `code_generation_job` — immutable request identity, considered revision, target reading,
  environment, requested action, requester, status, terminal details;
* `code_generation_job_member` — ordered option ids, selection revision, ▲ **the
  `selection_formula_binding_id`** (parent §11.0.1) rather than a loose draft id and hash, strategy,
  member state, blockers;
* `code_generation_job_event` — append-only stage events;
* links to build-set revision, **action authorization revision** (parent §0.1), generation request
  and sealed artifact.

▲ **The job's members carry the binding because the BUILD SET does** — parent §11: today
`build_set_member` is `(revision_id, position, selection_revision_id)` and nothing else, and
`restore_formula_v3.py:90` resolves the formula with `ORDER BY d.updated_at DESC LIMIT 1`. A newer
draft landing between request and worker changes what the build MEANS while its `content_hash` stays
put. The coordinator hands the parent's binding through; it does not re-resolve.

▲ **P0-6 — and a loose `(formula_draft_id, formula_content_hash)` pair was not enough.** It stops
*latest-draft-wins* and still permits a valid READY formula **belonging to a different selection** to
be pinned: the id exists, the hash matches, the build proceeds. Parent §11.0.1 makes the two agree in
the database through `selection_formula_binding`, with composite foreign keys to both source
identities. **This plan carries the binding id and never re-derives the pair from it.**

▲ **REVISION FIVE — one job carries SEVERAL actions, so one authorization link is wrong** (parent
§0.1.3). This journey performs `AUTHOR_FORMULA` then `GENERATE_PREVIEW`, and its workspace then shows
sandbox execution and publication; a single `requested_action` with a single authorization cannot
truthfully cover them:

```text
code_generation_job_action
    job_id · action · resource_identity_hash
    authorization_revision_id · decision_revision_id · state
    PRIMARY KEY (job_id, action)
```

**Each worker claims ONE action stage and revalidates that stage's own authorization and decision.**

States, closed and monotone:

```text
REQUESTED  PLANNING_FORMULAS  AUTHORING  READY_TO_BUILD  GENERATING_PREVIEW
PREVIEW_READY  BLOCKED  FAILED  CANCELLED
```

▲ **These states stop at `PREVIEW_READY` while §5b's workspace shows sandbox and production.** Settle
which this aggregate is. ▲ **It is a PREVIEW COORDINATOR** — terminal at `PREVIEW_READY`, with
immutable links to the sandbox and production attempts that follow. The alternative, owning the whole
journey, would make it a second authority over acts it does not gate (parent §3).

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
   counts for `formula_draft`, **`formula_draft_retirement`**, `authoring_work_item`,
   `feature_selection_revision`, `build_set_revision`, `generation_request`, `sealed_artifact_v2`.

   ▲ **Four of these are ALREADY MEASURED, and the answer decides a migration.** Live kind,
   2026-08-22: `build_set_revision=0`, `build_set_member=0`, `generation_request=0`,
   `sealed_artifact_v2=0` — which is why parent §11.0 takes the immediate-`NOT NULL` branch.
   **Re-measure at deploy anyway**: the rule governs, not the reading.
   ▲ **MEASURED 2026-08-22 — this item is CLOSED** (parent §0.3). `formula_draft` = **7** (4
   `FAILED`, 3 `BLOCKED`, 7 distinct identities) · `formula_draft_retirement` = **0** ·
   `authoring_work_item` = **0** · `feature_selection_revision` = **0** · `formula_authoring_run` = 3.
   ▲ **Two of those change a design rather than an estimate:** the tombstone backfill is **empty**,
   and **no live draft is in a previewable state** — so parent §11.1.1's "return the legacy draft as
   an auditable preview" branch has zero live subjects and must not assume one. **Re-measure inside
   each migration's transaction**: five branches now turn on a live row count.
3. Characterization tests pinning **317 / 298 / 19 / 295 / 3 / 90 / 1** — running the real
   registry, fold and deriver, not a hand-built fixture. ▲ **The last two are separate numbers on
   purpose** (§1.2): 90 derivable, 1 reviewed. A test that pins only "90" is how they get conflated.
   ▲ **These pins are the funnel's FIRST ROW, not the funnel.** Parent Phase B owns the versioned
   funnel — `funnel_version · registry_content_hash · code_revision · measured_at`, stage by stage
   with per-stage refusal histograms — and the owner's third review requires it **recorded before
   any coverage claim**. This plan may not state a preview-coverage number until that row exists.
4. Route-call enumeration tests showing which endpoints consult which policy today. ▲ **Its purpose
   changed with ruling 5:** it is no longer the before-state for a decision-equivalence comparison —
   there will be no second route to compare with — it is the **deletion inventory**, the **deletion
   inventory**. ▲ **Revision five corrects what that inventory PROVES:** it shows every legacy act
   has a **named** canonical home, which is not the same as a **working** one. Parent §9.0
   established that sandbox execution does not exist — the route records a row no worker claims.
   **It is a replacement checklist, not evidence of present equivalence**, and the deletion's entry
   condition is that those replacements EXECUTE.
5. Freeze applied migrations; corrections get new files; ledger verification runs in CI.

**Acceptance:** no behavioural change; clean ledger; baselines recorded; the 90-vs-1 distinction is
pinned by a test.

### Step 2 (PARENT, ▲ with one CHILD contribution) — the enlarged foundation

▲ **REVISED by P0-2.** This step used to be entirely the parent's, and that is what created the
cycle. **§3.1's strategy resolver and §3.3's `formula_draft_authoring_plan` (migration 1104) are now
built HERE**, because parent identity V2 composes `formula_strategy` and `strategy_identity_hash` and
cannot be activated without them. **Identity V2 activates at the END of this step.**

▲ **The ordering inside the step is load-bearing, and it is the whole reason the promotion is safe:**

```
1. tombstones written and verified            (parent 11.1.1; the live backfill is EMPTY)
2. the strategy resolver + authoring plan     (this plan's 3.1 and 3.3, migration 1104)
3. identity V1 recorded explicitly            (parent 11.1.1)
4. identity V2 composed and ACTIVATED         (parent 11.1.1, using the facts persisted at 2)
```

**The rest of the step is the parent's, and this plan depends on all of it and must not duplicate
any:**
server-derived roles (parent §0.1) · formula-draft pinning, `NOT NULL` on today's measurement
(§11, §11.0) · the money-guard composition, identity **V1 preserved / V2 introduced** (§11.1,
§11.1.1 — into which §3.3's two facts fold) · **retirement tombstones, written and verified BEFORE
V2 activates** (§11.1.1) · immutable action authorization (§0.1) · per-member method identity
(§10, §10.1).

▲ **The ordering rule that used to sit here has been SUPERSEDED by the promotion, and the hazard it
named has not gone away.** It said no `formula_draft_authoring_plan` row may be written until step 2's
tombstones are verified — which, with the table now inside step 2, becomes an **intra-step** rule:
**stage 2 of the sequence above never runs before stage 1**, and **identity V2 never activates before
stage 2 completes**. Authoring drafts under V2 while tombstones are missing re-buys withdrawn
formulas; composing V2 from strategy facts that are not yet persisted composes it from **absent**
facts, which is the same class of defect as the constant it replaces.

▲ **Also new here, and both are parent-owned but visible to this plan:** `llm_spend_authorization_revision`
(parent §11.2 — D5's durable half) and `selection_formula_binding` (parent §11.0.1 — which §3.5's
job members now carry instead of a loose pair).

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

▲ **STATUS 2026-08-23 — 4a is DONE and 4b is WIRED, POSTURE OFF.** The facts assembler
(`formula_strategy_facts.py`) reads the per-option decision row with the frozen idea as honest
fallback, probes each registry PER GENERATION (never the union — two of three reviewed recipes are
Formula V1), normalizes the three origin vocabularies once, and the route resolves BEFORE the draft
identity exists, persisting `formula_draft_authoring_plan` in the request transaction. The worker
dispatches by the STORED plan and the reviewed lane's rebuild — derive, verify against the pinned
bytes, bind, hand `run_authoring_v2_replay` the bound object — is implemented and tested behind an
injectable context loader. ▲ **What is deliberately NOT on:** the candidate's grounding context is a
serving-time product nothing persists to draft time, so the resolver routes reviewed candidates to
the LLM with `REVIEWED_LANE_UNAVAILABLE` recorded. **The successor increment is context persistence
plus a posture flip — a loader and a flag, never a re-derivation of this routing.**

▲ **MOVED TO STEP 2: the resolver and the authoring plan** (§3.1, §3.3, migration 1104) — P0-2. What
remains at step 4 is everything that **reads** that contract: the two lanes, the worker dispatch, and
the method override. Items 1, 2 and 7 of 4a below are therefore **built in step 2 and merely verified
here**; they are left in place so the acceptance criteria stay whole.

**4a — resolve and persist the strategy** *(contract built in step 2; this step consumes it)*

1. ▲ *(step 2)* Implement the pure selector; table-driven tests over every combination, including the
   `blueprint_derivable`-flips-nothing case (§3.1). ▲ **And a `formula_method_override_revision`
   present for this selection flips the strategy to `LLM_AUTHORED` — as an INPUT FACT, never as a
   client-supplied label** (parent §11.3).
2. Version-aware expectation resolver; **never union membership alone** (§1.3).
3. `posted_debit_amount` resolves to executable V2 reviewed blueprint.
4. The two V1 entries resolve to `v1_legacy` → `LLM_AUTHORED` (§1.3 — no V1 serving role to retire).
5. Derivable-but-unreviewed resolves to `LLM_AUTHORED` + `BLUEPRINT_DERIVED_NOT_REVIEWED` (§1.2).
6. Bind a reviewed blueprint against the **exact frozen candidate context**; a binding mismatch
   refuses deterministic authoring **by name**.
7. ▲ *(step 2)* Persist `formula_draft_authoring_plan` in the same transaction as the draft
   request/outbox.
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
   change. ▲ **It does so by creating a `formula_method_override_revision`** (parent §11.3), whose
   `original_refusal_code` the **server verifies is recorded and current** — an override naming a
   refusal that did not happen is refused, or "Try AI formula" is a client-chosen method with extra
   steps.
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
   decision becomes `NEEDS_USER_INPUT`, not another guess. ▲ **The spend authorization is checked and
   consumed BEFORE every provider call and every repair turn** (parent §11.2) — a bounded loop with
   an unbounded repair path is unbounded, and an exhausted authorization **stops** the job with
   `COST_AUTHORIZATION_EXHAUSTED` rather than silently truncating the critic and presenting the
   result as final.
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
   ▲ **A retired candidate and a legacy-only candidate are refused HERE, in the plan**, before
   either is counted as a costed LLM member: `FORMULA_DRAFT_RETIRED` and
   `LEGACY_REGENERATION_NOT_APPROVED` (parent §11.1.1). A cost estimate that quotes work the
   platform will then refuse is a quote for a purchase nobody can make.
   ▲ **And the plan endpoint READS a spend authorization; it never creates one** (parent §11.2). The
   estimate is what a person approves; the approval is what the write endpoint below records. A plan
   call that minted its own ceiling would be the modal-as-money-guard defect with a server address.
   ▲ **It also `ask`s rather than `decide`s** (parent §7.1): a cost estimate is a question, and a
   plan call that wrote a durable decision row would fill the audit with decisions nobody acted on.

   ▲ **The legacy-draft rule is STATE-AWARE, and this plan previously stated it two ways** — refused
   here during planning, returned as a preview in parent §11.1.1. One rule, keyed on state
   (parent §11.1.2):

   | Legacy V1 draft | Answer |
   |---|---|
   | `READY`, unretired | **previewable**, marked `LEGACY_CONFIG_UNPROVEN`; never production |
   | `BLOCKED` / `FAILED` / `CANCELLED` | **no preview** — there is nothing to show |
   | re-authoring under V2 | explicit regeneration **and** spend approval, one draft at a time |

   ▲ **All seven live drafts are `FAILED` or `BLOCKED`** (parent §0.3), so the second row is today's
   only real case — which is exactly why the first must not be written as though it were.
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

### Steps 6, 7, 8 (PARENT) — ▲ BUILD the sandbox lane, the production boundary, gold relocation

Not this plan's work. The previous revision's Phase 5 proposed to build these and is **dissolved**
into parent steps 3, 7 and 8. This plan consumes the result and, in step 5b item 4, renders the
sandbox split honestly.

▲ **P0-1 — and this section previously described step 6 as a re-gating. It is CONSTRUCTION.** The
verdict found that the canonical sandbox execution lane **does not exist**: `POST
/feature-execution/verifications` records a `verification_attempt` and promises a worker; the durable
worker's handler set is `frozenset({"materialization.compile.v1"})` — one handler, and it is compile;
`verification_request` (migration 1094) has no production writer; and `authorize_publication_v2`, the
one module that reads its store, has **zero importers in `src/`** (parent §9.0).

**Two consequences land directly on this document:**

1. ▲ **Step 5b item 4's seven-stage workspace describes two stages nothing performs.** *"Sandbox
   executed"* and *"Sandbox published"* are honest as UI only once parent §9.0's worker exists. Until
   then the workspace must render them as **not yet reachable**, never as pending — a stage that
   waits forever looks like a slow queue and is a missing consumer.
2. ▲ **Step 10's cutover cannot delete the legacy queue handler on schedule.** `run_l1` inside the
   legacy chain is the **only** concrete data execution in the codebase. The deletion's entry
   condition is now *"the canonical lane actually executes"*, which was assumed and is false.

### Step 9 (SHARED) — the two evaluation programmes, and growing the corpus

**Parent** owns both programmes (§12, Phase E). ▲ **Piece #4 is RULED — it is no longer an open
question and it changes what this plan must supply.** Parent §12.1: *the normalized semantic IR must
exactly match the expert-approved IR* **AND** *executing it against reviewed test data must produce
the expected rows and values*; generated source bytes are never compared; failure of either fails
the case.

**So a compiler clean case is now TWO reviewed artifacts, not one**, and both are this plan's supply
problem:

| | |
|---|---|
| **The approved IR** | the normalized `FormulaExecutionIRV2.identity_payload()` a reviewer signed off — structure, not bytes |
| **The reviewed test data** | a frozen dataset pinned by content hash, plus the expected rows and values, plus any per-case tolerance for an explicitly approximate operation (usually none) |

▲ **The dataset is governed data.** It carries read scope and a data-use licence like any other data
(parent §4, §13), so "assemble a certification corpus from production rows" is an exfiltration path
wearing a governance name. Synthetic or approved extracts, reviewed as fixtures.

**This plan owns the supply side**, and it is **not a prerequisite for LLM preview generation**:

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
least 27 approval events plus nine reviewed fixtures**, not nine signatures. ▲ **And after ruling 2,
plus nine reviewed DATASETS with their expected rows** — the fixture alone no longer constitutes a
compiler case.

▲ **MEASURED — and the SHAPE of this supply problem is the opposite of what this plan assumed.**
Live 2026-08-22: **996 approved `recipe_review_event` rows across all 317 recipes**, 6 reviewers,
**zero** single-identity violations, role coverage of 3+ roles on 306 of 317 — and **zero rows
carrying a `formula_expectation_hash`** (parent §21). **The reviewer population is established, the
roles are covered and the multi-person rule is already honoured.** What is missing is the artifact
those reviewers have never been shown. ▲ **The bottleneck is PRODUCING formula expectations and
governed case revisions to review — not recruiting reviewers**, which is what a plan budgeting for
signatures would have staffed for.

▲ **And after the verdict a compiler case is ONE GOVERNED REVISION, not two reviewed artifacts**
(parent §12.2): the approved IR, the frozen dataset manifest, the expected rows, the tolerance
declarations and the runtime/execution profile are hashed together and approved **as one revision**.
Approving the IR and the dataset separately would double the per-case approval count and would permit
the hole it closes — **an approved IR paired with a dataset nobody approved is a certification of
arithmetic against unknown inputs.** Synthetic datasets are the default; approved masked extracts are
the governed exception.

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
15. ▲ **REPLACED — there is no second route to compare with.** The previous revision asked for old-
    versus-new decision equivalence over the six-action matrix. Parent D2 deletes the old route, so
    that test would drive a fixture and report it as coverage. Its replacements (parent §8.3):
    * **route absence** — no OpenAPI path begins `/materialization-runs`, and a live call is 404;
    * **direct-queue bypass** — a legacy-shaped message dead-letters because the handler is gone,
      and a canonical `generation_request` with no `action_authorization_revision_id` refuses at the
      worker instead of building;
    * **request-time vs worker-time agreement** on the canonical route — the same
      `evaluate_action`, two moments, identical decision for unmoved state and a refusal on drift.
16. Direct API permission and stale-snapshot attempts cannot bypass UI policy.
17. ▲ **Derivable-but-unreviewed**: the sealed member says `LLM_AUTHORED`, never
    `REVIEWED_RECIPE_BLUEPRINT`.
18. ▲ **Retirement**: a candidate covered by a tombstone refuses **with no outbox message written**
    — asserted on the queue, not on the HTTP response (parent §11.1.1).
19. ▲ **Legacy identity**: a V2 request against a candidate that has only a V1 draft **spends
    nothing**, returns the legacy draft marked `LEGACY_CONFIG_UNPROVEN`, previews it, and is refused
    at production materialization.
20. **Mutation tests** must fail under each reintroduced defect: gold on preview · origin-as-method ·
    missing provenance treated as pass · selection-triggered LLM call · silent member dropping ·
    **routing on `blueprint_derivable`** · **recomposing `authoring_config_hash` without
    `provider_contract_hash`** · **checking retirement only on identity collision** · **activating
    identity V2 before the tombstones exist** · **restoring `POST /materialization-runs` or leaving
    `enqueue_materialization` in place**.

▲ **New scenarios the verdict requires** — concurrency, tamper and crash recovery, which this list
had none of:

21. ▲ **Retirement race**: a retirement committing between the request's tombstone read and its
    INSERT **refuses the draft**, because both paths take the same lock on the retirement scope key
    (parent §11.1.1). Assert on the queue: no outbox message exists.
22. ▲ **Regeneration exception reachability**: a legacy draft **with** a valid, unexpired, unconsumed
    exception regenerates **exactly once**; a second attempt refuses on `uses_consumed`.
23. ▲ **Retirement scope**: an `EXACT_DRAFT` retirement does **not** refuse a differently-configured
    request for the same candidate; a `CANDIDATE_ACROSS_CONFIGURATIONS` retirement does.
24. ▲ **Cross-selection pin tamper**: a build-set member whose binding names a formula belonging to
    **another selection** is refused **by the database**, not by a worker (parent §11.0.1).
25. ▲ **Decision drift**: evidence moved between request and worker → `DECISION_DRIFT`, and the
    worker does **not** silently re-decide (parent §7.1).
26. ▲ **Forged output id**: `PUBLISH_PRODUCTION` naming a materialized-output id the caller supplied
    publishes nothing; publication resolves its output from the attempt (parent §9.1).
27. ▲ **Crash recovery**: the process dies between the external Spark work and the database commit;
    on restart the lease expires, the work is reclaimed, and **nothing is written twice** (parent
    §9.0 item 8, §9.1).
28. ▲ **Spend exhaustion**: a repair loop that reaches its token or call ceiling **stops** with
    `COST_AUTHORIZATION_EXHAUSTED` rather than truncating and reporting success (parent §11.2).
29. ▲ **Entitlement revocation**: an actor's role is revoked between request and worker →
    `ACTION_AUTHORIZATION_REVOKED`, even though the frozen `read_scope_result` still parses
    (parent §0.1.1).

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
6. ▲ **Delete `POST /materialization-runs`, its status read, its router registration, its producer
   `enqueue_materialization` and its queue handler** — the full surface is parent §1 D2's table —
   once the run sheet (`frontend/src/api.ts:4257`, flag `nav.ts:37,41`) and the three test modules
   (`test_materialization_runs.py`, `test_materialization_e2e.py`, `test_seam_walkthrough.py`) are
   migrated. **No adapter, not even temporarily** (D9, parent D2/§8.3). Then run test 15's
   route-absence and direct-queue-bypass pair against the deployed image, not only in CI.
   ▲ **PRECONDITION, P0-1: parent §9.0's sandbox worker must EXIST and execute first.** `run_l1`
   inside the legacy chain is the only concrete data execution in the codebase — deleting the handler
   before the canonical lane executes removes the platform's only working execution path and replaces
   it with a route that returns 202 and a docstring.
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
  "authoring_identity_version": 2,
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

A **legacy** draft — one authored before the identity correction — differs in the version and in what
it may reach (parent §11.1.1):

```json
{
  "authoring_identity_version": 1,
  "formula_state": "READY",
  "warnings": [{
    "code": "LEGACY_CONFIG_UNPROVEN",
    "reason": "This draft was authored before the platform recorded which model configuration produced it.",
    "next_step": "Inspect it freely; regenerating it under the corrected identity needs approval and costs money."
  }],
  "actions": {
    "generate_preview":       {"allowed": true},
    "materialize_production": {"allowed": false, "blockers": [{"code": "LEGACY_CONFIG_UNPROVEN"}]}
  }
}
```

▲ The API never reports an LLM-authored recipe formula as "reviewed recipe formula", never reports a
**derived** blueprint as a **reviewed** one, and never reports a legacy constant configuration as a
**known** one.

---

## 6. Observability and operating controls

Record, without logging sensitive values:

* selected members by candidate origin and formula method;
* deterministic vs LLM authoring latency;
* provider calls / tokens / cost per job and per member;
* LLM parse, critic, admission and repair outcomes;
* blocker counts by **action** and code — six actions, so the counts are comparable across the gate;
* **percentage of unreviewed recipe candidates reaching preview and sandbox** — ▲ this is the number
  parent §0.2 fact 2 asks for, measured continuously rather than claimed once, and **stamped with
  the funnel version** so two readings are comparable rather than merely different;
* **drafts by authoring identity version** (V1 legacy vs V2), and **`LEGACY_CONFIG_UNPROVEN` members
  reaching preview** — expected non-zero, and expected to fall to zero only through approved
  regeneration, never through a sweep;
* **retirement refusals raised BEFORE an enqueue** vs **after an identity collision** (alert: the
  second must remain **zero** once parent §11.1.1 lands — a non-zero count means the tombstone check
  was skipped and the old coupling is back);
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

### ▲ "Just fix the `getattr` — it's a one-line bug"

**The most plausible-sounding attack in this list**, because the line really is wrong and the fix
really is one line. It is also, today, the code that enforces retirement: correcting the hash
re-mints every identity, every INSERT then wins, and **no retirement row is ever read again**. Two
governance rules ride on one mechanism; the fix is a sequence (tombstones, then V1 recorded, then V2
activated), not an edit. Parent §11.1.1, and it is not this plan's to attempt.

### ▲ "Re-buy the legacy drafts once and be done with it"

Refused by the owner. A one-time re-spend is a bill nobody approved, made on behalf of people who
are not in the room, for answers the platform already holds. Legacy drafts stay as auditable preview
artifacts marked `LEGACY_CONFIG_UNPROVEN`; regeneration is an approved, cost-confirmed act, one
draft at a time.

### ▲ "Certify the compiler by diffing the generated project against a golden file"

Cheap, fast, and it fails on a whitespace change while passing a wrong number. Parent §12.1 rules
the comparison is the **normalized semantic IR** plus **executed values against reviewed test
data** — never generated source bytes. A golden-file test of the rendered project must never become
a certification gate.

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
* selection never spends **and a withdrawn candidate refuses before an outbox message exists**;
* a legacy V1 draft is **previewable, honestly labelled `LEGACY_CONFIG_UNPROVEN`, never re-bought
  without approval, and never certifiable**;
* **(PARENT)** production materialization *and* publication are fail-closed on a current,
  method-matched certificate for **every member**;
* **(PARENT)** ▲ **the legacy route, its producer and its handler are DELETED** — no adapter — and
  route-absence plus direct-queue-bypass tests pass against the deployed image. *(This replaces the
  previous "old and new APIs return the same decision", which named a route that no longer exists.)*
* **(PARENT)** ▲ **the sandbox execution lane EXISTS and executes** — a durable verification worker
  claims its request under a lease, runs `run_l1` outside the transaction, records an output revision
  and stops before publication (parent §9.0). **Nothing in this plan's journey is real without it.**
* ▲ **the client never sends a formula method** — "Try AI formula" creates a server-authored
  `formula_method_override_revision` whose named refusal the server verifies (parent §11.3);
* ▲ **every provider call and repair turn is covered by a durable spend authorization**, and an
  exhausted one stops the job rather than truncating it (parent §11.2);
* ▲ **a build-set member's formula is bound RELATIONALLY to its selection** — a formula belonging to
  another selection cannot be pinned, and the database is what refuses it (parent §11.0.1);
* ▲ **the strategy contract and its resolver ship in step 2**, so parent identity V2 composes from
  persisted facts rather than from facts a later step would have created (P0-2).

▲ **What "done" does NOT mean.** Production stays closed for every recipe until the reviewed corpus
grows past its single clean case — at least 27 approval events, nine reviewed fixtures **and, after
ruling 2, nine reviewed datasets with expected rows** (step 9, parent §21) — and until §12's eight
compiler-certification pieces exist. ▲ **Piece #4 is no longer an undelivered ruling** (parent
§12.1); all eight are now engineering, and comparison B additionally needs a deployment whose
execution seam is configured, or it records `UNMEASURED` and certifies nothing. That is the intended
invariant, not a defect, and it is why preview must never be gated on the thing that cannot yet
happen.

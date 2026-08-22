# Five gated actions, one decision service, and a real production boundary

**Authority:** the product owner's rulings of 2026-08-22, quoted inline, as amended by their review
of this plan's first revision. Where this plan and a ruling disagree, the ruling wins. The three
decisions this document used to leave open (D1/D2/D3) are **resolved below** — they are no longer
inputs to be gathered, they are constraints to be built.

> *"If the backend can safely and honestly render the selected formula, show the user the code. Gold
> evaluation decides whether the generation method is certified for production — not whether code can
> be inspected."*

> *"A missing reviewed expectation changes the AUTHORING METHOD. It must not automatically prevent
> code preview."*

---

## 0. Where we actually are

**Done and green** (13 commits, tree clean at `364cd7fa`):

| | |
|---|---|
| Certification machinery | evaluation contract (migration 1097), 12-case corpus, V2/V3 evaluation lane (1098), current-evaluation validity reader |
| Method matching | per-member provenance table (1099) **and its sealing writer** — `derive_member_provenance` / `record_member_provenance`, called from `seal_v2` and reached from the real lane at `generation_lane.py:543` |
| Blockers removed | `grain_refs` reaches authoring; `build_set_revision.declaration_json` typed |
| Live schema | 1097 + 1098 applied to kind; **1099 is files only** |

▲ **Phase A of the previous revision is DONE.** The owner's review was written against `d4072f08`
and correctly said `derive_authoring_method` had no caller and `sealed_artifact_member_provenance`
had no writer. Both were fixed one commit later at `364cd7fa`. The *standing rule* the review draws
from those examples (§10) stands regardless — the examples are now history rather than open defects,
and one of the three (`evaluate_publish_production` with no endpoint) is still entirely true because
that function does not exist at all.

▲ **New deploy-ordering hazard created by that fix.** `record_member_provenance`
(`src/featuregen/materialize/authoring_provenance.py:236`) issues an unguarded
`INSERT INTO sealed_artifact_member_provenance`. Live schema is 1097+1098. **Shipping the current
image to kind without applying 1099 first makes every seal fail.** Migration 1099 is now
backend-blocking, not optional.

**Not started:** all five gated actions, the single decision service, gold's relocation, the
production publication boundary (endpoint, attempt record, namespace), the deterministic
reviewed-blueprint lane, the operator UI journey, and the end-to-end journey tests.

### Four facts that shape the sequence

1. **The readiness ladder has exactly ONE enforcing comparison** in production
   (`activation_policy.py:176`, `effective_readiness != MATERIALIZATION_READY`), gating
   `execute_materialization`. Everything else that reads readiness displays, sorts or reports it.
   **Removing gold from the ladder is measured to change ZERO served states** — only the three
   `gold_evaluation_unproven` blockers disappear.
2. ▲ **…and removing gold on its own helps almost nobody.** `recipe_readiness.py:83` sends any
   recipe without a reviewed expectation straight to `FORMULA_BLOCKED`, which is **295 of 317**
   recipes. *(Measured against the real registry, not inherited: `V2_RECIPES` is 317 — 298
   `deterministic_formula`, 11 `conceptual_pattern`, 8 `governed_model_output` — and exactly 3
   deterministic recipes satisfy `has_reviewed_expectation`: `merchant_mcc_diversity`,
   `obligor_facility_count`, `posted_debit_amount`. 298 − 3 = 295. The other 19 are legitimately not
   preview-able, so **295 is the addressable set**, not 314.)*
   The 3 that reach `FORMULA_AUTHORABLE` become `FORMULA_VALIDATED` when gold is removed
   (`recipe_readiness.py:89`) — and `FORMULA_VALIDATED != MATERIALIZATION_READY`, so the equality
   check at `activation_policy.py:176` **keeps blocking them too**. Separately,
   `activation_policy.py:182` appends `FORMULA_NOT_REVIEWED` on its own `if`, independent of the
   readiness comparison, so even a hypothetical `MATERIALIZATION_READY` recipe still collects that
   blocker. **The ladder change alone moves nothing.** This is why §4's new matrix row is a P0.
3. **Candidate origin is NOT authoring method.** `formula_draft_worker` passes no
   `reviewed_blueprint` — the parameter exists only on `replay_authoring_v2.py:486` and no
   production caller supplies it — so it ALWAYS drives the LLM author and critic. Every formula today
   is `LLM_AUTHORED` whatever the recommendation's origin, and `formula_drafts.py:269` hard-codes
   `"formula_source": "llm_authored"` to match.
4. ▲ **Therefore "mixed-method" is not testable today.** A build combining a recipe-origin
   recommendation and an LLM-origin recommendation is mixed-**origin**; both members are
   LLM-authored. Phase E's mixed-method acceptance test is **impossible without D1**. That is the
   argument that decides D1, not a preference.

---

## 1. The three decisions, resolved

* **D1 — the deterministic reviewed-blueprint lane is IN SCOPE**, with LLM fallback. When a current
  reviewed executable blueprint exists, instantiate the formula deterministically with **zero
  provider calls**. When none exists, **explicitly** ask the LLM, then validate the result
  deterministically, and allow preview/sandbox if it is valid. Deferring D1 would leave the
  recipe-compiler certification programme certifying a path nothing uses, and would make the
  mixed-method journey test unwritable (fact 4). The task-level design lives in the sibling plan
  `2026-08-22-recipe-to-code-llm-fallback.md`; this plan owns the gating contract it must satisfy.
* **D2 — the old materialization route becomes a THIN ADAPTER, then is deleted.** It is not deleted
  in the same change. It is deleted once route-equivalence tests pass *and* there are zero direct
  callers. See §7 for the condition that makes the adapter honest rather than decorative.
* **D3 — sandbox IS allowed while gold is pending, and the warning is part of the contract.** It
  MUST be returned by the API and **DISPLAYED in the UI**. A warning that is computed and dropped is
  worse than no warning: it teaches the platform to believe it warned. Today the nearest surface,
  `SuggestionCard.tsx:210`, renders `gold_evaluation_unproven` as a *blocker* line — that copy has to
  become warning-shaped, not disappear.

---

## 2. Terminology — the conflation this plan must not reproduce

The word "materialization" currently covers six different things. Each of the following is a
separate act with a separate gate, and the rest of this document uses only these names:

| Term | What it means |
|---|---|
| **Formula authoring** | deciding *what to calculate* — may legitimately show an incomplete formula with unresolved policy requirements |
| **Preview generation** | producing the Kedro/PySpark project — requires a safe compile and render |
| **Sandbox execution** | running the generated code against test data — touches a cluster |
| **Sandbox publication** | making test results available to look at |
| **Production materialization** | running the certified calculation and storing production values |
| **Production publication** | making those values available downstream |

▲ *"The older materialization path"* is the **historical name of a pipeline that conflated several
of these**, not a synonym for any one of them. When this plan says a gate belongs to sandbox
publication, that is a statement about which of the six acts it guards.

---

## 3. FIVE actions, not four

```
AUTHOR_FORMULA · GENERATE_PREVIEW · EXECUTE_SANDBOX · PUBLISH_SANDBOX · PUBLISH_PRODUCTION
```

They are not a stylistic subdivision. They differ in what they risk:

| Action | Why it is its own gate |
|---|---|
| `AUTHOR_FORMULA` | may show an incomplete formula with unresolved policy requirements — visibility is not execution |
| `GENERATE_PREVIEW` | requires a **safe compile and render**; nothing runs |
| `EXECUTE_SANDBOX` | **touches a cluster** — consumes compute, reads data |
| `PUBLISH_SANDBOX` | makes **test output visible** to other people |
| `PUBLISH_PRODUCTION` | needs **method certification** and stronger governance |

### What exists today, and what has to be added

`EvaluatorAction` (`src/featuregen/overlay/upload/evaluator_contracts.py:43`) has exactly three
members — `GENERATE`, `VERIFY`, `PUBLISH_SANDBOX` — and its docstring says naming three is
deliberate because they are *"the three an execution chain gates"*. **There is no
`PUBLISH_PRODUCTION` anywhere in `src/`.**

| Action (service vocabulary) | Closest existing evaluator | Status |
|---|---|---|
| `AUTHOR_FORMULA` | none | new; not a chain gate |
| `GENERATE_PREVIEW` | `EvaluatorAction.GENERATE` | exists, misgated (§4, §5) |
| `EXECUTE_SANDBOX` | `EvaluatorAction.VERIFY` | exists; verify is the artifact/permission/environment half — confirm it is the whole sandbox-execution question or widen it |
| `PUBLISH_SANDBOX` | `EvaluatorAction.PUBLISH_SANDBOX` | exists; **leave unchanged** |
| `PUBLISH_PRODUCTION` | — | **does not exist** (§8) |

▲ Adding `PUBLISH_PRODUCTION` to `EvaluatorAction` **requires editing that enum's docstring claim**,
because production publication IS a chain gate and the "deliberately three" sentence becomes false.
`AUTHOR_FORMULA` and `EXECUTE_SANDBOX` do not join the enum on that reasoning; the five-name
vocabulary is the *service's* `Action`, and `EvaluatorAction` is the subset the chain enforces.
Do not silently widen one into the other.

---

## 4. The blocker matrix — the contract §6 implements

▲ **The row the previous revision was missing is the whole product.** Without it this plan helps
**3 recipes out of 317**.

| Condition | Formula | Preview | Sandbox | Production |
|---|---|---|---|---|
| **No reviewed expectation, valid LLM-authored formula exists** | **Allow** | **Allow after deterministic validation** | **Allow + warning** | **Require LLM-method certification + feature governance** |
| **Reviewed executable blueprint exists** | **Deterministic formula** | Allow | Allow | **Require deterministic-method certification** |
| Gold evaluation pending | Allow | Allow + warning | Allow + warning | **BLOCK** |
| Missing customer relationship / grain | Block | Block | Block | Block |
| Target leakage | formula visible | Block | Block | Block |
| Unsupported renderer operation | formula visible | Block | Block | Block |
| Missing currency / reversal policy | formula visible | Block | Block | Block |
| User lacks read permission | formula visible | Block | Block | Block |
| Artifact not verified | Allow | Allow | Depends | Block |

**The rule, stated so it can be quoted back at a future change:**

> *A missing reviewed expectation changes the AUTHORING METHOD. It must not automatically prevent
> code preview.*

Which resolves to two routes and no third:

* **Reviewed blueprint exists** → instantiate the formula **deterministically. NO LLM calls.**
* **No blueprint** → **explicitly** ask the LLM, **validate deterministically**, and allow preview
  and sandbox if the result is valid.

There is no automatic silent fallback from a failed deterministic instantiation to the LLM: the
method is chosen from evidence before authoring, and a deterministic failure is a deterministic
refusal.

---

## 5. `GENERATE_PREVIEW` never consults readiness

> ▲ *"`GENERATE_PREVIEW` never consults `effective_readiness == MATERIALIZATION_READY`. It evaluates
> the formula, bindings, leakage, policies, permissions and renderer directly."*

Recipe readiness may remain a **discovery/maturity projection** — a way to sort and explain a
registry — but it must **STOP AUTHORIZING executable actions**. Every fact preview genuinely needs
is available without it: the formula and its bindings, the leakage check, the currency/reversal/
status policies, the caller's read authorization, and the renderer's capability for each operation.
The ladder adds nothing to that list; it only adds `FORMULA_NOT_REVIEWED` and
`gold_evaluation_unproven`, neither of which is a reason the code cannot be rendered.

▲ **Sequencing dependency:** **Phase C only works if Phase B has already removed the ladder from
preview authorization.** Relocating gold while `MATERIALIZATION_READY` equality still authorizes
preview would leave the same 295 recipes blocked by a different sentence, and the journey test in
§11 would pass its production assertion while failing its preview assertion for an unrelated reason.

---

## 6. The shared decision service — server-owned inputs

The previous revision proposed `evaluate_action(selected_features, action)`. That signature lets the
client hand the server the answer. Replace it with:

```python
evaluate_selection_action(
    conn, *, selection_revision_ids: Sequence[str], action: Action,
    actor: IdentityEnvelope, artifact_id: str | None = None,
) -> BuildActionDecisionV1
```

`IdentityEnvelope` already exists (`featuregen.contracts.identity`). `Action` and
`BuildActionDecisionV1` are new.

**It loads its own evidence, server-side, from immutable identities:**

```
considered_revision_id + option_id + decision_id
   → frozen semantic_option_decision
   → current activation state
   → formula draft / authoring run
   → artifact + per-member provenance (1099)
```

**It returns:**

* one decision **PER MEMBER**
* a group-level verdict, **all-must-pass**
* blockers
* warnings
* the policy version that produced them
* an evidence hash
* current-state revision pins

▲ **The client must not supply readiness, blocker codes, formula method, or certificate identity.**
Anything a caller can pass is something a caller can forge. The only client inputs are *which
selections* and *which action*.

**Route BOTH the old and new paths through it.** The ruling: *"Both old and new APIs must receive the
same answer"* and *"do not keep two independent readiness implementations."*

---

## 7. Closing the bypasses

### 7.1 The empty default is a silent bypass

```python
activation_blockers: Sequence[str] = ()
```

at `src/featuregen/materialize/generate_v2.py:91` and
`src/featuregen/materialize/evaluate_execution.py:50`. Supplying nothing means "no blockers", which
means generation proceeds. ▲ **This is not hypothetical: the real lane never passes it.**
`generation_lane._drive` calls `generate_v2` at `generation_lane.py:531-545` with twelve keyword
arguments and `activation_blockers` is not among them — so the production generation path runs today
with the empty default.

Remove the default. Require instead:

```python
action_decisions_by_member: Mapping[str, ActionDecisionV1]   # NO default
```

with:

* **EXACT coverage** of every compiled member — no extra keys, no missing keys;
* **one decision per member**;
* expected action `GENERATE_PREVIEW` on each (a decision for a different action is a refusal, not a
  near-miss);
* **any refused member prevents rendering** — all-must-pass, not best-effort;
* unknown or missing decisions **FAIL CLOSED**.

### 7.2 Check the decision TWICE

* **At request time** — so a person gets an immediate, actionable answer.
* **In the generation worker, immediately before rendering** — which prevents a direct queue or API
  bypass, and catches current-state drift between the request and the render.

Two checks are not redundancy; they answer at two different moments about a state that can move.

### 7.3 The old adapter must resolve or refuse

`materialization_runs.py:486` documents a keyless bypass in its own words: *"No option key is not a
refusal."* A work-item-driven request with no `considered_revision_id` and no `option_id` returns
`{}` and proceeds.

The adapter must therefore do **one of two things** for every request:

* resolve **every** member to a real selection revision and call `evaluate_selection_action`; or
* **REJECT the legacy keyless request with a typed deprecation error**.

▲ Without this, *"old and new APIs agree"* is **not a testable claim** — the old path can reach
generation without ever having asked the question the new path exists to ask. Equivalence tests over
a route that can skip the evaluation prove only that both routes ran.

---

## 8. A real production boundary — P0

The previous revision described `evaluate_publish_production` with **no authoritative caller**. An
evaluator nothing calls is a description of a gate, not a gate. Add, explicitly:

```
POST /feature-execution/production-publications
    → evaluate_publish_production
    → record the EXACT certificate revision on the attempt
    → enqueue production publication
    → worker re-checks attempt-bound evidence
    → publish or refuse
```

(The `/feature-execution` prefix already hosts `/generations`, `/verifications` and `/publications`
in `src/featuregen/api/routes/feature_execution.py`; `/publications` is **sandbox** publication and
stays as it is.)

Plus, all three, in the same change:

1. a **`PUBLISH_PRODUCTION` evaluator action** (§3);
2. a **production attempt record** — carrying the exact certificate revision, so the answer is
   re-checkable and cannot drift between decision and publication;
3. a **production namespace / publisher path** distinct from `sandbox_feature`.

`evaluate_publish_production` requires: current artifact verification · production publish permission
· production capability attestation · data-use and read authorization · a **method-matched** current
certificate · **the exact certificate revision recorded on the attempt** · all artifact members
passing.

It also needs a **method-level** certificate reader. `current_evaluation_validity`
(`overlay/upload/current_evaluation_validity.py:76`) is expectation-specific and cannot answer for a
novel LLM feature that has no recipe expectation. The question it must answer is *"was this exact
LLM authoring configuration certified by the platform-wide gold corpus?"*

▲ **Until the endpoint, the attempt record and the namespace exist, gold has not been RELOCATED —
it has only been REMOVED from one place and DESCRIBED in another.** Phase C is not complete when the
evaluator function merges.

▲ **The gate does NOT go on the current publish path.** That path is `PUBLISH_SANDBOX` on the
`sandbox_feature` namespace; putting certification there blocks sandbox testing. `chain.py:646`'s
pattern is publication-*mechanism capability*, not certification. **Leave verification and
`PUBLISH_SANDBOX` unchanged.**

**Hard-block from day one. No transitional "certification required only once a certificate
exists" rule** — absence would act as permission, and earning the first certificate would make the
platform stricter than before.

---

## 9. Certification is not feature approval

A current platform certificate means the **METHOD** is trusted enough to be **CONSIDERED** for
production. It is a precondition, not an approval.

A production feature must **still** pass, every time:

* formula validation
* target-leakage checks
* grain and join checks
* currency / reversal / status policies
* data-use and read authorization
* engine compatibility
* artifact verification
* sandbox checks
* publication permission and capability
* current, **method-matched** certification

Conflating the two produces both failure modes: a certified method waved through a leaking feature,
and a perfectly governed feature refused because the platform's evaluation job has not run this week.

---

## 10. Standing rule — a repeated failure in this codebase

> ▲ *"Never land a governance function without its enforcement point in the same change."*

The live examples that produced this rule:

| Example | Status |
|---|---|
| `derive_authoring_method` had no caller | **fixed at `364cd7fa`** — one commit after it landed |
| `sealed_artifact_member_provenance` (1099) had no writer | **fixed at `364cd7fa`** |
| `evaluate_publish_production` proposed with no endpoint | **still true — the function does not exist yet, and §8 is what stops it repeating** |

Two of three were caught within a commit. That is the *good* case, and it still cost a plan revision
and a review cycle. The rule is: a merged governance function whose enforcement point is "next
phase" is indistinguishable, from the outside, from a governance function that does nothing.

---

## 11. The two journeys are separate

They share a vocabulary and nothing else. Documenting them as one is how gold ended up on the
readiness ladder.

**The NORMAL HYPOTHESIS JOURNEY** (an analyst):

```
submit hypothesis → recommendations → select → formulas → preview code → sandbox → production
```

**The PLATFORM EVALUATION JOURNEY** (an admin/SME):

```
open Governance → Formula quality → run the reviewed corpus
    → evaluate the DEPLOYED model / prompts / contract
    → produce a certificate: current | stale | failed
    → consumed ONLY by production publication
```

▲ **The hypothesis user neither triggers nor waits for gold evaluation.** Nothing in the first
journey may block on the second before its final step.

---

## 12. The phases

Ordered by dependency, not size. **Phase B is the spine**; the rest attach to it, and doing it late
means writing each gate twice.

### Phase A — the sealing writer ✅ DONE at `364cd7fa`
Per-member provenance is derived and written inside `seal_v2`, from the one place where
`RestoredFormulaV3` (which carries `selection_revision_id` and `formula_draft_id`) and
`AdmittedFeatureV2` (which carries `feature_name` and `proposal_content_hash`) meet. An undecidable
method refuses the seal by name — `MemberProvenanceRefused` → `AUTHORING_RUN_INCOMPLETE`, terminal,
not retried. **Remaining: apply migration 1099 to kind BEFORE deploying this image** (§0).

### Phase B — the five actions and ONE decision service ▲ THE SPINE
* Split the single `MATERIALIZATION_READY` authorization into the **five** action decisions of §3.
* Build `evaluate_selection_action` exactly as §6 specifies — server-loaded evidence, per-member
  decisions, all-must-pass, warnings, policy version, evidence hash, revision pins.
* **Remove the ladder from preview authorization** (§5). This is the half of Phase B that Phase C
  depends on.
* Route **both** the old adapter and the new build-set path through it (§7.3).

**Proof:** a table-driven test over §4's matrix — **including both new top rows** — asserted through
BOTH routes.

### Phase C — `PUBLISH_PRODUCTION` + gold relocation ▲ ATOMIC, ONE COMMIT
> *"Do not merge 'gold removed from readiness' without simultaneously introducing the production
> publication gate; that would temporarily remove the protection instead of relocating it."*

* Remove gold from `recipe_readiness` — **keep `BLOCKER_GOLD_UNPROVEN` in the fold-owned set** so
  legacy rows strip it rather than re-entering it as a governed policy blocker (which would pin every
  legacy candidate at `FORMULA_BLOCKED`, strictly worse).
* Ship **everything in §8 in the same commit**: the evaluator action, the endpoint, the attempt
  record, the production namespace, the method-level certificate reader, the worker re-check.
* Fold all artifact members **all-must-pass**.

### Phase D — the deterministic reviewed-blueprint lane (D1)
Resolve the method **server-side, from evidence, before authoring**: reviewed current executable
blueprint → `deterministic_producer` with **zero provider calls**; otherwise → explicit LLM
authoring with the cost shown first, then deterministic validation. Stop `formula_drafts.py:269`
hard-coding `llm_authored` and read the evidence instead. Detailed tasks live in
`2026-08-22-recipe-to-code-llm-fallback.md`.

▲ Known wrinkle from that plan's measurement: `has_reviewed_expectation()` unions two registries, and
**two of the three reviewed recipes are Formula V1** — "reviewed expectation exists" therefore does
not by itself select the deterministic V3 producer. The resolver must return the expectation
generation too.

### Phase E — the operator journey (Governance → Formula quality)
UI page → `POST /formula-evaluations` → queue runner over all 12 frozen cases → progress endpoint →
results report. Cost-confirmed before starting (calls, token budget, max cost); **the backend reads
the deployed configuration — the operator never types prompt hashes.** Outcomes:
`CERTIFIED_CURRENT · PASSED_NOT_CERTIFIABLE · FAILED_QUALITY · FAILED_TECHNICAL · STALE`.
Two programmes now that D1 is in scope: **LLM authoring** and **recipe compiler**.

**The V2 corpus runner is the critical missing piece:** the backend can describe and score an
evaluation, but nothing walks the 12 cases as a user-triggered job.

### Phase F — the journey tests ▲ BEFORE removing the old path
Both methods, end to end: hypothesis → recommendation → selection → formula → preview project →
inspect code → sandbox verification → attempt production. §13 is the decisive one.

Must also prove: gold pending does **not** block preview · gold pending **does** block production ·
target leakage blocks preview · missing currency/reversal blocks preview · a reviewed-blueprint
member and an LLM-authored member build together in one artifact (**mixed-METHOD**, which Phase D is
what makes possible) · stale certificate refused · missing provenance refused · **every API gives the
same decision**.

### Phase G — cutover
Make the build-set workflow canonical; **delete** the old materialization route once route-
equivalence passes and it has zero direct callers (D2); fix the known build-set authorization gaps
(authenticated roles, authoritative metadata, policy binding, queue/build-set integrity).

---

## 13. The decisive journey test

Given a valid **"90-day incoming amount minus prior 90-day"** formula with complete grain and
bindings, resolved policies, a supported renderer, and **NO current gold certificate**:

```
Old API preview decision = allowed + GOLD_PENDING warning
New API preview decision = allowed + IDENTICAL warning
Generated artifact       = exists
Code view                = available
Sandbox decision         = allowed + warning
Production decision      = blocked by METHOD_CERTIFICATE_MISSING
```

▲ **And it must FAIL when each defect is reintroduced.** A test that only passes is a test that
proves nothing here. Reintroduce these one at a time and confirm the failure:

| Defect reintroduced | Which assertion must break |
|---|---|
| restore `MATERIALIZATION_READY` equality on preview | preview allowed |
| restore the empty `activation_blockers=()` default | worker refusal on a refused member |
| skip the new worker's evaluation | second-check drift detection |
| skip the old adapter's evaluation | "identical warning" through both routes |
| put gold on sandbox | sandbox allowed + warning |
| remove the production certificate check | `METHOD_CERTIFICATE_MISSING` |

Verify the injection actually applied, and read pytest's summary line — `grep -c "^FAILED"` silently
matches nothing against coloured output and reports a false pass.

---

## 14. Risks

* **Phase C's atomicity is the one that bites.** Landing the ladder change alone removes protection
  rather than relocating it. This has already been attempted once and reverted. §8's three artefacts
  (endpoint, attempt record, namespace) are part of the atom, not follow-ups.
* **Phase C also depends on Phase B's preview half** (§5). Getting the order wrong produces a green
  production assertion and a red preview assertion for an unrelated reason, which reads as a product
  question and is not one.
* **Placement.** A gate put on the sandbox path blocks sandbox testing. When a change makes many
  tests on ONE path fail, treat that as a placement signal before treating it as a product question.
* **Weak tests.** Tests that construct their own fixtures and assert them back prove nothing here.
  Drive the real path; §13's reintroduction table is the check that the tests have teeth.
* **Migration 1099 is now backend-blocking** (§0). Image before migration = every seal fails.
* **Nothing is exercisable live** until a deploy: the cluster image predates migration 1094.

## 15. Not engineering, and not blocking these phases

Nine expert sign-offs to grow the reviewed corpus past its single clean case (approved
`recipe_review_event` rows from every required role). Until then no evaluation is *certifiable*, so
production stays blocked — which is the intended invariant, not a defect.

▲ Note the shape this gives the product once §4's new rows are built: with gold uncertifiable and
295 recipes lacking a reviewed expectation, **the LLM route with deterministic validation is the
only route to preview for almost every recipe**, and production is closed for all of them. That is
the honest state, and it is exactly why preview must not be gated on the thing that cannot yet
happen.

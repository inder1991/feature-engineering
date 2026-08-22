# Recipe-to-code: reviewed formulas, LLM fallback, and one generation journey

**Date:** 2026-08-22  
**Status:** implementation plan only; this document changes no runtime behaviour  
**Branch validated:** `feature/asset-detail-reapply` at `d4072f08`, while preserving the existing
uncommitted provenance/sealing work  
**Parent plans:**

- `2026-08-11-semantic-eligibility-feature-generation-workflow.md`
- `2026-08-13-semantic-activation-and-one-engine-remediation.md`
- `2026-08-22-four-stage-gating-and-production-certification.md`

This plan resolves the parent plan's open formula-path decision:

> Use a reviewed recipe blueprint without an LLM when one exists. Otherwise, for a semantically
> eligible deterministic recipe, explicitly use the LLM to author a candidate formula. A missing
> reviewed expectation is not a reason to hide formula/code generation; it changes the authoring
> method and the evidence required for production publication.

It also resolves the remaining UI/API decisions:

- keep the old materialization API temporarily as a thin adapter, then delete it after the UI and
  journey tests use the canonical build-set lane;
- allow code preview and sandbox work while gold certification is pending, with a visible warning;
- block production publication until the certificate matches how every artifact member was
  actually authored.

---

## 0. Outcome

After this plan, a user can submit a hypothesis, select recipe and LLM-origin recommendations, ask
for formulas and code, inspect the result, and run it in a sandbox without encountering a dead end
merely because a recipe lacks a reviewed formula expectation.

The target journey is:

```text
Hypothesis
   |
   v
Semantic eligibility and recommendations
   |
   v
Human selects features (selection spends nothing)
   |
   v
Resolve formula method per selected feature
   |-----------------------------------------|
   | reviewed, current V2 blueprint exists   | no reviewed executable blueprint
   v                                         v
Deterministic instantiation                  Explicit LLM authoring
(zero provider calls)                        (cost shown before request)
   |                                         |
   +--------------------+--------------------+
                        v
        Parse -> semantic validation -> admission
                        |
                        v
            Compile -> render -> seal artifact
                        |
                        v
              Show formula and code
                        |
                        v
              Verify/run in sandbox
                        |
                        v
          Production-publication decision
             (method-matched certificate)
```

The user-facing promise is deliberately narrower than “all 317 recipes always generate code.” A
formula can only be authored when the selected candidate has a declared grain, bound operands and a
frozen catalog snapshot. The formula may expose a still-unresolved policy requirement, but preview
compilation and execution remain blocked until point-in-time, currency, reversal, status and other
governed policies are resolved. Missing data or ambiguous banking semantics remain honest blockers.
Missing review alone does not.

---

## 1. Verified current state

The plan is grounded in the current code rather than the readiness labels alone.

### 1.1 Registry measurement

Running the real registry and fold gives:

| Measurement | Count |
|---|---:|
| Recipes | 317 |
| Deterministic formula recipes | 298 |
| Conceptual patterns | 11 |
| Governed model outputs | 8 |
| `FORMULA_BLOCKED` | 295 |
| `FORMULA_AUTHORABLE` | 3 |
| `CONCEPTUAL_ONLY` | 19 |
| Reviewed expectation membership | 3 |
| Structurally derivable V2 blueprints | 90 |

The 227 non-derivable definitions break down as:

| Derivation result | Count |
|---|---:|
| Not a deterministic formula | 19 |
| Window not event-anchored | 102 |
| Multiple operands/body unresolved | 65 |
| Aggregation undeclared | 19 |
| Output policy underivable | 6 |
| Window unit unsupported | 6 |
| No measure operand | 4 |
| Temporal definition blocked | 3 |
| Parameter projection underivable | 2 |
| Grain key unresolved | 1 |

Those refusals are recipe-library improvement work. They are not all reasons to prevent an LLM
from proposing a candidate-specific formula when the actual selected columns provide the missing
facts.

### 1.2 The three “reviewed” recipes are not one homogeneous capability

`has_reviewed_expectation()` unions two different registries:

- `merchant_mcc_diversity` and `obligor_facility_count` are Formula V1 blueprints;
- `posted_debit_amount` is the single Formula V2 reviewed fixture.

Therefore “reviewed expectation exists” cannot by itself select the new deterministic V3 producer.
The resolver must return the expectation generation and whether it can produce a V3 formula. The
two V1 entries must be migrated to V2/V3 or use the LLM path; this plan never resurrects V1
materialization.

### 1.3 Useful machinery already exists

- `formula_draft_worker.py` authors and critiques a candidate with the LLM, parses Formula V3,
  validates it and admits it.
- `deterministic_producer.py` can turn a bound reviewed V2 blueprint into a Formula V3 proposal
  without a provider call.
- `run_authoring_v2_replay(..., reviewed_blueprint=...)` already records the honest
  `REVIEW_BYPASSED` path.
- `restore_formula_v3.py`, `generation_lane.py`, `generate_v2.py` and `seal_v2.py` form the new
  build-set compile/render/seal path.
- migration 1099 and `authoring_provenance.py` provide per-artifact-member method provenance; the
  current worktree is finishing its writer.
- `FormulaDraftAction.tsx` already guarantees that selecting a checkbox does not call the LLM and
  makes the paid request explicit.

### 1.4 The real gaps

1. `recipe_readiness.py` mixes recipe specification maturity, formula availability, engine support
   and production certification into one ladder.
2. `activation_policy.py` still requires exact `MATERIALIZATION_READY` for both request and execute,
   so the old governed route blocks all recipes.
3. `formula_draft_worker.py` always uses the LLM and never supplies `reviewed_blueprint`.
4. `formula_drafts.py` hard-codes `formula_source = "llm_authored"` instead of reading evidence.
5. `AuthoringWorkItemV1` treats candidate origin `recipe` as proof that a reviewed blueprint exists.
   That makes “recipe recommendation, LLM-authored formula” unrepresentable even though it is the
   required fallback.
6. No production coordinator takes selected options through selection revision -> formula -> build
   set -> authorization -> generation. The frontend has per-row formula drafting and a downstream
   artifact screen, but no complete bridge between them.
7. The old and new routes do not ask one shared action decision, so the same feature can be refused
   by one API and built through another.
8. Gold certification is attached to readiness instead of production publication.
9. The static, source-code-pinned reviewed registry cannot scale operationally to hundreds of
   recipes without a review/promotion workflow.

---

## 2. Product and architecture decisions

These decisions are part of the implementation contract, not questions for individual tasks to
re-litigate.

### D1 — Candidate origin and formula method are separate axes

Candidate origin answers where the idea came from:

```text
recipe | llm_intent | user_defined
```

Formula method answers how the exact formula was produced:

```text
REVIEWED_RECIPE_BLUEPRINT | LLM_AUTHORED
```

A recipe-origin feature can use either method. An LLM-origin feature cannot claim a reviewed
recipe blueprint merely because it resembles a recipe.

### D2 — Strategy selection is deterministic and server-owned

For one selected option:

```text
conceptual_pattern       -> NON_FORMULA
governed_model_output    -> MODEL_WORKFLOW
deterministic_formula:
    no current executable V2 blueprint              -> LLM_AUTHORED
    current executable V2 blueprint exists + binds  -> REVIEWED_RECIPE_BLUEPRINT
    current executable V2 blueprint exists + fails  -> BLOCKED (fix blueprint/binding)
```

The client never chooses a more favourable method label. It may explicitly request an LLM retry
after a deterministic blueprint defect, but that is a new draft identity and a new user action;
the backend never silently changes method after failure.

### D3 — Missing reviewed expectation is a route selector, not a code blocker

`no_reviewed_formula_expectation` remains useful recipe-maturity information, but it does not block
formula drafting, code preview or sandbox execution. It causes the formula strategy to be
`LLM_AUTHORED` and produces the UI warning “AI formula required.”

### D4 — Four actions, four decisions

One server service evaluates:

```text
AUTHOR_FORMULA
GENERATE_PREVIEW
EXECUTE_SANDBOX
PUBLISH_PRODUCTION
```

It returns `{allowed, blockers, warnings, policy_version}`. No route and no React component
re-implements the matrix.

| Condition | Formula | Preview code | Sandbox | Production |
|---|---|---|---|---|
| No reviewed expectation | LLM route | Allow after validation | Allow + warning | Method certificate required |
| Gold evaluation pending | Allow | Allow + warning | Allow + warning | **Block** |
| Recipe business review stale | Allow + warning | Allow + warning | Allow + warning | **Block** |
| Missing/ambiguous grain or operand | **Block** | Block | Block | Block |
| No frozen catalog snapshot | **Block** | Block | Block | Block |
| Target leakage | Formula visible | **Block** | Block | Block |
| Unsupported renderer operation | Formula visible | **Block** | Block | Block |
| Currency/reversal/status policy unresolved | Formula visible | **Block** | Block | Block |
| User lacks read/use authority | Formula visible only if already authored | **Block** | Block | Block |
| Artifact not verified | Allow | Allow | Depends on execution policy | **Block** |
| Conceptual pattern | Save/specify only | Block | Block | Block |
| Governed model output | Model workflow | Block deterministic code | Model workflow | Model certification |

“Formula visible” means the platform may show a proposal and the unresolved assumption; it does not
mean it may compile or execute it.

### D5 — Selection never spends

Checkboxes only update client state. The first paid action is an explicit “Prepare formulas and
code” confirmation that states how many selected features require LLM calls. Polling, reload and
double-click never create new provider work.

### D6 — One backend coordinator owns the journey

The React client must not chain five write APIs and hope the browser stays open. A durable
`code_generation_job` owns selections, formula preparation, build-set creation, generation and the
terminal outcome. Existing formula and generation queues remain the workers; the coordinator joins
their durable states and advances only from recorded evidence.

### D7 — Mixed-method artifacts are first-class

One build set may contain a reviewed-blueprint member and an LLM-authored member. Method provenance
therefore remains per artifact member. Production uses all-must-pass certification over those rows.

### D8 — Gold certifies a method, not every new feature

The gold programme tests whether the exact LLM author/critic configuration or deterministic
compiler version is reliable on a reviewed corpus. A novel feature still undergoes candidate-level
semantic validation and sandbox checks. Gold does not require a banking expert to pre-write every
user feature.

### D9 — No new rollout flags

The product is pre-live. Implement the canonical path, test it, deploy it to the branch-owned Kind
cluster, and remove the legacy decision path after its adapter has zero direct callers. Rollback is
the previous image. Do not create dark gates that allow the two behaviours to drift.

---

## 3. Canonical contracts

### 3.1 Formula strategy

Create `src/featuregen/overlay/upload/formula_strategy.py`:

```python
class FormulaStrategy(StrEnum):
    REVIEWED_RECIPE_BLUEPRINT = "REVIEWED_RECIPE_BLUEPRINT"
    LLM_AUTHORED = "LLM_AUTHORED"
    NON_FORMULA = "NON_FORMULA"
    MODEL_WORKFLOW = "MODEL_WORKFLOW"

@dataclass(frozen=True)
class FormulaStrategyFactsV1:
    candidate_origin: str
    computation_kind: str
    recipe_id: str | None
    recipe_revision_hash: str | None
    expectation_ref: str | None
    expectation_generation: str | None
    reviewed_expectation_current: bool
    blueprint_derivable: bool
    blueprint_bindable: bool
    semantic_inputs_ready: bool

@dataclass(frozen=True)
class FormulaStrategyDecisionV1:
    strategy: FormulaStrategy
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    strategy_identity_hash: str
```

The pure selector chooses no columns and reads no database. A facts assembler reads the frozen
option, current review/expectation revision, and bound blueprint. Unknown expectation generations
fail closed; they do not become V2 by default.

### 3.2 Action decision

Replace the materialization-only meaning in `activation_policy.py` with an action-oriented decision
used by both route families. Keep current freeze/current-state separation, but add artifact/formula
facts rather than overloading recipe readiness:

```text
formula_state
formula_strategy
formula_schema_version
formula_validation_result
renderer_capability
artifact_id / servable
verification_state
authoring_method_certificate
recipe_review_current
data-use/read permissions
```

Every blocker must have a closed code, a user sentence and a next action. At minimum add:

```text
LLM_AUTHORING_REQUIRED                 warning, not blocker
LLM_AUTHORING_UNAVAILABLE              blocks only LLM formula preparation
REVIEWED_EXPECTATION_LEGACY_VERSION    warning; current V3 path uses LLM until migration
REVIEWED_BLUEPRINT_NOT_EXECUTABLE      deterministic-path defect
FORMULA_NOT_READY
FORMULA_VALIDATION_FAILED
PREVIEW_RENDERER_UNSUPPORTED
PRODUCTION_METHOD_CERTIFICATE_MISSING
PRODUCTION_METHOD_CERTIFICATE_STALE
PRODUCTION_RECIPE_REVIEW_NOT_CURRENT
```

`gold_evaluation_unproven` stays in the legacy fold-owned set so old stored blockers are consumed,
but the new decision service produces it only as a preview/sandbox warning and a production blocker.

### 3.3 Durable authoring plan

Add a new migration after verifying the ledger's actual next number. At the current branch tip that
is expected to be **1100**. Never edit 1099 after it has been applied anywhere persistent.

Create append-only `formula_draft_authoring_plan` keyed one-to-one by `formula_draft_id`:

```text
formula_draft_id
candidate_origin
formula_strategy
strategy_identity_hash
recipe_id nullable
recipe_revision_hash nullable
expectation_ref nullable
expectation_generation nullable
reviewed_blueprint_revision nullable
reviewed_blueprint_hash nullable
provider_contract_hash nullable
planned_at
```

Database checks enforce:

- reviewed strategy names recipe, V2 expectation generation, blueprint revision and hash, and no
  provider contract;
- LLM strategy names the frozen provider contract and cannot claim a reviewed blueprint;
- non-formula/model decisions never create a `formula_draft` row;
- the plan cannot be updated or deleted.

For draft identity, continue using the existing `authoring_config_hash` column for compatibility,
but compute it as the hash of `{formula_strategy, strategy_identity_hash}`. Do not interpret it as a
model hash on new rows.

### 3.4 Fix the generalized work-item model

Migration **1101** separates `authoring_work_item.origin` from authoring strategy. Before writing
it, audit live row counts and values. If rows are empty, replace the old constraint directly in the
new migration. If rows exist, preserve them and backfill only facts logically guaranteed by the old
constraint; anything not provable becomes legacy-undetermined and is ineligible for sealing.

Change `AuthoringWorkItemV1` to carry:

```text
candidate_origin: recipe | llm_intent | user_definition
formula_strategy: REVIEWED_RECIPE_BLUEPRINT | LLM_AUTHORED
```

Remove `WorkItemOrigin.authors_from_reviewed_blueprint`. The new invariant is strategy-based:
`reviewed_blueprint_revision` is present exactly for `REVIEWED_RECIPE_BLUEPRINT`.

### 3.5 Durable journey aggregate

Migration **1102** adds:

- `code_generation_job`: immutable request identity, considered revision, target reading,
  environment, requested action, requester, status and terminal details;
- `code_generation_job_member`: ordered option ids, selection revision, formula draft, strategy,
  member state and blockers;
- `code_generation_job_event`: append-only stage events;
- links to build-set revision, generation authorization, generation request and sealed artifact.

States are closed and monotone:

```text
REQUESTED
PLANNING_FORMULAS
AUTHORING
READY_TO_BUILD
GENERATING_PREVIEW
PREVIEW_READY
BLOCKED
FAILED
CANCELLED
```

`BLOCKED` is a product outcome; `FAILED` is a platform failure. The job is idempotent on its exact
request content so a second click neither spends again nor creates a parallel build.

---

## 4. Implementation sequence

### Phase 0 — baseline and protect work already in flight

1. Finish or separately shelve the existing 1099 provenance writer work; do not mix it into this
   plan's first commit.
2. Record `git status`, full backend/frontend baselines, migration ledger verification and live
   row counts for `formula_draft`, `authoring_work_item`, `feature_selection_revision`,
   `build_set_revision`, `generation_request` and `sealed_artifact_v2`.
3. Add characterization tests pinning the 317/298/19/295/3/90 measurements. The test should run
   the real registry/fold/deriver, not assert a hand-constructed fixture.
4. Add route-call enumeration tests showing which old and new endpoints currently consult which
   policy. This is the before-state proof for the one-decision cutover.
5. Freeze applied migrations. Any correction gets a new migration and ledger verification runs in
   CI.

**Acceptance:** no behavioural change; clean migration ledger; existing dirty work preserved; full
suite baseline recorded.

### Phase 1 — split recipe maturity from executable action

1. Keep `fold_definition_readiness` for compatibility/reporting but stop using its state as action
   authority.
2. Add a recipe capability projection:

   ```text
   computation_kind
   reviewed_expectation: none | v1_legacy | v2_current | stale
   blueprint_derivation: derivable | refusal(code)
   suggested_formula_strategy
   ```

3. Change recipe/search/suggestion APIs to expose those fields alongside, then deprecate the
   overloaded execution-readiness label.
4. Update wording:
   - `FORMULA_BLOCKED + no_reviewed_formula_expectation` becomes “AI formula required” for a valid
     deterministic candidate;
   - conceptual/model items retain honest separate calls to action.
5. Remove client filters or disabled buttons that treat `FORMULA_BLOCKED` as equivalent to “cannot
   generate code.”

**Acceptance:** all 295 recipes remain honestly unreviewed, but none is blocked from formula
authoring solely by that fact. No recipe is promoted to reviewed by code.

### Phase 2 — implement and persist formula-strategy selection

1. Implement the pure selector and table-driven tests over every combination.
2. Add a version-aware expectation resolver; never use union membership alone.
3. Resolve `posted_debit_amount` as executable V2 reviewed blueprint.
4. Treat the two V1 expectations as `v1_legacy` and route them to LLM until separate V2 fixtures,
   pins and reviews are added.
5. Bind a reviewed blueprint using the exact frozen candidate context; a binding mismatch refuses
   deterministic authoring by name.
6. Persist `formula_draft_authoring_plan` in the same transaction as the draft request/outbox.
7. Re-read the stored plan in the worker; never recompute strategy after a review or registry moves.

**Acceptance:** identical requests resolve to the same strategy identity; recipe origin + LLM
strategy is representable; v1 review is never mistaken for v3 executable review.

### Phase 3 — wire reviewed deterministic authoring

1. Add a worker dispatch by the stored strategy.
2. For `REVIEWED_RECIPE_BLUEPRINT`, load and verify the frozen blueprint revision and binding, then
   call the existing replay orchestrator with `reviewed_blueprint=bound`.
3. Require zero author/critic provider dispatches and one verified `REVIEW_BYPASSED` event.
4. Send the proposal through the same Formula V3 parser, output authority, admission, compiler and
   renderer as LLM proposals.
5. If deterministic production refuses, record `REVIEWED_BLUEPRINT_NOT_EXECUTABLE`. Do not
   silently invoke the LLM. Offer an explicit new “Try AI formula” action that mints a new draft
   identity and shows cost/provenance change.
6. Replace the API's hard-coded formula source with the stored/verified method.

**Acceptance:** `posted_debit_amount` reaches READY and generated code with zero provider calls;
its sealed member proves `REVIEWED_RECIPE_BLUEPRINT` from trace evidence.

### Phase 4 — harden the LLM fallback

1. Route deterministic recipe candidates without a current executable blueprint to the existing
   LLM formula lane.
2. Construct the authoring intent from the frozen hypothesis, recipe semantics, bound roles,
   selected parameters, grain, temporal contract, row selections, policies, target reading and
   catalog snapshot. Do not send irrelevant catalog columns.
3. Mark recipe prose and LLM-enriched descriptions as context with authority labels; source-attested
   and human-confirmed facts outrank proposals. The LLM cannot promote its own metadata.
4. Reject physical refs outside the frozen read set, missing grains, cross-currency sums without a
   conversion policy, unsupported joins, future leakage and undeclared row-selection values.
5. Keep the author/critic loop bounded. Automatic repair may correct schema/grammar problems, but a
   missing business decision becomes `NEEDS_USER_INPUT`, not another guess.
6. Persist provider contract, dispatches, prompt/schema identities, result hash and critic evidence.
7. Provider absence blocks only LLM-strategy members. Reviewed deterministic members remain usable.

**Acceptance:** an unreviewed recipe can produce a valid Formula V3 proposal and preview artifact;
the same candidate with missing grain makes zero provider calls and returns the grain blocker.

### Phase 5 — build the one action-decision service

1. Implement the matrix in section 2 using the existing frozen/current split.
2. Remove gold from the recipe readiness producer and simultaneously add the production publish
   gate. Keep the legacy gold blocker in the fold-owned set. This is one atomic commit.
3. Add method-level current-certificate readers:
   - LLM: exact author/critic/provider/formula contract certified by the gold corpus;
   - reviewed blueprint: exact deterministic producer/compiler/grammar contract certified, plus
     current blueprint review.
4. Fold mixed artifacts all-must-pass by 1099 member provenance.
5. Record the exact certificate revision used on a production publication attempt; never resolve
   “latest” after the attempt.
6. Wire `GENERATE_PREVIEW`, `EXECUTE_SANDBOX` and `PUBLISH_PRODUCTION` at authoritative worker/write
   boundaries, not only route/button boundaries.
7. Make old materialization endpoints call the same service as thin adapters.

**Acceptance:** gold-pending produces code and sandbox warnings but blocks production; both API
families return byte-equivalent decisions for the same selection/artifact.

### Phase 6 — create the durable code-generation coordinator

1. Add `POST /code-generation-jobs/plan` as a read-only cost/readiness preview. It returns:
   - ordered selected options;
   - per-member formula strategy;
   - number of deterministic vs LLM formulas;
   - estimated provider calls/token ceiling;
   - required user decisions;
   - blockers and warnings from the server decision service.
2. Add `POST /code-generation-jobs` as the one explicit write/spend action. It records the request,
   immutable selection revisions and formula authoring plans in one transaction, then enqueues work.
3. The coordinator waits on durable formula states. It does not poll providers or hold a database
   transaction.
4. When every buildable member is READY, derive/validate the build declaration from confirmed facts:
   population spine, grain, cadence, availability promise, operand facts, policy realizations,
   empty-window behaviour and environment. Ask the user only for facts that cannot be derived.
5. Record the build set, generation authorization and generation request, preserving selected order.
6. Let the existing generation worker restore, admit, compile, render and seal.
7. Store per-member failures and allow “continue with ready members” only after explicit user
   confirmation that creates a new job identity. Never silently drop a selected feature.
8. Add `GET /code-generation-jobs/{id}` and an event/progress projection.
9. Cancellation stops not-yet-started provider calls and future stages; it cannot claim to cancel a
   provider call already in flight.

**Acceptance:** browser closure/reload does not interrupt the job; duplicate submission makes no
duplicate selections, LLM calls, build set or generation request.

### Phase 7 — frontend journey

1. Keep per-row “Draft formula” for inspection without selection.
2. Replace the decision rail's readiness wording with per-selection methods:

   ```text
   1 reviewed formula · no AI cost
   2 AI formulas required · estimated cost and provider-call ceiling shown at confirmation
   1 needs a grain decision
   ```

3. Add “Prepare formulas and code” as the explicit action. Show cost confirmation only when one or
   more members use the LLM.
4. Add a generation workspace reached from the returned job id, with stages:
   - Selected
   - Preparing formulas
   - Validating
   - Generating code
   - Code ready
   - Sandbox verified
   - Production eligibility
5. For each member show:
   - recommendation origin;
   - formula method;
   - exact formula and assumptions;
   - bound tables/columns, grain, event time, window, filters and policies;
   - validation findings;
   - code files and lineage;
   - sandbox/production decisions.
6. Use badges with plain language:
   - “Reviewed recipe formula”;
   - “AI-authored from recipe”;
   - “AI-proposed feature and formula”;
   - “Sandbox ready — production certification pending.”
7. Provide direct clearing actions for missing grain, ambiguous event time, currency/reversal
   policy and stale metadata. After resolution, regenerate a new considered revision rather than
   mutating the frozen one.
8. Conceptual patterns show “Save idea / Specify computation”; governed model outputs show
   “Configure model.” Neither presents a misleading deterministic Generate Code button.
9. Reuse `FeatureExecutionScreen` after sealing, but add a real active-publication read before ever
   showing “Published.”

**Acceptance:** the entire example journey is possible with keyboard and screen reader; button
labels state writes/spend; all status/blocker wording comes from the backend.

### Phase 8 — reviewed-expectation growth programme

This phase improves deterministic coverage; it is not a prerequisite for LLM preview generation.

1. Add a “Formula review” queue beside recipe review, with the derived blueprint or its named
   derivation refusal.
2. Seed the 90 structurally derivable recipes as proposed blueprints, never reviewed ones.
3. For the remaining deterministic recipes, let the LLM propose a semantic blueprint draft. It may
   accelerate authoring but cannot approve itself.
4. Reviewers confirm banking semantics, grain, event time, filters, row selections, window,
   aggregation, null/empty behaviour, currency and policy references against worked examples.
5. Store approved expectation revisions append-only with blueprint hash, recipe revision hash,
   approver roles, test vectors and status. Replace static source membership with a database-backed
   current-revision reader only after equivalence tests cover the existing three pins.
6. Migrate the two V1 count-distinct expectations to genuine V2/V3 fixtures; delete their V1 serving
   role only after the new deterministic lane produces identical canonical results.
7. Promote a successful candidate-specific LLM formula only after abstracting physical refs back to
   semantic roles and proving it across more than one valid binding. Never promote one customer's
   column names as the universal recipe.

**Acceptance:** adding an approved expectation requires no source edit, but still requires the full
review/event/test-vector contract. Revocation immediately stops new deterministic selection without
rewriting past artifacts.

### Phase 9 — end-to-end and adversarial tests

Add real-path tests, not source-inspection substitutes:

1. Reviewed `posted_debit_amount`: hypothesis -> recommendation -> selection -> zero LLM calls ->
   V3 formula -> code artifact -> sandbox.
2. Unreviewed deterministic recipe: same journey -> LLM author+critic -> code -> sandbox warning.
3. LLM-origin recommendation: LLM formula path with honest origin and method.
4. Mixed set: reviewed + recipe/LLM fallback sealed together with correct per-member provenance.
5. Gold absent: formula/code exists, sandbox allowed with warning, production blocked.
6. Current method certificate: production allowed only for members matching that method/config.
7. Stale/mismatched certificate: production refused.
8. Missing grain: blocked before any provider call.
9. Ambiguous event date: user decision required; no guessed date.
10. Currency, reversal, status and target-leakage negatives.
11. Provider unavailable: deterministic member succeeds; LLM member reports provider blocker.
12. Deterministic producer defect: no silent LLM fallback.
13. Double-click/reload/cancellation and queue redelivery idempotency.
14. Retirement during multi-turn LLM authoring stops the next provider call.
15. Old and new route equivalence over the full action matrix.
16. Direct API permission and stale-snapshot attempts cannot bypass UI policy.
17. Mutation tests reintroduce: gold on preview, origin-as-method, missing provenance treated as pass,
    selection-triggered LLM call, and silent member dropping. Every named test must fail under its
    reintroduced defect.

**Acceptance:** full backend, frontend and E2E suites green; each safety test has been proved to bite
the defect it names.

### Phase 10 — Kind deployment and cutover

1. Build and test migrations on a scratch restore of the branch-owned database.
2. Back up before applying; verify the migration ledger before and after.
3. Deploy backend workers before the frontend so every UI state has a server meaning.
4. Run the real CIB example through Kind/Postgres:

   ```text
   Hypothesis: customers with rapidly rising posted debit activity may require review
   Grain: public.bo_cib_customer.cust_num (or the governed transaction-to-customer key)
   Reviewed member: posted_debit_amount where applicable
   LLM member: debit amount growth, current 30d vs prior 30d
   ```

5. Verify generated Spark/Kedro files are visible, artifact hashes reproduce, sandbox results have
   one row per declared grain and production stays blocked without a current certificate.
6. Point the old API to the shared coordinator/decision service. Instrument direct calls.
7. After one UAT cycle shows zero non-adapter callers, delete the old materialization policy and
   route; do not leave two implementations “for safety.”
8. Keep the cluster branch-owned until its migration lineage is merged into main and the image
   contains the same migration set.

---

## 5. API response shape

The UI should receive one honest member record:

```json
{
  "option_id": "opt_...",
  "feature_name": "posted_debit_amount_30d",
  "candidate_origin": "recipe",
  "recipe_id": "posted_debit_amount",
  "formula_strategy": "REVIEWED_RECIPE_BLUEPRINT",
  "formula_state": "READY",
  "formula_content_hash": "...",
  "actions": {
    "generate_preview": {"allowed": true, "blockers": [], "warnings": []},
    "execute_sandbox": {"allowed": true, "blockers": [], "warnings": []},
    "publish_production": {
      "allowed": false,
      "blockers": [{
        "code": "PRODUCTION_METHOD_CERTIFICATE_MISSING",
        "reason": "The deterministic recipe compiler has not been certified for production.",
        "next_step": "Run Formula quality certification from Governance."
      }],
      "warnings": []
    }
  }
}
```

An unreviewed recipe differs in exactly the facts that differ:

```json
{
  "candidate_origin": "recipe",
  "formula_strategy": "LLM_AUTHORED",
  "warnings": [{
    "code": "LLM_AUTHORING_REQUIRED",
    "reason": "This recipe has no reviewed executable formula; AI will propose one for these bound columns."
  }]
}
```

The API never reports an LLM-authored recipe formula as “reviewed recipe formula.”

---

## 6. Observability and operating controls

Record, without logging sensitive values:

- selected members by candidate origin and formula method;
- deterministic versus LLM authoring latency;
- provider calls/tokens/cost per job and member;
- LLM parse, critic, admission and repair outcomes;
- blocker counts by action and code;
- percentage of unreviewed recipe candidates reaching preview and sandbox;
- method-certificate production refusals;
- deterministic routes with non-zero provider calls (alert: must remain zero);
- formula jobs with missing or contradictory member provenance (alert and refuse);
- queue age, retries, cancellations and retired-draft stops.

Add operator views for stuck jobs and provider faults. Product blockers are not paged as incidents;
platform failures are.

---

## 7. Adversarial review of this plan

### Attack: “Let the LLM write all 295 formulas and call the problem solved”

That would be faster but unsafe. Some candidates lack a grain, usable event time, currency policy or
non-duplicating join. The LLM path is available only after semantic eligibility and never converts
missing governed facts into guesses.

### Attack: “Recipe origin proves the formula is reviewed”

False in the current production lane: every formula draft uses the LLM. This plan separates origin
and method in the database and re-derives the final method from durable run evidence at sealing.

### Attack: “There are three reviewed expectations, so all three can take V3 deterministic authoring”

False: two are V1. The version-aware resolver and migration task prevent a union-membership boolean
from laundering V1 review into V3 execution authority.

### Attack: “Automatically fall back to the LLM if deterministic authoring fails”

That hides a broken reviewed blueprint, changes cost and changes the production certificate. The
fallback is explicit and creates a new identity.

### Attack: “Trigger formula generation as soon as the checkbox is selected”

That creates surprise spend, makes exploration costly and is hard to cancel. Selection remains
free; only the confirmed job starts provider work.

### Attack: “Put one authoring method on the artifact”

A mixed artifact can contain both methods. Per-member provenance and all-must-pass publication are
non-negotiable.

### Attack: “Keep gold on the readiness ladder until the UI is finished”

That preserves the dead end. Removing it without the production gate removes protection. The two
changes land atomically, and the UI then renders the server's four decisions.

### Attack: “Let the browser orchestrate the APIs”

A closed tab or failed request would leave partial selections/drafts/builds with no owner. The
durable coordinator owns progress and idempotency.

### Attack: “Promote one successful physical formula into the global recipe registry”

That overfits one bank schema and can embed an accidental debit sign/date choice. Promotion first
abstracts to semantic roles and requires review plus multiple binding examples.

### Attack: “All formula-shaped LLM output is safe after JSON validation”

Schema validity is not banking correctness. Grain, time, target leakage, row selection, join
cardinality, status/reversal/currency rules, permissions, engine support and sandbox results remain
independent gates.

---

## 8. Definition of done

This programme is complete only when all of the following are true:

- a valid recipe recommendation with no reviewed expectation can produce an honestly labelled
  LLM-authored formula and inspectable code;
- a current reviewed V2 blueprint produces the formula deterministically with zero provider calls;
- recipe origin and authoring method are stored separately everywhere;
- selecting features never spends and duplicate submission never spends twice;
- missing grain/operands/policies block before unsafe authoring or execution;
- code preview and sandbox are not blocked by gold certification alone;
- production publication is fail-closed on a current, method-matched certificate for every member;
- old and new APIs return the same decision and the old route has no independent policy;
- conceptual/model recipes do not advertise deterministic code generation;
- the frontend shows origin, method, formula, assumptions, code, blockers and next steps;
- the reviewed-expectation programme can increase deterministic coverage without source-code edits
  or self-approval by the LLM;
- the full journey passes against Kind/Postgres using both a reviewed recipe and an unreviewed
  recipe/LLM fallback in one build set.

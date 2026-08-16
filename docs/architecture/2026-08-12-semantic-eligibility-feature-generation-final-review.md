# Semantic-Eligibility Feature Generation — Final Implementation Review

**Date:** 2026-08-12  
**Reviewed branch:** `feature/asset-detail-reapply`  
**Reviewed commit:** `333eaa3c8c15595f755ab55ab2db4e225eb4451f`  
**Plan:** `docs/superpowers/plans/2026-08-11-semantic-eligibility-feature-generation-workflow.md`  
**Environment:** pre-live development and testing; Kind + PostgreSQL  
**Review type:** read-only end-to-end architecture, implementation, test, frontend, API, and deployed-state review

## 1. Executive conclusion

The semantic-eligibility implementation is a substantial and useful development foundation, but it
is not the complete implementation described by the plan and it is not yet a trustworthy launch
candidate.

This conclusion does **not** mean there is a current production incident. The system is not live,
there are no customer workloads to protect, and development records do not need production-grade
backward compatibility. That changes the remediation strategy significantly:

- there is no need to preserve the legacy physical-column generation path;
- development database records may be reset or migrated destructively under an agreed test-data
  procedure;
- family-by-family runtime flags and compatibility response versions can be removed if no external
  consumer exists;
- a deployment rollback can use the last known-good image rather than carrying multiple feature
  engines in the same build;
- zero recipe approvals and zero shadow observations are expected unfinished setup, not customer
  impact.

Pre-live status does **not** change the core correctness findings. Today, a candidate can be based
on provisional AI metadata, have no executable formula or governed physical plan, and still be
registered or governed through the UI. Several authored V2 constraints are carried in data classes
but not enforced. These defects make current end-to-end testing give misleading results and should
be fixed before serious analyst or banking-SME UAT.

### Overall disposition

| Question | Answer |
|---|---|
| Can engineers continue developing and testing components? | **Yes** |
| Is the semantic engine useful for exploratory shadow output? | **Yes, with experimental labeling** |
| Can current results be treated as governed or executable features? | **No** |
| Should `semantic_v1` be declared complete or production-ready? | **No** |
| Should the team preserve the legacy workflow for compatibility? | **No, unless a real external consumer is identified** |
| Best pre-live strategy | **Make the semantic workflow the only workflow and fix it directly** |

## 2. What was reviewed

The review followed the user workflow and its supporting contracts end to end:

1. hypothesis and target submission;
2. confirmed use-case scope;
3. recipe applicability and abstract LLM intent generation;
4. planning-request normalization;
5. semantic context and capability compilation;
6. eligibility, binding, temporal and dataset decisions;
7. candidate assembly and ranking;
8. typed validation;
9. considered-set persistence and immutable option identity;
10. Workbench API and UI behavior;
11. contract draft, confirmation, feature registration and downstream materialization;
12. Suggested Features reuse;
13. shadow metrics, release gates and deployed Kind state.

The review used source inspection, repository searches for actual consumers, focused and complete
test suites, frontend build/lint/tests, explicit evaluation and mutation suites, and read-only
queries against the deployed Kind environment.

## 3. Verification evidence

### 3.1 Repository state

At review time:

- local branch HEAD, `origin/feature/asset-detail-reapply`, and `origin/main` all resolved to
  `333eaa3c`;
- the implementation worktree contained no modified tracked source files;
- the plan file itself was untracked;
- this review made no application-code changes.

### 3.2 Test and quality results

| Check | Result | Interpretation |
|---|---:|---|
| Focused semantic/backend tests | 158 passed, 1 skipped | Core units are well covered |
| Complete default backend suite | 10,868 passed, 20 skipped, 69 deselected | Broad regression baseline is green |
| Frontend unit tests | 789 passed | Existing component contracts are green |
| Frontend lint | Passed | Frontend lint gate is green |
| Frontend build | Passed | Build succeeds; generated JS bundle is about 822 KB and exceeds the 500 KB warning threshold |
| Explicit `pytest -m eval` | **2 failed**, 58 passed, 9 skipped | Release/evaluation gate is not green |
| Ruff over program-changed Python files | **30 errors** | Changed-file quality gate is not green |
| `git diff --check` | **2 failures** | Two added EOF whitespace issues |
| Playwright | Could not complete | Configured local PostgreSQL on port 5432 was unavailable |

The two evaluation failures were concrete:

1. mutation `feature_gen_reads_the_thin_menu` refers to a test that no longer exists;
2. the release-suite count is pinned to 277 while the named suites now collect 364.

The current Playwright suite contains asset/search and Suggested Features scenarios. It does not
exercise the hypothesis → considered set → select → draft/govern Workbench journey.

### 3.3 Live Kind observations

| Observation | Value |
|---|---:|
| Actual backend semantic mode | `semantic_shadow` |
| Checked-in Kind manifest mode | `semantic_v1` |
| Semantic candidate observations | 0 |
| Recipe review events | 0 |
| Confirmed generation scopes | 0 |
| Immutable considered revisions | 0 |
| Active concept evidence | 229 |
| Active concept authority distribution | 229 `llm/proposed`; 0 declared/confirmed |
| Catalog profile revisions/current pointers | 2 / 2 |

In a pre-live environment, these numbers are not incidents. They demonstrate that rollout and
authority acceptance criteria have not yet been exercised and therefore cannot be used as evidence
for launch.

## 4. Plan completion assessment

The plan's own status annotations still identify unfinished work. The implementation matches that
reality.

| Plan area | Status | Assessment |
|---|---|---|
| SE-0 baseline and freeze | Partial | Baseline audit exists; cutover pins and final release evidence are not complete |
| SE-1 neutral planning contracts | Mostly implemented | Recipe/LLM/user adapters exist; user hints are not consumed and provenance is inaccurate |
| SE-2 frozen semantic context | Partial | Layer A is frozen; full consumed capability/evidence/path identity is not sealed |
| SE-3 capability compiler | Partial | Typed compiler exists; authority resolution and several required axes are unsafe or absent |
| SE-4 semantic eligibility | Partial | Important identifier/type/snapshot checks exist; floors, conflicts and many constraints are incomplete |
| SE-4b authority bootstrap | UI/backend implemented, outcome absent | Queue exists; live canary authority distribution has not moved |
| SE-5 one binder | Partial | Recipe and abstract-intent paths share a binder; constraints and physical planning do not |
| SE-6 abstract LLM intent generation | Partial | Wired, but it supplements rather than replaces legacy generation and fails in unscoped mode |
| SE-7 Gate-1 semantic serving | Partial | Semantic candidates are served; readiness, review and confirmation are not enforced |
| SE-8 physical/dataset planning | Incomplete | Adapter exists but serving never calls it; no exact source/join/PIT plan is frozen |
| SE-9 typed gauntlet | Partial | Basic typed checks exist; it can claim design-checked without checking key policies |
| SE-10 assembly and persistence | Partial | Observation and assembly exist; identity and immutable evidence are incomplete |
| SE-11 versioned API and audit | Backend partial | V2 and detail route exist; Workbench requests V1 and never opens option detail |
| SE-12 Workbench experience | Partial | Inputs/checks render; state-based selection and planned audit/buildability sections are absent |
| SE-13 Suggested Features reuse | Partial | V4 adds a semantic block; existing cards remain a separate engine result and hashes are incomplete |
| SE-14 release evidence and cutover | Incomplete | Gate fold is not a runtime guard; gold/mutation/performance/accessibility and real shadow evidence are missing |

The plan's definition of done has eleven conditions. Conditions 3, 4, 6, 7, 9, 10 and 11 are
demonstrably unmet; conditions 1, 2, 5 and 8 are only partially met.

## 5. Intended end-to-end workflow

The final workflow should have one decision path:

```text
Hypothesis + target
        │
        ▼
Human-confirmed objective scope
        │
        ├── V2 recipes
        └── abstract LLM intents (meaning only; no columns)
                    │
                    ▼
One versioned FeaturePlanningRequest per bounded parameter variant
                    │
                    ▼
One frozen, read-scoped semantic/capability snapshot
                    │
                    ▼
Deterministic semantic eligibility + authority resolution
                    │
                    ▼
Deterministic physical binding + source/join/grain/PIT plan
                    │
                    ▼
Typed safety, privacy and runtime-readiness gauntlet
                    │
                    ▼
Immutable considered revision with complete evidence
                    │
                    ├── Save idea
                    ├── Create governed contract
                    └── Materialize only when execution-ready
```

There should be no second physical-column LLM generator, no client-constructed governed feature,
and no downstream stage that silently reconstructs or substitutes a plan.

## 6. Detailed findings

Severity is interpreted for a pre-live system:

- **Critical:** must be fixed before credible governed-feature UAT;
- **High:** must be fixed before launch and normally before broad analyst UAT;
- **Medium:** may coexist with early development but must close before production readiness;
- **Low:** engineering-quality or efficiency improvement that should be scheduled deliberately.

### 6.1 Generation and intent formation

#### GEN-01 — Critical: semantic mode still runs and serves the legacy physical-column LLM path

`contract/gate1.py:844-855` freezes semantic context and then unconditionally calls
`recommend_feature_sets_report`. That function runs the old physical-column generator once per
strategy lens. Under `semantic_v1`, abstract intent and V2 recipe candidates are subsequently added
to the legacy alternatives instead of replacing them.

Consequences:

- two generation architectures are active in one request;
- legacy ideas do not pass through the new shared semantic binder/planner;
- provider cost and latency increase rather than remain neutral;
- a user can select a legacy idea even when the semantic engine would not have produced it;
- provenance and comparisons become hard to interpret.

**Pre-live recommendation:** delete the legacy generation call and its serving branch. Use Git/image
rollback rather than keeping two feature engines inside the application.

#### GEN-02 — High: broaden/unscoped abstract generation has an empty objective vocabulary

Gate 1 passes `()` as `scope_leaves` whenever `scope.primary` is null. That is exactly the broadened
unscoped case. The parser later requires every intent objective to be a member of this empty set, so
every generated intent is rejected as out of scope.

**Recommendation:** for an explicitly unscoped request, pass the complete selectable-leaf set or a
separately versioned broadened scope. Add an API test that returns at least one valid abstract intent
for `unscoped=true`.

#### GEN-03 — High: hypothesis-driven parameter selection is absent

The V2 registry contains 317 recipes; 295 have parameters and together represent up to 936 bounded
variants. `v2_recipe_candidates` calls `planning_request_from_recipe(recipe)` without chosen values,
which always selects the first allowed value.

Consequences:

- a hypothesis asking for a 90-day behavior may receive the 30-day default;
- other valid atomic variants never enter ranking;
- parameter values in identity are correct only for the arbitrarily chosen default.

**Recommendation:** resolve a bounded set of parameter variants before binding and identity. Use
deterministic hypothesis rules first and a single audited LLM choice only where necessary. Every
selected variant must be explicit on the card and in the option hash.

#### GEN-04 — High: abstract-intent provenance records the wrong scope identity

`feature_intent_generation.py:120-126` writes the catalog semantic-context hash into a field named
`confirmed_scope_hash`. Two different human scopes over the same catalog therefore carry the same
claimed scope identity. Gate 1 also omits the requesting actor when it invokes
`llm_intent_candidates`, so audited calls may not be attributable to the initiating user.

**Recommendation:** pass the immutable confirmed-scope content hash and the authenticated actor
explicitly. Keep `semantic_context_hash` as a separate provenance field.

#### GEN-05 — High: LLM intents are projected as recipes

`semantic_projection.py:98-127` assigns `generation_source="recipe"` and `recipe_id` to every
semantic candidate, including abstract LLM intents. It also loses most of the original intent's
business definition and rationale.

Consequences:

- the UI and audit API report incorrect authorship;
- option-detail lookup treats an intent ID as a recipe ID;
- corroboration and review meaning become misleading.

**Recommendation:** preserve `origin`, `source_definition_id`, intent definition, LLM call reference
and recipe identity separately. Recipe review must never be implied for an LLM-origin idea.

#### GEN-06 — Medium: the parser rejects physical keys but not physical references embedded in prose

The strict intent parser refuses keys such as `table`, `column`, `object_ref` and `sql`, which
correctly prevents those fields from controlling binding. Model-controlled display text, rationale
or definitions may still contain table/column-like strings.

This does not currently let the model choose a binding, but it weakens the stated “never name
physical data” contract and may leak misleading physical claims into UI/audit text.

**Recommendation:** add a bounded physical-reference detector against the offered catalog namespace
and reject or redact model prose that names a physical object.

### 6.2 Capability, eligibility, binding and planning

#### PLAN-01 — Critical: the shared physical planner is not connected to serving

`planner/requests.py` defines `plan_planning_request`, but repository-wide consumers are limited to
the module and tests. Neither `v2_recipe_candidates` nor `llm_intent_candidates` invokes it.

Served semantic ideas therefore have:

- no exact source-selection result;
- no governed join or cardinality decision;
- no frozen point-in-time plan;
- no `plan_envelope`;
- null aggregation and grain in the compatibility projection.

This violates the plan's no-hidden-source, no-hidden-join and frozen-plan invariants.

**Recommendation:** make physical planning mandatory after semantic binding and before candidate
assembly. A candidate without a resolved physical plan belongs in an actionable/setup section and
must not be governable.

#### PLAN-02 — Critical: review validity is ranked but never enforced at activation

Recipe review validity is computed for each candidate and used only as a sort preference. Contract
draft, confirmation, direct feature registration and materialization do not check that the recipe's
review is current at its canonical revision.

The live development database has zero recipe review events, so this is not an edge case: every
recipe is currently unapproved.

**Pre-live recommendation:** family allowlists are unnecessary unless internal staged testing needs
them. Retain one simple hard rule: an unreviewed recipe may be displayed or saved as an idea, but it
cannot create a governed computation or materialize.

#### PLAN-03 — Critical: provisional concept authority is informational, not enforced

The binder correctly treats `llm/proposed` as provisional and the projection sets
`RoleBinding.confirmation_required`. That flag is displayed by Workbench but no backend activation
consumer checks it. The UI also allows the card to be selected.

On the current catalog all 229 active concept evidence rows are `llm/proposed`. Consequently, the
current Workbench can govern candidates whose binding the engine explicitly says requires human
confirmation.

**Recommendation:** enforce `confirmation_required=false` at contract-authoring activation. Provide
a direct deep link to the exact field-decision queue and require regeneration after confirmation so
the new evidence is frozen into a new revision.

#### PLAN-04 — Critical: readiness is discarded before selection and governance

Registry state is:

- 295 `FORMULA_BLOCKED`;
- 19 `CONCEPTUAL_ONLY`;
- 3 `FORMULA_AUTHORABLE`;
- 0 `FORMULA_VALIDATED`;
- 0 `MATERIALIZATION_READY`.

Assembly knows readiness, but `semantic_projection` does not put it on `FeatureIdea`. Every bound,
non-refused candidate becomes a normal selectable card. Workbench's `canSelect` condition accepts
all generated candidates.

**Recommendation:** preserve separate axes through every layer:

1. semantic binding state;
2. design/runtime validation state;
3. recipe-review state;
4. formula readiness;
5. materialization readiness.

Do not compress them into `DESIGN_CHECKED`.

#### PLAN-05 — Critical: the direct feature-registration route bypasses the governed workflow

Workbench offers “Register” in addition to “Govern.” `POST /features` accepts a client-supplied name,
description, grain, aggregation and lineage, then writes a feature without verifying:

- considered-option identity;
- semantic verdicts;
- authority floors;
- recipe review;
- formula readiness;
- snapshot freshness;
- physical plan;
- personal-data policy.

This is a complete bypass, not merely a UI inconsistency.

**Pre-live recommendation:** redefine this route as `Save idea` with an explicit non-governed state,
or remove it from Workbench. Creating a governed feature must use only the immutable considered
option and server-side activation policy.

#### PLAN-06 — Critical: capability authority can be attached to the wrong value

`column_capabilities.py:121-132` reads all active evidence ordered oldest to newest and keeps the
last producer/strength for a `(logical_ref, field)` pair. It does not verify that the evidence's
`proposed_value` equals the resolved graph value, and it does not invoke the governed field resolver
or represent conflicts.

Example failure:

1. the resolved graph displays concept A;
2. a later active evidence row proposes concept B;
3. the compiler applies B's producer/strength to displayed concept A;
4. A may incorrectly clear an authority floor.

It can also let weaker later evidence downgrade stronger evidence.

**Recommendation:** compile from the versioned field-resolution result. Pin the selected value,
producer, strength, evidence ID/content hash and conflict state as one indivisible decision.

#### PLAN-07 — High: execution authority is never evaluated

Every one of the 1,195 V2 operands requires `execution_authority="governed"`. Eligibility always
checks a hard-coded `suggestion_at_declared` matrix key and only reports the authored floor in the
verdict. Nothing consumes `execution_authority`.

**Recommendation:** define and enforce separate matrices for retrieval, suggestion, governed
authoring and execution. A lower floor may permit showing a candidate; only the execution floor may
permit materialization.

#### PLAN-08 — High: authored V2 constraints are transported but not enforced

The following behavior-bearing fields are copied into `RequiredOperandV1` but are absent or only
partially consumed by the live semantic path:

- `allowed_source_grains`;
- `join_role`;
- `temporal_role`;
- `unit_expectation`;
- exact `currency_expectation` and conversion policy;
- `status_policy_ref`;
- `relationship_requirement` and cardinality;
- sign-direction policy;
- output null, empty-population and zero-denominator policies;
- additivity compatibility with the actual operation.

The current sign logic treats a non-empty authored expectation string as if governed sign evidence
exists. That is a particularly dangerous error for debit/credit, inflow/outflow and exposure legs.

**Recommendation:** make every field either:

- an enforced input to eligibility/planning/validation; or
- explicitly unsupported and therefore a named blocker.

Add a meta-test that fails whenever a behavior-bearing contract field has no registered consumer.

#### PLAN-09 — High: personal-data use policy is not part of semantic activation

The compiler records `use_policy_absent`. It blocks registry-classified protected characteristics,
but it does not evaluate the actual column's sensitivity and permitted-use policy for the requested
purpose. Read permission does not automatically authorize feature use.

**Recommendation:** integrate the existing personal-data policy resolver into capability and
planning. Persist the exact policy revision IDs that licensed the use. Refuse activation where the
purpose is absent or denied.

#### PLAN-10 — High: the typed gauntlet can overstate `DESIGN_CHECKED`

The current gauntlet checks target-ref leakage, blocked binding, entity-key uniqueness work,
event-history work, a small set of setup codes and dataset-story requirements. It does not determine
many items that the legacy UI copy associates with design checking, including:

- unit and currency compatibility;
- additivity against operation;
- status/reversal policy;
- relationship/cardinality safety;
- source grain;
- sensitivity/use policy;
- formula/output-policy correctness;
- execution authority.

**Recommendation:** `DESIGN_CHECKED` may be emitted only after a central registry confirms every
mandatory policy family was evaluated. Missing capability must produce `NEEDS_SETUP` or
`UNSUPPORTED`, never success by omission.

#### PLAN-11 — High: shortlisting silently truncates valid candidates

The binder keeps the first 16 exact-concept matches in catalog order. It calculates a `truncated`
flag and then discards it. A later human-confirmed column can be omitted while earlier proposed
columns are retained. The declared 4,096 assignment bound is not used because there is no bounded
assignment search.

User-definition `binding_hint_refs` are accepted into the contract but never used by the binder.

**Recommendation:** rank the full bounded shortlist by authority, exact concept, governed role,
source-grain compatibility and user hint before truncation. Persist and display truncation. If a
required role was truncated without a conclusive winner, fail closed rather than claim missing or
ambiguous from an incomplete search.

#### PLAN-12 — High: semantic closure is narrower than the plan

The generation context indexes exact concepts only. The binder does not use concept ancestry,
identifier namespace or governed alternative relationships unless an operand explicitly lists an
alternative. This reduces the benefit of the rich metadata already produced by enrichment.

**Recommendation:** build a versioned retrieval closure containing exact concept, allowed ancestors
or descendants, entity, identifier namespace and governed semantic relationships. Retrieval may
widen; deterministic eligibility must still decide whether a candidate clears.

#### PLAN-13 — High: database work scales per recipe

`v2_recipe_candidates` loops over each eligible recipe. Each iteration calls the capability compiler
and loads recipe review events. With hundreds of applicable recipes this becomes hundreds of SQL
queries. The existing query-count test proves only that `graph_node` is not repeatedly loaded.

**Recommendation:** batch all candidate refs across the run, compile each capability once, load all
review events once, and then apply pure folds per recipe. Add total-query and latency assertions over
the real CIB and FTR catalog sizes.

#### PLAN-14 — Medium: projection readiness is checked after model work

Semantic context is created before model dispatch, but the durable metadata snapshot and projection
readiness check occur after candidate generation. A projection-lagged request may therefore spend
LLM calls before ultimately returning 503.

**Recommendation:** perform all fail-closed projection/read-scope readiness checks before any paid
provider call.

#### PLAN-15 — High: the context seal does not cover all consumed decisions

The semantic-context hash covers Layer-A graph rows, table facts, watermark and registry/policy
versions. It does not seal the exact Layer-B evidence rows, resolver result, capability hashes,
dataset-profile hashes, relationship/path decisions or all policy revisions consumed later.

An evidence authority change that leaves the displayed graph value unchanged can evade the Layer-A
freshness comparison.

**Recommendation:** persist a generation decision manifest containing content hashes for every
consumed capability, evidence decision, relationship, dataset profile, formula policy and planner
artifact. Option freshness must compare that manifest, not only the display graph.

### 6.3 Candidate identity, persistence and downstream execution

#### LIFE-01 — High: semantic deduplication can merge different computations

`candidate_assembly.semantic_signature` hashes output type/unit-kind/additivity, broad computation
kind, objective, grains, part of temporal state, parameters and bound roles. It omits several
identity-bearing fields, including:

- exact formula expectation/reference;
- eligibility and leakage policies;
- output currency/null/empty/zero-denominator policies;
- operand authority, source-grain, unit, status and relationship constraints;
- exact source/join/PIT plan;
- policy and review revisions.

Two materially different formulas can therefore collapse into one card.

**Recommendation:** derive option identity from the complete canonical planning request plus frozen
physical plan and decision-manifest hashes. Keep display deduplication separate from executable
identity.

#### LIFE-02 — High: immutable considered revisions do not contain the complete semantic decision

The private considered revision stores the public `FeatureIdea`, option identity and legacy recipe
grounding context. It does not store the complete planning request, candidate readiness, review
validity, full semantic verdict set, losing shortlist, dataset story, formula result or source plan
as part of the exact option record.

The plan anticipated a considered-revision extension migration; only migration 1062, the observation
store, exists in this program.

**Recommendation:** store one immutable, hash-verified `SemanticOptionDecisionV1` per option and link
it directly from the considered revision. Drafting must consume that object without recomputation.

#### LIFE-03 — High: audit detail can retrieve evidence for the wrong option

The option-detail endpoint queries the newest semantic observation by `generation_run_id` and
`source_definition_id`. It is not linked by option ID, planning-request hash or observation ID.
Merged twins, variants or repeated observations for the same definition can therefore return a
different row from the exact candidate selected.

**Recommendation:** put the semantic observation ID and decision hash in the option record and
retrieve it by that immutable foreign key.

#### LIFE-04 — Critical: Formula V2 is not executable downstream

The main formula authoring orchestrator still stamps Formula V1. Materialization admission explicitly
refuses any formula schema version other than V1. Only one Formula-V2 reviewed expectation is pinned.

This is acceptable during development **only if the UI and APIs state the truth**. It is not
acceptable to let Formula-V2 or formula-blocked cards appear governable/materializable.

**Recommendation:** choose one of two honest pre-live milestones:

1. implement Formula-V2 authoring, validation and materialization before enabling those actions; or
2. scope the current milestone to semantic feature ideas/contracts and disable materialization with
   an explicit “engine support not implemented” state.

Do not emulate execution through generic prose or a V1 formula.

### 6.4 API and Workbench experience

#### UI-01 — High: Workbench does not request the semantic considered-set contract

The backend supports explicit `contract_version=2`, including semantic mode and immutable revision
identity. `frontend/src/api.ts:1532-1569` never sends it, so Workbench receives the V1 top-level
contract by default.

**Pre-live recommendation:** make the semantic contract the only Workbench contract. If no external
client needs V1, remove the version switch rather than retaining compatibility complexity.

#### UI-02 — High: Workbench never consumes the option-detail audit endpoint

The backend exposes
`GET /contract/considered-revisions/{revision}/options/{option}`, but the frontend has no client or
screen consumer for it.

**Recommendation:** load detail on demand and show the exact frozen roles, authorities, rejected
alternatives, formula/readiness, dataset choice, path, PIT policy and revision hashes.

#### UI-03 — Critical: selection is not state-aware

Workbench defines every generated candidate as selectable. “Take this set” selects every
unregistered member. Neither path checks binding state, confirmation requirement, review validity,
formula state or physical-plan state.

**Recommendation:** the backend must return `allowed_actions` for each option. The client should
render those actions rather than recreate policy. A disabled action must name the exact next step.

#### UI-04 — High: the card loses the information required for a responsible decision

The current semantic card can show inputs and external checks, but not the complete planned
hierarchy:

- what is computed, including formula/operation and parameter window;
- output grain, unit, currency and additivity;
- chosen population dataset and source;
- join path/cardinality and point-in-time behavior;
- recipe review and formula/materialization readiness;
- immutable audit revision;
- explicit distinction between “save idea,” “govern contract” and “materialize.”

**Recommendation:** make the card summarize these sections and move complete evidence into the audit
drawer. Do not use one green badge to represent multiple readiness axes.

#### UI-05 — High: Suggested Features can display two disagreeing engines

Suggestion contract V4 adds a separate semantic block to the V3 legacy suggestion response. Legacy
cards remain independently generated and can disagree with semantic ranked/actionable candidates.
The new engine therefore appears as commentary rather than the authority for each card.

Semantic context hashes are partial and dataset-profile hashes remain unpopulated on the actual
suggestion cards.

**Pre-live recommendation:** replace legacy suggestion hits with the same semantic option carrier
used by Workbench. Keep one card model and one eligibility result; use route context only to filter
or anchor the shared results.

#### UI-06 — Medium: actionable and empty states are incomplete

The planned distinction among “nothing applicable,” “metadata confirmation required,” “relationship
setup required,” “formula not authored,” and “data validation required” is not complete. Deep links
and regenerate-after-fix behavior are also missing.

**Recommendation:** return typed next actions and render separate empty/setup states. After a user
completes governance work, always generate a new immutable considered revision.

### 6.5 Shadowing, deployment and release gates

#### REL-01 — High: the semantic release gate is not a deployment or startup guard

`semantic_planning_gate` exists and has unit tests, but there is no production call site. Setting the
environment to `semantic_v1` activates serving without proving that the gate passed.

**Pre-live recommendation:** do not add another runtime flag system. Make a release command or CI
artifact compute and sign the gate result, and require that artifact at build/deploy time. Once the
semantic engine is the only path, rollback should deploy the preceding image.

#### REL-02 — High: shadowing does not measure the required divergence

Shadow mode persists semantic candidate observations and logs aggregate counts, but it does not
persist an exact mapping between legacy accepted/rejected outcomes and semantic outcomes, nor an
explained/unexplained adjudication. Abstract LLM intent generation is not meaningfully compared in
shadow.

`semantic_shadow_metrics` cannot directly produce all fields expected by the release gate.

**Recommendation:** during the remaining development period, run a finite, explicit comparison
corpus rather than preserve indefinite runtime shadow complexity. Persist candidate-pair divergence,
review it, then delete the legacy path.

#### REL-03 — Medium: checked-in and deployed modes disagree

The checked-in Kind manifest declares `semantic_v1`; the running backend ConfigMap contains
`semantic_shadow`. This is harmless experimentation but invalidates claims that the checked-in
configuration was deployed and verified.

**Recommendation:** record deployed image digest, Git SHA and effective configuration together.
Make the development dashboard show them.

#### REL-04 — High: release evaluation is red

The default test suite excludes eval-marked tests. When run explicitly, mutation and release-count
gates fail. Therefore “all tests pass” is true only for the default subset and is not a production
readiness statement.

**Recommendation:** repair the stale mutation target, deliberately rebaseline increased suite
counts, and add the semantic-program gold cases described below. Run the release marker in CI for
release candidates.

#### REL-05 — High: banking end-to-end, performance and accessibility evidence is missing

The implementation has strong unit tests, but the plan requires complete banking gold, mutation,
API, UI, performance and accessibility gates. Missing proof includes:

- real Workbench browser flow;
- exact `cust_num` authority transition;
- transaction-event versus snapshot rejection;
- mixed-currency and conversion-plan behavior;
- debit/credit sign-policy behavior;
- reversal/status-policy behavior;
- non-additive balance behavior;
- governed cross-catalog customer/account joins;
- total SQL-query and latency budgets on CIB/FTR-sized catalogs;
- keyboard and screen-reader behavior for selection, blockers and audit drawer.

**Recommendation:** make these named, versioned gold fixtures and Playwright/API scenarios, not
informal demonstrations.

#### REL-06 — Medium: changed-file quality gates are not green

Ruff reports 30 issues across program-changed Python files, including import ordering, unused imports
and an unused variable. `git diff --check` reports two added blank-line issues. These are not feature
correctness defects, but they indicate that the final branch did not pass a clean changed-file gate.

**Recommendation:** make changed-file Ruff, `git diff --check`, frontend lint, typecheck, build and
release-marker tests mandatory before declaring the program complete.

#### REL-07 — Low: frontend bundle exceeds the configured warning threshold

The frontend builds successfully but produces an approximately 822 KB JavaScript bundle. This may
slow first load and makes future UI growth less visible.

**Recommendation:** add route-level lazy loading and establish a measured performance budget before
launch. This is not a blocker for current component development.

## 7. Recommended candidate lifecycle

The product currently conflates “interesting idea,” “governed definition” and “executable feature.”
Use explicit states and server-authorized actions:

| State | Meaning | Allowed action |
|---|---|---|
| `IDEA_CONCEPTUAL` | Useful concept, but formula or required structure is not authored | Save idea only |
| `BINDING_ACTION_REQUIRED` | Missing/ambiguous/provisional input or governance setup | View and resolve tasks |
| `SEMANTICALLY_BOUND` | Inputs bind, but review/formula/plan/data checks remain | Save idea; create draft only if policy permits |
| `CONTRACT_AUTHORABLE` | Current recipe review, exact formula reference, exact frozen plan and authoring authority | Create governed contract |
| `EXTERNAL_VALIDATION_REQUIRED` | Governed design exists but named runtime checks are outstanding | Keep contract; do not execute |
| `FORMULA_VALIDATED` | Formula gold/provider validation passed | Eligible for materialization admission checks |
| `MATERIALIZATION_READY` | Execution authority, runtime checks, engine support and plan freshness all pass | Materialize |
| `RETIRED_OR_STALE` | Revision superseded, evidence drifted or policy changed | Regenerate; no activation |

The backend, not the browser, should return something equivalent to:

```json
{
  "allowed_actions": ["save_idea"],
  "blocked_actions": {
    "create_contract": ["CONCEPT_CONFIRMATION_REQUIRED", "FORMULA_BLOCKED"],
    "materialize": ["FORMULA_SCHEMA_UNSUPPORTED", "EXECUTION_AUTHORITY_UNMET"]
  }
}
```

## 8. Pre-live simplification decisions

The following choices are recommended specifically because the product is not live:

### 8.1 Remove rather than preserve

- Delete the legacy physical-column LLM generator from the hypothesis workflow.
- Delete `legacy / semantic_shadow / semantic_v1` serving modes after a finite comparison run.
- Make the current semantic API contract the default and remove V1 compatibility if no real client
  uses it.
- Remove feature-family allowlists unless the team needs staged internal UAT.
- Replace in-app engine rollback with deployment of the last known-good image.
- Reset or one-time migrate development considered sets rather than building elaborate compatibility
  readers for incomplete snapshots.

### 8.2 Keep as non-negotiable controls

- append-only review and decision evidence;
- current recipe review at the exact revision before governed activation;
- separate suggestion and execution authority floors;
- read-scope and purpose/use-policy enforcement;
- exact frozen physical plan before selection for governed use;
- immutable considered-option identity;
- named blockers and next actions;
- formula/materialization engine compatibility checks.

### 8.3 Safe to defer until closer to launch

- production rollback drill and operational runbook rehearsal;
- preserving old development response bytes;
- multi-family canary orchestration;
- large-scale performance tuning after the N+1 design is removed;
- predictive-value backtesting, which the plan already names as a successor program.

Deferral is acceptable only when the UI clearly marks the unavailable capability and no endpoint can
activate it indirectly.

## 9. Recommended remediation program

### Phase A — Make current testing honest

**Objective:** prevent incomplete candidates from being mistaken for governed features.

1. Introduce one server-side `activation_decision(option, action, actor)` policy.
2. Call it from feature registration, contract draft, confirm, formula authoring and materialization.
3. Convert direct `POST /features` into `Save idea`, or remove it from Workbench.
4. Preserve and return binding, confirmation, review, formula, validation, plan and materialization
   states.
5. Return backend-computed `allowed_actions` and typed blockers.
6. Disable Workbench selection/govern/materialize actions accordingly.
7. Add negative tests proving that provisional, unreviewed, conceptual and formula-blocked options
   cannot activate.

**Exit criterion:** no browser or direct API call can turn an incomplete option into a governed or
executable feature.

### Phase B — Establish the one real generation engine

**Objective:** eliminate competing decisions and freeze one exact candidate plan.

1. Remove the legacy physical-column LLM call and its lens outputs.
2. Fix unscoped objective handling.
3. Correct LLM origin, actor and confirmed-scope provenance.
4. Resolve bounded parameter variants before binding.
5. Batch semantic context, capability and review inputs once per run.
6. Run the shared physical planner for every recipe, LLM intent and user definition.
7. Refuse or action-classify unresolved source, join, cardinality, grain and PIT plans.
8. Persist the selected plan without later recomputation.

**Exit criterion:** every displayed governed candidate came through one request, binder, planner and
validation path, with no legacy candidate mixed in.

### Phase C — Complete metadata and banking-policy enforcement

**Objective:** make rich metadata materially improve feature quality.

1. Replace last-row authority pins with governed field resolution and exact evidence hashes.
2. Add retrieval, suggestion, authoring and execution authority matrices.
3. Implement every V2 operand/output/eligibility field or fail with `UNSUPPORTED_*`.
4. Integrate personal-data purpose/use policy.
5. Implement governed sign, status/reversal, unit, currency, additivity and source-grain checks.
6. Build governed semantic closure for concept ancestry and identifier namespaces.
7. Make shortlist truncation explicit and authority-ranked.
8. Require all mandatory policy families before `DESIGN_CHECKED`.

**Exit criterion:** a behavior-bearing recipe field cannot exist without a registered deterministic
consumer and test.

### Phase D — Finish identity, audit and product experience

**Objective:** guarantee that the candidate seen is the candidate governed.

1. Define `SemanticOptionDecisionV1` containing the full planning request, variant, verdicts,
   readiness, review, validation, dataset story, plan and decision-manifest hashes.
2. Persist it immutably per option and link observations by exact ID.
3. Base executable option identity on the complete request + physical plan, not the display card.
4. Make Workbench consume the semantic contract and detail endpoint.
5. Render computation, buildability, readiness and audit sections.
6. Replace Suggested Features' parallel semantic block with the shared option carrier.
7. Implement deep links and regenerate-after-governance behavior.

**Exit criterion:** option detail, draft and confirmation read the same immutable decision object and
never decorate it from current live metadata.

### Phase E — Prove launch readiness

**Objective:** replace confidence from unit tests with measured release evidence.

1. Repair and expand the eval/mutation suite.
2. Add the named banking gold corpus.
3. Add real Workbench API/browser and accessibility tests.
4. Measure total SQL queries, p50/p95 latency and LLM calls/cost on CIB/FTR catalogs.
5. Run a finite legacy-versus-semantic comparison and adjudicate every unexplained divergence.
6. Confirm at least one canary catalog's authority distribution through real human review.
7. Record build SHA, image digest, effective config and signed release-gate result.
8. Test deployment rollback immediately before launch.
9. Remove comparison code and temporary modes in the cutover commit.

**Exit criterion:** every definition-of-done condition is supported by a named automated result or
recorded operational artifact.

## 10. Required banking acceptance corpus

At minimum, the following cases should be hand-reviewed, versioned and automated:

| Case | Required result |
|---|---|
| `public.bo_cib_customer.cust_num` as a generic numeric measure | Hard refusal: identifier is not a measure |
| `cust_num` as customer/entity key with only `llm/proposed` concept | Visible as provisional; cannot govern |
| Same key after human concept confirmation | Eligible for key/grouping roles; still subject to uniqueness check |
| Event-window transaction feature over a current snapshot | Hard refusal or named event-history setup requirement |
| Account balance summed across time | Refuse unless the declared stock/flow and aggregation policy permits it |
| Transaction amount with mixed currencies | Require exact conversion policy and currency source |
| Debit and credit legs bound to one column | Refuse unless a governed sign/direction policy separates them |
| Posted transactions containing reversals | Apply exact eligible-status/reversal policy; do not infer from prose |
| Customer feature joining transaction and customer master | Require verified relationship, cardinality and PIT-safe path |
| Sensitive customer attribute allowed to read but not use | Refuse feature activation under purpose/use policy |
| 30/90/180-day recipe | Produce explicit bounded variants or select the hypothesis-compatible variant |
| Metadata change after consideration | Draft returns stale/regenerate; never silently rebinds |
| Recipe review superseded by revision change | Activation refused until the new revision is reviewed |
| Formula V2 presented to V1-only materializer | Honest unsupported state; never automatic downgrade |

## 11. Revised release gates

The following are appropriate for a pre-live launch decision:

### Zero-tolerance correctness

- no protected, purpose-denied or target-leaking input accepted;
- no identifier bound as a generic measure;
- no proposed metadata clears a declared/governed floor;
- no unreviewed recipe activates;
- no conceptual/formula-blocked candidate is described as executable;
- no hidden or recomputed source/join/PIT plan;
- no V2 formula enters a V1-only execution engine;
- no direct API bypasses the activation policy.

### Functional quality

- expected-buildable banking gold cases produce the reviewed variant and physical plan;
- expected-unbuildable cases refuse for the reviewed reason;
- parameter selection agrees with the hypothesis or exposes variants honestly;
- Workbench and Suggested Features present the same semantic verdict;
- exact option evidence survives reload and drift checks.

### Engineering quality

- default and eval suites green;
- mutation suite green with current victims and deliberate baselines;
- changed-file lint, typecheck, build and diff checks green;
- Workbench real-backend browser flow green;
- keyboard/screen-reader checks green;
- total query and latency budgets green on representative banking catalogs;
- LLM provider-call budget is measured and does not include the retired physical-column generator.

### Operational evidence

- deployed SHA/image/config agree;
- real semantic observations exist for reviewed canary runs;
- authority distribution moved through confirmation, not weaker policy;
- rollback to the preceding image was tested;
- the final build contains one serving engine.

## 12. Final recommendation

Continue development on this implementation, but change the program's status from “complete” to
“semantic feature-generation foundation implemented; activation and production-readiness work
remaining.”

Because the product is pre-live, make a clean architectural correction now:

1. remove the legacy generation path and compatibility machinery with no real consumer;
2. enforce one candidate lifecycle on the server;
3. connect semantic binding to the real physical planner;
4. complete authority, banking-policy and Formula-readiness enforcement;
5. make Workbench and Suggested Features consume the same immutable semantic option;
6. then prove it with banking gold, real Workbench E2E, performance and release evidence.

The current implementation is suitable for continued engineering and controlled exploratory tests.
It should not yet be used to claim that generated features are governed, formula-ready,
materialization-ready or production-ready.


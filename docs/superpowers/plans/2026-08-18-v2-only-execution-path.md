# V2-only: from a READY formula to a published feature

**Decision (given):** one formula language. V2 semantics, wire format `formula_schema_version = 3`.
No V1/V2 router, no compatibility mode, no V1 migration, no byte-equivalence guarantee, no
user-visible version choice. The product is not live. V1's *execution* machinery is reused; V1's
*language* is not.

**Revision 3**, after two review rounds. Three rulings adopted (compile target, capability model, FX
ownership), two blocking contradictions fixed, three missing contracts added, and the deletion
inventory folded back into the earlier steps it changes. Changes are marked ▲ and the reasons kept,
because the corrections are the useful part.

**No open questions remain.** The plan is ready for task-level execution.

**Verification standard:** every number here was produced by running the shipped code, not counted
by eye. Revision 1 broke that standard once and the record is kept in §2.

---

## 0. Execution status — updated 2026-08-19

| Steps | State | Notes |
|---|---|---|
| **0–8** | **done** | migrations 1091, 1092, 1093 APPLIED to live (189 total) |
| **9** | **compile half only — and narrower than its own header claims (§0.9)** | `compile_generation_v2` closes admitted → operator graph; rendering and sealing are the caller's, and are not yet wired |
| 10 | **blocked on GOVERNANCE, not code** | needs real policy payloads *and* each policy's `read_basis` — see below |
| **11** | **done** | `avg`, `min`, `max` render, type, and reach an operator graph |
| 12 | not started | FX needs the physical-binding layer §7 names (`bound_rate_dataset_ref`, `binding_snapshot_id`), which does not exist |
| **13** | **done** | migration 1094 + `verification_request_store` — the lifecycle `verification_attempt` had no room for |
| **14** | **gate done** | `authorize_publication_v2` + `VERIFICATION_ABSENT`; the reconciler and the endpoint change are not done |
| **15** | **stages 1–4 done; V1 ROUTING RETIRED** | leaf repoint, the v1 authoring lane's retirement (§0.2) and six shared-machinery extractions (§0.4) are all green. The DELETION is gated on 8 remaining V1-BY-TYPE edges, not on a producer — §0.1's producer blocker was wrong and is corrected in place |

### 0.1 ▲ Step 15 is not "one deliberate commit" — corrected 2026-08-19, and its
blocker was itself WRONG (see §0.2). Kept for the reasoning, not for the verdict.

The plan says step 15 is cheap **because** step 0 happened. That is true of the half step 0 was
about, and it is now DONE: every shared-leaf import across 68 files was repointed from
`formula.schema` to `formula.schema_leaves`, the re-export shim is gone (45 re-exported names → 20
genuinely used), and `formula/schema.py` is now exactly the V1 language and nothing else. Suite
green.

**What the estimate missed is that the V1 LANGUAGE is still the live authoring vocabulary.**

* 20 source modules use V1-language names, and **9 of them are the live authoring stack** —
  `result.py` (10 importers), `critic.py` (6), `parse.py` (5), `output_authority.py` (5),
  `capability.py`, `canonical.py`, `replay_authoring.py`, `recipe_authoring.py`, `authoring.py`.
  They are reachable from `recipe_formula_worker`, `enrich_llm` and `planner/requests`.
* Several V1 modules are **machinery V2 REUSES** rather than language: `ir.py`'s read-scope, union
  and authorize functions, `expression_ir.compile_expression`, `physical_types`' helpers. Those get
  repointed, never deleted — the plan's own rule is that V1's *execution* machinery is reused.

**▲ CORRECTED — a review found the blocking claim wrong.** An earlier version of this section said
the producer never declares. That was researched badly: `recipe_formula_eval` and
`recipe_formula_blueprint_derivation` were checked, found silent, and the conclusion drawn from
their silence. They are not the serialization owner. `build_recipe_authoring_egress`
(`recipe_egress.py:657`) is, and it has always written the declaration for a
`BoundRecipeFormulaExpectationV2`. Adding the field in the evaluation modules would have created a
second source of truth for one fact.

The real position was: **298 of 317 recipes declared `formula-v2`, 19 have no formula, and exactly
TWO still declared `formula-v1`** — `merchant_mcc_diversity` and `obligor_facility_count`, both of
which already derived valid v2 blueprints and needed no reason to stay.

### 0.2 The V1 routing retirement — DONE 2026-08-19

1. **Both remaining v1 recipes converted to `formula-v2`.** Capturable v1 recipes: **0**.
   * `obligor_facility_count` was trivial — its derived v2 blueprint carries the same grain
     (`obligor`/`obligor`) as its reviewed v1 entry, so the lane moved and nothing else did.
   * `merchant_mcc_diversity` was **not** trivial and was escalated rather than absorbed: its
     reviewed v1 entry declared `merchant` grain while its definition and derived blueprint say
     `customer`, so converting the lane changes what the feature is computed PER.
     `test_the_merchant_v1_entry_is_untouched` existed to say that belongs to a human. It was
     decided explicitly: **per customer**.
   * The stale entry is **retired in place, not re-keyed** — and that is forced rather than chosen.
     Its v1 template declares needs `merchant`/`mcc`/`event_ts` and no `customer`, so
     `validate_expectation_registry` refuses the re-key by name. Editing the template to add a need
     would rewrite a reviewed artifact to say something it was never reviewed for. Capture now
     derives the customer-grain blueprint; the entry survives unselected until the v1 registry goes
     with the rest of the stack.
2. **A registry-wide producer invariant** (`test_expectation_lane_invariant`) over all 317 recipes:
   nothing capturable declares v1, every capturable blueprint is the V2 type — the thing
   `CaptureBlueprintV1.bind` dispatches on, so "binds to a V2 expectation" is a type fact rather
   than 317 grounding contexts — and the single serialization owner always writes the declaration.
3. **Absence is terminal and the v1 worker arm is gone**, in one change because either alone is
   worse than neither. `DEFAULT_EXPECTATION_SCHEMA = "formula-v1"` deleted;
   `AUTHORABLE_EXPECTATION_SCHEMAS` has one member; both `authors_v2` branches removed.
   `EXPECTATION_SCHEMA_UNDECLARED` is DISTINCT from `EXPECTATION_SCHEMA_UNKNOWN`: unknown is a work
   item from a newer build, undeclared is our own producer, and one word for both sends an operator
   to investigate the wrong thing.

**Status to report until a real provider run succeeds:**

```
V1 routing retired:                     yes
V2 routing verified deterministically:  yes  (registry-wide invariant + FakeLLM producer→worker)
Capturable v1 recipes:                  0
Real-provider authoring verified:       NO   — a separate acceptance gate
```

The authoring lane is NOT to be reported as fully operational on the strength of the above.

**▲ NAMING, before the final deletion.** `formula_schema_version` names two different things: a
string LANE selector (`"formula-v2"`) and an integer product WIRE FORMAT (`3`). Split them on the
work item — `expectation_schema` and `output_formula_schema_version` — so later code cannot confuse
*which lane authors this* with *which format it produced*. Not done here: it changes the persisted
payload bytes, which the v1 golden freeze pins.

### 0.3 Stages 4–6 — measured 2026-08-20, and §8.1's pattern repeats at FUNCTION level

**Nothing is transitively dead.** All 13 V1 modules are reachable from live code, because V2 and V3
modules import from them. So stage 6 cannot delete anything until stages 4–5 cut those edges — the
deletion is gated on the extraction, not on courage.

**64 names across 8 modules are what stands between here and deletion:**

| V1-named module | live importers | names |
|---|---|---|
| ~~`formula.result`~~ | ~~6~~ → **3** | **EXTRACTION DONE.** The version-neutral half is now `formula/authoring_result_leaves.py`; the 3 that remain need `AuthoringResult`, which carries a `TypedFormulaV1` and is genuinely V1 |
| `formula.frozen_configuration` | 4 | 8 — incl. `freeze_current_configuration_v2`, `verify_frozen_configuration_v2` |
| `formula.critic` | 3 | 6 — `CriticFinding`, `CriticReview`, `critique`, `proposal_column_refs` |
| `formula.parse` | 2 | 5 — `_plain`, `_build_filter`, `_build_expected_output`, `parse_proposal_v1` |
| `formula.authoring` | 1 | 5 — `AUTHORING_MAX_TURNS`, `_Trace`, `_trace_turns` |
| `formula.recipe_authoring` | 2 | 3 — `recipe_expectation_validator_v2`, `recipe_tool_runner_v2` |
| `formula.canonical` | 2 | 2 — `filter_plain`, `formula_content_hash` |
| `formula.replay_authoring` | 2 | 2 — `_intent_material`, `_restore_terminal_result` |
| `formula.tools` | 1 | 2 — `TOOLS`, `run_tool` |

**▲ THE FINDING, and it is §8.1 again one level down.** §8.1 said `formula/schema.py` "is not a V1
module — it is the shared structural-leaf library, and the module name simply lies about its
contents". The same is true of these, for FUNCTIONS rather than types. Two of them **export `*_v2`
functions**: `frozen_configuration` ships `freeze_current_configuration_v2` and
`verify_frozen_configuration_v2`; `recipe_authoring` ships `recipe_expectation_validator_v2` and
`recipe_tool_runner_v2`. `formula.result`'s authoring-result vocabulary is imported by
`formula.result_v2` itself. None of that is V1 language; it is shared machinery wearing a V1 name.

**So stages 4–5 are step 0 repeated per module, and the order follows the edge count:**

1. ~~`formula.result`~~ — **DONE 2026-08-20.** `authoring_result_leaves.py` now holds the axes, the
   disposition vocabulary, the coherence error and the six status literals. `authoring_v2`,
   `replay_authoring_v2` and `result_v2` no longer import from a V1 module to describe a V2 result.
   Live→V1 names: **64 → 46**. What is left of `result.py` is V1 by type, not by name.
   Then the two unreachable v1 arms above went: **46 → 45**, and the first V1 code was DELETED
   rather than merely bypassed. `_intent_material` took it to **44**, and the canonical filter form to **43**.

**A note on which V1 functions are still dead-but-undeletable.** `freeze_current_configuration`
(v1), `freeze_provider_contract` and `verify_provider_contract` all have ZERO production callers,
and none can be deleted yet: live V1 TESTS exercise them, and `verify_frozen_configuration` (v1) is
still called by `replay_authoring`, which stays alive because `materialize.resolve` needs
`_restore_terminal_result`. That chain is the shape of the remaining work — one V1-typed function
in `materialize` holds a whole V1 module alive.
2. `formula.frozen_configuration` and `formula.recipe_authoring` — **the first real STAGE-6
   DELETES, 2026-08-20.** Both had a v1 arm that the routing retirement made unreachable:
   * `recipe_formula_shadow` froze a v1 configuration in an `else` branch reachable only for a v1
     BOUND expectation, which `capture_blueprint_for` can no longer produce. Branch removed.
   * `recipe_authoring.recipe_tool_runner` and `.recipe_expectation_validator` (v1) had **zero
     production callers** — 122 lines deleted, with `test_recipe_authoring.py`. Verified against
     §8.6 first: the byte-freeze is on `recipe_egress._validate_formula_expectation_v1`, not here,
     and there are 0 durable work items.
   * Three tests existed to prove the v2 siblings were needed by demonstrating the v1 ones
     misbehave on v2 input. That demonstration cannot run once the v1 functions are gone, so the
     claim was converted into a guard that asserts their ABSENCE — which fails if either is ever
     reintroduced. A demonstration deleted is a claim lost; a guard is the same claim that outlives
     its subject.
3. `formula.replay_authoring` — **half done 2026-08-20.** `_intent_material` extracted to
   `formula/intent_material.py`: five fields off an authoring intent, hashed identically by BOTH
   generations, which is why `materialize.authoring_trace` was reaching into a v1-named module to
   re-derive a trace for a v2 run. Moved VERBATIM — durable rows were sealed under
   `canonical_hash(_intent_material(intent))`, so a move that also tidied would be a
   re-identification wearing a refactor's commit message. `_restore_terminal_result` stays: it
   returns a V1-typed `AuthoringResult`, so `materialize.resolve`'s edge is stage-4 migration work.
4. ~~`formula.canonical`~~ — **DONE 2026-08-20.** `canonical_leaves.py` holds the normalization
   primitives and the FILTER canonical form. `filter_plain`'s own docstring already made the
   argument: it was made public so `materialize.expression_ir` — the compiler BOTH generations use
   — could ask the question the canonical form answers instead of rendering a filter twice and
   disagreeing about what it IS. A `FilterNode` is a shared leaf, so none of that was ever V1's.
   Moved verbatim: these bytes decide `formula_content_hash` and every governed formula was sealed
   under them.
5. `formula.parse`, `formula.critic`, `formula.tools`, `formula.authoring` — same play; extract the
   shared helpers, leave the V1 language behind.
6. THEN stage 6 deletes what is finally unreachable, and regenerates the goldens (§8.2).

Each extraction is independently committable and independently green, exactly as step 0 was. What
must NOT happen is deleting a V1 module while a V2 one still imports from it — the reachability
measurement above is the check, and it should be re-run before every delete.

### 0.4 Stage 4 progress and the honest remainder — 2026-08-20

Extracted so far, each verbatim and each independently green:
`authoring_result_leaves` · `intent_material` · `canonical_leaves` · `parse_leaves` ·
`materialize/identifiers` · and `frozen_configuration_v1` (the V1 half moved OUT, leaving the
shared module clean). Plus the first real deletes: the shadow's unreachable v1 freeze branch and
`recipe_authoring`'s v1 tool-runner + validator.

**▲ A mistake worth keeping.** `_tool_registry_material` went out with the v1 freeze functions and
BOTH freezes hash it — the v2 one broke instantly and took the whole capture path with it
(`CAPTURE_PERSIST_FAILED` on every shadow run). It is shared: the tool registry is what the model is
handed, and neither generation may be frozen against a registry it was not shown. The lesson is that
"lives beside the v1 function" is not evidence of being v1's; only reading who calls it is.

**What is left is no longer cheap.** The remaining V1-language users split three ways:

| | modules | why it is not an extraction |
|---|---|---|
| **The V1 compile chain** | `materialize.ir`, `materialize.compile.wiring`, `materialize.physical_types`, `materialize.admission` | they walk a `TypedFormulaV1` body. `compile_ir_v2` is their replacement; they go when V1's `compile_ir` goes |
| **The V1 authoring stack** | `formula.authoring`, `parse`, `canonical`, `capability`, `critic`, `output_authority`, `result`, `replay_authoring`, `operations`, `tools` | genuinely V1, and held alive by 8 live edges that are V1 BY TYPE |
| **The V1 expectation registry** | `overlay.upload.recipe_formula_contracts`, `recipe_formula_expectations` | the reviewed v1 blueprints; dead weight since the routing retirement, deletable once `recipe_audit`/`recipe_formula_eval`/`recipe_formula_gate` stop reading them |

**The 8 V1-BY-TYPE edges are the whole remaining gate**, and each needs a decision rather than a
move: `materialize.resolve` needs `_restore_terminal_result` → `AuthoringResult`;
`materialize.admission` needs `formula_content_hash(TypedFormulaV1)` and `AuthoringResult`;
`formula.gold` needs `AuthoringResult` and `proposal_column_refs`; `parse_v2`/`parse_v3` need
`_build_expected_output`, whose result is walked into `proposal_content_hash_v2` and folded into
sealed `formula_content_hashes` — so reshaping it re-identifies sealed artifacts, and
`output_intent_v2` reads its fields with `getattr(..., None)`, meaning a rename would silently yield
`unit=None` while still reporting the expectation as present rather than raising.

### 0.5 ▲ RULED 2026-08-20 — the evaluator stays truthfully V1 until its replacement exists

`recipe_formula_eval` stamps V1's `OPERATION_GRAMMAR_VERSION`/`OUTPUT_POLICY_VERSION`. **Do not
switch those to the V2 constants.** Both currently equal `1`, and that is ACCIDENTAL NUMERIC
EQUALITY, not semantic compatibility — swapping the import would relabel V1 evidence as V2 without
changing anything about what was actually evaluated.

The surviving evaluator is genuinely V1: it uses `recipe-formula-evaluator-v1`, the V1 expectation
registry, the V1 gold corpus and V1 formula expectations. It should keep saying so.

**The correct end state is a REPLACEMENT lane with an explicit identity** — V2 formula language, V3
wire format, V2 output policy — persisting on every run:

* evaluator contract version
* `expectation_schema = "formula-v2"`
* formula wire schema version `3`
* `OPERATION_GRAMMAR_VERSION_V3` (intentionally aliasing V2)
* `OUTPUT_POLICY_VERSION_V2`
* `CANONICALIZATION_VERSION_V3`
* a NEW corpus version and hash
* the V2 expectation/blueprint registry hash
* the existing provider-contract hashes

**The V1 gold corpus is not reusable.** It covers two legacy V1 recipes and cannot certify the new
execution path.

The product is not live and the evaluation tables are empty, so the evaluator is replaced rather
than run in parallel — but the transition is ATOMIC:

1. create the V2/V3 evaluation contract and corpus;
2. run it successfully;
3. make it the only accepted lane;
4. delete the V1 evaluator, corpus and expectation registry;
5. make a missing evaluation/version identity TERMINAL.

Never turn V1 evidence into "V2" by swapping an integer-valued import.

### 0.6 `ExpectedOutput` is now ONE shared contract — DONE 2026-08-20

Ruled: extract it EXACTLY, neither renamed nor loosely copied. `output_type`/`unit`/`currency` moved
to `schema_leaves` unchanged; V1's import is the SAME object; v2 and v3 declare
`ExpectedOutput | None` instead of `object | None`; `output_intent_v2` uses attribute access and
raises on a shape it cannot read.

That looseness was the bug: `getattr(expectation, "unit", None)` turned a renamed or missing field
into `unit=None` while `authored_expectation_present` stayed True — an expectation that exists and
carries nothing, which `AuthoredOutputIntentV2` does not refuse because it validates only the
converse. Silent data loss from one rename.

Regression tests pin all four required properties, with the pre-change hashes captured from the
SHIPPED code and written as literals — a test that derives both sides of its own equality proves
only that the code agrees with itself:

```
v2 content hash with expected_output:  d0ee93e6…  UNCHANGED
v3 content hash with expected_output:  f344e944…  UNCHANGED
```

The 26 gold_v2 fixtures all carry a NULL expected_output, so their passing is NOT coverage of this
field — the test says so out loud rather than letting a future reader mistake it for one.

### 0.7 ▲ WITHDRAWN — "one import plus fixtures" was WRONG. Corrected 2026-08-20.

This section claimed `materialize.resolve`'s import of `replay_authoring._restore_terminal_result`
was the single edge holding the V1 authoring stack alive, and that cutting it cost one import plus a
test module's fixtures. **That was researched from inside `resolve.py` alone.** It reads only
`authoring_disposition` — true — but it does not KEEP the result. It hands it to `admit_artifacts`
(`compile/chain.py:576`), whose check 4 is the anti-forgery gate and re-derives
`formula_content_hash` from `result.candidate_formula` (`admission.py:325`).

```
AuthoringResult    candidate_formula = TypedFormulaV1 | None
AuthoringResultV2  candidate_formula = does not exist   (it has candidate_proposal / candidate_output)
```

Switching the restorer would hand V1's forgery check an object with no formula to hash. `resolve` is
not a shared consumer with a V1 restorer; it is **one stage of the V1 execution chain**, and the
chain is what has to move.

### 0.8 ▲ THE RENDERER DID NOT ACCEPT V2 — corrected 2026-08-20, then FIXED 2026-08-20

An earlier commit widened `render_project`/`project_datasets` to accept either token and plan, and
reported that as the renderer accepting V2. **The outer type gate was widened; the internals were
not.** Verified by execution — passing a real `FeatureGroupPlanV2` to `published_dataset_name`
raises `TypeError`:

* `render/publish.py:69` — `published_dataset_name(plan: FeatureGroupPlanV1)`, refuses V2
* `render/nodes_compute.py:381` — `render_spine_node` requires a V1 plan AND a V1 contract
* `render/nodes_compute.py:912` — requires a V1 contract
* `render/nodes_compute.py:2181` — accepts a V2 IR and still requires a V1 plan
* `render/nodes_gate.py:165` — `render_assembly_node` requires a V1 plan
* `compile/wiring.py:50` — V1 schema, V1 plan, V1 contract, V1 chain inputs

**Consequently §5's "the project and wiring layers are reusable as they stand" is FALSE** and is
struck. They are reusable in SHAPE; every entry point is typed on V1's plan and contract.

**FIXED.** `render/renderable.py` now declares `RenderablePlan`, `RenderableContract` and
`RenderableIR` once, with no imports back into `render/` — `nodes_compute` already imports from
`project`, which imports `publish`, so a union defined in `nodes_compute` closed that loop. Every
annotation and every `isinstance` gate in `render/` takes the union.

**It is a union and not a second renderer because the overlap was MEASURED:** across `render/` and
`compile/wiring.py` the only contract attributes read are `ordered_keys` and `pit_semantics`, and
the V1/V2 contracts differ in exactly one field (`physical_type_policy_version` vs
`physical_type_policy`) — as do the plans. Nothing the renderer touches is versioned.

**And the acceptance is proved by RENDERING, which is what the first attempt lacked.**
`test_render_v2_boundary` no longer reads a single annotation: it drives a real V1 object and a real
V2 object through the same functions and diffs the output. `render_assembly_node` — the node that
reads the most off the plan — emits BYTE-IDENTICAL source from both. A structural test also fails on
any `isinstance(..., FeatureGroupPlanV1)` left anywhere under `render/`, since one of those refuses
a V2 plan at a single call site while every signature says otherwise. The 12901-test suite includes
the renderer goldens, so V1's emitted code is unchanged.

### 0.9 ▲ WHAT `compile_generation_v2` IS AND IS NOT — corrected 2026-08-20

`pilot_v2`'s module header claims the chain reaches rendering and sealing. It does not: it returns
authorization, group plan, one operator graph per feature and the contract hash, and stops
(`pilot_v2.py:203`). The header is corrected in code.

It does NOT call `resolve_executable_output_v2`, `record_bound_formula`, `record_group_plan`,
`evaluate_generate`, `render_project` or `seal_v2`. More seriously, `AdmittedFeatureV2` drops the
restored `candidate_output` and `output_intent`: the pilot re-resolves a basic output policy from
caller-supplied operand facts and never performs the authored-intent-versus-governed-output
reconciliation in `output_resolution_v2.py:119`.

**So step 3 is NOT done** and its row is corrected: `BuildSet` and `generation_request` exist as
stores with NO production callers. The runtime worker (`queue_lane.py:161`) drains the V1
materialization queue only, and `POST /feature-execution/generations` records an authorization and
says in as many words that nothing was queued (`feature_execution.py:141`). `build_set_store.py:267`
is a read followed by an update with no fenced claim — two workers could observe the same state.

**§8.7's "finished work waiting to be connected" overstates it** and is struck: the pieces are
unit-tested in isolation, and the middle is unwelded.

Three further gaps the review surfaced, none of them yet addressed:

* **Authorization is not bound to the artifact.** `generation_request` references no generation
  authorization; `sealed_artifact_v2` references none; `compile_generation_v2` takes authorization,
  logical group and environment separately without proving they agree; `evaluate_verify` accepts a
  client-supplied authorization id without checking it produced the artifact under verification.
  There is no referential chain authorization → request → sealed artifact.
* ~~**Sealing has an unresolved group-level design problem.**~~ **RESOLVED 2026-08-20.** `seal_v2`
  takes a graph OR a sequence, checks EVERY member and folds one verdict: satisfied only if all are,
  findings the union, each naming its own member. The member label is read off each graph's terminal
  `GROUP_ASSEMBLY` rather than supplied, so attribution is unforgeable — there is no second place to
  say which member a graph is. **Both shapes are legitimate:** one graph assembling the whole
  group's columns (what the vocabulary was designed for) or one per feature (what
  `build_operator_graph_v2` emits). A first cut required exactly one column per graph and broke a
  passing test that had been assembling two — mistaking this program's own habit for the rule.
* **Activation is deliberately closed.** `semantic_option_decision.py:426` always returns False and
  the seam walkthrough monkeypatches it, so no stored evaluation can promote a recipe to
  FORMULA_VALIDATED or MATERIALIZATION_READY today.

### 0.10 SEQUENCING — ruled 2026-08-20, replacing §0.5's ordering

The evaluator cannot validate materialization code it never invokes: `recipe_formula_eval` evaluates
author/critic/provider behaviour and does not compile, render, seal or execute. Adding V2/V3 identity
fields to it leaves it an authoring-quality evaluator. So the deterministic chain comes FIRST.

```
1. correct the plan                                          <- this section
2. complete deterministic V2 generation
     restore -> admit -> bind output -> compile -> authorize
     -> graph -> generate gate -> render -> seal -> persist
3. real BuildSet/request API + fenced V2 worker
4. prove the production chain
     anti-forgery + multi-feature + mutation + Kedro/Spark
5. the separate V2/V3 authoring/provider evaluator, and expand the reviewed corpus
     (only ONE reviewed V2 expectation exists today — `posted_debit_amount`,
      recipe_formula_expectations_v2.py:44)
6. the current-evaluation validity reader
7. cut API/runtime traffic V1 -> V2
8. delete the V1 chain and the V1 authoring language
```

**None of steps 2–4 need an Anthropic key.** They are built and tested against stored V3 traces and
reviewed fixtures. A real provider run earns the authoring-quality activation evidence later; it is
not what proves that rendering and sealing work.

**§8.6 verified against live on 2026-08-19 (read-only), and its ROW half clears:** the plan says the v1 egress
byte-freeze cannot be deleted because *"durable work-item rows were sealed against those exact
bytes"*. `recipe_formula_shadow_work_item` holds **0 rows** on this environment, so there is nothing
sealed against them. `sealed_artifact_v2`, `generation_request`, `build_set_revision`,
`verification_attempt` and `executable_policy_payload` are also empty; `formula_draft` holds the 7
known non-carrying rows and `contract_considered_revision` the 5 v2 candidates awaiting regeneration.

**▲ OPERATOR CONSEQUENCE OF STEP 11.** `renderer_build_hash` is derived from the emittable set, so
giving `avg`/`min`/`max` a rendering MOVED it:

```
live engine_operator_capability:  rbh-67dd87be54f5e80e25e8053d19e2656e   x39 rows
this build:                       rbh-e57a6c191559eaa34bd61171737a3dd7
```

That is the designed fail-safe — *"a moved renderer simply has no rows yet, and an operator with no
row for the current build is unsupported, which is exactly true"* — but it means **after deploying
this code every operator reads as unsupported until the dispatch surface is re-recorded**. Not a
defect; a step the deploy runbook has to include.

**Found during execution, and not in the plan when it was written:**

* **Step 4** — the six typed computation fields were dropped by the candidate serializer, so
  identity described how a candidate *read* rather than what it *computes*. Fixed forward; the
  frozen v2 candidates refuse with `CANDIDATE_REGENERATION_REQUIRED`, and regeneration is an
  operator runbook (`docs/architecture/candidate-regeneration-runbook.md`), never an automatic
  retry.
* **Step 5** — three defects its own tests could not reach, all found by step 6 consuming its
  output: the IR carried resolved payloads where `DeclaredPoliciesV2` belongs, `row_selections` were
  read off the proposal (a field that does not exist there) and silently dropped, and compiling
  returned a bare IR where the plan says planned.
* **Step 5** — policy payloads did not record **when** their columns are read, which is the one
  fact that decides whether a policy leaks. Added as a required field with no default: a default of
  `event_time` would have made every policy pass the leakage gate by construction.
* **Step 11** — the V2 resolver was missing V1's **fourth** nullability source: `null_input =
  ignore` on a NON-COUNT aggregate. A non-empty window whose every operand is NULL aggregates to
  NULL and the renderer deliberately does not coalesce it, so the column was being published NOT
  NULL for values the pipeline legitimately writes. It bites hardest on exactly the three
  aggregates step 11 adds, since all three are non-counts.
* **Step 11** — the advertisement test *runs* each aggregate through the fake engine, so adding
  three to the advertised set failed until the fake could execute them. That is the test working:
  advertising an aggregate nothing can run is the gap the whole capability model exists to close.
* **Step 9** — `resolve_physical_type_v3` took `DecimalTypeV2` operand types that **no caller in
  this codebase can produce**: the compiled IR establishes a governed *word* (`"numeric"`), never a
  width. Its signature was satisfiable only by a test that invented them. Rewritten to V1's
  contract, which the compiled IR satisfies directly — and `sum_type_v2`'s widening is consequently
  NOT applied, because widening on a precision nobody read publishes a type the author did not
  declare.
* **Step 9** — `compile_ir_v2` read the output policy off `proposal.expected_output`, which is what
  the author EXPECTED rather than what output authority permits. Now a required argument, resolved
  by `resolve_output_v2` in the orchestrator.
* **Step 8** — the ten sites were confirmed exactly ten, and the mechanism was worse than recorded.
  The two enums do not merely fail `is`: they compare EQUAL and **hash equal**, so a V2 member finds
  the right entry in a V1-keyed dispatch table. Dispatch working while identity fails is what let a
  V2 feature render down the wrong arm. Both halves are now one vocabulary, crossed once, in
  `compile_expression`, and enforced by `ExpressionExecutionIR.__post_init__`.

---

## 1. Rulings adopted

### Decision 1 — `FormulaExecutionIRV2` is the compile target

```
Typed Formula V3
  → PlannedFormulaExecutionIRV2
  → (leakage + authorization)
  → AuthorizedFormulaExecutionIRV2
  → OperatorGraphV2                  ← DERIVED VIEW, deterministic from the IR
  → Kedro/Spark renderer
```

The graph is derived for capability checking, dependency analysis, audit display, proof coverage and
explaining execution to a user. It is **not** a second independently authored executable form.

**▲ Verified, and it settles the matter:** `OperatorKindV2` has 13 members and **none of them is a
final-combination node**:

```
governed_scan, pit_availability_filter, semantic_selection, eligible_status_filter,
linked_reversal_survivor, as_of_fx_join, duplicate_rate_gate, missing_rate_gate,
quote_inversion, decimal_multiplication, aggregate, spine_left_join, group_assembly
```

There is no node for identity, ratio, difference or signed sum. The graph therefore *cannot* express
the final combination of any feature, and today it cannot truthfully claim to be the complete
executable form. Two honest options, and the plan takes the first:

1. **Add `FINAL_COMBINE`** with a variant per final operation. Keeps the graph able to describe a
   whole feature, which the capability model in Decision 2 needs.
2. Rename it an execution-topology/safety view and stop implying completeness.

### Decision 2 — capability by typed signature, bound to the build

Kind-level capability cannot express the true state, which is `sum` and `count_rows` supported while
`avg`, `median` and `percentile` are not — all four are `OperatorKindV2.AGGREGATE`.

Not 21 new top-level kinds. Keep the small topology vocabulary and qualify it:

```
engine_id
operator_kind          "aggregate"       "final_combine"   "semantic_selection"
operator_variant       "sum" | "avg"     "ratio"           "eligible_status"
renderer_build_hash    ← binds the proof to the code that produced it
renderer_dispatchable
execution_proof_hash
```

`renderer_build_hash` is the part that stops a stale proof outliving the code it was about. Without
it the renderer can change while an old proof stays nominally valid — a proof about a build that no
longer exists.

**This supersedes migration 1079's shape** and needs a new migration (1091), not an edit: 1079 is
applied on the live cluster.

---

## 2. The renderer gates — a corrected count

Revision 1 said five. The review said six. **Both were wrong; it is ten.**

```
 1   isinstance(ir, FormulaExecutionIRV1)        nodes_compute.py:2223   refuses LOUDLY — safe
 5   ` is FinalOperation.X `                     2447, 2586, 3081, 3086, 3144
 4   ` is AggregateFunction.X `                  2526, 2824, 2909, 2945
---
10   sites that must move atomically
```

**Why the nine are dangerous:** `FinalOperationV2.RATIO is FinalOperation.RATIO` is `False` —
different enum object, same name, same value. A V2 ratio does not fail; it takes the else-branch and
renders **something else**. Same for `AggregateFunctionV2.SUM`.

**▲ How revision 1 got it wrong, since the method matters more than the number.** It grepped four
enum names at once and reported the total. But `NullInput`, `EmptyWindowResult` and `FilterNode` are
*the same objects* in `schema` and `schema_v2` — re-exported, genuinely version-neutral. Eight of the
seventeen were comparisons that are perfectly fine. A plan that inflates a hazard is not safer than
one that understates it; it just moves the error.

**Rule: all ten move in one commit, or none.** Removing 2223 alone converts a loud refusal into a
wrong number, which here means a wrong feature in a credit model.

**▲ Beyond the sweep** — normalize V2 enum values **once at the renderer boundary**, validate against
a **closed dispatch table**, and refuse anything unknown by name. And migrate
`ExpressionExecutionIR.aggregation` off the V1 enum type: relying on string-enum equality is
accidental compatibility, not an execution contract.

**Structural guard worth its keep:** no ` is ` comparison against a V1 enum member anywhere under
`render/`. The defect class, stated once, in a form that cannot drift.

---

## 3. The two blocking contradictions in revision 1

### ▲ Contradiction 1 — the pilot could not seal

`seal_v2(conn, graph: OperatorGraphV2, ...)` — the graph is the **second positional argument**
(`seal_v2.py:106-108`). Revision 1 put the graph builder in Phase 5 and asked Phase 3's pilot to
reach sealed code. That is impossible without hand-constructing a graph, which would bypass the
architecture the pilot exists to prove.

**Fix:** the minimum deterministic graph builder moves **before** the pilot, covering exactly:

```
governed_scan → pit_availability_filter → aggregate{sum,count} → spine_left_join
              → group_assembly → final_combine{identity}
```

Later phases *expand* graph coverage rather than introducing the graph for the first time.

### ▲ Contradiction 2 — Phase 0 could not produce READY

`advertised_operators()` selects `WHERE renderer_dispatchable AND execution_proof_hash IS NOT NULL`.
Revision 1 proposed writing only dispatchability in Phase 0, deferring proofs to Phase 6, and then
claimed a formula would reach READY. Both cannot hold.

This was self-contradictory on its face — the intersection was quoted earlier in the same session
and then ignored. The temptation it creates is the dangerous part: the shortest path to a green
pilot is to write a proof record for a proof nobody ran, which is precisely the lie the advertised
set exists to prevent.

**Fix — three separate states, because they are three separate claims:**

| State | Means | Gates |
|---|---|---|
| **Formula admitted** | structurally valid, and the renderer supports every operation it needs | **code generation** |
| **Execution qualified** | this implementation passed its developer gold-data proof | visible; enforced per publication policy |
| **Artifact verified** | *this* generated artifact passed on-demand verification | **publication** |

Code generation requires only the first. Publication requires a current artifact verification.
Execution qualification stays visible and enforceable but does not block a user from *seeing* code —
which is the product decision already made.

**No manufactured proof records, ever.**

---

## 4. Missing contracts revision 1 did not define

### ▲ 4.1 The generation orchestrator — "Prepare selected features"

Revision 1 described components and no production operation joining them, which would have made the
pilot a hand-assembled demonstration rather than a feature.

```
select candidates → immutable BuildSet → queued generation request
  → load admitted formulas → bind physical inputs → resolve policies → compile IR
  → leakage + authorization → derive group + operator graph → render → seal
  → code and blockers in the UI
```

Must define, before implementation:

- a durable generation request with status
- an idempotency key (the draft lane's formula-identity pattern is the precedent — a double-click
  must not buy a second run)
- the binding from selected candidate to **formula revision**
- retry behaviour
- **partial group failure** behaviour — one refused feature in a group of five
- where every refusal is stored
- which artifact the UI shows

Note `BuildSetRevisionV1` is a dataclass with no store, and no build-set migration exists. That is
part of this work, not a prerequisite someone else did.

### ▲ 4.2 Executable policy payloads

A realization currently gives a content hash and provenance. `eligible_status_policy_hash = abc123`
does not let a renderer emit `WHERE transaction_status IN ('POSTED','SETTLED')`. A hash names a
decision; it is not the decision's content.

A versioned executable-policy payload store is needed, covering at least: eligible status values;
debit/credit direction mapping; reversal linkage and survivor rule; currency fields and rate
relation; FX quote convention; missing-rate behaviour.

**Every declared policy resolves to executable content or causes a named refusal. No silent
defaults** — a defaulted policy is a wrong number wearing a governed costume.

### 4.3 What is built and merely unwired

Needs calling, not building — each has zero production callers: `resolve_output_v2`,
`authorize_compilation_v2` (Gate 2 for V2), `leakage_v2` (V1 has *no* compile-time leakage gate at
all), `seal_v2`, `record_group_plan`, `record_bound_formula`, `record_inventory_observation`.

Does not exist: `compile_ir_v2` (nothing in production constructs a `FormulaExecutionIRV2` — every
V2 test wraps an expression compiled by the **V1** compiler), the graph builder, any writer of
`engine_operator_capability`.

---

## 5. Scope, stated honestly

**▲ Revision 1's headline was too broad.** "Not a rewrite, mostly unblocking" is true of **the narrow
pilot only**. Full V2 execution needs genuinely new calculation semantics: policies, row selections,
window offsets, second operands, signed expressions, FX, and seventeen unsupported aggregates.

What remains true: three of the five node renderers never touch the formula IR; `FormulaExecutionIRV2`
is V1's ten fields under the same names plus two, with zero renames. ~~The *project and wiring*
layers are reusable as they stand.~~ **STRUCK — see §0.8:** they are reusable in SHAPE, and every
entry point in `render/` and `compile/wiring.py` is typed on V1's plan and contract. The
*calculation* layer is not the only place the new work is.

```
V2 aggregate functions:  21
renderer can emit:        4    sum, count_rows, count_non_null, count_distinct
```

`avg` is not renderable. "Average balance over 90 days" cannot render today — which is why the pilot
is sum/count only.

---

## 6. Sequence

Adopted from the review, with the verified detail attached.

| # | Step | Notes |
|---|---|---|
| **0** | **Extract the shared schema leaves** — split `formula/schema.py` into shared leaves + `schema_v1` | ▲ **new, and first.** 24 names V2/V3 import verbatim currently live in a module named for V1. Mechanical, low-risk, independent — and until it lands, nothing in step 15 can be deleted safely. Doing it first turns the last step from "work out what breaks" into "delete `schema_v1`". |
| 1 | Typed capability signatures + build fingerprints | migration 1091; supersedes 1079's shape. Changes visible readiness for all 263 recipes — deliberate, not incidental. |
| 2 | Separate admitted / execution-qualified / artifact-verified | resolves §3's contradiction 2 |
| 3 | BuildSet + generation request + worker | §4.1. **▲ NOT DONE — see §0.9.** The stores exist with ZERO production callers; the runtime worker drains the V1 queue only, and the lifecycle has no fenced claim |
| 4 | V2 physical binding + policy resolution | §4.2 payload store; **plus the two missing resolvers** (§8.3) |
| 5 | Compile → `PlannedFormulaExecutionIRV2` | the missing `compile_ir_v2` |
| 6 | Leakage + authorization → authorized IR | calls the built-but-unwired `leakage_v2`, `authorize_compilation_v2` |
| 7 | Minimum deterministic operator graph | **before** the pilot; adds `FINAL_COMBINE` |
| 8 | Remove all ten renderer gates atomically; normalize V2 dispatch | §2 — plus the four join-step `isinstance` sites (§8.8) |
| 9 | **Narrow pilot** — no policy, identity + sum/count, to sealed code | the first end-to-end proof |
| 10 | Semantic pilot — status, direction, reversal policies | first real policy payloads |
| 11 | Common aggregates — avg, min, max | unblocks ordinary features |
| 12 | Complex V2 ops, FX, remaining aggregates | FX ownership settled in §9 |
| 13 | On-demand verification worker | lifecycle REQUESTED→CLAIMED→RUNNING→PASSED/FAILED/REFUSED; execution identity must include the sealed artifact id |
| 14 | Publication requires a current passing verification | plus the reconciler; then the endpoint can expose the active revision, which lets the UI say "published" again |
| 15 | Delete `schema_v1` and the V1 product language; regenerate goldens | one deliberate commit. Cheap **because** step 0 happened. Verify/drain shadow work items first (§8.6). |

**Why 9 is still where it is:** every step before it is a contract or a gate, and the pilot is the
first thing that can be *wrong in an interesting way*. If the seam analysis is mistaken, it surfaces
there — on a slice — rather than after the seventeen-aggregate payload.

---

## 7. FX ownership — ruled

**The policy realization owns the rate relation. The graph carries only its resolved execution
binding.** The last open question is closed, and closed without creating a second source of truth.

```
Currency-conversion policy
  ↓ identifies the required conversion semantics
Policy realization        ← AUTHORITATIVE: rate relation, keys, time column, rate column,
  ↓                          quote convention, missing-rate behaviour
Physical binding          ← resolves the governed relation to THIS environment's dataset
  ↓
AsOfFxJoinV2              ← records exactly what THIS compilation will execute
  ↓
Gate 2                    ← authorizes that exact resolved read set
```

`AsOfFxJoinV2.rate_table_ref` must **never** be independently chosen by the graph builder or accepted
from an external caller.

### What changes

The payload today is four bare refs with no link to any realization — the two-sources-of-truth shape
this ruling removes:

```python
# now (operator_graph_v2.py:210-221)          # ruled
currency_conversion_ref: str                  currency_conversion_ref: str
rate_table_ref: str          ← chosen freely  policy_realization_revision_id: str
as_of_ref: str                                executable_content_hash: str
rate_column_ref: str                          bound_rate_dataset_ref: str
                                              binding_snapshot_id: str
                                              as_of_column_ref: str
                                              rate_column_ref: str
                                              rate_key_refs: tuple[str, ...]
```

The apparent duplication is acceptable **only** as a derived snapshot: the realization is the
decision, the binding is the environment's answer, and the graph is the frozen record of what this
compilation will run.

### The builder refuses if

Any of these means the snapshot has stopped agreeing with its source, and a snapshot that disagrees
with its source is worse than no snapshot:

1. the policy payload's rate relation cannot be bound;
2. the bound dataset differs from the graph value;
3. the rate columns lie outside that dataset;
4. the rate dataset is missing from the authorized read set;
5. the realization or binding changed after compilation.

Each refuses **by name** — never a silent default, and never a re-derivation that quietly picks a
different table.

### Two notes for whoever implements it

* **Graph identity moves.** `AsOfFxJoinV2.identity_payload()` (`:229-232`) feeds the content-addressed
  graph hash, so this changes it. That is safe here and only here: the graph is deliberately **not
  persisted** — `seal_v2` stores the verdict, not the graph — so no stored hash is invalidated. Under
  Decision 1 the graph is a derived view, and a derived view's identity is allowed to move with its
  derivation.
* **`rate_key_refs` is new** and `as_of_ref` is renamed `as_of_column_ref`. Neither exists today, so
  the producer must supply them from the realization rather than infer them — which is the entire
  point of the ruling.

---

## 8. Deletion inventory, and the corrections it forces upstream

The V1-removal analysis finished after revision 2 was written. Most of it is step 15 detail, but
four findings change **earlier** steps and one contradicts the original brief.

### 8.1 ▲ `formula/schema.py` is not a V1 module — and this reorders step 15

It is the **shared structural-leaf library** that V2 and V3 import verbatim: `FilterNode`,
`NullInput`, `EmptyWindowResult`, windows, grains, parameters, decimal policy — 24 names. Deleting
it breaks V2, not V1.

**Consequence:** nothing can be safely deleted until those 24 names have a home that is not a V1
module. So step 15 gains a prerequisite that is worth doing early and independently:

> **Split `formula/schema.py` into shared leaves + `schema_v1`.**

This is low-risk, mechanical, and it converts step 15 from "work out what breaks" into "delete
`schema_v1`". Same reason the file already says the leaves "carry no versioned vocabulary" — the
module name simply lies about its contents.

### 8.2 ▲ The renderer goldens are the execution proof — regenerate, never delete

The original brief lists "testing that V1 output remains unchanged" as removable work. Mostly true,
with one exception that would be expensive to get wrong: the **renderer goldens** look like V1
output-stability tests but they are the only thing pinning emitted Spark against reviewed expected
output. They must be **regenerated against V2 output**, not deleted.

Genuinely deletable: four explicit V1 byte/source-freeze assertions — two of which `sha256` the
**source text** of V1 functions — plus roughly **187 test functions across ten files and an
11-fixture gold corpus, about 3,435 lines.**

### 8.3 ▲ The V2 restorer does not exist — a missing link steps 4–5 assume

`materialize/resolve.py` is privately bound to the V1 restorer, and there is no V2 equivalent. This
is the concrete gap between an admitted V2 formula and a compilable one. Estimated ~200 lines and
described as the smallest high-leverage item in the whole map — it belongs in step 4, named, rather
than being discovered inside step 5.

**▲ Name it `restore_formula_v3.py`, not `resolve_v2.py`.** `resolve_output_v2` already exists
(`formula/output_authority_v2.py:77`), and a `resolve_v2` beside it invites the reading that one is
the general case of the other. They are different verbs:

* **resolve_\*** — *decide* a value that was undetermined (an output policy, a physical type).
* **restore_\*** — *rehydrate* a stored artifact into the object a compiler can use.

Keeping the two verbs distinct is worth more than matching the existing suffix.

Related and equally unnamed: `physical_types_v2.py` is **not** the V2 replacement for
`physical_types.py`; the V2 feature→type resolver does not exist. `PlannedFeature` hard-requires a
resolved physical type, so step 4 cannot complete without it.

### 8.4 ▲ `compile/wiring.py` reads `empty_window` / `null_input` off the admitted **V1** formula

Because `PitSpec` deliberately excludes them. Easy to miss, and it means the V2 compile path needs
its own carrier for those two values before step 8's renderer work can pass them. Neither IR carries
them today.

### 8.5 Cross-effect: step 1 touches user-visible recipe readiness

The engine's advertised capability — derived from the V1 renderer's four aggregates — drives the
readiness answer shown for **all 263 recipes**. Changing the capability model in step 1 changes that
display. Not a blocker, but it must be deliberate: a capability refactor that silently re-labels 263
recipes is a product change wearing an infrastructure commit message.

### 8.6 Two things that look deletable and are not

- **The recipe/shadow lane is DUAL**, chosen per work item from a declaration whose *absence* means
  v1. The v1 arm cannot be retired by flipping a default — existing rows select it by saying nothing.
- **The v1 egress byte-freeze exists because durable work-item rows were sealed against those exact
  bytes**, and every dispatch re-validates against them. "No V1 data to preserve" is true of
  *product* data; it is not true of the shadow lane's sealed work items. **Verify before deleting.**

### 8.7 What is already V2 and already dead

The S11 generate/code/verify/publish surface is V2 throughout — and unreachable, because `seal_v2`
has no production caller. ~~That is finished work waiting to be connected.~~ **STRUCK — see §0.9:**
"finished" overstates it. The pieces are unit-tested in isolation and the middle is unwelded — the
renderer is typed on V1 plans, `compile_generation_v2` stops before rendering and skips six V2
stages, and the generation lifecycle has no fenced claim. Connecting them is step 2 of §0.10, not a
wiring exercise.

### 8.8 One additional gate class, beyond the ten

The join machinery dispatches by `isinstance` on the **V1 step classes**
(`CrossCatalogJoinStepV1`, `CrosswalkJoinStepV1`) at four sites in `nodes_compute.py`. A V2 graph
either reuses those step classes or needs an adapter producing them. This is separate from §2's ten
enum/type gates and was not counted there.

Corroborating the other direction: `OperatorGraphV2`'s `PitAvailabilityFilterV2` carries `PitSpec`
**verbatim** — the V2 vocabulary already reuses the exact V1 execution type. The PIT renderer needs
no change for the two supported window bases.

---

## 9. Found during step 4: the declared grain never reaches the model

**`FeatureIdea.grain_ref` is computed and then discarded**, and nothing downstream can tell.

The generator sets it (`feature_assist.py:2614`, from the resolved grain operand). The considered
revision does not serialise it — `_idea_json` emits fifteen keys and `grain_ref` is not among them —
so `_chosen_option_from_revision` always returns an idea whose `grain_ref` is `None`. Verified on the
live cluster: **zero** stored options carry it.

**What that costs.** The draft worker builds its authoring intent as:

```python
target_grain_keys = (tuple(sorted(column_refs)) if idea.grain_ref is None
                     else (logical_ref_of(...grain_ref...),))
```

The `else` branch is dead in production. Every formula is therefore authored with grain keys listing
**every column the feature derives from**, rather than the one column it is computed per. A feature
meant to be "per customer" is described to the model as grained on its amount column, its date
column and its customer column together.

The intent hash covers `target_grain_keys`, so this is consistent — the restorer re-derives the same
wrong value and the checkpoint agrees. Consistency is why nothing has noticed.

**Why it is not fixed here.** `_idea_json` feeds `_candidate_identity`, which is the canonical
candidate identity hash. Adding a field changes that hash, which changes every stored option
identity and every draft's `planning_request_hash`. That is a migration-shaped change with identity
consequences, and doing it inside a step about restoring formulas would bury it.

**Where it belongs:** step 4's physical-binding work, as an explicit item — the grain is exactly what
binding needs to be correct about. Until then, formulas are authored against a grain nobody
declared, which is a correctness question rather than a cosmetic one.

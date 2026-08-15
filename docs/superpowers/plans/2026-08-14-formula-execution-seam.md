# The Formula / Execution Seam

**Date:** 2026-08-14
**Branch at authoring:** `feature/asset-detail-reapply` @ `d4e95429` (the E4 cutover + its three defect fixes).
**Parent program:** `docs/superpowers/plans/2026-08-13-semantic-activation-and-one-engine-remediation.md`
— that plan's own §0.1 declares materialization **explicitly out of scope** and closes with
*"contract-authoring workflow ready for UAT; materialization visibly unavailable."* **This plan is
the charter that removes that sentence.**
**Sibling charters referenced, not modified:**
`2026-08-10-banking-recipe-production-readiness-and-expansion.md` (BR-6/BR-7/BR-18 — the v2 grammar,
the readiness ladder, the expectation registries) and `docs/DEFERRED-WORK.md` §A (the materialization
program's own deferral ledger — A.24, A.26, A.36, A.42 are live inputs here).
**Execution discipline:** full backend suite green gates every push; frontend suite gates frontend
pushes; `pytest -m eval` joins the gate from A0 onward; an acceptance row is appended under the
matching task in THIS file per landed slice, with its commit hash; push
`HEAD:feature/asset-detail-reapply HEAD:main`; memory updated per milestone.

---

## 0. What exists, what is missing — the verified inventory

Everything in this section was read on `d4e95429` before a word of the plan was written. The house
rule that produced this section: *every plan defect ever found sat in an API described from memory.*
Section 9 lists every interface verified, with its file and line.

### 0.0 Four premises of the commissioning brief, corrected

The brief that requested this plan carried four claims about Phase G. **Three are stale** and one is
sharper than stated. Correcting them is the single most valuable thing this inventory does, because
three tasks that would otherwise have been written do not need to exist.

| Brief said | Verified truth | Evidence |
|---|---|---|
| "`feature/phase-g`, 29 commits, unmerged — merge it" | **The branch does not exist**, locally or on `origin`. `git for-each-ref` finds no `phase-g` ref; `git ls-remote --heads origin` finds none. Phase G **G-1 is merged to `origin/main`** (26 commits match `--grep 'G-1|Phase G'`; `fd6309ee Merge branch 'feature/phase-g-followups' into main`). | `git branch -a --list '*phase-g*'` → empty; `git cat-file -e origin/main:src/featuregen/materialize/compile/chain.py` → present |
| "G-2 starts with item A.36" | **A.36's chain/lane row is CLOSED** (2026-08-04, `b8de4157 test(materialize): run the bridged chain path instead of inferring it (A.36)`). Only its second row — a triggered run asking for a `SANDBOX`-scoped realization via `execution_tier` — is open, and that is not where G-2 starts. | `docs/DEFERRED-WORK.md:804` |
| "ingress prefixes missing" | **Both ingress lists carry `/materialization-runs`.** `b300d388 fix(deploy): proxy /materialization-runs in both ingress lists`. | `frontend/vite.config.ts:27`; `deploy/kind/nginx.conf:16` |
| "terminal is `PUBLICATION_REFUSED(CAPABILITY_UNPROVEN)`, flag default OFF" | **True, and it is *deliberately* true.** `compile/chain.py`'s docstring calls it "THE TRUTHFUL TERMINAL … not a bug to be worked around". `MATERIALIZATION_FLAG = "FEATUREGEN_MATERIALIZE_ENABLED"`, default OFF, one public reader. | `chain.py:16-21`; `queue_lane.py:211,537` |

Two further precision points, because tasks below depend on them:

- **`PUBLICATION_REFUSED` and `CAPABILITY_UNPROVEN` are two different enums.** The first is a
  `RunEventKind` (`control_plane.py:119`), terminal, folding to `RunStatus.REFUSED`. The second is a
  `PublicationRefusalCode` (`codes.py:100`) returned by `select_publisher` and carried into the
  event's `detail` as `"CAPABILITY_UNPROVEN: <detail>"`. The Phase G plan itself conflated them once;
  `tests/featuregen/materialize/test_publish.py:269` records the correction. No task here may
  pattern-match one for the other.
- **`PUBLICATION_REFUSED` is G-1's SUCCESS terminal**, not a failure: it leaves the request
  `lifecycle_state='committed'`, because the evidence is on the plane. The failure terminal is
  `RUN_FAILED` / `failed`. Any status surface (D4) that renders "refused" as an error is lying.

**What this changes:** weld (3) is not "merge a branch". It is *"finish G-2 and G-3, which were
designed and left unbuilt on purpose"* — a smaller, better-defined job with a live landmine
(§0.4, D0) that the merge framing would have hidden.

**The 34 commits, for the record.** `feature/phase-g` (29 commits, `f3424c36..d2c6e43b`, fast-forward
merged) plus `feature/phase-g-followups` (5 commits, merged at `fd6309ee`) — the followups branch is
what closed A.36's chain/lane row and the ingress prefixes, and is the reason two of the three stale
premises above are stale. `origin/main` is 520 commits past the followups merge; `queue_lane.py`,
`request_store.py`, `reconcile.py`, `resolve.py`, `compile/wiring.py`, `authoring_trace.py` and
`api/routes/materialization_runs.py` are **byte-identical** to what Phase G shipped, while
`joins.py` (504 lines), `validation.py` (355) and `nodes_compute.py` (208) have moved under it.

### 0.1 What is ALREADY BUILT (and is better than the brief assumed)

**The whole render/compile/seal/prove machine is on main and is exercised by real execution, not
string matching.**

- `src/featuregen/materialize/` — 27 modules. Package invariant, from its own `__init__`: *"This
  package is render-only. It never imports `pyspark`; it emits the Kedro/PySpark project that does
  the computing."*
- The chain: `compile_feature_group(conn, *, request_id, work_item_ids, inventory,
  spine_declaration, cadence, availability_promise, mechanism, published_schema, assemble_nodes,
  project_root, l0, clock, contract_overrides=None, execution_tier=ExecutionTier.PRODUCTION) ->
  CompiledGroup` (`materialize/compile/chain.py:356`). Stages, in order, as `ChainStage`:
  `RESOLVE → ADMIT → COMPILE → AUTHORIZE → PHYSICAL_TYPE → CONTRACT → BIND → SPINE_INPUT →
  PUBLISHER → VALIDATE_L0`.
- The renderer: `render_project(authorized, plan, *, environment_id, engine_versions, spine_input,
  nodes, publisher_selection=None) -> SealedProject` (`materialize/render/project.py`),
  `RENDERER_VERSION = "3"`, `COMPILER_VERSION = "2"`. Emits a complete 15-file Kedro project plus
  `GENERATED.lock`. Two rendering tests that matter here:
  `test_rendering_the_same_compilation_twice_is_BYTE_identical` and
  `test_the_ORDER_the_features_compiled_in_does_not_change_the_bytes`.
- Real execution proof already exists at three levels: `fake_spark.py` (49 KB honest stand-in;
  tests inspect **rows**), `l0_gate.py` (a real interpreter, `make l0-gate`, run against kedro
  0.19.9/pyspark 3.5.1 *and* kedro 1.5.0/pyspark 4.2.0), and a committed golden project at
  `tests/featuregen/materialize/goldens/cif_daily/`.
- The trigger surface exists: `POST /materialization-runs` + `GET /materialization-runs/{id}`
  (`api/routes/materialization_runs.py`, registered at `api/app.py:263`), flag-gated,
  `require_confirmer`-gated, and provably a producer — a test reads its AST and asserts it never
  names `compile_feature_group`. The worker lane is `materialize/queue_lane.py`
  (`process_materialization_once`, `MATERIALIZATION_HANDLER = "materialization.compile.v1"`).
- G-2's three functions all **exist as functions**: `runprep.prepare_run` (`runprep.py:831`),
  `validation.run_l1` (`validation.py:1084`), `submit.LocalClusterSubmitter.submit`
  (`submit.py:169`). They are simply not called by the chain.
- The Formula-v2 **grammar** is complete: `schema_v2.py` (`TypedFormulaProposalV2`,
  `AggregateFunctionV2`, `WindowPolicyV2`, `AuthorityRefsV2`, `CompositeBodyV2`,
  `validate_semantics_v2`), `parse_v2.parse_versioned`, `canonical_v2.proposal_content_hash_v2`,
  `operations_v2.operation_rule`, `output_authority_v2.resolve_output_v2`,
  `capability_v2.classify_formula_capability_v2`. 36 reviewed `gold_v2` fixtures.
- The formula-shadow capture seam is fresh and working (E4 follow-up):
  `contract/gate1._engine_recipe_contexts` builds ONE `RecipeGroundingContextV1` per served recipe
  **at its leading variant** via `recipe_grounding_context.build_v2_recipe_grounding_context(
  candidate, *, catalog_source, logical_ref_by_object_ref)`, and
  `recipe_formula_shadow.capture_ranked_shadow(...)` turns each into an immutable
  `recipe_formula_shadow_work_item` + a transactional outbox pointer.
- **The bridge from that work item to the compiler already exists.**
  `materialize/resolve.resolve_feature_inputs(conn, *, work_item_ids) -> tuple[ResolvedFeature, ...]`
  reads `recipe_formula_shadow_work_item` rows and reconstructs the
  `ResolvedFeatureInput(intent, result)` that `admit_artifacts` consumes. This is the seam. It is
  built. Nothing has ever driven material through it end to end.

### 0.2 What is MISSING — the four welds, precisely located

**Weld 1 — there is no Formula-v2 AUTHORING path.**

- 317 V2 recipes: **295 `FORMULA_BLOCKED`, 19 `CONCEPTUAL_ONLY`, 3 `FORMULA_AUTHORABLE`.**
  (Measured, not assumed: `Counter(r.readiness for r in V2_RECIPES)`.) The brief's
  "FORMULA_BLOCKED everywhere" is 93% true; the three exceptions are load-bearing.
- The three authorable: `merchant_mcc_diversity` (formula-v1), `obligor_facility_count`
  (formula-v1), `posted_debit_amount` (formula-v2). **296 of 317 recipes declare
  `formula_schema_version="formula-v2"`; only 2 declare v1.**
- `run_authoring(conn, intent, author_client, critic_client, *, roles, actor) -> AuthoringResult`
  (`formula/authoring.py:266`) is **V1-only** end to end: it imports `parse_proposal_v1`,
  `classify_formula_capability` (v1), `resolve_formula_output_policy` (v1), and folds
  `TypedFormulaProposalV1`. There is no `run_authoring_v2`, no v2 critic path, no v2 disposition
  fold.
- `bind_formula_expectation(context: RecipeGroundingContextV1, blueprint:
  RecipeFormulaExpectationBlueprintV1) -> BoundRecipeFormulaExpectationV1`
  (`recipe_formula_contracts.py:201`) accepts only the **v1** blueprint shape.
- `RECIPE_FORMULA_V2_EXPECTATIONS` (`recipe_formula_expectations_v2.py:20`) holds **exactly one
  entry** — `"posted_debit_amount" -> ("30_posted_debit_amount_exemplar.json", "093de7a0…")` — and
  it is a *fixture pin*, not a bindable blueprint. Nothing consumes it except
  `has_reviewed_expectation` and a registry pin test.
- The shadow **only captures v1**:
  `capture_ranked_shadow(..., capture_recipe_ids=frozenset(RECIPE_FORMULA_EXPECTATIONS))`
  (`recipe_formula_shadow.py:1077`) and `blueprint = RECIPE_FORMULA_EXPECTATIONS.get(entry.recipe_id)`
  (`:923`). `posted_debit_amount` — the one *v2*-authorable recipe — is never captured. The capture
  reason literal is `"SELECTED_FORMULA_V1_AUTHORABLE"` / `"RECIPE_NOT_FORMULA_V1_AUTHORABLE"`.
- Of the two recipes it *does* capture, one is refused by its own blueprint:
  `merchant_mcc_diversity` yields `technical_axis="FORMULA_SOURCE_ENTITY_ROLE_UNRESOLVED"` because
  the reviewed v1 blueprint has grain role `merchant` while the V2 recipe computes per **customer**
  (`tests/featuregen/api/test_contract_ranked.py:511-557`). **This is an OPEN GOVERNANCE DECISION,
  not a bug this plan resolves** — see §1 D-7.
- **Latent defect, verified:** `semantic_option_decision.decision_facts_for_candidate` calls
  `has_reviewed_expectation(candidate.recipe_id)` (`:59`), passing a **recipe id** where an
  **expectation ref** belongs. `suggestion_contract.py:1639` does it correctly. Task A0.
  **Measured while executing A0 (correcting this bullet as authored):** the divergence is not a
  probe-only curiosity — **295 of the 298 formula-bearing V2 recipes declare a ref that is not
  their id** (`balance_slope` → `retail:balance_slope`), and 19 recipes carry no formula at all.
  The three that agree are exactly the three registry anchors, which is why the defect stayed
  invisible. The bite is forward-looking: every expectation A5 registers under a *pack-qualified*
  ref would have been unreachable through the frozen fact. The probe recipe does **not**
  discriminate the two behaviours (its id is unregistered too, so both readings answer `False`) —
  A0's acceptance uses a real registry recipe instead.

**Weld 2 — the frozen plan envelope never reaches compilation.**

- The envelope is real and complete. `recipe_planning_lens.fold_frozen_binding_plan(request,
  verdicts, story, pit_text, temporal_blocker, catalog_source, uoa_entity=None) ->
  (plan | None, refusal_codes)` (`:211`) returns exactly:
  `{"plan_kind": "single_source", "catalog_source", "source_table", "population_ref",
  "read_set": [...], "role_bindings": {role: ref}, "pit", "output_grain", "window"}`.
- It is frozen onto the decision record — but only inside the `dataset_story` **jsonb**, keyed
  `binding_plan` (`semantic_option_decision.py:73`), with its hash in the decision manifest
  (`canonical_hash(candidate.binding_plan)`, `:137`). `load_frozen_option_facts` reaches into that
  jsonb by string path for `read_set` and `catalog_source` (`:238-240`). The record's own comment
  says so: *"rides the story jsonb until D1 gives the record real columns."*
- Compilation receives **none of it**. `resolve_feature_inputs` rebuilds an `AuthoringIntent(name,
  hypothesis, target_entity, target_grain_keys, recipe_authoring_context)` from the work item's
  `provider_input_json` (`recipe_formula_worker.py:343-349`) and an `AuthoringResult` replayed from
  the trace. `compile_ir` then **re-derives** the source relation, the PIT gate and the read set
  from the formula's own refs plus live governed catalog facts. The governed plan the human was
  shown and the plan that would execute are two independent derivations that nothing compares.

**Weld 3 — G-2 and G-3 are unbuilt (see §0.0 for what that is *not*).**

- `chain.py:10-14`: *"this is G-1: stages 1 through SEAL, plus §11.2's L0. `prepare_run`, `run_l1`
  and `submit` are G-2, which is designed but not approved; publication is G-3 and does not exist
  at all."*
- The terminal: `select_publisher(conn, *, environment_id, engine_versions, mechanism, group_plan,
  published_schema)` (`publish.py:552`) reads `publication_capability_attestation` (migration
  **1034**) and returns `MaterializationRefused(PublicationRefusalCode.CAPABILITY_UNPROVEN, …)`
  when no attestation exists. `record_attestation(conn, probe_result)` and
  `assess_probe_observations(observations, *, probe_id, environment_id, mechanism,
  engine_versions, completed_at)` both exist. **`probe_publication_capability` — the live driver —
  is named at `publish.py:326` and absent from the repository.**
- Migration **1055 is reserved for the G-3 active-revision pointer** by name
  (`1053_materialization_request.sql` header: *"1055 for the active-revision pointer (§3.5)"*).
  The gap in the migration directory is a reservation, not an error.

**Weld 4 — no engine capability is registered anywhere.**

- `classify_formula_capability_v2(proposal, engine=None) -> "ok" | "unsupported_capability" |
  "unsupported_engine"` (`capability_v2.py:45`) and `EngineCapabilityV1(engine_id,
  supported_aggregations, supports_window_offset=False, supports_future_horizon=False)` (`:31`)
  have **zero production callers**. `grep -rn "classify_formula_capability_v2\|EngineCapabilityV1"
  src/` returns only the definitions, `formula/__init__.py`'s re-export, and two docstring
  mentions.
- `assemble_current_activation_state` (`semantic_option_decision.py:260`) therefore hardwires
  `formula_schema_supported=False` (`:363`) and `requirements_closed=False` (`:364`), with the
  honest comment: *"Effective readiness is the FROZEN readiness until a re-fold exists (C-phase)…
  materialization stays blocked regardless."*
- `recipe_readiness.fold_readiness(ReadinessInputsV1)` — the pure ladder that *would* answer this —
  is called in exactly two places, `suggestion_contract.py:1640` and `taxonomy/coverage.py:80`.
  **It is not called on the semantic engine's serving path at all**; `V2RecipeCandidateV1.readiness`
  carries the *authored literal* from the registry.
  **Closed by A6 (2026-08-14):** the lens now folds, both former callers route through the one
  `fold_definition_readiness`, and the authored literal is checked against the fold at import.
  What remains for C3 is the *durable-write* re-fold in `assemble_current_activation_state`,
  whose `effective_readiness` is still `frozen.readiness`.

### 0.3 The four activation codes that must flip

`activation_policy._materialization_blockers` (`:170-207`) is the gate. Its five materialization-only
rules, verbatim, with what each needs:

| code (`semantic_eligibility_reasons.py`) | rule | flips when |
|---|---|---|
| `READINESS_NOT_MATERIALIZATION_READY` (`:68`) | `current.effective_readiness != "MATERIALIZATION_READY"` | C3 makes effective readiness a **fold** (`fold_readiness`) instead of the frozen literal, and A6 supplies its five inputs |
| `FORMULA_NOT_REVIEWED` (`:66`) | `not frozen.has_reviewed_formula_expectation` | A5 registers a reviewed v2 expectation for the recipe **and** A0 fixes the ref/id confusion so it is asked correctly |
| `FORMULA_SCHEMA_UNSUPPORTED` (`:67`) | `not current.formula_schema_supported` | C1+C2 register an `EngineCapabilityV1` for the Kedro/PySpark engine and fold `classify_formula_capability_v2(proposal, engine)` into the current state |
| `EXECUTION_AUTHORITY_UNEVALUATED` (`:70`) / `EXECUTION_AUTHORITY_UNMET` (`:71`) | `not current.execution_authority_evaluated` / `not current.execution_floor_met` | **Already wired** — `assemble_current_activation_state:310-337` evaluates C2's `clears(authority, "execution_at_governed")` over `frozen.read_set`. It fails today only because a read set requires a plan, and B1 makes that plan a first-class column. No new mechanism; B1 makes it *reliable*. |

Also on the ladder above them, and therefore also required:
`EXTERNAL_VALIDATION_OUTSTANDING` (needs `current.requirements_closed`, hardwired `False`, task C3)
and every `_contract_blockers` rule, which the parent plan's Phase A/B already closed.

### 0.4 The landmine — read this before touching capability

`compile/chain.py:506-516`:

```python
selection = select_publisher(conn, environment_id=inventory.environment_id, ...)
if isinstance(selection, PublisherSelection):
    raise PublishStepMissing(...)
```

`PublishStepMissing`'s own docstring (`chain.py:186-197`) states the trigger plainly: *"`record_attestation`
is callable today, so ingesting one passing probe result for the target environment flips every run
of this chain from the truthful `CAPABILITY_UNPROVEN` terminal into this state."*

**Recording a passing publication attestation before G-3's publish step exists converts every
materialization run from a typed refusal into an unhandled `RuntimeError`.** Task D0 exists solely
to make that impossible to do by accident.

---

## 0.5 The bar this plan must clear

**One governed feature contract materializes end to end through Kedro on the kind cluster, and
publication is no longer refused.**

Concretely, and each item is a test or an operator-verifiable artifact:

1. A recipe candidate served by the semantic engine carries a **reviewed Formula-v2 expectation**,
   and `has_reviewed_formula_expectation` is `true` on its frozen option decision.
2. The formula-shadow capture produces a work item for that candidate at `variant_primary`, and the
   authoring worker drives it to a `RESOLVED` `AuthoringResult` over the **v2** grammar.
3. `activation_decision(frozen, current, "execute_materialization")` returns `allowed=True` — all
   four §0.3 codes clear, on a real DB row, in a test.
4. `POST /materialization-runs` accepts, the worker lane compiles, and the chain reaches
   `run_l0` **passed** — carrying the **frozen plan envelope**, with a test proving the compiled
   read set equals `binding_plan["read_set"]` and the compiled PIT equals `binding_plan["pit"]`.
5. G-2 runs: `prepare_run` → `run_l1` → `submit` executes the rendered pipeline.
6. G-3 publishes: a capability attestation from a **real probe** against the kind environment, the
   publish step, the active-revision pointer (migration 1055), terminal event `PUBLISHED`.
7. The feature is a **queryable object**: `sandbox_feature.<group>` returns rows on the kind
   cluster, and `GET /materialization-runs/{id}` reports it.

**The completion claim this plan may make when done:** *"one governed contract materializes end to
end through Kedro on kind; publication is proven for that environment at those engine versions."*
Never *"materialization is production-ready"* — §1 D-9 lists exactly what stays deferred.

**Honest interim bar (the A+B+C milestone, ≈14 days):** *"a governed contract compiles to a
build-verified Kedro project carrying its own frozen plan, and the UI can say why publication is
still unproven."* That is a real, demonstrable milestone and it is where the plan should be
reviewed before D is started.

---

## 1. Design decisions (stated once, so no task re-litigates them)

**D-1. The recipe definition IS the expectation blueprint; nothing new is authored by hand.**
A `RecipeDefinitionV2` already declares operands with roles, concepts and operand classes; a
`TemporalSpecV2` with basis/unit/parameter/inclusivity; an `OutputSpecV2` with additivity, unit and
null/empty policy; `EligibilitySpecV2.policy_refs`; and `output_grain` / `source_grain`. Those are
precisely the fields `RecipeFormulaExpectationBlueprintV1` carries by hand for two recipes. **The v2
blueprint is DERIVED from the definition by a pure function**, so 296 recipes gain a blueprint from
work already reviewed under BR-2 — and re-keying a grain (the `merchant_mcc_diversity` problem)
becomes structurally impossible, because the grain comes from the definition that also produced the
candidate. Hand-authoring 296 blueprints is the alternative and it is not one.

**D-2. Review is a governance ACT on a derived blueprint, not a second authoring.** A derived
blueprint is *proposed*; `RECIPE_FORMULA_V2_EXPECTATIONS` membership is *reviewed*. The existing
`recipe_review_event` store (migrations 1060/1061, `POST /recipes/{id}/reviews`) is the surface —
this plan adds no review UI. The registry stays a **code constant with a hash pin**, in the same
no-self-refresh freeze the v1 manifest carries, because a reviewed expectation that a DB write could
change is not frozen.

**D-3. The v2 authoring orchestrator is a SIBLING of `run_authoring`, never an edit of it.**
`formula/authoring.py`'s four invariants (output authority only from C1; every result built by
`derive_disposition`; technical outcomes never fabricate; `unsupported != invalid`) are pinned by
`test_authoring.py` including AST assertions. `run_authoring_v2` restates those invariants over the
v2 types in `formula/authoring_v2.py`. **The v1 path is not touched, not deprecated, and not
deleted by this plan** — two recipes and a large test corpus depend on it.

**D-4. The frozen plan envelope is CONSUMED, never re-derived.** Compilation must not compute a
second answer to "which table, which columns, which point-in-time clause, which window, which
population". It receives the frozen envelope and *validates against it*: a divergence is a typed
refusal (`PLAN_ENVELOPE_DIVERGENCE`), never a substitution — the same law
`fold_frozen_binding_plan` already applies to itself with `BINDING_PLAN_DIVERGENCE`, and the same
law `govern.py` applies at the governing write.

**D-5. Two capabilities, two mechanisms, never conflated.** *Formula-schema capability* is
`EngineCapabilityV1` + `classify_formula_capability_v2` — a **code-side registry**, no migration,
because it describes what the renderer can emit and the renderer is code. *Publication capability*
is `publication_capability_attestation` (1034) + a live probe — a **database record**, because it
describes what a cluster demonstrably does. `activation_policy.formula_schema_supported` is the
first; `PublicationRefusalCode.CAPABILITY_UNPROVEN` is the second. A plan that fused them would let
a code constant claim a cluster property.

**D-6. No new env flags.** `FEATUREGEN_MATERIALIZE_ENABLED` (default OFF) is the one switch and it
already exists with one public reader (`queue_lane.materialization_enabled`). The standing
pre-live steer (2026-08-11) applies: nothing here gets a second lever.

**D-7. `merchant_mcc_diversity`'s grain mismatch is an OPEN GOVERNANCE DECISION this plan
REFERENCES and does not resolve.** The reviewed v1 blueprint is merchant-grain; the V2 recipe is
customer-grain; `bind_formula_expectation` correctly refuses. `test_contract_ranked.py:526` states
the rule: *"Re-keying a REVIEWED expectation to a different grain entity is a governance act, not a
follow-up fix."* This plan's tasks must not silently re-key it, must not delete the failing test,
and must keep the refusal observable. When A2 derives a v2 blueprint for it from the *definition*,
that derived blueprint is customer-grain and **the v1 registry entry stays as it is** — the two
coexist and the v1 one goes on refusing until a human decides. A5's acceptance names this explicitly.

**D-8. Migrations.** Highest applied is **1065**; **1055 is a live reservation** for G-3's
active-revision pointer and must be used for exactly that. This plan reserves, now, by name:
**1066** `semantic_option_decision.binding_plan` (the envelope as a real column, task B1);
**1067** `materialization_request.considered_revision_id + option_id` (the governed provenance link,
task B4); **1068** `recipe_formula_shadow_work_item.binding_plan_json` (the envelope on the work
item, task B2 — **added at execution; see B2's acceptance row for why the original list was short
by one**); **1055** `feature_active_revision` (G-3, task D3). Nothing else. All deploy
backend-first, under the standing explicit-approval rule.

**D-9. What stays deferred, honestly.** Multi-feature groups beyond one member (the group machinery
exists; nothing maps a logical group to its members — `materialization_runs.py` says so and takes
the membership from the caller). **A.36's second row only** — the bridged chain/lane path is closed
and tested (five cases in `test_chain.py`, two in `test_materialization_e2e.py`, loading the
realization from the database), but neither `MaterializationJobV1` nor `MaterializationRunIn`
carries an `execution_tier`, so every HTTP-driven run compiles at `PRODUCTION`; A.36 frames closing
that as *a governance decision about who may widen the joins a compile may read*, argued the way
`published_schema` was — a declared field with no default. Not this plan's.
Content-addressed input snapshots (DEFERRED-WORK A, needs Iceberg).
Backfill/restatement. Scheduled cadence. The joined-dimension PIT gap (DEFERRED-WORK `:300` — a
**compiler** gap: nothing on the IR states an availability for traversed tables, so a multi-hop
feature must not be used for training data). A.24's transform-in-two-places, reassigned to 16b.
Deferral is honest only while the UI marks the capability unavailable — the parent plan's D-2
(`allowed_actions` / `blocked_actions`) guarantees that and this plan inherits it.

**D-10. Anthropic billing is exhausted.** Every task below that needs a provider call is marked
**⟨LLM⟩** and is blocked on billing being restored. Each such task ships its non-LLM half — the
seam, the types, the fixtures, the replay path — behind a recorded fixture, so the LLM half is a
single verification run rather than the task. `3219a209 fix(llm): a billing 400 is never a schema
diagnosis` is the precedent: a billing failure must never be recorded as a capability verdict.

---

## 2. Phase A — the Formula-v2 authoring path *(weld 1)*

*Closes: `FORMULA_NOT_REVIEWED`, and supplies `fold_readiness`'s `reviewed_expectation`,
`grammar_verdict` and `gold_validated` inputs.*

### Task A0 — hygiene, and the expectation-ref confusion (½ day)

The defect verified in §0.2: `decision_facts_for_candidate` asks
`has_reviewed_expectation(candidate.recipe_id)` where the contract is an **expectation ref**.

**Modify:** `src/featuregen/overlay/upload/semantic_option_decision.py` (`:59`) — resolve the
definition via `v2_recipe_by_id(candidate.recipe_id)` and ask
`has_reviewed_expectation(definition.formula.expectation_ref)`, with `False` for a definition
without a `formula` (governed-model-output and conceptual recipes have none).
`src/featuregen/overlay/upload/recipe_formula_expectations_v2.py` — docstring states the key is an
**expectation ref**, never a recipe id.

**Acceptance (tests):**
- `test_a_recipe_whose_expectation_ref_differs_from_its_id_is_recognised_by_its_ref` — a candidate
  over a real registry recipe whose ref is pack-qualified (`balance_slope` →
  `retail:balance_slope`), with the registry monkeypatched to hold that **ref**: today's
  id-keyed read answers `False`, the fixed ref-keyed read answers `True`.
- `test_a_registry_entry_spelling_the_recipe_id_is_not_a_reviewed_expectation` — the converse and
  the sharper half: an entry keyed by *id* names no expectation, so it must not flip the fact.
  Today's code answers `True` here. (These two replace the `PROBE_RECIPE` case as authored: the
  probe's id is unregistered too, so it cannot tell the two readings apart.)
- `test_a_recipe_with_no_formula_reference_is_not_reviewed` — a `conceptual_pattern` recipe.
- Full backend suite + `pytest -m eval` green (the eval marker joins the gate here, per §8).

> **ACCEPTED `df418c10` (2026-08-14).** `semantic_option_decision.has_reviewed_formula_expectation(
> recipe_id)` — a named public seam, not an inline expression, because A6 needs the same resolution
> on the serving path: resolve the definition via `v2_recipe_by_id`, ask the registry with
> `definition.formula.expectation_ref`, and answer `False` for a candidate the registry never
> minted (LLM intents, user definitions) or a definition with no `formula` (conceptual patterns,
> governed model outputs). `recipe_formula_expectations_v2`'s docstring now states the key law.
> **Measured, and it corrects §0.2:** 295 of the 298 formula-bearing recipes declare a ref that is
> not their id; only the three registry anchors agree, which is exactly why the wrong ask went
> unnoticed. **Plan defect found and fixed above:** the authored `PROBE_RECIPE` acceptance case
> does not discriminate (both readings answer `False` for it); the two `balance_slope` cases do,
> in both directions, and were proved to fail against the pre-fix expression before landing.
> Behaviour on today's registry is unchanged — a no-monkeypatch test pins that all three anchors
> still answer `True`. 5 new tests in
> `tests/featuregen/overlay/upload/test_semantic_option_decision_facts.py`.
> Gates: full suite **10946 passed, 20 skipped** (baseline on `fa7bce3f` was 10941/20 green);
> `-m eval` **73 passed**; ruff clean on all three touched files.

### Task A1 — the v2 expectation contract (2 days)

**New:** `src/featuregen/overlay/upload/recipe_formula_contracts_v2.py`.

- `RecipeFormulaExpectationBlueprintV2` — the v2 mirror of the v1 blueprint, over v2 vocabulary:
  `recipe_id`, `expectation_ref`, `final_operation: FinalOperationV2`, `expressions: tuple[
  ExpressionRoleExpectationV2, ...]` (each carrying `expression_path`, `aggregation:
  AggregateFunctionV2`, `operand_role`, `source_relation_role`, `window: WindowPolicyExpectationV2`
  with `offset_periods` and `basis: WindowBasisV2`, and `authority_refs: AuthorityRefsV2` role
  names), `grain: GrainExpectationV2(entity, key_roles)`,
  `semantic_parameter_projections`, `body_shape` (unary / ratio / diff / composite).
- `bind_formula_expectation_v2(context: RecipeGroundingContextV1, blueprint:
  RecipeFormulaExpectationBlueprintV2) -> BoundRecipeFormulaExpectationV2` — the preflight
  refusals the v1 binder raises, restated: `RECIPE_EXPECTATION_MISMATCH`,
  `RECIPE_DEFINITION_HASH_MISMATCH`, `SEMANTIC_PARAMETER_HASH_MISMATCH`,
  `FORMULA_SOURCE_ENTITY_ROLE_UNRESOLVED`, and duplicate-role. Raises
  `RecipeFormulaPreflightError` with the same closed codes, so the shadow's `technical_axis` needs
  no new vocabulary. **Corrected at execution:** "the exact five" undercounts — the v1 binder
  raises **ten** distinct codes (the five above plus `FORMULA_BINDING_MISSING`,
  `FORMULA_BINDING_SOURCE_MISMATCH`, `FORMULA_BINDING_SHAPE_INVALID`,
  `FORMULA_AUTHORING_UNSUPPORTED`, `SEMANTIC_PARAMETER_PROJECTION_INCOMPLETE`,
  `SEMANTIC_WINDOW_INVALID`). The v2 binder restates **all** of them; restating five and
  inventing behaviour for the rest is what "the same closed vocabulary" forbids.
- `validate_blueprint_v2(blueprint)` — construction-time law, mirroring `validate_blueprint`.

**Acceptance (tests):** `tests/featuregen/overlay/upload/test_recipe_formula_contracts_v2.py`
- `test_binding_a_v2_expectation_produces_exact_refs_for_every_role`
- each preflight refusal asserted by code, one test each
- `test_a_v2_blueprint_whose_grain_role_is_not_bound_is_refused` — the D-7 property, in the v2 binder
- `test_the_bound_expectation_is_hash_stable_over_role_order`

> **ACCEPTED `24a31734` (2026-08-14).** `recipe_formula_contracts_v2.py` — 6 new types
> (`WindowPolicyExpectationV2` with `offset_periods`, `ExpressionRoleExpectationV2` with
> `second_operand_role` / `aggregation_argument` / `authority_refs` / term name+sign,
> `GrainExpectationV2`, `RecipeFormulaExpectationBlueprintV2`, and the two bound mirrors),
> `validate_blueprint_v2`, `bind_formula_expectation_v2`, `expectation_content_hash_v2`. v1 is
> untouched; the version-neutral leaves (`RecipeFormulaPreflightError`,
> `SemanticParameterProjectionV1/Kind`, `DecimalPolicyExpectationV1`, `_plain`) are imported
> verbatim, the same law `schema_v2` states for the grammar.
> **Deviations from the task as authored, each deliberate:**
> (a) **ten preflight codes, not five** — corrected above; the v1 binder's whole vocabulary is
> restated, because a v2 situation the v1 binder names must not get a new word.
> (b) **`body_shape` is a derived property, not a field.** The task lists both `final_operation`
> and `body_shape`; `FinalOperationV2` already *is* that vocabulary (identity/ratio/difference/
> signed_sum), and two stored fields could disagree. `BODY_SHAPE_BY_FINAL_OPERATION` maps one to
> the other and `EXPRESSION_PATHS_BY_FINAL_OPERATION` pins each shape's canonical AST paths, so a
> blueprint cannot declare a ratio and then carry `body.expr`.
> (c) **`authority_refs` carries governed policy REFS, not role names.** The task says "role
> names"; `AuthorityRefsV2`'s four fields are policy identifiers (`policy:eligible-posted-status`)
> that bind to nothing physical and are identity-bearing exactly as authored. A role name in that
> slot would not validate.
> (d) The blueprint carries `expectation_ref` **and** `recipe_id` (A0's lesson: they are different
> keys), and `allocation_policy_ref` for increment 8's source→output rollup.
> 21 tests: the whole preflight vocabulary one case each, the D-7 grain-role refusal in the v2
> binder, hash stability under reversed binding order, and the two v2-only forks (a bound
> `second_operand_ref`, an order-sensitive aggregate refused on a `FUTURE_HORIZON` window).
> **Nothing calls this module yet** — A2 derives blueprints into it, A4 binds them at capture.
> Gates: full suite **10967 passed, 20 skipped**; `-m eval` **73 passed**; ruff + mypy clean.

### Task A2 — derive the blueprint from the definition (2 days)

The D-1 decision, implemented.

**New:** `src/featuregen/overlay/upload/recipe_formula_blueprint_derivation.py` —
`derive_blueprint_v2(definition: RecipeDefinitionV2) -> RecipeFormulaExpectationBlueprintV2 |
BlueprintDerivationRefusal`. Pure. Reads only the definition. Maps:
`definition.temporal` → the window policy (basis, unit, parameter, inclusivity — the same fields
`recipe_temporal_v2.compile_temporal` reads); `definition.operands` → expression operand roles by
`operand_class` (`measure` → the aggregated operand, `entity_key` → grain key, `event_timestamp` →
`event_time_role`, `direction`/`status` → authority refs); `definition.output_grain` →
`grain.entity`; `definition.output.additivity`/`empty_population_policy`/`null_input_policy` → the
window's `empty_window` and `null_input`; `definition.formula.result_class` → `final_operation`.

**Refuses (never guesses)** with named codes: `MULTIPLE_MEASURE_OPERANDS_UNRESOLVED` (the body shape
is not derivable from a single measure), `NO_MEASURE_OPERAND`, `AGGREGATION_UNDECLARED`,
`TEMPORAL_BLOCKED`. A refusal is a recipe that keeps `FORMULA_BLOCKED` **with a named blocker** —
exactly the readiness ladder's contract. **Corrected at execution: eleven codes, not four** —
the seven added (`NOT_A_DETERMINISTIC_FORMULA`, `WINDOW_NOT_EVENT_ANCHORED`,
`WINDOW_UNIT_UNSUPPORTED`, `GRAIN_KEY_UNRESOLVED`, `OUTPUT_POLICY_UNDERIVABLE`,
`PARAMETER_PROJECTION_UNDERIVABLE`, `AUTHORITY_REFS_AMBIGUOUS`) each name a distinct way the
definition fails to determine the blueprint, and the largest of them accounts for a third of the
registry. See the acceptance row.

**Acceptance (tests):** `tests/featuregen/overlay/upload/test_blueprint_derivation.py`
- `test_the_exemplar_derives_to_the_reviewed_gold_fixture_shape` — `posted_debit_amount`'s derived
  blueprint, bound against a seeded catalog, produces a proposal that **parses under
  `parse_versioned` as formula-v2 and matches `30_posted_debit_amount_exemplar.json`'s structure**
  (the fixture is the oracle; this is the test that proves derivation is not invention).
- `test_derivation_is_total_over_the_registry_or_refuses_by_name` — over all 317 `V2_RECIPES`,
  every recipe either derives or carries one of the four refusal codes. Pin the **counts** so a
  registry edit that silently loses derivability fails CI.
- `test_a_derived_blueprint_takes_its_grain_from_the_definition` — the D-7 property as structure:
  derive for `merchant_mcc_diversity` and assert `grain.entity == "customer"`, i.e. the derived
  blueprint cannot reproduce the v1 registry's merchant-grain mismatch.
- `test_derivation_reads_nothing_but_the_definition` — no `conn` parameter, asserted over the
  signature (the same shape `test_activation_policy` uses for the pure fold).

> **Expected honest outcome to record in the acceptance row:** the derivable count will be well
> below 296. That number IS the deliverable of this task — it is the first true measurement of how
> many banking recipes are executable-shaped, and every refusal code is a named piece of registry
> work, not a mystery.

> **ACCEPTED `0f124251` (2026-08-14).** `recipe_formula_blueprint_derivation.py` —
> `derive_blueprint_v2(definition)`, pure (one parameter, asserted over the signature), plus
> `derive_registry_blueprints()` for the sweep.
>
> **THE MEASUREMENT: 90 of 317 recipes derive a blueprint.** Every other recipe carries exactly
> one named blocker: `WINDOW_NOT_EVENT_ANCHORED` 102 · `MULTIPLE_MEASURE_OPERANDS_UNRESOLVED` 65 ·
> `NOT_A_DETERMINISTIC_FORMULA` 19 · `AGGREGATION_UNDECLARED` 19 · `OUTPUT_POLICY_UNDERIVABLE` 6 ·
> `WINDOW_UNIT_UNSUPPORTED` 6 · `NO_MEASURE_OPERAND` 4 · `TEMPORAL_BLOCKED` 3 ·
> `PARAMETER_PROJECTION_UNDERIVABLE` 2 · `GRAIN_KEY_UNRESOLVED` 1. The counts are pinned in
> `EXPECTED_OUTCOMES`. The single largest blocker is **not** formula grammar: 102 recipes are
> as-of / effective-interval / contractual-future anchored and `WindowPolicyV2` has no shape for
> a snapshot read at the cutoff. That is the next real piece of grammar work, and it was
> invisible before this task.
>
> **Deviations from the task as authored, each deliberate and each measured:**
> (a) **eleven refusal codes, not four.** The four authored ones are all present; the other seven
> exist because the definition genuinely fails to determine the blueprint in seven *distinct*
> ways, and collapsing them would hand a reviewer "underivable" with no action attached — the
> opposite of "every refusal code is a named piece of registry work". Ten fire on the shipped
> registry; `AUTHORITY_REFS_AMBIGUOUS` fires only by construction today and is kept so the
> derivation can never silently pick one of two governed policies of the same kind.
> (b) **three fields have no structural source anywhere in the registry and are DECLARED
> constants of the derivation, stated once in the module docstring:** `timezone_policy` is empty
> for all 317 recipes (→ `UTC`, what the reviewed v1 blueprints declare); `scale_policy` is empty
> for all 317 (→ precision 38, scale 0 for counts / 6 otherwise, matching the v1 blueprints and
> the gold exemplar); and the window inclusivity convention is taken from `compile_temporal`'s own
> compiled PIT text, `(cutoff − L, cutoff]`.
> (c) **the authored acceptance test `test_the_exemplar_derives_to_the_reviewed_gold_fixture_shape`
> is not executable as written at A2** — it asks for a *proposal* that `parse_versioned` accepts,
> and nothing renders a blueprint into a proposal until A3. The test ships as the same oracle at
> the level A2 actually produces: every field the blueprint and the reviewed proposal both carry
> is asserted equal, field by field.
>
> **Three disagreements between the derivation and the reviewed gold fixture, recorded not
> fudged** (each pinned by an assertion so it cannot drift unnoticed):
> 1. **timezone** — derived `UTC`, fixture `Asia/Dubai`. No registry source exists; the reviewer's
>    to set.
> 2. **window inclusivity** — derived `(start, end]` from the recipe's own compiled PIT text,
>    fixture `[start, end)`. **The reviewed gold corpus and the recipe registry disagree about
>    which end of a trailing window is closed.** This is a governance question, not a bug to pick
>    a side on, and it is exactly the kind of thing the fixture-as-oracle test exists to surface.
> 3. **policy-ref namespace** — the gold corpus writes `policy:eligible-posted-status`, the
>    registry writes `eligible_status:foundation-posted-events`. The *set* of governed policies
>    agrees (all four declared on both sides); only the spelling differs.
>
> **D-7 holds structurally:** `merchant_mcc_diversity` derives `grain.entity == "customer"` from
> its own definition, while the reviewed v1 registry entry still says `merchant` and still
> refuses. Both are asserted in one test; no task re-keyed anything.
>
> 12 tests in `tests/featuregen/overlay/upload/test_blueprint_derivation.py`, including
> `test_a_derived_blueprint_binds_against_a_grounded_context` — derivation and A1's binder proved
> to be one path end to end (blueprint → bound refs → `bank::public.txns.txn_amt`, 90-day window).
> Gates: full suite **10979 passed, 20 skipped**; `-m eval` **73 passed**; ruff + mypy clean.

### Task A3 — the v2 authoring orchestrator ⟨LLM⟩ (3 days)

**New:** `src/featuregen/formula/authoring_v2.py` —
`run_authoring_v2(conn, intent: AuthoringIntent, author_client: LLMClient, critic_client:
LLMClient, *, roles, actor) -> AuthoringResult`, the v2 sibling per D-3. Chain:
`open_authoring_run` → `author_formula` (v2 instruction + `proposal_v2.schema.json`) →
`parse_versioned` → `validate_semantics_v2` → `classify_formula_capability_v2(proposal, engine=None)`
(grammar arm only; the engine arm is C1's) → `resolve_output_v2` over C1 facts → `critique` →
`derive_disposition` → terminal event.

**Modify:** `src/featuregen/formula/author.py` — a v2 `AUTHOR_INSTRUCTION_V2` / `AUTHOR_PROMPT_ID_V2`
beside the v1 pair (the frozen configuration hashes both, so they must be distinct constants).
`src/featuregen/overlay/upload/recipe_formula_worker.py` — select the orchestrator by the bound
expectation's schema version; v1 work items keep `run_authoring` byte-for-byte.

**Acceptance (tests):**
- `test_run_authoring_v2_never_constructs_AuthoringResult` — the AST assertion `test_authoring.py`
  already makes for v1, restated (D-3's invariant 2).
- `test_a_v2_proposal_outside_the_grammar_is_UNSUPPORTED_not_REJECTED` (invariant 4).
- `test_a_provider_failure_is_technical_and_carries_no_formula` (invariant 3) — **and specifically,
  a 400 with a billing body is `technical_failure`, never a capability verdict** (D-10, the
  `3219a209` precedent).
- `test_the_v1_orchestrator_is_byte_identical` — v1 golden traces unchanged.
- **⟨LLM⟩ deferred half:** one live run against the exemplar, recorded as an acceptance row with
  its `llm_call` refs. Until billing is restored the suite drives A3 through a recorded-fixture
  client, and the task is marked *shipped, unverified against a live provider*.

> **ACCEPTED `0e876764` (2026-08-14). SHIPPED, UNVERIFIED AGAINST A LIVE PROVIDER.** Four new
> modules — `formula/authoring_v2.py` (`run_authoring_v2`, the v2 sibling; the v1 orchestrator is
> not touched, not deprecated, not deleted), `formula/result_v2.py`, `formula/turns_v2.py` — plus
> `AUTHOR_INSTRUCTION_V2` / `AUTHOR_PROMPT_ID_V2` and the `AuthorTurnContract` value type in
> `author.py`. 27 tests in `tests/featuregen/formula/test_authoring_v2.py`.
>
> **⟨LLM⟩ THE LIVE HALF IS DEFERRED, HONESTLY.** Anthropic billing is exhausted (D-10), so not one
> provider call in this task was real: every run is a `FakeLLM` recorded fixture. What is proven is
> the SEAM — stage order, axis mapping, artifact coherence, trace discipline, audit identity. What
> is NOT proven is that a real model, given `AUTHOR_INSTRUCTION_V2` and held to
> `proposal_v2.schema.json`, emits a usable v2 proposal at all. The instruction text is
> **unevaluated prose** until one live run says otherwise, and no measurement here should be read
> as evidence about it. `test_a_billing_refusal_is_technical_never_a_capability_or_schema_verdict`
> is what makes the deferral safe rather than convenient: a `PROVIDER_NON_RETRYABLE` refusal folds
> to `TECHNICAL_FAILURE` with `capability_status="ok"` and `structural_status="ok"` — a payment
> problem can never be written down as a durable statement about the v2 grammar (the `3219a209`
> precedent).
>
> **FOUR PLAN DEFECTS FOUND AND FIXED IN THIS COMMIT — the first is the material one:**
>
> 1. **`formula/authoring.py`'s `run_authoring` has NO production caller, and §0.2 presents it as
>    the v1 path.** The LIVE authoring worker imports `run_authoring` from
>    `formula/replay_authoring.py` (`recipe_formula_worker.py:35`) — a *different*, 684-line
>    orchestrator carrying checkpoint/replay, `frozen_configuration`, `proposal_validator`,
>    `tool_runner`, `authoring_run_id`, `facts_reader`, `critic_metadata_loader`,
>    `progress_callback` and `lease_fence`. The codebase already records this at
>    `materialize/authoring_trace.py:11-12` (*"`formula.authoring.run_authoring`, which no
>    production code path invokes"*); the plan did not. **Consequence:** A3's *"Modify
>    `recipe_formula_worker.py` — select the orchestrator by the bound expectation's schema
>    version"* is not a small edit and is **NOT DONE HERE**. Routing a v2 work item through the live
>    worker needs a *replay-shaped* v2 orchestrator plus v2 siblings of
>    `recipe_authoring.recipe_expectation_validator`, `recipe_authoring.recipe_tool_runner` and
>    `FrozenRecipeReadContext.formula_facts` (which returns v1 `ExprFacts` keyed by body path, not
>    `OperandFactsV2` keyed by ref). That is its own task; A4's acceptance row records what the
>    absence means for a captured v2 work item.
> 2. **`-> AuthoringResult` is not achievable and `AuthoringResultV2` is not symmetry.** v1 fuses
>    the authored structure and its resolved policy into `TypedFormulaV1`, which is why
>    `derive_disposition` can demand one for a resolved output. **BR-6 never minted a
>    `TypedFormulaV2`**: `resolve_output_v2` returns a `FormulaOutputPolicyV2` *beside* the
>    proposal. So the v2 artifact IS the pair, and `derive_disposition_v2` restates the coherence
>    law accordingly — a resolved output requires BOTH halves, an unresolved one carries the
>    proposal and forbids the policy. The §F PRECEDENCE is not re-decided: `_fold_v2` is v1's fold
>    verbatim and a test pins the two equal over all 432 axis combinations.
> 3. **The chain's `→ critique →` step could not run.** `critic._proposal_plain` raised
>    `SchemaError` on anything that was not a `TypedFormulaProposalV1`. Widened ADDITIVELY (the type
>    gate still admits only parsed, semantically-validated proposals — now two of them) rather than
>    forked: a critic finding says *"this operand is not what the intent asked for"*, which is a
>    statement about the CATALOG, and the closed §G code set has no version in it.
> 4. **Two smaller interface corrections.** `validate_semantics_v2` is not a separate stage —
>    `parse_proposal_v2` already calls it (`parse_v2.py:158`). And `resolve_output_v2`'s fact bundle
>    is keyed by operand **logical_ref**, not by v1's internal body path; a bundle keyed v1's way
>    resolves every operand to empty facts and assembles a policy out of nothing.
>
> **Two things the code says that the plan did not anticipate, both kept:**
> (a) `turns_v2` relaxes `final_operation` on the wire as well as `aggregation`, so an unknown
> COMBINER now genuinely reaches the `unsupported_operation` arm — in v1 that arm was unreachable
> through a provider because the wire pinned the const. (b) The v2 wire schema pins
> `formula_schema_version` to 2, so a v1-declared body can never arrive on a v2 run: it fails
> response validation and the run ends TECHNICAL ("the loop never got a v2 proposal"), never a
> false verdict about the grammar. `_parse_v2`'s version guard stays as defence in depth for a
> non-provider caller; both halves are asserted.
>
> **The schema-registry gate did its job and is recorded, not silenced:**
> `test_llm_schema_inventory.py::test_every_requested_schema_pair_resolves` failed the moment
> `formula_author_turn_v2` became statically requestable, because its registration hook only ran
> the v1 contract. Fixed by registering the v2 contract in the same hook — the pair is now
> genuinely registered, not exempted.
>
> Gates: full suite **11006 passed, 20 skipped**; `-m eval` **73 passed**; ruff clean on all seven
> touched files; mypy clean on the three new modules.

### Task A4 — the shadow captures v2 recipes (1 day)

**Modify:** `src/featuregen/overlay/upload/recipe_formula_shadow.py`
- `capture_ranked_shadow(..., capture_recipe_ids=…)` — the capture population becomes *every recipe
  with a bindable blueprint* (v1 registry ∪ v2-derivable), not `frozenset(RECIPE_FORMULA_EXPECTATIONS)`
  (`:1077`).
- `_capture_selected_entry` (`:923`) — resolve the blueprint by schema version: v1 registry lookup,
  else `derive_blueprint_v2`; bind with the matching binder.
- The two capture-reason literals lose their `_V1`: `SELECTED_FORMULA_AUTHORABLE` /
  `RECIPE_NOT_FORMULA_AUTHORABLE`. **These strings are in `capture_entries` jsonb on
  `recipe_formula_shadow_run_manifest`, which is hash-sealed** (`write_manifest` → `manifest_hash`);
  changing them changes the manifest hash for new runs only (existing rows are never rewritten —
  the store is append-only and `_checked_existing` compares stored to expected). Assert that
  explicitly rather than discovering it.
- `MAX_RECIPE_FORMULA_CAPTURES_PER_RUN = 12` stays; a wider population makes `BUDGET_TRUNCATED`
  reachable, which is the honest outcome and is already an observation axis.

**Acceptance (tests):** extend `tests/featuregen/api/test_contract_ranked.py`
- `test_the_v2_exemplar_recipe_reaches_a_work_item` — `posted_debit_amount` served → an EXACT
  candidate → a `recipe_formula_shadow_work_item` row + one outbox pointer. **This is the first
  time the v2 path produces durable authoring input.**
- `test_the_merchant_grain_disagreement_is_still_named` — the D-7 test at `:511` continues to pass
  unchanged for the v1 blueprint path.
- `test_a_wider_population_truncates_at_the_budget_and_says_so` — `BUDGET_TRUNCATED` observations.

> **TASK CORRECTED, NOT YET EXECUTED (2026-08-14, while landing A3).** A4 is not a one-day task and
> its acceptance test cannot pass as authored, for a reason the plan does not mention. Every claim
> below has its file and line, and A4-a was **reproduced**, not inferred.
>
> **A4-a. The egress whitelist is fail-close and it is v1-SHAPED.**
> `recipe_egress._validate_formula_expectation` (`:234`) calls `_exact_keys` on each expression
> with exactly seven v1 keys (`expression_path, aggregation, operand_ref, source_relation_ref,
> event_time_ref, window_length, window`) and on each window with exactly nine. A1's
> `BoundExpressionExpectationV2` carries **twelve** (`second_operand_ref`, `aggregation_argument`,
> `authority_refs`, `term_name`, `term_sign` on top), and its window carries `offset_periods`.
> `build_recipe_authoring_egress` (`:353`) is annotated
> `expectation: BoundRecipeFormulaExpectationV1` and projects six keys — which the v2 bound type
> does happen to carry, so the failure is not a `KeyError` but the exact-key gate. Built one and
> ran it: `RecipeEgressViolation: expressions[0] keys differ: missing=[], unknown=
> ['aggregation_argument', 'authority_refs', 'second_operand_ref', 'term_name', 'term_sign']`.
> **Consequence:
> binding a v2 blueprint at capture produces a payload the gate REFUSES, so
> `_capture_selected_entry` takes its `RecipeEgressViolation` arm (`:970`) and writes an
> observation with `delivery_axis="EGRESS_REJECTED"` and NO work item.**
> `test_the_v2_exemplar_recipe_reaches_a_work_item` therefore cannot pass until the egress contract
> has a v2 arm. That arm is a **governed-security change** — the whitelist is the fail-close
> boundary to a provider — so it is its own increment: a version-dispatched
> `_validate_formula_expectation`, the v1 shape asserted byte-identical, and every new v2 key given
> a real bound (`authority_refs` are policy identifiers → bounded text; `offset_periods` bounded;
> `term_sign` ∈ {1,−1}; `second_operand_ref` validated as a ref). Not "assert the manifest hash
> changed".
>
> **A4-b. A captured v2 work item would be authored by the V1 orchestrator.** Per A3's acceptance
> row defect 1, the live worker calls `replay_authoring.run_authoring` and A3 did not (could not)
> wire the v2 sibling into it. So the moment the population widens, a v2 work item is claimed by a
> worker that parses it with `parse_proposal_v1`, validates it with the v1
> `recipe_expectation_validator`, and would record `invalid_formula → REJECTED` — **a dishonest
> durable verdict about a recipe the platform simply cannot author yet.** A4 must therefore either
> land the replay-shaped v2 orchestrator first, or gate the worker on the work item's declared
> expectation schema version with a named, non-authoring terminal. The second is small; it is not
> optional, and it is not in the task as authored.
>
> **A4-c. The population selector.** `derive_registry_blueprints()` (A2) is the sweep;
> `v2_recipe_by_id(recipe_id).formula_schema_version` is the `"formula-v1" | "formula-v2"` literal
> (`recipe_contract_v2.py:246`) A4 should switch on. Measured: on the shipped registry, "declares
> formula-v1" and "has a v1 registry entry" pick out the same two recipes, so the two readings
> agree today — key on the declared version (the plan's words) and pin the agreement, so a future
> registry edit that separates them fails CI instead of silently changing which binder runs.
>
> Everything else in the task as authored is correct and was confirmed: the population line is at
> `recipe_formula_shadow.py:1077`, the blueprint lookup at `:923`, the two reason literals at
> `:471`/`:475`, `MAX_RECIPE_FORMULA_CAPTURES_PER_RUN = 12` at `:50`, and the manifest-hash
> reasoning is exactly right — `write_manifest` (`:481`) hashes `capture_entries` into
> `manifest_hash`, `ON CONFLICT (manifest_id) DO NOTHING` plus `_checked_existing` (`:109`) means
> existing rows are never rewritten, so the change lands on new runs only.

> **A4 INCREMENT 1 — THE EGRESS v2 ARM. ACCEPTED `162fb706` (2026-08-14).** The governed-security
> half, landed on its own because widening the fail-close boundary to a provider is not a
> side-effect of a capture change. `_validate_formula_expectation` is now a **dispatcher**: the v1
> arm and the v2 arm are separate functions and the payload's own declaration chooses.
>
> **The A4-a repro was reproduced first and now passes.** Before:
> `RecipeEgressViolation: expressions[0] keys differ: missing=[], unknown=['aggregation_argument',
> 'authority_refs', 'second_operand_ref', 'term_name', 'term_sign']`. After: a derived,
> bound `posted_debit_amount` expectation crosses the gate and carries
> `formula_schema_version="formula-v2"`.
>
> **v1 byte-identity is MEASURED, not asserted.** HEAD's module was loaded from git beside the new
> one and both were run over 33 payloads (the reviewed merchant payload plus 32 mutations, one per
> v1 bound): **31 outcomes identical, message for message.** The only two differences are payloads
> that *declare* a schema version — a case that could not exist before this commit, and both are
> still refused, now by name. Two digests are pinned in `test_recipe_egress.py`: the canonical v1
> provider payload (`09ce6764…`, which is also its `content_hash`) and the frozen v1 arm's own
> source (`c063aad8…`). Live work items were sealed against exactly those bytes.
>
> **Deviations from the increment as briefed, each deliberate:**
> (a) **The v1 arm is the UNDECLARED shape, and that is the fail-close reading — not a hole.**
> "Fail-close on anything undeclared" cannot mean "refuse the v1 shape": the worker re-validates
> each work item's *stored* `provider_input_json` before every dispatch
> (`recipe_formula_worker.py:301`), and those bytes are sealed into `provider_input_hash` /
> `payload_hash`. A key added to v1 would refuse every work item already on the queue. So absence
> IS the v1 declaration; the v1 arm exact-keys six/seven/nine keys and refuses everything else;
> and any *present* declaration other than `"formula-v2"` — including `null`, `""`, `2` and
> `"formula-v1"` — is a named refusal. Five cases assert it.
> (b) **`authority_refs` is a bounded OBJECT of four named policy identifiers, not a list.** The
> brief said "bounded list"; `AuthorityRefsV2` is a four-field dataclass
> (`status_policy_ref` / `direction_policy_ref` / `reversal_policy_ref` /
> `currency_conversion_ref`). The arm exact-keys those four, bounds each to 128 chars, **character-
> classes each against `[A-Za-z0-9][A-Za-z0-9_.:+-]*`** — an authority ref is a key, never prose,
> and prose is what leaks — and restates the schema's non-vacuity law (a block with four blanks is
> refused, not forwarded).
> (c) **"validated against the closed grammar vocabulary" was applied to `aggregation`, which is
> the key that has one.** `aggregation_argument` is a number; what makes it real is the rule table:
> `aggregation` must be an `AggregateFunctionV2` member, and `operation_rule` then decides whether
> the argument is required (percentile, strictly inside (0,100)) or forbidden, whether an operand
> is required, and whether a second operand is required/forbidden. Three keys are bounded by one
> closed table instead of by three hand-written guesses.
> (d) **`term_sign ∈ {1,−1}` holds only inside a signed sum.** The bound type defaults
> `term_name=""` / `term_sign=0` for every other shape, so the arm asserts the coherence:
> ±1 with a non-blank name inside a `signed_sum` (and at least two terms), exactly `""`/`0`
> outside one. A name or a sign on a unary body is refused.
> (e) **The v2 arm closes nine vocabularies v1 only bounded as text** (`final_operation`,
> `aggregation`, `basis`, `unit`, both inclusivities, `empty_window`, `null_input`,
> `decimal.rounding`, `decimal.overflow`). The v1 arm is frozen, so this strictness lands only
> where it is new. The token vocabularies are read from the grammar (our own authored words); the
> *bounds* — 4 expressions, 16 grain refs, 100 000 window length, 12 offset periods — are stated
> in `recipe_egress` itself, so a grammar that widens can never silently widen the boundary.
> `test_the_egress_offset_bound_is_pinned_against_the_grammar` is where the two are reconciled.
> (f) **The v2 projection stays as narrow as v1's**, plus the declaration: `expectation_ref`,
> `recipe_candidate_key`, `blueprint_content_hash`, `semantic_parameter_binding_hash` and
> `allocation_policy_ref` are server-private and asserted absent. A provider authors a formula; it
> does not audit our registry.
>
> **One forward gap recorded, not fixed here:** `recipe_formula_worker._formula_refs` (`:84`)
> collects `operand_ref` and `event_time_ref` only, so a v2 expression's `second_operand_ref`
> would never reach `FrozenRecipeReadContext.load`. Harmless today — increment 2 stops every v2
> work item before that line — but the eventual replay-shaped v2 orchestrator must widen it.
>
> 42 new cases in `tests/featuregen/formula/test_recipe_egress.py` (51 total in the file).
> Gates: full suite **11048 passed, 20 skipped** (baseline on `3e875ad3` was 11006/20);
> `-m eval` **73 passed**; ruff + mypy clean on the touched files.

> **A4 INCREMENT 2 — THE WORKER SCHEMA-VERSION GATE. ACCEPTED `10f25ddc` (2026-08-14).** Small,
> mandatory, and landed BEFORE the population widens, because the order is the point: the moment a
> v2 work item can exist, the live worker must already refuse to author it.
>
> `recipe_formula_worker` now reads `declared_expectation_schema(row)` from the work item's frozen
> `provider_input_json` and terminalizes anything that is not `formula-v1` **before any other
> evaluation**, with `technical_axis="V2_AUTHORING_UNAVAILABLE"` (or `EXPECTATION_SCHEMA_UNKNOWN`
> for a declaration this build has never heard of) and every other axis `NOT_EVALUATED` /
> `NOT_DISPATCHED` / `NOT_RUN` — the shape the integrity terminal already uses for "we stopped
> before evaluating anything".
>
> **Why `authoring_axis="NOT_RUN"` and not `UNSUPPORTED`:** `UNSUPPORTED` is a *capability verdict
> about a proposal* (A3's D-3 invariant 4), and here no proposal exists and no provider was asked.
> The observation says the PLATFORM cannot author v2 yet; it says nothing whatsoever about the
> recipe. That is the whole reason this gate exists — without it, A4-b's failure mode is a durable
> `invalid_formula → REJECTED` against a recipe nobody ever tried to author.
>
> **The declaration is read from the provider input, not from a new column** (D-8 reserves 1055 /
> 1066 / 1067 and nothing else): increment 1 made `formula_schema_version` a validated, bounded
> field of the payload, so the work item already carries its own generation. Absence is `formula-v1`
> — every work item written before A4 is exactly the undeclared shape, and a test proves an
> undeclared item still reaches the orchestrator.
>
> **Deviation:** two codes, not one. `V2_AUTHORING_UNAVAILABLE` is honest only for a v2
> declaration; an unknown declaration gets `EXPECTATION_SCHEMA_UNKNOWN` rather than being told it
> is v2. Both are `technical_axis` free text, the same slot `AUDIT_STORE_UNAVAILABLE` and
> `FROZEN_CONFIGURATION_INVALID` already use — no migration, no CHECK-constraint change.
>
> 3 new cases in `tests/featuregen/overlay/upload/test_recipe_formula_worker.py`; the gate is
> proved with the *real* downstream path in place (no stub could have hidden its absence — the v1
> orchestrator is monkeypatched to RAISE if it is ever reached).
> Gates: full suite **11051 passed, 20 skipped**; `-m eval` **73 passed**; ruff + mypy clean.

> **A4 INCREMENT 3 — THE CAPTURE WIDENING (A4 PROPER). ACCEPTED `06e935d1` (2026-08-14).**
>
> **THE HEADLINE, AND IT IS REAL:** `posted_debit_amount` — the one `formula-v2` recipe the
> registry calls `FORMULA_AUTHORABLE` — is served by the semantic engine, resolves to an EXACT
> candidate, derives its blueprint from its own definition (A2), binds it with the v2 binder (A1),
> crosses the whitelist's new v2 arm (increment 1) and lands as a durable
> `recipe_formula_shadow_work_item` row with its outbox pointer. **This is the first time the v2
> path has produced durable authoring input.** `test_the_v2_exemplar_recipe_reaches_a_work_item`
> asserts the bound refs, not merely a row: grain `account` →
> `posting_bank::public.txns.acct_id`, operand → `…txn_amt`, clock → `…event_ts`, aggregation
> `sum`, and the payload declaring `formula_schema_version="formula-v2"`.
>
> **The population** is `formula_capturable_recipe_ids()` — every recipe with a bindable
> blueprint — resolved per recipe by `capture_blueprint_for(recipe_id)`, which switches on
> `v2_recipe_by_id(recipe_id).formula.formula_schema_version` (A4-c, the declared literal).
> **90 of 317**, exactly A2's derivable count, re-pinned from the capture side. `None` — never a
> guess — for an unregistered id (LLM intents, user definitions), a recipe with no formula, a
> `formula-v1` recipe with no reviewed entry, or a definition A2 refuses to derive from.
>
> **A4-c's agreement is pinned, in both directions:** `test_the_two_readings_of_formula_v1_still_agree`
> asserts `{declares formula-v1} == set(RECIPE_FORMULA_EXPECTATIONS) == {merchant_mcc_diversity,
> obligor_facility_count}`. A third v1-declaring recipe, or a v1 entry for a v2-declaring one,
> fails CI instead of silently changing which binder runs on a customer's request.
>
> **D-7 is untouched and still observable.** `merchant_mcc_diversity` declares `formula-v1`, so it
> still resolves the REVIEWED merchant-grain entry (asserted by identity: `is` the registry
> object) and still refuses with `FORMULA_SOURCE_ENTITY_ROLE_UNRESOLVED`.
> `test_formula_shadow_reaches_the_reviewed_blueprint_and_names_its_disagreement` passes
> **unchanged, not one line edited** — the widened population did not disturb it, which was not
> guaranteed and was checked. The recipe's *derived* customer-grain blueprint exists (A2 proved
> it) and is deliberately NOT substituted: re-keying a reviewed expectation is the operator's act.
>
> **The reason literals lost their `_V1`** (`SELECTED_FORMULA_AUTHORABLE` /
> `RECIPE_NOT_FORMULA_AUTHORABLE`) and the consequence is asserted rather than discovered:
> `test_the_renamed_capture_reasons_change_new_manifests_only` writes a manifest, then re-writes
> the same `manifest_id` with the pre-A4 spelling, and proves (a) it raises `ShadowIntegrityError`
> and (b) the stored row — hash and `capture_entries` alike — is byte-identical afterwards. The
> store is append-only; `ON CONFLICT DO NOTHING` plus `_checked_existing` means new runs hash
> differently and old rows are never rewritten.
>
> **`MAX_RECIPE_FORMULA_CAPTURES_PER_RUN = 12` stays**, and a wider population makes
> `BUDGET_TRUNCATED` reachable for the first time: 15 selected capturable recipes produce 12
> capture attempts and 3 `BUDGET_TRUNCATED` / `CAPTURE_INCOMPLETE` observations, the run still
> reconciles `COMPLETE`, and nothing is enqueued for a truncated entry.
>
> **Deviations from the task as briefed:**
> (a) **The budget test lives in `test_recipe_formula_shadow.py`, not `test_contract_ranked.py`.**
> The rule it tests is in `_capture_selected_entry`, and reaching it through the API would mean
> building a catalog that grounds 13+ selected recipes — a fixture, not a proof, and one whose
> failure mode would be "the catalog changed" rather than "the budget broke". `initial_view_size`
> is 15 against a budget of 12, so the API path *can* truncate; the rule is proved where it lives.
> The other two acceptance tests are in `test_contract_ranked.py` as briefed.
> (b) **`capture_blueprint_for` and `formula_capturable_recipe_ids` are cached** (`functools`).
> `V2_RECIPES` and the v1 registry are code constants; the derivation sweep is 81 ms and would
> otherwise run per generation request.
>
> **Two forward gaps recorded, neither shipped as a defect** (both unreachable today because
> increment 2 stops every v2 work item at the worker door, and both must be closed by whoever
> builds the replay-shaped v2 orchestrator): `recipe_formula_worker._formula_refs` and
> `recipe_formula_authority.build_formula_authority_envelope` each collect `operand_ref` and
> `event_time_ref` only, so a v2 expression's `second_operand_ref` would reach neither the frozen
> read context nor the authority envelope. No v2 blueprint the derivation currently produces
> carries one (every derived expression is a single-operand identity body), so nothing is wrong
> in the shipped registry — but a `date_diff_avg` or `effective_at_cutoff` blueprint would need
> both widened first.
>
> 8 new cases (5 in `test_recipe_formula_shadow.py`, 1 in `test_contract_ranked.py` plus its
> catalog fixture, 2 parametrized). Gates: full suite **11059 passed, 20 skipped**;
> `-m eval` **73 passed**; ruff + mypy clean on the touched files.

### Task A5 — the reviewed-expectation seam (1 day)

**Modify:** `src/featuregen/overlay/upload/recipe_formula_expectations_v2.py` — grow
`RECIPE_FORMULA_V2_EXPECTATIONS` for the recipes whose derived blueprint has been reviewed, each
entry pinning a `gold_v2` fixture name + canonical sha256, under the existing
`validate_v2_expectation_registry` law. Start with `posted_debit_amount` (already pinned) plus
whichever recipes A2's derivation covers and a review event records.

**Modify:** `src/featuregen/overlay/upload/recipe_review.py` / the review store — record the
**derived blueprint hash** on the review event, so "reviewed at this revision" and "reviewed this
blueprint" are the same fact. No migration: `recipe_review_event` (1060) already carries a
revision hash; the blueprint hash is derived from the same definition.

**Acceptance (tests):**
- `test_the_v2_registry_pins_reviewed_fixtures` — the existing pin test, unchanged, still green
  with the larger registry.
- `test_a_registered_expectation_flips_has_reviewed_formula_expectation` — over a real
  `semantic_option_decision` row: the frozen fact is `true`, and `activation_decision(...,
  "execute_materialization")` **no longer carries `FORMULA_NOT_REVIEWED`** (it still carries the
  other three — this is the first of the four codes to fall, and the test asserts the others remain).
- `test_the_merchant_v1_entry_is_untouched` — D-7: the v1 registry entry for
  `merchant_mcc_diversity` still says `merchant`, still refuses, and no task moved it.

> **A5 — THE MECHANISM HALF. ACCEPTED `f0d208d8` (2026-08-14). THE REGISTRY-GROWTH HALF IS
> OPERATOR-GATED AND DELIBERATELY NOT DONE.**
>
> **`RECIPE_FORMULA_V2_EXPECTATIONS` DID NOT GROW, AND THAT IS THE FINDING.** Under D-2,
> membership here *is* review: an added entry flips `has_reviewed_formula_expectation`, which is
> the blocker this very task proves clears. So growing it is a governance act with an operator's
> name on it, not an engineering step — and the two things that would make it an engineering
> step are both absent. A2 derives a blueprint for 90 of the 317 recipes, but nothing in a
> derivation says a human reviewed one; and no `recipe_review_event` exists anywhere in this
> repository's fixtures or seeds (whether the live store holds any is an operator fact this
> branch cannot read). Choosing which of the 90 count as reviewed would have been an engineer
> inventing governance. `posted_debit_amount` stays the only entry, and
> `test_the_v2_registry_pins_reviewed_fixtures` now asserts the **exact set** so a later silent
> addition fails CI rather than quietly clearing a materialization gate.
>
> **What the operator must do to add one entry** (stated in the registry module's own docstring,
> so it travels with the code, and all four are required per entry):
> 1. an `approved` `recipe_review_event` from every role `required_reviewer_roles(recipe)` names,
>    at the recipe's **current** `canonical_recipe_v2_hash` — `POST /recipes/{id}/reviews` is the
>    surface, and each event now carries the blueprint hash the decision covers;
> 2. a reviewed `tests/featuregen/formula/gold_v2/` fixture for that expectation;
> 3. its canonical proposal sha256, pinned in the registry beside the fixture name;
> 4. green `validate_v2_expectation_registry()` **and** the fixture-side pin test in
>    `tests/featuregen/overlay/upload/recipes/test_transaction_foundation.py`, which parses the
>    named fixture under `parse_versioned`, requires `formula_schema_version == 2` and compares
>    `expected_proposal_hash` to the pin — editing either side alone fails CI.
>
> **The mechanism, landed.** `recipe_formula_shadow.capture_blueprint_hash(recipe_id)` sits
> beside `capture_blueprint_for` and resolves through it, so the hash a review records is the
> hash of the blueprint the **capture path would actually bind** — one resolution, never a
> second derivation. `CaptureBlueprintV1.content_hash()` dispatches by generation exactly as
> `.bind()` does. Measured today: `posted_debit_amount` → `0d843082…` (v2, derived),
> `merchant_mcc_diversity` → `5d93b5e7…` and `obligor_facility_count` → `97564d1c…` (v1,
> reviewed registry blueprints).
>
> **THREE PLAN DEFECTS FOUND AND CORRECTED:**
>
> 1. **The store needed no change at all; the ROUTE was the whole job, and it was recording
>    something else.** The task says *"Modify `recipe_review.py` / the review store"*.
>    `recipe_review_event.formula_expectation_hash` has existed since migration 1060 and
>    `record_review_event` has always accepted it — but its only writer,
>    `api/routes/recipe_review.record_decision`, was writing the **`gold_v2` fixture pin** from
>    `RECIPE_FORMULA_V2_EXPECTATIONS`, which is a code constant recoverable from the recipe id at
>    any time and is `None` for 316 of the 317 recipes. That is not a blueprint hash and it says
>    nothing about the decision. The swap is one line plus the resolver; the store change is
>    documentation.
> 2. **"the derived blueprint hash" is the wrong resolution for the two `formula-v1` recipes, and
>    taking it literally would have silently resolved D-7.** `merchant_mcc_diversity` derives a
>    *customer*-grain v2 blueprint while the expectation that actually governs it is the reviewed
>    *merchant*-grain v1 one. Recording the derived hash would have written down "this approval
>    covers the customer-grain shape" — the re-key the plan forbids, arriving through the back
>    door. `capture_blueprint_hash` resolves by the recipe's own declared
>    `formula_schema_version`, so a v1 recipe records its reviewed v1 blueprint and the
>    disagreement stays open and observable.
> 3. **`EXTERNAL_VALIDATION_OUTSTANDING` is not among "the other three" on this row, and the
>    reason is a rule, not a fixture accident.** `_materialization_blockers` short-circuits it on
>    `frozen.validation_status == "DESIGN_CHECKED"`, which is `FeatureIdea`'s default. The
>    acceptance test therefore asserts the **exact intersection** with the six
>    materialization-only codes rather than a subset: `{READINESS_NOT_MATERIALIZATION_READY,
>    FORMULA_SCHEMA_UNSUPPORTED, EXECUTION_AUTHORITY_UNEVALUATED}` for the exemplar, and the same
>    set **plus `FORMULA_NOT_REVIEWED`** for the discriminator. Exactly one code differs between
>    the two rows; a policy that had stopped blocking altogether could not pass both.
>
> **The seam is proved on a real DB row, not a constructed fact:**
> `test_a_registered_expectation_flips_has_reviewed_formula_expectation` freezes a real
> `semantic_option_decision` through `decision_facts_for_candidate` → `persist_option_decisions`,
> loads it through `load_frozen_option_facts`, assembles the current layer with
> `assemble_current_activation_state`, and asks `activation_decision(…,
> "execute_materialization")`. **`FORMULA_NOT_REVIEWED` is gone — the first of §0.3's four codes
> to fall.** `balance_slope` (derivable, unregistered, otherwise identical in the fixture) still
> carries it, which is what proves the test measures the registry and not the fixture.
>
> **D-7 untouched, asserted three ways:** the v1 registry entry still declares
> `grain.entity == "merchant"` while the definition's `output_grain` is `customer`;
> `capture_blueprint_for` still resolves the reviewed object **by identity** (`is`); and the
> route records the merchant-grain hash for it. Nothing re-keyed, nothing deleted.
>
> Also landed: `GET /recipes/{id}/reviews` now exposes `formula_expectation_hash` per event (it
> was written and never readable), and the review store's docstring states the column's meaning
> where it lives. The v1-pin-recording mutant was restored and run first — it fails
> `test_a_decision_records_the_blueprint_it_covers_not_the_fixture_pin` on the exact byte
> difference, so the route test discriminates.
>
> 8 new cases in `tests/featuregen/overlay/upload/test_reviewed_expectation_seam.py`, 2 in
> `tests/featuregen/api/routes/test_recipe_review_route.py`.
> Gates: full suite **11069 passed, 20 skipped** (baseline on `8cc50cad` was 11059/20);
> `-m eval` **73 passed**; ruff + mypy clean on the four touched source files.

### Task A6 — readiness folds instead of asserting (1½ days)

`V2RecipeCandidateV1.readiness` is the authored literal; `fold_readiness` is never called on the
serving path (§0.2).

**Modify:** `src/featuregen/overlay/upload/recipe_planning_lens.py` — the candidate's `readiness`
becomes `fold_readiness(ReadinessInputsV1(computation_kind=…, temporal_blockers=(temporal_blocker,)
if temporal_blocker else (), binding_blockers=…, reviewed_expectation=has_reviewed_expectation(
definition.formula.expectation_ref), grammar_verdict=…, gold_validated=…, engine_verdict=None,
governed_policy_blockers=…)).state`, with `.blockers` carried alongside.
`engine_verdict=None` until C1 — which is the ladder's *documented* honest resting point
(`FORMULA_VALIDATED`), not a fudge.

**Modify:** the authored `readiness=` field on `RecipeDefinitionV2` becomes an **assertion the fold
must not contradict**, checked at registry-validation time, rather than the answer. A definition
claiming `FORMULA_AUTHORABLE` that folds to `FORMULA_BLOCKED` is a `RecipeContractError` at import.

**Acceptance (tests):**
- `test_every_registry_recipe_folds_to_at_least_its_authored_readiness` — over all 317.
- `test_the_authored_literal_can_never_exceed_the_fold` — mutate a definition's `readiness` upward
  in a fixture and assert import fails.
- `test_readiness_moves_when_an_expectation_is_registered` — the monotonicity `fold_readiness`
  promises, observed through the serving path.
- Pin the **new** readiness distribution (the §0.2 counts will move; the number is evidence).

> **ACCEPTED `4510d08b` (2026-08-14).** `fold_readiness` is now called on the serving path, and
> the authored `readiness=` literal is an assertion the registry checks against it at import.
>
> **THE DISTRIBUTION DID NOT MOVE, AND THAT IS THE MEASUREMENT.** The task says *"the §0.2 counts
> will move; the number is evidence"*. Measured: **295 `FORMULA_BLOCKED` / 19 `CONCEPTUAL_ONLY` /
> 3 `FORMULA_AUTHORABLE` → 295 / 19 / 3.** Not merely the same totals — the same answer *recipe
> for recipe*, all 317, with zero disagreements (`test_the_registry_readiness_distribution_is_pinned`
> asserts the empty difference set, not just the counts). The registry has never drifted from its
> own declarations, which is a real result about 317 hand-authored definitions and is the
> opposite of what the plan expected. It also means the import-time law lands green rather than
> as a migration.
>
> **What IS new at registry level is the blocker vocabulary, which nothing served before:**
> `no_reviewed_formula_expectation` 295 · `model_feature_spec_owns_readiness` 8 (the governed
> model outputs — BR-7A owns their states) · `gold_evaluation_unproven` 3 (the anchors, resting
> at `FORMULA_AUTHORABLE`). Pinned in `EXPECTED_REGISTRY_BLOCKERS`.
>
> **The number that DOES move is the SERVED candidate's, and it moves per catalog.**
> `posted_debit_amount` is the one recipe the registry authors `FORMULA_AUTHORABLE`; against a
> catalog where not one operand binds it used to serve that literal anyway. It now serves
> `FORMULA_BLOCKED` with `REQUIRED_OPERAND_MISSING` — **the first time a BR-5 operand verdict has
> ever reached the readiness ladder.** That is the behaviour A6 bought, and it is asserted
> directly (`test_a_candidate_that_did_not_bind_is_no_longer_served_its_authored_literal`).
> Registering an expectation for a candidate whose operands still did not bind does **not** lift
> it — monotonicity clears one blocker, it does not promote.
>
> **The three inputs the task left as `…`, decided and justified — `fold_definition_readiness`
> states them ONCE so no surface can answer differently:**
> (a) **`grammar_verdict="ok"`.** BR-6's `classify_formula_capability_v2` classifies a
> *proposal*, and no proposal exists at serving time or at import. Reading A2's derivation as a
> grammar verdict instead was **measured**: identical distribution, because all three registry
> anchors derive — so it would have bought a third opinion and no new truth, and it would have
> mapped eleven derivation refusal codes onto one grammar code that means something narrower.
> C1/C2 is where a real verdict arrives.
> (b) **`gold_validated=False`, `engine_verdict=None`.** No gold or engine gate has run.
> `engine_verdict=None` rests the ladder at `FORMULA_VALIDATED`, the documented honest ceiling.
> (c) **`governed_policy_blockers=()`** on the lens: nothing on this path measures unresolved
> policy refs, and a fold input the caller cannot measure must stay empty rather than guess.
>
> **The refactor is the point, not a side effect.** `taxonomy.coverage.execution_readiness_of`
> and `suggestion_contract._fold_v2_definition` each hand-assembled their own `ReadinessInputsV1`
> — three call sites, three chances to diverge on questions no definition answers. Both now call
> `fold_definition_readiness`; `test_one_fold_answers_for_every_surface` asserts the coverage
> report and the fold agree for all 317, so a fourth opinion cannot appear quietly.
>
> **Deviations and judgements, each deliberate:**
> (a) **`exceeds_fold` treats `RETIRED` as a terminal, not a rung.** `READINESS_LADDER` ends with
> it, so a naive index comparison would read `RETIRED` as the *highest* state and let any
> definition claim it. It compares only to itself, and an unranked literal fails closed. No
> recipe is `RETIRED` today; the law is written for the first one that is.
> (b) **`BLOCKER_OPERAND_NOT_BOUND` — a fail-closed fallback with no live caller.** Every
> non-bound *required* path in today's binder attaches a BR-5 reason code, but the failure
> direction matters: a dropped blocker PROMOTES a candidate up the ladder. A required operand
> that did not bind always contributes a blocker, even if its verdict forgot to explain itself.
> (c) **`readiness_blockers` is carried on the candidate and NOT persisted.** `semantic_option_decision`
> has no column for it and D-8 reserves 1055/1066/1067 and nothing else; inventing a migration
> here would spend a reservation this plan already assigned. The frozen fact stays the folded
> `readiness` string, exactly as before.
> (d) **`llm_intent_candidates` still hardcodes `readiness="CONCEPTUAL_ONLY"` and was not
> touched.** There is no `RecipeDefinitionV2` behind an LLM intent to fold, and its docstring
> already calls that the *structural readiness ceiling*. Folding a planning request through a
> definition-shaped fold would be exactly the "claim a verdict nobody produced" this ladder
> forbids.
>
> **Both mutants were run before the tests were trusted:** reverting the lens to
> `readiness=recipe.readiness` fails the two serving-path cases; disabling the registry law fails
> `test_the_authored_literal_can_never_exceed_the_fold` with `DID NOT RAISE`.
>
> 10 new cases in `tests/featuregen/overlay/upload/test_readiness_folds_not_asserts.py`.
> Gates: full suite **11079 passed, 20 skipped** (11069/20 on `430140b2`); `-m eval` **73
> passed**; ruff clean on all touched files. **mypy honesty:** `recipe_planning_lens.py` and `suggestion_contract.py` carry **14
> pre-existing errors** (measured on `430140b2` by swapping HEAD's files back in: the identical
> 14, at shifted lines). This commit adds none and fixes none — they belong to
> `v2_applicability`'s optional scope fields and the LLM-intent branch's request/definition
> union, neither of which A6 touches. `recipe_readiness.py`, `recipe_registry_v2.py` and
> `taxonomy/coverage.py` are mypy clean.

---

## 3. Phase B — the frozen plan envelope reaches compilation *(weld 2)*

*Closes: `EXECUTION_AUTHORITY_UNEVALUATED` reliably; establishes D-4.*

### Task B1 — the envelope becomes a real column (1 day) — **migration 1066**

**New:** `src/featuregen/db/migrations/1066_semantic_option_decision_binding_plan.sql` — add
`binding_plan jsonb` and `binding_plan_hash text` to `semantic_option_decision`. Additive, nullable,
`IF NOT EXISTS`, no backfill of invented values: existing rows keep `NULL` and read through the
`dataset_story` path (the reader handles both, and a test proves the two agree on a row that has
both). The append-only guard on 1063 is unchanged — adding a column is not a rewrite.

**Modify:** `semantic_option_decision.py` — `persist_option_decisions` writes the column;
`load_frozen_option_facts` reads `binding_plan` from the column, falling back to
`dataset_story->'binding_plan'`; `decision_facts_for_candidate` keeps writing the story copy for one
release so a rollback reads correctly.

**Acceptance (tests):**
- `test_the_column_and_the_story_carry_the_same_plan` — byte equality on a fresh row.
- `test_a_legacy_row_without_the_column_still_loads_its_read_set` — the fallback.
- `test_the_plan_hash_matches_the_decision_manifest` — `canonical_hash(binding_plan)` equals
  `decision_manifest["binding_plan_hash"]`; a row where they differ is a load-time refusal.
- Migration audit: apply against a **populated** `semantic_option_decision` (the standing lesson —
  CI is blind to legacy data; repro against a seeded legacy shape).

> **ACCEPTED `31dd218f` (2026-08-14).** Migration **1066** adds `binding_plan jsonb` and
> `binding_plan_hash text` to `semantic_option_decision` — additive, nullable, `IF NOT EXISTS`, no
> default and no backfill. `decision_facts_for_candidate` computes the plan's identity ONCE and
> hands the same string to both the new column and `decision_manifest.binding_plan_hash`, so the
> seal and the sealed thing cannot be two answers; `persist_option_decisions` writes both;
> `load_frozen_option_facts` and the new public `frozen_binding_plan` read the column and fall
> back to `dataset_story->'binding_plan'`. The story copy is still written, for one release.
>
> **THE AUDIT FOUND SOMETHING, AND IT IS THE WHOLE REASON THE STANDING LESSON EXISTS.** The
> migration was mutated to do what a careless author would do — `UPDATE semantic_option_decision
> SET binding_plan = dataset_story->'binding_plan' WHERE binding_plan IS NULL` — and run against a
> POPULATED table. It does not merely violate the no-backfill rule: **1063's append-only trigger
> physically refuses it** (`semantic_option_decision is append-only: UPDATE is not allowed`). On a
> fresh CI database that migration passes, because there are no rows to update. Only
> `test_migration_1066_applies_to_a_POPULATED_legacy_table` — which drops the two columns, seeds
> rows in the exact pre-1066 shape and then runs the migration file's own SQL — can see it. The
> same test proves the converse the task asked for: the ALTER succeeds on a populated table, the
> append-only triggers are never reached (a nullable column with no default is a catalog-only
> change), every seeded row's pre-1066 bytes are digest-identical afterwards, the new columns are
> NULL, those rows still read through the story fallback, and an UPDATE is still refused after the
> ALTER.
>
> **Deviations and judgements, each deliberate:**
> (a) **The seal is checked on BOTH storage generations, not only the new column.** A legacy row's
> story copy is exactly as tamper-worthy as a new row's column, and a guard that only covered the
> new path would be opt-out for every row that predates it. A row written before migration 1065
> (`decision_manifest = '{}'`) carries no seal at all and is read rather than refused — there is
> nothing to compare against, and minting a hash for it here would seal a value this deployment
> computed rather than the one generation froze.
> (b) **The refusal is a typed `OptionDecisionIntegrityError`**, the same idiom
> `ShadowIntegrityError` uses, raised from the reader. The two `api/routes/contract.py` call sites
> are deliberately NOT given a 409 arm: nothing conflicts and no retry helps — an append-only row
> whose bytes disagree with its own seal means an out-of-band write, and dressing that up as a
> client-fixable conflict would send a caller looking for a problem they do not have.
> (c) **NULL, never `'{}'`.** 1065 could add `NOT NULL DEFAULT '{}'::jsonb` because an absent
> evidence record and an empty one mean the same thing; here `'{}'` would be a plan with no source
> table and an empty read set — a shape `fold_frozen_binding_plan` never returns and which the
> activation policy would read as "a plan exists and it authorizes nothing".
> (d) `load_option_decision_record` was left alone. It already returns `dataset_story`, so it
> already exposes the plan; adding the column would be a second copy of the same bytes on a wire
> projection with no reader for it.
>
> **Both mutants were run before the tests were trusted:** a story-only reader (the pre-B1
> expression) fails `test_the_column_is_the_source_once_the_story_copy_is_gone` and the tamper
> case; a reader that skips the seal fails both tamper cases with `DID NOT RAISE`.
>
> 12 tests in `tests/featuregen/overlay/upload/test_option_decision_binding_plan.py`.
> Gates: full suite **11090 passed, 20 skipped** (baseline on `b9979fd0` was 11079/20);
> `-m eval` **73 passed**; ruff clean on both touched files. **mypy honesty:**
> `semantic_option_decision.py` carries **1 pre-existing error** (`v2_recipe_by_id` returning
> `RecipeDefinitionV2 | None` at the review re-read) — measured on `b9979fd0` by swapping HEAD's
> file back in: the identical error, at a shifted line. This commit adds none and fixes none.

### Task B2 — the envelope rides the authoring intent (1 day)

`AuthoringIntent.recipe_authoring_context: dict[str, Any] | None` (`formula/turns.py:157`) already
exists and is already populated with the whole `provider_input`
(`recipe_formula_worker.py:348`). It is the carrier.

**Modify:** `src/featuregen/formula/recipe_egress.py` / `build_recipe_authoring_egress` — the
provider payload is **unchanged** (the envelope must not be sent to a provider; it is server-private
plan detail and the egress whitelist is fail-close). The envelope is added to the **work item's**
`provider_input_json`-adjacent material instead: `recipe_formula_shadow.write_work_item` gains a
`binding_plan` / `binding_plan_hash` pair, hashed into `_work_item_material` so
`verify_work_item_payload` covers it.

> **This changes `payload_hash` for new work items.** Existing rows are never rewritten
> (`ON CONFLICT DO NOTHING` + `_checked_existing`), so old rows verify against the old material and
> new ones against the new. A test must pin **both** shapes or replay of a pre-B2 work item breaks.

**Acceptance (tests):**
- `test_the_work_item_carries_the_frozen_plan_and_its_hash`
- `test_the_provider_payload_is_byte_identical_to_before` — the egress guarantee, asserted on bytes.
- `test_a_pre_B2_work_item_still_verifies` — the versioned-material property above.

> **ACCEPTED `bc7c8451` (2026-08-14).** The frozen plan envelope now rides the work item, sealed
> by its own hash and folded into `_work_item_material`, and it does **not** ride the provider
> payload.
>
> **THE PLAN DEFECT, AND IT IS A MIGRATION RESERVATION.** The task says the envelope is "added to
> the work item's `provider_input_json`-adjacent material" and hashed so `verify_work_item_payload`
> covers it. `recipe_formula_shadow_work_item` (migration 1023) **has no column it could ride in**,
> and none of its five jsonb columns may absorb it: `provider_input_json` is the payload the egress
> whitelist seals and this very task asserts byte-identical; `binding_envelope_json` is the formula
> AUTHORITY envelope, a different envelope with its own content hash; the other three are the bound
> expectation, the frozen configuration and the request identity. So D-8's list was short by one and
> **migration 1068** is it — additive, nullable, no default, no backfill (1023's write-once triggers
> refuse UPDATE in any case). D-8 and §8 are corrected in this commit.
>
> **The carrier, and what it deliberately is not.** `_engine_recipe_contexts` now returns a third
> map — plan by candidate key — filled from the candidate that folded it, carried on `ConsideredSet`
> **in memory only**, and handed to `capture_ranked_shadow`. It is NOT folded into
> `RecipeGroundingContextV1`, which is serialized into the considered revision and hashed into
> `considered_content_hash`: growing that type would move a governed identity for a value nothing on
> the wire reads. `_capture_selected_entry`'s new parameter is **required, not defaulted**, so a
> future call site cannot silently drop the envelope.
>
> **The two material shapes are pinned against a LITERAL, not against the implementation.**
> `_work_item_material` folds the two keys in only when a plan is present, and
> `_PRE_B2_MATERIAL_KEYS` writes out the 21 keys a pre-B2 row was sealed under. A work item written
> before B2 has NULL columns and therefore hashes the pre-B2 way, which is the whole compatibility
> claim: 1023 forbids rewriting those rows, so a material that folded the keys in unconditionally
> would terminalize every queued item with `WORK_ITEM_PAYLOAD_HASH_MISMATCH`. That mutant was built
> and run — it fails `test_the_two_material_shapes_are_pinned`. A second mutant (verify ignores the
> envelope) fails two cases, one of them on the exact code.
>
> **"Byte-identical to before" is proved three ways rather than asserted.** (1)
> `build_recipe_authoring_egress`'s signature is asserted to be exactly
> `{hypothesis, prediction_goal, expectation}` — no call site *could* pass a plan; (2)
> `recipe_egress.py` is untouched by this commit, so A4-increment-1's pinned v1 payload digest
> (`09ce6764…`) and the frozen-arm source digest (`c063aad8…`) remain the byte-level guard and stay
> green; (3) the stored `provider_input_json` is the caller's bytes unchanged, and no key of the
> envelope appears anywhere in it **at any depth** (the API-level test flattens the payload rather
> than checking the top level).
>
> **Honest limit, recorded:** the envelope's own hash is checked by `verify_work_item_payload`,
> which the *worker* calls. `materialize/resolve.py` (B3) reads the column directly and does not
> re-check it, because the only correct hasher lives in `overlay/upload/` and copying it into
> `materialize/` would be a second, drifting implementation of the seal. The consequence is bounded:
> the envelope is a CONSTRAINT on compilation, so a tampered one degrades the B3 check toward what
> compilation did before B3 — it can never widen what compilation reads, which is derived
> independently from governed catalog facts.
>
> 6 new cases in `tests/featuregen/overlay/upload/test_recipe_formula_shadow.py`, plus the
> end-to-end assertions on `test_the_v2_exemplar_recipe_reaches_a_work_item` — the served
> exemplar's plan (`single_source` / `posting_bank` / `txns` / `account`) is now on its work item.
> Gates: full suite **11096 passed, 20 skipped** (11090/20 on `b8325da2`); `-m eval` **73 passed**;
> ruff clean on all touched files. **mypy honesty:** the three touched source files carry **7
> pre-existing errors** (measured on `b8325da2` by swapping HEAD's files back in: the identical 7).
> This commit adds none — the first draft added one (`"object" has no attribute "binding_plan"` on
> the deliberately-untyped `leading` map) and it was fixed before the gate rather than accepted.

### Task B3 — compilation CONSUMES the envelope (2½ days)

The core of D-4.

**Modify:** `src/featuregen/materialize/resolve.py` — `ResolvedFeature` gains
`plan_envelope: Mapping[str, Any] | None` read from the work item; `resolve_feature_inputs` returns
it. It is *provenance*, not a second intent — the `intent_hash` proof is untouched.

**Modify:** `src/featuregen/materialize/admission.py` — `admit_artifacts` gains a seventh check:
when a `plan_envelope` is present, the admitted feature's grain entity must equal
`envelope["output_grain"]` and its source relation must equal `envelope["source_table"]`. A
mismatch is a new `CompilationRefusalCode.PLAN_ENVELOPE_DIVERGENCE`.

**Modify:** `src/featuregen/materialize/ir.py` — `compile_ir` accepts `plan_envelope` and
**validates against it after compiling**: `physical_read_set([ir], spine)` must be a subset of
`envelope["read_set"]` (subset, not equality — the compiler legitimately adds the spine's own keys,
and the test pins exactly which additions are allowed); the compiled `PitSpec`'s rendered clause
must match `envelope["pit"]`; the window must match `envelope["window"]`; the spine's population
must resolve from `envelope["population_ref"]`. Any divergence returns
`MaterializationRefused(PLAN_ENVELOPE_DIVERGENCE, …)` naming both sides.

**Modify:** `src/featuregen/materialize/codes.py` — the new member, with the §14 discipline its
neighbours carry. **Note the enum already gained `CompilationRefusalCode.FORMULA_SCHEMA_UNSUPPORTED`
(BR-6) and `ValidationFindingCode.ENGINE_VERSION_MISMATCH` (A.42) since Phase G shipped** — read the
current members before adding one, and do not confuse the compilation-side
`FORMULA_SCHEMA_UNSUPPORTED` with the activation-side reason code of the same spelling
(`semantic_eligibility_reasons.py:67`). They are different vocabularies answering different questions.

**Acceptance (tests):** `tests/featuregen/materialize/test_plan_envelope.py`
- `test_the_compiled_read_set_is_the_frozen_read_set` — the headline property.
- `test_the_compiled_pit_clause_is_the_frozen_pit_text`
- `test_a_divergent_source_table_refuses_by_name` — mutate the envelope; assert the code and that
  **nothing was rendered**.
- `test_the_allowed_additions_are_exactly_the_spine_keys` — the subset carve-out, pinned so it
  cannot widen silently.
- `test_a_run_without_an_envelope_compiles_exactly_as_before` — every existing `test_chain.py` case
  is byte-identical (53 cases; this is the regression guard on the whole phase).

> **ACCEPTED `7cb2f98d` (2026-08-14).** D-4 is real: the governed plan the human was shown and the
> plan that executes are no longer two independent derivations that nothing compares.
> `ResolvedFeature`/`ResolvedFeatureInput`/`AdmittedFeature` each gain `plan_envelope`, admission
> gains **check 7**, `compile_ir` gains `plan_envelope=` and validates AFTER compiling, and
> `CompilationRefusalCode.PLAN_ENVELOPE_DIVERGENCE` joins the closed §14 vocabulary with the
> discipline its neighbours carry. **Nothing substitutes:** every check runs on a complete artifact
> or a complete IR and the only outcome other than `None` is a refusal naming both sides.
>
> **THREE PLAN DEFECTS FOUND AND CORRECTED, and the first changes what an acceptance test can be:**
>
> 1. **`test_the_compiled_pit_clause_is_the_frozen_pit_text` is not executable, because there is no
>    compiled PIT clause.** `PitSpec` (`expression_ir.py:284`) carries structured fields and renders
>    no text anywhere in `materialize/`; `envelope["pit"]` is the human-facing PROSE
>    `recipe_temporal_v2.compile_temporal` writes (*"trailing 90d observation window over posted_ts
>    events: (cutoff − 90d, cutoff], values knowable strictly at or before the cutoff"*). Re-parsing
>    that prose to manufacture a comparison would be exactly the second derivation D-4 forbids. What
>    the two sides genuinely share is the WINDOW — frozen as a number, compiled as `window_length` —
>    so `test_the_compiled_window_is_the_frozen_window` is that test, checked on EVERY expression
>    (a ratio has two), and the frozen prose rides the refusal verbatim so an operator reads what
>    the human was told.
> 2. **The two sides do not speak the same dialect, and "must be a SUBSET of `envelope["read_set"]`"
>    taken literally would have compared nothing to nothing.** `physical_read_set` emits governed
>    LOGICAL refs (`hdfc::public.transactions.merchant_id`); the envelope's read set is the semantic
>    context's OBJECT refs (`public.transactions.merchant_id`) with the catalog carried once beside
>    them as `catalog_source` — which is precisely why `assemble_current_activation_state` has to
>    `normalize_ref(plan_catalog_source, *ref.split(".")[-3:])` before it can ask anything about
>    them. `_envelope_ref` is the one translation, and it handles the trap that comes with it: a
>    RELATION ref has two path segments, and shifting it one segment left would name a table
>    `transactions` in a schema `public` — equal to nothing, unequal to everything, and the read-set
>    check would silently pass forever. Pinned by `test_a_relation_ref_is_not_read_as_a_column`.
> 3. **The allowed additions are THREE classes, not "the spine's own keys".** The spine's relation,
>    keys, read set and availability column (the envelope names the population by TABLE only, so its
>    columns cannot be in the read set); each expression's AVAILABILITY column (§8 rule 1 — a
>    governed catalog fact, never a role the recipe bound); and each expression's SOURCE RELATION
>    (which enters the read set as a relation element while admission's check 7 already compared it
>    against `source_table`, in the envelope's own vocabulary — comparing it here would ask one
>    question twice in two dialects). `test_the_allowed_additions_are_exactly_the_spine_keys` pins
>    the exact five refs, so a fourth class must be argued for in the open.
>
> **Deviations and judgements, each deliberate:**
> (a) **Check 7 is split by what is KNOWABLE where.** The grain and the source relation are
> properties of the ARTIFACT and are checked at admission — the earliest point, and therefore the
> point at which nothing downstream has been produced. The read set, the population and the window
> need physical resolution and are checked after compiling. `test_a_divergent_source_table_refuses_by_name`
> asserts both halves of what the task asked: the code, and that `compile_ir` was never reached.
> (b) **Absence asserts nothing — neither a missing envelope nor a blank field.** Every work item
> written before migration 1068 carries none, and the whole materialization path predates the
> envelope; `window` is `None` for a recipe with no window parameter and `output_grain` is `""` for
> a request that declared none. Reading absence as an assertion would refuse the majority of genuine
> features for having been served by an earlier build.
> (c) **The envelope is outside `authoring_intent_hash`.** It is provenance, not identity: two
> inputs differing only in it re-hash identically and both admit
> (`test_the_envelope_is_outside_the_intent_hash`). The check-6 proof is untouched.
> (d) **The chain wiring is asserted from the AST** — `compile_ir` is called in exactly one place
> and must be handed `feature.plan_envelope`, so admission's check 7 and the compile checks are
> about ONE object. Without it the phase could validate nothing in production while every unit test
> passed.
> (e) `_split_table_ref` parses by hand rather than through `object_ref.parse_ref`: a malformed
> relation must not raise a bare `ValueError` out of a governed gate (§14) — it simply compares
> unequal and is refused as the divergence it is.
>
> **The regression guard holds:** `test_a_run_without_an_envelope_compiles_exactly_as_before`
> asserts `None`, `{}` and a MATCHING envelope produce the identical `FormulaExecutionIRV1` and the
> identical `ir_hash`; all 53 `test_chain.py` cases and the whole 1902-test `materialize/` package
> are green unchanged.
>
> **Four mutants were built and run before the tests were trusted:** validation made a no-op (5
> failures), the carve-out widened to "anything the compiler read" (2), admission's check 7 removed
> (2), and the pre-existing `_read_intent` monkeypatch in `test_resolve.py` — which now receives a
> pair — corrected rather than deleted.
>
> 14 new cases in `tests/featuregen/materialize/test_plan_envelope.py`, 5 in `test_admission.py`,
> 1 enum entry in `test_codes.py`'s exact-set assertion.
> Gates: full suite **11115 passed, 20 skipped** (11096/20 on `65fab2c6`); `-m eval` **73 passed**;
> ruff clean on `src/featuregen/materialize/` and `tests/featuregen/materialize/`; **mypy clean on
> all five touched source files** (`ir.py`, `admission.py`, `resolve.py`, `codes.py`,
> `compile/chain.py` — no pre-existing errors in this package).

### Task B4 — governed provenance on the request (1 day) — **migration 1067**

Today `materialization_request` carries a logical group name, an actor, a roles snapshot, an
idempotency key and an opaque `activation_state` jsonb. **Nothing links a run to the governed option
the human approved.**

**New:** `src/featuregen/db/migrations/1067_materialization_request_option_link.sql` —
`considered_revision_id text` + `option_id text`, nullable, with a composite FK to
`semantic_option_decision(considered_revision_id, option_id)`. Nullable because the existing
work-item-driven path predates it and must keep working.

**Modify:** `api/routes/materialization_runs.py` — the POST body accepts the option key; the route
calls `load_frozen_option_facts` + `assemble_current_activation_state` +
`activation_decision(frozen, current, "execute_materialization", actor)` and **refuses 409 with the
blocker codes** when it is not allowed. The `activation_state` jsonb records the decision verbatim
— which is what that column was for.

**Acceptance (tests):**
- `test_a_blocked_option_cannot_be_materialized` — each of the four §0.3 codes, one case each,
  asserting the response names the code and its `next_step`.
- `test_an_allowed_option_mints_a_request_carrying_its_provenance`
- `test_the_legacy_work_item_path_still_accepts` — nullable FK, proven.

> **ACCEPTED `a881ad59` (2026-08-14).** A materialization run now has governed provenance and a
> blocked option cannot be materialized. Migration **1067** adds `considered_revision_id` +
> `option_id` to `materialization_request` with a composite FK to
> `semantic_option_decision (considered_revision_id, option_id)` — 1063's own UNIQUE constraint is
> the target — nullable because the work-item-driven path predates the link and must keep working.
> `POST /materialization-runs` accepts the pair, calls `load_frozen_option_facts` +
> `assemble_current_activation_state` + `activation_decision(…, "execute_materialization", actor)`,
> and refuses **409 `ACTIVATION_BLOCKED`** carrying every blocker's `code` AND its `next_step` — the
> same body shape `contract.py` already returns, so a client that renders one renders both. The
> decision is written verbatim into `activation_state`, which is what that column was for.
>
> **PLAN DEFECT: "each of the four §0.3 codes, one case each" is not achievable on this branch, and
> it is C-phase's absence rather than a test-design problem.** `EXECUTION_AUTHORITY_UNEVALUATED` and
> `EXECUTION_AUTHORITY_UNMET` are the two arms of ONE rule, so they can never both appear;
> `formula_schema_supported` and `requirements_closed` are hardwired `False` and
> `effective_readiness` is the frozen literal until C1/C2/C3, so no fixture can clear them
> individually without inventing C-phase. **Measured** on a bound option with a frozen plan, the set
> that actually blocks is `{READINESS_NOT_MATERIALIZATION_READY, FORMULA_SCHEMA_UNSUPPORTED,
> EXECUTION_AUTHORITY_UNMET}` — `UNMET`, not `UNEVALUATED`, because B1's plan gives the current
> layer a read set to evaluate, which is exactly the reliability §0.3 predicted B1 would buy. Each
> is asserted present with its `next_step`, one parametrized case each, and the exact intersection
> with the six materialization-only codes is asserted rather than a subset.
>
> **The positive control is real and reachable**, without which every refusal above could be passing
> for the wrong reason: `test_an_allowed_option_mints_a_request_carrying_its_provenance` patches
> **only** `assemble_current_activation_state` (C-phase's job, and its own comment says so) and
> never `activation_decision` — the real fold over a real frozen `semantic_option_decision` row is
> what returns `allowed=True`, mints the request and stores the pair.
>
> **Deviations and judgements, each deliberate:**
> (a) **Three refusal shapes, told apart on purpose.** No option key → accept (the legacy path,
> proven). HALF a key → 422 (an option is addressed by both halves; recording it would put
> provenance in the table nothing could resolve). A key naming no decision row → **404, not 409**:
> "this approval does not exist" is a different fact from "this approval is blocked", and a caller
> chasing the wrong one wastes a governance conversation.
> (b) **The option key joins `IDEMPOTENT_IDENTITY_FIELDS`.** One idempotency key reused for a
> DIFFERENT governed option is not a retry — it is a second approval being answered with the first
> one's run. `test_request_store.py`'s partition assertion forced the decision, which is what that
> assertion exists for.
> (c) **The gate runs BEFORE anything is minted**, asserted: a blocked option leaves no request row
> and no queue row, the same "a typo leaves nothing behind" property the existing pre-flight has.
> (d) **`test_migration_1053.py::test_the_column_shape_is_pinned` was updated, not bypassed** — it
> caught the two new columns exactly as designed, and its docstring now records why these two are
> nullable for a different reason than `generation_id`/`run_id` ("not applicable", not "unknown
> yet").
> (e) **`MATCH SIMPLE` plus a CHECK, not `MATCH FULL`.** With the default, a row where either column
> is NULL satisfies the FK, so the legacy path is unconstrained while a *stated* pair must be real;
> the half-stated case is closed by its own named CHECK so the error says "an option is named by
> BOTH halves" instead of quoting a foreign key.
>
> **The migration is audited against a POPULATED table:** the link is dropped, a request is seeded
> in the pre-1067 shape, and 1067's own SQL is run against it — the ALTER lands, 1053's `updated_at`
> trigger is never reached (`updated_at` is byte-identical afterwards), no value is backfilled, the
> legacy row still reads, and the new constraints then bite (a pair naming no decision row is
> refused by the FK, half a key by the CHECK). Re-runnability is proved by applying it twice.
>
> **Two mutants were run before the tests were trusted:** a gate that never refuses (5 failures) and
> a route that validates but never records the provenance (2).
>
> 13 cases in `tests/featuregen/api/test_materialization_option_link.py`, 1 pin updated in
> `test_migration_1053.py`.
> Gates: full suite **11128 passed, 20 skipped** (11115/20 on `30ad17de`); `-m eval` **73 passed**;
> ruff clean and **mypy clean** on both touched source files (`materialization_runs.py`,
> `request_store.py` — no pre-existing errors in either).

---

## 4. Phase C — engine capability registration *(weld 4)*

*Closes: `FORMULA_SCHEMA_UNSUPPORTED`, `READINESS_NOT_MATERIALIZATION_READY`,
`EXTERNAL_VALIDATION_OUTSTANDING`.*

### Task C1 — the engine capability registry (1 day)

Per D-5 this is **code, not a table**: it describes what `materialize/render/` can emit.

**New:** `src/featuregen/materialize/engine_capability.py` —
`KEDRO_PYSPARK_ENGINE: EngineCapabilityV1` with `engine_id="kedro-pyspark"`,
`supported_aggregations=frozenset({…})`, `supports_window_offset=…`,
`supports_future_horizon=…`. **Every member is derived from what the renderer proves it can emit**,
not asserted: the module builds the set from `render/nodes_compute.py`'s aggregation dispatch, and a
test asserts the two agree exhaustively.

`engine_capability_for(engine_id: str) -> EngineCapabilityV1 | None` — the one lookup.

**Acceptance (tests):** `tests/featuregen/materialize/test_engine_capability.py`
- `test_every_advertised_aggregation_has_a_rendering_path` — for each member, render a calculation
  node and **execute it through `fake_spark.run_rendered`**; an aggregation the renderer cannot emit
  fails here. This is the test that makes the advertisement true rather than aspirational.
- `test_every_renderable_aggregation_is_advertised` — the converse; the pair is exhaustive.
- `test_window_offset_and_future_horizon_advertisements_match_the_renderer`
- `test_no_engine_capability_is_asserted_by_hand` — the constant is built, not typed (AST/structural).

> **ACCEPTED `945be26e` (2026-08-15).** Landed inline by the main session after six consecutive
> subagent API connection drops (three on one agent, three on its fresh replacement — none produced
> any file change; the worktree was verified untouched between every attempt).
>
> **The derivation runs one layer deeper than the plan asked, on purpose.** `EngineCapabilityV1`
> already exists (`formula/capability_v2.py:31` — BR-6 built the type and the engine arm; the plan's
> §C1 wording reads as if the type were new). What C1 adds is the INSTANCE and its truth: a public
> `renderable_aggregations()` ON THE RENDERER (`render/nodes_compute.py`, beside `_AGGREGATE_CALLS`)
> states the renderer's own vocabulary — the dispatch's keys plus `COUNT_ROWS`, added beside the
> fact that explains it (no operand to substitute; rendered on its own arm) — and
> `engine_capability.py` builds the advertisement from that, mapping members to v2 value strings.
> The two boolean advertisements are NOT written in the module at all: they are the dataclass's own
> fail-closed defaults, and `materialize/render/` contains no `offset_periods` and no
> `future_horizon` rendering anywhere — the advertisements test pins the absence at source level,
> so offset rendering added without revisiting the advertisement fails the build.
>
> **The rendering-path test executes, per member, with four distinguishable answers.** One
> projection (two 10s and a NULL for C1, one 7 for C2) yields sum=20/7, count_non_null=2/1,
> count_distinct=1/1, count_rows=3/1 — a dispatch that wired two members to the same Spark call
> collides on at least one row. `COUNT_ROWS` is exercised by re-aggregating the compiled SUM IR
> and shedding the OPERAND role from its read set (`operand is None` IFF `COUNT_ROWS` is the
> grammar rule the renderer enforces in both directions).
>
> **Mutant proof:** a hand-typed aggregation set fails the AST test; an over-advertised `"avg"`
> fails three tests (its rendering-path case, the converse, and the no-hand-typing scan); a false
> `supports_window_offset=True` fails the advertisements test. All three built and run; the
> original restored and green.
>
> 8 cases in `tests/featuregen/materialize/test_engine_capability.py` (the plan's four plus the
> unknown-engine lookup and a v2-only exclusion slice inside the converse).
> Gates: full suite **11136 passed, 20 skipped** (baseline 11128/20 at `0146c92e`); `-m eval`
> **73 passed**; ruff clean on the three touched files; mypy clean on `engine_capability.py`.

### Task C2 — activation reads the registry (1 day)

**Modify:** `src/featuregen/overlay/upload/semantic_option_decision.py` —
`assemble_current_activation_state` replaces the hardwired `formula_schema_supported=False`
(`:363`) with: resolve the recipe's reviewed expectation → parse its pinned `gold_v2` proposal via
`parse_versioned` → `classify_formula_capability_v2(proposal, engine=engine_capability_for(
"kedro-pyspark")) == "ok"`. Every failure path (no expectation, unparseable fixture, unknown engine)
returns `False` — the dataclass's existing fail-closed posture, unchanged.

**Acceptance (tests):**
- `test_a_supported_v2_formula_flips_formula_schema_supported`
- `test_an_offset_formula_is_unsupported_when_the_engine_does_not_advertise_offsets` — construct an
  `EngineCapabilityV1` without `supports_window_offset` and assert `FORMULA_SCHEMA_UNSUPPORTED`
  survives. **The refusal must remain reachable**; a capability seam that can only say yes is not one.
- `test_an_unparseable_pinned_fixture_fails_closed`

> **ACCEPTED `593c7148` (2026-08-15).** Landed inline (same subagent-outage context as C1).
>
> **THE PLAN'S MECHANISM WAS NOT BUILDABLE, and the correction is recorded here rather than
> silently substituted.** §C2 says: parse the recipe's pinned `gold_v2` proposal via
> `parse_versioned` at activation time. The pinned fixture lives under
> `tests/featuregen/formula/gold_v2/` — a deployed backend HAS no tests tree, so production can
> never parse it. What production CAN resolve is the CAPTURE BLUEPRINT
> (`recipe_formula_shadow.capture_blueprint_for` — the exact object a review event's hash covers
> since A5, chosen by the recipe's own declared schema version), and its expressions carry
> precisely the engine-relevant demands: aggregations, `window.offset_periods`, `window.basis`.
> The fixture parse remains the TEST-side proof it already was (the pin test parses under
> `parse_versioned` and hash-compares). `_formula_schema_supported(recipe_id)`:
> reviewed → capture → demands → `classify_demands_for_engine(...,
> engine=engine_capability_for("kedro-pyspark")) == "ok"`; every failure path (unreviewed, no
> bindable blueprint, unknown engine, any raise) is `False` — the dataclass posture, unchanged.
>
> **One engine arm, two carriers.** `classify_demands_for_engine` is factored out of
> `classify_formula_capability_v2`'s engine arm (the proposal path delegates to it, behavior
> identical); the blueprint path asks the same three questions of the same advertisement. A
> reviewed **v1** blueprint answers through the SAME resolver — `merchant_mcc_diversity`'s
> `count_distinct` is advertised and v1 windows have neither fork, so the platform's one
> executable formula generation honestly reads as supported (D-7's grain disagreement is a
> different question and stays open).
>
> **The exact-intersection tests moved, and the movement is the evidence.**
> `FORMULA_SCHEMA_UNSUPPORTED` fell for the reviewed exemplar — the second of §0.3's four codes
> to fall, exactly as the phase ordering predicted. The unreviewed discriminator now carries TWO
> coupled extra codes (`FORMULA_NOT_REVIEWED` + `FORMULA_SCHEMA_UNSUPPORTED`): an unreviewed
> recipe has no reviewed demands to compare, so review is the gate that opens the capability
> question. A5's and B4's intersections restated accordingly; `BLOCKING_TODAY` for the bound
> exemplar is now `{READINESS_NOT_MATERIALIZATION_READY, EXECUTION_AUTHORITY_UNMET}`.
>
> **The refusal stays reachable through the REAL seam:** the exemplar's own derived blueprint
> with one window shifted back a period (`offset_periods=1`) folds `formula_schema_supported`
> back to False. Mutant proof: rewiring the assembler to the old hardwired `False` fails the two
> real-row intersection tests; restored green.
>
> 6 cases in `tests/featuregen/overlay/upload/test_engine_capability_activation.py`.
> Gates: full suite **11141 passed, 20 skipped** (11136/20 after C1); `-m eval` **73 passed**; ruff clean on the five touched
> files; `capability_v2.py` mypy clean; `semantic_option_decision.py` carries **1 pre-existing**
> mypy error (`:443` union-attr, measured identical on the HEAD copy by swap) — none added.

### Task C3 — effective readiness and requirements are folded, not asserted (1 day)

**Modify:** `assemble_current_activation_state` — `effective_readiness` becomes a **re-fold** at the
durable write (`fold_readiness` with `engine_verdict` from C1/C2), replacing
`effective_readiness=frozen.readiness` (`:361`). `requirements_closed` becomes a real read: every
code in `frozen.outstanding_requirement_codes` has a recorded result in the validation store,
replacing the hardwired `False` (`:364`).

**Acceptance (tests):**
- `test_effective_readiness_is_refolded_not_inherited` — a frozen `MATERIALIZATION_READY` whose
  current inputs regressed folds down and blocks (drift, the whole point of the current layer).
- `test_requirements_closed_requires_a_recorded_result_per_code`
- **`test_all_four_materialization_codes_can_clear_together`** — the milestone test: a seeded option
  with a reviewed v2 expectation, a supported engine, a frozen plan, closed requirements and a met
  execution floor returns `activation_decision(..., "execute_materialization").allowed is True`.
  **This is the §0.5 item 3 bar.**

> **ACCEPTED `06b20dc3` (2026-08-15).** Landed inline (same context as C1/C2). **THE
> MILESTONE PASSES**: `test_all_four_materialization_codes_can_clear_together` ends in
> `activation_decision(..., "execute_materialization").allowed is True` with an EMPTY blocker
> list — the §0.5 item 3 bar, over a real frozen row.
>
> **The re-fold, and what it carries.** `effective_readiness` is `fold_readiness` re-run at the
> durable write (BR-7's one fold — never a second opinion): the review fact and the engine
> verdict are re-read NOW (an un-review DEMOTES a frozen `MATERIALIZATION_READY` to
> `FORMULA_BLOCKED`; C2's advertisement promotes), while the serving fold's measured
> temporal/binding/policy blockers CARRY VERBATIM on a new frozen fact
> (`FrozenOptionFactsV1.readiness_blockers`, riding the story jsonb like its B10 neighbours —
> nothing at the durable write can re-measure a binder verdict; drift in what they measured
> surfaces through the pins and snapshot checks). The fold's own vocabulary is stripped from the
> carried codes first — it re-derives those, and carrying them would double-count a fact the
> re-read may have changed. Non-recipe sources keep the frozen readiness: no fold owns them.
>
> **TWO PLAN CORRECTIONS, both structural:**
> 1. **`requirements_closed` cannot be a pure option-row read.** The validation store is
>    CONTRACT-keyed (`feature_validation_requirement` + the 1009 event stream) and
>    `semantic_option_decision` carries NO contract identity — no column, no FK, nowhere. The
>    read therefore rides an explicit `contract_id` parameter on
>    `assemble_current_activation_state` (None → False, fail closed: nothing recorded is not
>    nothing owed). The passed set comes from the projection's OWN fold —
>    `_fold_effective_state` now returns it (4th element, one call site updated) and
>    `passed_requirement_codes` maps ids→codes through the same walk; a second implementation
>    of the epoch/discard/invalidate rules was the alternative and was rejected.
> 2. **`gold_validated` has NO recorded source anywhere.** The provider half never ran (A3's
>    billing deferral) and no store records a gold-evaluation outcome; the fold input is served
>    by `_gold_evaluation_recorded` — honestly `False` for every recipe, a NAMED absence with a
>    docstring, not a hardwired verdict. The milestone seeds it there (the plan pre-authorized
>    seeding); the honest production ceiling today is `FORMULA_AUTHORABLE`, and a test pins
>    exactly that.
>
> **What the milestone seeds vs. what is real.** REAL: the frozen row through the real writers;
> review events from all three required roles at the frozen revision (the BR-23 fold, no
> patching); the execution/authoring floors via seeded `field_evidence` read through the REAL
> resolver pins (`human/confirmed` clears both matrix columns); the C1/C2 engine verdict; the
> activation fold itself. SEEDED, each named: the gold hook (above) and snapshot freshness
> (contract-rung scaffolding — snapshot minting rides the generation pipeline).
>
> **Mutant proof:** restoring the pre-C3 passthrough (`effective_readiness=frozen.readiness`)
> fails 3 of the 5 new tests; the requirements walk (none→half→all→empty) discriminates a
> hardwired boolean in either direction by construction. 5 cases in
> `tests/featuregen/overlay/upload/test_activation_refold.py`.
> Gates: full suite **11146 passed, 20 skipped** (11141/20 after C2); `-m eval` **73 passed**; ruff clean on the four touched
> files; the 2 mypy errors in `activation_policy.py`/`feature_validation_projection.py` are
> pre-existing (measured on HEAD copies by swap — the projection's is HEAD's `:188` shifted) —
> none added.

---

## 5. Phase D — G-2 and G-3: the chain reaches a published table *(weld 3)*

### Task D0 — disarm the `PublishStepMissing` landmine (½ day) — **do this FIRST**

§0.4. Until the publish step exists, an operator ingesting one passing attestation crashes every run.

~~**Modify:** `src/featuregen/materialize/queue_lane.py` — catch `PublishStepMissing` in
`process_materialization_once`~~ — **ALREADY BUILT, verified.** `queue_lane.py:719-721` already
catches it and returns `status="publish_step_missing"` with the exception's own text as the durable
reason; `test_queue_lane.py:345` (`test_a_PROVEN_capability_fails_the_request_with_a_LEGIBLE_reason`)
already asserts the request lands `failed`, the queue row lands `dead` (not retried) and no tree is
left behind. Both shipped in `b21a794a`, Phase G's own lane commit. This half of D0 is a no-op.
**Modify:** `src/featuregen/materialize/publish.py` — `record_attestation` refuses to write a
`passed` attestation while no publish step is registered, naming G-3. A capability record that makes
the platform crash is not a capability record.

**Acceptance (tests):**
- `test_recording_a_passing_attestation_is_refused_until_the_publish_step_exists`
- ~~`test_the_lane_classifies_PublishStepMissing_instead_of_crashing`~~ — exists as
  `test_a_PROVEN_capability_fails_the_request_with_a_LEGIBLE_reason`; not duplicated under a second
  name.
- Both tests are **deleted by D3** — record that in the task, so the guard cannot outlive its cause.

> **ACCEPTED `754bdfb8` (2026-08-15).** **HALF OF THIS TASK WAS ALREADY BUILT** — plan defect
> found and corrected above. `queue_lane.py:719-721` has caught `PublishStepMissing` and returned
> `status="publish_step_missing"` since `b21a794a` (Phase G's own lane commit), and
> `test_a_PROVEN_capability_fails_the_request_with_a_LEGIBLE_reason` already pins the request to
> `failed`, the queue row to `dead` (not retried — an operator ingesting an attestation is not a
> transient fault) and the tree to empty. A second test under the plan's name would have asserted
> the same three facts twice.
>
> **What landed: the writer-side guard.** `record_attestation` refuses a PASSING probe result while
> `_PUBLISH_STEP_REGISTERED` is `False`, naming G-3 and saying what to do instead ("keep the
> evidence and ingest it once G-3 lands"). A `RuntimeError`, for `PublishStepMissing`'s reason —
> §14's closed vocabulary has no member for "the step that would act on this has not been built",
> and typing one in would tell an operator the catalog refused their feature. **A FAILING result is
> still ingested**, and that half is load-bearing rather than incidental: it is the only evidence
> that tells `PUBLISH_MECHANISM_UNSUPPORTED` ("the design must change") from `CAPABILITY_UNPROVEN`
> ("go run the probe"), so a guard that swallowed it would erase the distinction it claims to
> protect. The new test asserts both directions.
>
> **Why a module constant and not a parameter.** A parameter would be a way for the caller to state
> that the platform HAS a publish step, which is not the caller's fact to state. The three test
> sites that legitimately need a passing attestation flip it explicitly — an autouse fixture in
> `test_publish.py` (76 cases turn on §10.3's selection algebra, which needs passing rows) and a
> try/finally in `test_chain._attest_capability`, which `test_queue_lane.py` imports. That flip IS
> the guard working: a test building the landmine on purpose is exactly what it distinguishes from
> an operator stepping on it.
>
> **DELETED BY D3**, all of it: the constant, the guard, the fixture, the try/finally and
> `test_recording_a_passing_attestation_is_refused_until_the_publish_step_exists` — together with
> `PublishStepMissing` itself, whose absence is the guard's entire cause.
>
> **Mutant proof:** deleting the two guard lines makes the new test fail `DID NOT RAISE
> RuntimeError`. (Flipping the constant's default to `True` does NOT discriminate, because the test
> sets it itself — recorded so the weaker mutant is not mistaken for the proof.)
> Gates: full suite **11147 passed, 20 skipped** (baseline on `7daec8a6` was 11146/20);
> `-m eval` **73 passed**; ruff + mypy clean on all three touched files.

### Task D1 — G-2 into the chain (3 days)

`prepare_run`, `run_l1` and `submit` exist and are tested in isolation (`test_submit.py`: 27 cases,
including that the exact parameters reach the interpreter, that nested `input_snapshots` survive
intact, that shell metacharacters are not interpreted, and that a timeout kills the process group).
They are not composed.

**Modify:** `src/featuregen/materialize/compile/chain.py` — new `ChainStage` members `PREPARE_RUN`,
`VALIDATE_L1`, `SUBMIT`, run after `VALIDATE_L0` **passed** only. Event kinds already exist on the
plane (migration 1034's closed vocabulary). The append-only law holds: still no `PUBLISHED` from
this module (D3 owns that), and `test_the_chain_can_never_append_PUBLISHED` stays green until D3
deliberately changes it.

> **PLAN CORRECTION (D1, verified).** "After `VALIDATE_L0` passed" is necessary and **not
> sufficient**: `prepare_run` takes a `capability_attestation_id` (`runprep.py:841`) and
> `sandbox_execution_hash` refuses a blank one — *"a defaulted `capability_attestation_id` would let
> an execution be identified without naming the attestation §10.3 requires"* (`identity.py:494`).
> So §11.1 identifies an execution PARTLY BY the attestation it will publish under, and G-2 is
> reachable **only under a `PublisherSelection`** — precisely the branch that raised
> `PublishStepMissing` before rendering. Two structural consequences: the raise **moves to after
> `SUBMIT`** (an unprepared run and a failed submission each have a truer thing to say than "the
> publish step is missing"), and the two existing proven-capability tests change with it.

**Modify:** `queue_lane.py` — `MaterializationLaneConfig` gains the submitter and the metastore
adapter; both `None`-able, and `None` is an honest **outcome** (an unprepared run), never a skip —
the same rule `run_l0` already follows.

> **PLAN CORRECTION (D1, verified).** Two, not three, is the wrong count and the deployment is the
> wrong owner for one of them. `prepare_run` also needs a **`staging_base`** (deployment) and a
> **`business_dt`** (per-RUN — a worker-wide date would run every group at whatever day the worker
> was configured with), and `MaterializationJobV1` carried no business date at all. The date rides
> the queue payload as an OPTIONAL key with **no `PAYLOAD_VERSION` bump**: an optional key added to
> a version is readable by both spellings, a job frozen before D1 decodes as `business_dt=None`
> (which is what it asked for — a compilation, not a run), and bumping the version would instead
> dead-letter every in-flight job. All four fold into ONE frozen `chain.RunExecution` at
> `MaterializationLaneConfig.execution_for(job)`, so no caller can assemble a half-configured seam.

**Acceptance (tests):** extend `tests/featuregen/materialize/test_chain.py`
- `test_a_passed_L0_advances_to_prepare_run`, `test_a_failed_L0_never_prepares`
- `test_the_prepared_parameters_are_exactly_REQUIRED_RUN_PARAMETERS`
- `test_a_submission_failure_is_RUN_FAILED_with_the_returncode_in_the_detail`
- `test_the_chain_still_appends_no_PUBLISHED`
- The real-JVM half runs in `l0_gate.py`'s sibling (`make l0-gate`), not the default suite —
  `pyspark`/`kedro` are deliberately not dependencies of this platform.

> **ACCEPTED `d3ff1064` (2026-08-15).** G-2 is composed: `prepare_run` → `run_l1` → `submit`,
> inside `_commit`'s one transaction, on a run whose L0 PASSED and whose publication capability was
> PROVEN. **THE TWO PLAN CORRECTIONS ARE ABOVE**, both structural — the attestation is part of an
> execution's identity, and the run's business date is not the deployment's to state.
>
> **THE LADDER, and why every rung is an OUTCOME rather than a skip** (`run_l0`'s rule, applied
> four more times). `_RunAttempt` is the ONE place the terminal, the lifecycle and `stopped_at` are
> decided together, because they are three readings of one fact and three expressions computing
> them from the same booleans is how they come to disagree.
> `VALIDATE_L0` not passed → nothing downstream is attempted (a run whose bytes were never
> validated has no artifact to resolve partitions for) · no selection → `PUBLICATION_REFUSED`,
> **G-1's terminal, unchanged**, and the reason is already its own detail · `execution=None` →
> `RUN_FAILED` at `PREPARE_RUN` naming the four things the deployment did not configure · a spine
> vintage or snapshot refusal → `PREPARE_RUN` with the governed code · L1 not passed →
> `VALIDATE_L1` · a submission that ran and failed → `SUBMIT` with the returncode in the detail; one
> that never STARTED → `SUBMIT` **without** one, because `returncode is None` means no process
> produced a verdict and printing an exit status would be an invented observation · a completed
> submission → `PublishStepMissing`, **which now rolls the whole record back** (generation,
> artifact, both reports, tree) rather than committing a terminal nobody can write.
>
> **G-2 IS COMPOSED AND NOT YET RUNNABLE OUTSIDE TESTS, and that is honest rather than a gap.** No
> `MetastoreMetadata` implementation exists anywhere in `src/` — `inventory.MetastoreInventoryAdapter`
> answers CAPTURE's three questions (`describe_columns` / `describe_partition_columns` /
> `table_location`), not L1's (`list_partitions` / `describe_table` / `can_read`) — so
> `lane_config_from_env` produces `metastore=None` and every deployed run is honestly unprepared.
> Writing that adapter is the operator-facing precondition for E1/E2, not a defect here.
>
> **Two existing tests changed, each recorded rather than quietly fixed.**
> `test_a_PROVEN_capability_stops_the_chain_rather_than_letting_it_claim_a_publish` became
> `..._without_an_execution_seam_is_an_UNPREPARED_run`: the terminal is now PRECISE where it used to
> be blunt, and it must not read as a publication refusal because publication capability is PROVEN
> on that path. The lane's `test_a_PROVEN_capability_fails_the_request_with_a_LEGIBLE_reason` split
> in two — the unconfigured deployment is `run_failed` with a `done` queue row (the message WAS
> processed), and the plan's own
> `test_the_lane_classifies_PublishStepMissing_instead_of_crashing` is the fully-configured half,
> still `dead`-lettered and still un-retried. **Both are deleted by D3.** `_recorded` gained a
> fourth lane status, `run_failed`, kept apart from `refused` for `build_unproven`'s reason: there
> is no governed verdict about a feature on those three stages, and `CompiledGroup.refusal` is
> often `None`.
>
> **A BRIDGED GROUP REFUSES AT `PREPARE_RUN`, by construction.** `prepare_run` requires a
> `BridgeExecutionAuthorization` covering the artifact's exact IRs whenever the compilation declares
> realization dependencies (`runprep.py:859`) and this chain has no parameter carrying one, so a
> cross-catalog group stops with `JOIN_CARDINALITY_UNKNOWN` rather than executing an unauthorized
> hop. That is ALSO what makes the default `required_parameters` correct at the submit seam: the one
> name that widens the rendered set is `bridge_predicate_values` (`render/project.py:1296`), which
> only a bridged artifact wires — and no bridged artifact can reach the submission. This is
> DEFERRED-WORK A.39's "the two tiers meet in G-2" row, met and fail-closed.
>
> **Mutant proof, three, each caught:** (a) `execution=None` folded to `PUBLICATION_REFUSED` — the
> "None is a skip" defect the rule forbids — fails the UNPREPARED test; (b) submitting
> `{"business_dt": …}` instead of `prepared.parameters` fails
> `test_the_prepared_parameters_are_exactly_REQUIRED_RUN_PARAMETERS` on the set equality; (c)
> treating a non-completed submission as completed fails both submission tests (they reach the
> raise instead). `test_the_chain_can_never_append_PUBLISHED` — the plan's
> `test_the_chain_still_appends_no_PUBLISHED`, under the name it already ships with — stays green
> untouched. 7 new cases in `test_chain.py`, 1 new in `test_queue_lane.py`.
> Gates: full suite **11154 passed, 20 skipped** (11147/20 after D0); `-m eval` **73 passed**;
> ruff + mypy clean on both touched source files.

### Task D2 — the publication probe driver (2 days; the live half is an **operator action**)

`assess_probe_observations` (the deciding half, needs no cluster) exists;
`probe_publication_capability` (the live driver) does not.

**New:** `src/featuregen/materialize/probe.py` —
`probe_publication_capability(cluster, *, mechanism, engine_versions) -> ProbeResult`. Per
DEFERRED-WORK A.26: it *collects observations and calls `assess_probe_observations`; it has no way
to state a different verdict.* Publishes A, polls, publishes B, repeats while adding a feature
column, hands every reading — including the ones that looked bad — to the assessor.

**Acceptance (tests):**
- `test_the_driver_states_no_verdict_of_its_own` — structural: `ProbeResult` is only ever
  constructed by `assess_probe_observations`, asserted over the module AST.
- `test_a_probe_that_observed_nothing_cannot_pass` — A.26's vacuity guard, from the driver side.
- `test_only_VERSIONED_POINTER_is_attempted` — `RENDERABLE_MECHANISMS` is a frozenset of one; asking
  for another refuses rather than inventing a catalog entry.
- **Operator action, explicit go required:** run the probe against the kind cluster; record the
  attestation. Cluster spend + a durable governance record.

> **PLAN CORRECTION (D2, verified).** The stated signature
> `probe_publication_capability(cluster, *, mechanism, engine_versions)` cannot run. `publish.py`'s
> own law — *"Nothing is generated here. No clock, no id factory"* — makes **`probe_id`** and
> **`clock`** parameters rather than things a driver mints (a probe that minted its own id would
> record when it was *told*, not what ran, and the attestation is recorded UNDER that id).
> `ProbeObservation` refuses an empty column list, so **`readers`** and **`columns`** are required;
> and §10.3 step 5's "repeated while ADDING a feature column" has to be told WHICH column, so
> **`feature_column`** is too. `environment_id` is deliberately NOT a parameter — it is the
> target's own fact, and a parameter would let a probe run against one cluster and be recorded
> against another.
>
> **PLAN CORRECTION (D2, verified).** `RENDERABLE_MECHANISMS` already exists, in
> `render/publish.py:62`, and is already a frozenset of one. The driver READS it rather than
> declaring a second: A.26 names `RENDERABLE_MECHANISMS` and `publish_entry_body` as the two places
> to extend, and a copy here would be a third that drifts. A test monkeypatches the renderer's set
> and asserts the driver follows.

> **ACCEPTED `a962934f` (2026-08-15).** `src/featuregen/materialize/probe.py` — the live driver,
> `PublicationTarget` (the cluster seam) and nothing else. **NO LIVE PROBE WAS RUN**: nothing in
> this task touched `kubectl`, a cluster or a subprocess, and running it against kind stays an
> OPERATOR ACTION requiring an explicit go (cluster spend + a durable governance record).
>
> **A.26's constraint is structural, not observed.** `test_the_driver_states_no_verdict_of_its_own`
> parses the module's AST and asserts `ProbeResult` is never CALLED in it while
> `assess_probe_observations` is — a behavioural test could only ever prove the verdict was right
> for the inputs it happened to try, and this proves there is nowhere else for one to come from.
> The signature test is its other half: no `passed=`, no `covers_schema_evolution=`, no
> `evidence_hash=`, no `result=`.
>
> **Every reading is handed over, and the test says so.** `_look` returns each reader's observation
> in order with no filtering, no de-duplication and no retry-until-clean: a torn read is the single
> most valuable thing a probe can observe (it is what tells `PUBLISH_MECHANISM_UNSUPPORTED` from
> `CAPABILITY_UNPROVEN`), and a driver that smoothed it away would report a pass on an environment
> it had just watched fail. A reader that could not read returns `None` and contributes nothing
> rather than a fabricated reading — which can only make the verdict weaker, the right direction
> for an absence.
>
> **The vacuity guard is where A.26 put it — in the TYPE.** `test_a_probe_that_observed_nothing_
> cannot_pass` drives a target whose every reader fails, and asserts the verdict rather than a
> branch; the driver still ASKED all eight times, so the emptiness is an observation and not a skip.
> A second case pins the other vacuity (readers who only ever saw one generation watched no swap).
>
> **The sequence.** Two rounds of publish-A → poll → publish-B → poll, the second over
> `columns + (feature_column,)`, all four rounds' readings handed over as ONE evidence set — two
> probes would produce two attestations neither of which covers schema evolution. Four calls that
> could only ever be assessed as a failure (no readers, no columns, a feature column already
> published, a non-renderable mechanism) are `ValueError`s BEFORE any cluster time: §14 has no
> member for "this call was assembled wrongly", and spending a live run to report a demonstrated
> absence where nothing was demonstrated is the worst of both.
>
> **Mutant proof, two, both caught:** (a) re-constructing the assessor's own answer as a fresh
> `ProbeResult` fails the AST test; (b) handing over only the first reader's look fails
> `test_every_reading_reaches_the_assessor_including_the_bad_ones`. 14 cases in
> `tests/featuregen/materialize/test_probe.py`.
> Gates: full suite **11165 passed, 20 skipped**; `-m eval` **73 passed**; ruff + mypy clean on
> both new files.

### Task D3 — G-3: the publish step (2½ days) — **migration 1055**

**New:** `src/featuregen/db/migrations/1055_feature_active_revision.sql` — the reserved
active-revision pointer, append-only in the 1034/1044 style.

**Modify:** `src/featuregen/materialize/publish.py` — the publish step proper: the metastore write,
the active-revision record, the pointer swap. `render/publish.py` already emits the
`VERSIONED_POINTER` catalog entry, and `render_project(..., publisher_selection=…)` already accepts
the selection — the renderer side is done.

**Modify:** `chain.py` — on a `PublisherSelection`, **honour it** instead of raising
`PublishStepMissing`; append `RunEventKind.PUBLISHED`. `PublishStepMissing` and D0's two guard tests
are deleted in this commit, and `test_the_chain_can_never_append_PUBLISHED` is replaced by
`test_the_chain_appends_PUBLISHED_only_after_a_proven_selection_and_a_passed_gate`.

**Acceptance (tests):**
- `test_a_published_run_folds_to_RunStatus_PUBLISHED`
- `test_publication_without_a_matching_attestation_is_still_refused` — the refusal must survive.
- `test_the_pointer_swap_is_recorded_before_it_is_claimed`
- `test_the_terminal_cannot_be_retracted` — migration 1044's ordering trigger, exercised.

> **PLAN CORRECTION (D3, verified).** "The metastore write … and the pointer swap" cannot be
> *written* here: there is no cluster-write seam anywhere in `src/`, and there must not be one —
> the package invariant is *"this package is render-only. It never imports `pyspark`"*. So the
> publish step's cluster half is a Protocol, `publish.PublicationSwap`, in exactly the posture D1
> gave `MetastoreMetadata`: the deployment supplies it, **no implementation exists in `src/`**, and
> `swap=None` on the lane config means this deployment cannot publish — recorded as the run's
> outcome, never as a skipped step. What D3 *builds* is the governed half: migration 1055's record,
> the ordering, and the seam's contract.

> **ACCEPTED `5e99ef24` (2026-08-15).** G-3 lands, and **`PublishStepMissing` is gone with its
> whole cause** — the class, the lane's `except` branch, D0's `_PUBLISH_STEP_REGISTERED` guard, its
> autouse fixture, `test_chain._attest_capability`'s try/finally, D0's guard test and D1's
> `test_the_lane_classifies_PublishStepMissing_instead_of_crashing`. The guard did not outlive its
> cause, which was the point of recording it as temporary in three places.
>
> **Migration 1055 — a FILE, applied to no cluster.** Verified free on every ref before writing
> (`git log --all --diff-filter=A -- 'src/featuregen/db/migrations/1055*'` is empty), and reserved
> for exactly this since Phase G's T1. Append-only in the 1034/1044 style, plus 1044's ordering
> argument applied **per GROUP**: publication is atomic per group (§10.1), so two groups publish
> independently while a stale publisher inside one group is refused at INSERT. **No `is_current`
> column** — the newest `seq` is what holds, because a mutable current-pointer is a record that can
> be rewritten, and this is the only record that says a feature was ever readable. Audited against a
> POPULATED seeded legacy shape (the standing lesson): 11 cases in
> `tests/featuregen/db/test_migration_1055.py`, including re-application over populated rows, the
> per-group ordering, the three mutation guards (UPDATE/DELETE/**TRUNCATE**, which needs its own
> STATEMENT-level trigger), the real FK, the closed mechanism vocabulary, and that the append-only
> trigger is **1034's own function OID** rather than a copy.
>
> **THE ORDER IS THE DESIGN, and the plan did not state it.** `publish_generation` RECORDS the
> pointer, then performs the swap; the chain appends `PUBLISHED` after both. Only one of the two
> writes can be rolled back, so the order decides what a failure leaves: a swap that fails rolls the
> record back with it and the plane has claimed nothing, while swap-then-record can leave a cluster
> whose readers see a generation the plane has no row for — and an append-only plane has no repair
> path for the row it did not write. A record refused by 1055's ordering trigger means the swap
> never happens, so a stale publisher cannot overwrite a pointer that moved past it. The irreducible
> residue is stated in the function's own docstring: the cluster takes no part in the transaction,
> the window is one statement wide, it fails closed, and closing it properly needs a two-phase
> metastore protocol no probe has attested.
>
> **THE REFUSAL SURVIVED.** `test_publication_without_a_matching_attestation_is_still_refused` runs
> with the seam FULLY configured and an attestation probed on other engine versions: the chain
> declines rather than cannot, `swap.calls == []`, and no revision row exists. A publish step is not
> permission to publish — `select_publisher` still decides.
>
> **The AST guard was replaced, not deleted.** `_PERMITTED_EVENT_KINDS` is now
> `{PUBLICATION_REFUSED, PUBLISHED, RUN_FAILED}` as an EQUALITY in one place, read by both the
> per-module test and the src-wide sweep, so a fourth member is a deliberate edit to one line.
> `test_the_chain_appends_PUBLISHED_only_after_a_proven_selection_and_a_passed_gate` keeps the four
> AST routes (attribute, string — `MaterializationRunEvent` COERCES `event_kind`, so
> `event_kind="PUBLISHED"` is the same write — subscript/call/`getattr`) and adds the behavioural
> conjunction.
> Gates: full suite **11182 passed, 20 skipped** (11165/20 after D2); `-m eval` **73 passed**;
> ruff + mypy clean on every touched file.

### Task D4 — the surface says what happened (1½ days)

**Modify:** `api/routes/materialization_runs.py` — the status read reports the folded run status,
the terminal event, the refusal code and the published object name. **`refused` must render as an
outcome, not an error** (§0.0: it leaves the request `committed`).

**New (greenfield):** `frontend/src/…` — verified: `grep -rn 'materialization-runs\|materializationRun'
frontend/src/` returns **nothing**. There is no screen, no `api.ts` function and no type; the ingress
prefix was added ahead of any caller. So this is new UI, not a modification. It renders the server's
answer (no client policy, per the parent plan's D-2): `allowed_actions` / `blocked_actions` already
carry the four codes and their `next_step` strings. Honest absence rules apply — *"not published
yet"*, never an invented table name.

**Also close, or name as accepted:** DEFERRED-WORK **A.35** — `LEGAL_LIFECYCLE_TRANSITIONS` has no
`requested → failed` edge (`request_store.py`), so a misconfigured deployment (missing
`FEATUREGEN_MATERIALIZE_INVENTORY`, say) strands a request at `requested` **forever**, and the status
surface will show it as pending indefinitely. Either add the edge with the reconciler as its only
writer, or make D4's surface say "never accepted — check the lane configuration" and record the
choice here.

**Acceptance (tests):** route tests for each terminal; a Vitest render test per state; the standing
UI law (`ui-honest-absence-never-fabricate`) asserted on the empty state;
`test_a_request_that_was_never_accepted_is_legible`.

> **ACCEPTED `5bb19265` (2026-08-15).** The greenfield claim is re-verified: `grep -rn
> 'materialization' frontend/src/` found two unrelated string literals and no screen, no `api.ts`
> function and no type. So this is new UI.
>
> **THE A.35 DECISION: name the class, do NOT add the edge.** Recorded here and in
> `docs/DEFERRED-WORK.md` (the row is now ⚪ with the argument attached).
> `LEGAL_LIFECYCLE_TRANSITIONS` is untouched; `GET /materialization-runs/{id}` answers
> `outcome: "never_accepted"` for a `requested` request whose queue message is no longer claimable,
> and `run_status_reason` names the class, says what to check (`FEATUREGEN_MATERIALIZE_*` on the
> worker) and what to do (a FRESH idempotency key — §3.3: a re-run is a NEW request). **Three
> reasons the edge lost:** the reconciler already refuses to invent a verdict for this class
> (`NO_LEGAL_TERMINAL`) and the edge would convert that refusal into a terminalization on the
> evidence "the message is unreachable" — a judgement about work nobody claimed; the operator's
> actual complaint was a SURFACE defect ("pending indefinitely") and it is closed without a
> state-machine change; and terminalizing is a one-way door on a row whose whole value is being
> honest about never having been claimed. It is read from the QUEUE, not the request, because a
> `requested` row looks identical whether its message is waiting, being retried, or dead — and the
> message id is derived, so it is a primary-key lookup.
>
> **`refused` renders as an OUTCOME.** `_OUTCOMES` maps the four terminal kinds to a word, and the
> screen carries a three-way TONE (`good`/`neutral`/`bad`) rather than a boolean precisely so
> `refused` can be neutral: `PUBLICATION_REFUSED` is G-1's SUCCESS terminal (compiled, rendered,
> sealed, build PROVED, request `committed`), and a red badge would tell an operator their feature
> was rejected when nothing about it was. A Vitest case asserts the `data-tone` attribute, because
> the tone IS the claim and it is what a stylesheet paints.
>
> **Honest absence, and the one that matters.** `published_object === null` renders *"Not published
> yet"* and the test asserts that `sandbox_feature` appears NOWHERE on the page — the platform names
> a physical target for every group whether or not anything was published there, so showing it would
> hand an operator a table that does not resolve. Every other `null` renders the absence's own
> meaning, supplied by the caller because only the caller knows which silence it is; where the
> server sends a reason (`run_status_reason`) the screen prints the SERVER's sentence verbatim and
> composes none of its own. `refusal_code` is parsed off the terminal event's own detail rather than
> stored a second time, and is `None` whenever the terminal is not a refusal.
>
> **A defect found while writing it, and fixed here.** `read_active_revision` answers about the
> GROUP, and this route answers about one REQUEST — so a refused run would have reported the object
> an EARLIER run published, and an operator reading a refusal beside a live table name would
> reasonably conclude their run had put it there. `_published_by_this_run` scopes the pointer by
> `run_id`; a group whose newest revision belongs to another run reads as "not published yet" on
> this request, which is true of this request. What is currently published for the GROUP is a
> different question and needs its own surface.
>
> **What this task did NOT do.** The plan's note that `allowed_actions`/`blocked_actions` "already
> carry the four codes and their `next_step`" is about the ACTIVATION surface, not the run-status
> one: nothing on `materialization_request` carries them, and rendering them here would mean
> re-deriving an activation decision on a request that has already been accepted. Left where they
> are.
>
> **The screen is flag-gated, mirroring the server.** `VITE_MATERIALIZATION_RUNS`, call-time like
> `gateConsoleEnabled`: `FEATUREGEN_MATERIALIZE_ENABLED` is default-OFF and every
> `/materialization-runs` route 404s while it is off, so a reachable screen with an unreachable API
> behind it could only ever show an error. Flag-off, `#/materialization` parses like any unknown
> hash — absent, not broken.
> 6 new route cases in `tests/featuregen/api/test_materialization_runs.py` (42 total) and 8 Vitest
> cases in `MaterializationRunScreen.test.tsx`.
> Gates: full suite **11188 passed, 20 skipped** (11182/20 after D3); `-m eval` **73 passed**;
> frontend **810 passed / 40 files** (baseline 802); ruff + mypy clean on both touched Python
> files; `tsc --noEmit` and `oxlint` clean.

---

## 6. Phase E — proof

### Task E0 — the seam walkthrough gate (1 day) — **this plan's own acceptance test**

One test, in the default suite, that walks §0.5 items 1–4 against a real DB and a seeded catalog:
served candidate → reviewed v2 expectation → shadow work item → authored (recorded-fixture)
result → activation allows `execute_materialization` → `POST /materialization-runs` accepts →
the lane compiles → `run_l0` **passed** → the compiled read set **equals the frozen envelope's**.

Modelled on the parent plan's Task E0 (`bd43964d`), which is the precedent for a walkthrough gate
that runs in CI.

> **SUPERSEDED 2026-08-15 by the successor charter's increment 4 — kept because it was TRUE when
> written and its reasoning is what got the orchestrator built.** The replay-shaped v2 orchestrator
> now exists, so "→ authored (recorded-fixture) result →" IS a step of this walk; everything below
> about `materialize/resolve.py` and the v2 PAIR is still exactly true, which is why the exemplar
> still cannot be the recipe that COMPILES.
>
> **PLAN CORRECTION (E0, verified). "→ authored (recorded-fixture) result →" cannot be a step of
> this walk, and the reason is already recorded twice in this document.** A3's plan defect 1 says
> the live authoring worker is `replay_authoring.run_authoring` and that routing a v2 work item
> through it needs a *replay-shaped* v2 orchestrator nobody has written; A4 increment 2 then made
> the worker **terminalize every v2 work item** with `V2_AUTHORING_UNAVAILABLE` precisely so the
> absence could not become a false verdict. Downstream, `materialize/resolve.py` restores a v1
> `AuthoringResult` (`_restore_result`), and A3's plan defect 2 established that a v2 artifact is a
> *pair* (proposal + `FormulaOutputPolicyV2`) — there is no `TypedFormulaV2` and no path from one
> into admission at all. So the recipe whose governed decision §0.5 item 1 is *about*
> (`posted_debit_amount`, the only reviewed v2 exemplar) **structurally cannot be the recipe that
> compiles**, and E0 asserts item 2 up to the platform's own refusal rather than faking an author.

> **PLAN CORRECTION (E0, verified, and it is a DEFECT rather than a wording problem).**
> §0.5 item 4's *"`POST /materialization-runs` accepts"* is **unreachable for any option whose own
> eligibility fold names an outstanding requirement code**, and the served exemplar is one:
> `semantic_eligibility` appends `STATUS_POLICY_UNRESOLVED` unconditionally for an operand carrying
> a `status_policy_ref`, which `posted_debit_amount` has on two of its nine operands. The cause is
> one missing argument: `api/routes/materialization_runs.py:455` calls
> `assemble_current_activation_state(conn, frozen=…, snapshot_id=…)` **without `contract_id`**, so
> C3's contract-keyed `_requirements_closed` read (`semantic_option_decision.py:610`) always
> answers `False` on that route and `EXTERNAL_VALIDATION_OUTSTANDING` can only clear through the
> `frozen.validation_status != "DESIGN_CHECKED"` short-circuit at `activation_policy.py:195`.
> **NOT FIXED HERE, deliberately:** naming the contract needs an option → contract resolution that
> does not exist — `contract` is keyed by FEATURE identity, and nothing anywhere records which
> contract a given option decision produced — and inventing one inside a proof task is the exact
> failure mode §8's verify-then-write rule exists to stop. E0 asserts the defect in both directions
> instead, so it is a green test rather than a note.
>
> **CORRECTED TWICE BY SUCCESSOR 4 (§6.6c, 2026-08-15).** (1) This paragraph used to add *"…
> `contract_gate1_choice_revision` records the option but the two id spaces are different"*, which
> is false: a choice row's `option_id` and `semantic_option_decision.option_id` are the SAME id
> space (both are `considered.options_by_id`, `gate1.py:1404-1426` / `:1499-1507`), and the confirm
> route already relies on it. The missing thing was a RECORD on the contract side, not a
> translation between two id spaces. (2) The defect itself is now CLOSED: migration 1069 stamps
> the option key on the contract at mint and the route resolves along it, so
> *"`POST /materialization-runs` accepts"* IS reachable for the served exemplar — the flipped test
> proves exactly that, and pins the remaining boundary (a contract governed before 1069 carries no
> link, resolves to `None`, and still fails closed).

> **ACCEPTED `cb8788f4` (2026-08-15).** `tests/featuregen/api/test_seam_walkthrough.py` — one
> walk, in the DEFAULT suite, plus the two tests that keep it from being decoration.
>
> **THE WALK, and every step of it is a real surface.** Reviews recorded FIRST (real
> `recipe_review_event` rows, every role the recipe names, at its live canonical revision hash —
> so the SERVING fold measures them rather than a later re-read papering over a frozen `False`) →
> `POST /contract/recognitions` + `POST /contract/considered-set` on a real `posting_bank` catalog
> → three served option decisions for `posted_debit_amount`, all `bound`, all
> `has_reviewed_formula_expectation = true` (**§0.5 item 1**) → one durable
> `recipe_formula_shadow_work_item` at an EXACT candidate key carrying the frozen plan envelope
> and its 64-char hash, with `binding_plan`/`read_set` proved ABSENT from the provider payload
> (**item 2, capture half**) → the platform's own `declared_expectation_schema` read off that real
> row answers `formula-v2` (**item 2, authoring half — the honest boundary**) →
> `activation_decision(frozen, current, "execute_materialization").blockers == ()` on a real frozen
> row (**item 3**) → `POST /materialization-runs` **202** with the governed option as provenance →
> `process_materialization_once` compiles → `run_l0` **PASSED**, read back through
> `read_validation_reports` → the compiled read set **equals** the frozen envelope's, as a SET
> EQUALITY (**item 4**).
>
> **THE READ-SET ASSERTION IS AN EQUALITY, not a subset.** `compiled == frozen | additions`, where
> the four additions are pinned individually with their reasons (the spine's relation, its ordered
> key, its availability column — the population is a separate governed declaration, §4 — and the
> expression's source relation, which admission's check 7 already compared). A widening cannot hide
> inside a passing compile, and a new class of addition must be argued for in the open. Measured:
> the compilation authorizes exactly nine refs, the envelope's five plus those four.
>
> **THE GAP IS A GREEN ASSERTION, which is the point.** `assert declared !=
> AUTHORABLE_EXPECTATION_SCHEMA` carries the message *"if this is now authorable, the v2
> orchestrator landed and this walkthrough must be extended through it"*. The day someone writes
> the replay-shaped v2 orchestrator, E0 goes RED and says why. An omitted step would have said
> nothing.
>
> **⟵ THAT DAY WAS 2026-08-15, AND THE TRIPWIRE DID ITS JOB.** The successor charter's increment 3
> made the worker route a v2 work item to `run_authoring_v2_replay`, this assertion went RED with
> its own message, and increment 4 extended the walk through the real authoring. The gap assertion
> is now its inversion (`declared in AUTHORABLE_EXPECTATION_SCHEMAS`).
>
> **MUTANT PROOF, THREE, EACH RUN AND EACH CAUGHT.** (a) `divergence = None` in `ir.compile_ir` —
> the envelope check disabled — makes `test_a_narrowed_envelope_breaks_the_walk_INSIDE_the_lane`
> fail with `status='completed'` where it demands `refused`, so the check is proved LIVE IN THE
> LANE and not only in B3's unit tests. (b) `declared_expectation_schema` returning
> `AUTHORABLE_EXPECTATION_SCHEMA` unconditionally fails the walk at the boundary assertion.
> (c) Deleting `posted_debit_amount` from `RECIPE_FORMULA_V2_EXPECTATIONS` — the "un-review the
> exemplar" mutant this task was briefed to try — is **unreachable**: `validate_v2_registry()` runs
> at import and raises `RecipeContractError` ("authors readiness 'FORMULA_AUTHORABLE' but its own
> declarations fold to 'FORMULA_BLOCKED'"), so the registry's own guard is strictly stronger than
> any test. Recorded rather than worked around.
>
> **WHAT IS SEEDED, EACH NAMED — the C3 milestone's vocabulary, deliberately.** The gold/provider
> evaluation (`_gold_evaluation_recorded`, the documented hook; no store exists) and snapshot
> freshness (`compare_snapshot_to_current`; snapshot minting rides the generation pipeline, not a
> test) — the same two C3 seeded, for the same reasons. `run_l0`'s VERDICT is injected at
> `chain.run_l0` exactly as the whole materialize suite does, over a project that is really
> rendered, sealed and materialized on disk — the real interpreter is E1's, and `pyspark`/`kedro`
> are not dependencies of this platform. The one option decision this test WRITES rather than
> reads is the C3-milestone-shaped row (a `DESIGN_CHECKED` idea), and it carries the served run's
> own revision id, revision hash, plan envelope and metadata snapshot — everything about it except
> the validation status is the served run's, and the validation status is the subject of the
> correction above.
>
> **`_seed_work_item` gained one keyword, `binding_plan=None`**, so a work item can carry the
> frozen envelope (B2 / migration 1068). The default is the pre-1068 shape every existing caller
> already relies on, which
> `test_a_run_without_an_envelope_compiles_exactly_as_before` pins.
>
> Gates: full suite **11191 passed, 20 skipped** (baseline on `15cff93f` was 11188/20 — the three
> new tests and nothing else moved); `-m eval` **73 passed**; ruff clean on both touched files;
> mypy unaffected (`[tool.mypy] files = ["src"]`, and this task touches no source module).

### Task E1 — the governed contract materializes (1½ days)

Extends E0 through §0.5 items 5–7 in the JVM gate (`make l0-gate` sibling), not the default suite:
`prepare_run` → `run_l1` → `submit` → publish → the object is queryable.

> **PLAN CORRECTION (E1, verified). "→ the object is queryable" cannot be asserted by any gate on
> any developer machine, and calling it item 7 in the same breath as items 5–6 hides that.** §0.5
> item 7 is *"`sandbox_feature.<group>` returns rows on the kind cluster"* — it needs a metastore
> to ask and an adapter to ask it with, and D3's acceptance row records that **neither
> `MetastoreMetadata` nor `PublicationSwap` has an implementation anywhere in `src/`**. The JVM
> gate can prove everything the control plane can state without a cluster (the publish step ran,
> the swap was handed the object/generation/columns, the durable pointer exists, the terminal is
> `PUBLISHED`, `run_status` folds to it) — on an artifact whose build is genuinely verified, which
> is the part only this gate can add. The last hop is E2's deployment work and an operator action.

> **ACCEPTED `812c0cef` (2026-08-15). RAN LOCALLY, GREEN.** Two tests appended to
> `tests/featuregen/materialize/l0_gate.py` — the JVM gate, not the default suite, because
> `pyspark`/`kedro` are deliberately not dependencies of this platform.
>
> **`test_the_chain_SUBMITS_the_rendered_project_into_a_REAL_kedro_session` (§0.5 item 5).** The
> chain's OWN `prepared.parameters` — the ones `prepare_run` resolved, not a hand-assembled dict —
> are handed to the production `LocalClusterSubmitter` through `RunExecution` and really launch the
> rendered project in the interpreter that has the engines. The existing hook test proves the gate
> fires for a caller who assembles parameters by hand; this proves the CHAIN clears it. The run
> then dies on `banking.transactions` not existing, which is the truth of a machine with no Hive
> and no data — so the assertion is that it did **not** die at the parameter gate
> (`RUN_PARAMETERS_MISSING` absent, `returncode is not None`), that `run_l1` PASSED first, and that
> the run stops at `SUBMIT` with the returncode in the event detail. A run refused before it
> started would prove nothing about submission, and that distinction is the whole content of the
> test.
>
> **`test_the_governed_contract_REACHES_A_PUBLISHED_TABLE` (§0.5 items 5–7, to the honest limit).**
> The full ladder on a BUILD-VERIFIED artifact: the L0 verdict is the real one from a real
> interpreter, read back off the durable record, and the generation that publishes is therefore a
> generation whose project genuinely imports and constructs its kedro pipeline. Terminal
> `PUBLISHED`, lifecycle `COMMITTED`, `run_status` folds to `PUBLISHED`, `read_active_revision`
> names the object (migration 1055), and the swap's recorded call carries that same object, the
> generation id and the feature column. The collected suite proves this ladder with the verdict
> injected; this is the same ladder with the verdict earned.
>
> **THE FAKES ARE TEST-SCOPED AND THE HEADER SAYS SO IN ONE PLACE.** `_G2Metastore` and `_Swap`
> are defined in `test_chain.py`, never in `src/`, and the gate's new section header states why
> that is the honest vehicle rather than a shortcut: no adapter exists to be real, so a "real" one
> here would be a fake with a better address. What the gate proves is that the chain COMPOSES
> through those seams and publishes; writing the adapters is E2's deployment work.
>
> **THE GATE RAN ON THIS MACHINE.** `.venv-artifact` was built from the golden project's own
> `requirements.lock` (kedro 0.19.9 / kedro-datasets 4.1.0 / pyspark 3.5.1, plus `hdfs` + `s3fs`
> per A.32) against Temurin 17. Both new tests take `the_declared_environment`, so under
> `.venv-l0-modern` or the kind image they SKIP with the engine disagreement named — a build proof
> is a claim about one environment and A.42 made `run_l0` refuse to make it anywhere else.
>
> **A DEFECT THIS GATE FOUND IN ITS OWN FIRST DRAFT, kept as an assertion.** The publish test first
> demanded `outcome.stopped_at is None` and went RED: `stopped_at` names the stage a run REACHED,
> and for a published run that is `ChainStage.PUBLISH`. Corrected to assert that stage by name
> rather than deleted, because a run that stopped anywhere EARLIER and still claimed `PUBLISHED` is
> the one shape this ladder must never produce.
>
> Gates: the JVM gate **RAN LOCALLY AND IS GREEN** —
> `FEATUREGEN_L0_PYTHON=$PWD/.venv-artifact/bin/python PYSPARK_PYTHON=… PYSPARK_DRIVER_PYTHON=…
> uv run pytest tests/featuregen/materialize/l0_gate.py -q` → **7 passed** (5 pre-existing + the 2
> new) against kedro 0.19.9 / kedro-datasets 4.1.0 / pyspark 3.5.1 on Temurin 17.0.19;
> `spark_semantics_gate.py` re-run on the same interpreter, unchanged. **Only the artifact line was
> exercised:** `.venv-l0-modern` was not built on this machine, and under it both new tests SKIP by
> construction (`the_declared_environment`) rather than assert anything — `make l0-gate` builds it
> and runs all four combinations. Full suite and `-m eval` are unaffected: this file is not named
> `test_*` and the default suite never collects it (re-confirmed after the edit).

### Task E2 — kind cluster acceptance (1 day engineering + **operator action**)

**Two real deployment blockers must be closed FIRST, and they are engineering, not operations:**

1. ~~**`deploy/kind/k8s/25-worker.yaml` carries no `MATERIALIZE` env at all**~~ — **WRONG, see the
   correction below. It carries all of it, through `envFrom`.**
2. **`FEATUREGEN_MATERIALIZE_INVENTORY` has no usable value in-pod.** The only inventory in the repo
   is `conf/environments/hdfc-local-inventory.yml`, which `load_inventory` **refuses by design**
   (`engine_versions.hive` is null, `tables: {}` — it is a template, and DEFERRED-WORK `:230` says
   Task 0 owns filling it against the live cluster). It is not copied into the image. The other three
   settings do have usable values since the followups branch added `/opt/kedro-venv` with
   kedro + pyspark. **This is Task 0 of the codegen program, unstarted, and it gates E2 absolutely.**

> **PLAN CORRECTION (E2, verified, and following the task as briefed would have SHIPPED the defect
> it describes.** Blocker 1 is false. `25-worker.yaml:65` declares
> `envFrom: [configMapRef: {name: backend-config}]` — the *same* ConfigMap `20-backend.yaml`
> defines, the one that carries `FEATUREGEN_MATERIALIZE_ENABLED: "0"` and the four commented
> companion settings. Parsed both files to confirm it: each Deployment's only `env:` entry is
> `ANTHROPIC_API_KEY`, and both take the whole ConfigMap through `envFrom`. So the flag is ONE
> edit for both processes and there is no second place to flip.
>
> **And adding the briefed block would have created the failure it was meant to prevent.**
> Kubernetes gives `env` precedence over `envFrom`. A mirrored
> `FEATUREGEN_MATERIALIZE_ENABLED: "0"` in `25-worker.yaml` would silently pin the WORKER off
> after an operator flipped the ConfigMap on — the backend accepting triggers that nothing ever
> claims, which is word-for-word the failure blocker 1 describes. What landed instead is the
> documentation that makes the inheritance and the trap legible at both ends, plus one correction:
> the backend ConfigMap's `PROJECT_ROOT` note ("no volume is mounted there… a restart takes every
> sealed tree with it") is written from the backend's perspective and is about the **worker's**
> ephemeral disk — the backend never writes a tree.

> **ACCEPTED `828fcfa4` (2026-08-15). ENGINEERING HALF ONLY; EVERY CLUSTER STEP BELOW NEEDS AN
> EXPLICIT GO.** `deploy/kind/k8s/25-worker.yaml` gains the materialization block as
> **documentation, not duplication** (the correction above says why): the worker is the only
> process that compiles; it already inherits the whole `FEATUREGEN_MATERIALIZE_*` block through
> `envFrom`; an `env:` entry for any of those names must never be added; a ConfigMap edit is not a
> restart, so BOTH Deployments must be rolled; and `PROJECT_ROOT` describes this pod's ephemeral
> disk. `20-backend.yaml` gains the matching note at the flag and the corrected `PROJECT_ROOT`
> paragraph. **No `kubectl`, no apply, no deploy — these are file changes.** Both files re-parsed
> with `yaml.safe_load_all` after editing.

#### What codegen Task 0 must produce, field by field — the operator brief

The inventory is the one absolute gate and it **cannot be written from this repository**: every
value below is an observation of a live cluster. `load_inventory` is total — there is no partial
load — so the file is refused until all of it is there. Reproduced against
`conf/environments/hdfc-local-inventory.yml` on `15cff93f`, the first refusal is verbatim:

> `engine_versions.hive is missing or null. Every runtime version is required: an unpinned
> dependency resolves to whatever the index offers on the day…`

Task 0 must capture and write, each one satisfying a validation that currently refuses:

- **`environment_id`** — non-blank text. Must equal the `environment_id` every capability
  attestation is keyed on (§10.3), or the probe's evidence describes a different environment.
- **`captured_at`** — non-blank text, `_required` + `_text`. Never enters an identity hash; it
  exists so *"this inventory is four months old"* is answerable.
- **`engine_versions`** — **all eight**, each non-null and non-blank:
  `hive`, `spark`, `metastore`, `python`, `java`, `pyspark`, `kedro`, `kedro_datasets`.
  Four describe the cluster (`hive`/`spark`/`metastore` are the attestation key; `java` is what a
  Spark version is only meaningful against); four are what §7's generated project pins itself to.
  `kedro_datasets` is separately versioned from `kedro` and is what `spark.SparkHiveDataset`
  resolves out of — a lock naming only `kedro` leaves the class that reads every governed source
  table unpinned.
- **`logical_schema_map`** — one entry per governed logical table ref (`source::schema.table`)
  whose `graph_node.schema_name` is NULL. Consulted only in that case; if neither yields a schema
  the compilation refuses `PHYSICAL_SCHEMA_NOT_RESOLVED` rather than defaulting to `public` and
  reading a different table than the catalog governs. The uploads are schema-flattened, so these
  lines are expected to be needed.
- **`tables`** — one entry per governed table, keyed `SCHEMA.TABLE` exactly as the cluster spells
  it (case is preserved; two keys that fold to the same identifier are refused). Each entry needs
  **every** key present:
  - `partition_columns` — ORDERED `[name, physical type]` pairs; the partition path is built from
    the order. A verified-unpartitioned table writes `null`. **`[]` is refused** — "we do not know
    how this is partitioned" is not "scan it".
  - `partition_mapping` — **DECLARED, never captured**, one of exactly five kinds
    (`event_time_partition`, `availability_partition`, `static_snapshot`, `full_scan`,
    `verified_unpartitioned`). `null` loads and then refuses at compile with
    `PARTITION_MAPPING_NOT_DECLARED`. A kind outside the closed set is refused at load, as is a
    `transform` outside `{date_iso, date_compact}`. **This field is identity-bearing** — correcting
    it later invalidates sealed artifacts.
  - `columns` — ORDERED `[name, physical type]` as the metastore prints them, **not normalised**:
    `varchar(150)` is not `string`, and §6's adapter is what decides what the difference means.
  - `location` — the physical path.
  - `rewritten_in_place` — a boolean no metastore knows: it is a statement about how the feed
    operates, and it is why an unpartitioned mutable table's snapshot is not content-addressed.
- **Getting it into the pod.** The file is not in the image and no volume mounts it. Task 0's
  output must also be delivered — a ConfigMap or Secret mounted at the path
  `FEATUREGEN_MATERIALIZE_INVENTORY` names, or a build-time copy. Nothing in the repo does this
  today.
- **Not consumed, and must not be mistaken for done.** The `engines:` block is *declared for
  later*: `load_inventory` tolerates the key without reading it, `ClusterInventoryV1` has no typed
  field for it, and `SOURCE_ENGINE_UNSUPPORTED` exists in `codes.py` with nothing raising it.

#### The operator runbook — every step needs an explicit go

Nothing below may be run without the user saying so, each time: it is a live cluster, real spend,
and a durable capability attestation that outlives the run that produced it.

1. **Migrations, backend-first.** `1055` (active-revision pointer, G-3), `1066`
   (`semantic_option_decision.binding_plan`), `1067` (`materialization_request` option link),
   `1068` (`recipe_formula_shadow_work_item.binding_plan`). The backend's init container runs
   `python -m featuregen migrate` before serving, so rolling the backend image applies them; the
   worker's init container WAITS for the same schema and does not compete. All four are append-only
   or additive-nullable and were each audited against a POPULATED seeded legacy shape.
2. **Roll the backend, confirm `/health`, and only then the worker.** A worker on the new image
   against the old schema is the one ordering that has no repair path.
3. **Stand up the SQL endpoint, and prove it through the production adapter** — SUCCESSOR 3, §6.6b.
   `bash deploy/kind/sandbox/up.sh` builds the image, initialises the Hive schema, applies
   `40-spark.yaml` and then `41-spark-thrift.yaml` (a `spark-thrift` ClusterIP Service on 10000, the
   same image and the same catalog). The order inside the script is load-bearing: the server opens
   its metastore connection while starting, so it goes up AFTER the schema init.
   **Two things must be delivered into the backend/worker pods first, because neither image has
   them:** `pip install "PyHive[hive]" thrift` (the control plane declares no engine client — a
   missing driver is a `ValueError`, which the lane treats as DETERMINISTIC, so it fails the request
   and dead-letters rather than retrying), and `scripts/thrift_smoke.py` itself
   (`Dockerfile.backend` copies only `src/`, so `kubectl cp` it). Then run the smoke test — it
   drives the SAME `metastore_sql` adapter the worker uses and prints one typed outcome per L1
   question, so first contact happens through the production code path rather than a beeline
   session. **Expect question 3 to report READ SCOPE UNANSWERABLE and the script to exit 0**: see
   the GRANT subsection below for why that is the correct answer here and not a misconfiguration.
4. **Land Task 0's inventory in the pod** and confirm it LOADS —
   `python -c "from featuregen.materialize.inventory import load_inventory;
   load_inventory('<path>')"` inside the pod. A file that exists but does not load fails exactly
   like a missing one: `load_inventory` raises before the lane accepts anything.
5. **Edit the ConfigMap once** — `kubectl -n featuregen edit configmap backend-config` — setting
   `FEATUREGEN_MATERIALIZE_ENABLED: "1"` **and** uncommenting all four companion settings together.
   The lane resolves them only when a job is claimed; a missing one is retryable, so the job burns
   its 12 attempts and dead-letters, and because the lane never claimed the request it is left
   stranded at `requested` with nothing queued behind it — re-triggering then needs a FRESH
   idempotency key, and the stranded row can never be closed at all (DEFERRED-WORK A.35, which D4
   chose to surface as *"never accepted — check the lane configuration"* rather than close).
6. **Roll BOTH Deployments.** A ConfigMap edit does not restart a pod, and a pod started before the
   edit keeps the old environment for its whole life. **A rolled pod loses a `pip install` done into
   the running container** — the PyHive step above is per-pod-lifetime until it is baked into the
   image, so re-do it after this roll or the first claimed job dead-letters on a missing driver.
7. **Run the publication probe** (`materialize/probe.py`, D2's driver) against the environment and
   record the attestation. It is keyed on `environment_id` + mechanism + the exact
   `hive`/`spark`/`metastore` triple: publication is refused `CAPABILITY_UNPROVEN` until one
   exists, and an attestation probed on other engine versions is refused too — drift is unproven,
   not failed.
8. **Trigger ONE governed feature** and read `GET /materialization-runs/{id}` (D4's surface). The
   expected terminals, each an outcome rather than an error: `PUBLICATION_REFUSED` if the probe has
   not run, `RUN_FAILED` at `PREPARE_RUN` if the execution block is unset or an adapter refuses,
   and `PUBLISHED` only when both are closed.
   **CORRECTED (SUCCESSOR 3):** this step used to say the G-2 adapters were absent — "no
   `MetastoreMetadata` implementation exists in `src/`; writing them is E2's remaining engineering
   and it is not done". **They exist**: SUCCESSOR 2 wrote `metastore_sql.SqlMetastoreAdapter` and
   `publish_sql.SqlPublicationSwap` (§6.6). What now stops a run on kind is different and narrower —
   `can_read` is unanswerable against a Spark endpoint, so `PREPARE_RUN` fails at L1's THIRD
   question rather than for want of an implementation. See the GRANT subsection.
9. **Read the table** — `sandbox_feature.<group>` — and compare it with the run's reported
   published object. This is §0.5 item 7 and it is the only step that can close it.

#### The GRANT model — what `can_read` actually needs, and why kind cannot supply it

`can_read` issues, per role, exactly:

```sql
SHOW GRANT ROLE `<role>` ON TABLE `<schema>`.`<table>`
```

and reads the **`privilege` column by NAME** from the result, passing only if it holds `SELECT` or
`ALL` (`_READ_PRIVILEGES`). `INSERT` on a table is not read access, and "holds some grant" is a
different question from "may read". So on an endpoint that supports SQL-standard authorization, the
admin grants **per table, not per schema** — a schema-level grant is not what this statement asks
about — over exactly L1's read set:

```sql
SET ROLE ADMIN;
CREATE ROLE featuregen_reader;
GRANT ROLE featuregen_reader TO USER featuregen;          -- the METASTORE_PRINCIPAL
GRANT SELECT ON TABLE risk.transactions TO ROLE featuregen_reader;   -- one line PER TABLE
SHOW GRANT ROLE featuregen_reader ON TABLE risk.transactions;        -- what can_read will run
```

and the endpoint itself must carry, verified as real class names in `hive-exec`:
`hive.security.authorization.enabled=true`,
`hive.security.authorization.manager=org.apache.hadoop.hive.ql.security.authorization.plugin.sqlstd.SQLStdHiveAuthorizerFactory`,
`hive.security.authenticator.manager=org.apache.hadoop.hive.ql.security.SessionStateUserAuthenticator`,
`hive.users.in.admin.role=<the named admin>`, `hive.server2.enable.doAs=false`.

> **NONE OF THIS CAN BE RUN AGAINST THE KIND SANDBOX, and the reason is the engine rather than the
> configuration.** The endpoint there is a Spark Thrift Server, and **Spark's SQL grammar carries a
> rule named `unsupportedHiveNativeCommands` whose members include `GRANT`, `REVOKE` and
> `SHOW GRANT`** — it parses them only to reject them, as
> `[_LEGACY_ERROR_TEMP_0035] Operation not allowed: SHOW GRANT`, and never consults Hive's
> authorization plugin even though `SQLStdHiveAuthorizerFactory` is on the classpath. So every
> statement in the block above fails there, and `can_read` correctly raises
> `MetastoreReadScopeUnanswerable`.
>
> **And it cannot be fixed by swapping in a HiveServer2**, which is the obvious next thought: the
> publication swap is `CREATE OR REPLACE VIEW … AS SELECT … FROM parquet.`<path>``
> (`publish_sql.py:134`), and `parquet.`<path>`` is Spark-only path-as-relation syntax that Hive
> cannot parse. **The swap requires Spark; `can_read` requires Hive's Driver; no single engine
> available here has both.** That is a genuine seam-level constraint, recorded rather than worked
> around, and it means **L1 cannot pass on kind** — the sandbox proves the endpoint, the transport,
> the classification and the swap, and leaves read-scope to an environment with an authorization
> model. §0.5 item 7 is closable only if L1's read-scope question is satisfied some other way, which
> is a governance decision this plan does not take.

#### What Task 0 can capture FROM THE ENDPOINT, and what it cannot

The inventory needs **all eight** `engine_versions`. The endpoint answers three of them, and being
precise about which matters because the other five are venv facts that an operator standing at a
beeline prompt cannot see:

| field | from the endpoint? | how |
|---|---|---|
| `spark` | **YES** | `SELECT version()` — the `SparkVersion` expression; returns version + git revision |
| `java` | **YES** | `SELECT reflect('java.lang.System','getProperty','java.version')` — `CallMethodViaReflection`; `reflect`/`java_method` are registered builtins |
| `hive` | **YES** (client) | `SET spark.sql.hive.metastore.version` — the Hive **client** Spark uses (2.3.9 on the sandbox), which is what the attestation key means here |
| `metastore` | **NO** | the schema version lives in the metastore DB's own `VERSION` table; ask Postgres, not the endpoint. On the sandbox there is no separate metastore *service* — Spark's built-in client talks JDBC — so this describes the schema `up.sh` initialised |
| `python` | **NO** | the interpreter that RUNS the artifact (`FEATUREGEN_MATERIALIZE_SUBMIT_PYTHON`). The endpoint is a JVM and has no opinion about it |
| `pyspark` | **NO** | a package version in the run venv. It SHOULD equal `spark`, and capturing it from the endpoint would assume the equality the field exists to check |
| `kedro` | **NO** | venv only — `pip show kedro` in the submit interpreter |
| `kedro_datasets` | **NO** | venv only, and separately versioned from `kedro`; it is what `spark.SparkHiveDataset` resolves out of |

The `tables` entries are a different matter and the endpoint IS the right source for most of them:
`columns` come from `DESCRIBE` **unnormalised** (`varchar(150)` is not `string`), `partition_columns`
from `SHOW PARTITIONS` plus `DESCRIBE`'s partition section in the metastore's own ORDER, and
`location` from `DESCRIBE FORMATTED`. **`partition_mapping` and `rewritten_in_place` are NOT
capturable from any endpoint** — the first is DECLARED and identity-bearing (correcting it later
invalidates sealed artifacts), the second is a statement about how the feed operates that no
metastore knows.

**What this plan may then claim, and no more:** *"one governed contract materializes end to end
through Kedro on kind; publication is proven for that environment at those engine versions."*

---

## 6.5 Successor: the replay-shaped v2 orchestrator

**PROVENANCE.** This section is not part of the plan as written. It is the successor charter A3's
acceptance row *recorded* (plan defect 1): *"Routing a v2 work item through the live worker needs a
replay-shaped v2 orchestrator plus v2 siblings of `recipe_authoring.recipe_expectation_validator`,
`recipe_authoring.recipe_tool_runner` and `FrozenRecipeReadContext.formula_facts` … That is its own
task."* A4 increment 2 then made the live worker terminalize every v2 work item
(`V2_AUTHORING_UNAVAILABLE`) so the absence could not become a false verdict, and E0 asserted that
refusal as a **green tripwire** — *"the day someone writes the replay-shaped v2 orchestrator, E0
goes RED and says why."* Four increments, each its own commit, each closing one of those.

> **SUCCESSOR EXECUTION (2026-08-15) — INCREMENT 1: THE v2 SEAMS. ACCEPTED `ff526276`.** The three
> things A3's defect 1 named, built as SIBLINGS with the v1 half left byte-frozen:
> `FrozenRecipeReadContext.formula_facts_v2`, `recipe_expectation_validator_v2` and
> `recipe_tool_runner_v2` (all in `formula/recipe_authoring.py`, beside their v1 originals).
>
> **The facts reader is keyed by `logical_ref`, and the test proves it through the REAL resolver.**
> A3's defect 4 said a v1-keyed bundle "resolves every operand to empty facts and assembles a policy
> out of nothing"; `test_the_v2_facts_bundle_is_keyed_by_ref_not_by_body_path` feeds both keyings to
> `resolve_output_v2` and shows the path-keyed one returning a monetary output with **no currency at
> all**. The reader also reads the SECOND operand (v1 has no such notion) and every grain key, and
> returns `(facts_by_ref, authority_failures)` — a governed read that failed CLOSED is attributed,
> never silently empty.
>
> **The slot→field mapping is IMPORTED from `authoring_v2`, not restated.** `_OPERAND_FACT_FIELDS`,
> `_GRAIN_FIELD`, `_fact_text` and `_hard_failure` are the live v2 reader's; a frozen reader that
> disagreed with the live one about what a fact IS would be a second authority. This needed one
> additive type change in `authoring_v2`: the two projections now take a read-only `GovernedRead`
> Protocol instead of the concrete `OperationalValue`, because both readers are FROZEN dataclasses
> and a mutable protocol attribute is invariant and would match neither. No new mypy errors (the
> four `ExprFacts` ones on the v1 method are pre-existing, measured by HEAD-swap).
>
> **The validator covers all twelve v2 expression keys** — the seven v1 preserves plus
> `second_operand_ref`, `aggregation_argument`, `authority_refs`, `term_name`, `term_sign` — over
> every body shape, with `offset_periods` added to the window comparison (v1's expected-policy
> projection lists seven window keys and a shifted window would ride through unnamed). The
> canonical expression paths come from `recipe_formula_contracts_v2`'s ONE vocabulary
> (`EXPRESSION_PATHS_BY_FINAL_OPERATION` / `composite_expression_path`), never a second list, and a
> degraded expectation is re-checked against the `operation_rule` table so it can never be
> "preserved" by an equally degraded proposal (`EXPECTATION_SHAPE_INVALID`).
> `test_the_v1_validator_still_refuses_a_v2_proposal_which_is_why_v2_needs_its_own` states the
> reason the sibling exists rather than assuming it.
>
> **Two things the code said that the charter did not anticipate, both kept:**
> (a) **`recipe_tool_runner_v2` was not optional.** `list_supported_operations` answers out of the
> v1 `AggregateFunction` enum and `validate_draft_formula` runs `parse_proposal_v1` — under a v2 run
> the first names a grammar the model is not authoring in and the second calls a *valid* v2 draft
> `invalid`, teaching the model to abandon a correct proposal. Both are asserted, including the v1
> runner's wrong answer for the same draft. The ref gate and the frozen-context read are v1's,
> unchanged. The v2 verdict is stamped `operation_grammar_version`, not v1's
> `capability_policy_version`: `capability_v2` declares no policy-version constant and a tool result
> is not where to invent one.
> (b) **A four-blank `authority_refs` block cannot be authored at all** —
> `AuthorityRefsV2.__post_init__` refuses it (*"authority_refs with every ref blank is a lie — omit
> the block instead"*), so the validator's `None`-vs-`{}` distinction is untestable through a parsed
> proposal. The projection keeps the distinction anyway, because a stored expectation is a DICT that
> nothing re-parses, and the test asserts the stronger law it found instead of the one it went
> looking for.
>
> 35 new cases in `tests/featuregen/formula/test_recipe_authoring_v2.py`. Gates: full suite
> **11226 passed, 20 skipped** (baseline on `8298f68e` was 11191/20 — the 35 new tests and
> nothing else moved); `-m eval` **73 passed**; ruff clean, no new mypy errors on the touched files.


> **SUCCESSOR EXECUTION (2026-08-15) — INCREMENT 2: `run_authoring_v2_replay`. ACCEPTED `877e587a`.
> SHIPPED, UNVERIFIED AGAINST A LIVE PROVIDER.** `formula/replay_authoring_v2.py` — the
> replay-shaped sibling of the orchestrator production actually runs, carrying every seam A3's
> defect 1 enumerated: checkpoint/replay, frozen configuration, proposal validator, tool runner,
> facts reader, deterministic `authoring_run_id`, critic metadata loader, progress callback, lease
> fence.
>
> **The stage vocabulary is v1's, and that is not a style choice.**
> `replay_trace._verify_stage_transition` ENFORCES `AUTHOR_TURN_n → AUTHOR_PROPOSAL_PARSED →
> EXPECTATION_VALIDATED → CRITIC_COMPLETED → OUTPUT_POLICY_RESOLVED → TERMINAL`, so a v2 run that
> invented its own stage names could never be resumed. Every resume point, idempotency key and
> fence-guarded write is v1's; the grammar inside them is v2's throughout. **No new stage names, no
> new table, no migration.**
>
> **`freeze_current_configuration_v2` had to exist, and the test says why.** The charter's line —
> *"frozen_configuration must hash the V2 instruction + schema ids"* — is not a preference:
> `freeze_current_configuration` hard-codes `AUTHOR_TURN_SCHEMA_ID` / `AUTHOR_TURN_V1_SCHEMA` and
> the v1 grammar/fold material. `test_a_v1_frozen_configuration_is_DRIFT_for_a_v2_run` pins all
> four differences (configuration hash, prompt id, output schema id, grammar hash) and proves a
> v1-frozen work item can never author a v2 formula. The ENVELOPE shape is deliberately unchanged,
> so `load_frozen_configuration_json` — which only re-hashes stored bytes — reads either generation
> without knowing which it holds. The critic contract stays v1's, because A3 widened `critic` to
> review both generations from one closed context and a second identity for the same bytes would
> invent a difference that does not exist.
>
> **The v2 grammar material includes the OPERATION RULE TABLE**, which v1's has no analogue for. It
> is the v2 semantics (operand / second-operand / argument requirements, additivity, result kind)
> and it is also what `recipe_tool_runner_v2` answers the model out of, so a rule-table edit that
> would change what the model is told cannot slip past a frozen work item. The disposition hash
> covers BOTH `derive_disposition_v2` and `_fold_v2`; v1 hashes only its constructor, and that gap
> is not worth copying.
>
> **THREE THINGS THE CODE SAID THAT THE CHARTER DID NOT, each reproduced:**
>
> 1. **A REJECTED v2 replay run writes `failed`, where `authoring_v2` writes `completed`.**
>    `replay_authoring` maps invalid→`failed`, unsupported→`completed`, technical→`failed`; the
>    non-replay v2 orchestrator maps only TECHNICAL_FAILURE to `FAILED`. The charter said *"the SAME
>    orchestration laws as v1's — do not invent different semantics"*, so this module follows the
>    store it writes to and `run_status` stays meaningful for a recovering worker. Both halves are
>    asserted, and the divergence is stated in the module docstring rather than smoothed over.
> 2. **A v1-declared body cannot produce `invalid_formula` THROUGH A PROVIDER**, so the test that
>    claimed it was wrong. The v2 wire schema pins `formula_schema_version` to 2 (A3 recorded this):
>    a v1 body fails RESPONSE validation, the loop never gets a proposal, and the run ends
>    **TECHNICAL** — which says nothing about the grammar. The orchestrator's own version guard
>    stays as defence in depth for a non-provider caller, and
>    `test_the_wire_pins_the_version_so_a_v1_body_never_becomes_a_false_grammar_verdict` asserts
>    both halves. The same correction applies to any malformed body: reaching the REJECTED arm needs
>    a proposal the WIRE admits and `validate_semantics_v2` refuses (a grain key naming a table).
> 3. **`formula_authoring_trace_event` is WRITE-ONCE at the database.** The planned tamper test
>    (`UPDATE` the terminal's recorded hash, replay, expect a refusal) cannot run: a trigger raises
>    *"records are write-once: UPDATE is not allowed"*. That is strictly stronger than the test
>    intended, so it is asserted as such and the hash check is driven at `_restore_terminal_result`
>    with a fabricated checkpoint instead.
>
> **The facts-reader seam's failure mode is worse than "a missing currency", and the test measures
> the real one.** Hand the orchestrator a v1 PATH-keyed bundle and the operand resolves to empty
> facts, so the output TYPE has no governed authority either: the run comes back
> `external_requirement` with **no authoritative policy at all**, not merely a currency-less one.
>
> **⟨LLM⟩ THE LIVE HALF IS DEFERRED, HONESTLY** — A3's deferral, unchanged. Every run in the suite
> is a `FakeLLM` recorded fixture; billing is exhausted (D-10). What is proven is the SEAM. What is
> NOT proven is that a real model held to `AUTHOR_INSTRUCTION_V2` emits a usable v2 proposal.
> `test_a_billing_refusal_is_technical_never_a_capability_or_grammar_verdict` is what makes the
> deferral safe: a `PROVIDER_NON_RETRYABLE` refusal folds TECHNICAL with `capability_status="ok"`
> and `structural_status="ok"` — a payment problem can never become a durable statement about the
> v2 grammar (the `3219a209` precedent, now restated at the replay layer too).
>
> **The v1 replay orchestrator is pinned BYTE-IDENTICAL** by a source digest (`96c3dbc3…`), the way
> A4 increment 1 pinned the frozen v1 egress arm. If it changes, that is a separate argued change,
> not a side effect of the v2 work.
>
> 19 new cases in `tests/featuregen/formula/test_replay_authoring_v2.py`. Gates: full suite
> **11245 passed, 20 skipped** (11226/20 after increment 1 — the 19
> new tests and nothing else moved); `-m eval` **73 passed**; ruff + mypy clean on both touched source files.

> **SUCCESSOR EXECUTION (2026-08-15) — INCREMENT 3: THE WORKER ROUTES BY DECLARED SCHEMA.
> ACCEPTED `e03392d8`.** `recipe_formula_worker` now authors BOTH generations and the work item's own
> declaration chooses. `formula-v1` → `run_authoring`, `verify_frozen_configuration`,
> `recipe_expectation_validator`, `recipe_tool_runner`, `formula_facts`. `formula-v2` →
> `run_authoring_v2_replay`, `verify_frozen_configuration_v2`, `recipe_expectation_validator_v2`,
> `recipe_tool_runner_v2`, `formula_facts_v2`. Five seams, paired: a v2 proposal validated by the
> v1 validator, or resolved over a body-path-keyed bundle, would produce a confident verdict out of
> the wrong evidence.
>
> **`V2_AUTHORING_UNAVAILABLE` IS DELETED, cause and code together** — the same discipline D3
> applied when it deleted D0's guards. A4 increment 2 introduced it because *"nothing has yet built
> the replay-shaped v2 orchestrator this worker would need"*; that sentence is now false, and a
> guard kept past its cause refuses work the platform can do.
> **`EXPECTATION_SCHEMA_UNKNOWN` stays and is not the same statement**: a declaration this build has
> never heard of terminalizes before any evaluation, `authoring_axis="NOT_RUN"` (never
> `UNSUPPORTED`, which is a capability verdict about a proposal that in this arm does not exist).
> A4 increment 2's parametrized test keeps its v1-unknown half and loses its v2 half; four
> replacement cases assert the routing.
>
> **THE CAPTURE SIDE HAD TO CHANGE TOO, and the charter did not name it — reproduced, not
> inferred.** `recipe_formula_shadow._capture_selected_entry` froze `freeze_current_configuration`
> **unconditionally**, so every captured v2 work item carried a **v1** frozen configuration. With
> the worker verifying the v2 configuration, every one of them would have terminalized
> `configuration_axis="DRIFTED"` — the routing would have looked wired and authored nothing. The
> capture now freezes the generation's own configuration; a v1 capture is the same call with the
> same bytes, so live work items are untouched.
>
> **BOTH RECORDED FORWARD GAPS ARE CLOSED, and they were REACHABLE the moment the routing landed.**
> `_formula_refs` and `build_formula_authority_envelope` each collected `operand_ref` and
> `event_time_ref` only. Without the widening a `date_diff_avg`-shaped v2 body's SECOND column
> would reach neither the frozen read context (so the tool runner would refuse to read it and the
> facts reader would resolve it to nothing) nor concept authority (so a formula-bearing column
> would never be verified at all). `build_formula_authority_envelope` reads it through `getattr`
> because the v1 bound type has no such field — every v1 expectation answers `None` and its
> envelope is byte-identical.
>
> **MUTANT PROOF.** Restricting `AUTHORABLE_EXPECTATION_SCHEMAS` back to `{"formula-v1"}` — the
> worker still terminalizing — fails **four** tests: the routing test (`KeyError: 'facts_reader'`,
> the orchestrator never called), the seams test, the frozen-configuration test, and **E0's walk**,
> which is what the tripwire was for.
>
> **E0's tripwire is INVERTED here, minimally, and walked for real in increment 4.** The assertion
> that read `declared != AUTHORABLE_EXPECTATION_SCHEMA` ("*if this is now authorable, the v2
> orchestrator landed and this walkthrough must be extended through it*") now reads
> `declared in AUTHORABLE_EXPECTATION_SCHEMAS`, and the module docstring's "no replay-shaped v2
> authoring orchestrator exists" paragraph is corrected in the same commit rather than left
> standing as a false statement inside a green test.
>
> 8 new cases in `tests/featuregen/overlay/upload/test_recipe_formula_worker.py` (16 in the file);
> the v2 work item its `_seed_work` writes now carries a REAL twelve-key v2 expectation, not a v1
> one with a version key bolted on. Gates: full suite **11251 passed, 20 skipped** (11245/20 after increment 2 —
> A4 increment 2's two-case parametrization is replaced by eight); `-m eval` **73 passed**;
> ruff clean; no new mypy errors (the 29 in `recipe_formula_authority` are pre-existing, measured
> by HEAD-swap).

> **SUCCESSOR EXECUTION (2026-08-15) — INCREMENT 4: E0'S TRIPWIRE FLIPS, AND THE WALK AUTHORS.
> ACCEPTED `33abd6c9`.** `test_seam_walkthrough.py`'s §0.5 item-2 step is no longer *"the platform's
> own refusal"* — the exemplar's captured v2 work item is **authored**, and every governed check
> the worker makes before it authors is made against the row the capture actually wrote: the
> egress whitelist re-validates the frozen payload, the **v2** frozen configuration is rebuilt
> from its stored bytes and re-verified, the authority envelope is re-resolved, the read scope is
> recomputed and compared to the hash sealed at capture, and the frozen read context is loaded out
> of the real metadata snapshot for exactly the refs `_formula_refs` derives.
>
> **THE OUTCOME IS FOLLOWED, NOT FORCED: `NEEDS_REVIEW` / `external_requirement`.**
> `posting_bank::public.txns.txn_amt` carries no GOVERNED `logical_representation` in this catalog,
> and `external_type_required` is literally `not facts.logical_type`, so §C cannot certify the
> output type. The formula is authored, structurally sound, capability-ok, critic-clean — and its
> output type still needs a check outside the catalog. `candidate_output is None` because the v2
> artifact is a PAIR and half a pair would launder a guess into authority. Nothing was tuned to
> make this RESOLVED.
>
> **A REAL DEFECT, FOUND ONLY BY WALKING THE STEP, AND IT MADE THE ENTIRE AUTHORING PATH INERT.**
> `recipe_formula_worker._current_read_scope_hash` re-hashed **every** snapshot item, while
> `gate1` seals `request_read_scope_hash` over the CANDIDATES' `(catalog_source, object_ref)`
> pairs. SE-2 seals one extra item per catalog run — the frozen Layer-A context's identity PIN,
> whose `graph_ref` is a read-scope KEY (`context:<…>`), not a catalog object. **Measured, not
> inferred:** frozen `56724882…`, recomputed `f3a41b58…`, and recomputing over the same rows minus
> the context pin gives `56724882…` exactly. So on any run that seals a semantic context — which
> is the live path — the worker's re-check could NEVER pass and EVERY formula-shadow work item, of
> BOTH generations, terminalized `AUTHORIZATION_SCOPE_CHANGED` with `authoring_axis="NOT_RUN"`. The
> subsystem looked wired and authored nothing. Fixed by excluding the `generation_semantic_context`
> item kind, which verifies nothing away: that pin has its own D6 freshness comparator and is
> checked by the `compare_snapshot_to_current` call a few lines further down the same worker.
>
> **MUTANT PROOF, and the first attempt at it failed usefully.** Restoring the unfiltered query
> fails E0's walk (*"the read scope the worker recomputes must equal the one sealed at capture"*)
> AND the unit test that pins the hash. The first mutant run passed — because the string
> replacement had silently missed — which is exactly why the second asserts its target exists
> before mutating. Recorded rather than quietly re-run.
>
> **WHAT THE WALK DELIBERATELY DOES NOT DRIVE, and why.** The authoring is driven through
> `run_authoring_v2_replay` with the worker's own seams rather than through
> `process_recipe_formula_shadow_once`: the remaining worker plumbing is the queue LEASE and the
> DURABLE AUDIT STORE, and both need a COMMITTED queue row on a second connection — the fenced
> trace writes run on their own DSN connection and physically cannot see this test's open
> transaction. That is `test_fenced_replay_integration`'s subject. The capture's outbox pointer is
> ASSERTED (topic, payload) rather than relayed, and the ROUTING — that a `formula-v2` row selects
> exactly these five seams and never the v1 ones — is proved in `test_recipe_formula_worker` with
> the v1 orchestrator poisoned to raise.
>
> **NO ACTIVATION OR READINESS SURFACE MOVED**, and that was checked rather than assumed: steps 4–6
> of the walk (the activation fold, `POST /materialization-runs` **202**, the lane compile, `run_l0`
> PASSED, the read-set set-equality) pass **unedited**. A v2 authored artifact still has no path
> into materialization — `materialize/resolve.py` restores a v1 `AuthoringResult` and there is no
> `TypedFormulaV2` (A3's plan defect 2) — so `formula_expectation_revision` does not begin minting
> and no §0.3 code changed. The execution half still runs on `total_debit_amount_30d`.
>
> 1 new case in `test_recipe_formula_worker.py` (the read-scope defect, in isolation) and E0's own
> walk extended. Gates: full suite **11252 passed, 20 skipped** (11251/20 after increment 3 — the one new
> test and nothing else moved); `-m eval` **73 passed**; ruff clean; no new
> mypy errors.

---

## 6.6 SUCCESSOR 2 (2026-08-15): the two deployment adapters

**PROVENANCE.** D1's acceptance row: *"No `MetastoreMetadata` implementation exists anywhere in
`src/` … so `lane_config_from_env` produces `metastore=None` and every deployed run is honestly
unprepared."* D3's: *"the publish step's cluster half cannot be written in `src/` (the package is
render-only, never imports pyspark). It is `publish.PublicationSwap`, a Protocol the deployment
supplies."* E2 named writing both as its remaining engineering. This section is that work.

### The placement decision, argued from the codebase's own laws

**The brief's premise was WRONG in a way worth recording, and verifying it is what decided the
design.** It said to reuse capture's transport — *"`inventory.MetastoreInventoryAdapter` … HOW does
it reach the metastore today?"* It does not reach one. `MetastoreInventoryAdapter.capture` takes a
`conn: MetastoreTableMetadata`, which is **itself a Protocol with no implementation anywhere**
(`docs/architecture/2026-08-04-physical-table-configuration.md:53` already recorded this: *"its
`MetastoreTableMetadata` is a Protocol with **no implementation**"*). So capture's adapter is a
second seam, not a vehicle, and there was no existing transport to inherit.

**What the deployment actually has, verified in the manifests.** `deploy/kind/sandbox/40-spark.yaml`
deploys one Spark pod driven by `kubectl exec` — **no Service, no thrift server** — whose metastore
client talks JDBC straight to `postgres:5432/metastore`; `deploy/kind/sandbox/up.sh` bootstraps
Hive's own DDL into that database. `deploy/kind/Dockerfile.backend` installs pyspark into
`/opt/kedro-venv` and **deliberately no JVM** (*"this makes the image able to PROVE a project
builds, not to RUN one"*). So the worker pod can reach the metastore's **backing database** and
cannot start a SparkSession.

**Three candidates, and why the winner wins.** (a) *Read the HMS backing database over psycopg* —
reachable today, and rejected: reaching the metastore's storage is not reaching the metastore, it
cannot answer read scope at all, and a pointer switch written as hand-rolled `TBLS`/`SDS` UPDATEs is
exactly the *"separate metadata write and pointer flip"* `PublicationSwap`'s docstring says the
attestation would then be evidence about neither of. (b) *Subprocess into a pyspark interpreter*,
the vehicle `run_l0`'s probe and `LocalClusterSubmitter` already use — sound in principle, and it
needs a JVM in the control-plane pod, which `Dockerfile.backend` refuses on purpose. (c) **A DB-API
2.0 cursor to the engine's own SQL endpoint** — HiveServer2 or the Spark Thrift Server — which is
what landed. It imports no `pyspark` (the render-only law holds trivially), needs no JVM, is
constructible from env strings alone, is faked in the default suite at the `connect` seam, and it
performs the swap as **the engine's own atomic metastore operation**, which is the act §10.3's probe
attests. It is also the platform's existing vehicle for a Hive-dialect engine:
`data_agent.connection.open_connection` already does exactly this (DB-API 2.0, lazily imported
driver named by a closed table, injectable `connect`). `DataSourceConnectionV1` itself is **not**
reused, and the reason is written into the module: that object authorizes a governed SOURCE read and
carries a schema allowlist and a secret reference, while the metastore endpoint is a deployment fact.

**So the implementations live in `src/`** — `materialize/metastore_sql.py` and
`materialize/publish_sql.py` — beside `submit.LocalClusterSubmitter`, which is the precedent
exactly: an implementation whose imports are the standard library, needing an address from the
environment rather than an engine. Nothing moved to `deploy/`, which ships YAML and Dockerfiles, has
no Python package and is not on the image's import path; putting an adapter there would have needed
a dotted-path-from-env plugin loader this codebase has nowhere, in the process that talks to the
governed cluster.

**AND THE HONEST CONSEQUENCE, which E2 must not discover later: the kind cluster cannot satisfy this
today.** There is no SQL endpoint in front of the sandbox metastore. E2's remaining deployment work
is therefore a thrift endpoint (`start-thriftserver.sh` in the spark pod plus a Service) in addition
to the inventory — and until there is one, the eight execution variables stay unset and every run is
recorded unprepared, which is the correct posture rather than a gap. Both deployment files now say
so at the place an operator would otherwise set the variables.

> **SUPERSEDED IN PART BY SUCCESSOR 3 (§6.6b), 2026-08-15.** The endpoint now exists as
> `deploy/kind/sandbox/41-spark-thrift.yaml`. Two corrections to the paragraph above:
> **`start-thriftserver.sh` does not exist in this image** — the PyPI pyspark distribution ships four
> scripts in `sbin/` and no thrift one (verified against pyspark 3.5.3's own sdist), so the manifest
> `spark-submit`s the class directly. And the endpoint being present does **not** make the eight
> variables settable for a passing L1: a Spark Thrift Server cannot answer `SHOW GRANT` at all, so
> `can_read` is structurally unanswerable there. See §6.6b.

> **SUCCESSOR 2 (2026-08-15) — INCREMENT 1: THE `MetastoreMetadata` IMPLEMENTATION. ACCEPTED
> `e170bcb5`.** `metastore_sql.SqlMetastoreAdapter` answers L1's three questions over one
> `MetastoreSession` (a DB-API 2.0 connection, opened lazily, driver imported from a closed table
> by engine name, `connect` injectable — which is how 47 tests prove every outcome without a
> metastore, a JVM or a socket).
>
> **EVERY AMBIGUITY IS TYPED, AND `()` IS NEVER ONE OF THEM.** `runprep` already names the hazard —
> *"'this table has no partitions' and 'the metastore did not answer' are the same empty list"* — so
> every driver error is classified against a closed, ordered, documented table into
> `MetastoreFault`, and the routing is: `UNREACHABLE` → `ClusterUnreachable` (L1's `status="error"`,
> zero findings) · `TABLE_UNKNOWN` → the new `validation.MetastoreTableUnknown` · a denial of a
> question the seam has no verdict slot for → `MetastoreAnswerRefused` (L1 was told these roles MAY
> read it, so a refusal afterwards is the environment contradicting itself) · **`UNRECOGNISED` is a
> MEMBER of the vocabulary and it RAISES**, carrying the statement and the driver's own words,
> because a message table is incomplete by construction and folding an unknown message into its
> nearest neighbour is how a wrong world gets validated. The ONE empty listing the adapter may
> produce is the one the engine positively stated (*"is not a partitioned table"*), and the
> ordering — denials classified BEFORE absences — is pinned by a test, because engines phrase a
> denial as an absence.
>
> **`can_read` REFUSES TO GUESS, and that is a real deployment constraint rather than a nicety.** It
> reads `SHOW GRANT ROLE … ON TABLE …` and takes the privilege **by column name** (Hive prints ten
> columns; a positional read answers with whatever column sat there). An endpoint with no
> authorization model raises `MetastoreReadScopeUnanswerable`: `True` would be the
> unconfigured-allowlist-reads-as-everything defect `data_agent.connection` refuses by name, and
> `False` would be a denial nobody issued that fails every L1 in the deployment. **So an endpoint
> without SQL-standard authorization cannot pass L1 with this adapter** — recorded here rather than
> discovered on the cluster.
>
> **ONE CHANGE TO `run_l1`, and it is what makes the typed absence consumable.** L1 calls
> `can_read`/`describe_table` on every read-set table and then lists partitions per snapshot. An
> adapter that RAISES on an unknown table would have destroyed the report the operator needs, so
> `run_l1` now catches `MetastoreTableUnknown` and files the `COLUMN_ABSENT` "the table does not
> exist" finding it already had for `describe_table is None` — and both now skip that table's
> partition checks, which is `READ_DENIED`'s existing rule (*"reporting its columns absent would
> invent a second fault out of the first one"*) applied to the other observation that ends a
> table's checks. No existing test covered a wholly absent table; the new one asserts exactly one
> finding.
>
> **Identifiers are validated, never escaped** — no dialect binds an identifier as a parameter, so
> quoting a hostile one would be a defence that depends on the quoting being right. Five hostile
> spellings are refused before a statement exists.
>
> **Mutant proof, both run:** (a) an adapter that answers `()` for every fault — the exact "empty
> means unreachable" defect — fails **six** tests (connect failure, unreachable, unknown table,
> denial, unrecognised, denial-before-absence); (b) `can_read` returning `True` when the endpoint
> has no authorization model fails its test. Both reverted; `git diff` confirmed clean between.
>
> Gates: full suite **11300 passed, 20 skipped** (baseline on `799bcf98` was 11252/20 — the 47 new
> `test_metastore_sql.py` cases and the one new L1 case, and nothing else moved); `-m eval`
> **73 passed**; ruff clean on all four touched files; mypy clean on both touched source files.

> **SUCCESSOR 2 (2026-08-15) — INCREMENT 2: THE `PublicationSwap` IMPLEMENTATION. ACCEPTED
> `63f5ab93`.** `publish_sql.SqlPublicationSwap` performs G-3's pointer switch over the SAME
> session the metadata adapter uses — one transport, so L1's answers and the swap cannot come from
> two clients seeing two worlds.
>
> **THE MECHANISM, and why the statement count IS the property.** §10.3's `VERSIONED_POINTER` is
> *"immutable versioned physical outputs with ONE reader-visible pointer/view switch"*.
> `render.publish` already renders the immutable output (generation-scoped path, `errorifexists`);
> what remained was the pointer, and it moves with a single `CREATE OR REPLACE VIEW <target> AS
> SELECT <the plan's columns, in the plan's order> FROM parquet.<location>` — one metastore commit,
> which is exactly what the probe watches readers through, and which carries the SCHEMA too (so it
> can cover step 5's added column, where a partition-location swap cannot). `PublicationSwap`'s
> docstring says a seam with a separate metadata write and pointer flip *"would be two operations,
> and the attestation would be evidence about neither"* — so a test COUNTS the mutating statements
> rather than asserting the swap "worked", and the drop-then-create mutant fails four tests.
>
> **THE READ-BACK IS NOT DECORATION — THIS REPOSITORY HAS MET THE FAILURE IT CATCHES.**
> `deploy/kind/sandbox/up.sh` records it in its own words: *"A write succeeding proves nothing:
> Spark falls back to an embedded Derby metastore silently, and a second SEQUENTIAL session still
> sees the tables"*, and `40-spark.yaml` records the same trap a second time. A DDL statement that
> returns without error against a session-local catalog is precisely the swap that half-happened,
> and the plane would then hold a pointer no reader can follow. So the swap asks the engine what
> the object now IS (`SHOW CREATE TABLE`, metadata — never a row) and raises
> `PublicationSwapUnconfirmed` unless the answer names this generation's location;
> `publish_generation`'s transaction then rolls the 1055 row back and the plane claims nothing.
> Removing the read-back fails three tests.
>
> **ONE DEFINITION OF THE PUBLISHED LOCATION, and this is a plan gap closed rather than a
> preference.** The rendered catalog entry writes `<staging_root>/published/<table>` and the swap
> must point at THAT; two spellings of one derived path are two paths, and the second finds an
> empty directory rather than an error. `render.publish.published_output_location` is now the one
> definition, called by the entry (with Kedro's `${runtime_params:staging_root}` placeholder) and
> by the swap (with the run's resolved root). The rendered bytes are unchanged — the goldens pass
> untouched, which is what proves the extraction was a refactor.
>
> **IDEMPOTENCY, honestly split.** `CREATE OR REPLACE VIEW` is idempotent by construction, so
> re-running a completed swap emits the same statement and leaves the same definition — the
> seam's *no-op* half, asserted. The *refusal* half is not this module's and it must not invent a
> second opinion: `publish_generation` records the 1055 pointer BEFORE swapping, and that table's
> trigger refuses a `seq` that does not strictly extend the group.
>
> **Injection is refused, never escaped**, for `quoted_identifier`'s reason: the target's segments
> and every published column are validated as identifiers, and the location — which is a path and
> cannot be validated as one — is refused if it contains any character that could end its own
> quoting.
>
> 17 new cases in `test_publish_sql.py`. The DB-API double gained one honest capability: unstocked,
> it APPLIES the `CREATE OR REPLACE VIEW` and reports it back, so the read-back test is a real
> round-trip through the statement the swap emitted rather than a canned confirmation that would
> agree with any swap at all.
>
> Gates: full suite **11340 passed, 20 skipped** (11300/20 after increment 1 — the 17 new
> `test_publish_sql.py` cases, plus increment 3's 7 lane cases and the 16 parametrized rows its
> eight new env vars add to the deployment-file documentation test, all of which were in the tree
> when this ran); `-m eval` **73 passed**; ruff clean on all touched files; mypy
> clean on both touched source modules. Mutant proof: (a) drop-then-create → 4 failed; (b) trust
> the write, no read-back → 3 failed.

> **SUCCESSOR 2 (2026-08-15) — INCREMENT 3: `lane_config_from_env` BUILDS BOTH, AND WHERE THE
> BOUNDARY NOW SITS. ACCEPTED `ffd62f3c`.** The lane gained an EXECUTION block — eight variables,
> **all of them or none** — from which it builds `SqlMetastoreAdapter` and `SqlPublicationSwap`
> over ONE `MetastoreSession`, plus the `LocalClusterSubmitter` and §9's staging base that
> `RunExecution` needs beside them.
>
> `FEATUREGEN_MATERIALIZE_METASTORE_ENGINE` · `_METASTORE_HOST` · `_METASTORE_PORT` ·
> `_METASTORE_AUTH` · `_METASTORE_PRINCIPAL` · `_STAGING_BASE` · `_SUBMIT_PYTHON` ·
> `_SUBMIT_TIMEOUT_SECONDS`, all appended to `MATERIALIZATION_ENV_VARS` — so the existing CI test
> that every lane variable is documented in **both** `.env.example` and
> `deploy/kind/k8s/20-backend.yaml` now covers them, and drift fails CI rather than a deployment.
>
> **EIGHT VARIABLES AND NOT ONE DSN, deliberately.** `data_agent/connection.py` states the rule this
> follows: *"A field that could hold a secret eventually holds one, and then it is in a log, a JSON
> column, an error message or an LLM prompt."* A connection string has a password slot; none of
> these eight can hold a credential.
>
> **UNSET IS STILL AN OUTCOME, AND HALF-SET IS NEITHER.** No variable set → all four seams stay
> `None`, `execution_for` returns `None`, and the run is recorded `RUN_FAILED` at `PREPARE_RUN`
> naming what was not configured — `l0=None`'s rule two stages later, unchanged. Some set → a
> refusal naming **every** missing variable, because `RunExecution` would refuse the half anyway
> ("ALL FIVE or `None`") and a deployment that stated a host and no staging base has not chosen a
> posture. The business date stays the JOB's: a fully configured lane still yields `execution=None`
> for a trigger that asked for a compilation and not a run.
>
> **WHERE THE BOUNDARY NOW SITS — the D-phase line moves, and only half of it.** *In the code* it
> has moved all the way: `test_a_lane_with_the_REAL_adapters_carries_a_run_PAST_prepare_run` drives
> the production lane over the production chain with the REAL adapters — the real
> `SHOW PARTITIONS`/`SHOW TABLES`/`DESCRIBE`/`SHOW GRANT` statements, the real parsing, the real
> single `CREATE OR REPLACE VIEW` and its read-back — with **only the DB-API driver faked** (and the
> submitter, which is `l0_gate.py`'s job because submitting for real launches Spark). It reaches
> `ChainStage.PUBLISH`, lifecycle `COMMITTED`, on one connection.
>
> *On the cluster it has not moved yet, and the reason is now a DIFFERENT one.* D1's sentence
> — "no `MetastoreMetadata` implementation exists anywhere in `src/`" — is retired. What replaces
> it: **the kind cluster has no SQL endpoint in front of its metastore.** `sandbox/40-spark.yaml`
> deploys a `kubectl exec`-driven Spark pod with no Service and no thrift server (its metastore
> client talks JDBC straight to `postgres:5432/metastore`), and `Dockerfile.backend` gives the
> worker pyspark with **no JVM** on purpose, so the worker can neither dial an endpoint nor be one.
> E2's remaining deployment work is therefore **two** things, not one: Task 0's inventory, and a
> thrift endpoint (`start-thriftserver.sh` in the spark pod + a Service). A third is now stated as
> well: **L1 cannot pass against an endpoint with no SQL-standard authorization**, because
> `can_read` refuses to guess. Until all three are closed the eight variables stay unset and every
> deployed run is honestly unprepared — which both deployment files now say, at the exact place an
> operator would otherwise set them.
>
> > **SUCCESSOR 3 (§6.6b, 2026-08-15) closes the second and REFRAMES the third.** The endpoint is
> > `deploy/kind/sandbox/41-spark-thrift.yaml`, and it is **not** `start-thriftserver.sh` — that
> > script is not in the pyspark distribution (four `sbin/` scripts, none of them thrift; verified
> > against pyspark 3.5.3's sdist). The third is not a configuration anyone can supply: a Spark
> > Thrift Server rejects `SHOW GRANT` by grammar (`unsupportedHiveNativeCommands`), and the swap
> > requires Spark-only `parquet.`<path>`` syntax that a real HiveServer2 could not run — so no
> > single engine available here has both properties. `can_read` is unanswerable on kind, by design
> > rather than by omission.
>
> **Six stale honesty claims corrected in the same commit**, each of which had become false the
> moment increment 1 landed: `l0_gate.py`'s section header, `test_chain.py`'s `_run` docstring and
> the UNPREPARED test's, `test_seam_walkthrough.py`'s `_config`, `test_queue_lane.py`'s `_config`,
> and `docs/DEFERRED-WORK.md:488`'s "No implementation exists in `src/`" row. They now name the
> posture (a deployment that states no execution block) rather than an absence that has been filled.
>
> 7 new cases in `test_queue_lane.py` (and 16 new parametrized rows in the deployment-file
> documentation test, two per new variable). Gates: full suite **11340 passed, 20 skipped**
> (11300/20 after increment 1; the same run gated increment 2, which was in the tree with it);
> `-m eval` **73 passed**; ruff clean on all touched files; **no new mypy errors** — 469 in 124
> files, identical to `799bcf98`'s, measured by archiving that commit's `src` and running the same
> config against it, and none in any file this successor touched.

---

## 6.6b SUCCESSOR 3 (2026-08-15): the cluster's SQL endpoint

SUCCESSOR 2 ended by naming E2's remaining deployment work as *"Task 0's inventory, and a thrift
endpoint (`start-thriftserver.sh` in the spark pod + a Service)"*, plus a third item: *"L1 cannot
pass against an endpoint with no SQL-standard authorization"*. This section is the endpoint. It
also **retires the parenthesis** — that script does not exist — and converts the third item from a
thing a deployment might configure into a thing this engine structurally cannot do.

> **SUCCESSOR 3 — INCREMENT 1: THE THRIFT ENDPOINT MANIFEST. ACCEPTED `2c259f20` (2026-08-15).
> FILES ONLY — no `kubectl`, no `docker`, no apply, no cluster contact of any kind.**
>
> **`deploy/kind/sandbox/41-spark-thrift.yaml`** — a `spark-thrift` Deployment plus a ClusterIP
> Service `spark-thrift:10000`, on the SAME `featuregen-spark:local` image, mounting the SAME
> `spark-defaults` ConfigMap and the SAME `spark-warehouse` PVC as `40-spark.yaml`. One engine, one
> catalog, one Spark version — the JDBC coordinates for `postgres:5432/metastore` are not restated
> anywhere, because a second copy is how two "engines" end up seeing two catalogs.
>
> **THREE FACTS WERE VERIFIED AGAINST THE PINNED DISTRIBUTION BEFORE A LINE WAS WRITTEN**, by
> listing the members of `pyspark-3.5.3.tar.gz` itself (the 3.5.x line publishes an sdist, not a
> wheel) — not inferred from a newer version and not from memory:
>
> 1. **`sbin/start-thriftserver.sh` IS NOT SHIPPED.** pyspark's `sbin/` holds exactly four scripts:
>    `spark-config.sh`, `spark-daemon.sh`, `start-history-server.sh`, `stop-history-server.sh`.
>    **Every prior note in this plan and in `20-backend.yaml` that named that script was wrong**, and
>    following them would have produced a Deployment whose command does not exist. The manifest does
>    what the missing script would have done — `spark-submit --class …HiveThriftServer2
>    spark-internal` — and `bin/spark-submit` IS shipped and IS on PATH (pip installs `bin/` into
>    the environment's `bin`).
> 2. **`spark-hive-thriftserver_2.12-3.5.3.jar` IS bundled**, so the class resolves. `Dockerfile.spark`
>    now ASSERTS both this jar and `spark-submit` at build time, so a future pyspark bump that drops
>    either fails `docker build` on the operator's laptop instead of becoming a CrashLoopBackOff.
> 3. **`hive-metastore-2.3.9.jar`, `hive-exec-2.3.9-core.jar`, `hive-common`, `hive-serde` are ALL
>    already bundled** under the same names `Dockerfile.spark` curls from Maven. That file's claim
>    that "Hive metastore support is not in the base pyspark wheel's jar set" is **false**; the real
>    cause of the silent-Derby symptom is the `SPARK_CONF_DIR` one `40-spark.yaml` documents. The
>    download is kept as a version pin and its comment corrected to say so.
>
> **WHY SPARK AND NOT HiveServer2 — decided by `publish_sql.py:134`, not by preference.** The swap is
> `CREATE OR REPLACE VIEW … AS SELECT … FROM parquet.`<path>``, and `parquet.`<path>`` is Spark SQL's
> path-as-relation syntax. Hive has no such construct, so a real HiveServer2 would fail the one
> statement the publication step exists to perform. It is moot besides: the image carries no
> HiveServer2 — its only Hive server jar is `hive-service-rpc` (the `TCLIService` wire protocol),
> not `hive-service`.
>
> **THE BRIEFED REQUIREMENT COULD NOT BE MET, AND THE REASON IS STRUCTURAL RATHER THAN A GAP.** The
> task asked for "SQL-standard authorization ENABLED and a named admin principal", fail-closed.
> **A Spark Thrift Server cannot have SQL-standard authorization at all.** Spark's SQL grammar
> carries a rule literally named `unsupportedHiveNativeCommands`, and `GRANT`, `REVOKE` and
> `SHOW GRANT` are members: Spark parses them only to reject them, as
> `[_LEGACY_ERROR_TEMP_0035] Operation not allowed: SHOW GRANT`. Spark never routes through Hive's
> `Driver`, so the `…authorization.plugin.sqlstd` classes sitting on the classpath in `hive-exec` are
> never consulted. Verified by extracting the grammar tokens and the rule name from
> `SqlBaseParser`/`SqlBaseLexer`, and the message template from Spark's own
> `error/error-conditions.json`. No configuration changes this, in this image or any other.
>
> **So E2's third blocker is not closable on kind, and the tension is a design fact worth stating
> once:** the swap REQUIRES Spark (only Spark parses `parquet.`<path>``), `can_read` REQUIRES an
> authorization model (only Hive's `Driver` has one), and no single engine here has both. The
> honest posture is the one the adapter was already built for — `can_read` raises
> `MetastoreReadScopeUnanswerable`, and **L1 does not pass against the sandbox endpoint**. That is
> recorded here rather than discovered on the cluster, which was the point.
>
> **AUTHENTICATION, on the other axis, is real but unavailable HERE.** The forked Hive service layer
> inside `spark-hive-thriftserver` does ship `HiveAuthFactory` with LDAP, CUSTOM, PAM and Kerberos
> providers. None can run in this image, and each reason was checked rather than assumed: CUSTOM
> needs a compiled `PasswdAuthenticationProvider` and the image installs `openjdk-17-jre-headless`
> with **no `javac`**; LDAP needs a directory server and this cluster runs postgres/backend/worker/
> frontend and nothing else; KERBEROS needs a KDC and keytabs; PAM needs native JPAM libraries.
> `NONE` is therefore not a convenience default chosen over something stronger — **it is the only
> mode the image can run**, and the manifest says so at the setting rather than implying a choice.
>
> **WHAT ACTUALLY BOUNDS ACCESS, then, stated so nobody mistakes it for more:** the Service is
> `ClusterIP` (never published to the host) and the whole file is opt-in, outside `k8s/`, so
> `deploy.sh` never applies it. **A `NetworkPolicy` was deliberately NOT shipped**: `deploy.sh` runs
> `kind create cluster` with no CNI configuration, which means kindnet, and **kindnet does not
> implement NetworkPolicy** — the object would be accepted, enforce nothing, and read in review as a
> control that exists. An unenforced policy is worse than an absent one.
>
> **ONE ADAPTER DEFECT FOUND AND FIXED, and it would have fired on first contact.** Spark's actual
> rejection — `Operation not allowed: SHOW GRANT` — matched **no** entry in `FAULT_PATTERNS`, so it
> classified `UNRECOGNISED` and **raised**, meaning the one condition the module has a designed typed
> answer for would have reached the operator as an unknown fault. Added to the
> `READ_SCOPE_UNANSWERABLE` patterns, deliberately narrow (`operation not allowed: show grant`, not
> the bare prefix): Spark uses `Operation not allowed:` for unrelated refusals too — TRUNCATE on an
> external table, ALTER TABLE SET SERDE — and matching it alone would report those as an answer about
> read scope, which is the over-broad-pattern failure that table's own comment warns against. Two
> tests pin it, one for each half.
>
> **DEPLOYMENT-FILE VALUES ARE NOW COPY-PASTE CORRECT** against the Service that exists:
> `.env.example`, `20-backend.yaml` and `25-worker.yaml` state `spark-thrift` / `10000` / `NONE`,
> each with the reason and each with the `can_read` limitation named at the exact place an operator
> would otherwise expect L1 to pass. `up.sh` applies the manifest AFTER the Hive schema init (the
> server opens its metastore connection at startup, so the order is load-bearing) and points the
> operator at the increment-3 harness rather than at beeline.
>
> Gates: full suite **11342 passed, 20 skipped** (baseline `b268567b` was 11340/20; the two new
> tests are the Spark-phrasing pair above); `-m eval` **73 passed**; ruff clean on both touched
> Python files;
> no new mypy errors; all three touched manifests re-parsed with `yaml.safe_load_all` and `up.sh`
> checked with `bash -n`.

> **SUCCESSOR 3 — INCREMENT 2: THE OPERATOR RUNBOOK AND TASK 0 ALIGNMENT. ACCEPTED `46af69c7`
> (2026-08-15).** Documentation only.
>
> **THE RUNBOOK IS NINE STEPS, NOT EIGHT.** The endpoint goes in at **step 3** — after the images
> are rolled (step 2) and BEFORE the ConfigMap edit (now step 5), because the eight variables that
> edit sets name a Service that has to resolve. Two prerequisites are stated there that nothing in
> this plan had noticed: **PyHive is not a dependency of this project** (`METASTORE_DRIVERS` names
> it, the control plane declares no engine client on purpose) and **`Dockerfile.backend` copies only
> `src/`**, so neither the driver nor the smoke script is in the pod. A missing driver raises
> `ValueError`, which `_DETERMINISTIC` treats as non-retryable — the request FAILS and dead-letters
> rather than waiting for a fix. Step 6 now also warns that rolling the Deployments discards a
> `pip install` made into a running container.
>
> **THE GRANT MODEL IS WRITTEN FOR THE ENGINE THAT CAN HONOUR IT, AND ITS UNAVAILABILITY HERE IS
> STATED RATHER THAN SOFTENED.** `can_read` runs `SHOW GRANT ROLE … ON TABLE …` and reads the
> `privilege` column by name, passing only on `SELECT` or `ALL` — so grants are **per table**, over
> exactly L1's read set, and a schema-level grant does not answer the statement being run. The
> endpoint settings are given with class names verified to exist in `hive-exec`
> (`SQLStdHiveAuthorizerFactory`, `SessionStateUserAuthenticator`). **None of it runs on kind**, and
> the section says so with the mechanism: Spark rejects `GRANT`/`REVOKE`/`SHOW GRANT` by grammar.
> It also closes the obvious escape — swapping in a HiveServer2 — because the swap needs Spark-only
> `parquet.`<path>`` syntax. **The swap requires Spark, `can_read` requires Hive's Driver, and no
> engine available here is both.** L1 therefore cannot pass on kind, which is now recorded as a
> seam-level constraint rather than a task somebody can pick up.
>
> **TASK 0's CAPTURE IS SPLIT BY SOURCE, because the brief's worry was exactly right.** Of the eight
> `engine_versions` the endpoint answers **three** — `spark` (`SELECT version()`, the `SparkVersion`
> expression), `java` (`SELECT reflect('java.lang.System','getProperty','java.version')`, via
> `CallMethodViaReflection`; `version`/`reflect`/`java_method` confirmed as registered builtins in
> `FunctionRegistry$`) and `hive` as the CLIENT version (`SET spark.sql.hive.metastore.version`).
> It cannot answer `python`, `pyspark`, `kedro` or `kedro_datasets` — all venv facts — and
> `metastore` comes from the metastore DB's own `VERSION` table rather than the endpoint.
> `pyspark` is called out separately: it SHOULD equal `spark`, and reading it from the endpoint
> would assume the very equality the field exists to let someone check. The `tables` half is the
> opposite — the endpoint IS the right source for `columns` (unnormalised), `partition_columns` and
> `location` — except `partition_mapping` and `rewritten_in_place`, which no metastore knows.
>
> **ONE MORE STALE CLAIM CORRECTED.** Runbook step 8 still said the G-2 adapters were absent ("no
> `MetastoreMetadata` implementation exists in `src/`; writing them is E2's remaining engineering
> and it is not done"). SUCCESSOR 2 wrote both adapters; the sentence survived in the runbook after
> being retired elsewhere. It now names the narrower thing that actually stops a run.
>
> Gates: full suite **11356 passed, 20 skipped**; `-m eval` **73 passed**. No code touched by this
> increment — the count is 14 above increment 1's because increment 3's harness tests were already
> in the tree when this ran, so **the same run gates increment 3**.

> **SUCCESSOR 3 — INCREMENT 3: THE VALIDATION HARNESS. ACCEPTED `1ffee9bc` (2026-08-15).**
> `scripts/thrift_smoke.py` — read-only, three metadata reads, nothing written.
>
> **IT IS THE PRODUCTION CODE PATH, WHICH IS THE ENTIRE POINT.** It builds a real
> `MetastoreSession` and a real `SqlMetastoreAdapter` and asks L1's three questions, so the first
> contact with a new endpoint exercises the same driver, the same validated back-quoted identifiers,
> the same read-of-`privilege`-by-name and above all the same `FAULT_PATTERNS` classification that
> the worker will. A beeline session proves a human can reach the endpoint; it cannot prove the
> adapter can, and it reports "an error" where this reports WHICH typed outcome. It deliberately
> does not exercise `SqlPublicationSwap` — that is a `CREATE OR REPLACE VIEW`, a real mutation, and
> a smoke test should not be the thing that first performs it.
>
> **THE GUARDS ANSWER THE ONE FAILURE IT COULD PLAUSIBLY CAUSE**, which is not a wrong answer but
> being aimed at the wrong cluster by an inherited shell environment. The five endpoint variables
> are imported BY NAME from `queue_lane` rather than re-spelled, so the script cannot drift from
> what the worker dials; **none has a default**; every missing one is named at once; and
> `--confirm-endpoint HOST:PORT` must equal what the environment resolved to, checked **before any
> connection is opened**. `--roles` is required and non-empty because `can_read` answers `False` for
> an empty role list WITHOUT asking the engine, and printing that would be a verdict nothing
> observed.
>
> **EXIT CODES ENCODE THE DISTINCTION THE ADAPTER IS BUILT ON.** `0` — every question produced a
> typed outcome, **including READ SCOPE UNANSWERABLE**, because that is an answer ABOUT the endpoint
> and exiting non-zero would train an operator to read the platform's most careful refusal as a
> broken deployment. `2` — configuration refused, nothing contacted. `3` — UNREACHABLE or
> UNRECOGNISED, the two outcomes that genuinely mean the endpoint is not usable yet.
>
> 14 tests in `tests/featuregen/materialize/test_thrift_smoke.py`, **every one with an injected fake
> `connect`** — a suite for the script that makes first contact must itself never make contact, or
> it passes and fails on whether a laptop happens to have a metastore. The refusals are pinned
> harder than the happy path, including an assertion that a mismatched `--confirm-endpoint` left the
> fake connection unopened.
>
> Gates: full suite **11356 passed, 20 skipped** (the same run that gated increment 2 — these
> 14 tests were in the tree for it); `-m eval` **73 passed**; ruff clean on both new files; mypy
> clean on `scripts/thrift_smoke.py` and no new errors in `src` (469 in 124 files, identical to the
> baseline, and none in any file this successor touched).

---

## 6.6c SUCCESSOR 4 (2026-08-15): the option → contract resolution

E0's acceptance row named a defect and deliberately left it open: `POST /materialization-runs`
called `assemble_current_activation_state` **without `contract_id`**, so C3's contract-keyed
`_requirements_closed` read was dead on the one route that gates materialization, and
`EXTERNAL_VALIDATION_OUTSTANDING` could only ever clear through the `DESIGN_CHECKED` short-circuit
(`activation_policy.py:195`). The row said naming a contract "needs an option → contract resolution
that does not exist", and refused to invent one inside a proof task. This successor builds it.

> **SUCCESSOR 4 — INCREMENT 1: THE LINK, RECORDED AT MINT. ACCEPTED `227de9c6` (2026-08-15).**
> Migration **1069** (`contract_option_link.sql`) adds `considered_revision_id` + `option_id` to
> `contract` — nullable, composite FK to `semantic_option_decision (considered_revision_id,
> option_id)` against 1063's own `semantic_option_decision_option_uq`, a named CHECK for the
> half-stated case, a partial index on the pair. 1067's shape, for 1067's reasons.
>
> **PLAN DEFECT, VERIFIED AND CORRECTED — E0's row was wrong about WHY the resolution was
> missing.** It says *"`contract_gate1_choice_revision` records the option but the two id spaces
> are different"*. They are the SAME id space: `_private_considered_revision_snapshot`
> (`gate1.py:1404-1426`) mints `options_by_id` from `cs.option_ids_by_path`, and
> `_persist_considered_revision` (`:1499-1507`) keys `persist_option_decisions` by exactly those
> ids — so a choice row's `option_id` IS a `semantic_option_decision.option_id`, and the confirm
> route already depends on it (`api/routes/contract.py:1512` loads `load_frozen_option_facts` with
> the recorded choice's pair for the A2 re-check). The id space that genuinely does not reach
> either of them is `contract`'s, which is FEATURE identity — and that is the gap 1069 closes. The
> row's operative conclusion (do not invent the resolution inside E0) was right for the wrong
> reason; the sentence is corrected in place.
>
> **THE DIRECTION OF THE LINK, and why it is not a matter of taste.** A contract is minted FROM a
> choice of an option, and `confirm_contract` is the ONE writer (a single production call site).
> The confirm route already holds both halves there — it loads the frozen decision row for the A2
> re-check immediately before minting — so the stamp happens at the only moment both id spaces are
> in one caller's hands. The reverse column cannot exist: `semantic_option_decision` is
> append-only and is written at GENERATION, before any contract does. A separate link table would
> be a third store for a fact that is one-to-one with a contract VERSION and immutable with it, and
> it would need its own write-once triggers to earn what 1012 already gives. And a join BY NAME is
> refused on principle and by evidence: the walkthrough fixture's own option carries three
> different strings — the card's name (`Complaints`), the governed `feature_name` (the same card
> name), and the decision row's `source_definition_id` (`complaint_count`) — so a name-based
> resolution would not have found the row at all. A test asserts that inequality rather than
> describing it.
>
> **NULLABLE IS THE HONEST VALUE, AND THE WORM TABLE MAKES IT PERMANENT.** 1012 forbids UPDATE and
> DELETE on `contract`, so these columns can only ever be written by the INSERT that mints the row:
> every contract governed before 1069 keeps a truthful NULL, there is no backfill path, and none
> was invented. The route passes the pair only when `load_frozen_option_facts` returned a row in
> that same transaction — the FK refuses a citation nothing can resolve, and the writer refuses
> half a key with the same sentence B4's route uses.
>
> **The migration is audited against a POPULATED table**: the link is dropped, a contract is seeded
> in the pre-1069 shape, and 1069's own SQL is run against it — the ALTER lands on a WORM table
> that already has rows (a rewriting migration would ABORT on 1012's trigger rather than fail in
> review), the definition is byte-identical afterwards, nothing is backfilled, and the constraints
> then bite. Re-runnability is proved by applying it twice.
>
> **Two mutants, both caught:** a route that never states the key (2 failures — the recorded link
> is asserted equal to the human's own choice, not merely non-null), and a migration carrying the
> columns but neither constraint (4 failures).
>
> 10 cases in `tests/featuregen/api/test_contract_option_link.py`.
> Gates: full suite **11366 passed, 20 skipped** (baseline on `b73c17c1` was 11356/20 — the ten new
> tests and nothing else moved); `-m eval` **73 passed**; ruff clean on all three touched files;
> mypy clean on `govern.py` and the 2 errors in `contract.py:883` are pre-existing (measured by
> HEAD-swap, identical line and codes) — none added.

> **SUCCESSOR 4 — INCREMENT 2: THE ROUTE RESOLVES AND PASSES `contract_id`. ACCEPTED `a57491c2`
> (2026-08-15).** `api/routes/materialization_runs.py` gains `_contract_minted_from` and hands its
> answer to `assemble_current_activation_state`. **E0's defect is closed**: the route that gates
> materialization now reads the validation store under the contract the approved option actually
> minted, so a recorded data check opens the gate it was always supposed to open.
>
> **THE RESOLUTION RULE IS STATED IN CODE, not left to be inferred: the HIGHEST version linked to
> this option.** `confirm_contract` never rewrites a contract (1012), so a re-confirm appends a
> version and both rows carry the link; the validation store is per-contract-version
> (`feature_validation_requirement` and the 1009 stream are both keyed by `contract_id`), so the
> newest linked version is the one whose requirements are actually owed. `version` is unique per
> `feature_name` (0961) and the confirm route refuses a draft whose name is not the chosen option's,
> so the ordering over one link is total; the trailing sort keys only make that explicit. **There is
> no fallback.** `None` — an option nobody confirmed, or a contract governed before 1069 — is a real
> answer, and C3's read fails closed on it.
>
> **E0's PINNED TEST FLIPPED, and it still asserts both directions on the SAME option with the SAME
> recorded homework.** `test_the_route_cannot_close_the_named_homework` is now
> `test_the_route_closes_the_named_homework_only_through_the_LINKED_contract`: with the link, `POST
> /materialization-runs` **202**s the served exemplar — the candidate whose own eligibility fold
> names eight outstanding codes including the unconditional `STATUS_POLICY_UNRESOLVED`; with the
> link removed (through 1012's own documented teardown hatch, because a pre-1069 row cannot be
> produced any other way), the identical option is refused **409** with exactly
> `EXTERNAL_VALIDATION_OUTSTANDING`.
>
> **WHERE THE BOUNDARY NOW SITS, precisely.** Only contracts minted BEFORE migration 1069 (and
> confirms that named no served decision row) carry NULL, and `contract` being WORM means no
> backfill can ever reach them — the honest answer for those is "nobody recorded which option this
> came from", and the gate stays shut. Every contract minted from here on carries the link.
>
> **What §0.5 item 4 can now honestly assert, and what has NOT changed.** Item 4 —
> *"`POST /materialization-runs` accepts"* — is now proved on the SERVED exemplar, not only on the
> C3-milestone-shaped `DESIGN_CHECKED` row, and E0's acceptance row is corrected where it said that
> was unreachable. The rest of the walk is untouched: the compile half still runs on
> `total_debit_amount_30d` because a v2 artifact is still a pair with no path into materialization
> (A3's plan defect 2), and the gold hook and snapshot freshness are still the two named seeds.
>
> **Three mutants, each run and each caught:** (a) resolving by `feature_name` instead of the link
> (2 failures — and it is not a near miss: the fixture's card name, governed `feature_name` and
> `source_definition_id` are three different strings); (b) `_requirements_closed` returning `True`
> before it consults the store (1 — the legacy leg accepts what it must refuse); (c) the resolver
> taking the OLDEST linked version (1).
>
> **ONE EXISTING TEST DOUBLE WAS CORRECTED, not bypassed.** B4's two positive-control tests stub
> `assemble_current_activation_state` to produce the ALLOWED state, and their stub was written to
> the pre-C3 signature — so the route's new `contract_id` argument made the CALL fail rather than
> the assertion. The stub now mirrors the real signature and threads the argument through to the
> genuine assembler it wraps; nothing about what those tests assert changed. (The full suite caught
> it, which is the point of running it: 2 failures, both `TypeError` at the double.)
>
> 2 further cases in `tests/featuregen/api/test_contract_option_link.py` (12 total there) and the
> flipped walkthrough test.
> Gates: full suite **11368 passed, 20 skipped** (11366/20 on `227de9c6` — the two new resolver
> tests and nothing else moved); `-m eval` **73 passed**; ruff clean on all four touched files;
> mypy **clean** on `materialization_runs.py` (no pre-existing errors in it).

---

## 6.6d SUCCESSOR 5 (2026-08-15): the DECLARED read-scope posture

**PROVENANCE, AND THE ONE THING THIS SECTION CHANGES ABOUT THE PLAN'S STORY.** SUCCESSOR 3 closed
E2's endpoint and, in closing it, recorded a blocker it could not close: *"the swap REQUIRES Spark
(only Spark parses ``parquet.`<path>```), `can_read` REQUIRES an authorization model (only Hive's
`Driver` has one), and no single engine here has both … **L1 does not pass against the sandbox
endpoint**"*, adding that §0.5 item 7 *"is closable only if L1's read-scope question is satisfied
some other way, which is a governance decision this plan does not take"*.

**THE USER TOOK THAT DECISION ON 2026-08-15, and it is option (a): a deployment may EXPLICITLY
DECLARE that it has no authorization model.** Under that declaration — and only under it — L1's
third question folds to an ACCEPTED outcome instead of failing the run. So the framing "STRUCTURAL
blocker, not closable on kind" is superseded by **"resolved by declared posture"**: the engine
constraint is unchanged and still true, and what changed is that the platform now has a governed way
for an operator to state it and accept its consequence. Everywhere else — every deployment that does
not declare — behaviour is byte-for-byte what SUCCESSOR 2 built, which the tests assert at the
default rather than assume.

> **SUCCESSOR 5 — INCREMENT 1: THE DECLARATION AND THE FOLD. ACCEPTED `<increment 1>`
> (2026-08-15).**
>
> **WHERE THE DECLARATION LIVES, argued against the alternative it was measured against.**
> `FEATUREGEN_MATERIALIZE_DECLARE_NO_AUTHORIZATION_MODEL` is a SEPARATE, independently-optional
> variable — **not a ninth member of the all-eight-or-none EXECUTION block** — and it is a member of
> `MATERIALIZATION_ENV_VARS`, so the CI documentation test covers it in `.env.example` and
> `20-backend.yaml`. The eight are ONE choice (*here is the engine, and here is how a run reaches
> it*) and all-or-none holds because each is useless without the others; this is not part of reaching
> the engine, it is an ACCEPTANCE of a risk. Folding it in would have made it MANDATORY for every
> deployment that executes runs at all — including production, where the only correct value is the
> one that accepts nothing — and a required field whose safe value every operator must type is a
> default in disguise. Worse, the half-configured refusal NAMES the missing variables, so the fastest
> way to silence it would have been to set the one the error had just mentioned. Kept separate, both
> laws stand: the block is still exactly eight and still all-or-none (a test states seven plus the
> declaration and asserts the refusal names the missing eighth and **never** the declaration), and
> the declaration is still never a default, because its absence is the STRICT posture rather than an
> incomplete one.
>
> **ONE COUPLING IS ENFORCED, because a standing acceptance is the one that outlives its reason.**
> The declaration set with NO execution block is a `ValueError` naming it: it is a statement about
> the authorization model of an endpoint nobody configured, and an acceptance nothing is using is
> exactly the one that survives into the deployment where it is not true.
>
> **THE FOLD IS IN L1, NOT IN THE ADAPTER, and that is the load-bearing placement.**
> `SqlMetastoreAdapter.can_read` still raises `MetastoreReadScopeUnanswerable` whatever a deployment
> declared — an adapter that answered `True` because a variable was set would be the
> unconfigured-allowlist-reads-as-everything lie with a permission slip. What the declaration changes
> is what the raise MEANS to `run_l1`, which is the layer that decides what an observation means for
> a run. `run_l1` gained `read_scope: ReadScopeDeclaration = ENGINE_ANSWERS`; `RunExecution` and
> `MaterializationLaneConfig` carry it with the same default, so **every construction that predates
> this parameter is unchanged**, and SUCCESSOR 2's tests did not move.
>
> **THE ACCEPTANCE IS A TYPED, RECORDED FINDING — `READ_SCOPE_UNVERIFIED`, severity `WARNING`, one
> per table**, filed with `observed="this deployment declares no authorization model"`. It is the
> platform's FIRST `WARNING`-severity finding, and a test reads `validation.py`'s AST to pin that
> there is exactly one emitter and which code it files. `ValidationReportV1`'s invariant moved from
> *"`passed` carries zero findings"* to *"`passed` carries no ERROR finding"* (and `failed` now
> requires at least one ERROR finding, so a report holding only warnings cannot be recorded as a
> failure) — the same rule it always meant, restated in severities now that not every finding is a
> condition under which the run cannot be trusted. **No migration:** 1034's physical CHECK constrains
> `error` alone, so a passing report carrying a warning was always legal in the database.
>
> **THE DECLARATION COVERS EXACTLY ONE OUTCOME, and six tests hold that line.** A denial the engine
> actually issued is still `READ_DENIED`; `ClusterUnreachable` is still `status="error"`;
> `MetastoreTableUnknown` is still `COLUMN_ABSENT`; a `RuntimeError` and a `ValueError` out of the
> adapter still propagate; and a declared deployment whose engine DOES answer records nothing at all
> — the warning marks the runs that were accepted, so a run that was verified must not carry it.
> The other two questions are still asked of an accepted table (a fold that skipped the table would
> have been a far larger acceptance than the operator made).
>
> **Three mutants, each run and each caught, then reverted:** (a) inferring the declaration from the
> endpoint (`METASTORE_HOST == "spark-thrift"`) — **11 failures**, including the one that says the
> posture is never inferred from the engine or the host; (b) the fold also catching
> `ClusterUnreachable`/`MetastoreTableUnknown` — 2; (c) dropping the `raise` on the undeclared path
> — 2, both on the byte-identical-undeclared-behaviour tests.
>
> Files: `codes.py`, `validation.py`, `metastore_sql.py` (`MetastoreReadScopeUnanswerable` MOVED to
> `validation.py` beside `ClusterUnreachable`/`MetastoreTableUnknown` — its two siblings — and
> re-exported under the same name, which is what lets `run_l1` fold on it without an import cycle),
> `compile/chain.py`, `queue_lane.py`, `.env.example`, `20-backend.yaml`, `25-worker.yaml`.
> Gates: full suite **11402 passed, 20 skipped** (baseline `0a914ec4` was 11368/20 — the 34 new
> cases are 13 in `test_validation.py`, 19 in `test_queue_lane.py` and the 2 parametrized rows the
> new variable adds to the deployment-file documentation test, and nothing else moved); `-m eval` **73 passed**; ruff clean on every touched
> file; mypy clean on all five touched source modules; both manifests re-parsed with
> `yaml.safe_load_all`.

> **SUCCESSOR 5 — INCREMENT 2: THE DURABLE, VISIBLE STAMP. ACCEPTED `<increment 2>` (2026-08-15).**
>
> **THE ACCEPTANCE RIDES TWO PERSISTED RECORDS, and neither of them is the HTTP response.**
> `pipeline_validation_report.findings` carries it per table, typed and queryable from psql
> (`code=READ_SCOPE_UNVERIFIED`, `severity=warning`, `observed="this deployment declares no
> authorization model"`), and the PUBLISHED run event's own `detail` says it in words —
> *"READ SCOPE WAS NOT VERIFIED for N table(s) (…): this deployment declares no authorization
> model…"*. The second is not redundancy: the report is what an audit reads, and the event is what a
> reader meets first, so a publication whose line said only *"published X at generation Y"* would
> read as a run that passed every check. Only the PUBLISHED terminal carries the sentence — every
> other terminal names the thing that stopped the run, and an accepted absence appended to a failure
> would compete with the headline while the report behind it says the same thing in full — and a run
> whose engine ANSWERED carries nothing, which is what makes the sentence mean something when it is
> there.
>
> **`GET /materialization-runs/{id}` GAINS TWO FIELDS, READ OFF THE RECORD AND NEVER OFF THE
> ENVIRONMENT.** `read_scope_verified` (`true`/`false`/`null`) and `read_scope_detail` (one sentence,
> always). The route resolves them from the newest L1 report of the run's generation, and the reason
> is a defect it would otherwise have: the declaration is a property of the deployment **at the time
> the run ran**, and the pod answering the GET may be a different pod on a different day with a
> different ConfigMap — a route that consulted the variable would report today's posture beside
> yesterday's run, and would answer identically for a declared deployment whose engine actually
> answered. `null` is an honest absence with three distinguished causes (no generation, no L1 report,
> an L1 that could not run) and is never `true`: a check that did not happen is not a check that
> passed, which is `ValidationStatus.ERROR`-carries-zero-findings applied to a surface.
>
> **THE D4 SCREEN RENDERS IT UNCONDITIONALLY**, tone `warning` — not `bad` (the run is legitimate and
> its deployment accepted this in advance; red would report a failure that did not happen) and not
> hidden (a section that appears only when something is wrong makes its absence meaningless, and the
> whole point is that a verified run and an unverified one must not look identical). The sentence is
> the server's, verbatim; the screen supplies the tone, which is exactly the division of labour
> `OUTCOMES` already makes.
>
> **THE DISTINGUISHABILITY CLAIM IS ASSERTED ON THE DATABASE, twice.** `test_queue_lane` drives the
> production chain over the production adapters against an engine that answers `SHOW GRANT` with
> Spark's own `[_LEGACY_ERROR_TEMP_0035] Operation not allowed: SHOW GRANT` — so
> `FAULT_PATTERNS` really classifies it and `can_read` really raises — and then reads the L1 report
> row and the run-event row back out of the plane. Its control is the SAME engine with no
> declaration: no PUBLISHED event, no L1 report, request not committed. A second pair in
> `test_validation` proves the finding survives the JSON round trip with its severity and its words
> intact, beside a verified run's empty findings list.
>
> **Three mutants, each run and each caught, then reverted:** (a) the route deriving the answer from
> `FEATUREGEN_MATERIALIZE_DECLARE_NO_AUTHORIZATION_MODEL` instead of the record — 1 failure, the
> route test that never sets it; (b) dropping the note from the PUBLISHED terminal — 1, on the
> persisted event; (c) flattening the screen's `warning` tone to `neutral` — 2 frontend failures.
>
> Files: `compile/chain.py`, `api/routes/materialization_runs.py`, `frontend/src/api.ts`,
> `frontend/src/screens/MaterializationRunScreen.tsx` (+ its test), `test_queue_lane.py`,
> `test_validation.py`, `test_materialization_e2e.py`.
> Gates: full suite **11408 passed, 20 skipped** (11402/20 after increment 1 — the 2 lane cases,
> the 2 report round-trip cases and the 2 route cases, and nothing else moved); `-m eval` **73
> passed**; frontend **813 passed / 40 files** (baseline 810 — the three read-scope render cases); `tsc --noEmit` clean; ruff clean on every touched file; mypy clean on both
> touched source modules.

---

## 7. Sequencing and dependencies

```
A0 ──► A1 ──► A2 ──► A5
              │        │
              └──► A4 ─┤        (A4 needs A2's derivation to widen the population)
                       │
A3 ⟨LLM⟩ ──────────────┤        (independent of A1/A2 for its seam; needs A1 for the v2 binder)
                       ▼
                      A6 ──────► C1 ──► C2 ──► C3
                       │                        │
B1 ──► B2 ──► B3       │                        │
  │            │       │                        │
  └──► B4 ◄────┴───────┘  (B4 needs A5+C3 for a decision that can say yes)
                                                │
D0 ── (do FIRST, before anyone touches attestations) ──┐
                                                       ▼
                       B3 + D0 ──► D1 ──► D2 ⟨operator⟩ ──► D3 ──► D4
                                                                    │
E0 needs A5 + B3 + C3 + D1 ────────────────────────────────────────┤
E1 needs D3; E2 needs E1 + explicit user go ───────────────────────┘
```

**Estimated effort:** A ≈ 10 days · B ≈ 5½ · C ≈ 3 · D ≈ 9½ · E ≈ 3½ — **≈ 31½ focused days.**

- **A + C alone (≈13 days) flips three of the four §0.3 codes** and is independently valuable: the
  UI stops saying "materialization unavailable" for the wrong reason.
- **A + B + C + E0 (≈19½ days) is the minimum honest milestone**: *a governed contract compiles to a
  build-verified Kedro project carrying its own frozen plan.* Review here before starting D.
- **D is the only phase that needs a cluster and an operator**, and D2/E2 are gated on explicit go.

---

## 8. Standing rules for execution

- Full backend suite green gates every push; frontend suite gates frontend pushes; `pytest -m eval`
  joins the gate from A0 onward (`pyproject.toml:85` sets `addopts = "-m 'not eval'"`, so it must be
  run explicitly — a gate nobody runs is not one).
- The real-JVM gates (`tests/featuregen/materialize/l0_gate.py`,
  `spark_semantics_gate.py`) are **not** `test_*` files and are not collected by the default suite,
  by design: `pyspark`/`kedro` are not dependencies of this platform. Run them via `make l0-gate`
  for any task touching `render/` or the chain.
- Migrations **1055** (reserved, G-3), **1066**, **1067**, **1068** (added at execution, task B2)
  and **1069** (`contract.considered_revision_id + option_id`, claimed at execution by SUCCESSOR 4,
  §6.6c — the free frontier was verified to be 1069 before it was taken) —
  those five and no others. The free
  frontier is 1066 (1065 is the highest applied; 1055 is a live reservation, not an error). Claiming
  a number appends to the Track-1-owned reservation table **in the same commit** — never "next
  available" at execution time. Deploy backend-first, with explicit user approval per the standing
  deploy rule. Every migration is audited against a **populated** table before it is called done
  (CI is blind to legacy data).
- No new env flags (D-6). `FEATUREGEN_MATERIALIZE_ENABLED` stays default OFF until E2.
- **⟨LLM⟩ tasks are blocked on Anthropic billing.** Ship the seam behind a recorded-fixture client
  and mark the task *shipped, unverified against a live provider*. A billing failure is never
  recorded as a capability or schema verdict (D-10).
- **`merchant_mcc_diversity`'s grain mismatch is not resolved here** (D-7). No task may re-key the
  reviewed v1 expectation, delete `test_formula_shadow_reaches_the_reviewed_blueprint_and_names_its_disagreement`,
  or make the refusal invisible. It is named in this plan so the decision has a home; the decision
  itself is a human's.
- Deploys, probe runs and any live LLM verification are **operator actions**: explicit user go each
  time, every time.
- An acceptance row is appended under the matching task in THIS file per landed slice, with its
  commit hash and its honest deviations.
- One pointer line is added to `docs/superpowers/plans/2026-08-13-semantic-activation-and-one-engine-remediation.md`
  §0.1, where it says materialization is out of scope, naming this file. Nothing else in that file
  is edited.

---

## 9. Interfaces verified before writing (file:line, on `d4e95429`)

Every name below was read, not remembered.

**Activation and the option decision**
- `activation_policy.FrozenOptionFactsV1` — 20 fields incl. `has_reviewed_formula_expectation`,
  `plan_envelope_present`, `read_set`, `plan_catalog_source`, `operand_authorities` — `:47-70`
- `activation_policy.CurrentActivationStateV1` — 12 fields, every default the failing side — `:73-90`
- `activation_policy.activation_decision(frozen, current, action, actor=None) -> ActivationDecisionV1` — `:210`
- `activation_policy.ACTIVATION_ACTIONS` five-rung ladder; `_materialization_blockers` — `:37, :170`
- `semantic_option_decision.decision_facts_for_candidate(candidate, idea, observation_id, context_hash, *, uoa_entity=None, spine_ref=None) -> dict` — `:25`; the ref/id defect at `:59`
- `semantic_option_decision.load_frozen_option_facts(conn, *, considered_revision_id, option_id)` — `:202`
- `semantic_option_decision.assemble_current_activation_state(conn, *, frozen, snapshot_id, intent_id=None)` — `:260`; the three hardwired falses at `:361-364`
- `semantic_eligibility_reasons` — `FORMULA_NOT_REVIEWED:66`, `FORMULA_SCHEMA_UNSUPPORTED:67`, `READINESS_NOT_MATERIALIZATION_READY:68`, `EXECUTION_AUTHORITY_UNEVALUATED:70`, `EXECUTION_AUTHORITY_UNMET:71`

**Recipes, readiness, expectations**
- `recipe_readiness.fold_readiness(ReadinessInputsV1) -> RecipeReadinessV1`; `READINESS_LADDER`; four blocker constants — `:68, :33, :37-40`
- `recipe_formula_expectations_v2.RECIPE_FORMULA_V2_EXPECTATIONS` (1 entry) / `has_reviewed_expectation(expectation_ref)` / `validate_v2_expectation_registry()` — `:20, :29, :39`
- `recipe_formula_expectations.RECIPE_FORMULA_EXPECTATIONS` (2 entries, keyed by recipe id) — `:74`
- `recipe_formula_contracts.bind_formula_expectation(context, blueprint) -> BoundRecipeFormulaExpectationV1`; five preflight codes — `:201`
- `recipe_contract_v2.RECIPE_READINESS`; `FormulaReferenceV2.expectation_ref` mandatory — `:36, :247-253`
- `recipe_registry_v2.V2_RECIPES` (317), `PROBE_RECIPE.formula.expectation_ref="probe:posted_debit_amount"` — `:97-156`
- Measured distribution: 295 `FORMULA_BLOCKED` / 19 `CONCEPTUAL_ONLY` / 3 `FORMULA_AUTHORABLE`; 296 `formula-v2` / 2 `formula-v1`

**Planning and the envelope**
- `recipe_planning_lens.fold_frozen_binding_plan(request, verdicts, story, pit_text, temporal_blocker, catalog_source, uoa_entity=None) -> (plan|None, refusals)`; the 9-key envelope — `:211, :256-266`
- `recipe_planning_lens.V2RecipeCandidateV1` (`readiness` is the authored literal); `variant_primary` — `:306-340`
- `recipe_planning_lens.fold_binding_state(verdicts, definition) -> str` — `:286`
- `contract/gate1._engine_recipe_contexts(...)` — one context per recipe at `variant_primary` — `:806`
- `recipe_grounding_context.build_v2_recipe_grounding_context(candidate, *, catalog_source, logical_ref_by_object_ref) -> RecipeGroundingContextV1 | None` — `:205`

**Formula v2 grammar**
- `capability_v2.EngineCapabilityV1(engine_id, supported_aggregations, supports_window_offset=False, supports_future_horizon=False)` — `:31`
- `capability_v2.classify_formula_capability_v2(proposal, engine=None) -> "ok"|"unsupported_capability"|"unsupported_engine"` — `:45`; **zero production callers**
- `schema_v2` — `TypedFormulaProposalV2:225`, `AggregateFunctionV2:49`, `FinalOperationV2:83`, `WindowBasisV2:95`, `WindowPolicyV2:108`, `AuthorityRefsV2:135`, `CompositeBodyV2:210`, `validate_semantics_v2:307`, `body_expressions_v2:295`
- `parse_v2.parse_versioned(raw)` / `parse_proposal_v2(raw)` — `:164, :131`
- `canonical_v2.proposal_content_hash_v2(proposal)` — `:40`
- `output_authority_v2.resolve_output_v2(...)` — `:78`; `operations_v2.operation_rule(aggregation)` — `:101`
- `authoring.run_authoring(conn, intent, author_client, critic_client, *, roles, actor) -> AuthoringResult` — **v1 only** — `:266`
- `turns.AuthoringIntent(name, hypothesis, target_entity, target_grain_keys=(), recipe_authoring_context=None)` — `:147`
- 36 fixtures in `tests/featuregen/formula/gold_v2/`; the pin test at `tests/featuregen/overlay/upload/recipes/test_transaction_foundation.py:19`

**The shadow capture seam**
- `recipe_formula_shadow.capture_ranked_shadow(conn, *, generation_run_id, intent_id, confirmed_scope_id, considered_revision_id, considered_content_hash, metadata_snapshot_id, metadata_snapshot_content_hash, ranked, ranking_version, ranking_enabled, candidate_keys_by_recipe_id, grounding_context_by_candidate_key, identity, request_read_scope_hash) -> ShadowReconciliation` — `:1038`
- the v1-only population at `:1077`; the blueprint lookup at `:923`; `MAX_RECIPE_FORMULA_CAPTURES_PER_RUN = 12` at `:50`
- `write_work_item(...)` + `_work_item_material(...)` + `verify_work_item_payload(row)` — `:700, :114, :163`
- `RECIPE_FORMULA_SHADOW_TOPIC = "recipe_formula_shadow.requested.v1"`, `..._HANDLER = "recipe_formula_shadow.author.v1"` — `:46-47`; routed at `runtime/worker.py:72`
- `recipe_formula_worker` builds the `AuthoringIntent` at `:343-349`

**Materialization (all on `origin/main`)**
- `compile/chain.compile_feature_group(...) -> CompiledGroup` — 14 keyword params — `:356`
- `ChainStage` 10 members; `CompiledGroup` 14 fields; `L0Interpreter`; `NodeAssemblyInputs`; `NodeAssembler` — `:203, :318, :243, :283, :304`
- `PublishStepMissing` and its trigger — `:186-197`; raised at `:506-516`
- `COMPILER_VERSION = "2"` (`compile/__init__.py`), `RENDERER_VERSION = "3"` (`render/__init__.py`)
- `render/project.render_project(authorized, plan, *, environment_id, engine_versions, spine_input, nodes, publisher_selection=None) -> SealedProject`; `materialize_to`; `project_datasets`; `REQUIRED_RUN_PARAMETERS` (6); `PIPELINE_NAME = "materialize"`
- `render/nodes_compute.render_spine_node / render_projection_node / render_calculation_node`; `render/nodes_gate.render_assembly_node / render_gate_node`; `render/nodes_join_gate.render_join_precondition_node`; `render/publish.render_publish`, `RENDERABLE_MECHANISMS = {VERSIONED_POINTER}`
- `ir.FormulaExecutionIRV1` (10 fields), `ir.AuthorizedCompilation`, `compile_ir(...)`, `authorize_compilation(...)`, `physical_read_set(irs, spine)`
- `identity.SealedProject`, `CompilationIdentity`, `RenderedArtifactIdentity`, `seal_project`, `generated_project_hash` (excludes `GENERATED.lock`), `sandbox_execution_hash(...)`; `canonical.materialize_hash` (RFC 8785 + sha256)
- `resolve.resolve_feature_inputs(conn, *, work_item_ids) -> tuple[ResolvedFeature, ...]`; `ResolvedFeature(work_item_id, authoring_run_id, intent_hash, input)` — `:99, :82`
- `publish.select_publisher(conn, *, environment_id, engine_versions, mechanism, group_plan, published_schema)` — `:552`; `record_attestation` — `:408`; `read_attestations` — `:468`; `assess_probe_observations` — `:315`; **`probe_publication_capability` named at `:326`, absent**
- `runprep.prepare_run(...)` — `:831`; `validation.run_l0(...)` — `:839`; `validation.run_l1(...)` — `:1084`; `submit.LocalClusterSubmitter` — `:169`; `submit.PipelineSubmitter` Protocol — `:74`
- `queue_lane.MATERIALIZATION_FLAG = "FEATUREGEN_MATERIALIZE_ENABLED"` — `:211`; `materialization_enabled()` — `:503`; `process_materialization_once(conn, *, owner, config=None)`; `MATERIALIZATION_HANDLER = "materialization.compile.v1"`; `COMPILE_BUDGET_SECONDS = 600.0`; `MaterializationJobV1(request_id, work_item_ids, spine_declaration, cadence, availability_promise, mechanism, published_schema, contract_overrides=None)`; `MaterializationLaneConfig(inventory, project_root, l0, assemble_nodes=_wired_assembler, clock=_utc_now_iso, compile_budget_seconds=…)`; `lane_config_from_env()` — `:572`
- the four companion env vars, all unset by default and each raising `ValueError` by name: `FEATUREGEN_MATERIALIZE_PROJECT_ROOT`, `..._INVENTORY`, `..._L0_PYTHON`, `..._L0_TIMEOUT_SECONDS` — `queue_lane.py:204-207`; `MATERIALIZATION_ENV_VARS` is the 5-tuple, and a test asserts each is documented in `.env.example` **and** `deploy/kind/k8s/20-backend.yaml` (25-worker.yaml is **not** covered — see E2)
- `request_store.RequestLifecycle` five lowercase states (`requested/accepted/running/committed/failed`); `LEGAL_LIFECYCLE_TRANSITIONS` — **no `requested → failed` edge**, DEFERRED-WORK A.35; `MaterializationRequestV1` 14 fields; `record_request / accept_request / renew_lease / advance_lifecycle / read_request / expired_requests`; `advance_lifecycle` refuses `to_state=ACCEPTED`
- `reconcile.ReconciliationVerdict` 8 members; `reconcile_abandoned_requests(...)` — `:313`
- `control_plane.RunEventKind` 8 members, 4 terminal (`GATES_FAILED`, `PUBLISHED`, `PUBLICATION_REFUSED`, `RUN_FAILED`) — `:94`; `RunStatus` 8 members — `:124`; `PUBLICATION_REFUSED → RunStatus.REFUSED` — `:148`
- `codes.PublicationRefusalCode` exactly three: `CAPABILITY_UNPROVEN`, `GROUP_BINDING_CONFLICT`, `PUBLISH_MECHANISM_UNSUPPORTED` — `:100`. `CompilationRefusalCode` has since gained `FORMULA_SCHEMA_UNSUPPORTED` (BR-6) and `ValidationFindingCode` gained `ENGINE_VERSION_MISMATCH` (A.42)
- `api/routes/materialization_runs.py` — registered `api/app.py:263`; ingress `frontend/vite.config.ts:27` + `deploy/kind/nginx.conf:16`
- migrations present: 1034 (control plane + `publication_capability_attestation`), 1044 (event ordering), 1053 (`materialization_request`), 1054 (`materialization_compiled_artifact`); **1055 reserved for G-3's active-revision pointer** (stated in 1053's header)

**Deferral ledger**
- `docs/DEFERRED-WORK.md` — A.26 (`:473`, capability-attestation handoffs, 16b owns the live probe),
  A.36 (`:804`, chain/lane row CLOSED, `execution_tier` row OPEN), the G-1 publish-pointer row
  (`:883`, *"every run this program can currently produce ends in a refusal, and that is the truthful
  terminal"*), the joined-dimension PIT gap (`:300`).

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
task B4); **1055** `feature_active_revision` (G-3, task D3). Nothing else. All deploy backend-first,
under the standing explicit-approval rule.

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

---

## 5. Phase D — G-2 and G-3: the chain reaches a published table *(weld 3)*

### Task D0 — disarm the `PublishStepMissing` landmine (½ day) — **do this FIRST**

§0.4. Until the publish step exists, an operator ingesting one passing attestation crashes every run.

**Modify:** `src/featuregen/materialize/queue_lane.py` — catch `PublishStepMissing` in
`process_materialization_once` and fail the request with a named, operator-legible reason (the
exception's own docstring says it is "exported and named so a queue lane can classify it").
**Modify:** `src/featuregen/materialize/publish.py` — `record_attestation` refuses to write a
`passed` attestation while no publish step is registered, naming G-3. A capability record that makes
the platform crash is not a capability record.

**Acceptance (tests):**
- `test_recording_a_passing_attestation_is_refused_until_the_publish_step_exists`
- `test_the_lane_classifies_PublishStepMissing_instead_of_crashing`
- Both tests are **deleted by D3** — record that in the task, so the guard cannot outlive its cause.

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

**Modify:** `queue_lane.py` — `MaterializationLaneConfig` gains the submitter and the metastore
adapter; both `None`-able, and `None` is an honest **outcome** (an unprepared run), never a skip —
the same rule `run_l0` already follows.

**Acceptance (tests):** extend `tests/featuregen/materialize/test_chain.py`
- `test_a_passed_L0_advances_to_prepare_run`, `test_a_failed_L0_never_prepares`
- `test_the_prepared_parameters_are_exactly_REQUIRED_RUN_PARAMETERS`
- `test_a_submission_failure_is_RUN_FAILED_with_the_returncode_in_the_detail`
- `test_the_chain_still_appends_no_PUBLISHED`
- The real-JVM half runs in `l0_gate.py`'s sibling (`make l0-gate`), not the default suite —
  `pyspark`/`kedro` are deliberately not dependencies of this platform.

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

---

## 6. Phase E — proof

### Task E0 — the seam walkthrough gate (1 day) — **this plan's own acceptance test**

One test, in the default suite, that walks §0.5 items 1–4 against a real DB and a seeded catalog:
served candidate → reviewed v2 expectation → shadow work item → authored (recorded-fixture)
result → activation allows `execute_materialization` → `POST /materialization-runs` accepts →
the lane compiles → `run_l0` **passed** → the compiled read set **equals the frozen envelope's**.

Modelled on the parent plan's Task E0 (`bd43964d`), which is the precedent for a walkthrough gate
that runs in CI.

### Task E1 — the governed contract materializes (1½ days)

Extends E0 through §0.5 items 5–7 in the JVM gate (`make l0-gate` sibling), not the default suite:
`prepare_run` → `run_l1` → `submit` → publish → the object is queryable.

### Task E2 — kind cluster acceptance (1 day engineering + **operator action**)

**Two real deployment blockers must be closed FIRST, and they are engineering, not operations:**

1. **`deploy/kind/k8s/25-worker.yaml` carries no `MATERIALIZE` env at all** — not the flag, not the
   four companion settings. The worker Deployment postdates Phase G and never received them. Since
   the worker is the only thing that compiles, a flag flipped on the backend alone accepts requests
   nothing will ever claim.
2. **`FEATUREGEN_MATERIALIZE_INVENTORY` has no usable value in-pod.** The only inventory in the repo
   is `conf/environments/hdfc-local-inventory.yml`, which `load_inventory` **refuses by design**
   (`engine_versions.hive` is null, `tables: {}` — it is a template, and DEFERRED-WORK `:230` says
   Task 0 owns filling it against the live cluster). It is not copied into the image. The other three
   settings do have usable values since the followups branch added `/opt/kedro-venv` with
   kedro + pyspark. **This is Task 0 of the codegen program, unstarted, and it gates E2 absolutely.**

Then, and only then: deploy backend-first with migrations 1055/1066/1067; flip
`FEATUREGEN_MATERIALIZE_ENABLED` on backend **and** worker; run the probe; run one governed feature;
read the table. **Explicit user go required** — live cluster, cluster spend, and a durable capability
attestation. Never without it.

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
- Migrations **1055** (reserved, G-3), **1066**, **1067** — those three and no others. The free
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

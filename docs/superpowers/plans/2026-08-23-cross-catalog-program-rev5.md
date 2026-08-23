# Cross-Catalog Program — Revision 5 (consolidated, self-contained)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. This document is **self-contained**: no task requires any earlier revision. `2026-08-23-cross-catalog-end-to-end-program.md` (Rev 4) and `...-rev1-phaseABC-archive.md` are historical records only — a banner in each says so. Execution order: T0 → Stage 1A → 1B → 1C → (gated) Stage 2. Per the fourth review: **A1 and A2 may execute immediately after T0**; everything else in Stage 1 follows this revision's corrected contracts.

**Program objective.** A hypothesis produces recipe-based and LLM-proposed features from any authorized catalog; every cross-catalog feature carries a deterministic, point-in-time-safe physical plan; a selected feature travels selection → immutable pins → formula → Kedro → validation without changing its frozen identity. **Stage 1 claims:** cross-catalog *planning* becomes real, governed, and observed — it does **not** change the hypothesis response payload or the served option set (it DOES change runtime/operational behavior: new telemetry writes, and B5 deliberately fixes a materialization-route bug). **Validation vocabulary:** Code validation (L0) / Fixture validation / Sandbox data profiling / Production certification — a fixture pass never marks data validation.

**Verified base:** all §V facts verified against `origin/main` @ `a1947d07` across sixteen verification passes (2026-08-23). T0 re-verifies the delta if the baseline moves.

## Global Constraints

- Baseline per T0. Migrations reserved: **1120** (observation + demand + review-event tables), **1121** (telemetry outbox). Coordinate with the run-spine reservations (~1099–1117) via the plan-doc registry.
- No new env flags. No Template field additions (§V7). No activation-vector changes anywhere in Stage 1 (the `GOVERNED_SERVING_POLICY_VERSION` key + bump ship atomically with Stage-2 serving). No changes to `PHYSICAL_PLAN_VERSION` material or `_decision_manifest` keys (additive only). New blocker codes must be registered in `REASON_FAMILIES` (pin-tested closed registry).
- Fail-closed invariants preserved: `_reject_cross_catalog_llm`; the H1c confirm interlock; the LLM never chooses columns or joins (`FORBIDDEN_PHYSICAL_KEYS`; binding hints user-origin-only); `find_cross_catalog_path` unreachable from governed paths; LLM `deterministic_formula` demoted to `conceptual_pattern` at request construction.
- **One normalization law:** `object_ref.normalize_ref`/`parse_ref` (`source::schema.table[.column]`) is the only qualified-ref vocabulary. No second parser (§V13).
- **Identity discipline:** the persisted `semantic_option_decision.source_definition_id` stays the CANONICAL definition id — registry consumers key on it (§V11). Variant identity is separate fields, never a mangled id.
- No FK to `graph_node` (rebuilt per ingest); runs/intents ARE FK targets (durable). Never `conn.commit()` in API tests. Entity resolution via `key_entity`/`object_grain` only.
- **Regression gates that prove what they claim:** file-freeze gates are `git diff --exit-code <T0-baseline>..HEAD -- <files>` (a clean worktree diff proves nothing after commits); identity pins compare against **literal pre-change hash values captured before the change lands** (computing before/after with the same new code proves only self-agreement); display tests assert **exact expected display strings** for one recipe and one intent fixture (an `!=` between name and id can false-fail and proves nothing).
- Operator actions (deploys, flag flips, live migrations) are never executed by this program.

---

## §V Verified facts (consolidated; each numbered fact carries its evidence)

- **V1** `_contract_blockers` emits `CONCEPTUAL_PATTERN_NOT_AUTHORABLE` (`activation_policy.py:130-134`) and `activation_decision` maps both `create_contract` AND `author_formula` to it (`:222-223`) → LLM-origin options dead-end without a promotion journey (Stage 2).
- **V2** Shadow capture runs at SERVE time, candidate-keyed, pre-selection (`contract.py:1003,1046-1048`); selection→code needs the run/build-set coordinator (Stage-2 dependency).
- **V3** `fold_definition_readiness`/`fold_readiness` over `ReadinessInputsV1` (`recipe_readiness.py`) is the only legitimate readiness answer; `ReadinessInputsV1` requires `retired`, which `FeaturePlanningRequestV1` does not carry (§S1A-6).
- **V4** The runtime dataset dialect is `catalog::schema.table` (`render/nodes_compute.py::_hop_datasets`).
- **V5** `_governed_idea_from_result` (`gate1.py:562-594`) populates identity/envelope/provenance only — no role bindings, no operation semantics, no policy pins.
- **V6** `catalog_source` is a logical ingest label, not an execution connection (physical-table-config program owns the binding).
- **V7** `recipe_grounding_context.py` enumerates Template fields DYNAMICALLY (`_TEMPLATE_FIELDS = frozenset(field.name for field in fields(Template))`) — any new Template field moves every legacy `template_content_hash`.
- **V8** `IngredientBindingV1` (`planner/contracts.py:372-385`): `recipe_id, need_role, concept, required_grains, join_role, temporal_role, bound_catalog_source, bound_object_ref, actual_source_grain, binding_quality, safety, reason_codes` — **no authority fields**.
- **V9** `physical_plan_id = "bp_" + sha256(...)[:16]` (`contracts.py:681`) — truncated, and its material excludes parameters and compiled declaration evidence. Full content hashes exist separately: `contract_input_hash` / `declarations_output_hash` (`planner/fingerprint.py`). `planning_request_hash` is field-exhaustive including `parameter_values`. `choose_params` is a legacy-menu, one-override-per-template closed selection (`contract/param_choice.py`).
- **V10** `feature_generation_run` rows are minted before the builder runs (`ensure_generation_run(..., intent_id=...)`); `contract_intent` is durable — both are FK targets.
- **V11** `assemble_current_activation_state` keys the registry on the persisted id: `v2_recipe_by_id(frozen.source_definition_id)` then `definition.recipe_id` (no None-guard — the wrapping `try` folds a miss to `review_now=False`), and `_formula_schema_supported(frozen.source_definition_id)` (`semantic_option_decision.py:488-499, 571-579`). A variant-mangled persisted id silently review-blocks and schema-blocks every governed recipe option.
- **V12** `ResolutionPinV1` carries `conflict_state` ("resolved" | named unresolved reason | "no_policy") and `load_bearing` (`field_resolution.py:565-581`). The existing floor projection reads only `pin.producer` — conflicted/pending pins can clear floors today (`semantic_option_decision.py:540-543`). Fixed for both arms in §S1A-5.
- **V13** `object_ref.py` owns `normalize_ref`/`parse_ref`: `source::schema.table[.column]`, exact arity (2 or 3 path parts), source-name validation, deterministic round-trip (`object_ref.py:75-110`).
- **V14** `TemporalDeclarationV1.anchor_binding: str | None` is an UNQUALIFIED ref chosen as `next(iter(bound_refs))` (`contracts.py:479`, `declarations.py:328-343`) — ambiguous across catalogs.
- **V15** `llm_intent_candidates` returns `(candidates, rejections)` with three consumers; `V2RecipeCandidateV1.planning_request` carries the typed request — collect requests FROM the candidates in gate1, never change the return shape (§S1B-3).
- **V16** At the planner's reject site the frontier holds the failing `EntityRelationshipRefV1`, `_Position(entity, catalog, table_ref)`, and (inside the realizer probe) the realizing catalogs with `from_key_ref`/`to_key_ref` — all discarded today (`assembly.py:460-590`). Defaulted `BindingPlanV1` fields are identity-safe (hash materials enumerate constructor args). Rejected plans enter no sealed hash, but `physical_plan_id` dedup can drop them (`plan.py:288`) — observation identity must not depend on the plan list.
- **V17** Registries: legacy 157 / V2 317 / overlap 106 / legacy-only 51 / V2-only 211 (measured by import). Review validity is event-sourced (`review_validity` + `by_role_at_revision` at `canonical_recipe_v2_hash`); `required_reviewer_roles` adds `model_risk` for near-label/outcome leakage.
- **V18** The `planning_probe`/`plan_planning_request` seam exists unwired (`planner/requests.py:42-76`) with two measured defects: 58/317 recipes raise in `derive_need_metadata` (missing source anchor; swallowed by `assembly.py:66-69`), and 37/317 are shadowed by `RESOLVED_NEED_METADATA` id collisions (`candidates.py:47`).
- **V19** The E4 cutover left the entity-only route path dead (422 `SEMANTIC_REQUIRES_CATALOG_SOURCE` before the builder): the entity-scoped governed branch and `run_shadow_planner` are unreachable from production routes.
- **V20** Multi-catalog read plumbing already works: `_candidate_refs`/`build_metadata_snapshot`/`capture_column_snapshot` are per-(catalog, ref); `materialize/ir.py::_envelope_ref` parses `cat::ref`; `check_projection_readiness` is global.

## §D Design rulings (consolidated)

- **D1 One deterministic planner** owns columns and joins for both origins; requests converge via the fixed probe seam.
- **D2 Identity model.** Persisted/card `source_definition_id` = canonical definition id (`incoming_amount`). New ADDITIVE fields wherever an option/observation is recorded: `definition_origin` (`recipe_v2 | llm_intent`), `governed_variant_id` = `"gvar_" + sha256(canonical_id | planning_request_hash | physical_plan_content_hash | plan_envelope_version)` (full digests in the material; the 16-hex `bp_` id remains display/lookup only), plus the component hashes stored separately (`planning_request_hash`, `physical_plan_content_hash` = `contract_input_hash` when compiled else the plan's own full-material sha256, `parameter_binding_hash` where minted). `recipe_id` is recipe-origin-only (NULL/None otherwise); review and formula-expectation lookups run only for recipe origin.
- **D3 Qualified refs.** A typed wrapper over `object_ref.parse_ref`/`normalize_ref`; governed read sets require the column form; refusal (never a skip) on: parse failure, missing column, or `normalize_ref(*parse_ref(raw)) != raw`; parsed count must equal stored count.
- **D4 Authority.** `authority = f"{pin.producer}/{pin.strength}"` ONLY when `pin.conflict_state == "resolved" and pin.load_bearing`; else `"absent"`. `evidence_id` rides the binding. Applied to BOTH the existing single-catalog floor and the new pair floor (one shared helper).
- **D5 Telemetry off the critical path.** The canonical governed pass runs in a WORKER from a durable outbox row persisted in the request transaction — the user response returns first. Frozen inputs (run id, intent id, scope material, snapshot id, request set) ride the outbox row; the worker restores, plans, and writes observations + demand. Lease/reclaim/reconciliation copy the `recipe_formula_worker` idiom.
- **D6 Observations before demand.** One append-only `governed_planning_observation` row per planned request (ALL outcomes); `bridge_demand_observation` is a rejection-specific CHILD referencing its observation. Idempotency includes `observation_mode`; intent lineage is `IS NOT DISTINCT FROM` the run's; reproducibility pins ride every observation.
- **D7 Two-wave evaluation.** Stage 1C computes wave-1 metrics from stored observations against a reviewed hypothesis corpus, with explicit acceptance thresholds; wave-2 metrics (served-ranking quality, SME review of served cards incl. incremental cross-catalog relevance) gate BROAD enablement after first activation.
- **D8 Honest UI/counts.** Governance surfaces label observation modes, roll up by full identity-relevant fields (aggregate-then-limit, cursor pagination, `as_of`), and show distinct intents vs runs vs observations. Bridge-evidence tiers use the real lifecycle vocabulary; never "approved" below VERIFIED.
- **D9 Review controls are four distinct things** (Stage 2): deterministically validated formula draft; human-reviewed individual formula; certified formula-generation method; production publication approval. "Reviewed formula" without one of these names is not a requirement.

---

# STAGE 0 — Task T0: baseline + delta report

> **T0 EXECUTED 2026-08-23.** Baseline = `origin/main` @ `850c371d` — PR #18 merged the reapply
> lineage into main (run-spine, recipe-to-code coordinator, sandbox worker, migrations to 1118),
> mooting the lineage question. Delta report in the SDD ledger
> (`.superpowers/sdd/2026-08-23-cross-catalog-program-rev5/progress.md`): all Sec-V facts hold; two
> amendments — `FeatureIdea.grain_refs` is now plural (S1A-4 sets the tuple; `grain_ref` is a
> derived property) and the governed binding-plan dict mirrors the new `"grain_refs"` key; plus
> the builder signature carries `budget: CompileBudget | None = None` (preflight-scan ruling).
> **Migrations 1120 + 1121 are hereby reserved for this program** (main tops at 1118).

- [ ] **T0.1 (user input):** confirm the integration lineage (`origin/main` vs `feature/asset-detail-reapply`). Record it here.
- [ ] **T0.2:** branch; if not `a1947d07`, produce a written **delta report** re-verifying every §V fact against the new baseline and mapping each to the tasks that depend on it, with a per-task re-GO. Named load-bearing assumptions: V11 (identity consumers), V12 (pin fields), V13 (parser), V19 (dead paths), the serve-time synthetic state, gate1's branch shape.
- [ ] **T0.3:** post migration reservations 1120+1121 to the shared reservation registry.

---

# STAGE 1A — Pure planner contracts

## Task S1A-1: `planning_probe` source anchor (may start immediately after T0)

**Files:** `src/featuregen/overlay/upload/planner/requests.py`; `tests/featuregen/overlay/upload/planner/test_requests.py`.

- [ ] Failing tests:

```python
def test_probe_carries_the_source_anchor_for_a_multi_entity_recipe():
    from featuregen.overlay.upload.need_metadata import derive_need_metadata
    from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id

    recipe = v2_recipe_by_id("own_transfer_outflow_amount")   # account + beneficiary keys
    probe = planning_probe(planning_request_from_recipe(recipe))
    assert probe.source_entity == recipe.source_grain
    assert probe.source_entity_need_role is not None
    derive_need_metadata(probe)                                # must not raise


def test_probe_anchor_derives_for_every_v2_recipe_or_leaves_planner_fallback():
    from featuregen.overlay.upload.need_metadata import derive_need_metadata
    from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES

    raising = []
    for recipe in V2_RECIPES:
        probe = planning_probe(planning_request_from_recipe(recipe))
        try:
            derive_need_metadata(probe)
        except ValueError:
            raising.append(recipe.recipe_id)
    assert raising == [], f"{len(raising)} probes still raise: {raising[:5]}"


def test_incompatible_sole_entity_key_is_never_an_anchor():
    """A sole entity key whose allowed grains EXCLUDE the source grain must yield None/None
    (planner fallback), never a mis-anchor the planner cannot bind."""
```

- [ ] Implement:

```python
def _source_anchor(request: FeaturePlanningRequestV1) -> tuple[str | None, str | None]:
    """The probe's source anchor from the V2 contract's own declarations: the entity_key
    operand COMPATIBLE with the source grain (allowed grains name it, or are unconstrained).
    None/None when absent or ambiguous — the planner's fallback then applies and a genuinely
    ambiguous recipe fails with the planner's NAMED error instead of raising."""
    keys = [op for op in request.operands if op.operand_class == "entity_key"]
    compatible = [op for op in keys
                  if not op.allowed_source_grains
                  or request.source_grain in op.allowed_source_grains]
    if len(compatible) != 1:
        return None, None
    return request.source_grain, compatible[0].role
```

bind once, pass `source_entity=`/`source_entity_need_role=` into the `Template(...)` construction.
- [ ] Run the file; if the sweep still lists ids, resolve each from its contract or pin it as a named planner error. Commit — `fix(planner): planning_probe derives a grain-compatible source anchor from the V2 contract`

## Task S1A-2: identity-neutral registry bypass (may start immediately after T0)

**Files:** `planner/plan.py` (signature), `planner/candidates.py`; tests as S1A-1.

- [ ] `plan_bindings` gains keyword-only `metadata_resolution_mode: str = "legacy_registry"` (closed: `legacy_registry | request_contract`, validated), threaded to `discover_ingredient_candidates`; the lookup becomes `resolved = {} if metadata_resolution_mode == "request_contract" else {...}`. `plan_planning_request` passes `"request_contract"`. **No Template change** (V7).
- [ ] Tests: a poisoned `RESOLVED_NEED_METADATA` entry under a colliding id is not consumed in request-contract mode AND still is in legacy mode; **the identity pin uses literal pre-change values**: capture `recipe_content_hash(<one legacy template>)` and `template_content_hash` equivalents as string literals in the test BEFORE this task merges, and assert equality after.
- [ ] Commit — `fix(planner): request-contract metadata resolution is a planner argument, never a Template field`

## Task S1A-3: the identity model (D2)

**Files:** `src/featuregen/overlay/upload/contract/governed_identity.py` (new, pure); tests beside it.

- [ ] Implement + test:

```python
@dataclass(frozen=True, slots=True)
class GovernedVariantIdentityV1:
    canonical_definition_id: str
    definition_origin: str                 # recipe_v2 | llm_intent
    planning_request_hash: str             # full
    physical_plan_content_hash: str        # full: contract_input_hash when compiled,
                                           # else sha256 over the plan's full id material
    parameter_binding_hash: str = ""       # where the engine minted one
    plan_envelope_version: str = "1"

    @property
    def governed_variant_id(self) -> str:
        material = "|".join((self.canonical_definition_id, self.definition_origin,
                             self.planning_request_hash, self.physical_plan_content_hash,
                             self.parameter_binding_hash, self.plan_envelope_version))
        return "gvar_" + hashlib.sha256(material.encode()).hexdigest()
```

Tests: two parameter variants of one recipe → distinct `governed_variant_id` even when their (truncated) `bp_` ids collide; the canonical id is recoverable verbatim; origin purity (`recipe_id` semantics live in consumers, pinned in S1A-4). Commit.

## Task S1A-4: the complete governed option

**Files:** `src/featuregen/overlay/upload/contract/governed_lens.py` (new); `planner` additions below; tests `tests/featuregen/overlay/upload/contract/test_governed_lens_requests.py`.

**Planner additions this task owns (all additive, defaulted, identity-safe per V16):**
- `BindingPlanV1.output_grain_ref: tuple[str, str] | None = None` — the QUALIFIED terminal target grain key `(catalog, ref)`, emitted by the assembler where the terminal table/key is known (the roll-up's landing position) — **never rediscovered from ingredient bindings** (a transaction→account→customer plan may have no customer-key ingredient binding).
- `TemporalDeclarationV1.anchor_catalog_source: str = ""` — set beside `anchor_binding` at the site that chooses it (`declarations.py:338`), disambiguating same-named refs across catalogs (V14).

**The carrier:**

```python
@dataclass(frozen=True, slots=True)
class DefinitionGovernanceStateV1:                    # origin-neutral readiness inputs
    retired: bool
    review_current: bool
    review_missing_roles: tuple[str, ...]
    reviewed_expectation: bool
    policy_revision_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GovernedOptionV1:
    idea: FeatureIdea
    request: FeaturePlanningRequestV1
    identity: GovernedVariantIdentityV1
    governance: DefinitionGovernanceStateV1
    readiness: RecipeReadinessV1                     # the FOLD's output
    display_name: str                                # display is never identity
    business_definition: str
    unmapped_requirement_codes: tuple[str, ...]      # FeatureIdea has no such field — carried here
```

**Builder contract** — `governed_options_from_requests(conn, *, requests, target_entity, roles, now, budget=None) -> tuple[list[GovernedOptionV1], list[dict]]`, per-request savepoint + `planner_internal_error` isolation (copy `gate1.py:621-633`), one shared scope/compiler context (copy `gate1.py:602-611`), and a **two-pass batched enrichment (no N+1):**

1. Plan every request (bounded by `budget`).
2. Collect across ALL resolved plans: every qualified ref, every recipe id.
3. ONE `current_resolution_pins` call over the union; ONE batched `key_entity`/`object_grain` read (extend those helpers with batched variants or one `graph_node` query over the ref set); ONE `review_events_all(conn)`.
4. Project every `GovernedOptionV1` from the frozen batch — the same batch feeds display evidence and the later activation facts (never re-read).

Enrichment rules:
- `idea.derives_pairs == _plan_read_set_pairs(plan)` (the FULL physical read set); `{b.ref for b in input_role_bindings if b.ref} <= set(derives_pairs)`.
- `input_role_bindings` from `IngredientBindingV1` (role → `(bound_catalog_source, bound_object_ref)`), each with D4's conflict-gated authority + `evidence_ids=(pin.evidence_id,)`.
- `grain_ref = plan.output_grain_ref`; `time_ref = (temporal.anchor_catalog_source, temporal.anchor_binding)` when both present, else `None`.
- Operation semantics populated from the request: `operation_kind`/`operation_class` from computation kind + formula result class, `window` from the resolved window parameter, `grouping_refs` from dimension-class bindings, output unit/currency policy from `request.output`, eligibility policy refs from `request.eligibility.policy_refs`.
- `personal_data_policy_revision_ids`: the same policy-pin read the engine path uses for its carriers — first implementation step locates that producer (grep `personal_data_policy_revision_ids` writers) and reuses it over the governed read set; a personal-data-bearing ref with no active purpose keeps the engine's honest outcome (the `PERSONAL_DATA_POLICY_REQUIRED` requirement code), never silence.
- Requirements via **closed builders**: `_REQUIREMENT_BUILDERS: dict[ReasonCode, Callable[[ReasonContextV1], Requirement | None]]` where `ReasonContextV1` carries the affected qualified operand + plan facts; builders construct schema-correct `Requirement` objects (read `feature_assist.Requirement`'s exact fields first — a prior production bug came from a wrong requirement schema version); hard blockers stay blockers (builder returns None and the code stays a refusal); unmapped codes land in `unmapped_requirement_codes`.
- Readiness: origin-neutral `fold_request_readiness(request, governance: DefinitionGovernanceStateV1, *, temporal_blockers, binding_blockers, governed_policy_blockers) -> RecipeReadinessV1` over `ReadinessInputsV1` — `retired` from governance (recipe: `definition.readiness == "RETIRED"`; intent: `False`), `reviewed_expectation` from governance; `fold_definition_readiness` stays the recipe adapter.
- Origin purity (R14): `idea.recipe_id` set only for `recipe_v2`; governance state for intents is `DefinitionGovernanceStateV1(retired=False, review_current=False, review_missing_roles=(), reviewed_expectation=False, policy_revision_ids=())` with zero registry lookups.
- `idea.source_definition_id = identity.canonical_definition_id` (V11 — canonical everywhere it persists); the variant id travels ONLY in `identity` (and Stage-2 decides the served-twin map rule).

- [ ] Tests: the real-recipe cross-catalog resolution (pin one TRANSACTION_FOUNDATION recipe by the documented discovery rule; seeds derived from its operands, failing loudly via verdict codes when wrong); the intent-origin request through the same builder; the two invariants above; exact display values for both fixtures; conflict-gated authority (a pin with `conflict_state="conflict"` yields `"absent"` and blocks the floor later); batched-read pin (query counter: enrichment issues a CONSTANT number of queries regardless of request count — assert with a counting cursor wrapper); origin purity; retirement (a RETIRED recipe folds RETIRED, never active).
- [ ] Commit — `feat(governed-lens): complete governed options — batched frozen evidence, conflict-gated authority, origin purity`

## Task S1A-4c (added by execution ruling 2026-08-23): projection-time role declaration

**Why (verified during S1A-4b review):** 0 of the V2 registry's 1195 operands declares a `join_role`/`temporal_role`, and request-contract discovery reads the operand's OWN field (never derives) — so no recipe-origin cross-catalog frontier can start (G1). The naive legacy derivation would misclassify status/dimension operands as MEASURE (G2), and every genuine measure over a bridge refuses `physical_cardinality_unavailable` because no plan ever carries a bridge-realization revision (`attach_executable_bridge_realizations` has zero callers; G3 — CONFIRMED on the legacy path too: the old fixture resolved only because it had no measure at all).

**Scope:** `planning_probe` sets explicit `join_role`/`temporal_role` on each projected `Need` from DECLARED operand facts only — `operand_class` + the concept registry's `entity_link`/`pit_role` — with a design-read step first: how does the legacy derivation treat the SAME concepts for dimension/status-class operands, and what role keeps them out of aggregation staging honestly? Registry-wide sweep tests; identity pins.

**Acceptance:** the pinned pack recipe's frontier STARTS (past G1), and the primary refusal becomes `physical_cardinality_unavailable` — pinned as the honest boundary until the G3 charter closes.

**G3 + G2-residual are NOT Stage-1A scope:** realization attachment changes bridge-segment identity (the revision enters segment identity material) and belongs with the bridge-admission machinery; the decision on WHERE it lands is taken at the Stage-1C report with the realization-gap queue's evidence in hand. Recorded as a first-class charter: until it closes, recipe-origin cross-catalog RESOLUTION rate is structurally 0 and the telemetry's value is the refusal taxonomy + demand queues.

## Task S1A-5: qualified-ref wrapper + conflict-gated floors (shared fix)

**Files:** `src/featuregen/overlay/upload/qualified_ref.py` (thin wrapper over `object_ref`); `semantic_option_decision.py` (loader + BOTH floor arms); `activation_policy.py` (two appended defaulted fields on `FrozenOptionFactsV1`: `plan_kind: str = ""`, `read_set_pairs: tuple[tuple[str, str], ...] = ()`); tests in the module's suites.

- [ ] Wrapper (D3): `parse_qualified_ref(raw) -> QualifiedRefV1` delegating to `parse_ref`, refusing `column is None` and any non-round-tripping input; NO second grammar.
- [ ] Loader: `plan_kind`/`read_set_pairs` mapped from the frozen plan; in a `governed_cross_catalog` plan every entry must parse (count equality) else `OptionDecisionIntegrityError`; **route handling verified and pinned** — read how draft/confirm/materialization surface that error today; if a 500, add the typed 409 (`"regenerate from the current considered set"`) and pin it.
- [ ] Floors: extract ONE shared helper used by both arms; the single-catalog arm's authority projection gains the D4 conflict/load-bearing gate (**this fixes the pre-existing V12 weakness for engine options too** — its own test: a conflicted pin no longer clears the floor); the pair arm qualifies per `QualifiedRefV1`.
- [ ] Governed facts (`decision_facts_for_governed_option(conn, option, *, context_hash, uoa_entity=None, spine_ref=None)`): key-for-key with `decision_facts_for_candidate` — `source_definition_id = identity.canonical_definition_id` (V11), `generation_source` by origin, `computation_kind = request.computation_kind`, `binding_state="bound"`, `readiness` = the fold's status with its blockers in `dataset_story.readiness_blockers`, `review_current` from governance, `recipe_revision_hash = request.source_content_hash`, `plan_envelope_present=True` with the governed `binding_plan` dict (`plan_kind="governed_cross_catalog"`, `catalog_sources`, qualified `read_set`, `role_bindings`, `ordered_path`, `output_grain`, `physical_plan_id`, `bridge_realization_dependencies`), `policy_revision_pins={"authority_matrix_hash": authority_matrix_hash()}`, populated evidence (planning request verbatim; per-binding eligibility audit `{role, catalog_source, object_ref, authority, evidence_ids}`; ingredient verdict facts; governed-plan block with ordered path + contract codes; the identity fields), `_decision_manifest`-mirroring manifest. Round-trip safety test: persist → load → assemble → `activation_decision("create_contract")` — recipe-origin unreviewed → `RECIPE_REVIEW_NOT_CURRENT`; intent-origin → `CONCEPTUAL_PATTERN_NOT_AUTHORABLE`; fully-reviewed + floors-clear → allowed; **and the V11 pin: with facts persisted, `assemble_current_activation_state`'s registry lookups succeed (the canonical id resolves; a variant-mangled id is asserted to be absent from the row)**.
- [ ] Fix the materialization `intent_id` omission (`materialization_runs.py:495-527` resolves the intent from `contract_considered_revision` and passes it — the pre-existing permanent-drift bug), with its regression test. This is the one intended Stage-1 route-behavior change and the program header says so.
- [ ] Commits per surface.

### Stage 1A gate
Planner + governed-lens + facts suites green; the literal-value identity pins hold; `git diff --exit-code <T0-baseline>..HEAD -- src/featuregen/overlay/upload/templates.py src/featuregen/overlay/upload/contract/live_activation.py` clean; backend `make lint typecheck`; failure-set vs pre-branch baseline recorded.

---

# STAGE 1B — Telemetry substrate

## Task S1B-1: migrations 1120 + 1121

**1120 — three append-only tables** (1062 trigger idiom: row-level UPDATE/DELETE + statement TRUNCATE; header comments carry the reservation rationale):

`governed_planning_observation` — one row per planned request, ALL outcomes:
```
observation_id PK · generation_run_id NOT NULL FK→feature_generation_run(<pk verified first>)
intent_id NULL FK→contract_intent(intent_id) · observation_mode CHECK IN ('live','telemetry')
definition_origin CHECK IN ('recipe_v2','llm_intent') · canonical_definition_id NOT NULL
recipe_id NULL (recipe-origin only) · governed_variant_id NOT NULL
planning_request_hash NOT NULL · parameter_binding_hash NOT NULL DEFAULT ''
physical_plan_content_hash NOT NULL DEFAULT '' · selected_physical_plan_id NOT NULL DEFAULT ''
contract_id NULL · primary_objective NOT NULL DEFAULT '' · target_entity NOT NULL
anchor_catalog_source NOT NULL DEFAULT '' · resolution_status NOT NULL
reason_codes jsonb '[]' · participating_catalogs jsonb '[]' · hop_count int DEFAULT 0
bridge_count int DEFAULT 0 · authority_floor_status NOT NULL DEFAULT ''
safety_status NOT NULL DEFAULT '' · readiness NOT NULL DEFAULT ''
intent_corroborated boolean DEFAULT false · param_divergence jsonb '[]'
catalog_scope_material jsonb '{}'          -- the scope's STABLE shape (catalogs+versions+entity),
                                           -- never the watermark-bearing scope_id alone
metadata_snapshot_id NULL · compiler_input_fingerprint NOT NULL DEFAULT ''
recorded_at timestamptz now()
UNIQUE (generation_run_id, observation_mode, governed_variant_id)
```
Intent-lineage trigger: `NEW.intent_id IS NOT DISTINCT FROM (SELECT intent_id FROM feature_generation_run WHERE <pk> = NEW.generation_run_id)` — missing intent is representable only when the RUN genuinely has none.

`bridge_demand_observation` — rejection-specific CHILD: `demand_id PK · observation_id NOT NULL FK→governed_planning_observation · demand_queue CHECK IN ('bridge_demand','realization_gap','planner_capacity') · demand_identity_hash NOT NULL (FULL sha256 over: recipe_revision_hash | relationship_id | relationship_version | from_entity | to_entity | position_catalog | position_table_ref | hop_index | verdict | GRAPH_VERSION | PLANNER_VERSION; capacity queue: reduced material recipe_revision_hash | verdict | anchor | versions, hop fields at defaults) · relationship/position/verdict columns as the unmet hop carries · realizers jsonb · near_side_key_refs jsonb · to_endpoint_hint (ADVISORY, non-identity, says so in the comment) · UNIQUE (observation_id, demand_identity_hash)`.

`governed_plan_review_event` — append-only SME judgements for Stage 1C (1060 idiom: reviewer, role, decision, rationale, the observation reviewed, supersedes chain).

**1121 — `governed_telemetry_outbox`** (D5): `work_item_id PK · generation_run_id FK · intent_id NULL FK (same trigger) · frozen inputs jsonb (request set refs, scope material, snapshot id, roles, target_entity, anchor catalog) · status CHECK IN ('queued','leased','done','failed') · lease columns + attempt count (copy the recipe_formula_worker claim/lease shape) · recorded_at/completed_at`. Mutable by design (status/lease) — NOT append-only; a comment says why it is the one exception.

- [ ] Store module `governed_observation_store.py`: `enqueue_governed_telemetry(...)` (request-txn write), `claim_telemetry_work(...)`, `record_planning_observations(...)`, `record_bridge_demand(...)` (child rows; savepoint-safe caller contract), `observation_queues(...)` read side — **aggregate over the full filtered population THEN limit**, cursor pagination, explicit `as_of`; distinct intents/runs/observations, per-mode counts, recent-vs-historical.
- [ ] Tests: FK + trigger refusals; mode-inclusive idempotency (same variant, live + telemetry → two rows; replay within a mode → one); child-without-parent refused; append-only triggers; rollup ordering (aggregate-then-limit pinned with >limit distinct groups); the outbox lease lifecycle.

## Task S1B-2: the planner carries the unmet hop (V16)

As previously specified and verified identity-safe: `RealizerFactV1` + `UnmetHopV1` defaulted onto `BindingPlanV1`; `_hop_realizable_elsewhere` → `_hop_realizers` returning facts (fix its stale "VERIFIED" docstring); near-side key refs computed AT the refusal site through a per-`(catalog, table_ref)` cache threaded like `realization_cache`, capped (50 columns), **and the walk's queries counted inside the planner's budget** (the budget object gains a query counter the reject site consults). Frontier-truncation rejects carry `unmet_hop=None` and land in `planner_capacity` via the reduced identity material. Tests: `_split` seeds → realizer facts name rev's key columns; literal pre-change `physical_plan_id` pin; resolved path carries `unmet_hop=None`.

## Task S1B-3: the telemetry producer — enqueue at the route, plan in the worker

**Files:** `gate1.py` (enqueue block), `governed_telemetry_worker.py` (new), route threading; tests at all three.

- Enqueue (inside the scoped engine branch, savepoint-wrapped, fail-soft): when the route threads `telemetry_enabled` (from the EXISTING `FEATUREGEN_INTENT_SHADOW_TELEMETRY` flag) — persist ONE outbox row with the frozen inputs: `v2_eligible_ids` (route-threaded from `v2_applicability_as_result`), `engine_intent_requests = tuple(c.planning_request for c in intent_cands)` collected in gate1 from the candidates the engine already built (V15 — the return shape of `llm_intent_candidates` is untouched), scope material, snapshot id, roles. **No planning in the request transaction.** Response-payload byte-identity test flag-on vs flag-off, plus a request-transaction query-count delta pin (the enqueue adds a constant handful of statements).
- Worker: claims, restores frozen inputs, runs `governed_options_from_requests` (primary variants; request cap 60 with the deterministic order + a logged drop counter; the standard compile budget), computes the R21 deterministic parameter-divergence rows (hypothesis tokens vs `identity_projection`/allowed values — pure function, no LLM) and the intent-corroboration flag (the engine's semantic-signature match), writes one observation per request + demand children, marks done; crash → lease expiry → reclaim; a poisoned store fails the ITEM, never the platform. Wall-clock and query ceilings are worker-side and measured, not user-facing.
- Tests: end-to-end enqueue→claim→observe with real run/intent lineage; resolution and rejection observations both present; divergence and corroboration rows; reclaim after a killed lease; the drop counter.
- Commit — `feat(telemetry): durable governed-planning telemetry — outbox at the route, deterministic worker, observations for every outcome`

## Task S1B-4: legacy live recording + governance surface

- The dead-but-test-reachable entity-scoped branch records via the SAME store (`observation_mode="live"`, observations + demand children), savepoint-wrapped; its test mints the run via `ensure_generation_run` first and asserts a direct call WITHOUT a minted run records nothing (the FK makes unattributed rows impossible). The Rev-3 `run_shadow_planner` threading stays CUT (V19, YAGNI). The program states plainly: production Stage-1 evidence is telemetry-mode.
- `GET /governance/bridge-demand` (`require_confirmer`, the `list_entity_bridges` pattern): three queues from `observation_queues`, nested rollup (relationship → position table), mode labels, distinct-intent/run counts, suggested endpoints (capped 5), the seven-way `existing_candidate` taxonomy computed at read time via `canonical_bridge_endpoints` (out-of-scope indistinguishable from absent, by authorization design); cursor pagination + `as_of` echoed. Frontend `BridgeDemandPanel` in `GovernanceReviewScreen`: three labeled sections, honest-absence and 403-quiet states, capacity queue has no propose affordance; `[Propose bridge]` reuses the existing entity-bridge client. Tests per the established conventions (403 under `AUTH`, 200 under the hyphen claim; vitest partial mocks, exact copy assertions).

### Stage 1B gate
1120/1121 suites green; byte-identity + query-delta pins; worker lifecycle tests; frontend green (`npm test`, typecheck, lint); the Stage-1A file-freeze gates still clean at `<T0-baseline>..HEAD`.

---

# STAGE 1C — Offline evaluation

## Task S1C-1: the reviewed hypothesis corpus (operator + engineering)
A versioned corpus file of banking hypotheses (retail, CIB, payments, cards, customer, accounts, servicing) each with SME-declared expectations: relevant recipes/intents, expected parameter implications (windows), expected cross-catalog reach. Engineering ships the schema + loader + a seed set marked DRAFT; SME review of corpus entries is an operator act recorded via `governed_plan_review_event`.

## Task S1C-2: the wave-1 report
`governed_planning_report.py` + a confirmer-gated `GET /governance/cross-catalog-report`: resolution rate by domain (pack mapping), origin coverage, hop distribution, authority-floor pass rate, missing/stale bridge rate, refusal taxonomy, fan-out-risk distribution (segment cardinalities in evidence), parameter-divergence rate, per-mode volumes, worker latency/query percentiles. An explicit "not computable in Stage 1" section enumerating every wave-2 metric and the evidence it lacks — the report never fakes a number. Tests per metric over seeded observations; the not-computable section pinned exhaustive.

## Task S1C-3: the shadow V2 parameter chooser
The typed chooser over `ParameterSpecV2` menus (closed selection, audited, content-addressed — the `param_choice` discipline, new task key) runs in the WORKER as evaluation-only: its choices are recorded on observations (`param_divergence` gains the chooser's pick beside the token-match), never served. This resolves the accuracy-measurement deadlock: chooser accuracy against the corpus is computable in Stage 1C; the same chooser is PROMOTED to serving in Stage 2 with its accuracy number already known. Provider cost bounded: one dispatch per (menu, hypothesis, prompt version) content address — replays free.

## Task S1C-4: acceptance thresholds
A one-page thresholds doc the SME signs per corpus review: minimum resolution rate per domain, maximum stale-bridge rate, chooser accuracy floor, worker p95, query ceilings. These numbers — not vibes — are Stage-2 entry evidence.

## Task S1C-5: the Stage-1 runbook
`docs/architecture/cross-catalog-stage1-runbook.md`: migrations 1120+1121 backend-first; the telemetry WORKER is a deployment (what runs it, how it is watched); enabling `FEATUREGEN_INTENT_SHADOW_TELEMETRY` is the explicit operator go that starts evidence accrual (without it, Stage-2 entry evidence starves); mode semantics (production evidence is telemetry-mode); the report/threshold queries; every flag flip and deploy is an explicit user go.

### Stage 1C gate
Report green over seeded + real telemetry; corpus loader green; chooser shadow rows present; thresholds doc exists and names its signatory; the runbook exists.

---

# STAGE 2 — GATED CHARTER (self-contained)

**Entry criteria:** (1) Stages 1A–1C merged and green on the T0 baseline. (2) The selection→code coordinator resolved: the run-spine program's corrections landed, or the user directs a minimal in-program coordinator — selection MUST run through immutable run/build-set pins, never shadow capture (V2). (3) Physical execution-source binding exists or is co-scheduled (physical-table-config program) (V6). (4) The planner latency benchmark accepted (worker-side p95 + query ceilings from 1C-4). (5) The wave-1 report meets 1C-4's thresholds, SME-reviewed.

**Work items** (each becomes a Stage-1-rigor plan at entry, re-verified against the then-current baseline):

- **S2-P1 — LLM promotion journey.** Conceptual option selected for development → immutable authoring-subject revision → formula draft authored & validated → READY formula revision → feature-selection revision binds that exact formula → build set pins selection + formula + governed plan; a build set is never mutated afterward. A NEW activation action (registered in the closed ladder + `REASON_FAMILIES`) or a distinct promotion-request object — never a bypass of `author_formula`'s blockers. Spend authorization REUSES the existing cost-confirmation mechanism (the formula-quality journey's `[Run evaluation]` pattern) — no second mechanism. Review controls per D9: promotion applies `required_reviewer_roles` to the authored subject — `model_risk` included for near-label/outcome leakage — and distinguishes the certified generation METHOD from per-formula human review so the workflow scales.
- **S2-P2 — Selection→code through the canonical coordinator.** Selected option decision → immutable run/build-set member pinning (option_id, considered_revision_id, formula revision, governed_variant_id + component hashes, plan-envelope hash) → generation request → worker restore/admit/compile/render/seal, **with worker crash recovery and reconciliation semantics specified** (lease/reclaim/idempotent restore). Integrates the parent gating program's ONE decision service (`evaluate_action`) — the six-action model — rather than inventing a parallel gate; preview generation is permitted without gold certification while production publication stays hard-blocked (the standing gold-gates ruling).
- **S2-P3 — Serving wire-up.** The governed lens on the scoped route (route-threaded `v2_eligible_ids`, lens `"governed"`, the two lens-pin tests updated deliberately); `GOVERNED_SERVING_POLICY_VERSION` joins `current_version_vector()` in the SAME commit (the stale-approval test: an approval recorded under the old vector no longer authorizes); served-twin facts rule: the feature JSON carries `governed_variant_id` additively and the decision-row persist prefers it, so an engine option and a governed option for one recipe never share a facts row while the PERSISTED `source_definition_id` stays canonical (V11); the promoted parameter chooser (accuracy known from 1C-3); the additive guardrail test (engine cards byte-unchanged; governed cards strictly additional); real-registry route tests, no registry monkeypatching; the positive draft/confirm proof (COMMITTED with the server-derived governed join path — a named refusal fails this test; refusal cases are separate tests); serve-time-optimism UX copy ("final checks run at draft").
- **S2-P4 — Exact plan identity via `PlanEnvelopeV2`.** A versioned sealed plan contract carrying per-segment direction, cardinality, relationship id+revision, the temporal-declaration hash (now catalog-qualified per S1A-4), and complete dependency evidence — the segment data exists (V16/§V's `BindingPathSegmentV1`); V1's flattened strings drop it. Canonicalization, version registry, persistence and migration are in scope; compilation and generation admission compare the FULL envelope, and the fan-out fixture (two same-read-set join shapes; one must refuse) is the acceptance test. Read-set equality alone never protects banking aggregates from double counting.
- **S2-P5 — One dataset-key dialect.** Renderer raw keys and Kedro catalog names adopt the resolver's `catalog::schema.table` dialect (V4), tested through `render_project` AND `_hop_datasets` in one test; `RENDERER_VERSION` bump (identity-impacting: engine capability re-recorded on deploy); two same-named tables in two catalogs render and resolve as two datasets.
- **S2-P6 — Execution-source compatibility gate.** Before generation: governed physical source/connection binding per participating catalog; verify engine/connector compatibility, credentials/service identity, read authorization, data-residency policy, PIT snapshot compatibility, availability/freshness, permitted data movement, currency/reference-data dependencies. Same-substrate reachability alone proves nothing; federated/staged execution is out of scope and refused by name.
- **S2-P7 — Ranking + honest labels.** Governed options rank in the shared framework extended with bridge-evidence tier and join-cardinality/fan-out risk; wave-2 metrics gate broad enablement; UI enforces the four-way validation vocabulary; predictive metrics only with an approved label + population.
- **S2-P8 — The two public-API journeys + banking adversarial fixtures.** Recipe-origin and LLM-origin journeys through the PUBLIC APIs and real workers, ending in a rendered two-catalog Kedro project, L0, and Fixture validation with grain/value assertions. Adversarial set: joint-account M:N ownership, as-of ownership change, late-posted transactions, reversals/chargebacks, debit/credit signs, multi-currency with as-of FX, duplicate transaction ids, closed/dormant accounts, missing bridge coverage, post-cutoff rows — each with a pinned expected refusal or value.
- **S2-P9 — Cutover hygiene.** A clean isolated-DB full-suite run (contamination failures quantified by evidence), and a rehearsed migration/deployment cutover per the runbook before any activation go.

## Deferred charters
Full weighted quality framework (eleven axes — product decision); per-variant serving beyond primary+chooser; production publication capability (`probe_publication_capability` absent by design); cluster sandbox execution/profiling until the execution lane exists; out-of-scope-bridge revelation ruling; `GRAPH_VERSION` content fingerprint; the stale "NOT APPLIED" migration-comment convention; the 211 V2-only recipes' ranking disadvantage.

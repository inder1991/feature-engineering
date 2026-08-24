# Cross-Catalog Serving — "First Served Card" Implementation Plan (REV 6, self-contained)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Rev 6 is SELF-CONTAINED — it replaces Revs 1-5 entirely; nothing binding lives only in git history. It absorbs five adversarial review rounds (2026-08-24); every falsifiable finding was verified against origin/main `463498ed` before adoption. Parent spec: `docs/superpowers/plans/2026-08-23-cross-catalog-program-rev5.md` §Stage-2, amended by Phase P below. Execute with Stage-1 rigor: T0 verification first, fresh implementer per task, review-gated, ledgered rulings, final whole-branch review. NO merge/push/deploy/flag-flip without explicit user go.

**Goal:** A user's hypothesis surfaces cross-catalog feature cards built from LOGICAL plans over AI-proposed-or-better links. Every action a card offers is the verdict of the platform's ONE canonical decision service fed by ONE server-owned fact loader. Whatever generates carries the EXACT pinned plan the user confirmed. Production actions render "unavailable — production execution governance is not released."

## Owner's serving policy (2026-08-24, binding)

| Action | AI-proposed link |
|---|---|
| Show cross-catalog card | Allow |
| Rank/recommend | Allow |
| Author formula | Allow (per disposition matrix) |
| Generate preview code | Allow when an exact join is encodable; else provisional with runtime gates |
| Execute in sandbox | BLOCKED this scope (`EXECUTION_SOURCE_COMPATIBILITY_UNPROVEN`) — CI fixture validation only |
| Publish to production | Unavailable — governance not released |

Human review of a link changes DISPLAY provenance only ("AI-proposed — not yet human-reviewed" → "Human-confirmed"); it never unlocks engineering workflow, changes identity, or changes code.

## The capability ladder (projection, never authority)

`CARD_AVAILABLE` (discovery: logical plan resolves over `active_bridges`) → `FORMULA_AUTHORABLE` → `PREVIEW_PROVISIONAL` → `PREVIEW_EXECUTABLE` → `PRODUCTION_ELIGIBLE`. Render mode: `EXACT | PROVISIONAL | UNAVAILABLE`. From `AUTHOR_FORMULA` onward every verdict comes from the six-action service; `CapabilityAssessmentV1` only formats. Execution-truth states on cards: *Preview rendered* / *Fixture validated* (artifact-bound only — see B6) / *Sandbox executable* (never claimable this scope).

## Rulings (all ledger-bound; each task tests against the ones it touches)

- **R1 — one authority, one fact loader.** Six-action service (`materialize/action_decision.py`: `ActionRequestV1` → ask/decide over `ACTION_DISPOSITIONS`) is sole authority from `AUTHOR_FORMULA` on. Legacy ladder keeps `save_idea`+`create_contract`; legacy `author_formula`/`request_materialization`/`execute_materialization` become adapters or retire (disagreement inventory first; proven row: `RECIPE_REVIEW_NOT_CURRENT` legacy-blocks vs canonical-warns — canonical wins). AND (round-5 P0-1, verified: the plan route assembles `member_blockers` into `ask` while `authorize_and_decide_generation` passes bare `member_names` — same fold, different facts): ONE server-owned `facts_for_action(conn, action, resource_identity) -> ActionFactsV1 {member_names, member_blockers, member_warnings, evidence_pins}` consumed by ask, decide, worker recheck, AND capability projection. No caller assembles facts.
- **R2 — execution context never touches logical identity.** Logical considered-option identity EXCLUDES environment/tier. Execution context lives only in `PhysicalExecutionPlanV1`. Formula reuse keys on the logical digest. New environment → new physical plan revision, never a new feature, never a re-bought formula.
- **R3 — physical adoption is an append-only, user-confirmed revision chain** (round-5 P0-3, verified: `feature_selection_revision` is append-only with no supersession — a second confirmation is unrepresentable today). New table `selection_physical_plan_adoption_revision` {adoption_revision_id PK, selection_revision_id FK, physical_plan_revision_id FK, confirmed_by, confirmed_at, supersedes_adoption_revision_id NULL FK self, content_hash}. A selection accumulates historical adoptions; every build pins ONE exact adoption revision; nothing resolves "latest" implicitly; no worker auto-attaches a newly current realization.
- **R4 — preview generates; sandbox does not execute.** `GENERATE_PREVIEW` allowed per matrix. `EXECUTE_SANDBOX` blocked for cross-catalog artifacts with `EXECUTION_SOURCE_COMPATIBILITY_UNPROVEN` until a minimum execution-source gate (connector, credentials/identity, residency, data movement, PIT, reference/FX availability) lands. Generated code runs ONLY in isolated CI fixture validation.
- **R5 — one planning owner; the worker never replans live work.** Live serving's synchronous plan is authoritative. Persistence design (round-5 chose): the serving transaction enqueues the EXACT SERIALIZED RESULT (plans, facts, verdicts — not inputs); the telemetry worker PERSISTS it verbatim, running no planner. Shadow/counterfactual planning is a separately-identified experiment lane excluded from live denominators. Demand records once per planning occurrence via `demand_identity_hash` dedupe.
- **R6 — reuse platform reason codes.** Governed cardinality reason = existing `DIRECTIONAL_CARDINALITY_UNPROVEN` (verified `_row(W,W,W,W,B,B)`). `CARDINALITY_EVIDENCE_REQUIRED` is a demand-queue category only. `REALIZATION_ATTACHMENT_DEFECT` is an ops alert, never feature-attributed demand.
- **R7 — promotable class = "LLM deterministic intent awaiting formula", exactly** (round-5 P0-4, verified: `feature_planning_contracts.py:248-252` deliberately demotes a deterministic intent to `conceptual_pattern` solely because no formula exists yet). Promotion re-projects: original `FeatureIntentV1` bytes + logical-plan digest + exact operation class + bound operands + target/leakage re-evaluation → immutable `authoring_subject_revision` → formula draft → bindings. `request_draft_for_candidate` consumes the PROMOTED subject (never re-reading the demoted option). Genuine conceptual patterns STAY blocked (`CONCEPTUAL_PATTERN_NOT_AUTHORABLE`).
- **R8 — this plan SUPERSEDES the production-readiness remediation's lineage tasks** (its 1122 target/selection/formula lineage, 1124 frozen authoring plan, and its fact-loader requirement). That plan is NO-GO with pending corrections; building two competing lineage models is worse than absorbing. Phase P records the supersession in BOTH documents. Its remaining (non-superseded) tasks keep their reservations; our migrations use 1130+.

## Identity payloads (complete — the executable spec)

```
LogicalFeaturePlanV2:
  canonical_definition_id, definition_origin, operation_class,
  operand_bindings (ordered: operand_ref, role, concept, catalog-qualified column),
  output_grain: ORDERED tuple of grain key refs,
  window: {parameter_name, selected_value, parameter_choice_revision_id},   # B7's chooser
  logical_bridge_path (ordered: link semantic revision, from_entity, to_entity,
                       full ordered endpoint tuples, traversal direction),
  formula_policies (status/currency policy refs), planning_request_hash
  → logical_digest = sha256(canonical serialization)

PhysicalExecutionPlanV1:
  logical_digest (FK-by-hash), execution_context_revision_id (bound to existing
  environment_id/ExecutionTier vocabularies), source_binding_revisions,
  per-segment: {realization_revision, exact ordered column pairs, predicates,
                directional cardinality, realization content+dependency hashes},
  join_validation_policy_revision_id                                        # B4's contract
  → physical_digest

RenderProfileV1: engine, compiler_version, renderer_version → render_digest
Composed generation digest = sha256(logical | physical | render)

JoinValidationPolicyRevisionV1:
  null_key_behavior, unmatched_key_behavior, minimum_coverage,
  maximum_matches_per_left_row, declared_by, policy_revision_id, content_hash
  # ABSENT → provisional journeys REFUSE (JOIN_*_POLICY_MISSING); no invented defaults;
  # a passing runtime observation NEVER auto-promotes cardinality to proven.
```

## Identity & staleness law (verbatim, binding)

| Change | Expected result |
|---|---|
| Proposed → human-confirmed | No physical-plan or code rekey |
| Strength/ranking changes | No code rekey |
| Candidate withdrawn/rejected | Refuse |
| Realization revoked | Refuse |
| New realization supersedes old | Refuse old pinned plan; NEVER substitute latest (R3 adoption instead) |
| Evidence display text changes | No physical rekey |
| Join column/predicate/cardinality changes | New plan identity |
| Environment/tier changes | New PhysicalExecutionPlanV1 revision; logical identity unchanged (R2) |

## Disposition matrix for new codes (policy, pinned here — round-5 Important-7; production columns always Block/Block until governance releases)

| Reason | AUTHOR_FORMULA | GENERATE_PREVIEW | EXECUTE/PUBLISH_SANDBOX |
|---|---|---|---|
| DIRECTIONAL_REALIZATION_MISSING | Warn | Block | Block |
| DIRECTIONAL_MAPPING_INCOMPLETE | Warn | Block | Block |
| EXECUTION_CONTEXT_MISSING | Warn | Block | Block |
| EXECUTION_SOURCE_COMPATIBILITY_UNPROVEN | Warn | Warn | Block |
| OPERAND_ROLE_UNRESOLVED | Block | Block | Block |
| JOIN_NULL/COVERAGE/MAX_MATCH_POLICY_MISSING | Warn | Block | Block |
| ALLOCATION_POLICY_REQUIRED | Block (final-grain; source-grain intermediate may author) | Block | Block |

All registered via the three-part commit (`REASON_FAMILIES` + `ACTION_DISPOSITIONS` + honest-vocabulary bucket); CI exhaustiveness stays green.

## §V verified facts (cumulative, five rounds; T0 re-verifies ALL at the then-current origin/main)

- V1. `active_bridges` (`bridge_projection.py:141`) serves proposed+confirmed alike (owner doctrine in its docstring); `ActiveBridgeV1` = {fact_key, entity_id, left/right catalog+object_ref, status-as-ANNOTATION, strength} ONLY; `members[0]` collapse at :165/:168 loses composite keys.
- V2. G3 (`physical_cardinality_unavailable`) is minted during contract compilation; the governed lens consumes compiled results (`governed_lens.py:580,648`) — an unrealized proposed link yields NO card today. G2 (MEASURE-defaulting in `need_metadata._derive_one`; 82-operand divergence worklist in `governed_lens.py:55-63`) unmasks the moment cardinality attaches (`compile_aggregation`'s `card is None` short-circuit, `declarations.py:716`).
- V3. `attach_executable_bridge_realizations` pure (`assembly.py:404`); `executable_bridge_realizations` excludes {ONE_TO_MANY, MANY_TO_MANY} (`bridge_store.py:125`) and hardcodes PRODUCTION tier (:900) while `revalidate_bridge_realization` (:776; `execution_tier` DEFAULTS to PRODUCTION :782) refuses tier mismatch (:795-800) → sandbox-scoped realization rows are REQUIRED or the executable rung is empty.
- V4. Serving unreachable: entity-only raise `routes/contract.py:776`; engine branch `gate1.py:1161` vs `elif is_live:` :1308, mutually exclusive.
- V5. Generation lane: `selection_formula_binding.py:32` pins selection+formula+`planning_request_hash`+`binding_plan_hash` (:41-42) — new digests must derive-from/check-against these; `restore_formula_v3.py:91` restores no plan; `ResolvedFeatureInputV2` (`admission_v2.py:94-100`) FORBIDS an optional plan field by docstring → mandatory wrapper; current-state join re-resolution in `materialize/joins.py:920`; `feature_selection_revision` (1072:83) append-only, no supersession (→ R3); formula drafts precede selection.
- V6. Renderer raw keys `{schema}.{table}` (`render/project.py:348-350`) vs wiring `catalog::schema.table` (`wiring.py:199`); same-name two-catalog collision real; spine/gate manifests/source-binding validation also unqualified.
- V7. 1120's demand verdict CHECK closed + file immutable → new demand types = new migration. 1121 outbox stores INPUTS and replans (its design) → R5 changes the payload to serialized RESULTS for the live lane.
- V8. Decision authorities: `activation_policy.py` ladder {save_idea, create_contract, author_formula, request_materialization, execute_materialization} with `RECIPE_REVIEW_NOT_CURRENT` blocking at :152; canonical `ACTION_DISPOSITIONS` has it `_row(W,W,W,W,B,B)` (:136) — LIVE disagreement. `DIRECTIONAL_CARDINALITY_UNPROVEN` exists :128 with the correct row. Caller-assembled facts: `code_generation_jobs.py:272` (with blockers) vs `generation_lane.py:282` (bare names) → R1's fact loader.
- V9. Formula reality: every AML recipe `FORMULA_BLOCKED`; `posted_debit_amount` is the ONLY reviewed V2 expectation (`recipe_formula_expectations_v2.py`, registry growth = operator act); the deterministic demotion at `feature_planning_contracts.py:248-252` (→ R7); blueprint `_AGGREGATION_BY_RESULT_CLASS` (`recipe_formula_blueprint_derivation.py:99`): "count"→COUNT_ROWS consumes NO operand — dimension-bearing "per rail" recipes would silently compute TOTALS (→ journey recipes below).
- V10. Deployment: `FEATUREGEN_MATERIALIZE_INVENTORY` commented (`deploy/kind/k8s/20-backend.yaml:197`); flag `FEATUREGEN_INTENT_LIVE_CROSS_CATALOG` + signed-gate startup check (`live_activation.py:56,242`); `PLAN_CONTRACT_VERSION` already in `current_version_vector()` (:62) → bump, and add `GOVERNED_SERVING_POLICY_VERSION`.
- V11. Chooser is evaluation-only (`governed_telemetry_worker.py:164` `param_chooser=None`, injected; content-addressed via the 1039 `structured_result` store) — B7 promotes it. Run-spine substrate (1100-1119, spend authorization, action decisions, build sets) IS in baseline. Migration registry: remediation plan provisionally owns 1121-1129 (its 1121 dead — ours is live); 1117 owner unknown; OUR block = 1130+ after T0 registry reconciliation.

**T0 (mandatory, before any dispatch):** re-verify V1-V11 + every review citation at the then-current origin/main; the R1 adapter-surface inventory (all legacy-rung callers); the canonical service's exact `ActionV1` members + ask/decide/recheck entry points; the candidate-revision identity material (display-evidence split); 1117's owner; write the migration-registry reconciliation + R8 supersession records; ledger deltas; amend tasks before dispatch.

---

# PHASE P — Parent amendment + supersession (first commit, own task)
Amend `2026-08-23-cross-catalog-program-rev5.md`: (1) Stage-2 gate split — implementation begins now (owner ruling 2026-08-24); ACTIVATION (targeted included) stays behind signed SME thresholds + accepted wave-1 evidence + latency benchmarks + inventory health + the signed gate artifact. (2) S2-P4 redefined: PlanEnvelopeV2 = the COMPOSED carrier of logical/physical/render identities (this plan's model). (3) S2-P6 split: render compatibility (in scope here) vs execution-source compatibility (blocks EXECUTE_SANDBOX, not GENERATE_PREVIEW — R4). (4) R8 supersession recorded here AND in the remediation plan.

# PHASE A — Discovery tells the truth

**A0 — G2 clearance as a SERVING gate + journeys pinned.** Files: `overlay/upload/need_metadata.py`, `contract/governed_lens.py`, the divergence worklist test. Every potentially-served cross-catalog option: operand roles resolved correctly OR `OPERAND_ROLE_UNRESOLVED` (Block/Block/Block — card shows only under "Discoveries requiring setup"). **Journey recipes (round-5 corrected):** deterministic journeys use `posted_debit_amount` (the ONE reviewed expectation; measure-consuming SUM; genuinely cross-catalog via the customer bridge) + ONE scalar count recipe whose row selection is STRUCTURALLY declared in eligibility (T0 names it from the registry — candidate class: salary/eligible-posted count recipes; NO dimension-bearing "per-X" recipe until "per-X" is a selected parameter/row filter or vector features exist). LLM-lane journeys per R7. Fixture preconditions per journey: recipe review rows, status/currency policy realizations (1075 substrate), party-role-resolved counterparty columns where used.

**A1 — authority consolidation + the fact loader.** Files: `materialize/action_decision.py` (or sibling) gains `facts_for_action`; `activation_policy.py` adapters; `code_generation_jobs.py` + `generation_lane.py` converge on the loader; three-part registration of the matrix above; rung↔action mapping; capability projection module. Tests: the disagreement inventory becomes regression tests (each row: legacy answer vs canonical answer → canonical served); property test — NO card action ever contradicts ask/decide fed by `facts_for_action`.

**A2 — projection extension.** Full ordered endpoint tuples, link semantic revision, evidence-dependency split (load-bearing / currentness / display), annotations; no `members[0]`, no global direction, no raw candidate-ledger reader. Display-evidence changes provably don't rekey (staleness-matrix test).

**A3 — server-owned execution context.** Bound to existing `environment_id`/`ExecutionTier`; immutable revision; lives ONLY in `PhysicalExecutionPlanV1` (R2); absence = `EXECUTION_CONTEXT_MISSING` per matrix.

**A4 — `BridgeRealizationSnapshotV1`.** Extends `CompileBudget`'s existing cap/deadline split; constant-query batch over the considered set (executable + provisional-relevant rows); truncation cause persisted + disclosed.

**A5 — the logical plan resolves without physical evidence.** Planner change (DECLARED identity impact; PLANNER_VERSION bump; Stage-1 literal pins regenerated deliberately): logical resolution with physical cardinality deferred produces a real `LogicalFeaturePlanV2` for card/formula rungs; `grain_refs` for the draft worker derive from it; realization attachment (before compile, batched via A4) upgrades to physical rungs; later attachment goes through R3's adoption chain only.

**A6 — demand vocabulary + one writer.** New migration (1130+ block): queue categories incl. `CARDINALITY_EVIDENCE_REQUIRED`; R5's once-per-occurrence law with `demand_identity_hash`; satisfaction projection over append-only history; telemetry reads provisional-relevant realizations so unknown-cardinality stops masquerading as no-realization.

### Phase A gate: property test vs canonical service through `facts_for_action`; G2 serving gate live; composite-key round-trip; staleness-matrix tests green; suites green; identity change reviewed.

# PHASE B — The pinned plan

**B1 — the three-layer identity model.** Migrations (1130+): relational persistence + FKs for the payloads above; canonicalization; composed digest; same-read-set/different-shape → different digests; ordered grain tuple surviving card→formula→build→IR→render.

**B2 — TOTAL binding chain, DDL-enforced.** Tables: `considered_option_plan_binding` {considered_revision_id, option_id, plan_envelope_id(logical), plan_digest}; `formula_draft_plan_binding` (NOT `formula_draft_authoring_plan` — exists, 1104, different job) {formula_draft_id, considered_revision_id, option_id, plan_envelope_id}; `selection_formula_plan_binding` {selection_revision_id, formula_draft_id, plan_envelope_id}; R3's `selection_physical_plan_adoption_revision`; build-set member references the COMBINED logical+physical+render binding. Totality mechanism SPECIFIED: parent-carried binding id + composite FK where insert order allows; DEFERRED CONSTRAINT TRIGGER at commit where it doesn't. Five refusing tests: option-without-logical-binding; draft-without-binding; selection-binding-across-plans; previewable-selection-without-confirmed-adoption; build-member-without-combined-binding. Legacy rows = explicitly pre-plan, refused for cross-catalog generation. Digests derive-from/check-against existing `binding_plan_hash`/`planning_request_hash`.

**B3 — authoring consumes the pinned logical plan.** The logical digest enters: authoring request content, provider input audit, authoring-decision evidence pins, formula validation. Then `PinnedResolvedFeatureInputV3` (mandatory wrapper) through restore → admission → compilation: joins derived FROM pinned realization revisions (sole authority — R5); revalidation with the pinned context; refusal on revocation/supersession; fresh planner runs diagnostic-only.

**B4 — the provisional lane.** Typed assessment {EXECUTABLE, PROVISIONAL_UNKNOWN_CARDINALITY, REFUSED}; separate provisional reader (production reader untouched); provisional join IR step (exact pairs + predicates); `compile_expression` wiring; renderer support; the PRE-AGGREGATION runtime gate = target-side join-tuple uniqueness under exact predicates + the DECLARED `JoinValidationPolicyRevisionV1` (absent → refuse per matrix); failure stops before any aggregate with: no output written, no partial aggregate, attempt terminalized (not retried), queue item completed, named failure on the run dashboard, immutable observation against the exact realization + snapshot.

**B5 — LLM promotion machinery (R7).** Immutable `authoring_subject_revision`; closed promotable class = deterministic-intent-awaiting-formula ONLY; server-side promotion validator; promotion request API; exact computation + operand binding from the original intent bytes + logical digest; target/leakage re-evaluation; canonical decision-service integration; spend authorization via the existing cost-confirmation mechanism; provider input + audit identity; UI transition "idea → formula ready". `request_draft_for_candidate` consumes the promoted subject. Conceptual patterns outside the class stay blocked.

**B6 — artifact-bound fixture evidence.** "Fixture validated" is claimable ONLY from an immutable record {sealed artifact id, logical/physical/render digests, fixture-set version, result}; otherwise cards say "Renderer tested on platform fixtures" (platform-level claim). Migration in the 1130+ block; the validation runner writes it.

**B7 — the parameter chooser promoted to serving.** From evaluation-only (V11) to the serving path: bounded menu (`ParameterSpecV2.allowed_values`), hypothesis-conditioned selection (the existing audited content-addressed `param_choice` machinery; deterministic token-rule fallback), cost control (content-address replay; per-request cap), immutable `parameter_choice_revision_id` INTO `LogicalFeaturePlanV2`, refusal/fallback behavior (chooser unavailable → recipe primary + the choice revision records "default_fallback"), lookback-vs-target-horizon tests ("90-day increase" hypothesis must not get a 30-day feature — pinned).

### Phase B gate: totality refusals all fire; authoring-without-digest refused; policy-less provisional refused; promotion end-to-end on FakeLLM; chooser revision in every logical digest; fixture-evidence record round-trips; suites green.

# PHASE C — Rendering + serving (wired last)

**C1 — one dataset-key dialect.** `catalog::schema.table` through raw keys, dataset names, node inputs, catalog YAML, spine validation, gate manifests, source-binding validation; RENDERER_VERSION bump (declared, in `RenderProfileV1`); tests: two catalogs same `schema.table` distinct through the complete rendered project; NEGATIVE: wrong-catalog spine REFUSES.

**C2 — serving under one verdict.** `ResolvedCrossCatalogActivationV1` {flag state, signed approval identity, artifact hash+expiry, cohort approval, version vector, reasons} resolved once, pinned into the considered option, revalidated same-facts at draft/confirm; branch restructure (engine + governed lanes, additive merge, anchor rule: every option includes the user's catalog); serving's OWN ceiling (cap, deadline, ONE snapshot read, disclosed truncation); R5's persistence (serialized-result enqueue, worker persists verbatim, no replan); first-serve ranking weights: hypothesis relevance, bridge tier, fan-out risk, semantic completeness, formula readiness, data availability; byte-identity when inactive (pinned); `GOVERNED_SERVING_POLICY_VERSION` + `PLAN_CONTRACT_VERSION` bump enter `current_version_vector()` in THIS commit (stale-approval test).

**C3 — twin identity end to end.** Every intermediate map + persistence keyed `option_id`/`governed_variant_id`; canonical `source_definition_id` stays canonical in its column; engine+governed twin survives every stage.

**C4 — the frontend card experience.** Four-section home: **Ready to generate / Formula available / Discoveries requiring setup / Not suitable** (the platform's standing no-"blocked" vocabulary); AI-proposed-no-realization cards under "Discoveries requiring setup" with named blockers + remedies; provenance labels; provisional warning; the three execution-truth states rendered distinctly; production actions = governance-not-released copy; ALL action states backend-driven (zero TypeScript policy); accessibility + responsive tests; `api.ts` contract fields (rung, per-rung blockers, provenance, render mode, catalogs + join path).

### Phase C gate: proposed-link card served under the four-section home; property test vs canonical service; byte-identity inactive; anchor + ceiling pinned; suites green.

# PHASE D — Journeys (public APIs; CI fixture validation only — R4; every journey names its recipe + preconditions from A0)

1. **No realization** (`posted_debit_amount`, proposed link): card + formula; preview `DIRECTIONAL_REALIZATION_MISSING`; demand once.
2. **Unknown cardinality** (count recipe, mapped link, no cardinality proof): provisional preview; runtime gate PASS + FAIL (full atomicity assertions); governed reason `DIRECTIONAL_CARDINALITY_UNPROVEN`.
3. **Exact N:1** (`posted_debit_amount`, realization + adoption confirmed): full preview; IR asserts ≥1 cross-catalog join step, two catalog-qualified inputs, the bridge-gate node, the pinned ORDERED grain tuple, ONE composed digest across card→selection→build→IR→render.
4. **Known M:N**: card; source-grain intermediate authorable; final-grain `ALLOCATION_POLICY_REQUIRED`; preview refused; demand recorded.
5. **LLM-fallback authoring journey** (a `FORMULA_BLOCKED` recipe; spend authorization EXPLICIT in the journey; FakeLLM; method provenance) → preview → fixture EXECUTION over the COMPLETE banking adversarial corpus: joint-account M:N, as-of ownership change, late posting, reversals/chargebacks, debit/credit signs, multi-currency with as-of FX, duplicate transaction ids, closed/dormant accounts, missing bridge coverage, post-cutoff rows, lookback-vs-horizon — each an exact expected value or named refusal.
6. **Genuine LLM-origin feature** through B5: deterministic intent awaiting formula → promotion → authored+validated formula → selection → bindings → build set → cross-catalog preview.

# PHASE E — Operator rail (every step explicit user go, parallel)
E1: migrations backend-first; telemetry flip + worker scheduling; `FEATUREGEN_MATERIALIZE_INVENTORY` + multi-catalog mappings + SANDBOX-scoped realization seeding (V3 — else the executable rung is empty), health-checked before live Phase-D runs. E2: satisfaction projection live; report denominators per R5 (shadow excluded). E3: targeted-cohort activation ONLY after Phase P's amended activation gate passes; SME thresholds + wave-1 gate BROAD activation.

# Not in scope (chartered, refused by name where reachable): M:N allocation policies (J4 records demand); dimension/reference-enrichment joins (incl. `high_risk_corridor_exposure` journeys); full S2-P6 beyond R4's minimum; public sandbox execution; federated execution; propose-bridge operator surface; LLM promotion beyond R7's one class; per-dimension ("per rail") features until parameterized row filters or vector features exist.

# Execution
SDD, Stage-1 protocol. T0 first (registry reconciliation + adapter inventory included). Ledger at `.superpowers/sdd/<plan-basename>/progress.md`. Final whole-branch review on the most capable model. NO merge/push/deploy/flag-flip without explicit user go.

# Cross-Catalog Serving — "First Served Card" Implementation Plan (REV 5)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Rev 5 supersedes Rev 4 after the fourth review round (2026-08-24; 8 P0s — the falsifiable ones verified at `463498ed`, including the LIVE disagreement between the two decision authorities and the pre-existing `DIRECTIONAL_CARDINALITY_UNPROVEN` disposition). Parent spec: `2026-08-23-cross-catalog-program-rev5.md` §Stage-2 (amended by Phase P). Standing corrections from rounds 1-3 carry forward unchanged: AI-proposed links propose and explain; physical evidence controls execution; the identity/staleness matrix binds all tasks.

**Goal:** unchanged — cards from logical plans; actions from the platform's ONE canonical decision service; generation carries the exact pinned plan; production shows "unavailable — governance not released".

## RULING R1 — one action authority (P0-1, verified disagreement)
From `AUTHOR_FORMULA` onward, the SIX-ACTION service (`materialize/action_decision.py`: `ActionRequestV1` → ask/decide over `ACTION_DISPOSITIONS`) is the SOLE authority. The legacy ladder keeps ONLY `save_idea` + `create_contract` (discovery/contract decisions). Legacy `author_formula` / `request_materialization` / `execute_materialization` become thin adapters over the canonical service (or retire where dead) — ONE task owns this adapter/retirement with a disagreement inventory as its first deliverable (`RECIPE_REVIEW_NOT_CURRENT` is the proven first row: legacy blocks, canonical warns until production — the canonical row wins). `CapabilityAssessmentV1` projects the CANONICAL results; every property test targets the canonical service; no card action may derive from `decide_all_actions` beyond the two retained rungs.

## RULING R2 — execution context never touches logical identity (P0-2, internal contradiction fixed)
Logical considered-option identity EXCLUDES environment and execution tier. Execution context lives ONLY in `PhysicalExecutionPlanV1`. Formula reuse keys on the LOGICAL plan digest; a new environment mints a new physical-plan revision, never a new feature and never a re-bought formula. A previewable selection additionally pins its CONFIRMED physical plan. (Rev 4's A3 "in considered-option identity" clause is dead.)

## RULING R3 — physical capability refresh is a user-confirmed transition (P0-3)
When a realization appears AFTER a card/formula existed: old logical option + formula (reusable if the logical digest is unchanged) → "refresh execution capability" → NEW `PhysicalExecutionPlanV1` revision → the USER confirms the exact join + environment → new physical binding → build set. NO worker ever auto-attaches a newly-current realization to an existing option; "latest" is never selected implicitly.

## RULING R4 — no public sandbox execution under this scope (P0-5)
`GENERATE_PREVIEW` is allowed per the ladder. `EXECUTE_SANDBOX` for cross-catalog artifacts is BLOCKED with a new registered reason `EXECUTION_SOURCE_COMPATIBILITY_UNPROVEN` until a minimum S2-P6 compatibility gate (connector, credentials/service identity, residency, data movement, PIT, reference/FX availability) lands. Generated code executes ONLY in isolated CI fixture validation. The UI distinguishes three truthful states: *Preview rendered* / *Fixture validated* / *Sandbox executable*.

## RULING R5 — one planning owner per live request (P0-8)
Live serving's synchronous plan is authoritative: the same transaction records (or queues persistence of) THAT exact result; NO second planner execution is enqueued for the same run/request/variant. Shadow telemetry remains counterfactual and is EXCLUDED from live denominators (distinct mode + the exclusion rule, both). Demand records once per planning occurrence; repeated occurrences remain countable.

## RULING R6 — reuse the platform's reason codes (Important-1, verified)
The governed cardinality reason is the EXISTING `DIRECTIONAL_CARDINALITY_UNPROVEN` (`action_dispositions.py:128` — warn through sandbox, block production; "needs_data_check" bucket). `CARDINALITY_EVIDENCE_REQUIRED` survives only as a demand-QUEUE category, never an action blocker. `REALIZATION_ATTACHMENT_DEFECT` is a technical alert (ops), not a governance demand attributed to the feature. New codes actually registered (three-part commit): `DIRECTIONAL_REALIZATION_MISSING`, `DIRECTIONAL_MAPPING_INCOMPLETE`, `ALLOCATION_POLICY_REQUIRED`, `EXECUTION_CONTEXT_MISSING`, `EXECUTION_SOURCE_COMPATIBILITY_UNPROVEN`, `OPERAND_ROLE_UNRESOLVED`, `JOIN_NULL_POLICY_MISSING`, `JOIN_COVERAGE_POLICY_MISSING`, `MAX_MATCH_POLICY_MISSING`.

## Carried structure (rounds 1-4, unchanged unless amended below)
Three-layer identity (logical/physical/render; composed digests; renderer bump never re-buys authoring); capability-as-projection; symmetric link vs directional path-segment facts; evidence identity split; pinned plan = sole compilation authority; production readers untouched + separate provisional contracts; M:N final-grain formula blocked `ALLOCATION_POLICY_REQUIRED` (source-grain intermediate potentially authorable); anchor rule; byte-neutrality; append-only demand + satisfaction projection; migrations at a COORDINATED 1130+ block (registry reconciliation at T0; the parent-child plan's 1121-1129 includes a dead 1121); `PLAN_CONTRACT_VERSION` bumped / `GOVERNED_SERVING_POLICY_VERSION` added; parameter-chooser revision inside `LogicalFeaturePlanV2`; ordered `output_grain_key_refs`; every deploy/flag = explicit user go; git hygiene rules.

## §V additions for T0 (beyond Rev 4's)
Verify the adapter surface of R1 (all callers of legacy `author_formula`/`request_materialization`/`execute_materialization` decisions); the canonical service's exact `ActionV1` members and ask/decide entry points; `DIRECTIONAL_CARDINALITY_UNPROVEN`'s consumers; the promotable conceptual-pattern classes actually emitted today (`formula_strategy.py:134`'s input population).

---

# PHASE P — Parent amendment (unchanged: implementation now; ACTIVATION behind signed thresholds, wave-1, latency, inventory health, signed gate artifact).

# PHASE A — Discovery tells the truth

**A0 (amended — P0-7 + Important-4): G2 clearance is a SERVING gate, not a journey courtesy.** Every potentially-served cross-catalog option must either have operand roles correctly resolved or carry `OPERAND_ROLE_UNRESOLVED` — which excludes it from FORMULA_AUTHORABLE and above (card may still show under "Discoveries requiring setup" with that named blocker). The 82-operand divergence worklist becomes a serving-eligibility check, not documentation. **Journey recipes PINNED NOW:** J1-J4 use `rail_txn_count` + `rail_txn_amount` (plain transaction→customer measures, minimal G2 surface); J5 uses `rapid_movement_passthrough` + `fan_in_fan_out` GATED on this task clearing their counterparty/direction operand roles; `high_risk_corridor_exposure` is OUT of the journeys (its corridor-risk policy seam needs 1075 policy-realization seeding — chartered, revisit when the enrichment contract lands). Fixture preconditions named per journey: recipe review rows, party-role-resolved counterparty columns; NO journey needs dimension-enrichment semantics.

**A1 (amended — R1):** the authority consolidation task: adapter/retirement of the three legacy rungs + the disagreement inventory + capability projection from the canonical service + the three-part code registrations (R6's list) + rung↔action mapping.

**A2:** projection extension (full ordered endpoint tuples, semantic revision, evidence split, annotations; no `members[0]`, no global direction, no raw ledger reader).

**A3 (amended — R2):** server-owned execution context bound to existing `environment_id`/`ExecutionTier` vocabularies; immutable revision; lives in `PhysicalExecutionPlanV1` ONLY; absence = `EXECUTION_CONTEXT_MISSING` on preview rungs; logical identity untouched.

**A4:** `BridgeRealizationSnapshotV1` extending `CompileBudget`'s cap/deadline split; constant-query batch; truncation cause persisted + disclosed.

**A5:** the logical plan resolves with physical cardinality deferred (declared planner identity change, pins regenerated); `grain_refs` derive from it; realization attachment upgrades to physical rungs; refresh per R3.

**A6 (amended — R5/R6):** demand vocabulary migration (1130+ block): queue categories may include `CARDINALITY_EVIDENCE_REQUIRED`; one demand-writing law per R5 with `demand_identity_hash` dedupe; satisfaction projection; telemetry reads provisional-relevant realizations.

### Phase A gate: property test against the CANONICAL service (no card action contradicts ask/decide); G2 serving-eligibility check live; composite-key round-trip; suites green.

# PHASE B — The pinned plan

**B1:** three-layer identity model + composed digests + ordered grain tuple (unchanged).

**B2 (amended — P0-6): totality is DDL, not prose.** Strategy specified per table: the PARENT row carries the binding id with a composite FK where insert order allows (option decision rows reference their logical binding at insert), and a DEFERRED CONSTRAINT TRIGGER checks totality at transaction commit where circularity forbids parent-carried FKs. The five checks, each with a refusing test: cross-catalog considered option ↔ exactly one logical binding; cross-catalog formula draft ↔ exactly one `formula_draft_plan_binding`; selection ↔ exactly one combined formula/logical binding; previewable selection ↔ exactly one CONFIRMED physical binding; build-set member ↔ combined logical+physical+render binding. Legacy rows are explicitly pre-plan and refused for cross-catalog generation. New digests derive-from/check-against the existing `binding_plan_hash`/`planning_request_hash` pins.

**B3:** logical digest into authoring request/audit/evidence pins/validation; legacy drafts refused for cross-catalog builds; `PinnedResolvedFeatureInputV3` through restore→admission→compilation (unchanged).

**B4 (amended — Important-2): the runtime policy contract defined.** `JoinValidationPolicyRevisionV1` {null_key_behavior, unmatched_key_behavior, minimum_coverage, maximum_matches_per_left_row, declared_by, policy_revision_id, content_hash} — pinned inside `PhysicalExecutionPlanV1`; ABSENT policy → the provisional journey REFUSES with the R6-registered policy blockers; no invented defaults; a passing runtime observation never auto-promotes.

**B5 (new — P0-4): the LLM-promotion machinery** (implementation, before journey 6 can exist): immutable `authoring_subject_revision`; a CLOSED set of promotable conceptual-pattern classes (start with ONE class, named at T0 from the real `formula_strategy` input population); server-side promotion validator; promotion request API; exact computation + operand binding; target/leakage re-evaluation; canonical decision-service integration; spend authorization via the existing cost-confirmation mechanism; provider input + audit identity; the UI transition "idea → formula ready". The `CONCEPTUAL_PATTERN_NOT_AUTHORABLE` dead-end survives for every class OUTSIDE the closed promotable set.

### Phase B gate: totality tests refuse every orphan shape; authoring-without-digest refused; policy-less provisional refused; promotion machinery end-to-end on FakeLLM; suites green.

# PHASE C — Rendering + serving

**C1:** catalog-qualified dialect everywhere + wrong-catalog-spine NEGATIVE refusal + RENDERER_VERSION bump (unchanged).

**C2 (amended — R5):** serving under `ResolvedCrossCatalogActivationV1`; branch restructure; serving's own ceiling numbers; the R5 ownership law implemented (authoritative synchronous plan; no duplicate enqueue; shadow excluded from live denominators); first-serve ranking (hypothesis relevance, bridge tier, fan-out risk, semantic completeness, formula readiness, data availability).

**C3:** twin identity end to end (unchanged).

**C4 (amended — Important-5): UI home DECIDED:** the suggested-features surface gains four sections — **Ready to generate / Formula available / Discoveries requiring setup / Not suitable** (the platform's standing "no blocked" vocabulary: nobody-decided vs needs-a-data-check vs unsuitable). AI-proposed cross-catalog cards without realizations land under "Discoveries requiring setup" with named blockers + remedies. Backend-driven action states only; the three R4 execution-truth states (*Preview rendered / Fixture validated / Sandbox executable*) rendered distinctly; accessibility + responsive tests. T0 verifies wire mechanics, never re-opens this placement.

### Phase C gate: proposed-link card served under the four-section home; property test vs the canonical service; byte-identity when inactive; suites green.

# PHASE D — Six journeys (recipes pinned in A0; adversarial corpus COMPLETE)
J1 no-realization (card+formula; preview `DIRECTIONAL_REALIZATION_MISSING`); J2 unknown-cardinality provisional (runtime PASS + FAIL with full atomicity assertions; governed reason `DIRECTIONAL_CARDINALITY_UNPROVEN`); J3 exact N:1 full preview (IR-level cross-catalog assertions; one composed digest end to end); J4 known M:N (`ALLOCATION_POLICY_REQUIRED`; demand recorded); J5 recipe-origin LLM-authored value journey over the FULL parent adversarial set — joint-account M:N, as-of ownership change, late posting, reversals/chargebacks, debit/credit signs, multi-currency with as-of FX, duplicate transaction ids, closed/dormant accounts, missing bridge coverage, post-cutoff rows, lookback-vs-horizon — each with an exact expected value or named refusal; J6 genuine LLM-origin feature through B5's machinery → cross-catalog preview. All journeys: fixture validation only (R4); no public sandbox execution.

# PHASE E — Operator rail (unchanged: migrations backend-first; telemetry + worker; inventory + mappings + SANDBOX-scoped realization seeding; satisfaction projection; targeted-cohort activation ONLY after the amended parent gate passes; SME thresholds gate broad).

# Not in scope (chartered): M:N allocation policies; dimension/reference-enrichment joins (incl. `high_risk_corridor_exposure`'s journey use); full S2-P6 beyond R4's minimum when built; federated execution; propose-bridge surface; LLM promotion beyond B5's one pattern class.

# Execution
SDD, Stage-1 protocol; T0 first (incl. migration-registry reconciliation + R1 adapter-surface inventory); ledger at `.superpowers/sdd/<plan-basename>/progress.md`; final whole-branch review; NO merge/push/deploy/flag-flip without explicit user go.

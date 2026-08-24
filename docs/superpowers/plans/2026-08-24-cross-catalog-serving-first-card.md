# Cross-Catalog Serving — "First Served Card" Implementation Plan (REV 4)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Rev 4 supersedes Rev 3 after TWO convergent deep reviews (2026-08-24: the owner's 12-P0 review + this session's independent review — 3 Critical / 7 Important / 5 Minor). All checkable findings verified at `463498ed`. Parent spec: `2026-08-23-cross-catalog-program-rev5.md` §Stage-2. The governing correction stands: **AI-proposed links propose and explain features; exact physical evidence controls what executes — never what the user can see.**

**Goal:** A hypothesis surfaces cross-catalog feature cards from logical plans over AI-proposed-or-better links; every action a card offers is the verdict of the EXISTING decision authorities (never a parallel gate); whatever generates carries the exact pinned plan; production remains unavailable until its governance is released.

## The three-layer plan identity (P0-5 — replaces Rev 3's single envelope)

```
LogicalFeaturePlanV2   operands, output grain (ORDERED key-ref tuple), window + SELECTED
                       parameter revision (the promoted 1C-3 chooser's pick), logical bridge
                       path (symmetric link + traversal), formula policies
PhysicalExecutionPlanV1  execution-context revision, source bindings, realization revisions,
                       exact ordered column pairs, predicates, directional cardinality
RenderProfileV1        engine, compiler version, renderer version
```

Composed digests: authoring pins the LOGICAL digest; selection pins logical+formula; build/generation pins logical+physical+render. A renderer bump never re-buys a paid authoring run; an environment change never redefines a feature; a join column/predicate/cardinality change ALWAYS mints new plan identity. The identity/staleness matrix (Rev 3, verbatim) binds every task; evidence splits into load-bearing semantic material / currentness dependencies / display+ranking annotations — display text changes never rekey code (T0 verifies the current candidate-revision identity material and splits it if it folds display evidence in).

## Capability = projection, never authority (P0-2 + C2)

Discovery: `CARD_AVAILABLE` (logical plan resolves over `active_bridges`). Technical rendering mode: `EXACT | PROVISIONAL | UNAVAILABLE`. Everything else is the EXISTING authorities' verdict, projected: per-option `activation_policy.decide_all_actions` (the wire's `allowed_actions`/`blocked_actions` — "the client re-implements NO policy") and the six-action `materialize/action_decision.py` service over `ACTION_DISPOSITIONS` (closed on both axes, CI-pinned exhaustive over `REASON_FAMILIES` × `ActionV1`; adding a code is the THREE-PART commit). `CapabilityAssessmentV1` FORMATS these for the UI and may add discovery/render-mode facts; it never independently authorizes. New blocker codes (`DIRECTIONAL_REALIZATION_MISSING`, `DIRECTIONAL_MAPPING_INCOMPLETE`, `CARDINALITY_EVIDENCE_REQUIRED`, `ALLOCATION_POLICY_REQUIRED`, `REALIZATION_ATTACHMENT_DEFECT`, `EXECUTION_CONTEXT_MISSING`, `JOIN_NULL_POLICY_MISSING`, `JOIN_COVERAGE_POLICY_MISSING`, `MAX_MATCH_POLICY_MISSING`) register via the three-part commit with a rung↔action mapping. Production actions render **"unavailable — production execution governance is not released"** (they are unavailable in the service today, not certificate-pending); gold certification becomes the production gate when the action exists.

## Global Constraints (carried + amended)

- Discovery from logic, execution from evidence; missing sandbox environment blocks PREVIEW (`EXECUTION_CONTEXT_MISSING`), never the card (P0-8).
- Pinned plan = sole compilation authority; revalidate pinned revisions; never re-plan/compare-choose/substitute-latest. Fresh planner runs are diagnostic-only.
- Production readers untouched; provisional lane via separate typed contracts; the pre-aggregation runtime gate = target-side join-tuple uniqueness under exact predicates + DECLARED null-key / unmatched-key-coverage / max-matches policies — absent declarations BLOCK by name, never invented defaults; a passing runtime observation is evidence from one sandbox snapshot, never automatic production-safety promotion.
- Known M:N: card available; SOURCE-grain intermediate formula potentially authorable; FINAL target-grain formula blocked `ALLOCATION_POLICY_REQUIRED` (a joint account's 100k must not silently double); preview blocked (P0-9).
- Direction, ordered column pairs, predicates, realization revision live on the PATH SEGMENT; the symmetric link carries full ordered endpoint tuples + semantic revision + evidence deps + annotations — never a global direction, never pairing-by-tuple-position (P0-11).
- One activation authority (`ResolvedCrossCatalogActivationV1` — flag state, signed approval identity, artifact hash+expiry, cohort approval, version vector, reasons), resolved once, pinned into the considered option, revalidated same-facts at draft/confirm. `PLAN_CONTRACT_VERSION` BUMPED (already in the vector); `GOVERNED_SERVING_POLICY_VERSION` added. Anchor rule; byte-neutrality when inactive; append-only demand + satisfaction projection; four-way validation vocabulary; "candidate predictor" with lookback/horizon separated. Scope: aggregate-then-bridge grain bridging ONLY; dimension/reference enrichment chartered.
- **Migrations: coordinate a block at 1130+.** The parent-child production-readiness plan provisionally owns 1121-1129 — AND its 1121 assignment is dead (ours is live). T0 task: reconcile both reservation tables in the shared registry, identify 1117's owner, then allocate. Never assign 1122-1129 unilaterally (P0-10, M1).
- Every deploy/migration/flag = explicit user go. Never bare `git stash`/`git stash pop`; never `git checkout --` uncommitted work.

## §V (Rev 3 facts re-verified by the independent review; exactness fixes applied)
V1-V11 stand with corrections: `execution_tier` DEFAULTS to PRODUCTION in `revalidate_bridge_realization` (:782) — and `executable_bridge_realizations` hardcodes PRODUCTION (:900) while revalidation refuses tier mismatch (:795-800), so preview-tier work needs SANDBOX-scoped realization rows or `PREVIEW_EXECUTABLE` is empty in practice; wiring dialect at `wiring.py:199`; entity-only raise `contract.py:776`; current-state join re-resolution lives in `materialize/joins.py:920`; `selection_formula_binding` ALREADY pins `planning_request_hash` + `binding_plan_hash` (:41-42) — the new bindings must derive-from/check-against it, or "disagreement impossible" is false; run-spine substrate (migrations 1100-1119, spend authorization, action decisions) confirmed IN the baseline; `environment_id`/`ExecutionTier` vocabularies already exist (`1041_catalog_engine.sql:45`, `bridge_realization.py:82`) — the execution context BINDS to them, minting nothing parallel.

**T0 (mandatory):** re-verify everything above + both reviews' citations at the then-current origin/main; migration-registry reconciliation; 1117 ownership; run-spine store contracts; gold-gate/production-action mechanics; the candidate-revision identity material (P0-12). Ledger deltas; amend before dispatch.

---

# PHASE P — Parent amendment (first commit, its own task)
Amend `2026-08-23-cross-catalog-program-rev5.md` §Stage-2 entry criteria EXPLICITLY: implementation may begin now (owner ruling 2026-08-24); ACTIVATION (targeted first-serve included) stays behind: signed SME thresholds, accepted wave-1 evidence, accepted latency benchmarks, `FEATUREGEN_MATERIALIZE_INVENTORY` + inventory health, and the signed gate artifact. The child never silently changes the parent's gate (P0-1).

# PHASE A — Discovery tells the truth

**A0 (new — C1): G2 disposition before anything the journeys touch.** The G2 masking (`need_metadata._derive_one` MEASURE-defaulting, unmasked the moment cardinality attaches — `compile_aggregation`'s `card is None` short-circuit) sits directly under journeys 2/3: `rapid_movement_passthrough` and `fan_in_fan_out` carry counterparty/dimension operands in the documented 82-operand divergence worklist. Task: rule G2 for the journey operand set (extend the S1A-4c role-mapping seam or explicitly pin the journey recipes' operands as correctly-classed via the by-shape divergence test), AND name each journey's exact recipe + fixture preconditions: review rows (`RECIPE_REVIEW_NOT_CURRENT` gates `author_formula`), policy realizations for `CORRIDOR_RISK` (1075 substrate) if `high_risk_corridor_exposure` is used, party-role-resolved counterparty columns in the fixture catalogs.

**A1 (amended):** capability projection layer per the section above — three-part code registration, rung↔action mapping, `activation_policy`/frozen-facts amendment so a LOGICAL-plan-backed option passes `_contract_blockers` for `author_formula` (today `plan_envelope_present=false` → `PHYSICAL_PLAN_MISSING`) while physical rungs still gate on evidence (C3).

**A2:** projection extension — full ordered endpoint tuples, semantic revision, evidence-dependency split, annotations; `members[0]` collapse removed; no raw ledger reader; no global direction.

**A3 (amended — I7):** server-owned execution context BOUND to the existing `environment_id`/`ExecutionTier` vocabularies; immutable revision id; in considered-option identity; absence = `EXECUTION_CONTEXT_MISSING` on preview rungs only.

**A4 (amended — M5):** `BridgeRealizationSnapshotV1` as an EXTENSION of `CompileBudget`'s existing cap/deadline split; constant-query batch; truncation cause persisted + disclosed.

**A5 (amended — C3):** the sub-executable plan artifact DECIDED and named: the logical plan resolves with physical cardinality deferred (planner change, declared identity impact, pins regenerated) — producing a real `LogicalFeaturePlanV2` for card/formula rungs even when no realization exists; `grain_refs` for the draft worker derive from it. Realization attachment before compile upgrades to the physical rungs.

**A6 (amended — I4):** demand vocabulary migration (in the 1130+ block) + ONE demand-writing law: serve-time and worker-lane writers dedupe through `demand_identity_hash`; the satisfaction projection; telemetry reads provisional-relevant realizations so unknown-cardinality stops masquerading as no-realization.

### Phase A gate: ladder projections agree with `decide_all_actions` on every fixture (a property test — no card ever shows an action the service blocks); composite-key round-trip; G2 disposition ledgered; suites green.

# PHASE B — The pinned plan

**B1:** the three-layer identity model (migrations in the 1130+ block): `LogicalFeaturePlanV2`, `PhysicalExecutionPlanV1`, `RenderProfileV1`, composed digests, canonicalization; same-read-set/different-shape → different digests; ordered `output_grain_key_refs` tuple surviving card→formula→build→IR→render.

**B2 (renamed — I1/I2/P0-6):** the TOTAL binding chain with composite FKs, atomically committed: `considered_option_plan_binding` → **`formula_draft_plan_binding`** (NOT `formula_draft_authoring_plan` — that table exists, migration 1104, different job) → `selection_formula_plan_binding` → build-set member referencing the COMBINED binding. Invariants: a considered cross-catalog option cannot exist without exactly one logical binding; a draft cannot start without it; a selection cannot bind a formula from another logical plan; the new digests derive-from/are-checked-against the EXISTING `binding_plan_hash`/`planning_request_hash` pins (one truth, tested); build-set identity versioned (M4).

**B3 (extended — P0-7):** the logical digest enters formula AUTHORING itself: authoring request content, provider input audit, authoring-decision evidence pins, formula validation. Legacy drafts without the binding are REFUSED for cross-catalog builds, never backfilled. Then `PinnedResolvedFeatureInputV3` (mandatory wrapper) through restore → admission → compilation (pinned = sole authority; revalidation with the pinned context; refusal on revocation/supersession).

**B4:** provisional lane (typed assessment, separate reader, provisional join IR with exact pairs+predicates, `compile_expression` wiring, renderer support) + the runtime gate with DECLARED policies and their closed absent-policy blockers; no auto-promotion from a pass.

### Phase B gate: Plan-A/Plan-B unrepresentable (binding property tests incl. the binding_plan_hash cross-check); authoring-without-plan-digest refused; fan-out fixture refuses at admission AND compilation; provisional runtime gate fails a seeded fan-out BEFORE aggregation with: no output written, no partial aggregate, attempt terminalized (not retried), queue item completed, named failure on the run dashboard, immutable observation against the exact realization + snapshot.

# PHASE C — Rendering + serving (wired last)

**C1:** catalog-qualified dialect everywhere (raw keys, dataset names, node inputs, catalog YAML, spine validation, gate manifests, source-binding validation); RENDERER_VERSION bump; two-catalog same-`schema.table` distinct through the complete project; NEGATIVE: wrong-catalog spine REFUSES.

**C2 (amended — I5):** serving under `ResolvedCrossCatalogActivationV1`; branch restructure (engine + governed, additive, anchor rule); SERVING'S OWN ceiling numbers (cap, deadline, one snapshot read, disclosed truncation — the shadow's 500/30s is not a serving budget); the outbox-lane reconciliation stated: serving still enqueues telemetry work, serve-time plans mint observations under a distinct mode or dedupe rule so the 1C report's denominators stay honest; ranking at FIRST serve weights hypothesis relevance, bridge tier, fan-out risk, semantic completeness, formula readiness, data availability (P1 — not deferred to Phase E).

**C3:** twin identity end to end (all intermediate maps keyed option_id/governed_variant_id).

**C4 (new — I3/P1):** the frontend card experience: `api.ts` contract (rung, per-rung blockers with remedies, provenance, render mode, participating catalogs + join path) with a HOME for card-only options (the considered-set's three actionability sections gain a discovery section or equivalent — decided at T0 against the real wire shape); Workbench rendering: cross-catalog badge, AI-proposed/human-confirmed provenance, provisional warning, disabled actions DRIVEN BY the backend verdicts (zero TypeScript policy), accessibility + responsive tests; production actions render the governance-not-released copy (M3: the executable-preview rung's card copy reconciles with the "not execution-ready" labeling until S2-P6 lands).

### Phase C gate: a proposed-link card served end to end with honest rungs; property test: no rendered action contradicts `decide_all_actions`; byte-identity when inactive; suites green.

# PHASE D — Six journeys (public APIs, real workers; recipes NAMED at A0)

1. Proposed link, no realization → card + formula; preview `DIRECTIONAL_REALIZATION_MISSING`; demand recorded once.
2. Proposed link, unknown cardinality → provisional preview; runtime PASS and FAIL (atomicity assertions per Phase B gate).
3. Exact 1:1/N:1 → full cross-catalog preview; IR asserts: ≥1 cross-catalog join step, two catalog-qualified inputs, the bridge-gate node, the pinned ORDERED grain tuple, ONE composed digest across card→selection→build→IR→render.
4. Known M:N → card; source-grain intermediate authorable; final-grain formula `ALLOCATION_POLICY_REQUIRED`; preview refused; demand recorded.
5. Recipe-origin, LLM-authored formula (spend via the existing cost-confirmation; FakeLLM; method provenance) → preview → fixture EXECUTION with expected banking values over the adversarial set (duplicates, reversals, multi-currency, late posting, post-cutoff, joint M:N).
6. **Genuine LLM-ORIGIN feature (P0-4):** LLM-proposed intent → immutable authoring-subject revision → exact computation promoted (the `CONCEPTUAL_PATTERN_NOT_AUTHORABLE` dead-end replaced by the parent's S2-P1 promotion journey, scoped to ONE promotable pattern class) → LLM formula authored + validated READY → selection → plan/formula binding → build set → cross-catalog preview.

# PHASE E — Operator rail (explicit user go)
E1: migrations backend-first; telemetry flip + worker; `FEATUREGEN_MATERIALIZE_INVENTORY` + multi-catalog mappings + SANDBOX-scoped realization seeding (I7 — else the executable rung is empty); E2: satisfaction projection; E3: targeted-cohort activation ONLY after the parent's amended activation gate passes (signed thresholds, wave-1, latency, inventory health); SME thresholds gate broad enablement.

# Not in scope (chartered): M:N allocation policies (journey 4 records the demand); dimension/reference-enrichment joins; full S2-P6 residency/PIT/data-movement; federated execution; propose-bridge surface; LLM promotion beyond journey 6's one pattern class.

# Execution
SDD, Stage-1 protocol; T0 first; ledger at `.superpowers/sdd/<plan-basename>/progress.md`; final whole-branch review; NO merge/push/deploy/flag-flip without explicit user go.

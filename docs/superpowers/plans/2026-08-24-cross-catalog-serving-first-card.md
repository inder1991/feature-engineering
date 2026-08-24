# Cross-Catalog Serving — "First Served Card" Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. This plan compresses the Stage-2 charter of `2026-08-23-cross-catalog-program-rev5.md` under the owner's 2026-08-24 directives: (1) cross-catalog feature functionality is the priority; (2) AI-proposed links and joins are first-class serving inputs; (3) SME thresholds gate BROAD enablement, not first serve. Execute with the same rigor as Stage 1: fresh implementer per task, review-gated, ledgered rulings.

**Goal:** A user's hypothesis returns served cross-catalog feature cards backed by governed plans over AI-proposed-or-better bridges; confirm and preview generation work end to end; production publication stays certificate-gated (the standing gold-gates ruling).

**Architecture:** Three build phases + one operator rail. Phase A makes cross-catalog plans *resolvable* (bridge candidacy widened to the AI-proposed tier; directional realization attached in the serving path — closing G3). Phase B wires the already-built governed lens into the scoped serving route (S2-P3 essentials). Phase C binds confirmed governed options into the run-spine coordinator through immutable pins and admits preview generation behind envelope-exact identity and per-catalog source binding. Phase D (operator, parallel) flips telemetry as reconnaissance and moves SME thresholds to the broad-enablement gate.

**Tech stack:** existing platform — FastAPI routes, the governed lens/planner (Stage 1), `entity_bridge_*` tables (0989/1036/1037/1040), run-spine stores (`build_set_store.py`, `code_generation_job_store.py`, `execution_revisions.py`), React frontend.

**Spec:** `docs/superpowers/plans/2026-08-23-cross-catalog-program-rev5.md` §Stage-2 charter (S2-P1..P8) is the parent spec; this plan is its compressed, re-gated execution order. Conflicts resolve against the parent EXCEPT where the owner's three 2026-08-24 directives override (recorded above; they are decisions, not defects).

## Global Constraints

- **Executable-authority law (verbatim from `planner/multisource_compile.py`):** "Human confirmation is audit metadata, never execution authority. A bridge crossing is executable only when the path carries the exact deterministic directional realization revision." AI-proposed serving rides THIS law: candidacy widens; executability still requires the deterministic realization revision on every governed segment. No task may substitute confirmation status for realization proof, or vice versa.
- **Tier honesty:** every served cross-catalog card carries its weakest bridge's tier (`ai_proposed` | `confirmed` | `verified`) and strength as display provenance; ranking may weight tiers; NOTHING may hide or refuse a card solely for being `ai_proposed` (owner directive + the standing "links usable before confirmation" and "no 'blocked'" rulings).
- **Byte-neutrality until the switch:** engine (single-catalog) cards byte-unchanged in every response; governed cards strictly additive. The serving flag default-off; flag-off responses byte-identical (pinned, as Stage 1 pinned telemetry).
- **Identity discipline:** the Stage-1 literal pins (`bp_0adcb0a8c5748e1a`, `cc_095c221534f53e67`) must hold through every task unless a task EXPLICITLY declares an identity-impacting change with its own migration/versioning; `PLAN_CONTRACT_VERSION` and `GOVERNED_SERVING_POLICY_VERSION` enter `current_version_vector()` (`contract/live_activation.py:59`) in Phase B's atomic commit, per the Stage-1 deferral ruling.
- **Four-way validation vocabulary:** Code / Fixture / Sandbox-profiling / Production-certification. Cards never claim beyond what ran. Prediction-goal hypotheses (e.g. AML escalation) serve features as "candidate predictors" — predictive claims require an approved label + population (S2-P7 rule).
- **Honest refusal:** where even an AI-proposed link is absent, or a directional realization cannot be uniquely matched, the planner refuses by name and files demand (Stage-1 ledger). Fan-out risk (M:N realization) displays on the card; it never silently double-counts.
- **Every deploy, migration apply, and flag flip is an explicit user go.** Never bare `git stash`/`git stash pop`; never `git checkout --` a file holding uncommitted work.

## §V Verified facts (2026-08-24, at origin/main `463498ed`)

- V1. `attach_executable_bridge_realizations(plan, realizations) -> BindingPlanV1 | None` exists at `planner/assembly.py:404` — pure; binds each `governed_bridge` segment to exactly ONE directional `CurrentBridgeRealizationV1` (else `None`); production source is `executable_bridge_realizations`. Its Stage-1 callers (governed_lens, worker) read realization STATE for evidence only — no serving-path attachment exists.
- V2. `entity_bridge_edge` (0989) is a VERIFIED-only projection (`status text NOT NULL -- 'VERIFIED'`); the compile path filters `status = 'VERIFIED'` AND `bridge_lifecycle_status(...) == REVIEWED_STATUS` (`multisource_compile.py:288-292`). AI-proposed candidates live in `entity_bridge_candidate_evidence` (0989) with `evidence_json`, `candidate_id`, `proposed_event_id` — they NEVER reach the edge projection today. Widening candidacy = a tiered read, NOT a status write to the edge table.
- V3. `_EXECUTABLE_BRIDGE_AUTHORITY = "deterministically_validated"` / `_PROVISIONAL_BRIDGE_AUTHORITY = "provisional"` (`multisource_compile.py`) — the authority split already distinguishes realization-proof from provisional.
- V4. Realization substrate: migrations 1036 (canonical orientation), 1037 (candidate realization store), 1040 (currentness) exist; `derive_catalog_realizations` feeds `RealizerFactV1` demand evidence (Stage 1B-2).
- V5. The scoped route (`api/routes/contract.py`): disposition-lens threading at ~:241/:846-:857; `disposition_applicability.eligible_ids` computed once and already threaded as `v2_eligible_ids` for telemetry (S1B-3); the shadow flags are read AT THE ROUTE.
- V6. Run-spine coordinator surfaces on main: `build_set_store.py`, `code_generation_job_store.py`, `execution_revisions.py` (merged `52e42f52`, "run ids, dashboard, honest rail, governed retry" + coupon-money law commits). Task C1 verifies their exact contracts BEFORE binding (T0 duty).
- V7. `current_version_vector()` at `contract/live_activation.py:59`; live_activation.py is byte-frozen vs the Stage-1 baseline — Phase B's edit is the FIRST sanctioned change and must update the freeze expectation deliberately.
- V8. Stage-1 serving-side building blocks, built and tested, awaiting consumption: complete `GovernedOptionV1` builder (`governed_lens.py`), `fold_governed_binding_plan`, `decision_facts_for_governed_option` (canonical id in column, `governed_variant_id` in evidence), `demands_for_rejection`, the chooser with content-addressed replay (`param_choice.py`), observation ledger 1120/1121 + report + demand queues.
- V9. G3 refusal `physical_cardinality_unavailable` rides RESOLVED contracts lacking realization; it is the dominant honest refusal Stage 1 measured for.
- V10. Gold gates: production publication hard-blocked without certification; preview permitted (standing ruling; Phase C4 verifies the exact gate names live on main before relying on them — G-1's `PUBLICATION_REFUSED(CAPABILITY_UNPROVEN)` lineage).

**T0 (mandatory, before Task A1):** re-verify V1-V10 against the then-current origin/main; verify run-spine store contracts (V6) and the gold-gate names (V10) in full; record deltas in the ledger; amend tasks before dispatch. Baseline every freeze/pin from that commit.

---

# PHASE A — Plans become resolvable (close G3, widen candidacy)

## Task A1: tiered bridge candidacy — the planner consumes AI-proposed links
**Files:** `planner/multisource_compile.py` (the VERIFIED-only read widens to a tiered read), `planner/contracts.py` (segment gains `bridge_tier: str = ""` — defaulted, NON-identity: prove via the literal pins), `overlay/upload/cross_catalog_links.py` (the candidate-tier read helper beside the existing VERIFIED read), tests beside the existing multisource tests.
**What:** one read function returning bridges at three tiers — `verified` (edge projection, as today), `confirmed` (lifecycle-confirmed but unprojected, if that state exists — verify; else omit the tier), `ai_proposed` (from `entity_bridge_candidate_evidence`, keyed off `candidate_id`/`evidence_json`, minimum-evidence floor: candidate must name both object refs + a key pair — no floor on human review). Plans built over proposed-tier bridges carry `bridge_tier="ai_proposed"` on the segment and `_PROVISIONAL_BRIDGE_AUTHORITY` until realization attaches. The tier is DISPLAY + ranking material, never identity, never an execution gate by itself (Global Constraint 1).
**Tests (failing first):** a two-catalog fixture whose only link is a candidate row → plan resolves with `bridge_tier="ai_proposed"`; the VERIFIED fixture still resolves `verified`; no candidate at all → the existing refusal + demand unchanged; literal identity pins hold.

## Task A2: realization attachment in the serving path
**Files:** `contract/governed_lens.py` (the resolved-plan path calls `executable_bridge_realizations` + `attach_executable_bridge_realizations` before minting the served option), `planner/assembly.py` only if the attachment needs the candidate-realization store (1037) as a second source — verify first,
**What:** a resolved cross-catalog plan gets its directional realization attached at serve time; `None` (missing/ambiguous) converts the option to the EXISTING `physical_cardinality_unavailable` refusal + demand (no new vocabulary). Realization-backed segments flip to `_EXECUTABLE_BRIDGE_AUTHORITY`. Realization evaluation also yields the segment cardinality → `fan_out_risk` plan fact (feeds B3 display + the report's currently-not-computable metric).
**Tests:** the S1A-4b real-recipe pack fixture that Stage 1 pinned as REJECTING at G3 now RESOLVES once its realization rows are seeded; ambiguous realization (two directional matches) still refuses; the fan-out fact appears for an M:N realization; demand queue drains for the seeded crossing (observation assertions).

## Task A3: the AML-class end-to-end planning proof (fixture)
**Files:** new test module under `tests/featuregen/overlay/upload/contract/`.
**What:** a two-catalog fixture modeled on the AML hypothesis (payments events catalog + core customer catalog, AI-proposed account↔customer candidate link, seeded realization): plan `corridor_cross_border_share`, `high_risk_corridor_exposure`, `fan_in_fan_out`, `rapid_movement_passthrough` at customer grain, 90-day window; assert resolved plans, `ai_proposed` tier on every crossing, correct grain, window binding, and one deliberately-missing-link recipe refusing with demand. This is the plan's acceptance fixture — Phases B/C reuse it.

### Phase A gate
Upload-tree suite green; literal pins hold; the Stage-1 behavioral guard's allow-list widened ONLY by sanctioned entries; A3 fixture green.

# PHASE B — Serving wire-up (S2-P3 essentials)

## Task B1: the governed lens on the scoped route (atomic serving commit)
**Files:** `api/routes/contract.py` (thread `v2_eligible_ids` + the governed lens behind `FEATUREGEN_GOVERNED_SERVING` read at route), `contract/gate1.py` (accept + pass the lens enablement kwarg, defaulted off), `contract/live_activation.py` (`GOVERNED_SERVING_POLICY_VERSION = "gsv-1"` joins `current_version_vector()` — SAME commit; the Stage-1 freeze expectation updated deliberately), the two Stage-1 lens-pin tests updated deliberately.
**Tests:** flag-off byte-identity (route-level `res.json()` compare); flag-on adds governed cards strictly additively (engine cards byte-unchanged — the additive guardrail); the stale-approval test: an approval recorded under the old version vector no longer authorizes.

## Task B2: served-twin facts live
**Files:** `contract/gate1.py`/serialization site (feature JSON carries `governed_variant_id` additively), `semantic_option_decision.py` (decision persist prefers it — `decision_facts_for_governed_option` is BUILT, wire it), tests.
**What/Why:** an engine option and a governed option for one recipe never share a facts row; persisted `source_definition_id` stays canonical (V11 of the parent plan).

## Task B3: the card tells the truth
**Files:** frontend `api.ts` types, the card component(s) (locate at T0; SearchScreen/feature card lineage), tests.
**What:** governed cards show: participating catalogs, the join path summary, bridge tier + strength ("AI-proposed link — not yet human-reviewed" phrasing per the honest-absence rules), fan-out risk when present, "candidate predictor" framing for prediction-goal hypotheses, "final checks run at draft" copy. No "blocked", no "approved" below VERIFIED, no validation claims beyond Code/Fixture.
**Tests:** vitest exact-copy assertions per element; 403-quiet unchanged; engine cards render byte-identically flag-off.

## Task B4: draft/confirm positive proof
**Files:** route-level tests (real registry, no monkeypatching).
**What:** the COMMITTED draft/confirm path over an A3-fixture governed card: confirm-time tamper checks pass, the server-derived governed join path lands in the confirmed artifact; a named refusal FAILS this test (refusals are separate tests). Confirm-time re-verification: if the bridge candidate/realization moved since serve, confirm refuses `stale_registry`-style (reuse Stage-1's staleness idiom) rather than binding a moved join.

### Phase B gate
Full suites green; flag-off byte-identity pinned; a served governed card demonstrable in the A3 fixture end to end.

# PHASE C — Confirm → preview generation (compressed S2-P1/P2/P4/P6)

## Task C1: governed options through the run-spine coordinator
**Files:** verify-then-modify `build_set_store.py` consumers / the selection persist path (T0 names the exact seam).
**What:** a confirmed governed option pins immutably into the build set: option_id, considered_revision_id, formula revision (recipe-origin: the recipe's formula; LLM-origin: DEFERRED to the promotion journey — out of this plan, charter note), `governed_variant_id` + component hashes, plan-envelope hash. Build sets never mutate after creation (run-spine law; verify and reuse, never reinvent).
**Tests:** pin round-trip; mutation refusal; a second confirm of the same option reuses the pinned identity.

## Task C2: envelope-exact generation admission (S2-P4 minimal)
**Files:** `planner/contracts.py`/new `plan_envelope_v2.py` (versioned sealed envelope: per-segment direction, cardinality, relationship id+revision, bridge realization revision, catalog-qualified temporal hash, dependency evidence — the segment data EXISTS per V16 of the parent plan), the generation admission compare site (T0 locates it in `code_generation_job_store.py`'s admit path).
**What/Why:** admission compares the FULL envelope, not read-set equality. **Acceptance test = the fan-out fixture:** two same-read-set join shapes; one must refuse. Banking aggregates never double-count because two joins looked alike.

## Task C3: execution-source binding minimum (S2-P6 floor)
**Files:** the pre-generation check site + tests.
**What:** before preview generation: every participating catalog resolves to a bound physical execution source (the physical-table-config substrate — T0 verifies what landed with run-spine; if NO binding store exists yet, this task builds the MINIMAL per-catalog binding record + check, and federated/multi-engine execution is refused BY NAME). Same-substrate reachability alone proves nothing (parent S2-P6) — but the floor here is: named binding exists + engine compatibility asserted, NOT the full eight-point checklist (deferred to the parent charter; Deferred-NFRs).
**Tests:** two-catalog preview with both catalogs bound → admitted; one unbound → named refusal; cross-engine → named refusal.

## Task C4: preview permitted, production hard-blocked (verification task)
**What:** prove by test against live main that the gold-gate still hard-blocks PUBLISH_PRODUCTION for governed cross-catalog features without certification while preview generation is admitted (V10). If the gate names moved, fix the TESTS, never the gate.

### Phase C gate
A3-fixture hypothesis → served card → confirm → build-set pin → preview generation runs → rendered two-catalog output validated at Code+Fixture level; production publication refused by name. Full suites green.

# PHASE D — Operator rail (parallel from day 1; every step explicit user go)

- **D1 (immediately, before/alongside Phase A):** apply migrations 1120/1121 backend-first; flip `FEATUREGEN_INTENT_SHADOW_TELEMETRY`; schedule the telemetry worker (runbook §3's manual invocation or a CronJob — owner picks). Purpose here: RECONNAISSANCE — the demand ledger ranks which crossings real hypotheses need, which orders Phase-A realization seeding and the bridge-candidate backfill.
- **D2:** ranking joins the shared framework with bridge-evidence tier + fan-out risk as features of the ranker (basic weights; no learned model). Honest-labels enforcement for prediction-goal hypotheses verified in B3's tests.
- **D3 (broad-enablement gate — moved per owner directive):** SME reviews the corpus, signs the thresholds doc; wave-1 report (now with real traffic + the A2-unblocked resolution rates) meets them → `FEATUREGEN_GOVERNED_SERVING` graduates from targeted-on to default-on. First-serve does NOT wait on D3.

# Not in scope (charter remains open in the parent plan)
S2-P1 LLM promotion journey (LLM-origin formulas to serving — the "shift/delta" features; next program), S2-P5 renderer dialect unification, S2-P6's full eight-point execution checklist, federated execution, the operator propose-bridge surface, `concept`-tier authority promotion, G2.

# Deferred-NFRs
Worker/planner latency SLOs beyond the 1C-4 draft numbers; realization backfill tooling at scale; ranking model learning; multi-engine execution.

# Execution
Subagent-driven (Stage-1 protocol): T0 first; fresh implementer per task; review-gated; ledger at `.superpowers/sdd/<this-plan-basename>/progress.md`; final whole-branch review; NO merge/push/deploy/flag-flip without explicit user go.

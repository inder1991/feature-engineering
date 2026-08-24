# Cross-Catalog Serving — "First Served Card" Implementation Plan (REV 3)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Rev 3 supersedes Rev 2 after a second adversarial review (2026-08-24, 15 findings — the checkable ones ALL verified at `463498ed`/`d7b2317f`). Parent spec: `2026-08-23-cross-catalog-program-rev5.md` §Stage-2. The governing correction, verbatim:
>
> **"AI-proposed links should be sufficient to propose and explain cross-catalog features. Exact physical evidence should control what can EXECUTE — not whether the user can SEE the feature idea."**

**Goal:** A hypothesis surfaces cross-catalog feature cards from LOGICAL plans over AI-proposed-or-better links; each card states exactly what the platform can do with it (the capability ladder) and why; whatever generates carries the EXACT pinned join the user confirmed; production stays certificate-gated.

## The capability ladder (the product model — closed vocabulary, closed reason codes)

```
CARD_AVAILABLE        logical plan resolves over active bridges (proposed suffices)
FORMULA_AUTHORABLE    grain + operands + window bindable (realization NOT required)
PREVIEW_PROVISIONAL   exact column mapping known, cardinality unproven → runtime-gated code
PREVIEW_EXECUTABLE    directional realization attached, cardinality 1:1 / N:1 proven
PRODUCTION_ELIGIBLE   certification + full execution-evidence checks
```

Each rung names its blocker from a CLOSED set when unavailable: `DIRECTIONAL_REALIZATION_MISSING`, `DIRECTIONAL_MAPPING_INCOMPLETE`, `CARDINALITY_EVIDENCE_REQUIRED`, `ALLOCATION_POLICY_REQUIRED`, `REALIZATION_ATTACHMENT_DEFECT`, plus the existing refusal vocabulary. A proposed link with no realization yields a CARD and FORMULA with `preview: DIRECTIONAL_REALIZATION_MISSING` — never invisibility. Human confirmation moves DISPLAY provenance only; it never changes a rung, an identity, or generated code.

## Identity & staleness law (adopted verbatim; every task tests against it)

| Change | Expected result |
|---|---|
| Proposed → human-confirmed | No physical-plan or code rekey |
| Strength/ranking changes | No code rekey |
| Candidate withdrawn/rejected | Refuse |
| Realization revoked | Refuse |
| New realization supersedes old | Refuse old pinned plan; NEVER substitute latest |
| Evidence display text changes | No physical rekey |
| Join column/predicate/cardinality changes | New plan identity |

## Global Constraints

- **Discovery from logic, execution from evidence.** Cards/formulas need only the logical plan (path over `active_bridges` — proposed and confirmed are equally traversable, per the projection's own doctrine). Execution rungs need deterministic physical evidence. Neither gate borrows from the other.
- **Pinned plan is the SOLE compilation authority.** Generation compiles FROM the frozen envelope and exact realization revisions; it revalidates those revisions (currentness, revocation, authorization) and refuses on failure; it NEVER re-plans, compares-and-chooses, or substitutes "latest". A fresh planner run is diagnostic-only.
- **Production readers stay intact.** `executable_bridge_realizations` (M:N-excluding) is untouched; provisional capability arrives via a SEPARATE typed assessment {`EXECUTABLE`, `PROVISIONAL_UNKNOWN_CARDINALITY`, `REFUSED`} + separate provisional reader + provisional join IR.
- **The runtime cardinality gate is join-tuple uniqueness, not row counts:** target-side join-tuple uniqueness under the exact predicates + declared null-key policy + unmatched-key coverage policy + optional max-matches, checked BEFORE aggregation; failure stops output by name. (Row-count preservation is unsound — join type and unmatched keys move counts independently.)
- **One activation authority, one object:** `ResolvedCrossCatalogActivationV1` {deployment flag state (`FEATUREGEN_INTENT_LIVE_CROSS_CATALOG`), signed approval/evaluation identity, signed artifact hash + expiry, cohort approval identity, version vector, allowed/blocked reasons} — resolved ONCE per request, identity pinned into the considered option, revalidated (same facts) at draft and confirm. `PLAN_CONTRACT_VERSION` is ALREADY in the vector — BUMPED, not added; `GOVERNED_SERVING_POLICY_VERSION` added. No new flag.
- **Anchor rule; byte-neutrality when inactive; append-only demand history with a satisfaction projection; four-way validation vocabulary; "candidate predictor" for prediction goals (lookback vs horizon asserted separately); every deploy/migration/flag = explicit user go. Scope: cross-catalog GRAIN BRIDGING (aggregate-then-bridge) ONLY — dimension/reference-enrichment joins (filter-before-aggregation, e.g. "payments BY customers rated high-risk") are a chartered follow-up with their own typed contract; cards for such hypotheses refuse the un-plannable clause honestly.**
- Never bare `git stash`/`git stash pop`; never `git checkout --` a file with uncommitted work.

## §V Verified facts (2026-08-24 @ `463498ed` + `d7b2317f`; T0 re-verifies ALL + reconciles with the then-current execution branch)

- V1. `active_bridges` serves proposed+confirmed alike; `ActiveBridgeV1` carries ONLY {fact_key, entity, left/right catalog+object_ref, status ("confirmed"|"proposed", documented as ANNOTATION), strength} — NO candidate revision, NO evidence refs, and the projection collapses endpoints to `members[0]` (`bridge_projection.py:165,168`) — composite keys are LOST today.
- V2. Compilation produces G3 (`physical_cardinality_unavailable`) on unrealized crossings; the lens refuses such options → today a proposed-but-unrealized link yields NO card. Rev 3's Phase A inverts this at the lens: capability-laddered cards, not refusals.
- V3. `attach_executable_bridge_realizations` pure (`assembly.py:404`); `executable_bridge_realizations` excludes {ONE_TO_MANY, MANY_TO_MANY} (`bridge_store.py:125`) and performs MULTIPLE reads per realization (no single batch); `revalidate_bridge_realization(conn, realization, *, purpose, environment, execution_tier)` exists (`:776`) — purpose/environment/tier are REQUIRED for validation and the serving request carries NONE today.
- V4. Serving unreachable: `routes/contract.py:775` entity-only rejection; `gate1.py:1160` engine branch vs `:1308` `elif is_live:` mutual exclusion.
- V5. Generation lane: `selection_formula_binding.py:32` pins selection+formula only; `restore_formula_v3.py:91` restores no plan; `ResolvedFeatureInputV2` (`admission_v2.py:94-100`) DELIBERATELY has no optional plan field (docstring: a `None`-filled field reads as "checked, and fine") — the pinned lane needs a NEW MANDATORY wrapper, not a field; `compile_ir_v2.py:108` re-resolves joins from current state; formula drafts are authored BEFORE selection and bound to no plan.
- V6. Renderer keys raw datasets `{schema}.{table}` (`render/project.py:347`) vs wiring's `catalog::schema.table` (`wiring.py:186`); catalog YAML, node inputs, spine validation, gate manifests, source-binding validation all need the qualified dialect; same-`schema.table` two-catalog collision is real.
- V7. Migration 1120's demand verdict CHECK is CLOSED and the file immutable — new demand types require a NEW migration. Telemetry today reads only executable realizations, so unknown-cardinality realizations masquerade as "no realization".
- V8. All A3 AML recipes `FORMULA_BLOCKED`; `posted_debit_amount` is the only reviewed expectation; growing that registry is an operator act. A `posted_debit_amount` feature CAN compile single-catalog — cross-catalog journeys must assert the IR itself (join step, two catalog-qualified inputs, bridge-gate node, pinned grain, one envelope digest card→selection→build→IR→render).
- V9. `PLAN_CONTRACT_VERSION` already in `current_version_vector()` (`live_activation.py:62`); flag at `:56`; signed-gate startup check ~`:242`.
- V10. Kind manifest: generation on, `FEATUREGEN_MATERIALIZE_INVENTORY` commented out (operator precondition).
- V11. Stage-1 substrate reusable (governed option builder, decision facts, 1120/1121 ledger + report, chooser). Migrations ≥1122 free (T0 confirms exact numbers).

**T0 (mandatory):** re-verify V1-V11 and every review citation against the then-current origin/main; pin run-spine store contracts + gold-gate names; reserve migrations (projection/evidence extension if persisted, PlanEnvelopeV2 relational model, three bindings, demand-type extension, execution-context revision, cohort approval); ledger deltas; amend tasks before dispatch.

---

# PHASE A — Discovery tells the truth (cards from logical plans)

## A1: capability states + closed reason codes (the contract everything else meets)
`contract/capability.py`: the five states, the closed blocker vocabulary, `CapabilityAssessmentV1` (per-option: rung reached, per-rung blocker codes, evidence refs). Pure; exhaustively tested; the single source for card copy, route payloads, demand mapping, and journey assertions.

## A2: the governed bridge projection carries what plans need
Extend the PROJECTION (never a raw candidate-ledger reader): full ORDERED endpoint member tuples (composite keys — `source_system+customer_number`-class), candidate revision, evidence revisions, direction, exact column-pair mapping when available. `members[0]` collapse removed. Annotations (status/strength/evidence text) stay OUT of physical identity per the matrix. If no exact ordered mapping exists: card allowed, formula per assessment, preview blocked `DIRECTIONAL_MAPPING_INCOMPLETE` — NEVER a mapping inferred by zipping flattened members.

## A3: server-owned execution context
Serving resolves an authorized preview/sandbox execution context BEFORE planning; persisted immutable `execution_context_revision_id`; included in considered-option identity; replanning under a different environment mints a NEW plan. (Without it, purpose/environment/tier — which `revalidate_bridge_realization` requires — would be fabricated at serve time.)

## A4: `BridgeRealizationSnapshotV1` — one immutable batched read
Constant-query snapshot of realization state for the COMPLETE considered set (executable + provisional-relevant rows), taken once per request; all downstream assessment reads the snapshot. Deterministic candidate CAP separated from the runtime DEADLINE; truncation persisted with its cause (cap vs deadline) and disclosed.

## A5: the lens serves the ladder instead of refusing
The governed lens converts compilation outcomes + snapshot evidence into `CapabilityAssessmentV1` per option: unrealized proposed link → CARD_AVAILABLE/FORMULA_AUTHORABLE with `preview: DIRECTIONAL_REALIZATION_MISSING`; mapped-but-unproven → PREVIEW_PROVISIONAL; realization attached + 1:1/N:1 → PREVIEW_EXECUTABLE; known M:N → card+formula, preview `ALLOCATION_POLICY_REQUIRED`. Identity-impacting planner changes (realization attachment before compile for the executable rung) are DECLARED: PLANNER_VERSION bump + literal pins regenerated deliberately in the same task.

## A6: demand ledger speaks the new vocabulary (new migration; 1120 untouched)
`bridge_demand_extension` (or successor table) with demand types {`DIRECTIONAL_REALIZATION_MISSING`, `DIRECTIONAL_MAPPING_INCOMPLETE`, `CARDINALITY_EVIDENCE_REQUIRED`, `ALLOCATION_POLICY_REQUIRED`, `REALIZATION_ATTACHMENT_DEFECT`} recording exact bridge, direction, ordered endpoints, realization revision, purpose/environment/tier. Telemetry + the report + the panel consume it; the satisfaction PROJECTION marks current demand met (history immutable).

### Phase A gate: ladder assessments correct on fixtures for ALL five rungs; composite-key fixture round-trips; suites green; identity change reviewed.

# PHASE B — The pinned plan (one join from card to code)

## B1: PlanEnvelopeV2 — relational model first
Migration + `planner/plan_envelope_v2.py`: ordered segment direction + endpoint refs (FULL tuples), relationship + realization revisions, realization content/dependency hashes, directional cardinality, selected parameters, output grain + grain key, temporal declaration + window, source-binding revisions, purpose/environment/execution tier (from A3's context revision), policy/currency/reference revisions, compiler + renderer versions. Canonicalization + full content digest. Same-read-set/different-shape → different digests.

## B2: three bindings, composite FKs — disagreement impossible
`considered_option_plan_binding` (considered_revision_id, option_id, plan_envelope_id, plan_digest); `formula_draft_authoring_plan` (formula_draft_id, considered_revision_id, option_id, plan_envelope_id); `selection_formula_plan_binding` (selection_revision_id, formula_draft_id, plan_envelope_id). Composite FKs so a formula authored under Plan A can never bind a selection under Plan B. Envelope digest enters build-set identity.

## B3: `PinnedResolvedFeatureInputV3` through restore → admission → compilation
A NEW MANDATORY wrapper (V5's docstring law — no optional field on V2): restore loads the bindings + envelope; admission compares digests; `compile_ir_v2` derives joins FROM the pinned realization revisions (sole authority); worker revalidates pinned revisions via `revalidate_bridge_realization` with the pinned context — refuse on revocation/supersession, never substitute. Fresh planner = diagnostic lane only, clearly labeled.

## B4: the provisional lane (separate contracts, production readers untouched)
Typed assessment {EXECUTABLE, PROVISIONAL_UNKNOWN_CARDINALITY, REFUSED}; separate provisional realization reader; provisional join IR step carrying exact column pairs + predicates; wiring into `compile_expression`; renderer support; the PRE-AGGREGATION runtime gate per the Global Constraint (join-tuple uniqueness + null-key + unmatched-key coverage + max-matches), failing by name before any aggregate. Rendered output labeled provisional.

### Phase B gate: the Plan-A/Plan-B story impossible by construction (binding tests); fan-out fixture refuses at admission AND compilation; provisional gate fails a seeded fan-out at runtime; suites green.

# PHASE C — Rendering + serving (wired LAST)

## C1: catalog-qualified dialect everywhere
`catalog::schema.table` through raw keys, generated dataset names, node inputs, catalog YAML, spine validation, gate manifests, source-binding validation; RENDERER_VERSION bump (declared). Tests: two catalogs, same `schema.table`, distinct datasets through the COMPLETE rendered project; NEGATIVE: wrong-catalog spine supplied → rendering REFUSES (not just distinct names).

## C2: serving under one verdict
Branch restructure (engine + governed lanes, additive merge, anchor rule), request-path ceiling (cap/deadline/one snapshot read/disclosed truncation), `ResolvedCrossCatalogActivationV1` resolved once and pinned; draft + confirm revalidate the same facts; byte-identity when inactive; staleness per the matrix. Card copy from `CapabilityAssessmentV1` — including the ladder, blockers, link provenance, provisional labeling, "candidate predictor".

## C3: twin identity end to end
Every intermediate map + persistence keyed `option_id`/`governed_variant_id`; canonical id stays canonical in its column; engine+governed twin survives all stages.

### Phase C gate: a proposed-link card served with honest rungs; suites green; byte-identity + anchor + ceiling pinned.

# PHASE D — The five journeys (public APIs, real workers)

1. **Proposed link, no realization:** card + authored formula; preview named `DIRECTIONAL_REALIZATION_MISSING`; demand recorded.
2. **Proposed link, unknown cardinality:** provisional preview; runtime gate PASS case and FAIL case (seeded fan-out stops before aggregation, named).
3. **Exact 1:1/N:1:** full cross-catalog preview — IR asserts ≥1 cross-catalog join step, two catalog-qualified physical inputs, the bridge-gate node, pinned landing grain, ONE envelope digest across card→selection→build→IR→render.
4. **Known M:N:** card + formula; preview refused; `ALLOCATION_POLICY_REQUIRED` demand recorded.
5. **AML value journey (LLM-authored):** a `FORMULA_BLOCKED` AML recipe through LLM authoring (spend via the existing cost-confirmation mechanism, FakeLLM fixtures, method provenance) → preview generation → fixture EXECUTION with expected banking values or named refusals over the adversarial set (duplicate txn ids, reversals, multi-currency, late posting, post-cutoff, joint-account M:N). Lookback vs horizon asserted separately.

### Phase D gate = the plan's acceptance. Operator lever (named): SME reviewing AML expectations converts journey 5 to deterministic.

# PHASE E — Operator rail (explicit user go, parallel)
E1: 1120/1121 (+ new migrations) backend-first; telemetry flip + worker scheduling (recon for realization seeding priorities); `FEATUREGEN_MATERIALIZE_INVENTORY` + multi-catalog mappings deployed + health-checked before live Phase-D runs. E2: demand-satisfaction projection live; ranking weights tier + fan-out risk (annotations only). E3: targeted-cohort activation (first-serve) inside the one authority; SME thresholds + wave-1 report gate BROAD activation.

# Not in scope (chartered): M:N allocation policies; dimension/reference-enrichment joins (typed contract, PIT rules, renderer support — the "filter by customer risk before aggregation" class); full S2-P6 residency/PIT/data-movement (preview stays labeled "not execution-ready" until it lands); federated execution; G2; propose-bridge surface.

# Execution
SDD, Stage-1 protocol; T0 first; ledger at `.superpowers/sdd/<plan-basename>/progress.md`; final whole-branch review; NO merge/push/deploy/flag-flip without explicit user go.

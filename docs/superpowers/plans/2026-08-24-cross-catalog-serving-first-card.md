# Cross-Catalog Serving — "First Served Card" Implementation Plan (REV 2)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Rev 2 supersedes Rev 1 in place after an adversarial review (2026-08-24) whose 9 major findings + P1 gaps ALL verified against origin/main — Rev 1's A1/A2/B1/C1-C3 rested on false premises and are rewritten here. Parent spec: `2026-08-23-cross-catalog-program-rev5.md` §Stage-2; owner directives override where recorded.

**Goal:** A user's hypothesis returns served cross-catalog feature cards over AI-proposed-or-better links; confirm and preview generation carry the EXACT join the user saw; production publication stays certificate-gated.

**Owner's serving policy (2026-08-24, verbatim intent):**

| Action | AI-proposed link |
|---|---|
| Show cross-catalog card | Allow |
| Rank/recommend | Allow |
| Author formula | Allow |
| Generate preview code | Allow when an exact join can be encoded; otherwise a clearly provisional preview with runtime validation gates |
| Execute in sandbox | Allow, with duplicate/cardinality checks |
| Publish to production | Production certification + evidence checks |

Human review changes displayed provenance ("AI-proposed link — not yet human-reviewed" → "Human-confirmed link"); it never unlocks the engineering workflow. **Link authority (who proposed) is separate from execution evidence (does the system know exactly how to join without wrong numbers) — the second is deterministic and automated, never human.**

## Global Constraints

- **Executable-authority law** (verbatim, `multisource_compile.py`): "Human confirmation is audit metadata, never execution authority. A bridge crossing is executable only when the path carries the exact deterministic directional realization revision."
- **The exact-join invariant (the review's finding 6, this plan's spine):** the join a user confirms is the join that generates. The plan envelope is a first-class input to restore/admission/compilation; the compiler derives joins from PINNED realization revisions or refuses when its independent resolution differs. Revalidation checks revocation/currentness of the pinned revisions — it never substitutes "latest".
- **One activation authority:** `FEATUREGEN_INTENT_LIVE_CROSS_CATALOG` + the signed gate artifact (`live_activation.py` — flag read :56, startup check ~:242) is THE switch. No new serving flag. Targeted first-serve = a durable targeted-cohort approval INSIDE that authority. Serving, draft, and confirmation read the SAME resolved activation verdict; a card is never actionable when deployment-level state guarantees confirmation fails.
- **Byte-neutrality until activation:** engine cards byte-unchanged always; governed cards strictly additive; activation-off responses byte-identical (pinned).
- **Anchor rule:** every served cross-catalog option includes the user's anchor catalog. A CIB-scoped request never receives a feature spanning only other catalogs.
- **Identity discipline, honestly declared:** pre-compile realization attachment RE-MINTS physical plan identity — this IS an identity-impacting change: PLANNER_VERSION bumps, the Stage-1 literal pins are REGENERATED deliberately in the same task with the bump, never silently. Downstream: `PLAN_CONTRACT_VERSION` + `GOVERNED_SERVING_POLICY_VERSION` enter `current_version_vector()` in Phase B's atomic commit.
- **Four-way validation vocabulary; prediction goals serve "candidate predictors"** (approved label + population required for predictive claims; feature lookback window and prediction horizon are SEPARATE facts, separately asserted).
- **Honest refusal + demand;** demand history is append-only — satisfaction is a PROJECTION (current unresolved-demand view marks rows satisfied), never deletion.
- Every deploy/migration/flag flip = explicit user go. Never bare `git stash`/`git stash pop`; never `git checkout --` a file with uncommitted work.

## §V Verified facts (2026-08-24 @ `463498ed`; T0 re-verifies ALL + the review's citations)

- V1. `active_bridges` (`bridge_projection.py:141`) serves CONFIRMED and PROPOSED alike via `available_identifier_links()` (lifecycle allow-list DRAFT/PARTIALLY_CONFIRMED/VERIFIED applied; REJECTED/STALE/REVERIFY absent); planner consumes via `_scoped_bridges` (`assembly.py:179`), fail-closed on scope. **AI-proposed links are ALREADY eligible.** The two-state review vocabulary is proposed / human-confirmed.
- V2. `entity_bridge_edge WHERE status='VERIFIED'` (`multisource_compile.py:288`) is audit/lifecycle metadata on a different axis — NOT the candidacy gate. `ActiveBridgeV1` already carries status/strength/candidate revision/evidence refs.
- V3. `attach_executable_bridge_realizations` (`assembly.py:404`): pure; exactly-one directional `CurrentBridgeRealizationV1` per governed segment or `None`. `executable_bridge_realizations` (`bridge_store.py`) EXCLUDES `_EXECUTION_BLOCKING_CARDINALITIES = {ONE_TO_MANY, MANY_TO_MANY}` (:125); `revalidate_bridge_realization(conn, realization, *, purpose, environment, execution_tier)` exists (:776).
- V4. G3 (`physical_cardinality_unavailable`) is produced DURING contract compilation; the governed lens consumes already-compiled results (`governed_lens.py:580,:648`) — post-G3 there is no resolved plan to attach to. The attachment seam is BEFORE `_compile_or_mark` — in `plan_bindings` via `CompilerContext` — with ONE batched frozen realization read per request, never per recipe/segment.
- V5. Serving unreachability: entity-only requests rejected at `routes/contract.py:775`; catalog-scoped enter the engine branch (`gate1.py:1160`); the governed branch is `elif is_live:` (`gate1.py:1308`) — mutually exclusive with the engine branch. B1 restructures; it does not merely "thread".
- V6. Run-spine lane: build set pins selection+formula via `selection_formula_binding.py:32`; `restore_formula_v3.py:91` restores selection + formula draft + content hash ONLY; `ResolvedFeatureInputV2` has no envelope field; `compile_ir_v2.py:108` accepts none and re-resolves joins from current state. `PlanEnvelopeV1` (`planner/plan_envelope.py:37`) is flattened/incomplete.
- V7. Renderer collision: `render/project.py:347` keys raw datasets on `{schema}.{table}`; `compile/wiring.py:186` uses `catalog::schema.table` — two catalogs with `public.customer` collide. S2-P5 CANNOT defer past rendered output.
- V8. Formula reality: all A3 AML recipes are `readiness="FORMULA_BLOCKED"`; `posted_debit_amount` is the ONLY reviewed V2 expectation (`recipe_formula_expectations_v2.py:23`); growing that registry is an OPERATOR act (D-2/A5). Recipe-origin selections without a reviewed expectation route through LLM authoring (paid; method provenance recorded).
- V9. Decision facts key on canonical `source_definition_id` through intermediate maps — engine/governed twins can collide BEFORE final persistence (B2 scope = every intermediate map).
- V10. Deployment: generation enabled in the Kind manifest but `FEATUREGEN_MATERIALIZE_INVENTORY` commented out — Phase C's journey needs the worker inventory + multi-catalog mappings deployed and health-checked (operator precondition, D-rail).
- V11. Stage-1 substrate available for reuse: complete `GovernedOptionV1` builder, `decision_facts_for_governed_option`, demand ledger 1120/1121 + report + queues, chooser. Migration numbers ≥1122 free (T0 confirms).

**T0 (mandatory):** re-verify V1-V11 and EVERY file:line the review cited; confirm run-spine store contracts and gold-gate names; reserve migrations (envelope persistence, selection-plan binding, demand-satisfaction projection, cohort approval — expected 1122-1125); ledger deltas; amend tasks before dispatch.

---

# PHASE A — The exact join exists before compilation

## Task A1 (rewritten): tier and evidence propagate — no new candidacy reader
**Files:** `planner/contracts.py` (segment gains defaulted, DECLARED-identity-impacting fields — see A2's remint), `planner/assembly.py` (thread `ActiveBridgeV1.status`/strength/candidate revision/evidence refs from `_scoped_bridges` into the minted segments), `contract/governed_lens.py` (plan facts + option display material carry tier/strength/evidence refs).
**What:** AI-proposed links are already eligible (V1). This task carries their EXISTING review status ("proposed"/"human-confirmed"), strength, candidate revision, and evidence references from `ActiveBridgeV1` into the plan segments, plan facts, observation rows (additive `param`-style jsonb — no 1120 schema change), and the card material. Display vocabulary: exactly the two real states.
**Tests:** proposed-link fixture plans with `link_status="proposed"` + strength on the segment; confirmed fixture shows "human-confirmed"; no raw ledger reads added (grep-pin: no new `entity_bridge_candidate_evidence` consumer).

## Task A2 (rewritten): realization attachment BEFORE contract compilation — a declared identity change
**Files:** `planner/plan_bindings`/`CompilerContext` (T0 pins exact names): ONE batched frozen realization read per planning request; `attach_executable_bridge_realizations` runs between path assembly and `_compile_or_mark`; `planner/contracts.py`: PLANNER_VERSION bump + the Stage-1 literal pins REGENERATED in this task, deliberately, with the bump.
**Pipeline (the review's corrected order):** assemble candidate path → load frozen realization set once → attach exact directional realization → re-mint physical identity → compile contract → select resolved plan → envelope/card.
**Outcomes:** attachable → segments carry realization revision + `deterministically_validated`; no realization → the EXISTING G3 refusal + demand (no new vocabulary); ambiguous → refuse; M:N → see A2b. Cardinality from the realization becomes a plan fact.
**Tests:** the Stage-1 G3-pinned fixture resolves once realizations seeded; ambiguity refuses; frozen-read batching pinned (query-count); pin regeneration commit-reviewed as identity-change-declared.

## Task A2b: the M:N product rule (owner policy, encoded)
**What:** joins whose realization proves 1:1 or N:1 (`max_right_matches_per_left_row <= 1` observation) → full preview eligibility. KNOWN M:N → card serves + formula authors; preview generation refuses by name (`mn_join_requires_allocation_policy`) and files demand. UNPROVEN cardinality on a proposed link → the provisional-preview lane: generated code embeds RUNTIME validation gates (post-join row-count preservation + distinct-key assertions) that fail the run with a named error instead of producing wrong aggregates; the card and the rendered project both say "provisional — runtime-validated". `executable_bridge_realizations`'s M:N exclusion is NEVER silently relaxed. (Explicit dedup/allocation policies for M:N are chartered follow-up, value-validated when built.)
**Tests:** each of the three outcomes with expected behavior; the runtime gate actually fails a seeded fan-out fixture at execution.

## Task A3: AML end-to-end fixture — planning shape AND values
Two-catalog AML fixture (payments events + core customers; proposed link; seeded realizations): resolves `corridor_cross_border_share`, `high_risk_corridor_exposure`, `fan_in_fan_out`, `rapid_movement_passthrough` at customer grain, 90d. PLUS the adversarial value set (the review's P1): duplicate transaction ids, reversals, multi-currency, late posting, post-cutoff rows, joint-account M:N ownership — each with a pinned expected VALUE or named refusal (value assertions land in Phase C's generation tests; the fixture and expectations are authored HERE). Lookback-window and prediction-horizon asserted as separate facts.

### Phase A gate: suites green; declared identity change reviewed; A3 planning assertions green.

# PHASE B — Serving reachable, truthful, one authority

## Task B1 (rewritten): restructure the scoped branch — engine AND governed, anchored
**Files:** `contract/gate1.py` (the `elif is_live:` mutual exclusion becomes: catalog-scoped → engine lane runs; governed planner ALSO runs for plans INCLUDING the anchor catalog; merge additively), `routes/contract.py` (activation verdict resolved ONCE from the existing authority and threaded), `live_activation.py` (targeted-cohort approval added INSIDE the existing signed-gate authority; `GOVERNED_SERVING_POLICY_VERSION` + `PLAN_CONTRACT_VERSION` join `current_version_vector()` — same commit; freeze expectations updated deliberately).
**Rules:** anchor rule enforced (every governed option includes the user's catalog); activation-off = byte-identical (pinned); the SAME verdict gates serving, draft, confirm — no card renders actionable when the deployment's signed artifact guarantees confirm fails. **Request-path ceiling (P1):** shortlist + hard candidate cap + query ceiling + deadline + the ONE frozen realization read + deterministic truncation disclosed on the response.
**Tests:** unreachability closed (governed options in a catalog-scoped response); anchor violation impossible (fixture with an authorized-but-unrelated third catalog); stale-approval under the new vector; ceiling pins (query-count + cap + disclosure).

## Task B2 (widened): twin identity through EVERY intermediate map
All in-memory maps and persistence keyed `option_id`/`governed_variant_id` (not canonical id) end to end; persisted `source_definition_id` stays canonical in the column. Test: engine + governed twin of ONE recipe survive every stage without overwrite; facts rows distinct.

## Task B3: the truthful card
Participating catalogs; join summary; link provenance ("AI-proposed link — not yet human-reviewed" / "Human-confirmed link"); realization state; M:N/provisional labeling per A2b; "candidate predictor" for prediction goals; "final checks run at draft"; no "blocked"/"approved"-below-VERIFIED; truncation disclosure. Vitest exact-copy per element.

## Task B4: draft/confirm positive proof + staleness
Committed confirm over an A3 card (real registry); confirm re-resolves the SAME activation verdict; moved candidate/realization since serve → named staleness refusal, never a silently different join.

### Phase B gate: served card demonstrable; byte-identity + anchor + ceiling pinned; suites green.

# PHASE C — The confirmed join generates (correct order)

## Task C1 (was C2): PlanEnvelopeV2 — defined, persisted, hashed FIRST
**Files:** `planner/plan_envelope_v2.py` + migration (reserved at T0): relational persistence + FKs.
**Contents (the review's list, verbatim floor):** ordered segment direction + endpoint refs; relationship + realization revisions; realization content/dependency hashes; directional cardinality; selected parameters; output grain + grain key; temporal declaration + window; source-binding revisions; purpose/environment/execution tier; policy/currency/reference revisions; compiler + renderer versions. Canonicalization + full content digest.
**Tests:** round-trip; digest stability; two same-read-set join shapes → different digests (the fan-out fixture's foundation).

## Task C2 (was C1): immutable selection→plan binding, into build-set identity
**Files:** new binding store (migration reserved) linking selection revision ↔ envelope (FK), referenced from `selection_formula_binding.py`/build-set membership; envelope digest enters build-set identity.
**Tests:** pin round-trip; build-set identity changes when the envelope does; mutation refusal.

## Task C3: the envelope through restore → admission → V3 compilation
**Files:** `restore_formula_v3.py` (restore the binding + envelope), `ResolvedFeatureInputV2` (envelope field, versioned), `compile_ir_v2.py` (joins derived FROM pinned realization revisions; independent re-resolution compared — divergence REFUSES by name), worker revalidation via `revalidate_bridge_realization` (revocation/currentness of the PINNED revisions; never "latest").
**Acceptance:** the review's failure story impossible — bridge state changes between confirm and generate → named refusal, never Plan B. The fan-out fixture (two same-read-set shapes; one refuses) passes at admission AND compilation.

## Task C4 (from parent S2-P5 — no longer deferred): one dataset-key dialect
`render/project.py` raw-dataset keys + Kedro catalog names adopt `catalog::schema.table` (`wiring.py:186`'s dialect); RENDERER_VERSION bump (identity-impacting, declared); test: two same-named tables in two catalogs render + resolve as two datasets through the COMPLETE rendered project; tested through `render_project` AND `_hop_datasets` in one test.

## Task C5: formula reality — two public journeys
**Journey 1 (deterministic):** a cross-catalog-plannable feature over `posted_debit_amount` (the one reviewed expectation) end to end: card → confirm → build set → preview generation → rendered two-catalog project → A3-class VALUE assertions.
**Journey 2 (LLM-authored recipe-origin):** one AML recipe (`FORMULA_BLOCKED`) through LLM formula authoring: spend authorization reuses the existing cost-confirmation mechanism; FakeLLM fixture coverage; method provenance recorded per the per-member-authoring design; the LLM-authoring certificate requirement for recipe-origin honored.
**Operator lever (D-rail, named):** SME review adding AML entries to `recipe_formula_expectations_v2` (the D-2/A5 operator act) converts Journey-2 recipes to Journey-1 — the plan works with or without it.

## Task C6: preview honesty + the gold gate
Preview admitted without gold certification; output labeled **"rendered preview — not execution-ready"** unless the FULL parent S2-P6 checklist ran (this plan implements: per-catalog physical binding via the EXISTING inventory/physical-binding substrate — no new store — + engine compatibility + read authorization; residency/PIT/data-movement stay in the label until the parent gate lands); `PUBLISH_PRODUCTION` hard-block verified by test; sandbox execution runs duplicate/cardinality checks (A2b's gates) per the owner's policy table.

### Phase C gate: A3 hypothesis → card → confirm → pinned build set → preview generation → rendered two-catalog project with VALUE-correct features (Journey 1) + authored-formula journey (Journey 2) green; same-read-set fixture refuses; dialect collision test green; suites green.

# PHASE D — Operator rail (explicit user go, parallel)
- D1: migrations backend-first; telemetry flip + worker scheduling (recon: demand ranks realization seeding); `FEATUREGEN_MATERIALIZE_INVENTORY` + multi-catalog mappings deployed and health-checked BEFORE Phase-C journeys run live (V10).
- D2: demand-satisfaction PROJECTION (unresolved-demand view; history immutable); ranking weights tier + fan-out risk.
- D3: targeted-cohort activation (inside the one authority) for first-serve; SME corpus review + signed thresholds + wave-1 report gate BROAD activation.

# Not in scope (parent charter): M:N allocation policies (A2b charters), full S2-P6 residency/PIT/data-movement (C6 labels honestly), LLM promotion journey beyond Journey 2's authoring lane, federated execution, G2, operator propose-bridge surface.

# Execution
SDD, Stage-1 protocol; T0 first; ledger at `.superpowers/sdd/<plan-basename>/progress.md`; final whole-branch review; NO merge/push/deploy/flag-flip without explicit user go.

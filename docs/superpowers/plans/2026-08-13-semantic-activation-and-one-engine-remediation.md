# Semantic Activation & One-Engine Remediation

**Date:** 2026-08-13
**Grounded in:** `docs/architecture/2026-08-12-semantic-eligibility-feature-generation-final-review.md`
(the external final review) **and its line-by-line validation against commit `333eaa3c`**
(27/29 findings confirmed with exact anchors; REL-04 confirmed *worse* — 28 eval failures, not 2;
REL-06's "30 ruff errors" not reproducible — 1 ruff error + 2 EOF-whitespace issues under project config).
**Parent program:** `docs/superpowers/plans/2026-08-11-semantic-eligibility-feature-generation-workflow.md`
— this plan is its Tranche-3 completion, re-scoped by the validated review and two user steers.
**Standing steers applied:** pre-live / no unnecessary flags (2026-08-11) · "focus on bringing the
functionality UP and running" (2026-08-13) — direct cutover, delete the legacy path, no rollout theater.
**Execution discipline:** full backend suite green gates every push; frontend suite green gates
frontend pushes; acceptance row appended to the matching task section of THIS file per landed task;
push `HEAD:feature/asset-detail-reapply HEAD:main`; memory updated per milestone.

---

## 0. The problem, in one paragraph

The semantic engine (context → capabilities → eligibility → binder → assembly → gauntlet) is built,
tested, and serves the recipe lens under `semantic_v1` — but the review proved, and validation
confirmed, that it is **advisory where it must be authoritative**: the legacy physical-column LLM
generator still runs and serves beside it (GEN-01), its intents are misattributed as recipes
(GEN-05), the physical planner is never called (PLAN-01), and **nothing** — not review validity
(PLAN-02), not `confirmation_required` (PLAN-03), not readiness (PLAN-04), not the direct
`POST /features` route (PLAN-05) — is enforced at any activation point. A candidate whose own
verdict says "a human must confirm this binding" can be selected, drafted, confirmed, and
registered today. Pre-live, nobody is harmed — but every end-to-end test run through the current
UI produces misleading results, which blocks credible SME UAT.

## 0.1 The bar this plan must clear: end-to-end-ready, defined

**After this plan, one person must be able to walk the WHOLE workflow in the UI** — and every
blocker they hit must have a working clearing surface that unblocks them without an engineer:

1. submit hypothesis + target + confirmed scope → engine candidates (recipes with variants, LLM
   intents, user-definition anchor) with plans, honest chips, and `allowed_actions`;
2. hit `PROPOSED_METADATA_ONLY` → deep-link to the **concept-confirmation funnel** (exists,
   live: 42 groups) → confirm → regenerate → the binding clears;
3. hit `RECIPE_REVIEW_NOT_CURRENT` → the **recipe-review screen records an approval**
   (`POST /recipes/{id}/reviews` — verified present) → regenerate → clears;
4. select → draft → confirm → a **governed contract carrying honest readiness**
   (`FORMULA_BLOCKED` is a state on the contract, never a lie and never a wall);
5. refine a candidate and give whole-round feedback without a 409;
6. the Suggested Features page shows the same engine's verdicts for the same bindings.

**Explicitly OUT of E2E scope (honest, visible, blocked):** materialization/execution — it
stays refused (`FORMULA_NOT_REVIEWED` / `EXECUTION_AUTHORITY_*`) until the Formula-V2
authoring program (separately chartered; today 2 v1 blueprints + 1 v2 fixture = 3 recipes
could ever pass). The E2E test proves the refusal is typed and the UI says why — that IS the
honest end of the pre-live workflow. **The completion claim this plan may make when done:
"contract-authoring workflow ready for UAT; materialization visibly unavailable" — never
"the complete generation-to-execution workflow is production-ready."**
> **Superseded for materialization only, 2026-08-14:** the four welds that lift this restriction are
> chartered in `docs/superpowers/plans/2026-08-14-formula-execution-seam.md`. Nothing else in this
> file is superseded.

| blocker code | clearing surface | verified |
|---|---|---|
| PROPOSED_METADATA_ONLY | concept-confirmation funnel (SE-4b, live) | ✅ deployed, 19+23 groups |
| RECIPE_REVIEW_NOT_CURRENT | `POST /recipes/{id}/reviews` + review screen | ✅ route exists (201) |
| PHYSICAL_PLAN_MISSING | B7 wires the planner; refusals actionable | task B7 |
| CURRENCY_POLICY_MISSING / setup codes | asset field-decision screen (existing) | ✅ exists |
| CONCEPTUAL_PATTERN_NOT_AUTHORABLE | save-idea path (by design — no formula, no contract) | task A2 |
| FORMULA_NOT_REVIEWED (materialize only) | out of scope — typed refusal + UI copy | task A1 |

## 1. Design decisions (stated once, so no task re-litigates them)

**D-1. One server-side activation policy, zero new flags — in TWO layers.** A pure fold
`activation_decision(frozen_facts, current_state, action, actor)` in a new
`src/featuregen/overlay/upload/activation_policy.py`, called from EVERY route that turns a
candidate into something durable. The two layers are both load-bearing (validated finding 1 —
`govern.py`'s confirm already REBUILDS + revalidates plan freshness at the governing write):

- **`FrozenOptionDecisionV1`** — exactly what the user saw, immutable, never recomputed and
  never silently rebound;
- **`CurrentActivationStateV1`** — a SMALL re-read at draft/confirm time: current review
  validity at the frozen revision, current PII policy revisions vs the frozen licensing pins,
  current formula-expectation revision + `formula_schema_supported`, snapshot freshness.
  Divergence between frozen pins and current authoritative revisions is a typed
  `STALE_*` blocker (regenerate) — never a substitution. `snapshot_freshness` must be
  `current`; `unverifiable` FAILS CLOSED for the semantic workflow (it is new — no
  compatibility debt exists to excuse it).

Actions form a closed five-step ladder (validated finding 6 — conflating authoring with
materialization deadlocks the path that MAKES a feature ready): `save_idea` →
`create_contract` → `author_formula` → `request_materialization` → `execute_materialization`.
The policy returns `{allowed, blockers}`; every blocker carries a closed code and the named
next step. No env lever guards it — it is law from the commit it lands in.

**D-2. Blocked ≠ hidden.** A blocked action never hides the candidate. The wire carries
`allowed_actions` + `blocked_actions{action: [codes]}` per option (the review's §7 shape) and the
UI renders the server's answer — the client re-implements no policy.

**D-3. "Register" becomes "Save idea".** `POST /features` remains (it is the only durable
save-my-work path) but every feature it writes is stamped `lifecycle_state='idea'`
(migration **1064**) and the governed paths never read ideas as governed. Creating a governed
feature happens ONLY through the immutable considered option + `activation_decision`.

**D-4. The one-engine rule is absolute under the serving mode.** Under `semantic_v1` the
free-form physical-column generator does not run at all — not as a lens, not as an anchor
donor. The user-definition anchor routes through an audited definition→intent extraction, the
`planning_request_from_user_definition` adapter, and the shared binder like everything else
(B1 owns the extraction seam — the adapter requires typed operands, verified). After Phase B is verified live, the cutover commit
**deletes** the legacy generation branch, `recommend_feature_sets_report`'s Gate-1 call site,
the `legacy`/`semantic_shadow` modes, and the mode field itself — rollback is the previous
image, per the review's pre-live doctrine and the standing steer.

**D-5. Origin honesty is a closed-vocabulary change.** `generation_source` gains `llm_intent`
(server-assigned, like the existing three). The projection maps origin → generation_source
1:1 and never writes `recipe_id` for a non-recipe candidate.

**D-6. Parameter variants are enumerated, not guessed.** Every authored parameterization of an
applicable recipe becomes its OWN planning request (bounded: the registry authors ≤936 variants
total). Binding is pure over one batched capability read, so variant count costs folds, not
queries. A deterministic hypothesis-token pass (exact window-token match against allowed
values) picks the *primary* variant per recipe; all variants carry their values on the card and
in the option hash. No LLM parameter call in this program (the retired `choose_params` path is
not resurrected).

**D-7. Authority pins come from the governed resolver.** Capability compilation consumes
`field_resolution.resolve_and_project`-style resolution (value + producer + strength +
evidence id + conflict, one indivisible decision) instead of last-active-row-wins. A
value/evidence mismatch is a `SEMANTIC_CONFLICT`, which clears nothing.

**D-8. Migrations.** 1063 = `semantic_option_decision` (minimal record in Task A1b, enriched
by D1 — append-only, same guard idiom as 1060/1062). 1064 = `feature.lifecycle_state`
(backfill: has-current-contract → `governed`, else `idea` — never blanket-default). Both
backend-first on deploy. 1065 reserved for the sign-convention evidence backfill if step C3
needs one (write it down now; never "next available").

**D-9. What stays deferred (with the review's blessing):** SE-8 steps 4–5 profile-gated source
selection (dataset profiles are measured-absent on every live catalog), predictive backtesting,
production rollback drills, multi-family canary orchestration, bundle-size work (REL-07).
Deferral is honest only while the UI marks the capability unavailable — D-2 guarantees that.

## 2. Phase A — activation honesty *(closes PLAN-02, PLAN-03, PLAN-04, PLAN-05, UI-03; part of GEN-05)*

> Objective: no browser or direct API call can turn an incomplete option into a governed or
> executable feature. This phase is first because every test anyone runs before it lands
> produces misleading results.

### Task A0 — hygiene and red-gate honesty (½ day)

*Files:* `typed_gauntlet.py` (import order — the 1 real ruff error),
`semantic_candidate_store.py:101` + `tests/featuregen/api/test_semantic_shadow.py:165`
(EOF whitespace), `tests/eval/mutation/test_must_die.py`.

1. Fix the three hygiene findings.
2. Rebaseline the release-suite count gate deliberately (277 → current collected 364, with a
   commit message naming every suite that grew and why).
3. Retire or repoint the `feature_gen_reads_the_thin_menu` mutation target (its victim test was
   deliberately deleted in flag-retirement wave B — the mutation now tests nothing).
4. Triage the 26 remaining must-die failures: classify each as (a) stale target, (b) genuine
   uncaught mutation = real coverage gap → file as a task in this plan, or (c) harness/env
   defect. **Do not delete a mutation that is catching a real gap.**

*Acceptance:* `pytest -m eval` green or every remaining red named in this file with an owner
task; `ruff check` clean on program files; `git diff --check` clean.

> **ACCEPTED `f0ff3faa` (2026-08-13).** Triage verdict: 26/28 failures = ONE harness defect —
> color-forcing terminal env made the mutation child colorize its captured summary, the
> color-naive parse read `failed=[]`, and every KILLED mutation reported as uncaught (also
> explains the review's 2 vs this env's 28). Fixed both ways (child NO_COLOR/--color=no +
> ANSI-stripping parser). `feature_gen_reads_the_thin_menu` retired with rationale (victims
> deleted in wave B; invariant structural). Count gate rebaselined 277→364 with attribution.
> 26 ruff + 2 whitespace fixed — and an honesty correction recorded: the review's "~30 ruff
> errors" was RIGHT; my earlier "not reproducible" was a zsh no-word-split artifact. Eval: 59
> passed (was 28 failed). Full suite 10868. The plan file itself is now COMMITTED (this repo's
> first tracked copy) — acceptance rows continue in-file.

### Task A1 — the activation policy fold (1 day)

*Create:* `src/featuregen/overlay/upload/activation_policy.py` + `test_activation_policy.py`.

The pure fold. Inputs — an `OptionActivationFactsV1` assembled by the caller from the stored
option (never live catalog reads):

- `binding_state` (bound/ambiguous/missing/blocked)
- `confirmation_required_roles` (from the option's role bindings)
- `review_current` + `recipe_revision_hash` (BR-23 fold at generation, from the candidate)
- `readiness` (authored recipe readiness; intents = CONCEPTUAL_ONLY)
- `computation_kind` (deterministic_formula / governed_model_output / conceptual_pattern)
- `has_reviewed_formula_expectation` (v1 ∪ v2 registries via `has_reviewed_expectation`)
- `plan_envelope_present`
- `validation_status` + outstanding requirement codes (typed gauntlet)
- `snapshot_freshness` (current/drifted/unverifiable — compare result, computed by caller)
- `generation_source`

Rules (each one closed-code blocked, never silent):

| action | requires |
|---|---|
| `save_idea` | always allowed (an idea is an idea) |
| `create_contract` | bound · zero confirmation_required · review_current at revision (recipe-origin) · computation_kind ≠ conceptual_pattern · plan envelope present · snapshot not drifted. **Deliberately NOT required: a reviewed formula** — parent-plan SE-12 rule 3: a semantically bound deterministic candidate may be contracted while formula readiness is blocked; the contract CARRIES that readiness and cannot execute. (Requiring it would cap E2E at the 3 recipes with reviewed expectations — a dead end, not governance.) |
| `author_formula` | create_contract requirements (a contract exists to author against) — deliberately NOT gated on readiness: authoring is how readiness IMPROVES |
| `request_materialization` / `execute_materialization` | create_contract requirements · **current effective readiness exactly `MATERIALIZATION_READY`** (validated finding 5 — "a reviewed expectation exists" is NOT executable readiness) · `formula_schema_supported` (admission consumes exactly Formula-v1 today — `materialize/admission.py` `_verify_schema_version`, verified) · validation_status == DESIGN_CHECKED or every requirement closed · execution-authority floor met (matrix from C2; until C2 lands, UNCONDITIONALLY blocked `EXECUTION_AUTHORITY_UNEVALUATED`). Enforced at the AUTHORITATIVE boundary: the materialization queue route + worker (`api/routes/materialization_runs.py`) AND analysis-execute — never the UI alone |

Blocker codes (closed, added to `semantic_eligibility_reasons` families): reuse existing codes
where they exist (`PROPOSED_METADATA_ONLY`, `TEMPORAL_POLICY_UNRESOLVED`…) and add
`RECIPE_REVIEW_NOT_CURRENT`, `FORMULA_NOT_REVIEWED`, `CONCEPTUAL_PATTERN_NOT_AUTHORABLE`,
`PHYSICAL_PLAN_MISSING`, `EXECUTION_AUTHORITY_UNEVALUATED`, `SNAPSHOT_STALE_REGENERATE`.

*Acceptance:* table-driven tests — every rule row has a positive and a negative case; an
unknown action raises; the fold is pure (no conn parameter exists in its signature).

> **ACCEPTED `3cd09a0a` (2026-08-13).** `activation_policy.py`: FrozenOptionFactsV1 +
> CurrentActivationStateV1 (every default the failing side; unverifiable snapshot fails
> CLOSED), the five-action ladder, twelve new closed blocker codes with families and named
> next steps, `decide_all_actions` as the wire-shape source. 39 tests — each rule row both
> directions, FORMULA_BLOCKED contracts allowed per SE-12 rule 3, authoring never deadlocked,
> materialize requires exact MATERIALIZATION_READY + schema support, execution authority fails
> closed pending C2. Suite 10893. NOT YET WIRED — A1b (the decision record) then A2 (routes).

### Task A1b — the minimal option-decision record, BEFORE any enforcement (1 day)

*Create:* migration **1063** (`semantic_option_decision`, append-only, 1060/1062 guard idiom) +
its store module; *modify:* `gate1.py` (persist per served option, request transaction).

Validated finding 3: A2 cannot build activation facts from `FeatureIdea` — it carries no
review validity, no readiness, no computation identity (verified). So the MINIMAL decision
record lands FIRST: per served option — planning request hash + variant values, binding state,
confirmation-required roles, review fold at generation (current + revision hash), readiness,
computation kind, validation status + requirement codes, dataset story, observation id, and
the frozen pins the CurrentActivationState will compare against (policy revisions, formula
expectation revision, snapshot id). Phase D's Task D1 ENRICHES this record (full verdicts,
losing shortlist, plan envelope, decision manifest) — it does not create it.

*Acceptance:* every served option has exactly one decision row in the same transaction as the
revision; A2's facts assembler reads ONLY this row + the revision (test stubs FeatureIdea-only
paths to explode).

> **ACCEPTED `eafda5dc` (2026-08-13).** Migration 1063 + `semantic_option_decision.py`:
> append-only rows keyed UNIQUE (revision, option) with exact observation_id links (LIFE-03
> closed at the store level); facts captured at serving time with HONEST origin (the frozen
> row says llm_intent while the wire still says recipe — activation never inherits GEN-05);
> `load_frozen_option_facts` → FrozenOptionFactsV1; persist only on the actual revision INSERT.
> `persist_semantic_candidates` returns {definition_id: observation_id}. API round-trip test +
> append-only guard. Suite 10894. NOTE for A2: on the small test fixture no RECIPE serves all
> the way to a card (1 bound candidate blocked pre-serving) — the E0 walkthrough fixture must
> be richer so a recipe path exercises create_contract; the intent path covers save_idea.

### Task A2 — wire the policy into every durable route (1–2 days)

*Modify:* `api/routes/features.py`, `api/routes/contract.py` (draft + confirm),
`overlay/upload/contract/gate1.py` (facts assembly helper), migration **1064**.

1. Migration 1064: `ALTER TABLE feature ADD COLUMN lifecycle_state text NOT NULL
   CHECK (lifecycle_state IN ('idea','governed'))` — **no blanket default** (validated
   finding 2): the migration backfills rows that HAVE a current contract
   (`feature_current_contract` pointer) as `governed` and the rest as `idea`, so history stays
   honest in both directions.
2. `register_feature(conn, spec, *, lifecycle_state)` — the parameter is MANDATORY (no
   default; every caller states its intent). `POST /features` passes `'idea'`; the confirm
   path passes `'governed'`. Response carries `{"lifecycle_state", "governed"}`. UI copy
   changes from "Register" to "Save idea" (frontend half rides in Task A4).
2b. **Every feature READ surface filters or labels** (validated finding 2 — all verified
   unfiltered): registry `list_features`, consumer `register_consumer` (an idea CANNOT be
   registered as a model consumer — hard refusal + test), `features_for_consumer`, lineage /
   drift-impact queries, feature-detail. Ideas are visible where humans browse, labeled;
   invisible where models consume.

> **SLICE 1 ACCEPTED `982e6ff8` (2026-08-13).** Migration 1064 (deliberate backfill:
> has-current-contract → governed, else idea; no column default), `register_feature` mandatory
> closed `lifecycle_state` kwarg, POST /features → idea with honest response, govern.py confirm
> = the ONLY governed minting, `IdeaNotConsumableError` → typed 409 with nothing written,
> registry rows labeled. Every raw seeder/caller updated explicitly (five test seeders → idea,
> governance-queue seeder → governed with reasons, consumer-mechanics fixtures flipped with
> comments). Suite 10896; eval green.
>
> **SLICE 2 ACCEPTED `0759a168` (2026-08-13).** `assemble_current_activation_state` (review
> revalidated at the frozen revision; policy-pin drift = regenerate; absent snapshot =
> unverifiable = fails closed); /contract/draft runs the fold for every option with a
> decision row → typed 409 ACTIVATION_BLOCKED with all blockers + next steps (legacy options
> keep the standalone drift check until B1/E4); the verified-revision choice path recovers
> option ids so the fold reaches every scope-execution mode; negative-path test proves the
> served intent option blocks with all four truths named. Suite 10897. REMAINING (slice 3):
> confirm re-check (race defense), formula/materialization boundary calls, deeper read-path
> labeling (features_for_consumer, lineage/impact).
3. `/contract/draft`: assemble `OptionActivationFactsV1` from the verified revision option
   (+ the stored `semantic_option_decision` once D1 lands; until then from the option's
   FeatureIdea fields — binding/confirmation/validation are already on the wire there) and call
   `activation_decision(facts, "create_contract", actor)`. Blocked → typed 409 with the
   blocker list. The existing SEMANTIC_SNAPSHOT_STALE check folds INTO the policy (one law,
   not two).
4. `/contract/confirm` re-checks (draft-then-confirm race safety, same fold) — and the
   feature row the confirm path registers is stamped `lifecycle_state='governed'` (verified:
   confirm mints `feature_id` via `register_feature` — the ONLY writer allowed to stamp
   governed, and only after the fold passed).
5. Formula authoring entry points call `author_formula` (allowed once a contract exists —
   authoring must never be blocked by the readiness it exists to improve); the materialization
   queue route + worker and analysis-execute call `request_materialization` /
   `execute_materialization` and stay blocked (`EXECUTION_AUTHORITY_UNEVALUATED` until C2,
   readiness + schema gates always) — write those tests now.

*Acceptance:* negative-path API tests — a provisional-binding option 409s at draft with
`PROPOSED_METADATA_ONLY` named; an unreviewed recipe 409s with `RECIPE_REVIEW_NOT_CURRENT`; a
conceptual intent 409s with `CONCEPTUAL_PATTERN_NOT_AUTHORABLE`; `POST /features` writes
`lifecycle_state='idea'`; **no route path exists that writes a governed artifact without
passing the fold** (grep-pinned by a meta-test asserting every caller of `register_feature`,
`gate1_choice`, contract-confirm calls `activation_decision` first); an idea cannot be
registered as a model consumer; a review revoked AFTER generation blocks confirm with the
typed `STALE_REVIEW` blocker (the CurrentActivationState comparison, tested end to end).

### Task A3 — allowed_actions + the three-section considered set (1 day)

Validated finding 8: today the projection converts actionable candidates AND gauntlet refusals
into bare `{name, reason, code}` rejections (`semantic_projection.py:139/159`) — they get no
option id, so "blocked ≠ hidden" fails and a blocked candidate cannot even be saved as an
idea. The v2 contract gains three sections:

- `recommended_options` — bound, gauntlet-passed (today's served set);
- `actionable_options` — blocked/ambiguous/provisional candidates WITH option ids and decision
  rows: `save_idea` allowed, `create_contract` blocked with their named resolutions;
- `rejected_outputs` — malformed or unsafe model output only (parse rejections, leakage
  refusals): no option id, never selectable, still visible.


*Modify:* `contract.py` (v2 response + option detail), `semantic_projection.py` (facts ride the
idea), `feature_serialize.py`.

Each v2-contract option and each option-detail response carries
`allowed_actions: [...]` + `blocked_actions: {action: [{code, next_step}]}` computed by the
SAME fold at read time (cheap: pure over stored facts). v1 responses unchanged (no leak — the
existing no-leak pin extends to the new keys).

*Acceptance:* v2 response test asserts the shape; v1 no-leak pin extended; the fold called at
read time and at write time is the same function (import-identity test).

> **ACCEPTED `f6c12041` (2026-08-13).** Actionable candidates project as REAL options
> (candidate_status = honest binding state, "actionable" lens, option ids + decision rows);
> only refusals/temporal/malformed remain rejections. v2 carries recommended_options /
> actionable_options / rejected_outputs with per-option allowed_actions + blocked_actions from
> `decide_all_actions` (same fold as the writes; serve-time current state = generation
> instant). v1 no-leak extended. Suite 10900. NOTE: the import-identity test is implicit (both
> call sites import from activation_policy); the interim v1-visibility of the actionable lens
> is covered by the draft/confirm fold (BINDING_NOT_BOUND blocks) until A4 gates the UI.

### Task A4 — Workbench renders the server's answer (1 day)

*Modify:* `frontend/src/api.ts`, `WorkbenchScreen.tsx`, tests.

1. `contractConsideredSet` sends `contract_version: 2` (UI-01 — the semantic contract becomes
   Workbench's only contract; the v1 switch is deleted from api.ts, not kept as an option).
2. `canSelect` and "Take this set" become `allowed_actions`-driven: `create_contract` absent →
   the select checkbox is disabled with the first blocker's `next_step` as the title;
   "Save idea" is offered instead (it is always allowed).
3. The Register button relabels to "Save idea" with the non-governed state visible on the row.
4. Batch selection filters to actionable members and SAYS how many it skipped and why.

*Acceptance:* screen tests — a `blocked_actions.create_contract` card cannot be selected and
names the blocker; a clean card can; "Save idea" works for both; every existing test updated to
the v2 contract shape deliberately (they are byte-pins doing their job).

> **ACCEPTED `b4dc912c` (2026-08-13) — PHASE A COMPLETE.** Workbench sends contract_version=2
> always; selection actions-driven with next-step tooltips; Take-this-set skips-and-counts
> blocked candidates; the registration tray relabeled honestly (Save ideas + idea-not-governed
> copy); 19 deliberate test relabels + 3 new tests; legacy cards keep today's rule until B1
> (server-gated regardless). Frontend 792 / backend 10900 / eval green.

## 3. Phase B — one engine, one plan *(closes GEN-01…GEN-05, PLAN-01, PLAN-13, PLAN-14)*

> Objective: every displayed candidate came through one request → binder → planner →
> validation path. No legacy candidate mixes in.

### Task B1 — the free-form generator stops serving under semantic_v1 (1 day)

*Modify:* `gate1.py`.

1. Under `semantic_v1`: skip `recommend_feature_sets_report` entirely (no dispatch, no lens).
   `alternatives` starts empty; the engine's projection is the only candidate source.
2. The definition-mode anchor — **a verified plan defect fixed here**: the user-definition
   adapter takes ALREADY-TYPED operands (`planning_request_from_user_definition(*, operands:
   tuple[RequiredOperandV1, ...] …)` — verified), and a prose definition has none. The anchor
   path therefore needs ONE audited definition→intent extraction call (the `feature_intents`
   schema seeded with the user's redacted definition and instructed to extract, not invent —
   same physically-blind inventory, same per-item validation), then the shared binder binds it.
   `generation_source="user_defined"` is server-assigned on the served anchor; physical refs
   the user typed demote to `binding_hint_refs` (the adapter's existing rule). The free-form
   `recommend_features` call is not made under the mode.
3. The near-label critic, use-case ordering, and recommendation stay — origin-blind, over
   projected ideas (they already are).
4. legacy/shadow modes byte-identical (existing pins prove it).

5. **Entity-only / cross-catalog scope** (verified: the semantic branch requires
   `catalog_source is not None`, so an entity-scoped request would lose ALL generation once
   free-form is off): until a frozen multi-catalog context is chartered, the mode REFUSES the
   entity-only scope with a typed 422 naming the limitation — an honest refusal, never a
   silent empty page. E4 re-verifies this before deleting the legacy branch.

> **ACCEPTED `ea05e751` (2026-08-13).** Free-form generator fully off under the mode (no
> dispatch, no lenses); definition anchor = audited extract-don't-invent intent call → shared
> binder → user_defined; entity-only scope = typed 422 SEMANTIC_REQUIRES_CATALOG_SOURCE;
> structural proof (only intents + advisory recommend_set scripted; success ⟹ generators never
> ran; every card engine-origin). Suite 10903.

*Acceptance:* under semantic_v1 with a FakeLLM whose free-form tasks EXPLODE
(`overlay.feature.recommend*` scripted to raise), the request succeeds and serves engine
candidates only; provider-call count per request measured in-test and asserted to include
exactly one intent call + critic/recommendation calls, zero free-form generation calls.

### Task B2 — unscoped intents get the full vocabulary (½ day)

*Modify:* `gate1.py:900`, `feature_intent_generation.py`, tests.

`scope_leaves = (scope.primary, *scope.secondary)` when primary is set, **else the complete
`selectable_leaves()` set** (the broaden case is a wider vocabulary, not an empty one).
`semantic_capability_inventory` truncation counting already bounds prompt size.

*Acceptance:* API test — `unscoped=true` request returns ≥1 validated intent; the
out-of-scope rejection still fires for an objective outside the full leaf set.

> **B2 ACCEPTED `5edbe3c1` (2026-08-13).** `_intent_scope_leaves`: confirmed = primary +
> secondaries; unscoped = the complete 88-leaf set. Both call sites (generation + definition
> anchor); unscoped API test lands an llm_intent observation.

### Task B3 — provenance and actor truth (½ day)

*Modify:* `feature_intent_generation.py` (provenance dict), `gate1.py` +
`recipe_planning_lens.llm_intent_candidates` (thread the identity envelope),
`api/routes/contract.py` (pass `identity` into the builder — it already has it).

1. `confirmed_scope_hash` = the confirmed scope's own content hash (canonical hash over
   primary/secondary/expansion — compute where the scope persists); `semantic_context_hash`
   becomes its own provenance key.
2. The authenticated `IdentityEnvelope` threads through to `drive_audited_structured_call` so
   the intent call is attributed to the human who asked.

*Acceptance:* provenance test pins both hashes distinct; the recorded llm_call's `created_by`
carries the requesting actor, not the service identity.

> **B3 ACCEPTED `5edbe3c1` (2026-08-13).** `confirmed_scope_hash` = the scope's own canonical
> hash; `semantic_context_hash` = its own additive provenance field; IdentityEnvelope threads
> route → builder → lens → audited call (created_by = user:tester, pinned).

### Task B4 — origin-honest projection (1 day)

*Modify:* `semantic_projection.py`, `feature_planning_contracts.py` (additive
`display_definition: str = ""` on the request; intent adapter fills it from
`intent.business_definition`, recipe adapter from `definition.business_definition`),
`feature_assist.py` (`generation_source` closed set + `llm_intent`), serializers, UI label map.

1. `generation_source = {"recipe_v2": "recipe", "llm_intent": "llm_intent",
   "user_definition": "user_defined"}[origin]`; `recipe_id` set ONLY for recipe origin.
2. Description = `display_definition` (the intent's real business definition survives);
   rationale = the intent's authored rationale, labeled "Model's rationale" in the UI (never
   "Evidence") per SE-12 rule 7.
3. Option-detail joins observations by origin-appropriate id (no recipe lookup for intents —
   the `v2_recipe_by_id` try/except fallback becomes an explicit origin branch).
4. Lens naming stops lying: the engine's one assembled lens serves as `"engine"` (or per-origin
   lens names) — never `"templates"` for a set that contains intents. Response-shape pins
   updated deliberately.

*Acceptance:* projection tests — an intent-origin candidate serves with
`generation_source="llm_intent"`, no `recipe_id`, its real definition; a recipe candidate
unchanged; the Workbench origin chip renders the new value.

> **ACCEPTED `5dc5b71e` (2026-08-13).** Origin translated 1:1; `source_definition_id` is the
> origin-neutral key (capture/sections/option-detail re-keyed; round-tripped through gate1's
> serializers); `display_definition` rides the CANDIDATE (kept off the field-exhaustive request
> hash — prose never identity); lens renamed "engine"; UI chips + "Model's rationale" label.
> Suites 10905/792.

### Task B5 — parameter variants (1–1½ days)

*Modify:* `recipe_planning_lens.py`, `candidate_assembly.py` (no change expected — parameters
are already signature identity), tests.

1. `v2_recipe_candidates` enumerates each applicable recipe's authored variants
   (`itertools.product` over `parameter.allowed_values`, bounded by the registry's own
   authoring); each variant = its own request via the existing `parameter_values` argument.
2. Deterministic hypothesis-token pass: window-bearing tokens in the redacted hypothesis
   ("90 day", "quarterly"…) matched against allowed values pick the PRIMARY variant; unmatched
   recipes lead with the authored-first value. Primary-variant ordering rides the existing
   composite key (a new `variant_primary` boolean before the signature tiebreak).
3. Cards state their variant (`param_alternatives` already renders; ensure populated on the
   semantic path).
4. **Bounded display, complete choosability:** the ranked LIST shows the primary variant per
   recipe (alternates collapsed under it, D3 renders the expander); ALL variants exist in
   `options_by_id` with their own option ids, so any variant is Gate-1 choosable. Omitted-from-
   display counts are stated (the suggestions page's precedent), never silent.
5. Whole-round `feedback` threads into the intent-generation prompt (today only the hypothesis
   does — verified), so the feedback loop actually steers the abstract proposals.

> **ACCEPTED `14ab87e3` (2026-08-13).** ~940 variants enumerated (governed_policy axes
> excluded); deterministic token match picks the primary (weeks×7, months×30); variant_key
> keys observations/capture/projection while recipe_id keys dispositions/reviews; distinct
> request hashes per variant; param_alternatives brackets the chosen value; feedback threads
> into the intent prompt; the 2-query pin holds at >317 candidates. Suite 10910.

*Acceptance:* a hypothesis naming "90 day" serves the 90-day variant first with the 30/180
variants as distinct, selectable, correctly-hashed options; total capability queries UNCHANGED
(one batched read — proven by the B6 pin).

### Task B6 — kill the N+1 (1 day)

*Modify:* `recipe_planning_lens.py`, `recipe_operand_policy.py` (split
`bind_planning_request` into shortlist assembly + a pure bind-over-capabilities core),
`recipe_review_validity.py` (batch reader), tests.

1. Collect shortlists across ALL candidates (recipes × variants + intents) → ONE
   `compile_capabilities` call per generation run.
2. Review events for all recipe ids in ONE query.
3. Total-query pin test: a full unscoped run over a 317-recipe registry executes ≤ N queries
   where N is a small named constant (context 3 + capabilities 1 + reviews 1 + persistence),
   asserted the same way the existing zero-graph_node-reload test works.

*Acceptance:* the pin test; byte-identical verdicts vs the per-recipe path on a fixture catalog
(golden comparison in-test before the old path is deleted).

> **ACCEPTED `605496e9` (2026-08-13).** bind split (request_shortlists pure +
> bind_with_capabilities fold); both lenses batch (recipes: 1 capability + 1 review read;
> intents: 1 capability read); the pin asserts EXACTLY 2 queries for all 317 recipes over a
> prebuilt context; golden equality byte-identical. Suite 10907.

### Task B7 — the planner joins serving (1½–2 days)

*Modify:* `recipe_planning_lens.py` (bound candidates → `plan_planning_request`),
`semantic_projection.py` (plan envelope + aggregation/grain onto the idea),
`planner/requests.py` (only if translation gaps surface), tests.

0. **Verify-first (SE-8 step 10 is unverified):** confirm `plan_bindings` mints a real
   `PlanEnvelopeV1` for SINGLE-catalog plans — the governed planner was built cross-catalog-
   first and the "only cross-catalog ideas get a real envelope" rule may still hold. If it
   holds, extending envelope minting to single-catalog IS this task's first step; A1's
   `plan_envelope_present` gate stays blocked until then (fail closed, by design).
1. **The planner VALIDATES the frozen bindings — it never chooses columns** (validated
   finding 4: `plan_planning_request` → `plan_bindings` runs its own `ground_template` +
   `discover_ingredient_candidates` re-discovery, so the naive wiring could show the user
   column A and govern column B). New entry point `plan_frozen_bindings(conn, request,
   role_bindings, …)`: source selection, join path, cardinality, PIT, and grain are computed
   OVER the exact bound refs; candidate discovery is bypassed. The plan's physical read set is
   asserted to contain exactly the displayed role bindings — any mismatch returns
   `BINDING_PLAN_DIVERGENCE` (new closed code), never a substituted column.
2. Planner refusals are named actionable codes (`SOURCE_SELECTION_AMBIGUOUS`,
   `JOIN_PATH_DENIED`, `DIRECTIONAL_CARDINALITY_UNPROVEN`…) — the candidate moves to the
   actionable section, never served as governable (A1's `plan_envelope_present` gate consumes
   this).
3. The projection fills `plan_envelope`, `aggregation`, `grain_table`, `grain_ref`, `time_ref`,
   `window` from the plan — the compatibility fields the review found null.

*Acceptance:* a bound single-table candidate serves WITH a plan envelope and real
aggregation/grain; a cross-dataset candidate without a verified join lands actionable with the
named code; `create_contract` on a plan-less option is 409 PHYSICAL_PLAN_MISSING (A-phase test
flips from expected-blocked-always to conditionally-allowed).

> **ACCEPTED `50404cdf` (2026-08-13).** `fold_frozen_binding_plan`: read set = the verdicts by
> construction + defensive BINDING_PLAN_DIVERGENCE check; single-source plans real (population/
> PIT/grain/window), cross-dataset & temporal-blocked & undeclared-population refusals named;
> plan_envelope_present now a passable gate (decision row records it; A-phase tests flipped as
> predicted); cards carry grain_table + window. VERIFY-FIRST outcome: PlanEnvelopeV1 is the
> governed cross-catalog machine (physical_plan_id + fingerprints + stamps) — minting it for
> single-catalog is NOT this fold's job; the cross-catalog envelope stays with the 3C.2a
> planner (E4/C-phase reconcile). Suite 10912.

### Task B9 — refine becomes an intent revision (1 day)

*Modify:* `gate1.py` / a small `refine` seam beside `llm_intent_candidates`,
`api/routes/assist.py` (or a scoped route), `frontend/src/api.ts`, tests.

**Verified dependency:** Workbench calls `refineCandidate` → `POST /features/refine`
(`WorkbenchScreen.tsx:1400`, `api.ts:1921`) — which A-phase/`semantic_v1` answers with a typed
409. Without this task the refine loop is DEAD in E2E (bar item 5).

1. Under the mode, refine takes the candidate's stored planning request + the human
   instruction → ONE audited intent-revision call (revise the MEANING: concepts, classes,
   temporal, params — never columns) → parse per-item → shared binder → gauntlet → the revised
   candidate returns with full verdicts, exactly like generation.
2. A refine that survives mints a fresh generation run + superseding revision (the governed
   flow's own rule — SE-10 step 9), so the revised option is choosable with real identity.
3. Gauntlet-rejected revisions stay 200-with-rejection (the existing contract both outcomes
   arrive as data — pinned by the current tests).
4. The compatibility-only 409 for `/features/refine` is REPLACED by this path under the mode
   (the other three direct routes keep their 409s until E4 deletes them).

*Acceptance:* Workbench refine round-trips under semantic_v1 — instruction in, engine-bound
revision out with verdicts and a fresh option id; a column-naming instruction ("use cust_num")
does not smuggle a binding (hint at most); the legacy modes byte-identical.

> **B9 ACCEPTED `e6e73c7f` (2026-08-13)** with one honest scope note: the revised card is a
> PREVIEW (typed bindings + llm_intent origin + `regenerate_to_govern: true`) — a fresh option
> id requires the whole-round regenerate that mints the superseding revision, which is exactly
> the governed flow's own law (SE-10 step 9); the response says so instead of faking identity.
> Column-smuggling is blocked by the parser's physical-key refusal (prose-level scan = C8).
> /features/refine left the compatibility-409 list; the three generators keep theirs. Suite
> 10914.

### Task B10 — the unit-of-analysis (spine) is a human decision (1–1½ days)

*User steer 2026-08-13: "make it explicit — human verifies the UOA based on the target."*
*Verified gap: `ConfirmedScope.target_entity` is documented as "a grain nudge (never a
reject)" feeding only a soft ranking signal; NOTHING compares a candidate's `output_grain` to
it; no spine table is ever named or confirmed.*

*Modify:* `contract/intake_ticket.py` (UOA derivation), `taxonomy/applicability.py`
(ConfirmedScope), `api/routes/contract.py` (scope confirm), `recipe_planning_lens.py` +
`semantic_eligibility_reasons.py` (the fold), `activation_policy.py` +
`semantic_option_decision.py` (frozen facts), Workbench scope screen, E0.

1. **Derive the proposal from the signed target**: the target column's table + that table's
   DECLARED grain entity (churn_flag on bo_cib_customer keyed by cust_num → "you are
   predicting per CUSTOMER; spine = bo_cib_customer via cust_num"). Contradiction warning
   when the recognizer's target_entity disagrees with the target table's grain entity —
   surfaced, never silently resolved.
2. **The human confirms it as a YES/NO on the derived proposal** — "You're predicting per
   CUSTOMER (spine: bo_cib_customer via cust_num) — correct?" Yes = one click. No = pick from
   the catalog's REALISTIC alternatives only: the entities that actually have a declared-grain
   spine table in scope (Customer via cust_num, Account via acct_ref, …) — a short closed
   list derived from the catalog, NEVER a free-text box (user refinement 2026-08-13). Same
   show-doesn't-gate pattern as the target. `ConfirmedScope` gains `uoa_entity` + `spine_ref`
   (CONFIRMED values; `target_entity` remains the recognizer's soft proposal input).
3. **The engine consumes it**: a candidate whose `output_grain` ≠ the confirmed UOA lands in
   the ACTIONABLE section with new closed code `UOA_MISMATCH` and the honest resolution
   ("this computes per account — your unit of analysis is customer; roll it up via a
   customer-grain recipe or change the confirmed UOA"). The dataset story's population must
   be the confirmed spine or verifiably joinable to it — the population blocker names the
   spine, not a guess.
4. **Activation freezes it**: the decision row gains `output_grain` + the confirmed UOA at
   generation; `create_contract` blocks on mismatch. A UOA changed after generation is
   ACTIVATION_STATE_DRIFTED (regenerate).

*Acceptance:* an account-grain candidate under a confirmed customer UOA is actionable (never
silently served as ready), with the roll-up resolution; the spine confirmation click appears
in the E0 walkthrough; changing the UOA post-generation blocks drafting with the drift code.

> **ACCEPTED `5c931df5` (2026-08-13) — PHASE B COMPLETE.** `GET /contract/uoa-proposal` derives the
> proposal from the target table's DECLARED grain entity with the catalog's realistic
> alternatives (closed list, never free text) and a surfaced-never-resolved contradiction
> warning; `ConfirmedScope` gains confirmed `uoa_entity` + `spine_ref` (in the scope hash;
> `target_entity` stays the recognizer's soft input). The fold refuses a mismatched
> `output_grain` with closed code `UOA_MISMATCH` — threaded through BOTH lenses (the recipe
> site AND `llm_intent_candidates`; the first test run caught the intent lens serving a
> wrong-grain card as recommended). Mismatches land in `actionable_options` with
> `candidate_status="uoa_mismatch"` and the roll-up next step; the decision row freezes
> `output_grain` + `confirmed_uoa_entity`/`confirmed_spine_ref` (story jsonb, D1 gives them
> columns); `assemble_current_activation_state` re-reads the intent's newest confirmed UOA at
> draft/confirm — a UOA re-confirmed differently after serving blocks as
> ACTIVATION_STATE_DRIFTED (`uoa_current`, failing default). Tests: proposal derivation,
> mismatch actionable + draft 409 naming UOA_MISMATCH, matching-UOA no-op, post-generation
> UOA change drift-blocks the old draft, drift unit test. HONEST NOTES: (1) the proposal
> endpoint lives in `api/routes/contract.py`, not `intake_ticket.py`; (2) the confirmed UOA
> does NOT ride `confirmed_scope_dimension` (its dimension CHECK is closed in deployed 0976)
> — it rides the scope hash + frozen decision rows; `scope_for_run` does not rebuild it;
> (3) item 3's "population verifiably joinable to the spine" beyond same-grain refusal is
> C-phase work (resolver-backed pins); (4) the scope screen's confirmation block landed
> (yes/no on the derived proposal, closed alternatives list, contradiction line, skip-free;
> carried on confirm AND broaden) — its end-to-end appearance is asserted in E0; (5) the UOA
> compare is case-insensitive (the catalog declares "Customer", grains say "customer" —
> caught when the endpoint's entity casing met the fold). Suites 10919 backend / 795
> frontend / eval 59.

### Task B8 — readiness probe before spend (½ day)

*Modify:* `api/routes/contract.py` (scoped route step 4–5), `gate1.py`.

`check_projection_readiness` runs at run-mint time, BEFORE any model dispatch; a lagged
projection 503s having spent zero provider calls.

*Acceptance:* test with a lagged projection asserts 503 and `FakeLLM` recorded zero calls.

> **B8 ACCEPTED `e6e73c7f` (2026-08-13).** Probe at the top of the scoped route, pre-dispatch;
> the zero-spend proof is a nothing-scripted fake that 503s without a KeyError. Suite 10914.

## 4. Phase C — the metadata actually governs *(closes PLAN-06…PLAN-12, GEN-06)*

### Task C1 — resolver-backed capability pins (1–1½ days)

*Modify:* `column_capabilities.py`, tests.

Compile pins from the governed field-resolution DECISION — through a new **read-only batched
current-resolution API** (verified: `resolve_and_project` WRITES — it records decisions and
projects into `graph_node`; generation must never mutate the catalog it reads). The pinned
decision is `(value, producer, strength, evidence_id, conflict_state)` — indivisible, and
`conflict_state` is **the resolver's own verdict**, not any-mismatch (a newer weak LLM
proposal disagreeing with a human-confirmed value is a losing proposal, not a semantic
conflict — validated correction). Weaker-later evidence can no longer displace stronger.

*Acceptance:* the review's exact failure sequence as a test — display concept A, later active
proposal B: capability carries A with A's OWN authority and a conflict marker; B's strength
never rides A. Existing capability tests updated deliberately.

> **ACCEPTED (2026-08-13).** `current_resolution_pins` (field_resolution.py) — READ-ONLY
> batched re-run of the ONE resolver law (`resolve_field_authority`, the same fold the write
> path records decisions with) over active evidence: two constant queries (evidence +
> pending-revalidations) regardless of fan-out; never writes. The pin is indivisible
> `(value, producer, strength, evidence_id, conflict_state, load_bearing)`; the winner's
> authority is attributed to the STRONGEST active view carrying the resolved value.
> `compile_capabilities` consumes it — the `:129` "newest active wins" read is gone; a
> policy-less field (economic_role) pins strongest-wins with `conflict_state="no_policy"`.
> Capability gains `authority_conflicts` — populated ONLY by the resolver's own verdicts
> (operational strategy "conflict" OR the display selection's equal-strength tie, carried as
> new additive `FieldResolution.display_conflict` — concept is a RECOMMENDATION-tier field
> that short-circuits before the operational check, so its contested state was invisible).
> Acceptance tests: the exact A-then-B sequence pins A + human/confirmed with NO marker (a
> losing proposal is not a conflict); two disagreeing human-confirmed values ARE the marker;
> a lone llm proposal keeps llm/proposed so the floors ride. Deliberate pin updates:
> capability compile = 2 constant queries; the lens = 3 (was "exactly 2"; C1 adds the
> revalidation read — still O(1)).

### Task C2 — the four authority matrices (1 day)

*Modify:* `semantic_eligibility.py`, `activation_policy.py`, tests.

`AUTHORITY_MATRIX` grows `retrieval / suggestion_at_declared / authoring / execution_at_governed`
columns (data, content-hashed — the policy hash moves). Eligibility keeps consuming the
suggestion column; `activation_decision` consumes authoring (create_contract) and execution
(materialize) — replacing A1's unconditional `EXECUTION_AUTHORITY_UNEVALUATED` block with the
real floor check against every bound operand's measured authority.

*Acceptance:* matrix tests per column; materialize allowed only when every operand clears
`execution_at_governed` (which today means human/confirmed or source/attested — a test proves
llm/proposed NEVER clears it); the A2 materialize tests flip from unconditional-block to
floor-driven.

> **ACCEPTED (2026-08-13).** AUTHORITY_MATRIX grew the four use columns (+ `clears()` fail-
> closed on both axes); the matrix rides the policy hash, so pre-C2 frozen options read as
> ACTIVATION_STATE_DRIFTED and regenerate — the intended rollout. Monotone-ladder pin
> (execute ⟹ author ⟹ suggest ⟹ retrieve) + the load-bearing rows: source/declared authors
> but NEVER executes; llm/proposed clears neither. The decision row freezes
> `operand_authorities` (measured at serving) + the plan's `read_set`/catalog rides to
> FrozenOptionFactsV1; `assemble_current_activation_state` re-folds BOTH floors at the
> durable write through C1's `current_resolution_pins` (the same resolver law, read-only) —
> `authoring_floor_met` catches only DRIFT (serve-time failures keep riding
> confirmation_required_roles, no double-naming: guarded + tested), and the execution floor
> replaces A1's unconditional EXECUTION_AUTHORITY_UNEVALUATED with evaluated/met from the
> matrix. Serve-time sections fold the frozen authorities the same way. E0's step 4 flipped
> deliberately: after the funnel confirmations the floor is measured and MET (neither
> EXECUTION code appears) and the honest remainder refuses (readiness + formula schema).

### Task C3 — enforce or refuse every authored constraint (2–3 days, the long tail)

*Modify:* `semantic_eligibility.py`, `recipe_operand_policy.py`, `typed_gauntlet.py`,
`semantic_eligibility_reasons.py`, migration **1065** only if sign evidence needs a store.

For each transported-but-unconsumed field, ONE of: enforced check, or named
`UNSUPPORTED_*` blocker. Verified inventory and dispositions:

| field | disposition |
|---|---|
| `allowed_source_grains` | enforce: candidate's population/dataset story grain ∈ allowed set, else `SOURCE_GRAIN_MISMATCH` |
| `join_role` / `temporal_role` | enforced by B7's planner translation (verify; else UNSUPPORTED) |
| `unit_expectation` | enforce against unit facts where present; absent facts → needs_setup |
| `currency_expectation` + conversion | already floors on missing currency; add per-row-column vs fixed-code check |
| `status_policy_ref` | UNSUPPORTED blocker until a status-policy resolver exists (named, visible) |
| `relationship_requirement` + cardinality | enforced by B7 planner facts, else blocked |
| **sign** | the authored expectation is an EXPECTATION, never authority. Same-column opposing legs require GOVERNED sign representation — EITHER a `sign_convention` evidence row on the amount column (signed-amount convention) OR a governed pairing with a bound direction column (`debit_credit_indicator` role — the representation real banking schemas use, validated correction); otherwise blocked. Fixes the confirmed `recipe_operand_policy.py:276` defect |
| output null/empty/zero-denominator policies | gauntlet checks presence when the operation makes them load-bearing (ratio ⟹ zero-denominator, etc.) |
| additivity vs operation | gauntlet: `sum` over non_additive/semi_additive without an as-of dimension = blocked |

Plus the **meta-test**: every behavior-bearing field on `OperandSpecV2`/`OutputSpecV2` must
appear in a registered-consumer map (enforced or named-unsupported) or the test fails — the
review's "no field without a consumer" ratchet.

*Acceptance:* per-field positive + negative tests; the meta-test; the banking battery gains the
sign-leg, additivity-abuse, and grain-mismatch cases.

> **SIGN SLICE ACCEPTED (2026-08-13).** The confirmed defect is dead: the authored
> `sign_direction_expectation` no longer licenses anything (the pre-C3 test asserting it as a
> feature was flipped deliberately). ONE law in both binders (`_resolve_opposing_legs`):
> opposing legs on one physical column are licensed by (a) a BOUND direction operand
> (`debit_credit_indicator`) in the same request — PROBE_RECIPE's own shape — or (b) a
> governed `sign_convention` fact at AUTHORING authority on every shared column, read through
> C1's pins + C2's matrix (human/confirmed licenses; llm/proposed does not — both tested).
> The capability compiles `sign_convention_cleared` in the SAME batched read (pin still 3 —
> the request binder stays pure; the first draft's per-request read showed up as 27 queries
> and was rehomed). The block's resolution names both real fixes. DISCOVERY: recipes whose
> opposing legs previously bound via authored strings now block honestly until the catalog
> carries a representation.

> **C3 COMPLETE (2026-08-13) — the long tail wired.** Every inventoried field enforces or
> refuses by name: `SOURCE_GRAIN_MISMATCH` (the event/snapshot AXIS at declared+ — the
> SE-8p2 posture; finer row-kinds have no catalog fact, named honestly),
> `UNIT_INCOMPATIBLE` (a currency-bearing column serving a non-monetary expectation; the
> absent-facts half reports through C5), `STATUS_POLICY_UNRESOLVED` (52+ operands' governed
> status reads ride every candidate as named setup work — provisional, never blocked, never
> silent), `ADDITIVITY_INCOMPATIBLE` (the stock/flow law: sum over non_additive never; over
> semi_additive only under an as-of anchor — output+anchor threaded through the binder's
> eligibility call), `OUTPUT_POLICY_INCOMPLETE` (a ratio-shaped output without its authored
> zero-denominator policy is a named gauntlet requirement). THE META-TEST lands
> (`test_recipe_field_consumers.py`): every `OperandSpecV2`/`OutputSpecV2` field must carry
> a registered disposition (enforced/partial/expectation/display) — a new field fails the
> build until its consumer or honest non-consumer is registered. Battery gains the
> stock-flow additivity-abuse and snapshot-fed-transaction grain-mismatch cases (sign-leg
> cases live in the operand-policy suite). Honest scope notes: `join_role` = partial
> (cross-catalog planner only; single-source plans have no join by construction);
> sign/output prose fields = expectation-class, consumed at the formula-authoring seam.

### Task C4 — personal-data purpose policy in the engine path (1 day)

*Modify:* `column_capabilities.py` (consume the D14 `pii_policy` resolver the legacy gauntlet
already uses), `activation_policy.py`, `semantic_projection.py`
(`personal_data_policy_revision_ids` onto served ideas — the carrier exists).

Capability gains `personal_data: {required, licensed, policy_revision_ids}`; an unlicensed
personal-data operand is `PERSONAL_DATA_POLICY_REQUIRED` (code exists); activation refuses
`create_contract` while unlicensed; licensing revision ids persist on the served idea exactly
as the legacy path persists them.

*Acceptance:* read-allowed-use-denied column refuses activation with the named code (the
review's §10 case); licensed case carries revision ids end to end.

> **ACCEPTED (2026-08-13).** The capability compiles the licence state in ONE bulk
> content-verified read (`active_pii_use_policies` — absence, revocation, and tamper all
> refuse; the read is SKIPPED entirely when the shortlist binds no personal data, so the
> query pin holds). `evaluate_operand` refuses unlicensed personal data with
> `PERSONAL_DATA_POLICY_REQUIRED` naming the governance action (provisional — a policy
> question with an owner, never structural); the code rides the gauntlet's requirements into
> the frozen decision, and `activation_decision` refuses `create_contract` while it is
> outstanding (save_idea stays open). The licensed case carries the EXACT revision ids:
> capability → eligibility verdict → served idea's `personal_data_policy_revision_ids`
> (the same carrier the legacy path persists). Tests: §10 unlicensed/licensed at the fold,
> the compile round-trip against a real approved policy (`pep_flag`, the standing example),
> and the activation refusal.

### Task C5 — DESIGN_CHECKED means every family evaluated (1 day)

*Modify:* `typed_gauntlet.py`, tests.

A closed `_POLICY_FAMILIES` registry (leakage, identifier, temporal, dataset, unit/currency,
additivity, sign, status, relationship, personal-data, formula-output). Each family reports a
TRI-STATE (validated correction — a recipe with no monetary operand has no currency family to
evaluate): `evaluated` / `not_applicable` (with the reason derived from the request's own
shape) / `missing` (could not evaluate — capability axis absent). `design_checked` requires
every family `evaluated` or `not_applicable`; any `missing` emits `NEEDS_SETUP`/`UNSUPPORTED`.

> **ACCEPTED (2026-08-13).** `POLICY_FAMILIES` (the 11, closed) + `FamilyReportV1` tri-state
> on `TypedValidationV1.families` (additive). Applicability derives from the REQUEST's own
> shape (no unit expectation → unit_currency not_applicable WITH the reason; no opposing-leg
> groups → sign n/a); "missing" comes from the eligibility fold's recorded `facts_absent`
> axes (new additive carrier on the verdict — unit/additivity/table_shape absence recorded
> where the check would have run) plus candidate-level absences (no dataset story; a status
> policy with no resolver). Every missing family emits POLICY_FAMILY_UNVERIFIABLE with the
> family named — design_checked now MEANS every family answered. Tests: the registry is
> complete per validation; missing blocks design_checked (story-less candidate); status
> family missing until a resolver exists; the pre-C5 design-checked test now seeds a real
> story (the honest flip).
Success-by-omission becomes structurally impossible without blocking features the family
genuinely does not concern.

*Acceptance:* removing any family's evaluation flips a previously-design-checked fixture to
needs_external_validation (parameterized over all families).

### Task C6 — authority-ranked shortlists, honest truncation, consumed hints (1 day)

*Modify:* `recipe_operand_policy.py`, `semantic_candidate_store.py` (truncation onto the
observation row), tests.

1. Rank before truncating: authority tier (confirmed > attested > declared > proposed >
   hint) → exact-concept-before-alternative → governed economic role → user
   `binding_hint_refs` match → stable ref order. THEN cut at 16.
2. `truncated` returns on the verdict/eligibility audit and persists.
3. A REQUIRED operand whose shortlist truncated without an eligible winner fails closed as
   `REQUIRED_OPERAND_AMBIGUOUS` (+truncated marker), never "missing" from an incomplete search.
4. `binding_hint_refs` become a ranking signal (user-only, already validated as such) — never
   an override of eligibility.

*Acceptance:* a human-confirmed column at index 20 of 25 same-concept columns WINS; the
truncation flag survives to the observation row; the hint promotes an eligible ref and cannot
promote a blocked one.

> **ACCEPTED (2026-08-13).** `request_shortlists` no longer blind-cuts at 16 (safety bound =
> MAX_BINDING_ASSIGNMENTS); the cut moved to `bind_with_capabilities` AFTER the authority
> ranking (tier → exact-concept-before-alternative → governed economic role → the user's
> hint → stable ref), where the evidence pins exist. `OperandBindingVerdictV1` gains additive
> `shortlist_truncated` (persists to the observation row via the existing asdict
> serialization — proven by test). A REQUIRED operand whose cut shortlist yields no winner
> fails closed as REQUIRED_OPERAND_AMBIGUOUS+truncated, never "missing" from an incomplete
> search. The hint is (a) a retrieval ranking signal and (b) the requester's OWN adjudication
> among BINDABLE same-tier peers (`tie_break_verdict_ref="user_hint"`) — it never reaches a
> blocked or ineligible ref (those never enter `bindable`). Acceptance tests: index-20-of-25
> confirmed column WINS with truncation recorded; observation round-trip; hint adjudicates
> equal peers and cannot promote a structurally-blocked hinted ref.

### Task C7 — semantic closure for retrieval (1 day)

*Modify:* `generation_semantic_context.py` (versioned closure map: concept → self + ancestors +
namespace-mates from the registry, content-hashed into the context), `recipe_operand_policy.py`
(shortlist assembly consults the closure; eligibility still decides exactly), tests.

*Acceptance:* an operand asking `monetary_flow` retrieves a column enriched with a REGISTERED
descendant concept and eligibility still refuses a mismatched meaning; closure changes move the
context hash.

> **ACCEPTED (2026-08-13).** Context v4: `concept_closure` (enriched concept → self + is-a
> ancestors + namespace mates, from the frozen registry — new `namespace_mates` helper) rides
> the field-exhaustive hash, so any closure change IS a new context (pre-C7 frozen options
> drift honestly — C2's rollout pattern). Shortlist assembly widens through the closure
> (126 registered descendant concepts previously NEVER retrieved for their ancestor
> operands — `interest_income` for `monetary_flow`); eligibility accepts a registered
> DESCENDANT (its is-a path reaches a wanted name — the specialized flow IS the flow) and
> refuses a namespace MATE with CONCEPT_MISMATCH (join-candidacy peer, never a meaning
> substitute — retrieved into the audit, visible, refused). SELECTION law sharpened by E0
> itself: an exact-name candidate outranks a closure descendant in selection — the closure
> adds recall when the exact meaning is absent and never manufactures a tie against it (the
> walkthrough caught `origination_date` tying the exact `event_ts` before this rule).
> Tests: descendant binds; mate retrieval-only; closure moves the hash; E0 green.

### Task C9 — history requirements per catalog: declared, listed, confirmed (1½ days)

*User steer 2026-08-13: "the history requirements per catalog should be listed and confirmed
with human at some stage in the process."*
*Verified gap: NO declared history-depth fact exists anywhere — the only defense is the
runtime EVENT_HISTORY_VERIFICATION check, so a 180-day feature over 13 months of data and one
over 13 DAYS look identical until someone runs the data check.*

*Modify:* `generation_semantic_context.py` (table_facts v3→v4 + pin test),
`column_capabilities.py`, `semantic_eligibility.py` + reasons, the governance funnel
(SE-4b pattern) + a listing API/section, tests. Mechanism = the SE-8p2 dataset-axis template,
reused exactly: a TABLE-level fact with its own evidence authority.

*Steer refinement (user, 2026-08-13): "we don't have such things in governance and we want
to keep this info OPTIONAL — most business users won't have this technical info." This also
catches a design error in the first draft: "undeclared → provisional" would demote candidates
for missing OPTIONAL metadata, violating invariant 6 (missing ≠ contradictory). Corrected:*

1. **The fact is OPTIONAL, with TWO entry points and a valid third of "never"** (user
   refinement 2026-08-13): (a) AT UPLOAD — an optional field in the upload manifest lands as
   `source/declared` with zero extra clicks; (b) ANY LATER TIME — the optional governance
   panel writes `human/confirmed`; (c) never — permanently fine. A correction is a new,
   stronger evidence row (append-only), never an overwrite. An `llm/proposed` depth clears
   NOTHING. Nobody is ever REQUIRED to provide it.
2. **Absence changes nothing**: no depth declared → the candidate keeps EXACTLY today's
   behavior — eligible, with the runtime EVENT_HISTORY_VERIFICATION data check as its named
   homework (which already exists on every event anchor). At most a presentation-only
   `history_depth_absent` marker rides `missing_context` — never a status change, never a
   blocker, never a new to-do pushed at a business user.
3. **Only a KNOWN contradiction bites**: when someone DID declare a depth and the variant's
   window exceeds it (W > D at declared-or-better authority) → **blocked**
   `HISTORY_DEPTH_INSUFFICIENT` ("reduce the window or extend the source's history") — with
   B5's variants this is surgical: the 180-day variant blocks, the 30-day variant of the SAME
   recipe stays eligible. Declaring MORE information can only make the engine smarter, never
   the workflow harder.
4. **The LISTING is an optional enrichment panel, not a gate**: a per-catalog "How far back
   does this data go?" section — business phrasing, never the column name — computed from the
   applicable recipes/variants (per event table: the MAX window any applicable variant needs,
   beside whatever is declared and by whom): "transactions — features here look back up to
   180 days; source says 400 days ✓ / accounts — features look back 90 days; not stated —
   add it if you know". Answering is one optional click that writes the evidence row;
   ignoring the panel costs nothing. No funnel to-do count, no walkthrough requirement.
5. **Runtime honesty unchanged**: the gauntlet's EVENT_HISTORY_VERIFICATION stays on every
   bound event anchor — declared depth is a claim about intent; the data check verifies the
   rows actually reach that far.

*Acceptance:* the banking battery gains BOTH directions — window-exceeds-declared-depth
blocks at declared+ (proposed never blocks), and an UNDECLARED depth leaves the candidate's
status byte-identical to today (only the runtime check named); the listing endpoint returns
the computed requirements beside declared values; an optional confirm writes the evidence row
and a previously-blocked variant clears on regenerate. NOT in the E0 mandatory walkthrough —
it appears only as an optional branch.

> **ACCEPTED (2026-08-13).** `history_depth_days` = an OPTIONAL table-level fact riding the
> capability's EXISTING pin read (zero new queries — `_PINNED_FIELDS` + a `_TABLE_ADVISORY`
> policy entry, the SE-8p2 template exactly). The law is surgical and one-directional: the
> variant's own window (threaded from `parameter_values` through the binder) blocks with
> HISTORY_DEPTH_INSUFFICIENT only when it EXCEEDS a depth declared at declared-or-better —
> proposed never blocks, absence changes NOTHING (proven byte-identical: both windows bind
> with nothing declared), and the resolution names the shorter-variant sibling. Slice 2:
> `GET /governance/history-requirements` (per event table: the registry's max window axis
> beside declared depth + authority; "not stated" is a fact with `sufficient: null`, never a
> to-do) + `POST` (one optional click → human/confirmed evidence row, append-only; 404 on an
> unknown table, confirmer-gated). The correction path is proven: a human confirming 400d
> outranks the source's 90d and the blocked 180-day variant clears on the next bind.
> VOCABULARY NOTE: the plan's "source/declared at upload" is `source/attested` in the
> evidence vocabulary (no "declared" strength exists); the upload-manifest entry point
> remains open — the governance POST covers entry point (b), and (a) lands with the upload
> seam when that manifest gains optional fields. RATCHET CATCH: the feature-context coverage
> guard flagged the new field on the first gate — registered DELIBERATELY_OMITTED with the
> reason (a machine floor check, never prompt material; the model never needs to see it).

### Task C8 — prose physical-reference detector (½ day)

*Modify:* `feature_intent.py`, tests.

Bounded SERVER-SIDE scan of intent display/definition/rationale strings against the frozen
semantic context's table/column token set — validated correction: the model-facing inventory
is deliberately physically blind (concepts + counts only), so scanning "against the inventory"
would match nothing; the scan runs after the model responds, against `context.columns`
tokens. A match rejects the item (`INTENT_REJECTED_PARSE`, "model prose names physical
objects").

*Acceptance:* an intent whose rationale says "use bo_cib_customer.cust_num" is rejected
per-item; clean prose passes.

> **ACCEPTED (2026-08-13).** `prose_physical_references` (feature_intent.py) — the bounded
> server-side scan over display/definition/rationale/conceptual_reason against the frozen
> `context.columns` token set (column names, table names, qualified forms), AFTER the model
> responds (the inventory is physically blind by design — the validated correction). Bounded
> deliberately: only physical-LOOKING tokens (underscore or dot) are candidates, so a table
> named "transactions" never fires on ordinary English prose — the naming convention is the
> signal. A match rejects THAT item (INTENT_REJECTED_PARSE, "model prose names physical
> objects: …") and clean siblings in the same batch survive. Tests: the qualified-ref case,
> a bare column name in the definition, and the plain-English false-positive guard.

## 5. Phase D — the candidate seen is the candidate governed *(closes LIFE-01…03, PLAN-15, UI-02/04/05/06)*

### Task D1 — enrich `SemanticOptionDecisionV1` to the full evidence record (1 day; A1b created it)

*Create:* migration 1063 (`semantic_option_decision`, append-only, guard triggers), a store
module; *modify:* `gate1.py` (persist per served option in the request transaction),
`contract.py` (option detail reads it by primary key).

The record: full planning request + variant values, all verdicts + full eligibility audit +
losing shortlist + truncation, readiness, review fold at generation, validation result, dataset
story, plan envelope, `decision_manifest` (content hashes of every consumed capability,
evidence decision, policy revision, closure map, planner artifact — PLAN-15's seal),
observation id, and the option id it serves. Option detail joins by **decision id carried in
the option record** — LIFE-03's wrong-row risk is structurally gone.

*Acceptance:* drafting reads the stored decision without recomputation (test stubs live
metadata to explode); detail returns exactly the selected candidate's evidence for merged
twins and variants; hash verification test (tamper → typed 409).

> **ACCEPTED (2026-08-14).** Migration 1065 (the reserved slot — sign needed no store):
> additive `evidence` + `decision_manifest` jsonb on the append-only 1063 table (the
> existing guard triggers make them write-once with the row). `evidence` = the COMPLETE
> audit frozen verbatim at serving: full planning request, every verdict, the per-candidate
> eligibility with the losing shortlist and its truncation marker, and the typed validation
> with C5's family tri-state. `decision_manifest` = PLAN-15's seal: content hashes of every
> consumed input (context, authority matrix, gauntlet version, operand-class map, planning
> request, binding plan, recipe revision). `load_option_decision_record` reads by the EXACT
> (revision, option) primary key; the option-detail route serves it with the observation
> joined by the frozen `observation_id` — LIFE-03's newest-row-for-the-definition read
> survives ONLY as the compatibility path for pre-A1b revisions. Verification is real: a
> manifest disagreeing with the row's own request identity is a typed 409
> (DECISION_RECORD_TAMPERED). Frozen-layer no-recompute was already A1b's property
> (`load_frozen_option_facts`); the detail read adds no recomputation either — everything
> served is the stored jsonb.

### Task D2 — executable identity vs display identity (½ day)

*Modify:* `candidate_assembly.py`, `gate1.py` option-id mint.

**The candidate seen is the candidate governed — so the DISPLAY signature carries the
canonical formula/mechanism identity too** (validated finding 7: hiding different formulas
behind one card with different secret option ids breaks the program's core promise). Merge
ONLY candidates with identical executable semantics; different formulas are separate,
visibly-distinct selectable variants; corroborations mean "the same computation from another
origin", never "a different computation". The OPTION ID additionally hashes the plan envelope
+ decision-manifest hash (physical identity on top of executable identity).

*Acceptance:* two candidates differing only in formula expectation render as TWO cards; a
recipe/intent pair with identical executable semantics still merges; drift in any manifest
hash mints a new id.

> **ACCEPTED (2026-08-14).** `semantic_signature` gains `mechanism_identity` (the pinned
> formula expectation ref, "" when none) — two formulas are two visibly distinct cards.
> DESIGN SIMPLIFICATION discovered mid-build: the planned "unpinned twin folds into a
> single-mechanism group" rule is UNREACHABLE by the planning contract's own atomicity law
> (deterministic ⟹ formula present, enforced at construction; conceptual ⟹ formula refused)
> — pinned-vs-unpinned "identical executable semantics" cannot exist, so the fold was
> dropped rather than shipped dead. The cross-origin merge stays where it is real: identical
> conceptual twins (both formula-less by contract) fold into one corroborated card, recipe
> primacy intact. A conceptual candidate never merges with a deterministic one (a different
> computation — three cards, not two; tested). Option ids are v3: the PHYSICAL identity
> (planning-request hash + binding plan + the D1 decision manifest) hashes in on top of the
> executable identity — drift in any consumed input mints a new id.

### Task D3 — Workbench audit drawer + card sections (1½ days)

*Modify:* `WorkbenchScreen.tsx`, `api.ts` (detail client), tests.

1. Option detail fetched on demand (UI-02) into a drawer: frozen roles + authorities, losing
   shortlist, dataset story, plan/PIT summary, policy hashes, revision identity.
1b. The list groups by each candidate's TYPED operation class ("Ratios & utilization",
   "Recency & activity", "Flows & sums" — from the closed `RESULT_CLASS_ADDITIVITY`
   vocabulary), restoring the browsing experience the legacy lenses gave, with headings that
   are facts about the feature rather than names of the prompt that produced it.
2. Card gains the review's §UI-04 sections: "what it computes" (operation, window/variant,
   unit, additivity, grain), "can it be built" (plan summary or first blocker), readiness +
   review chips as separate axes (never one green badge).
3. Deep link from a `PROPOSED_METADATA_ONLY` blocker to the asset field-decision screen focused
   on the field; returning regenerates a fresh run (UI-06 / rule 10 — the card never
   self-approves its own metadata).

*Acceptance:* screen tests per section; the deep link carries the exact field; a post-confirm
regenerate mints a new revision (route already does — test the UI flow).

> **ACCEPTED (2026-08-14).** Drawer (UI-02, shipped with `010b6c13`): on-demand fetch of the
> STORED decision record by exact key — frozen roles + authorities, the losing shortlist,
> the frozen plan, family tri-states, revision identity; honest absence for pre-A1b options.
> 1b: `operation_class` rides the card (FeatureIdea + projection stamps the formula's
> result_class + both gate1 serializers round-trip it); the browsing list groups under FACT
> headings from the closed vocabulary ("Flows & sums", "Ratios & utilization", "Conceptual
> patterns"…) only when ≥2 real groups exist — a single-class round keeps the flat list
> byte-identical, and group order = first appearance so the engine's ranking still leads.
> §UI-04: the card carries "can it be built" (the SERVER's verdict — plannable, or the first
> blocker's named next step) and review currency as its OWN chip (derived from the server's
> RECIPE_REVIEW_NOT_CURRENT blocker code, never folded into one green badge). UI-06: the
> needs-confirmation chip is now a deep LINK to `#asset?source=…&object_ref=…` — the exact
> field, asserted in-test; regeneration on return is the route's existing law. One deliberate
> relabel ("needs confirmation →").

### Task D4 — Suggested Features on the shared carrier (1–1½ days)

*Modify:* `suggestions.py`, `suggestion_contract.py`, `SuggestedFeaturesScreen.tsx`,
`SuggestionCard.tsx`.

Contract v4's hits are REPLACED by the engine's assembled candidates rendered through the same
option carrier Workbench uses (UI-05 — one card model, one eligibility result; the legacy
per-table template pass retires from v4; v1–v3 remain frozen until the E4 cutover deletes
them). The page anchors/filters the shared results by the opened table.

*Acceptance:* the parity test hardens from "binding states agree" to "the CARDS are the same
carrier"; v3 byte-freeze pins still hold.

> **ACCEPTED (2026-08-14) — PHASE D COMPLETE.** Every v4 semantic entry carries `card` — the
> projected FeatureIdea serialized by gate1's OWN `_idea_json` (imported, never copied: one
> carrier by construction). The hardened parity test re-projects the anchored candidates and
> compares the served cards EQUAL to the same-serializer output, over both sections (this
> fixture's engine entries are all actionable — the comparison covers whatever sections
> exist and requires ≥1 card). The screen renders the shared card (name, description,
> operation-class chip, needs-data-checks chip, typed input rows with authorities,
> corroborations) with the raw recipe-id row surviving only for pre-D4 deployments. v3
> byte-freeze pins hold (the v4-minus-semantic == v3 equality test still passes with `card`
> riding INSIDE the semantic block). HONEST SCOPE NOTE: "the legacy per-table template pass
> retires from v4" is deliberately deferred INTO E4's cutover knife — one deletion pass over
> suggestion v1–v3 + the legacy pass together, verify-first, rather than two partial
> deletions.

## 6. Phase E — proof, then the knife *(closes REL-04/05, REL-01/02 pragmatically, GEN-01's tail)*

### Task E0 — the end-to-end walkthrough gate (1 day) — **this plan's own acceptance test**

*Create:* `tests/featuregen/api/test_e2e_walkthrough.py` (API-level, FakeLLM-scripted) + the
UAT runbook section in `docs/architecture/`.

The bar in §0.1, as ONE test that walks the whole workflow and clears every blocker through
its real surface:

1. seed a cib-shaped catalog (proposed-only concepts, no reviews) → considered set →
   assert the target candidate is served with `create_contract` BLOCKED, codes
   `PROPOSED_METADATA_ONLY` + `RECIPE_REVIEW_NOT_CURRENT` (+ `PHYSICAL_PLAN_MISSING` until B7);
2. confirm the concept through the REAL funnel route; record a review through the REAL
   `POST /recipes/{id}/reviews`; confirm the UOA/spine at scope (B10 — plain-English, one
   click); regenerate. (C9's history panel is OPTIONAL enrichment — exercised as a branch,
   never a required step);
3. assert `create_contract` now ALLOWED → draft → confirm → feature registered with
   `lifecycle_state='governed'` and the contract carrying `FORMULA_BLOCKED` readiness honestly;
4. assert `materialize` refused with the typed code;
5. save-idea path works for a conceptual LLM intent;
6. refine round-trips (B9);
7. the suggestions v4 page agrees on the same binding.

The UAT runbook documents the same walk as human clicks (screens, buttons, expected chips) for
the SME session.

*Acceptance:* the test is green and RUNS IN THE DEFAULT SUITE (not eval-marked) — from the day
it lands, any regression that breaks the end-to-end path breaks the build.

> **ACCEPTED (2026-08-13).** `tests/featuregen/api/test_e2e_walkthrough.py` — ONE default-suite
> test walks the whole workflow: blocked card (PROPOSED_METADATA_ONLY +
> RECIPE_REVIEW_NOT_CURRENT, save_idea still allowed) → REAL funnel confirm
> (`POST /governance/concept-confirmations`, CAS anchors from the queue) → REAL reviews
> (`POST /recipes/complaint_count/reviews`, all required roles, two identities) → UOA one-click
> confirm (proposal endpoint asserted: Customer via public.accounts.customer_id) → regenerate →
> create_contract ALLOWED → draft → confirm → `lifecycle_state='governed'` → materialization
> typed refusal (READINESS_NOT_MATERIALIZATION_READY + EXECUTION_AUTHORITY_UNEVALUATED, never
> in allowed_actions) → conceptual intent saves as idea → refine round-trips
> (regenerate_to_govern) → suggestions v4 serves the SAME binding ranked. UAT runbook:
> `docs/architecture/2026-08-13-semantic-v1-uat-runbook.md` (same walk as human clicks; sign-off
> scope = authoring ready, materialization visibly unavailable). DISCOVERIES: (1) the hero is
> `complaint_count` — `tenure_days` binds but its AUTHORED temporal has no snapshot policy, so
> it is honestly rejected TEMPORAL_POLICY_UNRESOLVED (asserted in-test; recipe setup work, and
> the old "small fixture serves no recipe card" note was stale — A3 made ~100 recipes serve);
> (2) the fixture records llm/proposed FIELD EVIDENCE per column (what the funnel reads — the
> real freshly-ingested shape); (3) the C0 seal's isolation gates are stubbed so generation
> SEALS FOR REAL in the READ COMMITTED harness (production always seals; isolation pinning has
> its own suite); (4) PRODUCT FIX: `recipe_review.record_decision` held this codebase's ONLY
> route-level `conn.commit()` — redundant under get_conn's commit-on-success and a request-
> atomicity/test-isolation defect; removed.

### Task E1 — banking acceptance corpus (2 days)

*Create:* `tests/eval/gold/test_banking_acceptance.py` + fixtures.

The review's §10 table, all 14 cases, as named versioned fixtures run END TO END through the
serving path (route in, wire out) — not unit folds. Each case asserts the exact refusal
code/action/served variant the table names.

*Acceptance:* all 14 green; each case's docstring cites its review row.

> **ACCEPTED (2026-08-14).** `tests/eval/gold/test_banking_acceptance.py` — all 14 §10 rows
> END TO END (route in, wire out), each docstring citing its row; versioned v1 fixture
> builders; eval-marked (the eval gate is now 73). THE CORPUS EARNED ITS KEEP ON DAY ONE —
> two real defects found and fixed: (1) the projection's legacy-map translate-and-drop meant
> C3/C4's requirement codes (STATUS_POLICY_UNRESOLVED, PERSONAL_DATA_POLICY_REQUIRED, …)
> never reached the FROZEN decision facts on the real path — C4's activation rule could
> never fire on the wire; the frozen facts now carry the gauntlet's RAW vocabulary alongside
> the card's legacy translation; (2) a LATENT SERVING CRASH: CURRENCY_CONSISTENT registers
> at schema v2 but the projection minted at the v1 default — UnknownRequirement the first
> time a currency-expecting operand bound a currency-less column; requirements now mint at
> each code's OWN registered version. DESIGN FACTS the corpus surfaced: the intent wire
> schema cannot even EXPRESS distinct groups / currency expectations / aggregation policies
> (deliberate — authored-recipe vocabulary), so rows 5/6/7 bind at the RECIPE surface: a
> misdeclared stock summed by rfm_monetary_amount; a currency-less amount under its per-row
> expectation; fan_in_fan_out's party legs colliding on one identity column. Case 13's
> supersession uses `changes_required` (the vocabulary has no "revoked"). Cards speak the
> closed legacy data-check names (GRAIN_IS_UNIQUE); the D1 decision record serves the raw
> gauntlet codes — both asserted where each belongs.

### Task E2 — Workbench journey + budgets (1½ days)

*Create:* Playwright hypothesis → considered set → blocked-select → save-idea →
confirm-metadata → regenerate → select → draft → confirm journey; keyboard/focus assertions on
selection, blockers, drawer. *Create:* perf pin — total SQL + provider calls + p95 latency on a
CIB/FTR-sized fixture catalog (SE-0's measured 237-column shape), asserted as budgets.

*Acceptance:* journey green against a real backend (the existing Playwright harness's postgres
requirement fixed or documented); budget test red-lines named numbers.

> **ACCEPTED (2026-08-14).** JOURNEY: `frontend/e2e/workbench-journey.spec.ts` green against
> the REAL backend — real routes, real Postgres, real activation policy; only the MODEL
> client is scripted via the new test-only `featuregen.api.e2e_app:create_e2e_app` factory
> (production's D5 no-fake-fallback untouched; a `_TolerantFakeLLM` folds unscripted tasks
> to the provider-refusal shape every caller already handles fail-soft). The loop: blocked
> checkbox with the server's tooltip → funnel + 3-role reviews via real routes → regenerate
> with the UOA one-click Yes → keyboard select (focus asserted) → decision-record drawer
> (aria-expanded + frozen roles + identity hashes) → Govern → "Governed …
> DESIGN-CHECKED" renders. Idempotent via a per-run source (append-only confirmations would
> otherwise pre-clear a reused DB). BUDGETS: `test_workbench_budgets.py` (default suite) on
> the SE-0-shaped 240-column catalog — SQL ≤600 with the measured composition documented
> (~234 append-only product writes, ~185 tie replays bounded by genuine ambiguity, fixed
> reads ≤40 as their own ratchet), provider calls ≤2, wall ≤20s. HARNESS: the postgres
> requirement is DOCUMENTED (throwaway instance workflow verified); two PRE-EXISTING
> asset-spec breaks surfaced (hidden while postgres never ran, confirmed on origin's own
> config with clean-boot servers): a one-line Search-button locator drift (fixed) and the
> legacy per-table pass yielding zero on un-enriched uploads (the case now asserts the
> page's honest-empty contract; the ≥1-card variant returns with the shared-carrier page —
> the legacy pass itself retires at E4).

### Task E3 — finite divergence run (½ day operator + tooling)

*Create:* a one-shot comparison script (not a mode): replay N recorded hypotheses through
legacy and semantic paths in a dev environment, persist per-candidate pairs, emit the
explained/unexplained table. Operator (user) reviews; unexplained divergences become tasks or
accepted notes IN THIS FILE.

*Acceptance:* the adjudication table committed to `docs/architecture/`; zero unexplained rows.

### Task E4 — the cutover commit (1 day)

*Modify/delete:* `gate1.py` legacy branch, `recommend_feature_sets_report` Gate-1 call site,
`recipe_rollout.semantic_planning` mode + parser, `semantic_shadow` machinery that exists only
for comparison, SE-0's `ALL_TEMPLATES` gate1 pin (updated IN THIS COMMIT per its own rule),
suggestion v1–v3 if E3 confirmed no consumer, the 20-backend.yaml mode line (deleted, not
flipped), and the direct `/features/*` compatibility-only guard (routes now always refuse —
or delete the generation routes outright).

One commit, one review, suite green, deployed with explicit user approval. Rollback = previous
image.

**Verify-first:** before deleting any reader/parser (suggestion v1–v3 serializers, legacy
considered-set reconstruction), prove no persisted record still needs it — query the dev
store for rows in the old shapes; a one-time migration or an explicit reset (user-approved)
precedes the deletion, never follows it.

*Acceptance:* `FEATUREGEN_SEMANTIC_PLANNING` appears nowhere in `src/`; the frozen-config test
updated; every suite green; the deployed cluster serves the engine with the env var absent;
no orphaned persisted record references a deleted parser.

> **ACCEPTED (2026-08-14) — THE LEGACY PATH IS GONE.** One commit. `FEATUREGEN_SEMANTIC_PLANNING`
> appears NOWHERE in `src/` (nor in `deploy/` — the 20-backend.yaml line is DELETED, not flipped,
> with a comment saying rollback is the previous image). What died: gate1's free-form `else` arm
> and its `recommend_feature_sets_report` / `recommend_features` call sites; the `semantic_mode`
> parameter and BOTH of the params only that generator consumed (`entity`, `objective`);
> `_semantic_shadow_compare` and `semantic_shadow_metrics`; `RecipeRolloutConfig` +
> `SEMANTIC_PLANNING_MODES` + the closed parser + `semantic_planning_gate` (a gate that decides one
> promotion has nothing left to decide once it has happened); the route's mode resolution and the
> `semantic_planning_mode` response key (DELETED, not frozen to a constant — a lever with one
> position is a lie about what a deployment can choose); the `/features/*` generators' bodies
> (routes stay and refuse typed 409 — a 404 would tell a client its address was wrong when the
> address is right); and suggestion contract versions 1, 2 and 3 (route + v1 producer path +
> Pydantic mirrors + the v1/v2/v3 frontend clients). `contract_version` now defaults to 4 and
> 1/2/3 earn the same typed 422 as 99, naming `[4]`.
>
> **VERIFY-FIRST, and it earned its keep.** Two findings. (1) A PERSISTED record DOES replay
> through the v2 serializer: `frontend/src/screens/SuggestedFeaturesScreen.serverCapture.json` is a
> checked-in `page_to_json` body replayed by `SuggestionCard.capture.test.tsx` and re-derived by
> `test_suggestion_contract.py::test_the_frontends_captured_server_body_is_still_the_body_the_
> server_sends`. It survives because only the wire VERSIONS were retired: v4's body IS that shape,
> so `page_to_json`/`page_to_json_v3`/`build_page_v2` are v4's producers, not v2's leftovers — and
> deleting them would have been exactly the STOP condition. (2) The legacy per-table template pass
> has a LIVE consumer with no built replacement: `columnSuggestions.ts` → `AssetDetailOverview`
> renders per-column suggestion cards off `page.hits`, and the engine's semantic block is
> TABLE-anchored with a different carrier. Retiring the pass would have blanked a shipped surface,
> which is what the verify-first rule exists to prevent, so it is NOT deleted — see the deliberate
> non-deletions below. Nothing else is orphaned: the `semantic_candidate_observation` rows keep
> their reader (the audit drawer in `contract.py`), and no DB-persisted shape lost a parser.
>
> **B1's typed 422 re-verified post-deletion** on both surfaces, with the docstring saying why it
> matters MORE now: `test_entity_only_scope_is_refused_typed` (considered-set) and
> `test_refine_without_a_catalog_is_a_typed_422_not_an_empty_answer` (refine). While the mode
> existed, an unrefused entity-only request would merely have been answered by the other engine;
> the refusal is now the only thing between it and a page that reads "your catalog can build
> nothing". SE-0's `ALL_TEMPLATES` gate1 pin is UPDATED IN THIS COMMIT per its own rule — it now
> pins the new source (no `semantic_mode` parameter exists; the scoped branch is
> `v2_recipe_candidates`; `recommend_feature_sets_report` is absent from the builder's source) and
> says plainly which callers of `_template_candidates` remain. The frozen-config test became
> `test_no_pipeline_mode_survives_the_cutover`, which asserts the absence of six names AND that the
> env var's own string is absent from `recipe_rollout.py`.
>
> **DELIBERATELY NOT DELETED, with reasons.** (a) The legacy per-table template grounding pass
> (`_template_candidates`, `suggest_features_for_table`, `build_page_v2`) — the verify-first finding
> above; it is the asset-detail column dossier's only content source and a column-anchored engine
> surface is not chartered. It is no longer reachable from the hypothesis route. (b) The `elif
> catalog_source is not None:` template branch in `build_considered_set` and the emergency unscoped
> route — reachable only WITHOUT a confirmed scope, which the scoped route always supplies; retiring
> them means migrating 75 direct builder call sites across 14 test files onto scripted-engine
> fixtures, which is its own charter, not a line in a cutover. (c) `feature_assist.py`'s
> `recommend_features_report` / `recommend_feature_sets_report` / `refine_idea` / `feature_recipe`
> — now unreferenced from `src/`, left standing because the enumerated cutover scope is the CALL
> SITES and deleting the module's internals cascades through shared gauntlet helpers. Each is worth
> its own follow-up; none of them can be reached by a user request any more.
>
> **ONE CONSEQUENCE WORTH NAMING, found by the test surgery.** The governed cross-catalog lens is
> now unreachable over HTTP. The scoped route refuses `catalog_source: null`, and the legacy
> unscoped route never passes `target_entity`, so `build_considered_set`'s `elif is_live:` branch
> cannot be entered by any request. The lens, its planner and its fail-closed invariants stay fully
> covered at the builder and govern layers — but the 3C.2a live cross-catalog feature has no
> customer-visible entrypoint until a multi-catalog frozen context is chartered, which is the same
> charter B1's 422 has been pointing at since Phase B. This is a REAL narrowing of what the product
> can do, recorded here rather than discovered later from an empty screen.
>
> **THREE DEFECTS THE SURGERY SURFACED — open, not fixed here** (each is behaviour the mode used
> to hide, now on the only path there is; all three are asserted-as-current with docstrings that
> say so, never quietly accommodated). (1) **The Delivery-B formula shadow captures nothing on the
> engine path.** `capture_ranked_shadow` resolves its private grounding context through
> `cs.recipe_candidate_keys_by_recipe_id`, which ONLY the legacy `_template_candidates` branch
> fills; the engine branch leaves it empty, so every capture resolves `CANDIDATE_MISSING` and
> writes zero work items. `_revision_recipe_candidate_key` reads the same empty map for E4b
> operand-role reattachment and is likely degraded identically. (2) **The ranker is still keyed on
> the LEGACY template registry.** `_rank_signals` skips any id with no `Template`, so of 317 V2
> recipes only the ~106 in both registries can ever be ranked — an eligible V2-only recipe is
> dropped from `ranking`, from the initial view and from shadow-capture selection. (3) **B1's 422
> fires AFTER the generation run and confirmed scope are minted** — an entity-only request still
> writes a `feature_generation_run` and a `confirmed_generation_scope` row before being refused.
> Cheap to move if the refusal was meant to precede the mint.
>
> **AND ONE BEHAVIOUR CHANGE, recorded rather than papered over.** The feature-360 view's
> top-level `verification` is now `UNVERIFIED` where it was `DESIGN-CHECKED` (the contract row
> itself still earns `DESIGN-CHECKED`). An engine recipe card carries its outstanding runtime data
> checks — `GRAIN_IS_UNIQUE` — and the deleted free-form candidate simply declared none. The card
> got MORE honest; the view's headline followed it.

> **E4 follow-up defects fixed (2026-08-14).** All three defects the cutover surfaced above are
> CLOSED, and each asserted-as-current test is flipped into a fixed-behaviour pin.
>
> **(2 above, fixed first — user-visible) The ranker is re-keyed on the V2 recipe registry.**
> `_rank_signals` now reads a per-recipe profile from `ranking_signals.v2_rank_profiles()` — 317
> profiles, total over the registry — instead of `{t.id: t for t in ALL_TEMPLATES}`, so every
> eligible recipe is rankable rather than only the ~106 with a legacy twin. The five ordering axes
> are UNCHANGED: relevance tier still comes from the disposition, binding quality from this run's
> grounding, modelling-context fit and the soft entity compatibility from the confirmed dimensions,
> under the same laws (`modelling_context_fit_v2` / `entity_compatibility_v2` restate them verbatim).
> Only the universe moved. Where the V2 contract carries the fact it is read directly — `family`,
> `output_grain` as the grain entity, and the temporal COMPILER's verdict for PIT completeness
> (never keyword markers on prose). Explainability, funnel journey and regulatory modelling context
> have NO V2 field — they are legacy authoring metadata — so they are bridged through
> `replaces_legacy_ids`, which is source-controlled and explicit, never heuristic. The 126 V2-only
> recipes therefore carry an honest ABSENCE: no journey, no framework, and an unauthored
> explainability that the ranker's documented total order sorts last on that axis rather than an
> invented `"H"`. `ranking_version` moved off `APPLICABILITY_MAPPING_VERSION` onto a new
> `RANKING_MAPPING_VERSION = "ranking-v2-recipes@1"`: the ranker's mapping changed and
> applicability's did not, and one stamp that speaks for both can only lie about one of them.
> FLIPPED: `test_flag_on_churn_scoped_ranks_eligible_set` asserted `ranked_ids <= eligible` with a
> comment naming the gap; it now asserts `ranked_ids == eligible`. Its family-cap assertion was
> restated honestly — with the whole eligible set ranked, pass 3's incremental cap RELAXATION runs
> on this catalog, which is documented ranker behaviour (pinned unit-side in
> `taxonomy/test_ranking.py`), not a cap violation.
>
> **(1) The Delivery-B formula shadow captures real work off the engine path.**
> `build_v2_recipe_grounding_context` rebuilds a `RecipeGroundingContextV1` from an engine candidate
> — the authored definition (`canonical-recipe-v2`), the resolved variant, and the shared binder's
> per-role column — and `gate1._engine_recipe_contexts` fills both
> `recipe_candidate_keys_by_recipe_id` and `recipe_grounding_context_by_candidate_key` from the
> candidates the run SERVED (ideas and actionable options alike). Logical refs come from the frozen
> context's own index, so no per-binding query is added. ONE context per served recipe, at its
> LEADING variant: both maps are keyed by `recipe_id` (the dispositions' and the ranking's key)
> while B5 serves one card per parameterization, so recording all of them would resolve AMBIGUOUS
> and capture nothing — a regression wearing a different reason code. `variant_primary` is the
> hypothesis match or the authored-first default, exactly what the retired `choose_params` pass
> captured; bindings are variant-invariant, so only the captured window differs, to the one shown
> first. E4b's `_revision_recipe_candidate_key` recovers with it. FLIPPED:
> `test_formula_shadow_records_why_it_could_not_capture_on_the_engine_path` became TWO tests —
> `test_formula_shadow_captures_a_work_item_on_the_engine_path` proves a real immutable work item
> (authority envelope over the three re-resolved roles, grain fact, verified event-time decision)
> plus its outbox pointer on a new obligor catalog, and
> `test_formula_shadow_reaches_the_reviewed_blueprint_and_names_its_disagreement` keeps the merchant
> case, now resolving an EXACT candidate and failing one step later.
>
> **A FOURTH DEFECT SURFACED BY THE THIRD, open and named.** `merchant_mcc_diversity`'s REVIEWED
> Formula-v1 blueprint declares grain entity/role `merchant` (authored against the legacy template),
> while the V2 recipe computes per CUSTOMER. With the map filled, the capture now reaches
> `bind_formula_expectation` and is refused `FORMULA_SOURCE_ENTITY_ROLE_UNRESOLVED` — correctly:
> silently authoring a merchant-grain formula for a customer-grain recipe is the class of error the
> preflight exists to stop. `validate_expectation_registry` still validates blueprint roles against
> the LEGACY template's needs, so re-keying the blueprint is an expectation-registry cutover AND a
> governance act on a reviewed artifact. NOT done here; asserted-as-current with a docstring saying
> why. `obligor_facility_count` — the other authorable recipe — agrees role for role, which is what
> the positive test above exercises.
>
> **(3) B1's typed 422 precedes every durable write.** The refusal moved above the run mint and the
> scope persist (staying BELOW the live-activation interlock, so an unapproved flag-on deployment
> still gets the stronger 503). A refused entity-only request now leaves no `contract_intent`, no
> `feature_generation_run` and no `confirmed_generation_scope` — previously two orphan rows per
> refusal, indistinguishable in the store from a run that generated an empty page. NEW PIN:
> `test_entity_only_refusal_leaves_no_run_and_no_scope_row` asserts the whole write set;
> `test_flag_on_cross_catalog_request_is_refused_and_never_reaches_the_permissive_path` had a
> comment claiming "the refusal precedes every write" that was not true of runs and scopes — it now
> asserts all four tables.

## 7. Sequencing and dependencies

```
A0 ──► A1 ──► A2 ──► A3 ──► A4
              │
B1 ◄──────────┘ (A2's negative tests make B1's serving change safe to verify)
B2, B3, B4 — independent, parallel after B1
B6 ──► B5 (batching lands FIRST — variant expansion must never create a 936-candidate N+1)
B6 ──► B7 (the planner consumes the batched capability universe)
B8 — independent; B9 after B4 (refine revises honest-origin intents); B10 after B2 (intake owns the derivation)
C9 parallel after C1 (it reuses the SE-8p2 table-fact mechanism, not the resolver)
C1 ──► C2 ──► (A2 materialize flips floor-driven)
C3, C4, C5, C6, C7, C8 — parallel after C1
D1 ──► D2 ──► D3; D4 after D1
E0 needs A complete (incl. A1b) + B1/B2/B7/B9 (create_contract cannot go green without the
plan envelope B7 mints; C1 strengthens the authority pins but the funnel-confirm walk clears
without it); E1 needs B+C complete; E2 needs D3;
E3 needs B complete; E4 LAST, after E0–E3 green
```

Estimated effort: A ≈ 4 days · B ≈ 7½ · C ≈ 8 · D ≈ 5 · E ≈ 6 — ~30 focused days; phases A and B
alone (≈11½ days) close every Critical finding, and **A + B + E0 (≈12½ days) is the minimum
end-to-end-testable milestone** — the walkthrough gate green means an SME can run the workflow.

## 8. Standing rules for execution

- Full backend suite green gates every push; frontend suite for frontend changes; the
  eval marker (`pytest -m eval`) joins the gate from A0 onward.
- Acceptance row appended under the task in THIS file per landed slice, with commit hash.
- Migrations 1063/1064 (and 1065 if used) deploy backend-first, with explicit user approval
  per the standing deploy rule; the E4 cutover deploy likewise.
- No new env flags anywhere in this program (D-1); the only mode that exists is deleted by E4.
- LLM-spend actions (E3's replay, live verification runs) are operator actions — explicit user
  go each time.
- The plan file `2026-08-11-semantic-eligibility-feature-generation-workflow.md` receives one
  closing annotation pointing here; its unfinished acceptance rows are superseded by this file.

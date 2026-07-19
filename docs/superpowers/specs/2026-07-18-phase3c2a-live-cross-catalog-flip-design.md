# Phase 3C.2a — Live governed cross-catalog flip (deployment-scoped, activation-gated)

> **Status:** Design, ready for implementation planning. This spec is **3C.2a only**. Governing
> LLM-proposed cross-catalog *ideas* through the planner (and the outright removal of the permissive
> implementation) is **3C.2b** — a separate spec: it needs a real `FeatureIdea → typed planner intent →
> physical-plan enumeration → contract compilation` adapter, which does not exist yet. 3C.2a **holds or
> rejects** cross-catalog LLM ideas rather than pretending the current planner can validate them.

**Goal:** Behind a deployment-scoped, activation-gated flag, make the governed deterministic cross-catalog
planner's output the *only* source of customer-visible cross-catalog features — surfacing resolved
governed recipes, rejecting unresolved ones with structured reason codes, holding/rejecting cross-catalog
LLM ideas, drafting exclusively from a server-persisted governed plan, and **never** invoking the
permissive `find_cross_catalog_path`.

**The invariant this delivers:**
> In an activation-enabled deployment, **every customer-visible cross-catalog feature has a governed
> physical plan.** No two governance standards coexist in the enabled experience.

**Architecture:** A single env flag (`FEATUREGEN_INTENT_LIVE_CROSS_CATALOG`) whose effect is gated by a
durable, server-assembled activation interlock (two append-only records binding a decision to a persisted
PASS evaluation and a code-version vector, keyed by `FEATUREGEN_DEPLOYMENT_ID`). When active, the
governed planner runs inside `build_considered_set`'s entity-scoped branch; each governed option carries
structured provenance + a persisted **plan envelope**; drafting/confirmation rebind to that envelope and
recheck freshness via `ReplayFreshness`. When the flag is off, behaviour is byte-identical.

**Tech Stack:** Python/FastAPI, Postgres (append-only migration), the existing planner package
(`plan_bindings`, `PlannerReplayEnvelopeV1`, `replay.ReplayFreshness`) + the 3C.1 gate harness
(`gate_operate`, `shadow_report`).

---

## 1. Where this sits, and what it is not

Everything through 3B built + shadow-measured the governed cross-catalog planner; 3C.1 gave an operator
an authority-only way to read a machine PASS/FAIL over a real batch (results-only). **3C.2a is the first
customer-facing behaviour change in the whole intent initiative.** It is deliberately reversible (one env
flag) and deliberately *not* the full retirement of the permissive path — LLM ideas that span catalogs
are held/rejected, not silently governed, until 3C.2b builds the idea→planner adapter and removes the
legacy implementation.

**Current live wiring (verified):**
- `build_considered_set` (`gate1.py:220`) runs the deterministic recipe lens **only when a single catalog
  is in scope**; on an **entity-scoped / cross-catalog run it is skipped** — cross-catalog recommendations
  come only from the LLM path today, and the governed planner runs post-response in **shadow**.
- The chosen feature is drafted from the **server-persisted considered-set snapshot** (`_snapshot`,
  `gate1.py:247/273`; `chosen_feature`, `gate1.py:319`), and `draft_contract` records its join path via
  `_join_path` → `find_cross_catalog_path` (the permissive path).

## 2. Behaviour — flag off vs on

**Flag OFF (default):** byte-identical to today. The legacy LLM + `find_cross_catalog_path` flow is intact.
No new query, no readiness check, no response change.

**Flag ON *and* activation-approved** (the enabled predicate in §3), on an entity-scoped run:
1. **Surface resolved governed planner recipes** in the considered set (as options under the appropriate
   semantic lens — see §5 for why authority is a structured field, not the lens).
2. **Surface unresolved governed recipes as structured rejections** carrying the existing planner reason
   codes (never silently dropped).
3. **Single-catalog LLM features unchanged.**
4. **Cross-catalog LLM features are REJECTED** — surfaced as a structured rejection with
   `GOVERNED_CROSS_CATALOG_PLAN_REQUIRED` (a new rejection reason), so they can never become a
   customer-visible recommendation. (A "hold"/pending-governance state is a 3C.2b concept — 3C.2a has no
   mechanism to later govern the idea, so it rejects rather than queues.)
5. **Persist the governed physical plan** (the plan envelope, §4) with the considered-set option, server-side.
6. **Draft exclusively from that server-persisted plan** — `draft_contract` uses the envelope's
   `ordered_path`; it must NOT recompute a permissive path from the feature's columns.
7. **Recheck plan freshness at draft AND at confirmation** via `ReplayFreshness`.
8. **Never call `find_cross_catalog_path`, never fall back to it** — a governed rejection stands.

**Flag ON but NOT activation-approved:** return a **configuration/readiness error before any LLM or planner
dispatch** (§3). Never serve the legacy path in this state.

## 3. The activation interlock (deployment-scoped, no signing)

The flag is necessary but not sufficient. Activation requires a durable, server-assembled approval binding
a human decision to a persisted PASS evaluation and the exact code-version vector, keyed to this deployment.

### 3.1 Two append-only records (new migration)

**`enablement_evaluation`** — a persisted, content-hashed run of the 3C.1 machine gate (3C.2a adds the
persistence; 3C.1's computation stays results-only). Server-assembled from trusted sources, never the client:
- `evaluation_id`
- `telemetry_window` (cohort/producer_commit + `[since, until)`)
- `population_report` (the §9 report JSON)
- `gold_set_result`
- `stability_result` (double-compile)
- `layer_b_labels` — **nullable/empty under the 3C.1 results-only model** (kept in the schema for a future
  formal-labeling phase; not populated by 3C.2a)
- `version_vector` (§3.3)
- `result` — `PASS | FAIL`
- `content_hash` (sha256 over the canonical assembled contents + version vector)
- `evaluated_at`

**`live_activation_decision`** — the human APPROVE/REVOKE, bound to one evaluation and this deployment:
- `decision_id`
- `evaluation_id` (FK → `enablement_evaluation`)
- `deployment_id`
- `decision` — `APPROVE | REVOKE`
- `decided_by`
- `reason`
- `decided_at`
- `supersedes_decision_id` (nullable — the prior decision this overrides)

Both are append-only (WORM revoke, mirror 0971). **APPROVE is permitted only over a persisted `result=PASS`
evaluation** (enforced server-side). REVOKE supersedes.

### 3.2 The enabled predicate (fail-closed)

```
enabled(deployment_id) =
      FEATUREGEN_INTENT_LIVE_CROSS_CATALOG == "1"
  AND a live_activation_decision exists for (deployment_id) that is APPROVE, is the latest
      (non-superseded/non-revoked) decision, references an enablement_evaluation with result=PASS,
  AND that evaluation's version_vector == the CURRENT server version vector (§3.3)
```
If the flag is on but the predicate is false (no approval / revoked / superseded / **version-vector
mismatch** — i.e. a planner/compiler/classifier/registry/gate/gold change since approval), the
considered-set and draft/confirm routes return a **readiness error before any dispatch**. Never a legacy
fallback. Re-approval (a fresh evaluation + decision) is required after any code-version change — the
"PASS is not permanent" property, delivered by the version match, **not** cryptographic signing.

### 3.3 The version vector — CODE versions only (not catalog state)

Server-assembled from the real constants; a change to any invalidates a prior approval:
- planner → `PLANNER_VERSION`
- compiler (the compile rules) → `PLAN_CONTRACT_VERSION`, `PHYSICAL_PLAN_VERSION`, `AGGREGATION_RULE_VERSION`,
  `ADDITIVITY_RULE_VERSION`, `TEMPORAL_RULE_VERSION`, `SAFETY_EVALUATOR_VERSION`
- contract-classifier / declaration side → covered by the compile-rule set above + `PLAN_CONTRACT_VERSION`
- reason-code registry → `REASON_CODE_REGISTRY_VERSION`
- recipe registry → `RECIPE_REGISTRY_VERSION`
- gate evaluator → `EVALUATOR_VERSION`
- gold set → `GOLD_SET_VERSION`

**The graph/catalog fingerprint is deliberately EXCLUDED** — catalog data churns continuously and is a
**per-plan** `ReplayFreshness` concern (§4/§6), not an activation concern; including it would force
re-approval after every ingestion. `producer_commit` is recorded for diagnosis but is **not** the semantic
activation key (the version vector + `deployment_id` are).

### 3.4 Deployment identity

`FEATUREGEN_DEPLOYMENT_ID` (e.g. `production-eu`) is a stable per-deployment identity. Approval is keyed by
`deployment_id + version_vector`, so a shared database or a copied environment **cannot inherit another
deployment's approval**. Call this the **activation scope / deployment scope** — NOT a "cohort" (there is no
user segmentation; identity is bound to read-scope only).

## 4. The plan envelope (carry-forward)

Each governed considered-set option carries a **plan envelope**, persisted as part of the server-side
`_snapshot` JSON (never a client payload). It is a projection of the selected `BindingPlanV1` + its
`PlannerReplayEnvelopeV1`:
- `recipe_id`
- `physical_plan_id`
- `generation_run_id`
- `catalog_sources` (participating catalogs)
- `ordered_path` (the plan's `path_segments`, ordered)
- `contract_id`
- `contract_resolution_status`
- `contract_reason_codes`
- `catalog_fingerprint` (the plan's per-catalog state stamps / fingerprints)
- `compiler_version` (the compile-rule version set)
- `input_stamps` (the `CatalogStateStampV1` set the plan was compiled against)

The chosen option binds to this envelope server-side. **Drafting must not recompute a permissive path from
the feature's columns.**

## 5. Authority is a structured field, not a lens

A `lens` is a recommendation *strategy/source* grouping; authority is a separate dimension. Folding them
(`lens="governed_cross_catalog"`) would entangle ranking, dedup, UI grouping, and future LLM-planner
integration. So every option keeps its semantic lens and gains structured provenance:
```json
{ "lens": "templates", "origin": "governed_planner",
  "path_authority": "governed_cross_catalog", "physical_plan_id": "pplan_...",
  "contract_resolution_status": "resolved" }
```
The UI may render "Governed path" from `path_authority`, but **code enforces authority from the structured
field + the persisted plan envelope, never from the lens name.**

## 6. Wiring + components

- **The flag + activation gate** — `contract/live_activation.py` (new): `deployment_id()`, the version-vector
  assembler, `is_live_cross_catalog_enabled(conn)` (the §3.2 predicate), and a typed readiness error. Read
  ONLY at the route boundary (the planner/builder stay pure). `api/routes/gate.py` (extend, authority-only):
  persist an `enablement_evaluation` (run the 3C.1 harness + content-hash) and record a
  `live_activation_decision` (APPROVE/REVOKE over a PASS).
- **Governed lens in the considered set** — `contract/gate1.py::build_considered_set` entity-scoped branch:
  when enabled, run `plan_bindings` over the eligible recipes; resolved plans → options with §5 provenance +
  a §4 envelope; unresolved → rejections with their reason codes; **cross-catalog LLM features → structured
  rejections with `GOVERNED_CROSS_CATALOG_PLAN_REQUIRED`** (single-catalog LLM untouched — a feature spans
  catalogs when its `derives_pairs` cover >1 catalog_source). `_snapshot` serializes the envelope per
  governed option.
- **Draft/confirm rebinding** — `contract/author.py::draft_contract` + `_join_path`: when the chosen option
  carries an envelope, use its `ordered_path` and skip `find_cross_catalog_path` entirely; recheck freshness
  via `ReplayFreshness` (envelope stamps vs current) → a **stale-plan result requiring regeneration** on
  drift, never a substitute path. The confirmation route rechecks freshness again before the contract is
  finalized.
- **Both-boundary enforcement** — (a) considered-set: no cross-catalog LLM option survives flag-on without a
  governed plan; (b) draft/confirm: a cross-catalog option without a **valid, current** persisted plan is
  rejected even if it somehow entered the snapshot (missing/tampered plan identity fails closed).
- **New reason code** `GOVERNED_CROSS_CATALOG_PLAN_REQUIRED` (a considered-set rejection reason).

## 7. Reconciliation with 3C.1

3C.1 chose **results-only** (compute + display the gate verdict, persist nothing). 3C.2a's activation model
requires a persisted, content-hashed PASS to bind a decision to — so **3C.2a adds the evaluation
persistence** (`enablement_evaluation`), owned here, not in 3C.1. 3C.1's gate *computation* is unchanged;
3C.2a layers persistence-for-activation on top by re-running the same `gate_operate`/`shadow_report`
harness server-side and hashing the assembled result. `layer_b_labels` is nullable/empty (3C.1 dropped
formal labeling); the field exists for a future phase.

## 8. Invariants preserved

- **F4:** the cross-catalog result is a contract DEFINITION (declared join), never an attested cross-catalog
  `approved_join`. Retiring the permissive path strengthens governance; F4 is never approached.
- **NO data plane:** definitions only, never computed values.
- **Behaviour-neutral flag-off:** byte-identical response, no new query/dispatch, flags read only at the route.
- **Fail-closed:** unapproved/stale activation → readiness error before dispatch; drift → regenerate; missing/
  tampered plan identity → reject; a governed rejection never falls back to the permissive path.
- **WORM:** the two activation tables + the snapshot are append-only; the migration is additive.
- `@dataclass(frozen=True, slots=True)` + lowercase-snake `StrEnum`. Model split (Fable impl / Opus review).

## 9. Acceptance tests

1. **Flag off** → the existing response and the existing draft path (`find_cross_catalog_path`), byte-identical.
2. **Flag on + approved** → resolved governed recipes surface with `path_authority=governed_cross_catalog` +
   a plan envelope.
3. **Unresolved governed recipes** appear as structured rejections with their reason codes.
4. **Cross-catalog LLM candidates cannot reach drafting** — held/rejected with
   `GOVERNED_CROSS_CATALOG_PLAN_REQUIRED` at the considered-set boundary.
5. **The draft path exactly matches the persisted governed plan** (`ordered_path`), not a recomputed one.
6. **Drift → regeneration**, not fallback (a stale plan yields a stale-plan result requiring regeneration).
7. **Missing or tampered plan identity fails closed** (a cross-catalog option without a valid/current
   envelope is rejected at draft/confirm).
8. **`find_cross_catalog_path` is never invoked while the flag is on** — a test replaces it with a function
   that RAISES and proves every flag-on cross-catalog considered-set, draft, and confirm path still succeeds
   or fails closed without invoking it.
9. **Activation prerequisite:** flag-on but no matching non-revoked PASS approval (or a version-vector
   mismatch) → readiness error before any dispatch; APPROVE is rejected server-side over a FAIL or a
   non-persisted evaluation; a copied deployment_id / mismatched version vector does not inherit approval.
   All **without** signing or an `authority_sign_gate` dependency.

## 10. Scope boundary — 3C.2b and later (NOT this phase)

- **No** `FeatureIdea → planner-intent → enumeration → compilation` adapter (that IS 3C.2b); 3C.2a *holds/
  rejects* cross-catalog LLM ideas, it does not govern them.
- **No** removal of the `find_cross_catalog_path` implementation — it stays for the flag-off path and for
  single-catalog `_join_path`; 3C.2a only guarantees it is never *invoked* on a flag-on cross-catalog path.
  Its outright removal is 3C.2b's final step, after LLM ideas are governed.
- **No** formal Layer-B label capture (deferred; the field is nullable).
- **No** per-user/per-role/per-catalog cohorting (no tenancy model); activation is deployment-scoped.
- **No** authoring surfaces that move recipes unresolved→resolved (that is Phase 3D).

## 11. Handoff to 3C.2b

3C.2b builds the idea→planner adapter, compiles every cross-catalog LLM candidate through the governed
planner (resolved → surface; unresolved → reject/hold), and then removes the permissive
`find_cross_catalog_path` implementation entirely — at which point the flag-on and flag-off cross-catalog
paths converge on a single governed standard and the flag can become the default.

# Four-stage gating and production certification — the pending plan

**Authority:** the product owner's rulings of 2026-08-22, quoted inline. Where this plan and a
ruling disagree, the ruling wins.

> *"If the backend can safely and honestly render the selected formula, show the user the code. Gold
> evaluation decides whether the generation method is certified for production — not whether code can
> be inspected."*

---

## 0. Where we actually are

**Done and green** (12 commits, suite 13132, tree clean at `cd055e49`):

| | |
|---|---|
| Certification machinery | evaluation contract (migration 1097), 12-case corpus, V2/V3 evaluation lane (1098), current-evaluation validity reader |
| Method matching | per-member provenance table (1099, **inert**), evidence-based `derive_authoring_method` (**no caller**) |
| Blockers removed | `grain_refs` reaches authoring; `build_set_revision.declaration_json` typed |
| Live schema | 1097 + 1098 applied to kind; **1099 is files only** |

**Not started:** every one of the four gating stages, the single decision service, gold's relocation,
the operator UI journey, and the end-to-end journey tests.

### Three facts that shape the sequence

1. **The readiness ladder has exactly ONE enforcing comparison** in production
   (`activation_policy.py:176`, `effective_readiness != MATERIALIZATION_READY`), gating
   `execute_materialization`. Everything else that reads readiness displays, sorts or reports it.
   **Removing gold from the ladder is measured to change ZERO served states** — only the three
   `gold_evaluation_unproven` blockers disappear.
2. **Candidate origin is NOT authoring method.** `formula_draft_worker` passes no
   `reviewed_blueprint`, so it ALWAYS drives the LLM author and critic. Every formula today is
   `LLM_AUTHORED` whatever the recommendation's origin.
3. ▲ **Therefore the deterministic recipe lane does not exist.** There is currently no formula a
   recipe-compiler certificate could apply to. See Decision D1.

---

## 1. Open decisions — these change the plan, and they are the owner's

* **D1 — Is the deterministic recipe-instantiation lane in scope?** The ruling says *"the LLM hit
  rate should not control this path if no LLM authored the formula"*, but no such path exists. Until
  one does, the recipe-compiler evaluation programme certifies nothing that is used. Options: build
  the lane (larger), or defer it and run one LLM programme for now (honest, smaller).
* **D2 — Does the old materialization route get deleted, or become a thin adapter?** The ruling
  allows either. Deletion is cleaner; the adapter keeps existing callers working while the journey
  tests are written.
* **D3 — Does `execute_sandbox` require anything gold-shaped at all?** The matrix says *"allow with
  warning"*. Confirm a warning is surfaced rather than silently dropped.

---

## 2. The phases

Ordered by dependency, not by size. **Phase B is the spine** — the later phases attach to it, and
doing it late means writing each gate twice.

### Phase A — the sealing writer *(step 1's other half)*
**Why first:** provenance must be written at sealing and can never be reconstructed
(*"another draft may have been created after the artifact, producing the wrong answer"*). Every hour
this is missing, artifacts are sealed without a recoverable authoring method.

* Establish where `selection_revision_id` / `formula_draft_id` are available at seal time.
  `AdmittedFeatureV2` already carries `feature_name`, `proposal_content_hash`, `authoring_run_id`;
  the other two are **not** on it and may need threading through `compile_generation_v2`.
* Call `derive_authoring_method` per member; write one row per member; **fail the seal loudly** when
  the method is undecidable or a selection cannot be established. Never default, never skip a member
  — a missing row later reads as "nothing to check".

**Proof:** a real provider-authored run seals with an `LLM_AUTHORED` row carrying real dispatch
evidence; an undecidable run refuses the seal by name.

### Phase B — the four statuses and ONE decision service ▲ THE SPINE
* Split the single `MATERIALIZATION_READY` into **formula / preview-code / sandbox / production**
  statuses.
* Build `evaluate_action(selected_features, action)` →
  `{allowed, blockers, warnings}` for
  `generate_preview | execute_sandbox | publish_sandbox | publish_production`.
* **Route BOTH the old and new paths through it.** The ruling: *"Both old and new APIs must receive
  the same answer"* and *"do not keep two independent readiness implementations."* This is what fixes
  the inconsistency where the same feature gets two answers depending on which route the UI calls.

**Proof:** a table-driven test over the owner's blocker matrix (below), asserted through BOTH routes.

### Phase C — `PUBLISH_PRODUCTION` + gold relocation ▲ ATOMIC, ONE COMMIT
> *"Do not merge 'gold removed from readiness' without simultaneously introducing the production
> publication gate; that would temporarily remove the protection instead of relocating it."*

* Remove gold from `recipe_readiness` — **keep `BLOCKER_GOLD_UNPROVEN` in the fold-owned set** so
  legacy rows strip it rather than re-entering it as a governed policy blocker (which would pin every
  legacy candidate at `FORMULA_BLOCKED`, strictly worse).
* Add `evaluate_publish_production(artifact, verification, publication_capability,
  authoring_certification, feature_governance)` requiring: current artifact verification · production
  publish permission · production capability attestation · data-use and read authorization · a
  **method-matched** current certificate · **the exact certificate revision recorded on the attempt**.
* Add a **method-level** certificate reader. `current_evaluation_validity` is expectation-specific
  and cannot answer for a novel LLM feature with no recipe expectation. The question is
  *"was this exact LLM authoring configuration certified by the platform-wide gold corpus?"*
* Fold all artifact members **all-must-pass**.
* ▲ **The gate does NOT go on the current publish path** — that path is `PUBLISH_SANDBOX` on the
  `sandbox_feature` namespace. Putting it there blocks sandbox testing. `chain.py:646`'s pattern is
  publication-*mechanism capability*, not certification. **Leave verification and `PUBLISH_SANDBOX`
  unchanged.**

**Hard-block from day one. No transitional "certification required only once a certificate exists"
rule** — absence would act as permission, and earning the first certificate would make the platform
stricter than before.

### Phase D — the operator journey (Governance → Formula quality)
UI page → `POST /formula-evaluations` → queue runner over all 12 frozen cases → progress endpoint →
results report. Cost-confirmed before starting (calls, token budget, max cost); **the backend reads
the deployed configuration — the operator never types prompt hashes.** Outcomes:
`CERTIFIED_CURRENT · PASSED_NOT_CERTIFIABLE · FAILED_QUALITY · FAILED_TECHNICAL · STALE`.
Two programmes (LLM authoring / recipe compiler) — scope depends on **D1**.

**The V2 corpus runner is the critical missing piece:** the backend can describe and score an
evaluation, but nothing walks the 12 cases as a user-triggered job.

### Phase E — the journey tests ▲ BEFORE removing the old path
Both origins, end to end: hypothesis → recommendation → selection → formula → preview project →
inspect code → sandbox verification → attempt production.

Must prove: gold pending does **not** block preview · gold pending **does** block production ·
target leakage blocks preview · missing currency/reversal blocks preview · recipe and LLM features
build together (**mixed-method**) · stale certificate refused · missing provenance refused ·
**every API gives the same decision**.

### Phase F — cutover
Make the build-set workflow canonical; delete the old materialization route or reduce it to a thin
adapter (**D2**); fix the known build-set authorization gaps (authenticated roles, authoritative
metadata, policy binding, queue/build-set integrity).

---

## 3. The blocker matrix — the contract Phase B implements

| Problem | Formula | Preview code | Sandbox | Production |
|---|---|---|---|---|
| **Gold evaluation pending** | Allow | **Allow + warning** | **Allow + warning** | **BLOCK** |
| Missing customer relationship / grain | Block | Block | Block | Block |
| Target leakage | formula visible | Block | Block | Block |
| Unsupported renderer operation | formula visible | Block | Block | Block |
| Missing currency / reversal policy | formula visible | Block | Block | Block |
| User lacks read permission | formula visible | Block | Block | Block |
| Artifact not verified | Allow | Allow | Depends | Block |

---

## 4. Risks

* **Phase C's atomicity is the one that bites.** Landing the ladder change alone removes protection
  rather than relocating it. This has already been attempted once and reverted.
* **Placement.** A gate put on the sandbox path blocks sandbox testing. When a change makes many
  tests on ONE path fail, treat that as a placement signal before treating it as a product question.
* **Weak tests.** Tests that construct their own fixtures and assert them back prove nothing here.
  Drive the real path; reintroduce each defect and confirm the test fails; **verify the injection
  applied** and read pytest's summary line (a `grep -c "^FAILED"` silently matches nothing against
  coloured output and reports a false pass).
* **Nothing is exercisable live** until a deploy: the cluster image predates migration 1094.

## 5. Not engineering, and not blocking these phases

Nine expert sign-offs to grow the reviewed corpus past its single clean case (approved
`recipe_review_event` rows from every required role). Until then no evaluation is *certifiable*, so
production stays blocked — which is the intended invariant, not a defect.

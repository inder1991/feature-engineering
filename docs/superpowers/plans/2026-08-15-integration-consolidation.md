# Integration consolidation — one plan for every open finding

**Date:** 2026-08-15
**Supersedes:** `2026-08-15-recognition-seam-review-remediation.md` (`a6c7cd77`) — its seven tasks are
absorbed below as **D1–D3, C4, A1, A3, H1**. That plan fixed one seam; these findings say the seams
do not join up, and fixing them separately would sequence the work wrongly.

Two adversarial reviews, **23 findings**. Every one was checked against the code before it was
sequenced here. Where a claim was reproduced, the evidence is cited; where it was accepted on the
reviewer's evidence without independent reproduction, it says so.

## 0. The one sentence that reorganises everything

> The product journey ends at **"governed definition"**, not **"working feature"** — and the single
> path onward is either dead, unreachable from the UI, or able to execute something other than what
> was approved.

Three findings compose into that, and they are the release blockers:

- **A2/F1** — an approved option and the formula that actually executes can be **different
  parameterizations of the same recipe**.
- **C1/F2** — the UI has no way to start materialization at all.
- **C2/F3** — the second authoring path calls an endpoint that is permanently `409`.

Everything else in this plan is either a consequence of the same missing concept (**one executable
identity**) or a truthfulness debt around it.

## 0.1 Findings, as validated

**Reproduced directly:**

| Finding | Evidence | Verdict |
|---|---|---|
| Approved option ≠ executed formula | `gate1.py:828` documents "ONE context per served RECIPE, at its **LEADING** variant" while B5 serves one card per parameterization; `resolve.py:219` names the intent `str(recipe_id)`, losing the variant; `_require_every_member_exists` is "One existence read — NOT a compile"; the option key is optional and never reconciled with the work items | **CONFIRMED — blocker** |
| UI cannot start materialization | `api.ts` has only `GET /materialization-runs/{id}`; no create function | **CONFIRMED — blocker** |
| "Write definitions" is dead | `assist.py` `_refuse_bypass` is unconditional post-E4; `api.ts:2083` still POSTs `/features/recipe` | **CONFIRMED — blocker** |
| Frontend does not build | 3 × TS2739 in `WorkbenchScreen.test.tsx` under `npm run typecheck` (`tsc -b`); `tsc --noEmit` passes, which is why the seam's rows wrongly claim clean | **CONFIRMED — blocker** |
| Recognition partial recovery unreachable | cap counts `relationship == "primary"` over raw candidates before validity (`recognition.py:523-540`); and schema v2's enum makes the incident `schema_invalid`, so partition never sees it | **CONFIRMED — blocker** |
| Variant explosion + name collisions | measured: **940 variants, 317 display names, 918 colliding, 295 labels affected**; worst 15 and 12. The enumerator's own docstring says "≈940" | **CONFIRMED** |
| No set-level validation | `WorkbenchScreen.tsx:2435` — "there is NO set-level re-check of the human's mix (tracked follow-up)" | **CONFIRMED** |
| LLM ideas have no promotion path | `activation_policy.py:130` refuses `conceptual_pattern` and says "the formula seam is what promotes it" — while the same policy gates `author_formula` | **CONFIRMED — circular** |
| Multi-table refused without consulting a join | `fold_frozen_binding_plan` returns `RELATIONSHIP_REQUIRED` on `cross_dataset`; the module contains **zero** references to `approved_join`/`join_path` | **CONFIRMED** |
| Cross-catalog unreachable | `contract.py:739` refuses entity-only cross-catalog by design | **CONFIRMED** |
| Leakage claim overstated | the hard check is `verdict.selected_ref == target_ref` — exact-reference equality only (`typed_gauntlet.py:103`) | **CONFIRMED** |
| Probe has no production caller | `probe_publication_capability` referenced only in its own module and a docstring | **CONFIRMED** |
| Client declares published schema | `published_schema: list[str] \| None` is a caller field on the POST body | **CONFIRMED** |
| Schema identity ≠ executed contract | `_require_schema` returns the mutable registry's body, never compared to the canonical digest | **CONFIRMED** |
| Closed vocabulary unenforced | `CandidateDrop` documents the invariant; nothing validates it | **CONFIRMED** |
| Idempotency is sequential only | lookup → provider → `ON CONFLICT DO NOTHING`, no lock or re-check | **CONFIRMED** |
| Paid gate neither durable nor capped | no `commit()` in the module; run creation is a nested savepoint; the loop consults no budget | **CONFIRMED** |
| Release not provider-qualified | the seam plan's own Task 6 row | **CONFIRMED** |

**Accepted on the reviewer's evidence, not independently reproduced:** human confirmation blocking
exploratory execution (execution tiers), stranded `requested` rows (previously recorded as A.35),
worker-tick contention, the publication consistency window, and the post-publication gap
(`model_input` deferred at `render/project.py:113`).

**One correction:** ruff reports **79 errors repo-wide, 35 under `src/`**. Immaterial to the point.

## 0.2 Done-bar

> One customer feature and one transaction feature travel **hypothesis → candidate → the exact
> approved parameterization → sandbox Kedro run → Hive table → validation**, started from the UI,
> with the run's provenance naming the precise option that was approved — and no claim on any screen
> outruns what was actually checked.

## 1. Part A — stop the bleeding

### A1 — the build is green under the project's own scripts (½ day)
Three fixtures gain `recognition_quality: null` / `ambiguity_note: null`; extract one typed fixture
factory. **Every frontend gate in every plan and brief now names `npm run typecheck`, `npm test`,
`npx oxlint`** — `tsc --noEmit` is banned as a gate, having twice reported green over a red build.

### A2 — an approved option can only execute its own formula (3 days) — **release blocker**
The narrow fix, ahead of Part B's full identity: **the route reconciles the governed option key with
the submitted work items and refuses a mismatch**, and the authoring intent's name carries the
variant, not the bare `recipe_id`. Where a work item exists only for the leading variant, the
non-leading option is **refused by name** — never silently executed at another parameterization.

*Acceptance:* approving "Balance slope — 180 days" and submitting the 90-day work item **refuses**;
a matched pair proceeds; a run's recorded option and its executed formula are asserted equal.

### A3 — recognition partial recovery actually works (2½ days) — **schema v3**
Membership moves out of the wire schema into the semantic validator (still driving repair via Task
2's arm); `partition_candidates` drops candidate-local invalids **before** applying aggregate caps.
Two genuinely valid primaries still refuse. Full reasoning in the superseded plan's §0.2.

*Acceptance:* the verbatim incident body, repair exhausted → `CLASSIFIED` on churn with one drop
recorded — replacing the test that currently asserts the opposite.

## 2. Part B — one executable identity

The concept whose absence produces A2, the variant collisions and the missing set validation.

### B1 — `ExecutableFeatureRevision` (4 days) — **migration**
Durable, carrying: considered revision + option id, **exact parameter binding**, governed contract
id/version, formula content hash, physical-plan hash, canonical output name, formula work-item id.

### B2 — canonical parameterized identity (2 days)
`balance_slope_30d` / `_90d` / `_180d`. The parameter binding enters the identity **and** the display
name. 918 variants currently share 317 labels; after this, none do.

### B3 — materialization accepts a revision, not raw work-item ids (2 days)
The A2 reconciliation becomes structural: there is nothing to mismatch.

### B4 — set-level validation before governance (2 days)
Duplicate output names, incompatible grain/cadence, conflicting join assumptions, features that
cannot publish together — checked at the set, not discovered by materialization. Closes the
`:2435` follow-up the UI already admits to.

## 3. Part C — the journey completes

- **C1 — the UI starts and follows a run (3 days).** A create function, a "Build in sandbox" action
  after governance, and queued → compiling → validating → running → published/refused in the
  workspace, linked to the exact candidates. *(Deployment enablement stays an operator act.)*
- **C2 — "Write definitions" through the semantic pipeline (3 days).** Reimplemented against the real
  planner; the dead `/features/recipe` call removed. **Frontend tests must stop mocking success on a
  permanently-409 endpoint** — that mock is why this survived.
- **C3 — publication capability is earnable (1½ days).** A supported operator path that runs the
  probe and stores the attestation. Today the refusal is correct and undischargeable.
- **C4 — stranded requests reach a terminal (1 day).** The `requested → failed` edge with the
  reconciler as its only writer (previously recorded as A.35 and deferred to the surface).

## 4. Part D — claims match reality

- **D1 — recorded identity is the executed contract (1½ days).** Immutable registration for
  `use_case_recognition`; dispatch compares resolved and canonical digests and refuses a mismatch;
  the evaluator verifies `schema_content_hash`; repair/retry policy versions enter request identity.
- **D2 — the closed vocabulary is enforced (1 day)** at construction and persistence; corrupt legacy
  data reads as `null` plus a diagnostic.
- **D3 — one provider call per request under concurrency (1 day).** Advisory lock over
  `(intent_id, request_hash)` + a second lookup before dispatch.
- **D4 — leakage claims narrowed to what is checked (1 day).** The UI stops saying "structurally safe
  against leakage" while only exact-reference equality is enforced. **Copy changes now; the stronger
  checks are D6.**
- **D5 — the worker reads the live schema (1 day).** `published_schema` stops being a caller
  assertion about physical cluster state.
- **D6 — real leakage checks (4 days).** Target-lineage closure, post-outcome stage checks,
  availability-vs-cutoff, proxy warnings. LLM critic stays advisory.

## 5. Part E — sandbox exploration vs production promotion

Resolves the standing product direction: AI-proposed evidence should be explorable, with visible
provenance, while promotion carries stronger gates.

- **E1 — an execution tier on the materialization job (3 days).** SANDBOX vs PRODUCTION exists
  internally; the HTTP surface exposes none and compilation defaults to production.
- **E2 — a promotion path for LLM ideas (3 days).** Today `conceptual_pattern` is refused by the
  policy whose own message names the formula seam as the promoter — a closed loop.

## 6. Part F — reach

- **F1 — governed joins reach the planner (4 days).** Multi-table within one catalog: consult the
  governed join instead of refusing `cross_dataset` outright, so governing a relationship and
  regenerating actually changes the answer.
- **F2 — cross-catalog becomes reachable (4 days).** Including `BridgeExecutionAuthorization` through
  the chain. **This is what the verified-but-unrealized customer bridge needs**, and what most of the
  787 missing-operand candidates are waiting on.

## 7. Part G — operability

- **G1 — a dedicated materialization worker pool (2 days).** Compilation and L0 currently share a
  single-threaded tick with relay, timers, projections and ingestion.
- **G2 — publication consistency (3 days).** Intent/apply/confirm or reconciliation for the window
  between the control-plane row and the metastore swap.

## 8. Part H — release qualification

- **H1 — the paid gate is durable and capped (2 days).** Commit before any provider call; resumable;
  budgets checked *before* each recognition; `budget_exhausted` reported distinctly; rename any field
  that is a threshold rather than a ceiling.
- **H2 — qualify the release (operator).** Add the motivating churn case to a reviewed corpus, get
  gold labels expert-reviewed, then run the 100-case gate. **Explicit approval required — real
  spend.** Until then the honest status is *"repair-loop and observability implementation complete,
  release qualification incomplete."*

## 9. Explicitly NOT in this plan

Output EDA/profile history, drift summaries, backtesting, the model tournament, model-input assembly
and feature selection. `model_input` is deferred at `render/project.py:113` and stays deferred: none
of it is worth building on a journey that cannot yet execute the feature that was approved.

## 10. Sequencing

```
A1 ─► A2 ─► A3            (blockers; A1 first, it blocks all frontend work)
        │
        ▼
B1 ─► B2 ─► B3 ─► B4      (one identity; B3 makes A2 structural)
        │
        ├─► C1 ─► C2 ─► C3 ─► C4      (the journey completes)
        ├─► D1..D5 ─► D6              (claims match reality; D4 copy now, D6 later)
        ├─► E1 ─► E2                  (sandbox vs production)
        └─► F1 ─► F2                  (reach: multi-table, then cross-catalog)
                        │
                        ▼
                  G1, G2 ─► H1 ─► H2 ⟨operator⟩
```

**≈ 55 focused days.** The first honest milestone is **A + B + C1**: one approved feature executes
*its own* formula, started from the UI. That is the vertical slice the second review asks for, and
nothing after it is safe to prioritise before it.

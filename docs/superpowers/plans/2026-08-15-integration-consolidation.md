# Integration consolidation — one plan for every open finding

**Date:** 2026-08-15 · **Revision 2** — restructured around the product owner's direction of
2026-08-15, whose release ordering and success criterion this plan now adopts verbatim.

**The whole goal, in one line:**

> When you choose a feature, the system must build **that exact feature** on Hadoop and show you the
> result.

**The first success, stated concretely enough to be unarguable:**

> You select `balance_slope_90d`, click **Build in sandbox**, Kedro runs on Hadoop, Hive gets exactly
> `balance_slope_90d`, and the UI shows its output profile.

## 0.0 What the owner's direction changed in this plan **[R2]**

Two additions are better than what revision 1 had, and both are adopted:

1. **A frontend↔backend contract test (Release 0).** Revision 1 fixed the dead `/features/recipe`
   call as a one-off (C2). It did not fix the *class*: frontend tests mock a success that the backend
   permanently refuses, so any future retirement re-creates the bug invisibly. A test that checks the
   real client's calls against the real server's routes turns that into an automatic failure. **This
   is now R0-2 and runs before everything else.**
2. **Build formulas only for SELECTED features.** Revision 1 fixed the leading-variant divergence by
   reconciling keys at the route (A2). The owner's fix is better and cheaper: stop speculatively
   capturing a formula per recipe at generation, and capture the exact parameterization *after* the
   human selects it. That removes the divergence **at the source** rather than detecting it at the
   boundary — there is no leading variant to diverge from. Reconciliation stays as the belt-and-braces
   check. **This is now R1-2.**

Two scope changes follow: **output profiling moves INTO this plan** (owner's step 9) where revision 1
had deferred it, and **leakage strengthening moves BEFORE the first Hadoop run** (owner's step 7),
because a first real run that quietly uses a leaky column teaches the wrong lesson.

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

## 1. Releases

Structure and order are the owner's. Every finding from both reviews is mapped to a release; nothing
is dropped, and where a finding is deliberately deferred it says so.

### Release 0 — the codebase is healthy (1 day)
- **R0-1 — the build is green.** *(DONE `5feebf09`: three `RecognitionResp` fixtures now come from
  one typed factory defaulting `recognition_quality` to `null` — the honest legacy state.)*
  **`tsc --noEmit` is banned as a gate**; every plan and brief names `npm run typecheck`, `npm test`,
  `npx oxlint`, run from `frontend/`. It reported green over a red build twice.
- **R0-2 — a frontend↔backend contract test.** The real client's routes are checked against the real
  server's, so a call to a retired endpoint fails automatically. Today `api.ts:2083` POSTs
  `/features/recipe`, which `_refuse_bypass` refuses unconditionally, and the frontend tests mock a
  success — this test is what makes that class impossible.
- **R0-3 — ruff clean.** 79 repo-wide, 35 under `src/`.

### Release 1 — an approved feature is bound to its exact formula (7 days) — **migration**
- **R1-1 — `ExecutableFeatureRevision`.** Considered revision + option id, **exact parameter
  binding**, contract id/version, formula content hash, physical-plan hash, canonical output name,
  work-item id. Verified before execution.
- **R1-2 — formulas are built only for SELECTED features** *(the owner's fix, adopted over revision
  1's)*. Generate ideas → human selects → author formulas for the selection → verify → prepare.
  Speculative per-recipe capture at its "leading variant" is what allows an approved 90-day option to
  execute a 30-day formula; removing the speculation removes the divergence at source, and is cheaper.
- **R1-3 — canonical parameterized identity.** `balance_slope_30d` / `_90d` / `_180d` in identity and
  display name. Today **940 variants share 317 labels; 918 collide across 295 labels.**
- **R1-4 — materialization accepts a revision, not raw work-item ids**, with key reconciliation kept
  as belt-and-braces.
- **R1-5 — recognition partial recovery** (schema v3: membership semantic, not enum; drop invalids
  before applying caps). Reasoning in the superseded plan's §0.2.

### Release 2 — the user runs it from the UI (6 days)
- **R2-1 — "Build in sandbox"** after governance, with staged progress: preparing formula → compiling
  → generating Kedro project → validating → running on Hadoop → checking output → publishing sandbox
  table → creating profile. Linked to the exact candidates.
- **R2-2 — "Write definitions" reimplemented** through the semantic planner; the dead call removed.
- **R2-3 — publication capability earnable**: a supported path that runs the probe and stores the
  attestation. Today the refusal is correct and undischargeable.
- **R2-4 — no stranded requests**: `configuration failed / waiting for retry / cancelled / running /
  failed / published`, and the same logical request is retryable after a fix.

### Release 3 — sandbox exploration, production promotion (6 days)
- **R3-1 — an execution tier on the job.** SANDBOX/PRODUCTION exists internally; the HTTP surface
  exposes none and compilation defaults to production.
- **R3-2 — AI-proposed metadata runs in sandbox**, with the provenance shown as the owner wrote it:
  *"This feature uses an AI-proposed customer identifier mapping. Evidence: matching names, types and
  data overlap. Human confirmation: not recorded."* Production keeps the stronger gates.
- **R3-3 — a promotion path for LLM ideas.** `conceptual_pattern` is refused by the policy whose own
  message names the formula seam as its promoter — a closed loop.

### Release 4 — the selection is checked as a group (2 days)
Grain, cadence, duplicate output names, access restrictions, point-in-time rules, join safety,
population source — **before** anything runs, with grouping recommended rather than refused
("Group 1: customer daily; Group 2: customer monthly; Group 3: account daily"). Closes the missing
set-level re-check the UI already admits to at `WorkbenchScreen.tsx:2435`.

### Release 5 — features over more than one table (8 days)
- **R5-1 — one-hop governed joins reach the planner**: find the relationship, check uniqueness,
  determine fan-out, choose the population, freeze the join in the plan, **refuse when fan-out safety
  is unknown**. Today `fold_frozen_binding_plan` refuses `cross_dataset` outright and the module never
  consults a governed join, so governing a relationship changes nothing. Chains come later.
- **R5-2 — cross-catalog via the bridge**, recording which bridge, which columns, cardinality,
  evidence, whether a human confirmed, and sandbox- vs production-safety. This is what the
  **verified-but-unrealized `cust_num` ↔ `cif_id` bridge** needs, and what most of the 787
  missing-operand candidates are waiting on.

### Release 6 — leakage detection worth the claim (5 days)
Target-derived columns, post-cutoff information, post-outcome events, target-defining status flags,
availability at prediction time. **Deterministic rules decide; the LLM only warns.** Until they exist
the UI's "structurally safe against leakage" is narrowed **immediately** — today the only hard check
is `selected_ref == target_ref`.

### Release 7 — Hadoop execution is reliable (7 days)
- **R7-1 — the worker reads Hive**, not the browser: table existence, real columns, partitions,
  published generation, supported publication mechanism. `published_schema` stops being a caller
  assertion about cluster state.
- **R7-2 — a dedicated materialization worker.** Compilation, Kedro and Hadoop execution leave the
  tick shared with relay, timers, projections and ingestion.
- **R7-3 — publication consistency** across the control-plane row and the metastore swap
  (intent/apply/confirm, or reconciliation).
- **R7-4 — identity and durability debts**: canonical-vs-registry schema digest compared at dispatch;
  closed vocabularies enforced where documented; single-flight idempotency; the paid gate committed
  before spend and capped during it.

### Release 8 — the first real feature (operator)
`balance_slope_90d` end to end. **Requires explicit go** — cluster spend, and the inventory capture
that is still unstarted.

### Release 9 — output profiling (3 days)
Row count, null %, min/max/mean/median, duplicate key count, last refreshed. **Summaries only —
customer-level data never leaves Hadoop.**

### Release 10 — is the feature actually useful (deferred, charter later)
LightGBM/AUROC over single features and sets, feeding back into ranking. `model_input` is deferred at
`render/project.py:113` and stays deferred until Releases 0–9 hold.

## 2. Sequencing

```
R0 ─► R1 ─► R2 ─► R3 ─► R4 ─► R5 ─► R6 ─► R7 ─► R8 ⟨operator⟩ ─► R9 ─► R10 ⟨later⟩
```

**≈ 45 focused days to Release 7**, plus the operator's cluster work. The order is the owner's, with
one note: **R6 (leakage) lands before R8 (the first real run)** deliberately — a first run that
quietly uses a leaky column would teach exactly the wrong lesson about what the platform guarantees.

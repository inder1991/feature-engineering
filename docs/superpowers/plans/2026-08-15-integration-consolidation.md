# Integration consolidation — the exact-feature vertical slice

**Date:** 2026-08-16 · **Revision 3** — restructured after a review found revision 2 internally
contradictory. Kept: the goal, the scope, every finding. Replaced: the linear R0–R9 ordering.

**The whole goal:**

> When you choose a feature, the system must build **that exact feature** on Hadoop and show you the
> result.

**The first success:**

> You select `balance_slope_90d`, click **Build in sandbox**, Kedro runs on Hadoop, Hive gets exactly
> `balance_slope_90d`, and the UI shows its output profile.

## 0.0 Why revision 2 was not implementable **[R3]**

Its Release 2 promised a Hadoop build with staged progress and a profile — while the sandbox
execution tier was R3, group validation R4, live Hive inspection R7, and profiling R9. **The plan's
own first-success statement required a profile that the plan implemented two releases later.** A
linear ordering cannot express a vertical slice; the slice needs a thin piece of each layer, not all
of one layer before the next.

Revision 3 is therefore organised around **five load-bearing concepts**, in dependency order:

> `feature_key` → **build request** → **executable revision** → **materialization group revision** →
> **profiled run**

### What the review found, validated **[R3]**

| # | Finding | Verified | Verdict |
|---|---|---|---|
| 1 | A route-existence contract test **passes while the UI stays broken** — `/features/recipe` exists and refuses every call with 409 (`assist.py` keeps the route deliberately: *"a 404 would tell a client it had the wrong address"*) | reproduced | **CONFIRMED** |
| 2 | "Author after selection" has no durable workflow, and the machinery it replaces **defaults off**: `recipe_formula_shadow_enabled()` reads `FEATUREGEN_RECIPE_FORMULA_SHADOW` defaulting `"0"` | reproduced | **CONFIRMED** |
| 3 | **Canonical identity lands too late.** `govern.py:609` selects the latest contract `WHERE feature_name = %s`, with the comment *"ONE feature per feature_name — re-confirm reuses + refreshes the feature (no proliferation)"*. Three approved "Balance slope" variants become **versions of one feature**, by design | reproduced | **CONFIRMED — worse than assumed** |
| 4 | **A group cannot be represented.** The run is keyed by `logical_group_name` and publication is *"atomic per group … never per-feature"*, yet only ONE `(considered_revision_id, option_id)` pair is recorded | reproduced | **CONFIRMED** |
| 5 | Release order contradicts the Release 2 promise | self-evident in revision 2 | **CONFIRMED** |
| 6 | **Sandbox still requires a confirmer** — `require_confirmer` gates every `POST /materialization-runs`, and compilation defaults to PRODUCTION | reproduced | **CONFIRMED** |
| 7 | Set validation lands after the build it should gate | self-evident | **CONFIRMED** |
| 8 | Live cluster facts stay caller assertions (`published_schema` on the POST body) until R7 | reproduced earlier | **CONFIRMED** |
| 9 | One-hop joins and cross-catalog bridges are different increments; the chain refuses cross-catalog for want of `BridgeExecutionAuthorization` | reproduced earlier | **CONFIRMED** |
| 10 | Recognition absorption is **traceability-only** — revision 2's labels (D1–D3, C4, A1, A3, H1) name a structure it deleted | self-evident | **CONFIRMED — my defect** |
| 11 | Leakage narrowing was promised "immediately" with **no task assigned to do it** | self-evident | **CONFIRMED — my defect** |
| 12 | Profiling is a metric list, not a durable contract | — | **CONFIRMED** |

## 1. Stages

### S1 — integrity gate (3 days)
- **S1-1 — behavioural, not route-existence, contract tests.** For every UI operation, call the real
  backend handler and assert the **capability is supported** — not that the URL appears in OpenAPI.
- **S1-2 — hide/disable "Write definitions"** until S7 reimplements it. A visible control whose only
  outcome is 409 is the defect; removing the call is the fix, not mocking it.
- **S1-3 — narrow the leakage wording NOW.** The UI stops claiming "structurally safe against
  leakage" while the only hard check is `selected_ref == target_ref`. *(Revision 2 promised this and
  assigned it nowhere.)*
- **S1-4 — recognition correctness, in full, here.** Partial recovery (schema v3, membership semantic,
  drop invalids before caps), canonical-vs-registry digest compared at dispatch, closed vocabularies
  enforced, single-flight idempotency, paid-gate durability + real spend caps. **Recognition is the
  front door; it does not wait behind a Build button.**
- **S1-5 — ruff ratchet.** 79 repo-wide / 35 in `src/`: fix or ratchet, never leave it ambient.

### S2 — exact selected-feature identity (6 days) — **migration**
- **S2-1 — `feature_key` vs `display_label`.** `balance_slope_90d` as immutable machine identity;
  *"Balance slope · 90 days"* as human text. **Underscore identifiers never appear as display text.**
  **Applied at CONTRACT CREATION**, not only at executable-revision time — otherwise `govern.py`'s
  name lookup keeps collapsing variants into versions of one feature.
- **S2-2 — `ExecutableFeatureBuildRequest`** — mutable, evented: `queued → authoring → refused /
  failed / ready`. This is what selecting a feature *creates*, and where retries, concurrency and
  idempotency live.
- **S2-3 — `ExecutableFeatureRevision`** — immutable, minted **only after** the selected formula
  verifies: option id, exact parameter binding, contract id/version, formula hash, physical-plan hash,
  canonical output name, work-item id.
- **S2-4 — author from the selected option's canonical typed parameter payload**, replacing
  speculative leading-variant capture. **Explicit cutover** — the shadow machinery defaults off and
  must not be assumed live.
- **Legacy:** contracts are immutable, so ambiguous existing ones stay **legacy/non-executable** until
  explicitly mapped or reconfirmed. They are never silently rewritten.

### S3 — the materialization group contract (5 days) — **migration**
- **S3-1 — `materialization_group_revision` + `materialization_group_member`**, one member per
  executable revision; the run references `group_revision_id`; **the server derives work items and
  membership — clients never submit arbitrary work-item lists.**
- **S3-2 — group compatibility validated before a build is accepted**: grain, cadence, duplicate
  output names, access classification, point-in-time rules, join safety, population spine. Recommend
  groupings rather than refusing outright.
- **S3-3 — execution tier derived SERVER-SIDE** from operation and destination namespace; a caller
  may not ask for PRODUCTION. Sandbox admits AI-proposed evidence **with visible provenance**;
  production keeps the stronger gates. `require_confirmer` no longer gates a sandbox build.

### S4 — the first functional vertical slice (10 days)
One exact, single-table feature — `balance_slope_90d` — all the way through, with **a thin piece of
every layer it needs**:

UI "Build in sandbox" → exact formula → compile → generated Kedro project → **live Hive schema
inspection** (server-side; `published_schema` stops being a caller assertion) → **capability probe**
(a supported path that earns and stores the attestation) → Hadoop submission → validation → atomic
sandbox publication → **minimal profile**.

Plus **no stranded requests**: `configuration failed / waiting for retry / cancelled / running /
failed / published`, retryable as the same logical request.

**S4 is the done-bar.** Everything before it exists to make it true; everything after it widens it.

### S5 — single-catalog multi-table (6 days)
One-hop governed joins: find the relationship, check uniqueness, determine fan-out, choose the
population, freeze the join in the plan, **refuse when fan-out safety is unknown**. Chains later.

### S6 — cross-catalog (6 days)
Bridge realization + `BridgeExecutionAuthorization` pinned into the run, recording bridge revision,
joined columns, cardinality, evidence, human confirmation, and sandbox- vs production-safety. This is
what the verified-but-unrealized `cust_num` ↔ `cif_id` bridge needs.

### S7 — broader capability (12 days)
LLM-idea promotion (today `conceptual_pattern` is refused by the policy whose own message names the
formula seam as its promoter); **"Write definitions" reimplemented** through the semantic planner;
richer leakage detection with **deterministic outcomes — `PASS` / `REFUSE` / `UNRESOLVED`** over
prediction cutoff, horizon, target-lineage closure, event-vs-knowledge time and prohibited lifecycle
stages, the LLM explaining but never deciding; multi-feature profiles; production promotion; a
dedicated materialization worker; publication consistency across the control-plane row and the
metastore swap.

### S8 — predictive evaluation (charter later)
Model-input assembly, LightGBM/AUROC over single features and sets, feeding back into ranking.

## 2. Contracts this plan owes, stated rather than implied **[R3]**

- **`feature_output_profile`** keyed by materialization run, group revision, executable revision,
  business date and profile-schema version; inherits the group's access classification; **computed
  inside Hadoop, summaries only across the boundary**; a profile failure is recorded separately and
  **never falsely fails an otherwise validated publication**.
- **Build request vs revision** — the mutable lifecycle and the immutable artifact are different
  records. Revision 2 defined only the artifact, leaving failure, retry, concurrency and idempotency
  undefined.
- **Leakage verdicts** — `PASS` / `REFUSE` / `UNRESOLVED`, with the behaviour on missing evidence
  stated, not inferred.

## 3. Sequencing

```
S1 ─► S2 ─► S3 ─► S4 ⟨the done-bar⟩ ─► S5 ─► S6 ─► S7 ─► S8 ⟨later⟩
```

**≈ 30 days to S4**, then widening. The change from revision 2 is not scope but shape: a vertical
slice that runs one real feature end to end, rather than eight horizontal layers that only meet at
the end.

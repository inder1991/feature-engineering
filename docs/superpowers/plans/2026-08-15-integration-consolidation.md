# Integration consolidation — capability-grounded, exact-feature execution

**Date:** 2026-08-16 · **Revision 4** — rewritten after a review showed revision 3 started from the
desired UI outcome and skipped the executable facts beneath it. Goal and scope unchanged. **The
30-day estimate is removed** until R4-0 resolves the pilot, the execution environment and the
Formula-v2 path.

**The goal:** when you choose a feature, the system builds **that exact feature** and shows the result.

## 0.0 Why revision 3 was not executable **[R4]**

It named `balance_slope_90d` as the first vertical slice. Measured against the code, that single
choice smuggles in a new execution architecture, a new statistical operation and a data onboarding:

| Gate | Reality | Verified |
|---|---|---|
| Admission accepts the schema | **Formula-v1 ONLY** — *"A v2 (or any-other-version) formula reaching admission is refused loudly"* | `admission.py:228-237` |
| Engine advertises the aggregate | advertises exactly `count_distinct, count_non_null, count_rows, sum` — **`slope` is not advertised** | `engine_capability`, probed live |
| A reviewed executable formula exists | `balance_slope` has none; the v1 reviewed set is **two recipes**; the only reviewed v2 exemplar is `posted_debit_amount` — and v2 cannot be admitted at all | probed live |
| Source data exists | deployed catalogs are `cib` (1 table, customer master) and `ftr` (1 table, transactions). **No account-day balance snapshot** | probed live |
| The API can request execution | `business_dt` **appears nowhere** in `materialization_runs.py`; without it the lane returns no execution seam — every public request is compile-only | reproduced |
| "Hadoop submission" | `LocalClusterSubmitter` is *"a local `kedro` run"* — a subprocess, not YARN/Livy/remote Spark; the worker image has PySpark and no Java | `submit.py:168` |

**I had the slope evidence first-hand and did not connect it.** In C1 I built the engine capability
registry, wrote the test asserting v2-only aggregates — `slope` among them — are *not* renderable,
and then chose slope as the pilot. That is the specific failure this revision exists to prevent:
**a pilot must be selected from measured capability, never from a mockup.**

### The measured field, today

```
admission accepts: formula-v1 ONLY
engine advertises: count_distinct, count_non_null, count_rows, sum

reviewed v1 expectations (the only schema that can compile):
  merchant_mcc_diversity   count_distinct   engine=YES   FORMULA_AUTHORABLE
  obligor_facility_count   count_distinct   engine=YES   FORMULA_AUTHORABLE
reviewed v2 expectations:
  posted_debit_amount      (v2 — admission refuses v2 entirely)
```

The schema gate alone cuts 317 recipes to **two**. Whether either has a source table, bindable
operands, a spine and an inventory mapping on the deployed cluster is **R4-0's** question, and this
plan does not pre-empt its answer.

**`merchant_mcc_diversity` is INELIGIBLE as a pilot — three contradictions, not one [R5]:**

| | v2 recipe | reviewed v1 formula |
|---|---|---|
| Output grain | `customer` | `merchant` |
| Eligible rows | posted, non-reversed, non-technical | **inexpressible** — `ExpressionRoleExpectationV1` carries only `expression_path`, `aggregation`, `operand_role`, `source_relation_role`, `window`; there is no filter or authority field |
| Additivity | `additive` | legacy declares non-additive |

Generating its v1 formula would produce a technically valid number over the **wrong rows, at the wrong
grain, with the wrong roll-up rule**. That is a refusal, not a disagreement for R4-0 to weigh — leaving
**`obligor_facility_count` as the only v1 candidate**, pending R4-0 confirming its source data exists.

**All 23 findings were validated; the load-bearing ones were reproduced.** None dismissed.

## 1. Stages

### R4-0 — capability-grounded pilot selection (no assumed pilot)
Capture the cluster inventory **read-only** and compute a candidate matrix over: source table exists ·
operands bind · population/spine exists · reviewed executable formula exists · engine supports every
aggregate · inventory maps the physical table. **Select the simplest candidate that passes all six.**
If none passes, R4-0's output is the shortest list of gaps to close — not a pilot chosen anyway.

**No catalog upload or re-upload without explicit approval.** `balance_slope_90d` stays as the
*second*, more valuable acceptance feature, after account snapshots and slope execution exist.

*In parallel, not blocking:* hide the retired "Write definitions" control; narrow the leakage wording
now; recognition correctness; ruff ratchet. Recognition fails open for generation, so it must not
gate proving one deterministic feature executes.

### R4-1 — exact `FeatureDefinitionRevision`
Persist the **canonical typed planning-request JSON** plus schema version and hash — today the option
row stores `repr(value)` parameter strings, which prove equality but cannot reconstruct the request.
**`repr()` must never enter execution identity.**

Five identities, separated because the code already has overlapping ones:
- `feature_definition_key` — logical meaning + canonical parameters (`recipe:balance_slope?window_days=90`)
- `feature_definition_revision` — a revision of that meaning
- `executable_revision_id` — exact formula, binding, IR, environment-independent plan
- `output_column_name` — normalized physical name, deterministic collision handling
- `display_label` — presentation only (*"Balance slope · 90 days"*; **never an underscore identifier**)

**Applied at contract creation**, or `govern.py`'s *"ONE feature per feature_name"* lookup keeps
collapsing variants into versions of one feature. **Rebinding a physical column mints a new executable
revision, not a new logical feature.** Existing ambiguous contracts stay legacy/non-executable.

### R4-2 — explicit build workflow
**Selection stays local and reversible** — the Workbench promises that today, and creating an
authoring request on a checkbox would spend resources before the human commits. **The explicit "Build
in sandbox" click creates the durable request.**

Three lifecycles, separated:
- `FeatureBuildRequest` — durable user intent
- `FeatureBuildAttempt` — retryable authoring/compilation attempt
- `MaterializationRun` — execution/publication outcome

A retry mints a **new attempt** under the same request; it never mutates a failed attempt back to
running. Cancellation states explicitly which of these it does: drop a queued item, abort authoring,
kill the remote application, or forbid publication after computation.

### R4-3 — selected-formula authoring, and the Formula-v2 bridge **[R5]**

Author from the exact selected option into a **selected-authoring work item owned by the build
request** — not through `recipe_formula_shadow_work_item`, whose capture requires
`selected_for_initial_view` (so a feature the user scrolls to and picks may never have been captured),
whose identity is a speculative shadow capture bound to a generation run, and whose flag defaults off.

**Author deterministically wherever the recipe already decides.** A fully-specified recipe pins the
operation, operand roles, window, grain and output policy — that is instantiation, not authorship.
**Use the model only where a semantic choice genuinely remains**, and record which arm produced the
formula. Cheaper, reproducible, and it removes the provider from the common path.

**The v2 bridge is THREE layers, not one. Most of the value is in the first two.**

**Layer 1 — resolve the authority refs. Required before ANY v2 execution, by any route.**
`AuthorityRefsV2` governs *"which eligible-status set filters rows, which sign/direction convention
reads amounts, how reversals neutralize originals, and which rate policy converts currency"* — and
**`materialize/` never sees them** (verified: no reference anywhere under that package). So a v2
formula today carries policy declarations that no execution honours. Each ref must resolve to a real
filter, sign convention, reversal rule or conversion — **or refuse by name**.

> **INVARIANT:** no v2 formula may execute by any route until its authority refs are resolved or
> explicitly refused. Executing one with unresolved refs computes a number that does not match its own
> formula, silently — precisely the failure the v1-only refusal exists to prevent, arriving through a
> different door.

**Layer 2 — lowering (the cheap bridge, and the one the pilot should use).** When a v2 formula's
*computational shape* fits v1 — `identity`/`ratio`/`difference`, one of the four v1 aggregates,
trailing or calendar window, **no** offset, **no** second operand, **no** aggregation argument —
translate it to v1 with the Layer-1 policies expressed as v1's structural filters, and compile with the
**existing compiler and existing renderer**. Anything outside that envelope refuses by name.

*Measured:* the reviewed v2 exemplar `posted_debit_amount` qualifies on every count —
`identity` / `sum` / `trailing` / offset 0 / no second operand / no argument. **Zero renderer work and
no new compiler**, once Layer 1 exists.

**Layer 3 — native v2, only where genuinely needed.** A fused executable artifact (BR-6 never minted a
`TypedFormulaV2`; the artifact is a proposal *beside* an output policy) plus versioned admission —
required for v2-only *shapes* (offsets, second operands, composite signed terms). Then per-operation
rendering for v2-only *aggregates*, **one at a time, each with its semantic decision** (percentile:
exact or approximate? slope: which time basis, what minimum observations? stddev: sample or
population?) and each **executed** before the engine advertises it.

**Sequencing inside R4-3:** Layer 1 → Layer 2 → pilot executes. Layer 3 waits until a feature people
actually want is blocked by it.

### R4-3b — a qualified evaluation-artifact reader **[R5]** — replaces a hardcoded refusal

`_gold_evaluation_recorded()` returns `False` for every recipe, and its docstring — **mine, and
false** — claims *"NO store records a gold-evaluation outcome anywhere in the platform."*

**Migration 1029 creates four:** `recipe_formula_eval_run`, `recipe_formula_eval_case`,
`recipe_formula_eval_attempt`, `recipe_formula_eval_artifact`. The store predates that comment. The
end-to-end walkthrough proves the consequence by monkeypatching the function to `True`; without the
patch, materialization stays blocked forever.

**The gap is not a missing store — it is a missing reader with a validity contract.** A passing
artifact only counts if it was produced under the *current* world, so the reader validates it against
the current recipe revision, blueprint hash, grammar version, policy versions, model configuration and
code revision. **A stale pass is not a pass.** Anything unverifiable stays `False` — but for a stated
reason rather than a wrong one.

**Acceptance:** a passing artifact under matching versions flips readiness; one whose recipe,
blueprint, grammar or policy version has since moved does **not**, and names which moved; the
walkthrough's monkeypatch is deleted and the test seeds a real artifact.

### R4-4 — group integration onto the existing owners
There is already `MaterializationContractV1` (the authoritative compatibility/group hash),
`FeatureGroupPlanV1`, `GroupContractBinding`, `GroupPlanRevision`. The new concept must **map onto
them, not compete**:

```
FeatureSelectionGroupRevision → compile members → MaterializationContractV1
   → group by exact contract hash → FeatureGroupPlanV1 → existing binding/revision/publication
```

**Two gates, because the authoritative one cannot run early:** a cheap **preflight** (duplicate
selections, duplicate output names, declared cadence, obvious grain differences) that *recommends*
groupings; then a **post-compile** decision on **equality of the full `MaterializationContractV1`
hash** — which includes sensitivity, access, retention + policy version, availability promise,
publication policy, backfill boundary, spine declaration and classification/physical-type policy
versions. **The hash is authoritative**; a hand-maintained subset would drift. Incompatible selections
are **split into multiple group revisions with an explanation**, never silently refused.

### R4-5 — sandbox execution readiness
- **`business_dt` on the API** — without it every request is compile-only.
- **Live Hive schema read server-side**; `published_schema` stops being a caller assertion.
- **Captured inventory** from R4-0.
- **Dedicated materialization worker — before the first cluster run.** A long compile currently
  blocks relays, timers, projections and pollers in the shared tick. This is functional isolation,
  not deferred performance work.
- **A real remote execution seam** — remote submitter with durable application id and status, or a
  dedicated execution pod with Java/Spark and shared storage. The current local subprocess is not
  Hadoop submission and must not be described as such.
- **Persistent artifacts** — the generated project sits on ephemeral worker disk today, so a restart
  loses the sealed artifact and with it retry, audit, reconciliation and proof that a rerun used the
  same bytes. Persistent storage, or deterministic regeneration from the immutable revision plus hash
  verification.
- **Execution tier derived server-side**; a caller may not request PRODUCTION. Sandbox admits
  AI-proposed evidence with visible provenance; structural safety, PIT correctness, fan-out safety and
  read authorization still fail closed. **A governed contract is load-bearing for production
  promotion, not for sandbox exploration** — and `require_confirmer` must not gate a sandbox build or
  its status read.

### R4-6 — validate and publish honestly
Structural and data validation, then **publication intent → metastore swap → observed confirmation →
control-plane completion → reconciler repairs incomplete states.** Until that exists the claim is
narrowed to **"table-level atomic visibility"** — the swap and the Postgres transaction are not atomic
together, and the code says so.

### R4-7 — profile and UI
**Profiling is a separate attempt, not a node in the publishing pipeline** — otherwise a profile
failure fails the publication it must not fail. Two records, because one keyed by executable revision
would duplicate group metrics:

```
feature_group_profile(run_id, group_revision_id, business_dt, …)   -- row count, duplicate keys
feature_column_profile(run_id, executable_revision_id, business_dt, …) -- nulls, min/max/mean/median
```

With profile-schema version, exact-vs-approximate algorithm and version, bounded histogram/cardinality
policy, null and invalid-value semantics, attempt status and retry identity, an idempotent ingestion
API, and suppression for identifier/restricted features. **Computed inside Hadoop; summaries only
cross the boundary.** The UI shows *published, profile pending* · *published, profiled* · *published,
profile failed*. The first golden run may require a successful profile as its acceptance bar while the
lifecycle still permits publication with a failed one.

APIs the UI needs, named: create build request · get request + attempts · retry · cancel ·
confirm/split a proposed group · get output profile.

### R4-8 — the as-of snapshot window shape · **the single largest coverage unlock**

**Measured:** of 317 recipes, **90 can derive a formula blueprint automatically today**. The largest
single refusal is `WINDOW_NOT_EVENT_ANCHORED` at **102 recipes** — one third of the registry, blocked
by one missing window shape.

**What those 102 actually declare** (probed on `balance_slope`, representative of the group):

```
anchor_kind             = 'as_of'          ← the blocker
event_time_role         = ''               ← empty: there is no event to anchor to
business_effective_role = 'as_of_date'
window_basis            = 'trailing', unit 'days'
snapshot_policy         = "latest-known end-of-day snapshot at or before each day's cutoff"
```

The grammar offers exactly three bases — `trailing`, `calendar_period`, `future_horizon` — and **all
three anchor on an event time**. These recipes are not asking "which events fell inside a window".
They are asking **"what was the state on each of the last N days, taking the latest snapshot known at
or before each day's cutoff"**. That is a different read, not a different parameter.

**Two halves, and the second is smaller than it looks:**

1. **Grammar + derivation.** A fourth basis (an as-of series) on `WindowPolicyV2`, carrying the
   snapshot policy the recipes already state in prose, plus the derivation arm that maps
   `anchor_kind='as_of'` onto it. Recipes whose snapshot policy is absent or ambiguous keep refusing
   **by name** — this widens the grammar, it does not guess a temporal meaning.
2. **Rendering.** The renderer already knows as-of snapshot selection: `LatestAvailableAsOf`,
   `PartitionMappedSnapshot` and `CurrentSnapshot` exist and are rendered for the **spine**. What does
   not exist is expressing it as an **operand window**. This is extending a proven read pattern to a
   second position, not inventing one.

**Why it is NOT before the vertical slice.** Unlocking 102 blueprints before one feature has ever
executed would repeat the error revision 3 made: widening ahead of proof. A derived blueprint that
cannot run is not progress. **This is the first thing after S4/R4-7, and it is worth more than
anything else on the widening list.**

**Acceptance:** the 102 drop to a named residue, and the count is reported — not asserted; a recipe
with an absent or ambiguous snapshot policy still refuses by name; the rendered as-of series is
**executed** through `fake_spark` and produces the expected per-day values (the C1 discipline: an
operation may not be advertised until it has run); the engine capability advertisement moves only if
the renderer genuinely emits it.

### Then
One-hop governed joins (directional cardinality, duplicate and null join keys, SCD2 overlap refusal,
PIT dimension selection, population preservation, fan-out allocation, row-count inflation validation)
→ cross-catalog with the exact bridge realization revision, evidence snapshot, environment and tier
pinned, and `ExecutionTier.SANDBOX` passed explicitly → LLM-idea promotion → richer leakage
(`PASS`/`REFUSE`/`UNRESOLVED`, deterministic decides, model explains) → production promotion → AUROC.

**The `cust_num` ↔ `cif_id` example becomes an acceptance fixture only after a current observed bridge
snapshot confirms it.**

## 2. Testing correction **[R4]**
"Call the real backend handler for every UI operation" would demand 2xx from operations whose correct
answer is a business refusal. Instead: **OpenAPI route/method/schema conformance** for every frontend
operation · a **retired-capability registry** · **selected** end-to-end behavioural tests against a
real app, test database and controlled provider · and an explicit assertion that **no visible control
invokes a retired endpoint**. A route-existence check alone passes while the UI is broken —
`/features/recipe` exists and refuses everything.

## 3. Sequencing

```
R4-0 ⟨decides the pilot⟩ ─► R4-1 ─► R4-2 ─► R4-3 ─► R4-4 ─► R4-5 ─► R4-6 ─► R4-7
                                                                        │
                                            R4-8 ⟨as-of window: 90 → ~192 derivable⟩ ◄─┘ then widen
        │
        └─ parallel: recognition correctness · leakage wording · retired-control removal · ruff
```

**No duration estimate until R4-0 reports.** Revision 3's "≈30 days to S4" assumed a pilot that cannot
execute, an API that cannot request execution, and a submitter that is not Hadoop.

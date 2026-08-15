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
plan does not pre-empt its answer. `merchant_mcc_diversity` additionally carries the open D-7 grain
disagreement, which is a governance decision, not an engineering one.

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

### R4-3 — selected-formula authoring, and the Formula-v2 bridge
Author from the exact selected option into a **selected-authoring work item owned by the build
request** — not through `recipe_formula_shadow_work_item`, whose identity is a speculative shadow
capture bound to a generation run (and whose capture defaults **off**).

**Define the Formula-v2 executable artifact and add admission + lowering.** BR-6 never minted a
`TypedFormulaV2`; the artifact is a proposal/output-policy pair, and admission refuses v2 outright.
**Implement only the operation the chosen pilot needs** — not the whole v2 grammar.

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
R4-0 ⟨decides the pilot⟩ ─► R4-1 ─► R4-2 ─► R4-3 ─► R4-4 ─► R4-5 ─► R4-6 ─► R4-7 ─► widen
        │
        └─ parallel: recognition correctness · leakage wording · retired-control removal · ruff
```

**No duration estimate until R4-0 reports.** Revision 3's "≈30 days to S4" assumed a pilot that cannot
execute, an API that cannot request execution, and a submitter that is not Hadoop.

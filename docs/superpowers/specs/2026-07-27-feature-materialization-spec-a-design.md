# Spec A — Executable Materialization Vertical Slice (design)

**Goal:** Turn a governed `TypedFormulaV1` into a **runnable Kedro/PySpark project** that computes several features at once on a Hadoop/Hive cluster and publishes them as one complete, atomically-visible feature-group partition.

**One line:** Child-1 decided *what a feature means*; Spec A generates *the code that computes it* and proves the published table is complete and correct.

**Tech stack:** Kedro (nodes/pipelines/hooks) · PySpark (compute) · HDFS Parquet (`intermediate`, `primary`, `feature_staging`) · Hive (`feature`) · Postgres control plane (existing) · Python 3.11.

**Scope discipline.** This is the first of three specs. **Spec B** owns statistical profiling/EDA and its UI. **Spec C** owns `model_input` assembly. Spec A owns calculation correctness and atomic publication, and reports what it produced through a **run manifest only** — no min/max, quantiles or histograms.

---

## Global Constraints

Copied verbatim into the implementation plan; every task's requirements implicitly include these.

- **Functional-first (2026-07-27 directive).** Feature delivery is the priority; NFRs are deferred and recorded in `docs/DEFERRED-WORK.md`. **Exception 1:** governed authority is the *feature*, never deferred. **Exception 2:** an NFR whose damage is irreversible while deferred is fixed immediately.
- **Render-only. There is no interpreter.** The generated Kedro project **is** the execution path. Nothing in this system computes feature values except the generated code. (This deliberately departs from parent §2's "executor + renderer over one IR"; with a single path there is no equivalence to prove and no drift to police.)
- **The control plane never reads feature data.** It generates code and ingests small manifests. All computation happens in the customer's data plane. Nothing but bounded, access-classified summaries crosses back.
- **Frozen slotted dataclasses + `StrEnum`** — NOT pydantic. Matches `src/featuregen/formula/`.
- **All new hashes use Child-1's canonicalization**: RFC 8785 (JCS) via `featuregen.formula._jcs.dumps`, then `sha256`, mirroring `featuregen.formula.canonical.formula_content_hash`. No second canonicalization scheme.
- **Timezones validate through `zoneinfo.ZoneInfo`**, as `WindowPolicy.timezone` already does (this is why `tzdata` is a declared dependency).
- **Fail closed.** Any ungoverned or unresolvable input yields a typed external requirement or rejection — never a guess, never a silent default.
- **No new LLM call.** Spec A is deterministic end to end.
- Commit trailer: `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

---

## §0 Prerequisite and the sandbox rule

Parent §3 requires a **frozen, materially-eligible feature version and deployment binding** before anything may be published as a production feature. That lifecycle is Child-2 and is **not implemented**.

**Input to Spec A:** a Child-1 `AuthoringResult` whose `authoring_disposition == "RESOLVED"` (so it carries a real `candidate_formula: TypedFormulaV1` and a governed `FormulaOutputPolicyV1`), plus exactly one human declaration — the structured cadence (§C).

**The sandbox rule.** Spec A publishes to `sandbox_feature.<group>`, never `feature.<group>`. This is **not a naming convention** — it is derived from identity completeness (§B): the production target's name requires `deployment_binding_hash`, which cannot be computed without a frozen feature version. There is no flag to forget and no reviewer to rely on. When Child-2 lands, the same code publishes to `feature.<group>` because the identity finally exists.

A Spec A publication **must not be described anywhere** (table name, manifest, log line, docstring) as a production feature publication.

---

## §A The compilation chain

```
AuthoringResult(RESOLVED) → TypedFormulaV1              [Child-1, exists]
  ↓ A1  translate to planner intent, preserving exact logical_refs
FormulaPlannerIntentV1
  ↓ A2  resolve the PHYSICAL read set through governed facts:
        C1 authority reads · GRAIN · AVAILABILITY_TIME · VERIFIED join path
FormulaExecutionIRV1                                     → ir_hash
  ↓ A3  derive the output contract over the IR's physical read set
        + the one declared field (cadence)
MaterializationContractV1                                → materialization_contract_hash
  ↓ A4  group features by contract hash; fix the exact packing list
FeatureGroupPlanV1                                       → group_plan_hash
  ↓ A5  plan computation sharing (independent of grouping)
ComputationPlanV1
  ↓ A6  render
Generated Kedro project                                  → generated_project_hash
  ↓ A7  submit to the cluster; validate the pipeline (§N)
        L0 builds · L1 schema/access · L2 tiny-sample (ON DEMAND)
ValidationReportV1                                       → classified findings
  ↓      findings route: renderer defect → regenerate
  ↓                      governed-fact mismatch → re-attest, then regenerate
  ↓                      environment/data → operator
  ↓ A8  `kedro run` (L3) in the data plane (Spark on YARN)
intermediate → primary → feature_staging → assemble → validate → publish
  ↓ A9  manifest returns to the control plane
RunManifestV1                                            → sandbox_execution_hash
```

A7 is a loop, not a step: the platform submits, reads the classified report, fixes either the renderer or the governed fact, and regenerates. A8 only happens once L0 and L1 pass.

**A1 `FormulaPlannerIntentV1`** states requirements in *logical* terms and preserves `logical_ref` strings byte-exactly. It must never substitute a similar column. It carries: the required column refs (by role — measure, filter, event-time, grain key), the target grain (`entity`, ordered `keys`), the window policy, and the operation shape. It resolves nothing physical.

**A2 `FormulaExecutionIRV1`** is the compiled physical plan and the single source of truth for both the contract derivation (§C) and the renderer (§F). It carries:

- `physical_read_set`: for every logical ref, the resolved physical `(schema, table, column)` **plus its role**. The read set is the *complete* set — operands, filter columns, join keys, joined tables, event-time columns, and every bridge/path dependency — not merely the formula's operands. This distinction is load-bearing for §C.
- `grain_path`: how each expression's source relation reaches the grain keys (§H).
- `pit`: the event-time column, the availability column and `basis` (from the governed `AVAILABILITY_TIME` fact), and the window boundaries.
- `catalog_state_stamp`: the governed-fact versions the compilation relied on (so a later replay can tell whether the catalog moved).
- `operation`: aggregation, final operation, filter tree, decimal policy, null/÷0 policy — carried from the formula, not re-derived.
- `output_policy`: the `FormulaOutputPolicyV1` **as resolved by Child-1** (`resolve_formula_output_policy` over C1). Spec A never re-derives output type/unit/currency/additivity and never consults the LLM's advisory `expected_output`.

`ir_hash` covers the IR's *identity* fields only. Provenance (who compiled it, when, run ids) stays outside, per Child-1's discipline.

---

## §B Identity model — reconciliation, not a parallel system

Parent §3 defines four hashes. Spec A adds four and computes one over a reduced input set. Nothing is renamed and nothing is duplicated.

| Identity | Definition | Status in Spec A |
|---|---|---|
| `formula_content_hash` | canonical `TypedFormulaV1` only | **Exists** (Child-1, `formula/canonical.py`) |
| `formula_binding_hash` | formula + contract/version/provenance | **Absent** — requires the frozen FeatureVersion (Child-2) |
| `deployment_binding_hash` | feature version + environment + semantic parameter values | **Absent** — requires a feature version (Child-2) |
| `ir_hash` | **new** — the compiled physical plan (§A2 identity fields) | New |
| `materialization_contract_hash` | **new** — the output contract (§C) | New |
| `group_plan_hash` | **new** — the exact packing list (§D) | New |
| `generated_project_hash` | **new** — the rendered project's bytes (§F) | New |
| `execution_hash` | deployment binding + `business_dt` + input snapshot ids + compiler | **Reduced in A** — see below |

**`sandbox_execution_hash`.** Because `deployment_binding_hash` does not exist, Spec A cannot compute the parent's `execution_hash`. It computes a distinctly-named identity over what genuinely exists:

```
sandbox_execution_hash = H(
    ir_hash(es),  materialization_contract_hash,  group_plan_hash,
    generated_project_hash,  environment_id,  resolved_parameter_values,
    business_dt,  input_snapshot_ids,  compiler_version,  renderer_version )
```

Three of those terms need definitions, or the hash is not reproducible:

- **`environment_id`** — a configured, stable identifier for the target cluster/metastore (e.g. `hdfc-local-hadoop`). Declared in the generated project's parameters; not inferred from a hostname.
- **`resolved_parameter_values`** — the values bound for this run: every `ParameterDecl` the formula declares (Child-1 `schema.py`), plus `business_dt`. Semantic and operational parameters are both included; run-scoped observations are not.
- **`input_snapshot_ids`** — **honest definition for this slice:** the resolved `(schema, table, partition)` list actually read, together with the IR's `catalog_state_stamp`. This identifies *which inputs* were read and *which governed facts* the plan relied on. It is **not** a content snapshot — true content-addressed input versioning needs the deferred Iceberg layer, so two runs over the same partitions after an in-place source rewrite will share a `sandbox_execution_hash`. That limitation is recorded rather than papered over; it is also a reason Spec A publishes to sandbox.

**Upgrade path (normative):** when Child-2 provides `deployment_binding_hash`, `execution_hash` is computed per the parent definition with the above folded in, and the name `sandbox_execution_hash` disappears. The reduced identity must never be recorded under the name `execution_hash`.

**Namespace derivation (normative).** The publication target is a **function** of identity completeness, computed in one place:

```
formula_binding_hash present AND deployment_binding_hash present  →  feature.<group>
otherwise                                                         →  sandbox_feature.<group>
```

**`generated_project_hash`** is `sha256` over the sorted, path-keyed bytes of every generated file. A hand-edited project therefore fails to match its manifest, which is the tamper-evidence: the publisher refuses a project whose hash does not match the one recorded in the group plan's render record.

---

## §C `MaterializationContractV1` — the group key

The contract describes the **output agreement**, never the calculation. Two features with the same contract hash may land in the same table.

### Derived vs declared

| Field | Source | Rule |
|---|---|---|
| `entity`, `keys` | `TypedFormulaV1.grain` | Derived. Ordered keys are semantic. |
| `pit_semantics` | formula window + governed `AVAILABILITY_TIME` facts | Derived. Two features may only share a group if `(cif_id, business_dt)` means the same thing. |
| `sensitivity_class` | most restrictive classification across the **IR physical read set** | Derived. |
| `access_class` | intersection of permissions required by the read set's dependencies | Derived. |
| `retention_class` | most restrictive applicable retention policy | Derived (platform default policy). |
| `availability_class` | **declared** in this slice — see §C.1 | Declared once **per contract**, never per feature. |
| `cadence` | **declared** — the only mandatory human input | Structured, not a word (§C.2). |
| `publication_policy` | platform default `atomic_group` | Default; overridable only to stricter. |
| `backfill_boundary` | platform default `group_level` | Default; optional explicit `isolation_key` override. |

**Monotonic override (normative).** A human may override a derived field **only in the stricter/later direction**. `T+3 → T+4` is allowed; `T+3 → T+1` is refused. Same rule for sensitivity, access and retention: stricter accepted, looser refused. A refused override is an error, not a warning.

Because derivation runs over the **IR's physical read set**, a feature that reaches `cif_id` through a restricted `accounts` bridge inherits that restriction even though no formula operand is restricted. This is why §C cannot run on `TypedFormulaV1` alone.

### §C.1 Why availability is declared (and the honesty note)

The intended derivation is:

```
feature availability = latest readiness guarantee across ALL dependencies
                     + platform computation/publication allowance
```

**The governed fact this needs does not exist.** `AVAILABILITY_TIME` (`overlay/facts.py`) is `{column, basis ∈ {posted_at, ingested_at, event_time_plus_lag}, lag_hours?}` — it names *which column* carries knowledge time. It is **not** a source-delivery SLA. `drift_freshness_sla` (`overlay/config.py`) measures catalog-scan freshness, not business-data arrival. Deriving availability from either would be dishonest.

Deferring it does **not** compromise correctness: look-ahead is prevented by the `AVAILABILITY_TIME` column comparison (§G), so a late-arriving row is simply absent from an earlier run — the value is correct as of knowledge time, merely incomplete relative to event time. The delivery SLA governs the *promise to consumers* and the *trigger*, not whether the number is right.

**Recoverability requirement (normative):** the contract persists its full physical read set, so that when a governed source-delivery SLA lands, derivation can retroactively validate every declared `availability_class` and flag the ones that were lying. The deferral must not become a permanent blind spot. Recorded in `docs/DEFERRED-WORK.md`.

### §C.2 Cadence is a structured declaration

```json
{ "period": "daily",
  "timezone": "Asia/Dubai",
  "business_date_cutoff": "23:59",
  "trigger": "scheduled" }
```

`timezone` validates through `ZoneInfo`. `trigger` in this slice is `scheduled` or `manual`; **`dependencies_ready` is deferred** because it needs the same missing delivery-SLA machinery. Trailing-window length is *not* cadence: `total_debit_amount_30d` and `distinct_merchant_count_90d` are both daily.

### §C.3 What the hash covers

**Include** — stable declarations and derived classifications: entity, ordered keys, pit semantics, sensitivity/access/retention class, `availability_class`, cadence, publication policy, backfill boundary/isolation key, and every policy version used in derivation.

**Exclude** — live observations: current source watermark, actual arrival timestamps, current job status, run ids, wall-clock times. Including any of these would churn the group id on every source refresh, which would silently re-partition the world.

---

## §D `FeatureGroupPlanV1` — the packing list

The contract says *what may travel together*; the group plan says *exactly what is in this shipment*.

Carries: `materialization_contract_hash`, the ordered required features (each as `feature_id` + `formula_content_hash` + `ir_hash` + output column name and type), and the expected output schema (`keys… , business_dt, <feature columns…>`).

**`feature_id` in this slice.** The production identity lives in `feature_versions.feature_id`, which Child-2 owns and which does not exist here. Spec A therefore uses the **authoring intent's feature name** (from the Child-1 `AuthoringIntent`) as `feature_id`, normalized to a valid Hive column identifier, and requires it to be unique within the group plan. It is *not* a governed feature identity, and the group plan records it as `intent_feature_name` alongside `formula_content_hash` so that Child-2 can later attach the real `feature_id` without renaming published columns. Two features whose normalized names collide is a plan-construction error, not a silent overwrite.

`group_plan_hash` covers all of it. Adding a fourth feature leaves `materialization_contract_hash` unchanged and produces a new `group_plan_hash`.

**Completeness gate (normative).** The publisher validates the assembled table against the group plan and **refuses to publish** if any required feature column is missing, extra, mistyped, or produced by a different `ir_hash` than the plan names. Atomic publication guarantees *no partial visibility*; the group plan is what guarantees *completeness*.

---

## §E Computation planning (independent of grouping)

Three grouping decisions are separate concerns and must not be conflated:

| Decision | Optimises for | Owner |
|---|---|---|
| Computation grouping | shared scans and intermediates | `ComputationPlanV1` (§E) |
| Materialization grouping | compatible output contracts | `MaterializationContractV1` (§C) |
| Model-input grouping | only what a model needs | **Spec C** |

**Sharing rule (normative).** Features sharing a physical source table **and** the same availability basis share one `intermediate` projection, which:

- selects only the union of their physical read sets;
- applies the PIT availability cutoff **once**;
- retains the **maximum** required event-time range across consumers (a 90-day window covers a 30-day one);
- applies **only** filters every consumer shares. Feature-specific predicates (`transaction_type = 'debit'`, `country != 'UAE'`) stay in the feature's own node.

A wide landing table does **not** imply one inseparable job: every feature aggregates in its own node to its own `feature_staging` output, so one failure cannot corrupt a sibling or the published partition.

---

## §F Layers and the generated project

### Layers

| Layer | Storage | Contents |
|---|---|---|
| `raw` | existing Hive/HDFS, read-only | governed source tables; catalog entries generated from the IR read set, so the project declares exactly what it reads |
| `intermediate` | HDFS Parquet | per-(source, availability-basis) PIT-filtered projection, max window, shared filters only |
| `primary` | HDFS Parquet | reusable conformed facts + the entity × `business_dt` **spine** |
| `feature_staging` | HDFS Parquet | one independent output per feature |
| `feature` | Hive, partitioned by `business_dt` | the published wide group table (`sandbox_feature.<group>` in this slice) |
| `model_input` | — | **Spec C** |

### Generated project

```
<project>/
  conf/base/catalog.yml        datasets for every layer; storage locations are CONFIG, not literals
  conf/base/parameters.yml     business_dt, cadence, group plan, identity hashes
  src/<pkg>/nodes.py           the node functions
  src/<pkg>/pipeline.py        wiring
  src/<pkg>/hooks.py           metrics + provenance hooks
  pyproject.toml / requirements.lock
  GENERATED.json               identity block: every hash in §B + renderer version
```

Every file carries a header naming `formula_content_hash`, `ir_hash`, `group_plan_hash` and the renderer version. Storage locations live in `catalog.yml` so retargeting a cluster is a config change, not a regeneration.

### Nodes

`build_pit_projection` (per shared source) → `build_entity_date_spine` → `calculate_<feature>` (one per feature) → `assemble_feature_group` → `validate_feature_group` → `publish_feature_group`.

### Hooks

- **`MetricsHook`** — per-node wall time, input/output row counts, Spark stage metrics.
- **`ProvenanceHook`** — stamps every written dataset with the §B identity block.

Hooks capture *metrics*; nodes produce *artifacts*. Profiling is therefore a node (Spec B), never a hook.

---

## §G Point-in-time correctness (core, not deferred)

A look-ahead feature is **wrong**, not merely unhardened.

1. **Availability gate.** Each `intermediate` projection keeps a row only if its governed availability column (per `AVAILABILITY_TIME.basis`, plus `lag_hours` for `event_time_plus_lag`) is `<= business_dt` cutoff, where the cutoff comes from the cadence's `timezone` + `business_date_cutoff`.
2. **Window boundaries.** From `WindowPolicy`: `basis` (trailing / calendar period), `length`, `unit`, and `start_inclusive` / `end_inclusive` honoured exactly.
3. **Spine reduction.** The group LEFT-JOINs each feature's grain-level aggregate onto the entity × `business_dt` spine and yields **exactly one row per `(keys…, business_dt)`**, including entities with no matching source rows.
4. **Empty-window and null policy.** From the formula's `EmptyWindowResult` / `NullInput` / `ZeroDenominator` — not re-invented in the renderer.

**Worked example.** A transaction with `transaction_date = 2026-07-01` and `posted_at = 2026-07-05`: excluded from the `business_dt = 2026-07-03` feature, included at `2026-07-06`.

Golden and Spark-local tests must both assert this exclusion (§L).

---

## §H Reaching the grain

Aggregate sources frequently lack the grain key: `transactions` has `account_id`, the grain is `cif_id`.

**Normative rule.** The IR resolves the grain path **only** through VERIFIED governed joins/bridges — the existing operational join path (`active_bridges` are VERIFIED-only; `_scoped_bridges` additionally requires both endpoint catalogs authorized). If no VERIFIED path exists, compilation fails closed with the external requirement `GRAIN_PATH_NOT_GOVERNED`. Spec A never infers, guesses or hard-codes a join.

Child-1's capability classifier already restricts v1 formulas to a **single catalog source**, so cross-source planning is out of scope here.

---

## §I Validation gates (blocking)

Run after assembly, before publication. Any failure **rejects the group**; the previously published partition remains visible and untouched.

1. **Key uniqueness** — exactly one row per `(keys…, business_dt)`. A duplicate `(cif_id=1001, business_dt=2026-07-27)` rejects the group.
2. **Required columns** — every column named by `FeatureGroupPlanV1`, no more, no fewer.
3. **Output types** — each feature column matches the type in its Child-1 `FormulaOutputPolicyV1`.
4. **Completeness** — every required feature's `calculate_*` node completed and its `ir_hash` matches the plan.
5. **Schema hash** — the assembled schema hash matches the group plan's expectation.
6. **Forbidden numerics** — invalid values (e.g. NaN / ±Inf) where the formula's policy forbids them.
7. **Project integrity** — the running project's `generated_project_hash` matches the group plan's render record.

Statistical judgements ("this distribution looks unusual") are **not** gates here and never will be in v1 — that is Spec B, observational.

---

## §J `RunManifestV1`

The only artifact crossing back to the control plane in Spec A. Small, structured, no feature data.

```
run_id · business_dt · group_plan_hash · sandbox_execution_hash
expected_feature_columns · staged_row_count · published_row_count
schema_hash · key_uniqueness_result · required_column_result
publication_location_pointer · started_at · published_at · status
```

`status ∈ {running, validated, published, rejected, failed}`. Ingested into the control-plane Postgres by an explicit command; the authenticated callback protocol is Child-6 and deferred, so nothing POSTs from the data plane in this slice.

This answers exactly one question — *what did this cluster run produce, and did it publish?* — and deliberately no more.

---

## §K Atomic publication

**Invariant (functional, not deferred).** A reader sees either the complete previous partition or the complete new one. Never: mixed old/new columns, a missing partition, or partially-written rows. If `distinct_merchant_count_90d` fails, nothing publishes and the old partition stays.

**Mechanism is pluggable** behind a `GroupPublisher` seam, because atomic reader visibility is metastore- and engine-specific.

- `INSERT OVERWRITE` is **rejected outright** — it deletes before writing, leaving a window where readers see nothing or partial data.
- The candidate Hive mechanism (stage to a fresh location, then a single metastore pointer operation) **must be proven by an executable test against the target cluster** — concurrent readers observing only complete states throughout a swap — before the design may claim it. A task in the plan, not an assumption.
- If no mechanism on the target stack satisfies the invariant, the honest outcome is to report that and publish behind an explicit reader-visible pointer instead. Silently accepting a non-atomic swap is not an option.

---

## §L Testing

Three tiers, mirroring Child-1's gold gate — and, as there, the cheap tier proves plumbing and must never be credited with proving correctness.

1. **Golden-file render tests** (default CI, no Spark). IR + group plan → generated project bytes, compared to committed goldens. Catches renderer drift. Also asserts `generated_project_hash` stability and the identity headers.
2. **Spark-local execution tests** (marked, opt-in; the `eval`-marker pattern). Actually run the generated pipeline in Spark local mode on tiny hand-authored fixtures and assert: computed values match hand-computed expectations; **the §G look-ahead row is excluded**; exactly one row per `(keys…, business_dt)`; entities with no source rows still appear; every §I gate fires on a deliberately broken group.
3. **Validation-loop tests** (default CI where possible, no cluster for L0). Assert that each level detects what it is for and — critically — that findings are **classified correctly**, since the classification is what routes the fix. The discriminating cases: a deliberately-broken renderer output yields `RENDERER_DEFECT`; a Hive column whose real type contradicts the governed fact yields `GOVERNED_FACT_MISMATCH` and **not** `ENVIRONMENT_OR_DATA`; a missing partition yields `ENVIRONMENT_OR_DATA` and **not** a renderer defect; an unattributable finding yields `UNCLASSIFIED` and fails closed. Also assert the egress rule: no finding carries a data value, only counts, types and locations. And assert the regeneration rules — a `GOVERNED_FACT_MISMATCH` blocks regeneration, and validation results never carry across a changed `generated_project_hash`.

4. **Cluster acceptance** (manual). Submit via the local submitter; L0 and L1 pass; L2 on demand over a sample; `kedro run` (L3) on the real Hadoop/Hive environment; the atomic-publication test from §K; the run manifest ingested and readable.

Fixtures are **hand-authored**, not generated from the code that renders them — the Child-1 lesson that a fixture derived from the implementation asserts only that the code agrees with itself.

---

## §M Failure handling

| Failure | Outcome |
|---|---|
| No VERIFIED grain path | compile-time external requirement `GRAIN_PATH_NOT_GOVERNED`; nothing generated |
| Contract override loosens a derived field | error; refused |
| A `calculate_*` node fails | that feature's staging output absent → completeness gate rejects the group → previous partition intact |
| Any §I gate fails | group rejected, manifest `status = rejected`, reason recorded |
| Publication mechanism cannot guarantee atomic visibility | publication refused; reported, not downgraded |
| `generated_project_hash` mismatch | publication refused (tamper evidence) |
| L0 fails | nothing submitted; findings classified (almost always `RENDERER_DEFECT`) |
| L1 fails | no L2/L3 attempted; a type or existence contradiction is `GOVERNED_FACT_MISMATCH` and blocks regeneration until re-attested; a missing partition or permission denial is `ENVIRONMENT_OR_DATA` |
| L2 fails | no L3 run; findings classified; nothing published |
| A finding cannot be attributed | `UNCLASSIFIED` — fails closed, never downgraded to environmental |
| Submission itself fails (cluster unreachable) | `status = error` on the report, no findings invented, loop not advanced |

---

## §N Validation loop and local submission

The platform does not emit code and hope. A generated project is **submitted, validated, and reported on**, and the classified report drives regeneration. This is what lets the platform fix a feature rather than hand you a broken pipeline.

### Levels — fail fast, cheapest first

| Level | What it proves | Cost | When |
|---|---|---|---|
| **L0** project builds | imports resolve, the Kedro DAG builds, catalog entries parse, `generated_project_hash` matches its render record | seconds, **no cluster** | every generation |
| **L1** schema + access | every column in the IR physical read set exists in Hive with the declared type, is readable, and the `business_dt` partition exists | seconds, **metastore metadata only — no data read** | every submission |
| **L2** tiny-sample execution | Spark analysis errors, join-cardinality blowups, and the §I gates over a sampled slice | minutes, small Spark job | **on demand** |
| **L3** full partition run | the real materialization (§A8) | full | operator |

L0 and L1 are standard. **L2 is explicitly on demand** — requested when L1 passes but the output isn't trusted yet. L3 is a run, not a validation.

The layering is the point: a wrong column name must surface in seconds from L1, never after an hour of L3 Spark.

### `ValidationReportV1`

Small, machine-readable, no feature data. Carries `report_id`, `generated_project_hash`, `group_plan_hash`, `level ∈ {L0, L1, L2}`, `environment_id`, `started_at`, `finished_at`, `status ∈ {passed, failed, error}`, and `findings: tuple[ValidationFinding, ...]`.

Each `ValidationFinding` carries a `code` from a **closed vocabulary**, a `severity`, its `classification` (below), a `location` (physical `(schema, table, column)` and/or the generated node), `expected` / `observed` **as type and schema facts only**, and a `count` where one is meaningful.

**Egress rule (normative).** Findings carry **counts, types and locations — never data values.** `"3 duplicate grain keys in sandbox_feature.cif_daily"` is permitted; the offending `cif_id` values are not. Where a value would aid debugging it stays in the data plane's own logs and the finding points at its location. A validation report crosses the data-plane boundary, so it is governed by the same metadata-only discipline as Child-1's tool egress.

### Classification — what makes the loop actionable

"It failed" is useless. Every finding is classified into exactly one bucket, because each routes to a different fix:

| Classification | Meaning | Fix route |
|---|---|---|
| `RENDERER_DEFECT` | we generated wrong or invalid PySpark/Kedro | fix the renderer and **regenerate**. The generated project is never hand-edited — `generated_project_hash` makes an edit detectable |
| `GOVERNED_FACT_MISMATCH` | reality contradicts a governed fact — the catalog says `transactions.amount` is decimal, Hive says `string` | the **code is right and the catalog is wrong**: raise an external requirement against that fact and re-attest it before regenerating |
| `ENVIRONMENT_OR_DATA` | partition absent, permission denied, source not yet loaded | nothing to fix in code; the operator acts |
| `UNCLASSIFIED` | cannot be attributed | **fails closed** — must never be silently treated as environmental, which would let a real renderer defect masquerade as someone else's problem |

That second bucket is the one that earns the loop: it distinguishes "our generator is broken" from "your catalog is lying", which are the same symptom and opposite fixes.

### Regeneration rules (normative)

- `GOVERNED_FACT_MISMATCH` **blocks** regeneration until the fact is re-attested — regenerating against a fact known to be wrong reproduces the same failure and wastes a cluster round-trip.
- `RENDERER_DEFECT` permits regeneration once the renderer changes. The new project necessarily has a different `generated_project_hash`, so a stale project cannot be re-validated by accident.
- A regenerated project starts at L0 again. Validation results are never inherited across `generated_project_hash` values.

### Submission

`PipelineSubmitter` is the seam; `LocalClusterSubmitter` is the only implementation in this slice — a deliberately thin adapter that submits the generated project to the local Hadoop/Spark cluster (`spark-submit`, or Livy where available) and collects the report. It is cheap precisely because there is no cross-organisation security boundary on localhost.

**Deferred to Child-6:** authenticated submission into a real bank environment — request-endpoint authentication, idempotency-key derivation, the run acceptance/status/cancellation protocol, result attestation, and reconciliation after partial failure. Only the transport changes; the loop's value does not depend on it.

---

## First-slice bounds

One entity (CIF) · one cadence (daily) · one materialization group · the three worked-example features (`cross_border_value_ratio_90d`, `total_debit_amount_30d`, `distinct_merchant_count_90d`) · one `business_dt` partition per run · one Hadoop/Hive environment · only the formula operations those features need · L0+L1 validation standard with **L2 on demand** · a local submitter only · **no UI · no cross-cadence assembly · no statistical profiling**.

---

## Deferred NFRs

Mandatory section per the functional-first directive. All recorded in `docs/DEFERRED-WORK.md` with triggers.

**Deferred from the parent architecture:** Iceberg atomic revisions, time-travel and restatement · the run state machine (`REQUESTED→ACCEPTED→RUNNING→COMMITTED/FAILED/CANCELLED/STALE_INPUT`) · outbox and reconciliation · external attestation round-trip and authenticated callbacks (Child-6) · execution-signature batching optimization (Child-7) · quarantine by bounded bisection · profiling privacy hardening (Spec B) · the full Child-2 lifecycle (frozen artifact → feature version → deployment binding, status axes → `materialization_eligibility`, template-reuse policy, parameter split) · the full `TemporalPolicyV1` (SCD effective/system time, reversal policy, late-arrival horizon, restatement policy).

**Newly identified here:** a **governed source-delivery SLA** distinct from catalog freshness — the prerequisite for deriving `availability_class` (§C.1) · the `dependencies_ready` cadence trigger, which needs it · **authenticated submission into a real bank environment** (§N — Spec A ships a local submitter; the authenticated request endpoint, idempotency keys, run state machine, result attestation and reconciliation are Child-6) · multi-partition and backfill runs (one `business_dt` per run here) · multi-environment promotion.

**Reclassified as functional and now IN Spec A:** the submit → validate → classify → regenerate loop (§N). It was originally deferred as "automated job submission", but the *loop* is what lets the platform fix a feature instead of handing over a broken pipeline — and distinguishing "our renderer is wrong" from "the catalog is lying" is governance, not operations. Only the authenticated cross-organisation transport stays deferred.

**Explicitly NOT deferred**, despite resembling NFRs: PIT correctness (§G) — a look-ahead feature is wrong; atomic group publication (§K) — a partially-published table is a correctness failure; the §I validation gates; and the derived sensitivity/access/retention classification (§C) — governance is the product.

---

## Out of scope (later specs)

**Spec B — profiling and inspection.** Bounded EDA computed in the data plane (count, null count, min, max, mean, stddev, p01–p99; categorical: distinct count, top-k), the append-only `feature_output_profile` keyed by `materialization_run_id`, the data-plane→control-plane profile protocol, access-class inheritance, and the access-controlled UI. Observational: never blocks publication in v1. Profile history is retained from the start so drift detection can be added later without backfill.

**Spec C — model-input assembly.** Requested-feature selection across published groups, daily/monthly as-of alignment (a daily row takes the latest month-end value **not later than** its `business_dt`), access inheritance, model-input schema and identity, model-specific publication.

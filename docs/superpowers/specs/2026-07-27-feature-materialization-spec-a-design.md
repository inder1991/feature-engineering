# Spec A — Executable Materialization Vertical Slice (design, rev 2)

**Goal:** Turn a **governed, RESOLVED** feature authoring result into a **complete runnable Kedro/PySpark project** that computes the three worked-example features on a Hadoop/Hive cluster and publishes them as one atomically-visible feature-group partition — proven by actually executing them.

**One line:** Child-1 decided *what a feature means*; Spec A generates *the code that computes it*, proves the numbers, and publishes completely or not at all.

**Tech stack:** Kedro (nodes/pipelines/hooks) · PySpark · HDFS Parquet (`intermediate`/`primary`/`feature_staging`) · Hive (`sandbox_feature`) · Postgres control plane · Python 3.11.

**Scope.** First of three specs. **Spec B** owns profiling/EDA and its UI. **Spec C** owns `model_input` assembly. Spec A owns calculation correctness and atomic publication, reporting through a **run manifest** only.

> **Rev 2** rewrites rev 1 after a code-grounded review found twelve defects, several of which would have produced silently wrong feature values. The material changes: the only public input is a governed `ResolvedFeatureInput`; joins go through the existing `classify_join_path` planner (cardinality- and role-aware) instead of a hand-rolled bridge match; the IR is **per expression**; the spine has a governed source; contract PIT semantics exclude the calculation window; identity is split into two non-circular phases and is **sandbox-only**; computation sharing is **removed** from this slice; completeness is proven by per-feature staging manifests rather than schema; and Spark-local execution plus a cluster publication proof are **mandatory gates**, not opt-in tests.

---

## Global Constraints

- **Functional-first (2026-07-27 directive).** NFRs are deferred and recorded in `docs/DEFERRED-WORK.md`. **Exception 1:** governed authority is the *feature*. **Exception 2:** an NFR whose damage is irreversible while deferred is fixed immediately.
- **Render-only.** The generated project **is** the execution path. Nothing in `src/` computes a feature value; no `pyspark` import outside rendered text.
- **The control plane never reads feature data.** It generates code and ingests small manifests/reports.
- **Frozen slotted dataclasses + `StrEnum`** — NOT pydantic.
- **Every new hash is RFC 8785 (JCS) + sha256** via one helper wrapping `featuregen.formula._jcs.dumps`. Hashes cover identity fields only; provenance and live observations stay out.
- **Reuse governed machinery; do not re-implement it.** Joins go through `classify_join_path`; sensitivity gating through `allowed_sensitivities`; C1 reads through `read_operational_value`.
- **Fail closed.** Ungoverned, unverified, denied or unresolvable ⇒ a typed refusal. Never a guess.
- **Manifests and findings carry counts, types, hashes and locations — never data values.**
- **Sandbox only.** Spec A cannot publish to `feature.*` at all (§B).
- **`INSERT OVERWRITE` is forbidden** as a publication mechanism.
- Timezones validate through `zoneinfo.ZoneInfo`. No new LLM call.
- Commit trailer: `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`

---

## §0 The only public input: `ResolvedFeatureInput`

Materialization has exactly one entry point and it cannot be handed a bare formula.

```python
@dataclass(frozen=True, slots=True)
class ResolvedFeatureInput:
    intent: AuthoringIntent      # carries the feature NAME (AuthoringResult does not)
    result: AuthoringResult      # carries candidate_formula + disposition
```

**Admission checks (all normative, all fail closed):**

1. `result.authoring_disposition == "RESOLVED"`.
2. `result.candidate_formula is not None`.
3. `formula_content_hash(result.candidate_formula) == result.candidate_formula_hash`.
4. `authoring_intent_hash(intent)` matches the `intent_hash` recorded on the authoring run.
5. The caller's `roles` authorize **the entire physical read set** — every operand, filter column, event-time column, join key, joined table, bridge hop **and the spine source** (§A4). A single denied element refuses the whole compilation.

`AuthoringResult` deliberately does not carry a feature name, which is why the intent travels with it: the name becomes the output column and must be governed provenance, not a caller-supplied string.

---

## §A The compilation chain

```
ResolvedFeatureInput (RESOLVED + admission checks §0)
  ↓ A1  logical requirements, exact logical_refs preserved
FormulaPlannerIntentV1
  ↓ A2  PER-EXPRESSION physical resolution
ExpressionExecutionIR  (one per body path)  →  expression_ir_hash
  ↓ A3  assemble
FormulaExecutionIRV1   (expressions + grain + spine + output policy)  →  ir_hash
  ↓ A4  governed spine resolution
SpineSpec
  ↓ A5  classify sensitivity/access/retention over the UNION of all read sets
MaterializationContractV1  →  materialization_contract_hash
  ↓ A6  fix the packing list
FeatureGroupPlanV1  →  group_plan_hash          }  CompilationIdentity
  ↓ A7  render a COMPLETE runnable project
RenderedProject  →  generated_project_hash      }  RenderedArtifactIdentity
  ↓ A8  submit + validate (§N): L0 import+DAG · L1 schema/access · L2 on demand
ValidationReportV1 → classified findings → fix renderer OR re-attest fact → regenerate
  ↓ A9  execute (Spark local, then the cluster)
per-feature staging + StagingManifestV1 each
  ↓ A10 assemble → validate → publish atomically → run manifest
RunManifestV1
```

### §A2 Expression-level IR (was a rev-1 defect)

Child-1 permits each `AggregateExpression` its own `source_relation`, `filter`, and `window`. A legal ratio can therefore read **two different tables with two different event-time columns and two different windows**, sharing only the catalog source. One `PitSpec` per formula cannot represent that.

```python
@dataclass(frozen=True, slots=True)
class ExpressionExecutionIR:
    expr_path: str                       # "body.expr" | "body.numerator" | ... (Child-1 vocabulary)
    physical_read_set: tuple[PhysicalRef, ...]   # operand + filters + event time + join keys + hops
    join_plan: JoinPlan                  # from classify_join_path — steps, cardinality, authority
    pit: PitSpec                         # THIS expression's event-time, availability basis, window
    aggregation: AggregateFunction
    filter_tree: Mapping[str, Any] | None
```

`FormulaExecutionIRV1` holds `expressions: tuple[ExpressionExecutionIR, ...]`, the grain, the `SpineSpec`, the carried `FormulaOutputPolicyV1`, the final operation and decimal/null policy, and a `catalog_state_stamp`.

### §A3 Joins — reuse the governed planner

Grain reaching and any expression-level join **must** go through `classify_join_path(conn, catalog_source, from_table, to_table, *, roles) -> JoinOutcome` (`overlay/upload/join_path.py`). It already provides multi-hop BFS, per-step **cardinality oriented to traversal direction**, clearing (declared or VERIFIED) vs unverified vs denied classification, read-scope denial, and four discriminated outcomes.

**Normative mapping:**

| `JoinOutcome.kind` | Spec A behaviour |
|---|---|
| `OPERATIONAL` | proceed; retain every `JoinStep` verbatim in `JoinPlan` |
| `UNVERIFIED` | refuse — `JOIN_PATH_NOT_VERIFIED`, naming the fact keys needing confirmation |
| `DENIED` | refuse — `JOIN_PATH_DENIED_BY_READ_SCOPE`, naming the endpoints |
| `NO_PATH` | refuse — `GRAIN_PATH_NOT_GOVERNED` |

**Cardinality is load-bearing.** A traversal step that fans `1:N` toward the grain multiplies rows and silently inflates a SUM. `JoinPlan` records each step's cardinality, and the renderer **must** collapse fan-out (aggregate before joining, or de-duplicate on the grain key) — never emit a naive join across an N-side hop. This is a correctness requirement, not an optimization.

`JoinPlan` retains: ordered steps (`from_ref`, `to_ref`, `cardinality`), the authority reference for each step, the roles the classification was performed under, and the resulting outcome kind.

### §A4 `SpineSpec` — the entity population has a governed source

"Every customer appears, including those with no transactions" is impossible if the spine is built from the transaction table. The spine needs its own governed source.

```python
@dataclass(frozen=True, slots=True)
class SpineSpec:
    entity: str
    source_table_ref: str                    # e.g. hdfc::banking.customers
    key_columns: tuple[str, ...]             # exact, ordered, matching Grain.keys semantics
    availability: PitSpec                    # the spine's own as-of policy
    join_plan: JoinPlan | None               # if spine keys need a governed hop to the grain keys
    snapshot_identity: str                   # the physical partition/snapshot read
```

The spine source is part of the physical read set for **role authorization (§0.5), sensitivity classification (§C) and `input_snapshot_ids`**. There is no default and no inference: a formula whose entity has no governed spine source refuses with `SPINE_SOURCE_NOT_GOVERNED`.

---

## §B Identity — two phases, non-circular, sandbox-only

Rev 1 was circular (identity contained `generated_project_hash`, yet was consumed to render the files that produce it) and scalar (one `formula_content_hash` for a multi-feature group).

```python
@dataclass(frozen=True, slots=True)
class CompilationIdentity:
    formula_content_hashes: tuple[str, ...]      # one per feature, sorted
    ir_hashes: tuple[str, ...]                   # one per feature, sorted
    materialization_contract_hash: str
    group_plan_hash: str

@dataclass(frozen=True, slots=True)
class RenderedArtifactIdentity:
    compilation: CompilationIdentity
    generated_project_hash: str                  # computed AFTER rendering
```

**Non-circularity (normative).** The rendered files embed the **`CompilationIdentity`** only. `generated_project_hash` is computed over the rendered bytes afterwards and lives in a **detached** `GENERATED.lock` file and the control-plane row — never inside a file that the hash covers.

**Sandbox only.** Spec A has **no production publication path**. `derive_namespace()` returns `sandbox_feature` unconditionally, and there is no parameter that changes it. Rev 1's "two non-empty strings unlock production" gate is removed: Child-2 must later supply a factory that *validates actual frozen bindings*, and that factory does not exist, so neither does the door.

`sandbox_execution_hash` covers: `CompilationIdentity`, `generated_project_hash`, `environment_id`, resolved parameter values, `business_dt`, `input_snapshot_ids`, compiler and renderer versions, and the **publication capability attestation id** (§K). It is never recorded under the name `execution_hash`.

`input_snapshot_ids` = the `(schema, table, partition)` set actually read (including the spine) plus the `catalog_state_stamp`. **Not** a content snapshot — an in-place source rewrite is invisible to it. Recorded, not papered over; one more reason this slice is sandbox-only.

---

## §C `MaterializationContractV1` — the group key

### §C.1 What PIT semantics means here (rev-1 contradiction fixed)

Rev 1 said the contract's PIT semantics include the formula window, while also requiring 30-day and 90-day features to share a group. Both cannot hold.

**Normative:** the contract's `pit_semantics` is the meaning of the **landing key** — `(entity keys, business_dt, cutoff timezone, cutoff time, availability basis class)`. It answers "does `(cif_id, 2026-07-27)` mean the same thing in every column of this row?"

The **calculation window lives in the expression IR and is NOT hashed into the contract.** `total_debit_amount_30d` and `distinct_merchant_count_90d` therefore share a contract, which is the intended behaviour.

### §C.2 Classification adapter (rev-1 defect: unimplementable)

`read_column_facts` returns `{value, authority, provenance}` — it carries no sensitivity, access or retention. Classification therefore needs its own **versioned adapter**, `CLASSIFICATION_POLICY_VERSION`, defined once and hashed into the contract.

The repository has two vocabularies: read-scope tags (`pii`, `restricted`) and effective restrictions (`public`, `internal`, `confidential`, `restricted`, `prohibited`). The adapter:

- reads `graph_node.sensitivity` for every element of the **union of all expression read sets plus the spine**;
- maps to the **effective-restriction** vocabulary (`public < internal < confidential < restricted < prohibited`) — that ordered five-value scale is the contract's `sensitivity_class`; the plan does not invent a third vocabulary;
- derives `access_requirements` as the set of role predicates needed to read that maximum, expressed as `allowed_sensitivities` requires them, so the contract states what a reader must hold;
- takes `retention_class` from an explicit **platform retention policy version** — no governed per-column retention policy exists, and inventing one per column would be dishonest.

Derivation runs over the **IR**, not the formula: join keys, bridge tables and the spine can each be the most restrictive element.

### §C.3 Derived vs declared, and the monotonic rule

Derived: grain/keys, landing PIT semantics, sensitivity, access requirements, retention (policy default). Declared: **cadence** (the one mandatory human input) and **`availability_class`** (declared per contract — the governed source-delivery SLA needed to derive it does not exist; see `DEFERRED-WORK.md`). Platform defaults: publication policy `atomic_group`, backfill boundary `group_level`.

Overrides are **monotonic**: stricter/later accepted, looser/earlier **refused as an error**.

Cadence is structured — `{period, timezone, business_date_cutoff, trigger}` — with `timezone` validated through `ZoneInfo` and `trigger ∈ {scheduled, manual}`; `dependencies_ready` is deferred.

### §C.4 Hash contents

**Include:** entity, ordered keys, landing PIT semantics, sensitivity class, access requirements, retention class + policy version, `availability_class`, cadence, publication policy, backfill boundary/isolation key, `CLASSIFICATION_POLICY_VERSION`.
**Exclude:** calculation windows, current watermark, arrival timestamps, job status, run ids, wall-clock.

---

## §D `FeatureGroupPlanV1` and completeness by manifest

The plan carries `materialization_contract_hash` and, per feature: `intent_feature_name` (from the authoring intent, normalized to a Hive identifier; a post-normalization collision is a plan error), `column_name`, `column_type`, `formula_content_hash`, `ir_hash`.

**Completeness is proven by manifests, not schema (rev-1 defect).** A matching schema cannot show that a column was produced by the expected IR. Each `calculate_*` node writes:

```python
@dataclass(frozen=True, slots=True)
class StagingManifestV1:
    intent_feature_name: str
    ir_hash: str
    output_location: str
    schema_hash: str
    row_count: int
    status: str          # "completed" | "failed"
```

Assembly **consumes every planned feature's staging manifest** and refuses when one is missing, `status != "completed"`, or its `ir_hash` differs from the plan's. Schema equality is a necessary check, never a sufficient one.

---

## §E Computation: independent per feature in this slice

Rev 1 shared projections keyed on `(schema, table, availability_basis)`, which omitted catalog source, availability column, lag, event-time column, timezone, window basis, joins, access scope and snapshot — and converted month/quarter/year windows to days, changing calendar-period semantics.

**Normative for Spec A: no scan sharing.** Every feature computes independently from `raw`. A duplicated scan is slower; an incorrectly shared filter or window produces a **wrong feature**, and this slice's job is to prove the numbers.

Sharing returns in a later slice behind a strict compatibility fingerprint covering **all** of: catalog source, schema, table, availability column + basis + lag, event-time column, timezone, window basis and unit, the full join plan, access scope, and input snapshot — with calendar-period windows never normalized to days.

The three grouping decisions remain separate concerns: computation (here), materialization (§C), model-input (Spec C).

---

## §F Layers and the **complete runnable** project

| Layer | Storage | Contents |
|---|---|---|
| `raw` | existing Hive/HDFS, read-only | governed sources; catalog entries generated from the read sets + spine |
| `intermediate` | HDFS Parquet | per-feature PIT-filtered projection (independent in this slice) |
| `primary` | HDFS Parquet | the entity × `business_dt` spine from its governed source (§A4) |
| `feature_staging` | HDFS Parquet | one independent output per feature + its `StagingManifestV1` |
| `feature` | Hive, partitioned by `business_dt` | the published group — **always `sandbox_feature.<group>`** |
| `model_input` | — | Spec C |

**A complete project, not fragments (rev-1 defect).** `render_project()` emits a directory that runs:

```
<project>/
  pyproject.toml            pinned deps
  requirements.lock
  conf/base/catalog.yml     every dataset, storage locations as CONFIG
  conf/base/parameters.yml  business_dt, cadence, group plan, CompilationIdentity
  conf/base/logging.yml
  src/<pkg>/__init__.py
  src/<pkg>/settings.py     Kedro settings incl. hook registration
  src/<pkg>/pipeline_registry.py
  src/<pkg>/pipelines/materialize/__init__.py
  src/<pkg>/pipelines/materialize/nodes.py
  src/<pkg>/pipelines/materialize/pipeline.py    explicit input/output wiring
  src/<pkg>/hooks.py        MetricsHook + ProvenanceHook
  GENERATED.lock            detached identity incl. generated_project_hash
  README.md                 how to run: `kedro run --params business_dt=...`
```

**Execution entry point (normative).** The project is launched by `kedro run` inside a Spark session configured by a Kedro hook; `spark-submit` is used only to place that `kedro run` on the cluster, with the project shipped as a packaged wheel plus `conf/`. The distinction is stated in the generated README so no operator guesses.

Nodes: `build_spine` · `build_pit_projection_<feature>` · `calculate_<feature>` (writes its `StagingManifestV1`) · `assemble_feature_group` (consumes manifests) · `validate_feature_group` · `publish_feature_group`. Hooks capture metrics and stamp provenance; profiling is Spec B and is not a hook.

---

## §G Point-in-time correctness

A look-ahead feature is **wrong**, not unhardened.

1. **Availability gate per expression** — keep a row only if its governed availability column (per `AVAILABILITY_TIME.basis`, plus `lag_hours` for `event_time_plus_lag`) is `<= business_dt` cutoff, where the cutoff comes from the cadence's timezone and `business_date_cutoff`.
2. **Window boundaries per expression** — `basis`, `length`, `unit`, `start_inclusive`, `end_inclusive` honoured exactly. Calendar-period windows are computed as calendar periods, never as day counts.
3. **Fan-out control** — where the join plan crosses a `1:N` step toward the grain, aggregate or de-duplicate before joining (§A3).
4. **Spine reduction** — LEFT JOIN each feature's grain-level aggregate onto the spine; exactly one row per `(keys…, business_dt)`; entities with no source rows still present.
5. **Empty-window / null / ÷0** — from the formula's policies, never re-invented.

Worked example: `transaction_date = 2026-07-01`, `posted_at = 2026-07-05` — excluded at `business_dt = 2026-07-03`, included at `2026-07-06`. Asserted by executing the generated code (§L), not by inspecting rendered text.

---

## §H Validation gates (blocking)

Run after assembly, before publication; any failure rejects the group and leaves the previous partition untouched.

Key uniqueness · required columns present, no extras · output types match each `FormulaOutputPolicyV1` · **every staging manifest present, `completed`, and `ir_hash`-matching** · assembled schema hash matches the plan · forbidden numerics (NaN/±Inf where policy forbids) · `generated_project_hash` matches `GENERATED.lock`.

Statistical judgements are never gates here — that is Spec B, observational.

---

## §J `RunManifestV1`

`run_id` · `generation_id` (**FK to the exact generation**) · `group_plan_hash` · `materialization_contract_hash` · `generated_project_hash` · `sandbox_execution_hash` · `business_dt` · `publication_mechanism` + capability attestation id · expected feature columns · staged and published row counts · schema hash · key-uniqueness and required-column results · publication location · `started_at` · `published_at` · `status ∈ {running, validated, published, rejected, failed}`.

**Append-only run events.** Because `status` implies mutation, the control plane records **`materialization_run_event`** rows (append-only, write-once, UPDATE/DELETE/**TRUNCATE** all blocked) and derives current status by folding them — the same discipline as the Child-1 authoring trace. A single terminal manifest row may be inserted at the end; nothing is ever updated in place.

Ingestion of `ValidationReportV1` and `RunManifestV1` into the control plane is **in scope** and has its own task — rev 1 left it implied.

---

## §K Atomic publication — capability must be attested

**Invariant:** a reader sees either the complete previous partition or the complete new one. Never mixed columns, missing partition, or partial rows.

**Capability attestation (normative).** Before any mechanism may be selected, the platform records a `PublicationCapabilityAttestation` for the **exact** Hive/Spark/metastore versions of the target environment: version triple, mechanism, the proof-test result, and a timestamp. **`select_publisher` refuses any mechanism lacking a passing attestation for that environment.** No mechanism is selectable "by default".

**Known constraints that the probe must settle:**
- `EXCHANGE PARTITION` cannot exchange into a destination where the partition already exists, and requires matching source/destination schemas.
- `ALTER TABLE … PARTITION … SET LOCATION` is documented DDL but its syntax does **not** by itself prove atomic visibility across Spark, Hive, cached sessions and other readers.
- **Schema evolution is unresolved by partition swapping:** adding a feature changes the wide table's schema, and swapping one partition's location does not atomically change table schema. The probe must cover *adding a feature to an existing group*, not only replacing a partition.

**Preferred first-slice mechanism:** immutable versioned physical outputs with a single reader-visible pointer/view switch — still requiring demonstration on the actual cluster.

**Proof test requirements:** concurrent readers polling throughout the swap must observe only complete states, discriminated by a **generation marker or content check** (not schema and row count alone, which can coincide), and the test must include the add-a-feature case. `INSERT OVERWRITE` is rejected outright.

The publication target is derived internally from the sandbox identity; it is never accepted as a caller-supplied string.

---

## §L Testing — execution is a mandatory gate

Rev 1 could go green without ever computing a feature. Corrected:

1. **Golden-file render tests** (default CI). Rendered bytes vs committed goldens; identity headers; `generated_project_hash` stability.
2. **Spark-local execution — MANDATORY.** Runs in default CI. Executes the generated project on tiny hand-authored fixtures and asserts real numbers for **all three** worked features: `total_debit_amount_30d` (SUM + filter), `distinct_merchant_count_90d` (COUNT DISTINCT), `cross_border_value_ratio_90d` (RATIO). Plus: the look-ahead exclusion; exactly one row per `(keys…, business_dt)`; an entity with no source rows still present; zero-denominator policy; decimal rounding; empty-window policy; null policy; a `1:N` join step not inflating a SUM; and every §H gate firing on a deliberately broken group. **If pyspark cannot run in CI the task is not complete** — it is not an opt-in marker.
3. **Cluster acceptance — MANDATORY final task.** Publication capability probe on the real cluster; `kedro run` producing a published `sandbox_feature.cif_daily` partition; the atomicity proof including add-a-feature; manifest and validation-report ingestion verified. **If the cluster cannot be reached or the run cannot publish, Spec A is not done.**

Fixtures are hand-authored, never generated by the code under test, and every fixture has an owning task.

---

## §M Failure handling

| Failure | Outcome |
|---|---|
| Input not `RESOLVED` / hash mismatch / intent mismatch | refused at §0; nothing compiled |
| Caller roles do not cover the full read set incl. spine | `READ_SCOPE_INSUFFICIENT`; refused |
| `classify_join_path` → `UNVERIFIED` / `DENIED` / `NO_PATH` | `JOIN_PATH_NOT_VERIFIED` / `..._DENIED_BY_READ_SCOPE` / `GRAIN_PATH_NOT_GOVERNED` |
| No governed spine source | `SPINE_SOURCE_NOT_GOVERNED` |
| Missing `AVAILABILITY_TIME` fact | `AVAILABILITY_TIME_NOT_GOVERNED` |
| Override loosens a derived field | refused |
| A `calculate_*` node fails | its staging manifest is absent/failed → assembly refuses → previous partition intact |
| Any §H gate fails | group rejected, run event `rejected`, reason recorded |
| No passing publication capability attestation | publication refused; reported, never downgraded |
| `generated_project_hash` ≠ `GENERATED.lock` | publication refused (tamper evidence) |

---

## §N Validation loop and local submission

**Levels.** **L0** — materialize the project to a temp directory, install it into an isolated environment, **import it and build the Kedro pipeline object** (rev 1 only parsed text, which does not prove what L0 claims); verify `generated_project_hash` against `GENERATED.lock`. **L1** — metastore metadata only: every read-set and spine column exists with the declared type, is readable under the caller's roles, and the `business_dt` partition exists. **L2 (on demand)** — tiny-sample execution catching Spark analysis errors, join fan-out and the §H gates. **L3** — the real run.

**`ValidationReportV1`** carries `report_id`, `generation_id`, `generated_project_hash`, `group_plan_hash`, `level`, `environment_id`, timing, `status ∈ {passed, failed, error}`, and findings. Each finding: closed-vocabulary `code`, `severity`, `classification`, `location`, `expected`/`observed` as **type and schema facts only**, and a `count`.

**Egress rule.** Counts, types, hashes and locations only — never data values.

**Classification** routes the fix: `RENDERER_DEFECT` (fix renderer, regenerate) · `GOVERNED_FACT_MISMATCH` (code is right, catalog is wrong — re-attest; **blocks regeneration**) · `ENVIRONMENT_OR_DATA` (operator acts) · `UNCLASSIFIED` (**fails closed**; never silently environmental). A regenerated project restarts at L0; results never carry across a changed `generated_project_hash`.

**Submission** is behind a `PipelineSubmitter` seam with one implementation, `LocalClusterSubmitter` (`spark-submit`/Livy on localhost). An unreachable cluster yields `status="error"` with **zero** findings — never invented ones. Authenticated cross-organisation submission is Child-6.

---

## First-slice bounds

One entity (CIF) · one cadence (daily) · one materialization group · three features (`total_debit_amount_30d`, `distinct_merchant_count_90d`, `cross_border_value_ratio_90d`) · one `business_dt` per run · one Hadoop/Hive environment · **no scan sharing** · L0+L1 standard, L2 on demand · local submitter only · **sandbox only** · no UI · no cross-cadence assembly · no statistical profiling.

---

## Deferred NFRs

Recorded with triggers in `docs/DEFERRED-WORK.md`.

**From the parent architecture:** Iceberg revisions/time-travel/restatement · run state machine · outbox/reconciliation · external attestation and authenticated callbacks (Child-6) · execution-signature batching · quarantine by bisection · profiling privacy (Spec B) · the full Child-2 lifecycle · the full `TemporalPolicyV1`.

**Identified here:** governed **source-delivery SLA** (prerequisite for deriving `availability_class`) · `dependencies_ready` trigger · authenticated submission into a real bank environment · content-addressed input snapshots · multi-partition/backfill runs · multi-environment promotion · **computation scan sharing** (§E — returns behind a strict compatibility fingerprint) · governed per-column retention policy (platform policy version used instead).

**NOT deferred despite resembling NFRs:** PIT correctness · join cardinality handling · atomic group publication · the §H gates · derived sensitivity/access classification · Spark-local and cluster execution proofs.

---

## Out of scope

**Spec B** — profiling/EDA, `feature_output_profile`, the data-plane→control-plane profile protocol, access-controlled UI. **Spec C** — `model_input` assembly with daily/monthly as-of alignment.

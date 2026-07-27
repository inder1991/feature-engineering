# Spec A — Executable Materialization Vertical Slice (design, rev 6)

**Goal:** Turn a **governed, provenance-verified** feature authoring result into a **complete runnable Kedro/PySpark project** that computes the worked-example features on the Hadoop/Hive cluster and publishes them as one atomically-visible feature-group partition — proven by executing them and reading the published table.

**One line:** Child-1 decided *what a feature means*; Spec A generates *the code that computes it*, proves the numbers, and publishes completely or not at all.

**Scope.** First of three specs. **Spec B** owns profiling/EDA and its UI. **Spec C** owns `model_input` assembly.

> **Rev 4** follows a third review (10 findings on rev 3), several of which were self-contradictions inside rev 3 itself. Changes: **static IR is separated from run-time snapshots** (§3.3) so a generated project does not change every business date · the atomic-publication **probe is executable and its evidence is what an attestation ingests** (§10) · `SnapshotPolicy` is **fully specified** (§4.2) · the physical target is **bound to its contract** (§10.1) · Gate 2 is **group-wide** (§1.3) · unknown join cardinality and ambiguous intermediate hops are **refused** (§3.1) · failure codes are **three closed enums** (§14) · the spine declaration's **provenance is excluded from identity** (§4) · the fictional `FormulaPlannerIntentV1` stage is **removed** (§2).

> **Rev 3** followed a second code-grounded review (16 findings on rev 2, 12 on rev 1). Every API this spec names is verified in `docs/architecture/2026-07-27-verified-interfaces-materialization.md` with `file:line` citations. **Nothing may enter this spec or its plan unless it is verified there first** — both earlier revisions failed because plausible detail was written faster than it was checked.

**Material changes in rev 3:** admission verifies the **immutable terminal authoring event**, not a caller-constructed result · authorization is **two gates** (artifact, then complete physical read set) · the spine source is an **explicit declaration** with facts as validators only · `1:N` traversal toward the grain is **refused** · contracts are derived **per feature then grouped**, never unioned · a **versioned physical-type adapter** replaces the logical/physical conflation · `PhysicalInputSnapshot` carries **plural** partition specs · publication requires a `PublisherSelection` carrying a passing capability attestation.

---

## Global Constraints

- **Functional-first.** NFRs deferred and recorded in `docs/DEFERRED-WORK.md`. Exceptions: governed authority is the *feature*; an NFR whose damage is irreversible while deferred is fixed now.
- **Render-only.** The generated project **is** the execution path. No `pyspark` import in `src/featuregen/materialize/`.
- **The control plane never reads feature data.** It generates code and ingests small manifests/reports.
- **Frozen slotted dataclasses + `StrEnum`** — NOT pydantic.
- **One hasher:** RFC 8785 (JCS) + sha256 wrapping `featuregen.formula._jcs.dumps`. Identity fields only.
- **Reuse governed machinery.** Joins → `classify_join_path`. Sensitivity → `graph_node` + `safety_floor.SENSITIVITY_ORDER` + `read_scope`. C1 → `read_operational_value`. Actor → `IdentityEnvelope`.
- **Never mint identity.** `declared_by` records the `IdentityEnvelope` threaded from the request. Materialization never constructs one with `authenticated=True` — that is a trust-root violation this codebase has already been bitten by.
- **Fail closed.** Ungoverned, unverified, denied, unknown or ambiguous ⇒ a typed refusal.
- **Manifests and findings carry counts, types, hashes and locations — never data values.**
- **Sandbox only.** No production publication path exists.
- **`INSERT OVERWRITE` is forbidden.**
- Commit trailer: `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`

---

## §0 Prerequisite: target-cluster inventory (blocking)

**Nothing may assume how the source tables are physically laid out.** No partition key — `business_dt`, `transaction_date`, `load_dt` or otherwise — may be named as an assumption anywhere in this spec or its plan.

Before cluster acceptance, an inventory task captures from the metastore, for `banking.transactions`, `banking.accounts` and `banking.customers`:

- whether the table is partitioned at all;
- ordered partition columns and their types;
- example partition values covering the acceptance date;
- physical location;
- whether historical partitions are rewritten in place;
- how a customer snapshot corresponding to a business date is selected;
- **`late_arrival_days`** for any table declared `AVAILABILITY_PARTITION` (§3.4 — how far past the event window late arrivals may land);
- **the logical→physical schema mapping** for each table (needed by §3.5 when `graph_node.schema_name` is NULL);
- **how account-to-customer ownership is modelled** (see §H — a joint-account bridge makes the traversal `1:N` and would refuse the worked feature);
- **the Hive, Spark and metastore versions** (§10's capability attestation is keyed on that exact triple);
- **the full runtime compatibility set needed to RUN the generated project**: Python, Java, Spark/PySpark and Kedro versions. A project that compiles against the wrong PySpark cannot run, and that is discovered at `kedro run`, not at render.

**The inventory is a typed object, not a document.** The Markdown write-up is the human record; compilation and run preparation consume `ClusterInventoryV1` — a frozen schema with a loader and a **metastore adapter** that can refresh it. A prose file cannot be a runtime input.

Compilation contracts are parameterized on this inventory and can be specified before it exists; **cluster acceptance is blocked until it is complete.**

---

## §1 Admission — two gates, provenance-verified

### §1.1 The only public input

```python
@dataclass(frozen=True, slots=True)
class ResolvedFeatureInput:
    intent: AuthoringIntent     # carries the feature NAME; AuthoringResult does not
    result: AuthoringResult
```

`AuthoringResult` already carries `authoring_run_id` (verified, `result.py:97`), so no separate field is needed — and a forger citing a legitimate run is exactly the attack Gate 1 defeats, because that run's terminal payload will not carry their formula's hash.

### §1.2 Gate 1 — artifact admission against the immutable terminal event

`AuthoringResult` is a publicly constructible frozen dataclass. A caller can fabricate a "RESOLVED" result, attach any formula, and cite a legitimate run id. **In-memory self-consistency proves nothing.**

Child-1's authoring trace records a terminal `COMPLETED`/`FAILED` event carrying a canonical `payload` and a sha256 `payload_hash`, making it tamper-evident. Gate 1 verifies the supplied result **against that event**:

1. A terminal event exists for `authoring_run_id` — no terminal ⇒ `AUTHORING_RUN_INCOMPLETE`.
2. `payload_hash` validates the payload ⇒ else `TERMINAL_PAYLOAD_TAMPERED`.
3. The terminal payload's disposition is `RESOLVED` ⇒ else `NOT_RESOLVED`.
4. The terminal payload's recorded candidate-formula hash equals `formula_content_hash(result.candidate_formula)` ⇒ else `FORMULA_HASH_MISMATCH`.
5. The supplied status axes equal the terminal payload's axes ⇒ else `AXES_MISMATCH`.
6. `authoring_intent_hash(intent)` matches the run's recorded `intent_hash` ⇒ else `INTENT_HASH_MISMATCH`.

The feature name is `intent.name`, normalized to a Hive identifier; a post-normalization collision within a group is a plan error, never a silent overwrite.

### §1.3 Gate 2 — authorization over the complete physical read set

Gate 1 cannot authorize reads, because availability columns, join hops, bridge tables and the spine are only discovered during compilation. Authorization therefore runs **after** the IR is complete:

```
Gate 1: admit authored artifact   →  compile complete physical IR  →  Gate 2: authorize read set
```

**Gate 2 is group-wide, not per feature:**

```python
authorize_compilation(conn, irs: tuple[FormulaExecutionIRV1, ...], spine: SpineSpec,
                      *, roles) -> AuthorizedCompilation | AuthorizationRefused
```

It checks the **union** of every expression read set across every feature, plus the spine source and its keys, every join step's endpoints, and every availability column, against `allowed_sensitivities(roles)`.

A per-feature signature would be wrong in a way that matters: a public feature genuinely *is* individually authorized, so testing "every IR is refused" asserts the wrong thing. What must fail is the **group operation** — one denied element anywhere returns a single `READ_SCOPE_INSUFFICIENT`, and **no contract, group plan or project is produced.** There is no partial authorization and no per-feature bypass.

---

## §2 Compilation chain

```
ResolvedFeatureInput  →  §1.2 Gate 1
AdmittedFeature
  ↓ formula-to-expression adapter (logical_refs preserved byte-exactly)
  ↓ PER-EXPRESSION physical resolution (§3)
ExpressionExecutionIR (one per body path)   → expression_ir_hash
  ↓ + spine (§4) + carried output policy
FormulaExecutionIRV1                        → ir_hash
  ↓ §1.3 Gate 2 over the complete read set
  ↓ physical type resolution (§6)
  ↓ contract PER FEATURE (§5), then group by contract hash
MaterializationContractV1                   → materialization_contract_hash
  ↓ exactly ONE group in this slice, else MULTIPLE_MATERIALIZATION_CONTRACTS
FeatureGroupPlanV1                          → group_plan_hash    } CompilationIdentity
  ↓ render a COMPLETE runnable project (§7)
RenderedProject                             → generated_project_hash } RenderedArtifactIdentity
  ↓ submit + validate (§11): L0 import+DAG · L1 schema/access/partitions · L2 on demand
ValidationReportV1 → classified findings → fix renderer OR re-attest fact → regenerate
  ↓ execute
per-feature staging + StagingManifestV1
  ↓ assemble (manifest-verified) → validate (§9) → publish (§10) → run manifest (§12)
```

---

### §2.1 Reference completeness (replaces the removed intent stage)

Rev 3 named a `FormulaPlannerIntentV1` stage that **does not exist in the repository** and had no owning task. It is removed rather than invented: expressions compile directly from the admitted formula through a formula-to-expression adapter.

What that stage was meant to guarantee is kept as a **test**, which is stronger than an abstraction: every `logical_ref` reachable from the formula — operands, filter `left` refs, event-time refs, grain keys, source tables — must appear in the compiled IR's physical read set **or** be explicitly classified as non-physical with a stated reason. An unaccounted reference is a compilation defect.

---

## §3 Expression-level IR and joins

Each `AggregateExpression` owns its `source_relation`, `filter` **and** `window`, so a legal ratio may read two tables with two event-time columns and two windows. One PIT spec per formula cannot represent Child-1's grammar.

```python
@dataclass(frozen=True, slots=True)
class ExpressionExecutionIR:
    expr_path: str                     # body.expr | body.numerator | body.denominator | ...
    physical_read_set: tuple[PhysicalRef, ...]
    join_plan: JoinPlan
    pit: PitSpec                       # THIS expression's event time, availability basis, window
    input_requirements: tuple[PhysicalInputRequirement, ...]   # STATIC; run-time snapshots are NOT here
    aggregation: AggregateFunction
    filter_tree: Mapping[str, Any] | None
```

### §3.1 Joins go through the existing planner

`classify_join_path(conn, catalog_source, from_table, to_table, *, roles) -> JoinOutcome`.

**Table arguments are BARE table names.** `_table_of` returns the second dotted segment, so a schema-qualified destination never matches. The adapter parses schema-qualified logical refs, passes bare names, keeps schema/source identity separately, and **refuses ambiguity** (`AMBIGUOUS_TABLE_NAME`) when one catalog source has the same table name in two schemas.

**Authority provenance must be carried, not reconstructed.** `classify_join_path` currently drops `approved_join_fact_key`/`approved_join_status` for clearing (operational) edges even though its SQL selects them. Extend the planner **inside its own query** so each returned step carries `from_ref`, `to_ref`, `cardinality`, `approved_join_fact_key`, `approved_join_status` and `authority`. Reconstructing provenance later with a second read risks drift.

Outcome mapping: `OPERATIONAL` → proceed · `UNVERIFIED` → `JOIN_PATH_NOT_VERIFIED` (naming the fact keys) · `DENIED` → `JOIN_PATH_DENIED_BY_READ_SCOPE` (naming endpoints) · `NO_PATH` → `GRAIN_PATH_NOT_GOVERNED`.

**Unknown cardinality is refused.** `JoinStep.cardinality` is `str | None` and `graph_edge.cardinality` is nullable (both verified). Refusing only `1:N` would let `None` proceed — and an unknown edge may *be* `1:N`, inflating a SUM. Any step with `cardinality is None` refuses with `JOIN_CARDINALITY_UNKNOWN`. Unknown is never assumed safe.

**Table-name ambiguity is checked after PHYSICAL resolution (§3.5), not on the refs.** The planner's BFS indexes nodes by bare table name, so a path can be stitched between two tables that share a name. But logical refs are schema-flattened (§3.5) — every `object_ref` is written under `public` — so the ambiguity can **never** appear as two different schemas *in the refs*, and a rule written against that would be unreachable rather than discriminating. The real condition is **two resolved physical schemas containing the same table name**; the adapter checks it against resolved physical identities across every returned step, not just the endpoints, and refuses with `AMBIGUOUS_TABLE_NAME`. Reverse-edge provenance and cardinality are verified in the same pass.

### §3.2 Fan-out is refused, not repaired

**Any traversal step that fans `1:N` toward the target grain refuses the feature with `JOIN_FANOUT_UNSUPPORTED`.**

No `dropDuplicates`, no pre-aggregation, no renderer-level repair. Those are not equivalent, they differ per operation (SUM vs COUNT DISTINCT vs RATIO), and — decisively — a joint account whose transaction is attributable to two customers is a **business allocation decision**, not a technical de-duplication. Deduplicating would discard real transactions; pre-aggregating would silently pick an allocation rule nobody approved.

A later governed allocation policy will own those semantics. Until then, refusal is the honest outcome.

**Known consequence:** if account-to-customer ownership is modelled as a joint-holder bridge, `total_debit_amount_30d` is refused in this slice. §0's inventory must establish this before acceptance planning.

### §3.3 Physical inputs — STATIC requirement vs RUN-TIME snapshot

`business_dt` is a **run** parameter, not a compilation parameter. Resolving concrete partitions during compilation would put run-specific observations into `ir_hash` and `CompilationIdentity`, so **the generated project would change every business date** — defeating the point of a stable, hash-identified artifact.

Two phases, two objects:

```python
# GENERATION TIME — static, enters ir_hash and CompilationIdentity
@dataclass(frozen=True, slots=True)
class PhysicalInputRequirement:
    catalog_source: str
    schema: str
    table: str
    partition_columns: tuple[tuple[str, str], ...] | None   # ordered (column, type); None = verified unpartitioned
    partition_mapping: PartitionMappingV1 | None            # DECLARED (§3.4); never inferred
    layout_fingerprint: str          # SEMANTIC: partition columns+types, physical types, mapping
    catalog_state_stamp: tuple[tuple[str, str], ...]
    # NOTE: `captured_at`, refresh time and any other OBSERVATION provenance are deliberately
    # absent — re-capturing identical metadata must not change ir_hash.

# RUN PREPARATION — business_dt + live metastore; NEVER in ir_hash
@dataclass(frozen=True, slots=True)
class PartitionSpec:
    columns: tuple[tuple[str, str], ...]      # ordered (column, value)

@dataclass(frozen=True, slots=True)
class PhysicalInputSnapshot:
    requirement: PhysicalInputRequirement
    partition_specs: tuple[PartitionSpec, ...] | None       # the exact partitions THIS run reads
    resolved_at_business_dt: str
```

**Snapshots resolve PER EXPRESSION.** A group holds a 30-day feature, two 90-day features, and a ratio whose two expressions may read different tables with different windows and different availability bases. A single `window` argument cannot describe those reads. Run preparation therefore takes one `RunInputRequest(feature_name, expr_path, physical_requirement, pit_spec)` per expression and returns snapshots keyed by `(feature_name, expr_path, requirement_id)`. Identical resolved reads may be de-duplicated **after** their semantics are compared, never before.

```
generation:  formula → IR (PhysicalInputRequirement)      → ir_hash, CompilationIdentity
run prep:    business_dt + ClusterInventoryV1 + metastore  → PhysicalInputSnapshot (per expression)
                                                           → sandbox_execution_hash
                                                           → L1 partition validation
                                                           → execution
```

### §3.4 `PartitionMappingV1` is declared, never inferred

A partition column named `load_dt` does **not** tell you how a 90-day *event-time* window maps to *load* partitions — late-arriving transactions live in load partitions outside the event range, so an inferred mapping silently drops data. The mapping is therefore **declared** in the environment inventory (§0) with closed variants:

```python
class PartitionMappingKind(StrEnum):
    EVENT_TIME_PARTITION = "event_time_partition"      # (time_ref, partition_column, transform, timezone)
    AVAILABILITY_PARTITION = "availability_partition"  # (time_ref, partition_column, transform,
                                                       #  timezone, late_arrival_days)
    STATIC_SNAPSHOT = "static_snapshot"                # (partition_values)
    FULL_SCAN = "full_scan"
    VERIFIED_UNPARTITIONED = "verified_unpartitioned"
```

A table with no declared mapping refuses with `PARTITION_MAPPING_NOT_DECLARED`. **"A 90-day window resolves 90 partitions" is only true for an explicitly declared one-day `EVENT_TIME_PARTITION` mapping** — it is never a general rule, and an `AVAILABILITY_PARTITION` mapping must extend the partition set beyond the event window to catch late arrivals — **`late_arrival_days` is by how much**, and it is required, because a mapping that must widen without saying by how much is unimplementable.

**Plural** — a 90-day feature reads many partitions. `partition_columns is None` means **verified unpartitioned**, never "unknown"; unknown refuses with `PARTITION_IDENTITY_UNKNOWN`. `input_snapshot_ids` (a run-time value, inside `sandbox_execution_hash` only) is the exact ordered partition set read. L1 runs at **run preparation**, after snapshots resolve, and verifies every one exists.

For an unpartitioned mutable table the snapshot is **not content-addressed** — recorded honestly, and one more reason this slice is sandbox-only.

---

### §3.4b Source engine is part of physical identity

The catalog upload carries a **`source_type`** (e.g. `edp`, `ods`) and a **`source`** engine (e.g. `hive`, `oracle`) per row. The engine is part of physical identity, because Spec A generates PySpark that reads **Hive and HDFS only** — it cannot read an Oracle table, and the semantics differ beyond the mechanics (an ODS is typically current-state-only, so it cannot answer the point-in-time reconstruction §4.2's spine policies assume).

**Normative:**

- Physical identity carries `source_type` and `engine`.
- **Spec A refuses any read set element whose engine is not `hive`** with `SOURCE_ENGINE_UNSUPPORTED`, naming the element and its engine. A formula spanning two engines is refused, not half-generated.
- **A blank or absent engine refuses too** — it must never default to `hive`, for the same reason a blank schema must not default to `public`.
- The environment inventory (§0) declares the engine mapping **per `(source_type, source)`**, not per table, so it stays a handful of lines regardless of column count.

Oracle support is deliberately deferred: it needs a different reader, different auth, and its own answer to what availability means.

### §3.5 Physical schema resolution is an explicit step

**A logical ref's schema segment is catalog-side, not the physical Hive schema.** `build_graph` flattens every `object_ref` to `public.<table>[.<column>]` (`graph.py:20`); the real declared schema survives only in `graph_node.schema_name`, which is **nullable**. So a governed ref may legitimately read `hdfc::public.transactions.amount` for a table that lives in `banking`.

Every example in this spec written as `hdfc::banking.…` should be read as *"the ref for the transactions table"* — its literal schema segment depends on what the upload declared, and nothing may parse a physical schema out of it.

**Normative:**

1. Physical `(schema, table)` is resolved **from the governed catalog**, reusing the existing `column_authority.logical_ref_of` pattern (read `graph_node.schema_name`) — never by parsing the ref.
2. When `schema_name` is `NULL`, resolution consults the environment's declared logical→physical schema mapping from the §0 inventory.
3. If neither yields a physical schema, compilation refuses with **`PHYSICAL_SCHEMA_NOT_RESOLVED`**. It must **not** silently fall back to `public`, which would read a different table than the catalog governs — a wrong-table read that produces plausible numbers is exactly the failure this spec exists to prevent.
4. The resolved physical identity, not the ref, is what feeds `PhysicalInputRequirement`, the join adapter's ambiguity check (§3.1), and the generated catalog entries.

---

## §4 `SpineSourceDeclarationV1` — the entity population

`ENTITY_ASSIGNMENT` + `GRAIN` prove that a column represents an entity and that some columns uniquely identify rows. **They do not prove the table contains every member**, retains inactive members, or is authoritative — several tables can carry a unique `cif_id` over incomplete populations. Choosing one from those facts is inference, which this spec forbids.

```python
@dataclass(frozen=True, slots=True)
class SpineSourceDeclarationV1:
    entity: str
    source_table_ref: str                     # exact; no table inference
    ordered_key_refs: tuple[str, ...]         # exact column refs
    population_semantics: PopulationSemantics
    availability_ref: str | None
    snapshot_policy: SnapshotPolicy           # structured; how a snapshot maps to business_dt
    declared_by: IdentityEnvelope             # threaded from the request, never minted
    declaration_reason: str
    declaration_version: int
```

```python
class PopulationSemantics(StrEnum):           # CLOSED
    CURRENT_COMPLETE_POPULATION = "current_complete_population"
    CURRENT_ACTIVE_ONLY = "current_active_only"
    HISTORICAL_AS_OF = "historical_as_of"
```

**Rules (normative):**

- Declared **once per materialization contract**, never per feature.
- **Only its SEMANTIC payload enters the contract hash.** The declaration carries provenance (`declared_by`, `declaration_reason`, recorded-at, record id) which must NOT affect identity — otherwise two people making the identical semantic declaration produce different contract hashes and therefore different groups, which contradicts the global no-provenance-in-hashes rule.

```python
SpineSourceDeclarationV1.identity_payload()    # entity, source_table_ref, ordered_key_refs,
                                               # population_semantics, availability_ref,
                                               # snapshot_policy, declaration_version
SpineSourceDeclarationV1.provenance_payload()  # declared_by, declaration_reason,
                                               # recorded_at, declaration_record_id
```
- **Governed facts validate the declaration; they never choose it.** `ENTITY_ASSIGNMENT`, `GRAIN`, read scope and `AVAILABILITY_TIME` may *reject* a declaration (wrong entity, non-unique keys, denied read, missing availability) but must never select a source.
- A `CURRENT_COMPLETE_POPULATION` claim is recorded as a **human declaration**, not a governed catalog fact, and is labelled as such wherever it is surfaced.
- **No arbitrary SQL predicate.** Any active/current-row selection is structured via `snapshot_policy`; free-text SQL would smuggle ungoverned business logic into the population definition where no governance check can see it.
- When a governed `ENTITY_POPULATION_SOURCE` fact is later introduced it **supersedes** the declaration; existing declarations are checked against it and disagreement blocks further materialization.

### §4.2 `SnapshotPolicy` — closed and fully specified

Rev 3 named `SnapshotPolicy` without designing it, which left an implementation free to read the whole customer table — producing duplicate spine keys or leaking future customer versions into a past `business_dt`. Closed variants:

```python
class SnapshotPolicyKind(StrEnum):
    CURRENT_SNAPSHOT = "current_snapshot"
    LATEST_AVAILABLE_AS_OF = "latest_available_as_of"
    PARTITION_MAPPED = "partition_mapped"
    ACTIVE_POPULATION = "active_population"

CurrentSnapshot(observed_snapshot_ref)  # the table IS the current population; no history.
                                        # REQUIRES an observed snapshot date/version — a present-day
                                        # table cannot honestly answer an arbitrary HISTORICAL
                                        # business_dt, so a mismatch refuses rather than pretending.
LatestAvailableAsOf(effective_time_ref, availability_ref,
                    deterministic_tie_break_refs)   # SCD-style history
PartitionMappedSnapshot(ordered_partition_mapping)  # business_dt → partition values
ActivePopulation(status_ref, allowed_status_values) # closed status set, no free-text predicate
```

**Normative PIT rules for the spine — these mirror §8 and are not optional:**

1. **Future versions are excluded.** Under `LATEST_AVAILABLE_AS_OF`, a row is eligible only if `effective_time_ref <= business_dt` cutoff **and** its `availability_ref` is `<=` the same cutoff. A customer record created after `business_dt` must not appear.
2. **Exactly one row per entity key.** For each key, the eligible row with the greatest `effective_time_ref` wins.
3. **Ties are broken deterministically.** When two eligible rows share an `effective_time_ref`, `deterministic_tie_break_refs` orders them. If ties remain unresolved the spine refuses with `SPINE_NON_DETERMINISTIC` — a non-deterministic spine silently changes the population between runs.
4. **`CURRENT_ACTIVE_ONLY` requires `ActivePopulation`.** The status column and its allowed values are declared; there is no implicit notion of "active" and no free-text predicate.
5. **Duplicate spine keys are a blocking gate**, not a de-duplication step.
6. **The spine's `availability_ref` participates in PIT filtering** exactly as an expression's does.

### §4.1 Completeness is checked against the claim

You cannot prove a population is complete, but you can prove it is not: a grain key present in the aggregates and absent from the spine is a member the spine is missing. The response depends on what was claimed:

| `population_semantics` | Orphan grain key ⇒ |
|---|---|
| `CURRENT_COMPLETE_POPULATION` | the declaration is **provably false** → **block publication** (`SPINE_INCOMPLETE`) |
| anything narrower | orphans are expected → **count and report an orphan rate** in the run manifest; do not block |

This makes the declaration self-checking against its own claim rather than against an assumption, and converts an otherwise undetectable population bias into a first-run failure.

---

## §5 `MaterializationContractV1`

### §5.1 Derived per feature, then grouped

**A contract is derived for each feature independently and features are then grouped by equal contract hash.** Deriving one contract from the union of supplied IRs would let a caller force a public feature into a restricted group merely by passing them together.

This slice publishes exactly one group: more than one distinct contract hash returns `MULTIPLE_MATERIALIZATION_CONTRACTS`, listing the groups. Nothing is silently promoted.

### §5.2 Classification — two independent axes

`graph_node.sensitivity` (read-scope tags `pii`, `restricted`) and `graph_node.effective_restriction` (ordered `public < internal < confidential < restricted < prohibited`) are **different fields**. Conflating them is a governance error.

- **`sensitivity_class`** = the maximum `effective_restriction` across the complete physical read set **including the spine**, ranked by `safety_floor.SENSITIVITY_ORDER`. Do not mint a parallel enum.
- **`access_requirements`** = the union of roles required by the `graph_node.sensitivity` tags present, via `SENSITIVITY_ROLES` (`pii → pii_reader`, `restricted → restricted_reader`).
- **Unknown restriction labels fail closed to `prohibited`** (`safety_floor` behaviour) and are never returned verbatim.
- **Missing classification has an explicit policy** stated in `CLASSIFICATION_POLICY_VERSION`, not an implicit default.
- **A `prohibited` input refuses materialization** (`PROHIBITED_INPUT`). It does not produce a "prohibited feature group".
- `retention_class` comes from an explicit platform retention policy: **`RETENTION_POLICY_VERSION`** and **`DEFAULT_RETENTION_CLASS`** are declared constants (no governed per-column retention exists), and both enter the contract identity — otherwise the implementation must invent a retention model.

### §5.3 PIT semantics exclude the calculation window

`pit_semantics` is the meaning of the **landing key**: `(entity keys, business_dt, cutoff timezone, cutoff time, availability basis class)`. It answers "does `(cif_id, 2026-07-27)` mean the same thing in every column of this row?"

The **calculation window lives in the expression IR and is not hashed into the contract**, so a 30-day and a 90-day trailing feature share a group — the intended behaviour.

### §5.4 Derived, declared, defaulted

Derived: grain/keys, landing PIT semantics, sensitivity class, access requirements, retention (policy default), physical types (§6).
Declared: **cadence** (structured; `ZoneInfo`-validated; `trigger ∈ {scheduled, manual}` — `dependencies_ready` deferred), **`availability_class`**, and the **spine source declaration** (§4).
Defaults: publication policy `atomic_group`, backfill boundary `group_level`.

Overrides are **monotonic** — stricter/later accepted, looser/earlier refused as an error.

### §5.5 Hash contents

**Include:** entity, ordered keys, landing PIT semantics, sensitivity class, access requirements, retention class + policy version, `availability_class`, cadence, publication policy, backfill boundary, spine declaration, `CLASSIFICATION_POLICY_VERSION`, `PHYSICAL_TYPE_POLICY_VERSION`.
**Exclude:** calculation windows, current watermark, arrival timestamps, job status, run ids, wall-clock.

---

## §6 Physical type adapter (`PHYSICAL_TYPE_POLICY_VERSION`)

`FormulaOutputPolicyV1.output_type` is *logical* (`numeric`, `integer`, `decimal`). No mapping to physical Hive/Spark types exists anywhere in the repository. **The operation determines the physical type — not the logical word alone.**

| Formula operation | Published type |
|---|---|
| `COUNT_ROWS` / `COUNT_NON_NULL` / `COUNT_DISTINCT` | `BIGINT` |
| `SUM` | `DECIMAL(precision, scale)` from `DecimalPolicy` |
| `RATIO` | `DECIMAL(precision, scale)` from `DecimalPolicy` |
| `DIFFERENCE` | `DECIMAL(precision, scale)` from `DecimalPolicy` |

**Rules:**

- Intermediate accumulation may use a wider decimal (Spark widens `SUM` over `DECIMAL(18,2)` to `DECIMAL(38,2)`), but the **published result is rounded and cast to the formula's declared precision and scale**.
- `RoundingMode` is implemented explicitly, never left to engine default.
- **`OverflowBehavior.ERROR` must fail the feature.** Spark's default decimal behaviour on overflow returns **NULL**, so genuine ERROR semantics require deliberate configuration plus explicit checks in generated code. All three first-slice features declare ERROR, so this is on the critical path — a silent NULL where the formula demanded an error is exactly the quiet wrongness this system exists to prevent.
- **`SATURATE` is refused** in this slice unless its clamping behaviour is explicitly implemented and tested.
- **Nullability is part of the decision**, derived from `EmptyWindowResult` and `ZeroDenominator`: a `ZERO` empty-window yields a non-null column; `ZeroDenominator.NULL` yields a nullable one. The §9 type gate cannot check honestly otherwise.
- Hive/Spark `DECIMAL` maxes at **precision 38**; a policy exceeding it, or any ambiguous conversion, returns `PHYSICAL_TYPE_UNSUPPORTED`. **Never silently map ambiguous numerics to `DOUBLE`.**
- `DECIMAL(p,s)` and `BIGINT` support is validated during the environment capability check (§10).

The resolved physical type and `PHYSICAL_TYPE_POLICY_VERSION` enter `FeatureGroupPlanV1`, `group_plan_hash` and therefore `CompilationIdentity`. They do **not** alter the formula hash — physical type is a materialization concern, not formula identity.

---

## §7 Identity and the complete project

```python
CompilationIdentity(formula_content_hashes, ir_hashes, materialization_contract_hash,
                    group_plan_hash)          # all plural where a group has many features
RenderedArtifactIdentity(compilation, generated_project_hash)
```

Rendered files embed the **`CompilationIdentity` only**. `generated_project_hash` is computed over the rendered bytes **excluding `GENERATED.lock`**, and is written *into* `GENERATED.lock` — the detached manifest. That precise exclusion is what makes the identity non-circular; every other generated file must be free of it.

**Sandbox only.** `derive_namespace()` takes no parameters and returns `sandbox_feature`. There is no production path; Child-2 must later supply a factory that validates actual frozen bindings.

`sandbox_execution_hash` covers `CompilationIdentity`, `generated_project_hash`, `environment_id`, resolved parameter values, `business_dt`, `input_snapshot_ids`, compiler/renderer versions and the **publication capability attestation id**. It is never recorded as `execution_hash`.

**The project is complete and runnable** — `pyproject.toml`, `requirements.lock`, `conf/base/{catalog,parameters,logging}.yml`, `src/<pkg>/{__init__,settings,pipeline_registry,hooks}.py`, `src/<pkg>/pipelines/materialize/{__init__,nodes,pipeline}.py`, `GENERATED.lock`, `README.md`. The README states how it is launched: `kedro run` inside a Spark session configured by a Kedro hook, with `spark-submit` used only to place that run on the cluster.

Layers: `raw` (read-only sources) → `intermediate` (per-feature PIT projection) → `primary` (the spine) → `feature_staging` (independent per feature + manifest) → `feature` = `sandbox_feature.<group>` (Hive). `model_input` is Spec C.

**No scan sharing in this slice.** Each feature computes independently from `raw`. A duplicated scan is slower; an incorrectly shared filter or window produces a wrong feature. Sharing returns later behind a strict compatibility fingerprint covering catalog source, schema, table, availability column/basis/lag, event-time column, timezone, window basis and unit, the full join plan, access scope and input snapshots — with calendar-period windows never normalized to days.

---

## §8 Point-in-time correctness

1. **Availability gate per expression** — keep a row only if its governed availability column (per `AVAILABILITY_TIME.basis`, plus `lag_hours` for `event_time_plus_lag`) is `<=` the `business_dt` cutoff derived from the cadence's timezone and `business_date_cutoff`.
2. **Window boundaries per expression** — `basis`, `length`, `unit`, `start_inclusive`, `end_inclusive` honoured exactly. **Calendar-period windows are computed as calendar periods, never as day counts.**
3. **Spine reduction** — LEFT JOIN each feature's grain-level aggregate onto the spine; exactly one row per `(keys…, business_dt)`; entities with no source rows still present.
4. **Empty-window / null / ÷0** — from the formula's own policies.

Worked example: `transaction_date = 2026-07-01`, `posted_at = 2026-07-05` — excluded at `business_dt = 2026-07-03`, included at `2026-07-06`. Asserted by **executing** the generated code, never by inspecting rendered text.

---

## §9 Validation gates (blocking)

Run after assembly, before publication. Any failure rejects the group; the previous partition stays untouched.

Key uniqueness · required columns present, none extra · physical types match §6 including nullability · **every staging manifest present, `completed`, `ir_hash`-matching, and bound to this generation/run/business_dt** · assembled schema hash matches the plan · forbidden numerics · overflow behaviour honoured · `SPINE_INCOMPLETE` when a `CURRENT_COMPLETE_POPULATION` declaration has orphan grain keys (§4.1) · `generated_project_hash` matches `GENERATED.lock`.

**Completeness is proven by manifest, not schema** — a matching schema cannot show which IR produced a column.

```python
@dataclass(frozen=True, slots=True)
class StagingManifestV1:
    intent_feature_name: str
    ir_hash: str
    generation_id: str          # binds the output to THIS compilation
    run_id: str                 # …and THIS run
    business_dt: str
    generated_project_hash: str
    sandbox_execution_hash: str
    output_location: str
    schema_hash: str
    row_count: int
    status: str                 # "completed" | "failed"
```

Without the generation/run/date binding, a reused staging path could leave an older successful manifest with a matching `ir_hash` and publish stale output. Staging paths are **generation-scoped and immutable**; assembly requires exactly one manifest per planned feature and rejects duplicates or unexpected manifests.

---

## §10 Atomic publication

**Invariant:** a reader sees either the complete previous partition or the complete new one — never mixed columns, a missing partition, or partial rows.

### §10.1 The physical target is bound to its contract

The contract hash is the group key, but `sandbox_feature.cif_daily` is a human name. Nothing yet stops a *different* contract — changed cutoff semantics, spine declaration, sensitivity, retention or cadence — from overwriting the same physical table, silently replacing one materialization contract with a semantically different one under an unchanged name.

An append-only row cannot hold a field that moves, so the binding is **two records**:

```python
@dataclass(frozen=True, slots=True)
class GroupContractBinding:          # written ONCE per logical name
    binding_id: str
    logical_group_name: str          # "cif_daily"
    materialization_contract_hash: str
    physical_target: str             # "sandbox_feature.cif_daily"

@dataclass(frozen=True, slots=True)
class GroupPlanRevision:             # appended whenever the packing list changes
    binding_id: str
    group_plan_hash: str
    generation_id: str
    created_at: str
```

Both tables are append-only (UPDATE/DELETE/TRUNCATE all blocked). **"Current plan" is derived** as the latest revision that published successfully — never stored as a mutable field. Publication refuses with `GROUP_BINDING_CONFLICT` when a logical name's contract hash differs from its binding; adding a feature appends a `GroupPlanRevision` and keeps the binding.

### §10.2 Published generation metadata lives in the data

The atomicity probe depends on a generation marker being visible in **the same atomic state as the data**, so the marker cannot live in side metadata that moves separately. For this slice it is **system columns in the immutable physical output**:

```
__generation_id · __generated_project_hash · __sandbox_execution_hash
```

**They are added exactly once, after assembly — never in per-feature staging.** Per-feature staging carries only `(keys…, business_dt, <one feature column>)`; if every staging output carried the system columns, assembly would produce duplicate or conflicting copies. Order: stage per feature → assemble onto the spine → **add the three system columns once** → validate.

Two of the three cannot be literals in the rendered source: `__generated_project_hash` would be self-referential (§7), so the assembled node **reads it from `GENERATED.lock` at runtime**; `__sandbox_execution_hash` is a run-time value and arrives via **prepared run parameters** (§11.1).

They are part of `expected_schema(plan)` and therefore part of the §9 schema gate. A pointer/view switch then moves data and identity together by construction. Spec C omits them from model inputs.

### §10.3 Capability attestation — ingested only from a probe

**Capability attestation.** No mechanism is selectable without a passing attestation for the **exact** environment:

```python
@dataclass(frozen=True, slots=True)
class PublicationCapabilityAttestation:
    attestation_id: str
    environment_id: str
    hive_version: str; spark_version: str; metastore_version: str
    mechanism: PublishMechanism
    passed: bool
    covers_schema_evolution: bool
    attested_at: str
```

**An attestation may be created ONLY by ingesting a probe result.** Rev 3 allowed `record_attestation` to store `passed=True` directly, so the live test proved only that someone had stored a boolean — not that publication is atomic. There is now an executable probe:

```python
probe_publication_capability(cluster, *, mechanism, engine_versions) -> ProbeResult
```

The probe must, against the real cluster:

1. materialize **generation A** and publish it;
2. start concurrent readers **polling continuously**, each observation recording the `__generation_id` system column **and** a content check (§10.2);
3. publish **generation B**;
4. assert every observation is a *complete* A state or a *complete* B state — schema and row count alone can coincide, so the generation marker is what discriminates;
5. **repeat the whole sequence while ADDING a feature column**, since a partition-location swap does not atomically change table schema;
6. return the actual observations plus an `evidence_hash` over them.

`record_attestation(probe_result)` accepts nothing else. An attestation with no probe evidence cannot exist.

**`adds_feature` is derived, never passed.** Rev 3 let the caller supply it, so passing `False` bypassed the schema-evolution requirement entirely. It is computed by comparing the **currently published schema** against the group plan's expected schema.

Recorded append-only. `select_publisher` verifies current engine versions against the attestation and returns an immutable

```python
PublisherSelection(environment_id, mechanism, capability_attestation_id, engine_versions)
```

**The renderer consumes a `PublisherSelection`, never a bare mechanism enum** — so rendered publication code cannot exist without evidence that selection succeeded. The target table is derived internally from the sandbox identity, never accepted as a caller string.

**Constraints the probe must settle:** `EXCHANGE PARTITION` cannot exchange into a destination where the partition already exists and requires matching schemas · `ALTER TABLE … SET LOCATION` is documented DDL but does not by itself prove atomic visibility across Spark, Hive, cached sessions and other readers · **schema evolution is unresolved by partition swapping** — adding a feature changes the table schema, which a partition-location swap does not atomically change.

**Preferred first-slice mechanism:** immutable versioned physical outputs with one reader-visible pointer/view switch — still requiring demonstration.

**Proof requirements:** concurrent readers polling throughout the swap observe only complete states, discriminated by a **generation marker plus a content check** (schema and row count alone can coincide), and the probe **must include adding a feature to an existing group**. `INSERT OVERWRITE` is rejected outright.

---

## §11 Run execution and the validation loop

### §11.1 Prepared parameters are what execution reads

Resolving and validating exact partitions is worthless if the generated project then reads whatever it likes. Run preparation produces `RunPreparation.parameters`, and **submission passes them to execution**:

```python
prep = prepare_run(result, business_dt=…, inventory=…, metastore=…)
submit_and_run(project, run_parameters=prep.parameters)     # NOT business_dt alone
```

The generated project **must**: apply exactly the prepared partition predicates; **refuse to run** if a snapshot parameter is missing or unexpected (`RUN_PARAMETERS_MISSING`); write the same snapshot ids, `run_id` and `sandbox_execution_hash` into its manifests; and **never re-resolve partitions itself** during execution. An execution test proves Spark read precisely the prepared partitions and nothing else.

### §11.2 Validation loop

**L0** — materialize to a temp directory, install into an isolated environment, **import the project and build the Kedro pipeline object**, and verify `generated_project_hash` against `GENERATED.lock`. Parsing source text does not prove what L0 claims.
**L1** — metastore metadata only, over **every feature IR, every expression, and the spine**: each read-set column exists with the declared type, is readable under the caller's roles, and **every resolved partition in the run's `PhysicalInputSnapshot`s exists** (L1 runs at run preparation, after snapshots resolve).
**L2 (on demand)** — tiny-sample execution: Spark analysis errors, the §9 gates.
**L3** — the real run.

`ValidationReportV1` carries `report_id`, `generation_id`, `generated_project_hash`, `group_plan_hash`, `level`, `environment_id`, timing, `status ∈ {passed, failed, error}` and findings. Each finding: closed-vocabulary `code`, `severity`, `classification`, `location`, `expected`/`observed` **as type and schema facts only**, and a `count`.

**Classification** routes the fix: `RENDERER_DEFECT` (fix renderer, regenerate) · `GOVERNED_FACT_MISMATCH` (code is right, catalog is wrong — re-attest; **blocks regeneration**) · `ENVIRONMENT_OR_DATA` (operator acts) · `UNCLASSIFIED` (**fails closed**; never silently environmental). A regenerated project restarts at L0; results never carry across a changed `generated_project_hash`.

Submission is behind a `PipelineSubmitter` seam with one implementation, `LocalClusterSubmitter`. An unreachable cluster yields `status="error"` with **zero** findings — never invented ones.

---

## §12 `RunManifestV1` and the control plane

```python
@dataclass(frozen=True, slots=True)
class RunManifestV1:
    run_id: str
    generation_id: str                    # FK to the exact generation
    group_plan_hash: str
    materialization_contract_hash: str
    generated_project_hash: str
    sandbox_execution_hash: str
    business_dt: str
    publication_mechanism: str
    capability_attestation_id: str
    expected_feature_columns: tuple[str, ...]
    staged_row_count: int | None
    published_row_count: int | None
    schema_hash: str | None
    key_uniqueness_result: str | None
    required_column_result: str | None
    orphan_grain_key_count: int | None    # §4.1
    publication_location: str | None
    started_at: str | None
    published_at: str | None
    status: str
```

Because `status` implies mutation, the control plane records **append-only `materialization_run_event` rows** (UPDATE, DELETE **and TRUNCATE** all blocked — a `FOR EACH ROW` trigger does not fire on TRUNCATE) and folds them to derive current status. A single terminal manifest row may be inserted at the end; nothing is updated in place.

Generation records, validation reports, run events and the terminal manifest all have owning implementations and ingestion paths — none is assumed.

---

## §13 Testing

1. **Golden-file render tests** — rendered bytes vs committed goldens; identity headers; `generated_project_hash` stability.
2. **Spark-local execution — MANDATORY, runs by default.** Executes the generated project on tiny hand-authored fixtures with hand-computed expected values for **every** first-slice feature, plus: look-ahead exclusion; one row per `(keys…, business_dt)`; an entity with no source rows still present; zero-denominator policy; declared rounding; overflow ERROR actually failing (not NULL); empty-window policy; null policy; and every §9 gate firing on a deliberately broken group.
3. **Cluster acceptance — MANDATORY final task**, blocked on §0. Capability probe recorded; L0/L1 pass over **all** IRs; `kedro run`; then verify the published table: exact expected schema, all feature columns present, generation marker matching this run, manifest row count equal to Hive's, bounded non-null/type checks per feature, atomic reader observations, published project hash matching the generated project, and **the acceptance fixture executed on the cluster producing the same numbers as Spark-local**.

Fixtures are hand-authored and validated against the real Child-1 resolver — a fixture claiming `ADDITIVE` for a plain `SUM` is a forgery, since Child-1 resolves `NON_ADDITIVE` without `path_additive`, and `COUNT_DISTINCT` resolves `NON_ADDITIVE` with logical type `integer`.

---

## §14 Failure vocabulary — four closed enums

Every refusal uses a value from one of these enums. **A governed refusal never surfaces as a bare `TypeError`/`ValueError`**, and no code may be used that is not listed here.

```python
class CompilationRefusalCode(StrEnum):          # raised/returned during compile
    AUTHORING_RUN_INCOMPLETE · TERMINAL_PAYLOAD_TAMPERED · NOT_RESOLVED
    FORMULA_HASH_MISMATCH · AXES_MISMATCH · INTENT_HASH_MISMATCH
    READ_SCOPE_INSUFFICIENT · PROHIBITED_INPUT
    AMBIGUOUS_TABLE_NAME · JOIN_PATH_NOT_VERIFIED · JOIN_PATH_DENIED_BY_READ_SCOPE
    GRAIN_PATH_NOT_GOVERNED · JOIN_FANOUT_UNSUPPORTED · JOIN_CARDINALITY_UNKNOWN
    SPINE_SOURCE_NOT_DECLARED · SPINE_DECLARATION_REJECTED_BY_FACTS
    PARTITION_MAPPING_NOT_DECLARED · PHYSICAL_SCHEMA_NOT_RESOLVED
    SOURCE_ENGINE_UNSUPPORTED
    AVAILABILITY_TIME_NOT_GOVERNED
    PHYSICAL_TYPE_UNSUPPORTED · MULTIPLE_MATERIALIZATION_CONTRACTS
    PARTITION_IDENTITY_UNKNOWN · UNACCOUNTED_LOGICAL_REF

class PublicationRefusalCode(StrEnum):          # pre-execution publication decisions
    CAPABILITY_UNPROVEN            # no passing probe attestation for THIS environment/versions
    GROUP_BINDING_CONFLICT         # the logical name is bound to a different contract hash
    PUBLISH_MECHANISM_UNSUPPORTED  # a probe RAN and proved no mechanism satisfies the invariant

class ValidationGateCode(StrEnum):              # BLOCKING gates in the generated pipeline (§9)
    KEY_NOT_UNIQUE · MISSING_FEATURE_COLUMN · UNEXPECTED_COLUMN
    WRONG_COLUMN_TYPE · WRONG_NULLABILITY · SCHEMA_HASH_MISMATCH
    MISSING_STAGING_MANIFEST · STALE_STAGING_MANIFEST · DUPLICATE_STAGING_MANIFEST
    IR_HASH_MISMATCH · INCOMPLETE_COMPUTATION · FORBIDDEN_NUMERIC
    OVERFLOW_VIOLATION · SPINE_INCOMPLETE · SPINE_DUPLICATE_KEY
    SPINE_NON_DETERMINISTIC · RUN_PARAMETERS_MISSING · PROJECT_INTEGRITY

class ValidationFindingCode(StrEnum):           # L0/L1/L2 findings (§11), non-blocking by themselves
    PROJECT_DOES_NOT_BUILD · PROJECT_HASH_MISMATCH · PIPELINE_NOT_CONSTRUCTIBLE
    COLUMN_ABSENT · COLUMN_TYPE_MISMATCH · PARTITION_ABSENT · READ_DENIED
    UNKNOWN_FINDING                              # → FindingClass.UNCLASSIFIED, fails closed
```

**`CAPABILITY_UNPROVEN` vs `PUBLISH_MECHANISM_UNSUPPORTED`.** The first means no passing attestation exists for this environment and version triple — nobody has proved anything yet. The second means a probe **did** run and demonstrated that no available mechanism satisfies the atomic-visibility invariant. They route differently: the first is "go run the probe", the second is "this cluster cannot publish atomically, and the design must change rather than the claim". §10.3's probe is what distinguishes them, and **Task 16 must confirm this split against what the probe can actually observe.**

`SPINE_NON_DETERMINISTIC` is a **runtime** gate — an unresolved tie depends on actual rows, so it is discovered during execution, not compilation. `CAPABILITY_UNPROVEN` and `GROUP_BINDING_CONFLICT` are publication decisions and live in `PublicationRefusalCode`; a refusal must never fall back to comparing a raw string because its code is missing from the enum it is typed to.

**Enum tests use `==`, never `>=`.** A superset assertion permits arbitrary extra codes and therefore does not test a closed vocabulary at all.

**Normalize-then-refuse.** Classification normalizes an unknown `effective_restriction` to `prohibited` **internally** (per `safety_floor`) and then refuses with `PROHIBITED_INPUT`. Rev 3 asserted both that unknown *returns* `prohibited` and that `prohibited` *raises* — which cannot both hold through one public API. Internal normalization, single public refusal.

---

## First-slice bounds

One entity (CIF) · one cadence (daily) · one materialization group · one `business_dt` per run · one Hadoop/Hive environment · no scan sharing · no fan-out · L0+L1 standard with L2 on demand · local submitter only · sandbox only · no UI · no cross-cadence assembly · no statistical profiling.

### Choosing the acceptance features (normative)

The acceptance features are **not named in advance**. They are whichever authored features the platform's own authoring chain produces that cover the **three shapes** the acceptance test must exercise:

| Shape | What it proves |
|---|---|
| Aggregate with a filter (e.g. `SUM … WHERE …`) | `DECIMAL(p,s)` from `DecimalPolicy`, explicit rounding, overflow **raising** rather than yielding Spark's default NULL |
| `COUNT_DISTINCT` | `BIGINT`, and Child-1's non-additive resolution |
| A ratio | the zero-denominator policy and column nullability |

**Why not name them:** a formula produced by the authoring chain references catalog columns *by construction* (its §I tools read the real catalog) and can only enter materialization through §1.2's gate against an immutable terminal event. Hand-picked names carry no such guarantee — earlier revisions of this spec named two features that were invented as illustrations and then propagated as if they were requirements.

The features are selected at acceptance time (§13 tier 3), from a real authoring run against the ingested catalog. If the data model cannot support one of the shapes, the acceptance test substitutes another feature of that shape and records the substitution.

---

## Deferred NFRs

**From the parent architecture:** Iceberg revisions/time-travel/restatement · run state machine · outbox/reconciliation · external attestation and authenticated callbacks (Child-6) · execution-signature batching · quarantine by bisection · profiling privacy (Spec B) · the full Child-2 lifecycle · the full `TemporalPolicyV1`.

**Identified here:** governed **source-delivery SLA** (prerequisite for deriving `availability_class`) · governed **`ENTITY_POPULATION_SOURCE`** fact, superseding §4's declaration — *trigger: before production publication, or supporting multiple entity populations* · governed **fan-out allocation policy** (§3.2) · `dependencies_ready` trigger · authenticated submission into a real bank environment · content-addressed input snapshots · multi-partition/backfill runs · multi-environment promotion · computation scan sharing · governed per-column retention · `SATURATE` overflow behaviour.

**Newly recorded in rev 4:** governed `ENTITY_POPULATION_SOURCE` fact (supersedes §4's declaration) · governed fan-out **allocation policy** (§3.2) · content-addressed input snapshots (§3.3) · production publication path (Child-2 must supply a binding-validating factory).

**NOT deferred despite resembling NFRs:** PIT correctness · join cardinality handling · atomic group publication · the §9 gates · derived sensitivity/access classification · physical type and overflow semantics · Spark-local and cluster execution proofs.

---

## Out of scope

**Spec B** — profiling/EDA, `feature_output_profile`, the data-plane→control-plane profile protocol, access-controlled UI. **Spec C** — `model_input` assembly with daily/monthly as-of alignment.

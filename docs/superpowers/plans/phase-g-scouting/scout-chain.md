# Scout report — the materialize chain, exact interfaces for an orchestrator

Worktree: `/Users/ascoe/Projects/ai/feature-engineering/.claude/worktrees/phase-g` (branch
`worktree-codegen-review-remediation`, HEAD `2e5f3e45`). All paths absolute-relative to that root
unless noted. **Read-only scout — nothing was modified.**

Package total: 16,764 lines across 29 modules in `src/featuregen/materialize/`.

---

## 0. The headline: there is no orchestrator, and the chain is NOT a straight line

`src/featuregen/materialize/compile/__init__.py:18` states it in the source:

> "Nothing else lives here yet: §2's orchestrator is not built, and inventing a shell for it would be
> a second place the chain is described."

The docstring (lines 10–16) also **reserves the module path**: the chain must land in
`featuregen/materialize/compile/chain.py` (a SIBLING inside the `compile` package), because
`compile/__init__.py` must stay import-free — `identity.py:52` does `from featuregen.materialize
import binding, compile, render`, so a chain in `compile/__init__.py` would close an import cycle
(`compile → identity → compile`). A test pins the import-free property for both `compile/` and
`render/`.

The chain as given in the task prompt has **two ordering errors**:

1. `select_publisher` must run **BEFORE** `render_project` (its `PublisherSelection` is rendered
   into `conf/base/catalog.yml`, `render/project.py:1242-1243`) **and before** `prepare_run`
   (which needs `capability_attestation_id`, `runprep.py:841`). It is not a post-run step.
2. `bind_group` must run **before** `render_project` in practice — not for a data dependency
   (`render_project` derives the target itself via `physical_target_for`), but because
   `GROUP_BINDING_CONFLICT` is the one refusal that says "do not render for this name at all".

The true order is:

```
admit_artifacts
  → compile_ir (per feature)                          [needs spine_decl + inventory + roles]
  → authorize_compilation (Gate 2, group-wide)        → AuthorizedCompilation TOKEN
  → derive_group_contract (wraps classify + derive_contract + group_by_contract)
  → resolve_physical_type (per feature, physical_types.py — NOT in the prompt's chain, REQUIRED)
  → build_group_plan
  → bind_group + plan_revision                        [pure; caller persists]
  → select_publisher                                  [needs live published_schema]
  → derive_requirement (spine table)                  [inputs.py — NOT in the prompt's chain]
  → project_datasets
  → render_spine_node / render_projection_node / render_calculation_node
    / render_join_precondition_node / render_assembly_node / render_gate_node
      ← THE ORCHESTRATOR MUST BUILD THIS SEQUENCE ITSELF. Nothing in src/ does.
  → render_project (calls build_compilation_identity + seal_project internally)
  → materialize_to
  → run_l0
  → run_input_requests + spine_input_request
  → authorize_execution_realizations (cross-catalog only, immediately before prepare_run)
  → prepare_run
  → run_l1
  → submit
  → record manifests/events
  → publish  ← DOES NOT EXIST (see §E.1)
```

---

## 1. Stage-by-stage interfaces

### 1.1 `admission.admit_artifacts` — Gate 1

`src/featuregen/materialize/admission.py:140-157`

```python
def admit_artifacts(
    conn: DbConn, inputs: Iterable[ResolvedFeatureInput]
) -> tuple[AdmittedFeature, ...]:
```

- **Caller supplies:** `DbConn`; one `ResolvedFeatureInput(intent: AuthoringIntent, result:
  AuthoringResult)` per feature (`admission.py:111-122`).
- **Derives internally:** everything else. Reads `formula.trace.read_terminal_event` and
  `read_run_intent_hash`; re-derives `formula_content_hash` from the supplied formula object
  (`admission.py:239-274` — the supplied `result.candidate_formula_hash` field is *never read*);
  folds the feature name through `hive_identifier`.
- **Returns:** `tuple[AdmittedFeature, ...]` — `AdmittedFeature(feature_name, formula,
  formula_content_hash, intent, authoring_run_id)` (`admission.py:124-138`).
- **Refusal contract: RAISES.** No partial admission — the first failure raises and the batch yields
  nothing.
  - `MaterializationRefused(CompilationRefusalCode.<one of six>)`:
    `AUTHORING_RUN_INCOMPLETE`, `TERMINAL_PAYLOAD_TAMPERED`, `NOT_RESOLVED`,
    `FORMULA_HASH_MISMATCH`, `AXES_MISMATCH`, `INTENT_HASH_MISMATCH`.
  - `FeatureNamePlanError` (`admission.py:98`, a plain `Exception`, NOT a §14 code) — two names
    normalize to one Hive identifier, or a name cannot be one at all.
- **Ordering/preconditions:** first stage; nothing precedes. Explicitly does NOT authorize reads
  (docstring lines 40–43): "availability columns, join hops, bridge tables and the spine are only
  discovered during compilation, so authorization is Gate 2."

Helper the orchestrator will also need:

```python
def hive_identifier(name: str) -> str:      # admission.py:330
```
Idempotent; PUBLIC precisely so the group plan reaches the same answer.

---

### 1.2 `ir.compile_ir` — per-feature IR

`src/featuregen/materialize/ir.py:232-240`

```python
def compile_ir(
    conn: DbConn,
    admitted: AdmittedFeature,
    *,
    roles: Iterable[str] = (),
    spine_decl: SpineSourceDeclarationV1 | None,
    inventory: ClusterInventoryV1,
    bridge_realizations: tuple[CurrentBridgeRealizationV1, ...] | None = None,
) -> FormulaExecutionIRV1 | MaterializationRefused:
```

- **Caller supplies:** `conn`, one `AdmittedFeature`, `roles`, `spine_decl` (**keyword-only, no
  default** — must be passed, even as `None`), `inventory: ClusterInventoryV1`.
- **Derives internally:** `bridge_realizations` when `None` — calls
  `executable_bridge_realizations(conn, purpose="feature_generation",
  environment=inventory.environment_id)` (`ir.py:270-274`). **This is the production entry point;
  `None` must NOT be read as "no bridges".**
- **Also derives:** the whole `SpineSpec` by calling `validate_spine_declaration(conn, spine_decl,
  roles=roles_used)` **on every call** (`ir.py:275`). Per-feature spine re-validation is deliberate;
  "declared once per contract" is enforced in `authorize_compilation`, not here (`ir.py:253-256`).
- **Returns:** `FormulaExecutionIRV1` (`ir.py:118-141`) or `MaterializationRefused`
  **RETURNED, not raised** — "a refused feature is one governed verdict among the many a compilation
  collects" (`ir.py:243`).
- **Raises:** `featuregen.formula.schema.SchemaError` if `formula.body` is outside Child-1's union.
- **Check order (first failure decides the code):** spine → grain entity (`GRAIN_PATH_NOT_GOVERNED`)
  → each body expression via `compile_expression`.
- **Fields on `FormulaExecutionIRV1`:** `feature_name, formula_content_hash, final_operation,
  zero_denominator, grain_entity, grain_keys, expressions, spine, output_policy, authoring_run_id`.
  `authoring_run_id` is provenance and is **excluded** from `identity_payload()` (`ir.py:127-130`).

```python
def ir_hash(ir: FormulaExecutionIRV1) -> str:   # ir.py:181
```

---

### 1.3 `ir.authorize_compilation` — Gate 2 (group-wide)

`src/featuregen/materialize/ir.py:565-571`

```python
def authorize_compilation(
    conn: DbConn,
    irs: Sequence[FormulaExecutionIRV1],
    spine: SpineSpec,
    *,
    roles: Iterable[str] = (),
) -> AuthorizedCompilation | MaterializationRefused:
```

- **Caller supplies:** `conn`, all IRs, the ONE `SpineSpec` (typically `irs[0].spine`), `roles`.
- **Derives internally:** the complete §1.3 read-set union (`_union_of`, `ir.py:443-488`) — every
  expression read, every join endpoint taken from the PLAN (not only the read set), every
  availability column, plus the spine's source table / ordered keys / read set / availability. Runs
  ONE `graph_node` fetch per catalog source (`_hidden`, `ir.py:514-562`).
- **Returns:** `AuthorizedCompilation(irs, spine, authorized_refs, roles_used)` (`ir.py:186-202`)
  — a TOKEN, not a boolean, and the only way into the downstream chain — or
  `MaterializationRefused` **RETURNED**:
  - `COLUMN_NOT_GOVERNED` (existence decided FIRST, `ir.py:612-624`)
  - `READ_SCOPE_INSUFFICIENT` (`ir.py:625-635`)
  When a group has both, existence wins.
- **RAISES `ValueError`** for three call-assembly defects (`ir.py:584-591`): empty group; an IR
  compiled against a different spine (`identity_payload()` inequality, `ir.py:600-607`); an IR with
  join steps and an empty read set.
- **Ordering:** must run AFTER every `compile_ir`. `derive_group_contract`, `project_datasets` and
  `render_project` all `isinstance`-check for this token and `TypeError` without it.

Two companions:

```python
def physical_read_set(irs: Sequence[FormulaExecutionIRV1], spine: SpineSpec) -> tuple[str, ...]   # ir.py:496
def bridge_realization_dependencies(irs) -> tuple[tuple[str, str], ...]                           # ir.py:322
```

---

### 1.4 `ir.authorize_execution_realizations` — cross-catalog only

`src/featuregen/materialize/ir.py:335-340`

```python
def authorize_execution_realizations(
    conn: DbConn,
    authorized: AuthorizedCompilation,
    *,
    environment_id: str,
) -> BridgeExecutionAuthorization | MaterializationRefused:
```

- **Explicit ordering statement** (`ir.py:341-347`): "minted separately, **immediately before run
  preparation**" — i.e. after rendering, not at compile time.
- Returns `BridgeExecutionAuthorization(ir_hashes, environment_id, realization_dependencies)`
  (`ir.py:205-218`), or `MaterializationRefused(JOIN_CARDINALITY_UNKNOWN)` — that code is reused for
  bridge staleness (`ir.py:366-370`, `ir.py:378-382`); §14 has no bridge-specific member.
- **Raises** `TypeError` if not given the Gate-2 token; `ValueError` on blank `environment_id`.
- If `bridge_realization_dependencies(authorized.irs)` is empty, it returns a token with an empty
  dependency tuple and performs no work.

---

### 1.5 `classify.classify_read_set` — §5.2 (called *by* `derive_contract`)

`src/featuregen/materialize/classify.py:123-125`

```python
def classify_read_set(
    conn: DbConn, refs: Iterable[str]
) -> Classification | MaterializationRefused:
```

- **Returns:** `Classification(sensitivity_class, access_requirements, unclassified_refs,
  classification_policy_version)` (`classify.py:93-106`), or
  `MaterializationRefused(PROHIBITED_INPUT)` **RETURNED** (`classify.py:166-173`).
- **Raises `ValueError`:** empty `refs`; a ref that is not a normalized `logical_ref`; a
  `graph_node.sensitivity` outside the shipped vocabulary (a catalog integrity violation,
  `classify.py:183-188`).
- `CLASSIFICATION_POLICY_VERSION = 2` (`classify.py:77`) enters the contract hash.
- An orchestrator normally never calls this directly — `derive_contract` does (`contract.py:612`).

---

### 1.6 `contract.derive_group_contract` — §5 (the only entry point to a publishable group)

`src/featuregen/materialize/contract.py:733-740`

```python
def derive_group_contract(
    conn: DbConn,
    authorization: AuthorizedCompilation,
    *,
    cadence: CadenceDecl,
    availability_promise: AvailabilityPromiseV1,
    overrides: ContractOverrides | None = None,
) -> ContractGroup | MaterializationRefused:
```

- **Caller supplies:** the Gate-2 token, a `CadenceDecl` and an `AvailabilityPromiseV1` — **both
  are DECLARATIONS the platform does not derive.** `CadenceDecl(period, timezone,
  business_date_cutoff, trigger)` (`contract.py:291-334`); today `CadencePeriod.DAILY` is the only
  member and `CadenceTrigger` is `SCHEDULED | MANUAL`. `AvailabilityPromiseV1(kind, calendar_days,
  plus_minutes)` (`contract.py:170-260`); the plain constructor REFUSES non-canonical input, use
  `AvailabilityPromiseV1.normalized(...)` (`contract.py:220-250`) to canonicalize arithmetic.
- **Derives internally:** each feature's own §1.3 read set via `physical_read_set((ir,), ir.spine)`,
  its classification, and the group key. Constants that enter the hash and are **not** caller-
  supplied: `DEFAULT_RETENTION_CLASS`/`RETENTION_POLICY_VERSION` (`contract.py:102,106`),
  `BUSINESS_DT_COLUMN = "business_dt"` (`contract.py:110`), `PublicationPolicy.ATOMIC_GROUP`,
  `BackfillBoundary.GROUP_LEVEL` (`contract.py:644-645`).
- **Returns:** `ContractGroup(contract_hash, contract, feature_names)` (`contract.py:560-566`), or
  `MaterializationRefused` **RETURNED**: `PROHIBITED_INPUT` (from `derive_contract`) or
  `MULTIPLE_MATERIALIZATION_CONTRACTS` (from `group_by_contract`, `contract.py:720-726`).
- **Raises:** `TypeError` if `authorization` is not the token; `ValueError` if two IRs share a
  `feature_name` (`contract.py:768-773`), or if an override loosens/moves-earlier
  (`contract.py:667-682`, `contract.py:429-434`).

Sub-functions the orchestrator may also want:

```python
def derive_contract(conn, ir, *, cadence, availability_promise, overrides=None
                    ) -> MaterializationContractV1 | MaterializationRefused   # contract.py:569
def group_by_contract(contracts: Mapping[str, MaterializationContractV1]
                      ) -> ContractGroup | MaterializationRefused             # contract.py:686
def contract_hash(contract: MaterializationContractV1) -> str                 # contract.py:555
```

---

### 1.7 `physical_types.resolve_physical_type` — **missing from the prompt's chain, mandatory**

`src/featuregen/materialize/physical_types.py:375-379`

```python
def resolve_physical_type(
    formula: TypedFormulaV1,
    *,
    operand_types: Mapping[str, OperandTypeEvidence],
) -> PhysicalType | MaterializationRefused:
```

- Needed to build every `PlannedFeature`. `operand_types` is exactly
  `{e.expr_path: e.operand_type for e in ir.expressions}` (`physical_types.py:403-406`).
- **This is the only step that needs the ORIGINAL `TypedFormulaV1` after `compile_ir`** — the
  orchestrator must keep the `AdmittedFeature` objects alive past compilation.
- Returns `PhysicalType` or `MaterializationRefused(PHYSICAL_TYPE_UNSUPPORTED)` **RETURNED**.
- Raises `ValueError` if `operand_types` does not describe exactly the formula's expressions.

---

### 1.8 `group_plan.build_group_plan` — §6/§9/§10.2

`src/featuregen/materialize/group_plan.py:287-292`

```python
def build_group_plan(
    group: ContractGroup,
    features: Sequence[PlannedFeature],
    *,
    logical_group_name: str,
) -> FeatureGroupPlanV1:
```

- **Caller supplies:** the `ContractGroup`, one `PlannedFeature(column_name, ir_hash,
  physical_type)` per member (`group_plan.py:132-165`), and the **logical group name** — a human
  decision with no source anywhere in the package.
- **Derives internally:** landing key columns from `group.contract.ordered_keys` via `parse_ref` +
  `hive_identifier` (`group_plan.py:251-272`), the `business_dt` column, `SYSTEM_COLUMNS`
  (`group_plan.py:73-77`), `PHYSICAL_TYPE_POLICY_VERSION`.
- **Returns:** `FeatureGroupPlanV1(logical_group_name, materialization_contract_hash,
  entity_key_columns, business_dt_column, features, physical_type_policy_version)`
  (`group_plan.py:168-198`).
- **NO refusal path. RAISES only:** `TypeError` (not a `ContractGroup`), `ValueError` (features ≠
  group members; a key ref that names a table), `FeatureNamePlanError` (collision / unrenderable
  name).

Companions:

```python
def group_plan_hash(plan) -> str                                  # group_plan.py:201
def expected_schema(plan) -> tuple[PlannedColumn, ...]            # group_plan.py:349
def expected_schema_hash(plan) -> str                             # group_plan.py:372
def check_completeness(plan, manifests, *, generation_id, run_id, business_dt
                       ) -> tuple[GateFailure, ...]               # group_plan.py:390
```
`check_completeness` returns EVERY failure (empty tuple = proceed) and raises `ValueError` on a
blank binding value. **It has zero callers in `src/`** — see §E.3.

---

### 1.9 `binding.bind_group` / `plan_revision` / `current_plan_revision` — §10.1

`src/featuregen/materialize/binding.py:134-139, 200-206, 229-233`

```python
def bind_group(
    plan: FeatureGroupPlanV1,
    *,
    binding_id: str,
    existing: GroupContractBinding | None = None,
) -> GroupContractBinding | MaterializationRefused:

def plan_revision(
    plan: FeatureGroupPlanV1,
    binding: GroupContractBinding,
    *,
    generation_id: str,
    created_at: str,
) -> GroupPlanRevision:

def current_plan_revision(
    revisions: Sequence[GroupPlanRevision],
    *,
    published_generation_ids: Collection[str],
) -> GroupPlanRevision | None:
```

- **PURE — no `DbConn` anywhere in this module** (docstring `binding.py:21-24`). The caller fetches
  `existing` via `control_plane.read_group_binding(conn, name)` and persists via
  `control_plane.record_group_binding` / `record_plan_revision`.
- **Caller supplies:** `binding_id` (an id factory the orchestrator must own — this module has none),
  `generation_id`, `created_at` (**must be offset-aware ISO 8601**, `binding.py:118-131`).
- **Returns:** `bind_group` returns the existing binding UNCHANGED when the contract still agrees, or
  `MaterializationRefused(PublicationRefusalCode.GROUP_BINDING_CONFLICT)` **RETURNED**
  (`binding.py:182-196`).
- **Raises `ValueError`:** blank `binding_id` on a first bind; `existing`/`binding` for a different
  logical name (a lookup bug, `binding.py:175-180`, `binding.py:217-221`); revisions from >1 binding;
  a tie on the latest published instant (`binding.py:273-276`).
- `physical_target_for(logical_group_name) -> str` (`binding.py:59`) = `f"{SANDBOX_NAMESPACE}.{hive_identifier(name)}"`,
  `SANDBOX_NAMESPACE = "sandbox_feature"` (`binding.py:56`). **There is no production namespace and
  no gate to unlock one** (`identity.py:28-32`).

---

### 1.10 `publish.select_publisher` — §10.3 (must precede render)

`src/featuregen/materialize/publish.py:552-560`

```python
def select_publisher(
    conn: DbConn,
    *,
    environment_id: str,
    engine_versions: EngineVersions,
    mechanism: PublishMechanism,
    group_plan: FeatureGroupPlanV1,
    published_schema: Sequence[str] | None,
) -> PublisherSelection | MaterializationRefused:
```

- **Caller supplies:** `published_schema` — **the live column list of the currently published table,
  with NO default** (`publish.py:566-568`). There is no seam in `materialize/` that fetches it;
  `validation.MetastoreMetadata.describe_table` (`validation.py:666`) is the nearest thing and the
  orchestrator must adapt it.
- **Derives internally:** `adds_feature` via `adds_feature_for(group_plan, published_schema)`
  (`publish.py:523-549`; `None` ⇒ `True`, fail-closed). Explicitly NOT a parameter (`publish.py:16-18`).
- **Returns:** `PublisherSelection(environment_id, mechanism, capability_attestation_id,
  engine_versions, adds_feature)` (`publish.py:488-520`), or `MaterializationRefused`
  **RETURNED** with `CAPABILITY_UNPROVEN` (no attestation / version drift / adds a column and no
  passing attestation covers schema evolution) or `PUBLISH_MECHANISM_UNSUPPORTED` (a matching
  attestation FAILED, or a failure ties the newest `recorded_at`, `publish.py:655-665`).
- **Raises `TypeError`** on a raw-string mechanism or loose engine versions.
- Only `PublishMechanism.VERSIONED_POINTER` can be RENDERED (`render/publish.py:62`), even though
  `EXCHANGE_PARTITION` and `SET_LOCATION` are legal probe targets.
- Attestations exist only by ingesting a probe:
  `record_attestation(conn, probe_result) -> PublicationCapabilityAttestation` (`publish.py:408`),
  built by `assess_probe_observations(observations, *, probe_id, environment_id, mechanism,
  engine_versions, completed_at) -> ProbeResult` (`publish.py:315-323`). The live driver
  `probe_publication_capability(cluster, ...)` referred to at `publish.py:326-327` **does not exist
  in the repository** — grep finds no definition.

---

### 1.11 `inputs.derive_requirement` — §3.3/§3.5 (needed for the spine)

`src/featuregen/materialize/inputs.py:271-273`

```python
def derive_requirement(
    conn: DbConn, inventory: ClusterInventoryV1, *, table_ref: str
) -> PhysicalInputRequirement | MaterializationRefused:
```

- The orchestrator must call this for the spine's table (`authorized.spine.source_table_ref`);
  expression-side requirements are already on `expression.input_requirements`.
- Returns `PhysicalInputRequirement(catalog_source, schema, table, partition_columns,
  partition_mapping, layout_fingerprint, catalog_state_stamp)` (`inputs.py:64-110`), or
  `MaterializationRefused` **RETURNED**: `AMBIGUOUS_TABLE_NAME`, `PHYSICAL_SCHEMA_NOT_RESOLVED`,
  `PARTITION_IDENTITY_UNKNOWN`, `PARTITION_MAPPING_NOT_DECLARED`.
- Raises `ValueError` if given a COLUMN ref.
- `requirement_id()` is DERIVED (`inputs.py:104-110`), never stored.

---

### 1.12 `render/project.project_datasets` and the node renderers

`src/featuregen/materialize/render/project.py:294-299`

```python
def project_datasets(
    authorized: AuthorizedCompilation,
    plan: FeatureGroupPlanV1,
    *,
    spine_input: PhysicalInputRequirement,
) -> ProjectDatasets:
```

`ProjectDatasets` (`project.py:141-168`) fields: `raw` (keyed by folded `"<schema>.<table>"`),
`join_gates` (keyed by `realization_revision_id`), `spine`, `projections` (keyed by
`(column, expr_path)`), `staging`, `manifests`, `assembled`, `published`.

Raises `TypeError` (wrong types) / `ValueError` (dataset-name collision, or `spine_input` describes
a table other than the declared population's, `project.py:326-331`).

**The node renderers the orchestrator must call — nothing in `src/` assembles them:**

```python
def render_spine_node(spine: SpineSpec, plan: FeatureGroupPlanV1,
                      contract: MaterializationContractV1, *,
                      spine_input: PhysicalInputRequirement,
                      source_dataset: str, spine_dataset: str) -> RenderedNode
                                              # render/nodes_compute.py:342

def render_projection_node(expression: ExpressionExecutionIR,
                           contract: MaterializationContractV1, *,
                           feature_column: str, source_dataset: str,
                           projection_dataset: str,
                           joined_datasets: Mapping[str, str] | None = None) -> RenderedNode
                                              # render/nodes_compute.py:873

def render_calculation_node(ir: FormulaExecutionIRV1, feature: PlannedFeature,
                            plan: FeatureGroupPlanV1, *,
                            empty_window: Mapping[str, EmptyWindowResult],
                            null_input: Mapping[str, NullInput],
                            projection_datasets: Mapping[str, str],
                            spine_dataset: str, staging_dataset: str,
                            manifest_dataset: str) -> RenderedNode
                                              # render/nodes_compute.py:1962

def render_join_precondition_node(step: CrossCatalogJoinStepV1, *,
                                  target_dataset: str,
                                  validated_dataset: str) -> RenderedNode
                                              # render/nodes_join_gate.py:86

def render_assembly_node(plan: FeatureGroupPlanV1, *, spine_dataset: str,
                         staging_datasets: Mapping[str, str],
                         manifest_datasets: Mapping[str, str],
                         assembled_dataset: str) -> RenderedNode
                                              # render/nodes_gate.py:165

def render_gate_node(plan: FeatureGroupPlanV1, *, assembled_dataset: str,
                     published_dataset: str) -> RenderedNode
                                              # render/nodes_gate.py:515
```

Notes an orchestrator MUST know:

- `render_calculation_node`'s `empty_window` / `null_input` are **required, keyed by body path, and
  come from the FORMULA's `WindowPolicy`** (`formula/schema.py:230-231`), not from the IR —
  `PitSpec` deliberately excludes them (`nodes_compute.py:1982-1991`). Another reason to keep the
  `AdmittedFeature` formulas past compilation.
- `render_projection_node`'s `joined_datasets` is keyed by `"<source>::<schema>.<table>"` OR the
  legacy `"<schema>.<table>"` (`nodes_compute.py:1014-1024`) and must be **exactly** the hops the
  traversal reaches — extra or missing entries raise `ValueError`.
- For cross-catalog, the projection must read the **join-gate output**, not the raw target —
  `_check_wiring` rule 7 (`project.py:636-641`) refuses a bypass.
- The reference wiring is in tests only: `tests/featuregen/materialize/test_render_nodes_compute.py:1882-1908`
  (`_wired_nodes`) plus `tests/featuregen/materialize/test_render_gate.py:104-125`.

---

### 1.13 `render/project.render_project` — §7

`src/featuregen/materialize/render/project.py:1119-1128`

```python
def render_project(
    authorized: AuthorizedCompilation,
    plan: FeatureGroupPlanV1,
    *,
    environment_id: str,
    engine_versions: EngineVersions,
    spine_input: PhysicalInputRequirement,
    nodes: Sequence[RenderedNode],
    publisher_selection: PublisherSelection | None = None,
) -> SealedProject:
```

- **Derives internally:** `build_compilation_identity(authorized.irs, plan)` (line 1236) — the
  identity is never accepted; `project_datasets` (1237); `_check_wiring` (1238); the package name
  `f"{derive_namespace()}_{plan.logical_group_name}"` (1240); `physical_target_for(...)` (1241);
  `required_parameters` = `REQUIRED_RUN_PARAMETERS ∪ {node params:*}` (1231-1234); and calls
  `seal_project` (1268).
- **Returns:** `SealedProject(identity: RenderedArtifactIdentity, files: Mapping[str, str])` —
  files INCLUDE `GENERATED.lock`.
- **NO refusal path. RAISES only** `TypeError` / `ValueError` (wiring does not close, a node's source
  does not define its function, dataset collision, spine table mismatch, IRs ≠ plan features,
  `publisher_selection` disagrees with `environment_id` or `engine_versions`).
- **Fail-closed default:** omitting `publisher_selection` renders a Hive entry with
  `write_mode: errorifexists` that cannot publish over anything (`project.py:546-552`).

```python
def materialize_to(project: SealedProject, root: str | os.PathLike[str]) -> pathlib.Path
                                              # render/project.py:1271
```
Raises `ValueError` if `root` exists and is non-empty (so an orchestrator must hand it a fresh dir).

---

### 1.14 `identity.seal_project` and the two-phase identity

`src/featuregen/materialize/identity.py:185-188, 321, 357, 393, 437-445`

```python
def build_compilation_identity(irs: Sequence[FormulaExecutionIRV1],
                               plan: FeatureGroupPlanV1) -> CompilationIdentity

def generated_project_hash(files: Mapping[str, str]) -> str

def seal_project(compilation: CompilationIdentity, files: Mapping[str, str]) -> SealedProject

def read_lock(document: str) -> RenderedArtifactIdentity

def sandbox_execution_hash(
    rendered: RenderedArtifactIdentity,
    *,
    environment_id: str,
    parameters: Mapping[str, Any],
    business_dt: str,
    input_snapshot_ids: Sequence[str],
    capability_attestation_id: str,
) -> str
```

- **`build_compilation_identity` is the ONLY place `PlannedFeature.ir_hash` is checked against the
  actual `ir_hash(ir)`** (`identity.py:190-196, 236-244`). §9 later gates every staging manifest
  against the PLAN's value.
- `seal_project` is the ONLY path that writes `GENERATED.lock` (`identity.py:359-362`).
- `sandbox_execution_hash` has **no** `compiler_version`/`renderer_version` parameters — it reads
  `compile.COMPILER_VERSION` ("2", `compile/__init__.py:50`) and `render.RENDERER_VERSION` ("3",
  `render/__init__.py:47`) through their modules (`identity.py:514-515`).
- Everything here **RAISES** (`TypeError`/`ValueError`); nothing returns a governed refusal
  (`identity.py:38-41`).

---

### 1.15 `runprep.prepare_run` — §11.1

`src/featuregen/materialize/runprep.py:831-845`

```python
def prepare_run(
    rendered: RenderedArtifactIdentity,
    inventory: ClusterInventoryV1,
    metastore: MetastorePartitions,
    *,
    generation_id: str,
    run_id: str,
    business_dt: str,
    requests: tuple[RunInputRequest, ...],
    staging_base: str,
    capability_attestation_id: str,
    bridge_authorization: BridgeExecutionAuthorization | None = None,
    additional_parameters: Mapping[str, Any] | None = None,
    required_parameters: Sequence[str] = REQUIRED_RUN_PARAMETERS,
) -> RunPreparation | MaterializationRefused:
```

- **Caller supplies:** the rendered identity, the inventory, a `MetastorePartitions` adapter,
  `generation_id`, `run_id`, `business_dt` (**strict ISO calendar date**), the request tuple,
  `staging_base`, `capability_attestation_id` (from the `PublisherSelection`).
- **Derives internally:** `environment_id` off `inventory.environment_id` (`runprep.py:853-855, 906`);
  the staging root via `staging_root_for(staging_base, generation_id=...)` (`runprep.py:754`); the
  execution hash; toolchain versions.
- **Returns:** `RunPreparation(snapshots, sandbox_execution_hash, parameters)`
  (`runprep.py:275-299`; `parameters` is a `MappingProxyType`), or `MaterializationRefused`
  **RETURNED**: `JOIN_CARDINALITY_UNKNOWN` (missing/mismatched bridge authorization,
  `runprep.py:859-876`), `PARTITION_IDENTITY_UNKNOWN` / `PARTITION_MAPPING_NOT_DECLARED` etc. from
  `resolve_snapshots`, `PHYSICAL_TYPE_UNSUPPORTED` from `_date_typed_clock_refusal`
  (`runprep.py:776-828`, DEFERRED A.29).
- **Raises `ValueError`:** malformed `business_dt`; `additional_parameters` overlapping owned keys;
  `required_parameters` omitting a base parameter; prepared params ≠ `required_parameters`
  (`runprep.py:900-919`).
- **Ordering:** requires a `RenderedArtifactIdentity`, so it runs AFTER `seal_project`. Bridge
  authorization must be minted just before. `_date_typed_clock_refusal` runs AFTER snapshots resolve
  and BEFORE the hash — "a run this refuses never acquires an execution identity" (`runprep.py:887-891`).

The request builders:

```python
def run_input_requests(irs: Iterable[FormulaExecutionIRV1]) -> tuple[RunInputRequest, ...]  # runprep.py:599
def spine_input_request(spine: SpineSpec, spine_input: PhysicalInputRequirement, *,
                        business_dt: str) -> RunInputRequest | MaterializationRefused        # runprep.py:622
def resolve_snapshots(inventory, metastore, *, requests, business_dt
                      ) -> tuple[PhysicalInputSnapshot, ...] | MaterializationRefused        # runprep.py:531
def input_snapshot_ids(snapshots) -> tuple[str, ...]                                         # runprep.py:736
def staging_root_for(staging_base: str, *, generation_id: str) -> str                        # runprep.py:754
```

`run_input_requests` **deliberately excludes the spine** (`runprep.py:608-612`); the orchestrator
must add `spine_input_request(...)` or `run_l1` raises (`validation.py:765-768`).
`spine_input_request` is where §4.2's vintage condition refuses:
`SPINE_DECLARATION_REJECTED_BY_FACTS` when a `CurrentSnapshot`/`ActivePopulation` vintage ≠
`business_dt` (`runprep.py:696-703`) or is not a calendar date (`runprep.py:689-695`).

`REQUIRED_RUN_PARAMETERS` (`render/project.py:101-108`) = `("business_dt", "generation_id",
"input_snapshots", "run_id", "sandbox_execution_hash", "staging_root")`.

---

### 1.16 `validation.run_l0` / `run_l1` — §11.2

`src/featuregen/materialize/validation.py:548-558, 771-783`

```python
def run_l0(
    root: str | os.PathLike[str],
    *,
    generation_id: str,
    environment_id: str,
    report_id: str,
    python_executable: str,
    clock: Callable[[], str],
    env: Mapping[str, str] | None = None,
    timeout_seconds: float = 300.0,
) -> ValidationReportV1:

def run_l1(
    rendered: RenderedArtifactIdentity,
    snapshots: Sequence[PhysicalInputSnapshot],
    *,
    irs: Sequence[FormulaExecutionIRV1],
    inventory: ClusterInventoryV1,
    metastore: MetastoreMetadata,
    roles: Sequence[str],
    generation_id: str,
    run_id: str,
    report_id: str,
    clock: Callable[[], str],
) -> ValidationReportV1:
```

- **Caller supplies to L0:** a materialized DIRECTORY (not a `SealedProject` — otherwise
  `PROJECT_HASH_MISMATCH` is unreachable, `validation.py:561-566`); `report_id` (**no id factory in
  this module**); `python_executable` (**no default** — the artifact needs kedro+pyspark, which
  `src/` must never depend on); `clock` (read twice); `env` (must set BOTH `PYSPARK_PYTHON` and
  `PYSPARK_DRIVER_PYTHON`, `validation.py:583-587`).
- **Caller supplies to L1:** the IRs again, the inventory, a `MetastoreMetadata` adapter, the
  `roles` used at Gate 2, ids and a clock. `environment_id` is read off the inventory
  (`validation.py:821`). The spine is read off the IRs and its table off its snapshot — not a
  parameter.
- **Returns:** always a `ValidationReportV1` — never a refusal, never `None`.
  `status ∈ {PASSED, FAILED, ERROR}`; `ERROR` carries **zero** findings and is the only state where
  a validation "could not run" (`validation.py:281-294`). L0 discards findings already collected
  when the probe environment is unreachable (`validation.py:619-627`).
- **Raises:** L0 `ValueError` if `root` has no lock. L1 `ValueError` on empty/disagreeing IRs or a
  missing spine snapshot; any non-`ClusterUnreachable` adapter exception propagates
  (`validation.py:805-809`).
- Regeneration policy: `may_regenerate(report) -> bool` (`validation.py:300`) and the cross-process
  `may_regenerate_for(conn, *, generation_id) -> bool` (`validation.py:381`). Blocking classes are
  `GOVERNED_FACT_MISMATCH` and `UNCLASSIFIED` (`validation.py:153`); `ERROR` also blocks.
- Persisted via `record_validation_report(conn, report)` (`validation.py:321`);
  read via `read_validation_reports(conn, *, generation_id)` (`validation.py:353`).

---

### 1.17 `submit` — §11.1

`src/featuregen/materialize/submit.py:52-67, 70-78, 101, 126, 137-166`

```python
@dataclass(frozen=True, slots=True)
class SubmissionOutcome:
    completed: bool
    returncode: int | None
    detail: str
    @property
    def started(self) -> bool: ...        # returncode is not None

class PipelineSubmitter(Protocol):
    def submit(self, project_root: str | os.PathLike[str], *,
               run_parameters: Mapping[str, Any],
               pipeline_name: str = PIPELINE_NAME) -> SubmissionOutcome: ...

def check_run_parameters(run_parameters: Mapping[str, Any]) -> Mapping[str, Any]

def submission_command(python_executable: str, project_root: pathlib.Path,
                       run_parameters: Mapping[str, Any], pipeline_name: str) -> tuple[str, ...]

@dataclass(frozen=True, slots=True)
class LocalClusterSubmitter:
    python_executable: str
    env: Mapping[str, str] | None = None
    timeout_seconds: float = 3600.0
    def submit(self, project_root, *, run_parameters, pipeline_name=PIPELINE_NAME) -> SubmissionOutcome
```

- **Caller supplies:** `RunPreparation.parameters` verbatim, `python_executable`, `env` (must contain
  both `PYSPARK_*` vars or `submit` raises `ValueError`, `submit.py:170-175`).
- **Never raises a governed refusal.** `check_run_parameters` raises `TypeError`/`ValueError`
  before a process exists. Everything else becomes a `SubmissionOutcome`.
- **The three-way branch the orchestrator needs is exactly:**
  `started is False` ⇒ never ran (spawn failure or timeout kill, `returncode is None`);
  `started and not completed` ⇒ the pipeline ran and failed (`returncode != 0`);
  `completed` ⇒ exit 0.
- `detail` is `f"stderr: {…[-1500:]} | stdout: {…[-500:]}"` (`submit.py:210`) — **the only channel
  by which a §9 gate code reaches the control plane.**

---

### 1.18 `control_plane` — §12

`src/featuregen/materialize/control_plane.py`

```python
def fold_run_status(events: Sequence[MaterializationRunEvent]) -> RunStatus          # :330
def record_generation(conn: DbConn, generation: MaterializationGeneration) -> None   # :376
def append_run_event(conn: DbConn, event: MaterializationRunEvent) -> None           # :387
def read_run_events(conn: DbConn, run_id: str) -> tuple[MaterializationRunEvent, ...]# :414
def run_status(conn: DbConn, run_id: str) -> RunStatus                               # :422
def published_generation_ids(conn: DbConn) -> frozenset[str]                         # :427
def record_run_manifest(conn: DbConn, manifest: RunManifestV1) -> None               # :440
def read_run_manifest(conn: DbConn, run_id: str) -> RunManifestV1 | None             # :460
def record_group_binding(conn: DbConn, binding: GroupContractBinding) -> None        # :472
def read_group_binding(conn, logical_group_name: str) -> GroupContractBinding | None # :481
def record_plan_revision(conn: DbConn, revision: GroupPlanRevision) -> None          # :489
def read_plan_revisions(conn: DbConn, binding_id: str) -> tuple[GroupPlanRevision, ...]# :499
```

- **"Nothing is generated here — every id, hash and timestamp is supplied"** (`control_plane.py:22-28`).
  There is no clock and no id factory. **`seq` is supplied, never `max(seq)+1`** — a
  read-modify-write would be a race. The orchestrator owns: `generation_id`, `run_id`, `binding_id`,
  `report_id`, `probe_id`, every `seq`, and every ISO-8601 offset-aware timestamp.
- `append_run_event` relies on the DATABASE for three guarantees (`control_plane.py:392-405`):
  duplicate `(run_id, seq)` ⇒ `UniqueViolation`; a second terminal event ⇒ `UniqueViolation`;
  any event after a terminal one or a non-extending `seq` ⇒ migration 1044's
  `materialization_run_event_ordered` trigger `RaiseException`. **The orchestrator must handle all
  three; there is no retry/repair path** (append-only triggers leave none).
- `fold_run_status` raises `ValueError` on: no events; events from >1 run; duplicate `seq`; an event
  after a terminal one.
- `RunManifestV1` (`control_plane.py:252-319`) must carry a **terminal** status and, when
  `PUBLISHED`, all of `published_at`/`publication_location`/`published_row_count`.

---

## A. What is PERSISTED between stages vs what lives only in memory

Migration `src/featuregen/db/migrations/1034_materialization_control_plane.sql` creates exactly
seven tables (plus `1044_run_event_ordering.sql`'s trigger). Everything else is in-process.

### A.1 Has a writer

| Record | Writer | Table |
|---|---|---|
| `MaterializationGeneration` | `record_generation` (`control_plane.py:376`) | `materialization_generation` (1034:44) |
| `MaterializationRunEvent` | `append_run_event` (`control_plane.py:387`) | `materialization_run_event` (1034:139) |
| `RunManifestV1` | `record_run_manifest` (`control_plane.py:440`) | `materialization_run_manifest` (1034:173) |
| `GroupContractBinding` | `record_group_binding` (`control_plane.py:472`) | `group_binding` (1034:86) |
| `GroupPlanRevision` | `record_plan_revision` (`control_plane.py:489`) | `group_plan_revision` (1034:98) |
| `ValidationReportV1` | `record_validation_report` (`validation.py:321`) | `pipeline_validation_report` (1034:111) |
| `PublicationCapabilityAttestation` | `record_attestation` (`publish.py:408`) | `publication_capability_attestation` (1034:63) |

### A.2 Has NO writer — in-process only, LOST on a crash

| Stage output | Where it lives | Consequence for resume |
|---|---|---|
| `AdmittedFeature` tuple | memory | Re-derivable: re-run `admit_artifacts` against the immutable trace. Cheap. |
| `FormulaExecutionIRV1` / `ir_hash` | memory | **Not persisted anywhere.** Only the *hashes* survive, inside `GENERATED.lock` (`CompilationIdentity.ir_hashes`). Re-derivable only by re-running `compile_ir` against a catalog that may have moved. |
| `AuthorizedCompilation` (incl. `authorized_refs`, `roles_used`) | memory | **The Gate-2 verdict is never recorded.** Nothing in the control plane says which refs were authorized or under which roles. Resume = re-authorize. |
| `Classification` (`sensitivity_class`, `access_requirements`, `unclassified_refs`) | memory | Folded into the contract hash; the *values* are nowhere. |
| `MaterializationContractV1` body | memory | Only `materialization_contract_hash` is stored (generation, binding, manifest). **The contract itself — cadence, cutoff, promise, spine declaration — cannot be read back from the control plane.** |
| `ContractGroup.feature_names` | memory | Approximated by `RunManifestV1.expected_feature_columns`, which only exists at the END of a run. |
| `FeatureGroupPlanV1` body | memory | Only `group_plan_hash`. **The packing list — columns, sql types, per-feature `ir_hash` — is not recoverable.** This is the single biggest resume gap: §9's gate compares manifests against `PlannedFeature.ir_hash`. |
| `SealedProject.files` | disk only, via `materialize_to` | Only `generated_project_hash` is stored. L0 re-derives from disk. |
| `PublisherSelection` | memory | Only `capability_attestation_id` reaches `RunManifestV1`; the mechanism reaches it as `publication_mechanism`. `adds_feature` is lost. |
| `BridgeExecutionAuthorization` | memory | Nowhere. Must be re-minted (and may now refuse). |
| `RunPreparation` (snapshots, parameters) | memory | `sandbox_execution_hash` reaches `RunManifestV1` at the END. **The resolved partitions and the exact parameter document are never persisted** — a crash between `prepare_run` and `submit` loses the run's whole read scope, and re-preparing may resolve differently (a `FULL_SCAN` list, a moved layout fingerprint). |
| `SubmissionOutcome` | memory | Only what the orchestrator chooses to put in `MaterializationRunEvent.detail`. |
| `StagingManifestV1` | the STAGING AREA as JSON, written by the generated pipeline (`render/project.py:538-541`) | Never ingested into the control plane. There is no reader for it in `src/`. |
| `GateFailure` tuple | memory (and only if `check_completeness` is called at all — it never is) | See §E.3. |

**Net:** an orchestrator that crashes after `render_project` and before `record_run_manifest` can
recover only: the generation row, whatever run events it already appended, the files on disk, and
the lock. It **cannot** recover the plan, the contract, the IRs or the prepared parameters. Any
resume design must either (a) newly persist `FeatureGroupPlanV1` + `MaterializationContractV1` +
`RunPreparation.parameters`, or (b) accept full re-compilation and re-verify the resulting hashes
against `GENERATED.lock` (`read_lock`, `identity.py:393`).

---

## B. The identity/hash thread

| Hash | Produced by | Consumed by | Stored? | Re-derived? |
|---|---|---|---|---|
| `formula_content_hash` | `formula.canonical.formula_content_hash`, re-derived in `admission._verify_formula_hash` (`admission.py:261`) | `FormulaExecutionIRV1.formula_content_hash` → `ir.identity_payload()` → `ir_hash`; `CompilationIdentity.formula_content_hashes` | Only inside `GENERATED.lock` | Re-derived at admission from the formula object; never trusted from `result.candidate_formula_hash` |
| `ir_hash` | `ir.ir_hash(ir)` (`ir.py:181`) over `ir.identity_payload()` (`ir.py:143-178`) | `PlannedFeature.ir_hash` (caller-supplied!), `CompilationIdentity.ir_hashes`, §9's `IR_HASH_MISMATCH` gate, `BridgeExecutionAuthorization.ir_hashes` | `GENERATED.lock` only | Re-derived in `build_compilation_identity` and compared against the plan's (`identity.py:236-244`) — **the only such comparison in the chain** |
| `contract_hash` | `contract.contract_hash` (`contract.py:555`) over `MaterializationContractV1.identity_payload()` (`contract.py:531-552`) | group key in `group_by_contract`; `FeatureGroupPlanV1.materialization_contract_hash`; `GroupContractBinding`; `MaterializationGeneration`; `RunManifestV1` | **YES** — 3 tables | Never re-derived downstream; compared as a stored value in `bind_group` |
| `group_plan_hash` | `group_plan.group_plan_hash` (`group_plan.py:201`) | `CompilationIdentity.group_plan_hash`; `GroupPlanRevision`; `MaterializationGeneration`; `RunManifestV1`; every `ValidationReportV1` | **YES** — 4 tables | Re-derived inside `plan_revision` (`binding.py:224`) and `build_compilation_identity` (`identity.py:251`), never accepted |
| `expected_schema_hash` | `group_plan.expected_schema_hash` (`group_plan.py:372`) | rendered INTO the gate node as a literal (`nodes_gate.py:748`); compared at run time ⇒ `SCHEMA_HASH_MISMATCH`; also `StagingManifestV1.schema_hash` and `RunManifestV1.schema_hash` | Only via the manifest | Recomputed by the generated pipeline on real rows |
| `generated_project_hash` | `identity.generated_project_hash(files)` (`identity.py:321`) — sha256 per file, then `materialize_hash`; **excludes `GENERATED.lock` always** | `RenderedArtifactIdentity`; `sandbox_execution_hash`; §10.2 system column `__generated_project_hash`; `MaterializationGeneration`; `RunManifestV1`; every `ValidationReportV1`; the rendered assembly node reads it from the lock AT RUN TIME (`identity.py:386-389`) ⇒ `PROJECT_INTEGRITY` | **YES** — 3 tables | Re-derived by L0 over the project on disk and compared with the lock ⇒ `PROJECT_HASH_MISMATCH` |
| `sandbox_execution_hash` | `identity.sandbox_execution_hash` (`identity.py:437`), called only from `prepare_run` (`runprep.py:905`) | run parameter `sandbox_execution_hash`; §10.2 system column `__sandbox_execution_hash`; `StagingManifestV1`; `RunManifestV1` | **YES** — `materialization_run_manifest` | Rebuildable from `RunPreparation.covered_parameters()` (`runprep.py:289-299`) — the parameters minus itself |
| `requirement_id` | `PhysicalInputRequirement.requirement_id()` (`inputs.py:104`) | snapshot keys; `snapshot_id` | Never | Always derived |
| `snapshot_id` | `PhysicalInputSnapshot.snapshot_id()` (`runprep.py:246`) over `read_payload()` (requirement id + partitions + business_dt) | `input_snapshot_ids` → `sandbox_execution_hash`; the `input_snapshots` run parameter | Never | Always derived |
| `layout_fingerprint` | `materialize_hash(layout.semantic_payload())` (`inputs.py:319`) | inside `requirement_id` ⇒ inside `ir_hash`; re-checked by `resolve_snapshots` ⇒ `PARTITION_IDENTITY_UNKNOWN` on drift | Never | Re-derived per run |
| `evidence_hash` | `assess_probe_observations` (`publish.py:356`) | `PublicationCapabilityAttestation`; re-derived and compared in `ProbeResult.__post_init__` (`publish.py:303-312`) | **YES** | Re-derived on construction |

**The §9 gate comparisons, and where each side comes from:**

- `IR_HASH_MISMATCH` — `StagingManifestV1.ir_hash` (written by the calculation node from a rendered
  literal) vs `PlannedFeature.ir_hash` (rendered into the assembly node). Both sides originate in
  the plan; `build_compilation_identity` is what guarantees the plan's value is a real `ir_hash`.
- `PROJECT_INTEGRITY` — the run's `__generated_project_hash` vs `GENERATED.lock`, read at run time
  by walking `parents[4]` from `nodes.py` (`_LOCK_DEPTH`, DEFERRED-WORK A.16).
- `SCHEMA_HASH_MISMATCH` — the assembled frame's computed schema hash vs the literal
  `expected_schema_hash(plan)`.
- `STALE_STAGING_MANIFEST` — `(generation_id, run_id, business_dt)` on the manifest vs the run
  parameters. Judged BEFORE `ir_hash` (`group_plan.py:406-412`).

**One asymmetry worth designing around:** `CompilationIdentity.formula_content_hashes` and
`ir_hashes` are **positionally paired** and are neither sorted independently nor de-duplicated
(`identity.py:127-134`). The order is the PLAN's (sorted by column name). Anything that rebuilds a
`CompilationIdentity` must preserve that.

---

## C. `ExecutionTier`

**Definition:** `src/featuregen/overlay/upload/bridge_realization.py:81-83`

```python
class ExecutionTier(StrEnum):
    SANDBOX = "sandbox"
    PRODUCTION = "production"
```

Two members. It is **not** defined in `materialize/` at all.

**Where it is a field:** `RealizationApplicabilityScopeV1.execution_tier`
(`bridge_realization.py:207-212`) — alongside `scope_id`, `purposes`, `environment`,
`partition_scope_ref`. It enters that scope's `identity_payload()` (`bridge_realization.py:227`).

**Every read site:**

| Site | What it does |
|---|---|
| `bridge_realization.py:209` | the field itself |
| `bridge_realization.py:227` | `identity_payload()` — so the tier is identity-bearing for a realization scope |
| `bridge_store.py:266-267` | deserialization from the stored payload |
| `bridge_store.py:782` | `revalidate_bridge_realization(..., execution_tier: ExecutionTier = ExecutionTier.PRODUCTION)` — **the default** |
| `bridge_store.py:795-796` | `if revision.applicability_scope.execution_tier is not execution_tier: reasons.append("realization_execution_tier_mismatch")` — the ONLY behavioural effect |
| `bridge_store.py:900` | `executable_bridge_realizations(...)` hard-codes `execution_tier=ExecutionTier.PRODUCTION` |
| `bridge_realization_governance.py:255, 296` | carries/serializes the scope's tier |
| `overlay/upload/contract/invalidation.py:335` | carries the tier when rebuilding a scope |

**What varies by tier: exactly one thing.** A realization whose scope tier ≠ the requested tier is
marked non-executable with reason code `realization_execution_tier_mismatch`. Nothing else in the
codebase branches on it — no rendering, no publication, no namespace, no gate.

**The trap for the orchestrator.** `materialize` is structurally sandbox-only
(`binding.SANDBOX_NAMESPACE`, `identity.derive_namespace()`, `sandbox_execution_hash`,
`identity.py:28-36`), yet its two bridge entry points both demand `PRODUCTION`:

- `compile_ir` → `executable_bridge_realizations(conn, purpose="feature_generation",
  environment=inventory.environment_id)` (`ir.py:270-274`) → hard-coded `ExecutionTier.PRODUCTION`
  (`bridge_store.py:900`).
- `authorize_execution_realizations` → `revalidate_bridge_realization(conn, realization,
  purpose=..., environment=...)` (`ir.py:371-376`) → the `PRODUCTION` default.

So **a bridge realization scoped `SANDBOX` can never be used by the materialize chain**, and there
is no parameter anywhere to change that. There is also no way for the orchestrator to select a
tier. Test evidence: `tests/featuregen/materialize/test_cross_catalog_ir.py:148` and
`test_bridge_joins.py:42` both build `execution_tier=ExecutionTier.PRODUCTION`;
`tests/featuregen/overlay/upload/test_bridge_assessment_contracts.py:126` is the only `SANDBOX` use
and it is an overlay-side test.

---

## D. Failure vocabularies, and how a caller tells the three failure modes apart

`src/featuregen/materialize/codes.py` — four CLOSED `StrEnum`s, **no code appears in two of them**
(`codes.py:11-15`).

### D.1 `CompilationRefusalCode` (`codes.py:37-82`) — 25 members

`AUTHORING_RUN_INCOMPLETE, TERMINAL_PAYLOAD_TAMPERED, NOT_RESOLVED, FORMULA_HASH_MISMATCH,
AXES_MISMATCH, INTENT_HASH_MISMATCH, READ_SCOPE_INSUFFICIENT, PROHIBITED_INPUT,
COLUMN_NOT_GOVERNED, AMBIGUOUS_TABLE_NAME, JOIN_PATH_NOT_VERIFIED,
JOIN_PATH_DENIED_BY_READ_SCOPE, GRAIN_PATH_NOT_GOVERNED, JOIN_FANOUT_UNSUPPORTED,
JOIN_CARDINALITY_UNKNOWN, SPINE_SOURCE_NOT_DECLARED, SPINE_DECLARATION_REJECTED_BY_FACTS,
PARTITION_MAPPING_NOT_DECLARED, PHYSICAL_SCHEMA_NOT_RESOLVED, SOURCE_ENGINE_UNSUPPORTED,
AVAILABILITY_TIME_NOT_GOVERNED, OUTPUT_TYPE_NOT_GOVERNED, PHYSICAL_TYPE_UNSUPPORTED,
MULTIPLE_MATERIALIZATION_CONTRACTS, PARTITION_IDENTITY_UNKNOWN, UNACCOUNTED_LOGICAL_REF`

Note `SOURCE_ENGINE_UNSUPPORTED` has a code but **no reader** (`inventory.py:358-363`: the §3.4b
engine map is "declared but unread").

### D.2 `PublicationRefusalCode` (`codes.py:85-100`) — 3 members

`CAPABILITY_UNPROVEN`, `GROUP_BINDING_CONFLICT`, `PUBLISH_MECHANISM_UNSUPPORTED`.

### D.3 `ValidationGateCode` (`codes.py:103-140`) — 20 members, emitted INTO the generated pipeline

`KEY_NOT_UNIQUE, MISSING_FEATURE_COLUMN, UNEXPECTED_COLUMN, WRONG_COLUMN_TYPE, WRONG_NULLABILITY,
SCHEMA_HASH_MISMATCH, MISSING_STAGING_MANIFEST, STALE_STAGING_MANIFEST,
DUPLICATE_STAGING_MANIFEST, IR_HASH_MISMATCH, INCOMPLETE_COMPUTATION, FORBIDDEN_NUMERIC,
OVERFLOW_VIOLATION, JOIN_AMPLIFICATION, SPINE_INCOMPLETE, SPINE_DUPLICATE_KEY,
SPINE_NON_DETERMINISTIC, RUN_PARAMETERS_MISSING, PROJECT_INTEGRITY`

**`MaterializationRefused.__init__` REFUSES these** (`codes.py:175-187`): only
`CompilationRefusalCode | PublicationRefusalCode` are accepted as `code`.

**How a gate code reaches the orchestrator.** The generated project cannot import `featuregen`, so
a gate's code travels as the **leading token of a `RuntimeError` message**, and
`render/nodes_gate.gate_code_of(message: str) -> ValidationGateCode | None`
(`nodes_gate.py:95-111`) parses it back — anchored at the start, so a mention is not a verdict.
Multiple findings are joined with `FINDING_SEPARATOR = " | "` (`nodes_gate.py:86`).
The ONLY transport is `SubmissionOutcome.detail` (last 1500 chars of stderr).

### D.4 `ValidationFindingCode` (`codes.py:143-158`) — 8 members, L0/L1, non-blocking

`PROJECT_DOES_NOT_BUILD, PROJECT_HASH_MISMATCH, PIPELINE_NOT_CONSTRUCTIBLE, COLUMN_ABSENT,
COLUMN_TYPE_MISMATCH, PARTITION_ABSENT, READ_DENIED, UNKNOWN_FINDING`

Routed by `FINDING_CLASSES` (`validation.py:141-150`) into `FindingClass`
(`validation.py:107-117`): `RENDERER_DEFECT` | `GOVERNED_FACT_MISMATCH` | `ENVIRONMENT_OR_DATA` |
`UNCLASSIFIED`. `classify()` (`validation.py:156`) is TOTAL and fails closed.

### D.5 The caller's discrimination table

| Question | Signal | Where |
|---|---|---|
| Refused **before** anything was generated | a `MaterializationRefused` returned or raised by a compile-stage function; `.code` is a `CompilationRefusalCode` | `codes.py:161-190` |
| Refused **before** publication (group computed fine) | `MaterializationRefused` with a `PublicationRefusalCode` — from `bind_group` or `select_publisher`, always **RETURNED** | `binding.py:183`, `publish.py:617` |
| The artifact itself is broken | `ValidationReportV1(level=L0, status=FAILED)` with `PROJECT_*` / `PIPELINE_*` findings | `validation.py:641-647` |
| The environment can't answer | `ValidationReportV1(status=ERROR, findings=())` — **never a pass, never a finding** | `validation.py:281-285` |
| The environment contradicts the compilation | L1 `FAILED` with `COLUMN_ABSENT`/`COLUMN_TYPE_MISMATCH`/`READ_DENIED` ⇒ `GOVERNED_FACT_MISMATCH` ⇒ `may_regenerate() is False` | `validation.py:145-147, 300-318` |
| The run **never started** | `SubmissionOutcome.started is False` (`returncode is None`) | `submit.py:56-67` |
| The run started and **failed** | `started and not completed` (`returncode != 0`); parse `detail` with `gate_code_of` to see whether a §9 gate is named | `submit.py:205-210`, `nodes_gate.py:95` |
| The run **failed a gate** specifically | `gate_code_of(...) is not None` ⇒ append `RunEventKind.GATES_FAILED` (terminal); otherwise `RUN_FAILED` (terminal) | `control_plane.py:88-95` |
| The run's **current** status | `fold_run_status(events)` / `run_status(conn, run_id)` — ordered by `seq`, never by `occurred_at` | `control_plane.py:330-370` |

`RunStatus` (`control_plane.py:98-108`): `PREPARED, SUBMITTED, COMPUTED, VALIDATED, REJECTED,
PUBLISHED, REFUSED, FAILED`. Terminal set (`control_plane.py:129-134`): `GATES_FAILED`,
`PUBLISHED`, `PUBLICATION_REFUSED`, `RUN_FAILED`.

**Note the modelling gap:** `RunEventKind` has `COMPUTATION_COMPLETED` and `GATES_PASSED` as
distinct moments, but the generated pipeline runs assembly and the §9 gates **inside one Kedro
session** and reports only a process exit code. The orchestrator cannot observe
`COMPUTATION_COMPLETED` separately from `GATES_PASSED` — there is no per-node callback and no
structured result file. `GATES_PASSED` is inferable only from `completed is True`.

---

## E. Still a stub / not built

### E.1 🔴 The publish step does not exist

`render/publish.py:22-24` (module docstring) and `render/publish.py:139-140` (rendered comment):

> "**The pointer switch is not rendered here and is not a catalog entry** — it is a single metastore
> operation performed against the live cluster after the run's gates pass, and it belongs with the
> live probe that has to demonstrate it is atomic."

The rendered `published` dataset writes parquet to
`${runtime_params:staging_root}/published/<table>` (`render/publish.py:142-144`), i.e. into the
generation-scoped staging root — **not** into `sandbox_feature.<group>`. Nothing anywhere in `src/`
performs the pointer switch: `grep -rn "ALTER TABLE|SET LOCATION|EXCHANGE PARTITION"` over
`src/featuregen/` finds only the docstrings in `publish.py:108,110`.

**Consequence:** the chain terminates at "the immutable versioned output exists". The
`RunEventKind.PUBLISHED` event and a `RunManifestV1(status=PUBLISHED, publication_location=...,
published_row_count=...)` cannot be truthfully written today, because nothing published.

### E.2 🔴 `probe_publication_capability` does not exist

`publish.py:326-327` names "`probe_publication_capability(cluster, *, mechanism, engine_versions)`
— the live driver" as the thing that calls `assess_probe_observations`. No such function is defined
anywhere in the repository. Without it there is no way to obtain an attestation, so
`select_publisher` returns `CAPABILITY_UNPROVEN` on every first run, which means `render_project`
gets `publisher_selection=None` and renders the fail-closed Hive entry.

### E.3 🟡 `check_completeness` has no production caller

`group_plan.check_completeness` (`group_plan.py:390`) is referenced only in tests
(`test_group_plan.py:70,171,434,543,555`, `test_render_nodes_compute.py:72,1801,2438`). §9's
manifest gate is **re-implemented as generated Python** inside `render_assembly_node`
(`nodes_gate.py:285-377`, docstring: "§9's manifest gates, in `check_completeness`'s order and with
its codes"). Two implementations of one gate, pinned only by tests.

### E.4 🟡 No `StagingManifestV1` reader

The generated pipeline writes manifests as JSON datasets (`render/project.py:538-541`). Nothing in
`src/` reads them back. An orchestrator that wants to record staged row counts into `RunManifestV1`
must write that reader.

### E.5 🟡 The join-gate node is never wired into a project

`render_join_precondition_node` (`nodes_join_gate.py:86`) is exercised only in
`tests/featuregen/materialize/test_render_join_gate.py`. No test — and no code — passes such a node
to `render_project`, so **no complete cross-catalog project has ever been assembled**, even though
`project_datasets` declares `join_gates` entries (`project.py:354-359`) and `_check_wiring` rule 7
requires them to be consumed (`project.py:636-641`).

### E.6 🔴 A cross-catalog project cannot be submitted

`render_join_precondition_node` adds `params:bridge_predicate_values` to its inputs
(`nodes_join_gate.py:113-115`). `render_project` folds node parameters into `required_parameters`
(`project.py:1225-1234`), and `prepare_run` accepts `additional_parameters` + a widened
`required_parameters` (`runprep.py:843-844`). But `submit.check_run_parameters`
(`submit.py:115-116`) compares against the module-level `REQUIRED_RUN_PARAMETERS` **only**:

```python
missing = sorted(set(REQUIRED_RUN_PARAMETERS) - set(run_parameters))
unexpected = sorted(set(run_parameters) - set(REQUIRED_RUN_PARAMETERS))
```

So a prepared 7-parameter cross-catalog run is refused by `LocalClusterSubmitter.submit` with
`ValueError: … unexpected ['bridge_predicate_values']` before a process starts. `submit` has no
parameter to widen the expected set.

### E.7 🟡 The L0 gate proves the SHELL, not the compute

`tests/featuregen/materialize/l0_gate.py:37-40` imports the `project` fixture from
`test_render_project.py`, whose `_nodes` are **stubs** (`test_render_project.py:131-172`: "A node
whose BODY is a placeholder and whose WIRING is real"). The real compute nodes go into
`render_project` only in `test_render_nodes_compute.py` / `test_render_gate.py`, which never run
under kedro+pyspark. **The rendered compute has never been imported in a real engine environment.**

### E.8 Other explicit "not on a production path" statements

- `ir.py:531, 576, 621` — "L1 sits on no production path" (which is why Gate 2 gained
  `COLUMN_NOT_GOVERNED`).
- `classify.py:218` — the same, for `COLUMN_ABSENT`.
- `binding.py:53` — "There is no production path in this slice, and Child-2 must later supply a
  factory that validates actual frozen bindings."
- `identity.py:31, 34, 470` — no production namespace, no production execution hash.
- DEFERRED-WORK A.31 (`docs/DEFERRED-WORK.md:559+`) — `input_snapshots` is carried and hashed but
  **never enforced as the run's read scope**; no rendered node consumes the list.
- DEFERRED-WORK A.29 — `prepare_run` refuses DATE-typed clocks outside UTC.
- DEFERRED-WORK A.30 — a real SCD-2 spine with distinct `effective_time_ref`/`availability_ref`
  **cannot validate under any catalog state**.
- DEFERRED-WORK A.32 — `requirements.lock` installs an environment that cannot construct the
  rendered catalog (kedro-datasets hard-imports `hdfs`/`s3fs`).
- `inventory.py:358-363` — the §3.4b engine map is parsed but read by nothing.
- `docs/DEFERRED-WORK.md:156` — "§1.3 and §2 disagree on where the spine lives … Resolve when the
  orchestrator (T15/T17) wires the real call."

---

## F. Everything the orchestrator must own that no module provides

1. **An id factory.** `generation_id`, `run_id`, `binding_id`, `report_id`, `probe_id`. Neither
   `control_plane` nor `binding` nor `validation` nor `publish` mints one.
2. **A clock.** Every ISO-8601 timestamp must be **offset-aware** (`binding.py:127-131`,
   `control_plane.py:160-163`). `run_l0`/`run_l1` take a `clock: Callable[[], str]` read twice.
3. **`seq` allocation** for run events, with `UniqueViolation` / trigger-`RaiseException` handling
   (`control_plane.py:392-405`).
4. **Three adapters, none of which is implemented in `src/featuregen/materialize/`:**
   - `MetastorePartitions` (`runprep.py:153`) — `list_partitions`
   - `MetastoreMetadata` (`validation.py:653`) — adds `describe_table`, `can_read`
   - `MetastoreTableMetadata` (`inventory.py:652`) — for `MetastoreInventoryAdapter.capture`
     (`inventory.py:708`), which IS implemented
5. **Declarations with no derivation:** the `SpineSourceDeclarationV1`, the `CadenceDecl`, the
   `AvailabilityPromiseV1`, the `logical_group_name`, the `staging_base`, the `roles` list, the
   `python_executable`, and the `env` dict with both `PYSPARK_*` vars.
6. **The `nodes` sequence** (§1.12) — the biggest single piece of missing code.
7. **The `published_schema` fetch** for `select_publisher`.
8. **A crash-resume strategy** for the unpersisted artifacts in §A.2.
9. **The publish operation itself** (§E.1).

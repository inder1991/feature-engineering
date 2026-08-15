# V2 as the feature language, on shared execution

**Date:** 2026-08-16 · **Revision 13** — a seventh review found **11 functional blockers and 7 major
gaps**, all validated. Two are corrections to invariants revision 12 introduced: **rule 7's
byte-freeze is unimplementable as described**, and the deterministic-authoring status it specified
**raises on the existing vocabulary** — the reviewer ran it. **S0.5 is expanded into four concrete
contract-freeze blocks. Do not begin S1 until they are frozen.**

> **V1 remains the stable compatibility language. V2 becomes the intelligent, policy-aware language
> for all new features. LLMs create and enrich V2 formulas; a deterministic V2 compiler resolves
> their banking meaning and runs them through the existing Spark/Kedro materialization platform.**

**Do not reduce V2 to V1. Do not build a second V2 platform.** The V2 execution boundary below is an
**adapter** onto language-neutral machinery — rendered nodes, wiring validation, sealing, staging,
output gates, publication — not a second platform.

## 0. Position

| | V1 | V2 |
|---|---|---|
| Role | frozen compatibility language | the language for all new features |
| Gets | correctness fixes only | policy awareness, multi-expression, advanced aggregation, LLM authorship |
| Users choose? | no — the platform decides internally | no — the UI never asks for a version |

**"V2" is the language; `formula_schema_version` is a wire pin.** The V2 language is served by wire
schema **2 today and 3 after S0.5**; bumping the pin does not create a "V3 language".

**Deferred, and not silently:** cross-catalog V2 execution · multi-environment support ·
Iceberg-grade snapshots · operations beyond the pilot family. **No user-facing feature is removed.**

## 0.1 Verified before planning

Re-checked against this tree. **Baseline `29f3b8ac`; targeted suite 2,827 passed, 1 skipped.**

**Revision 12's byte-freeze cannot work as written [R13]**

- `canonical_v2._plain_v2` serializes **every dataclass field automatically**:
  `{f.name: _plain_v2(getattr(value, f.name)) for f in sorted(fields(value), …)}`.
- So adding an optional `semantic_row_selection` to `AggregateExpressionV2` **adds it to existing
  schema-2 canonical JSON even when `None`**, and moves every old hash. There is one V2 validator,
  one V2 dataclass, one canonicalizer.

**Revision 12's deterministic-authoring status raises [R13]**

- `result.py:52`: `CriticStatus = Literal["clean", "advisory", "blocking"]`, and `:124` feeds
  `frozenset(get_args(CriticStatus))` into a coherence check. `not_applicable_reviewed_blueprint`
  raises `IncoherentResultError`. **The reviewer executed this.**
- `replay_trace.py:367`: `OUTPUT_POLICY_RESOLVED` requires `CRITIC_COMPLETED`.
- Revision 12 also contradicted itself — "the critic records this status" **and** "no fabricated
  critic event".

**Direction has two competing owners [R13]**

- `AuthorityRefsV2` already carries `status_policy_ref · direction_policy_ref · reversal_policy_ref ·
  currency_conversion_ref`, all identity-bearing. Revision 12's `SemanticRowSelectionV1.policy_ref`
  would be a **second** owner, permitting a formula that names two different direction policies with
  no rule on which executes.

**The V2 execution types do not fit the V1 contracts [R13]**

- `FeatureGroupPlanV1.physical_type_policy_version: int`, in its identity payload; same in
  `MaterializationContractV1`. **`formula-v2/physical-types@1` is a string.**
- `authorize_compilation(conn, irs: Sequence[FormulaExecutionIRV1], spine, …) →
  AuthorizedCompilation` — *"the TOKEN Gate 2 issues, and the only way into §2's downstream chain."*
- `render_project` and `build_compilation_identity` runtime-check V1 types.
- As written, an implementer must put a string in an int field, disguise V2 IR as V1, or change V1.

**Input snapshots are evidence, not enforcement [R13]**

- `runprep.py` states it outright: *"prepared **EVIDENCE, not an enforced read scope**"* — resolved
  at preparation, consumed by L1's `PARTITION_ABSENT` check; **the rendered nodes do not read it.**
  Hashing a file list does not prove Spark read that list.

**Publication has no staleness guard [R13]**

- `next_revision_seq` is read-then-write, and migration 1055's trigger refuses a value that does not
  strictly extend the group — **that solves concurrency, not staleness.** An old verification
  published later takes a new `seq` and becomes current.

**Everything else confirmed**

- `required_policy_kinds()` takes broad booleans and has zero production callers. It cannot name an
  expression path, physical source or semantic role.
- `prepare_run` requires `capability_attestation_id`; `sandbox_execution_hash` requires it
  non-defaulted; the 1034 terminal manifest requires publication mechanism and attestation.
- `materialization_group_binding` is `logical_group_name text NOT NULL UNIQUE` — **no environment
  key.** Hive identifiers are **≤ 128 chars**, and `admission.py:494` says exceeding it *"is a plan
  error, not a name to invent a mangling for."*
- `materialization_compiled_artifact` stores `group_plan` and `materialization_contract` as jsonb
  plus hashes — **no generated source files.**
- `_gold_evaluation_recorded()` returns `False` for every recipe.
- `engine_capability` derives from renderer dispatch, not execution evidence.
- `render/project.py` refuses when `datasets.published not in written`.
- `@router.post("/materialization-runs", dependencies=[Depends(require_confirmer)])` requires the raw
  `platform-admin` claim; the fold inherits `_contract_blockers`.
- `govern.py:610` selects `WHERE feature_name = %s ORDER BY version DESC LIMIT 1`.
- `identity_payload()` excludes `non_physical_refs` / `operand_type`; `filter_tree` is an opaque
  `Mapping[str, Any]`; `authority_refs` are *"identity-bearing exactly as authored"*.
- `PHYSICAL_TYPE_POLICY_VERSION = 4` hardcoded in `contract.py:648` and `group_plan.py:346`;
  `physical_types.py` carries substantial exact-numeric handling.
- `1023`'s work item has recipe-specific NOT NULL columns, a FK, and four triggers; it is the only
  durable intent anchor.
- `PitSpec` excludes `empty_window` / `null_input`; **`GATES_PASSED` is not terminal**; eight
  external validation requirements; `api.ts:4190` has only a GET; the POST takes shadow work-item
  ids capped at 12; `business_dt` appears nowhere in the route; cross-catalog refuses at
  `chain.py:906`; migration prefixes collide seven times.
- The pilot's gold is three disagreeing artifacts; the ledger's `direction` is `D`/`C`; the fixture
  lacks availability timestamps.
- `WorkbenchScreen.tsx` still renders the result list far below repeated intake panels.

**Two standing instructions**

1. **Grep for the thing before designing it.** Seven prior instances.
2. **Every acceptance criterion must be satisfiable with only what exists at its own stage.**

## 0.2 Invariants

1. No V2 formula executes with an unresolved policy reference.
2. **A non-empty policy string is not proof that the policy exists.**
3. **Coverage is derived from typed operator kinds, parameters and graph topology — never from
   self-attestation [R13].** `realizes_occurrences` is **attribution**, not proof: a renderer could
   delete the FX duplicate-rate gate and leave the tag on the multiplication operator. Each policy
   kind has a **frozen operator-subgraph requirement** (S0.5-C) and the validator checks the graph
   against it.
4. No policy may disappear when V2 reuses existing operators.
5. **Two identities, kept apart.** *Formula identity* (the proposal's canonical bytes) covers what
   was **authored** — authority refs and semantic selections. **`ir_hash`** covers **resolved
   executable semantics** and excludes provenance and revision numbers.

   | Identity-bearing in `ir_hash` — **changes rows** | Recorded, **not hashed** — explains |
   |---|---|
   | status column, eligible values, null behaviour | producer, confidence, evidence, observed values |
   | direction column, debit values, normalization | model and prompt version |
   | reversal mode and its columns/linkage | approval state, who confirmed, when |
   | rate table, keys, effective timestamp, booking-vs-settlement, direction, rounding | **the realization revision id itself** |
   | allocation method and its columns | lineage narrative |
6. **V1 formula hashes, V1 IR canonical bytes, V1 function signatures and V1 contract types remain
   unchanged.**
7. **V2 wire schemas freeze once shipped — which requires per-version projections, not an optional
   field [R13].** Schema 2 keeps **its own parser, model and canonical projection producing the exact
   old bytes**; schema 3 gets its own; **parser, semantic validator and canonicalizer dispatch
   together**. Schema-2 fixtures are tested **byte-for-byte**, not merely "still parseable". A gold
   fixture is re-pinned deliberately, in a commit that says so.
8. V1 and V2 share **language-neutral** publication and validation machinery, reached through a V2
   adapter — never through V1's typed contracts.
9. The LLM proposes and explains; deterministic code executes.
10. The UI never asks a user to choose a formula version.
11. **"Supported" means generated code has been executed successfully**, recorded against a
    **capability signature**.
12. **Required = declared = resolved = covered, occurrence-addressed and MANY-TO-MANY.** One FX
    occurrence needs several operators; `Scan` / `Aggregate` / `FinalCombination` may realize none;
    two compatible policies may fuse. An occurrence is `expression_path + physical source binding +
    policy kind + semantic role`. **Two occurrences may pin the same immutable realization
    revision.**
13. **The formula says WHAT; the policy says HOW.** The formula declares `debit`; the realization
    says "debit means `D` here". Neither infers it from a recipe name.
14. **One policy reference has one owner [R13].** `AuthorityRefsV2` owns policy references.
    `SemanticRowSelectionV1` carries **`kind · role · semantic_value` and no `policy_ref`.**
15. **Every physical read carries its own temporal semantics [R13].** A monetary transaction feature
    has several clocks — transaction event and knowledge time, reversal event and availability time,
    FX effective date and FX knowledge time — with different columns and rules. One
    expression-level spec governing all reads reproduces V1's error for policy inputs.
16. **LLM-proposed realizations are usable, visibly [R13].** They may be used for sandbox generation
    after deterministic structural validation, stay marked `LLM_PROPOSED`, and **human review changes
    trust provenance and ranking, not executability**. Source-declared or governed evidence outranks
    conflicting LLM evidence; **conflicts refuse rather than silently picking one**. A successful
    Spark run proves computational execution, **not semantic correctness**.

**Rule 11's bootstrap:** an operator's first execution happens on **S7's development gold path**.

## 0.3 Division of labour

**LLM:** understand the hypothesis · select recipes · propose candidates · map roles to concepts ·
recommend physical columns · interpret source-specific status descriptions · propose debit/credit
conventions and reversal representations · identify currency and rate columns · produce structured V2
formulas · critique · explain refusals · summarise results.

**Deterministic code:** validate structure · bind columns · derive occurrences · resolve
realizations · check coverage · compile · generate Spark · execute · validate · publish. **The LLM
never injects free-form SQL.**

**Three gates, three requirements [R13]** — none of which is human semantic confirmation:

| Action | Requires |
|---|---|
| `generate_code` | authorable **+** renderable |
| `verify_in_sandbox` | a sealed artifact **+** execution permission |
| `publish_sandbox` | a current passing verification **+** publication capability |

S2 owns the routes, the permissions (`feature:generate`, sandbox-verify, sandbox-publish) and
**removing generation and verification from `_contract_blockers`' inherited human-confirmation
requirements**. `require_confirmer` is why AI-proposed features are blocked today.

**Changing `AUTHORITY_MATRIX` is a migration**: it moves `authority_matrix_hash` and every frozen
option pinned to the old hash regenerates.

## 0.4 Three words that must not blur

**Execution proof** (S7) — development-time, mutation-tested, on the existing Spark gates. **Sandbox
verification** (S8) — user-triggered, against the exact sealed artifact. **Publication** (S9) —
atomic, group-wide, promoting *exactly the verified bytes* under compare-and-set.

## 0.5 Migration ledger

**S0 builds a ledger with a CI uniqueness test** (prefixes collide seven times). Allocation happens
per stage: S1 authoring work item · selected feature revision · `BuildDeclarationV1` — S2 build-set /
child-group records · staged group output · lifecycle triples — S3 policy realization revisions — S6
content-addressed artifact files — S7 operator execution proofs — S8 verification records · verified
output revisions · profiles — S9 publication records.

**S2's first task is the explicit existing-vs-new map** against `materialization_request`,
`materialization_generation`, the compiled-artifact and run-event tables.

## 1. The sequence

### S0 — decide the pilot's semantics *(hard STOP: humans decide)*

Decide: policy-reference namespace · window timezone, boundary and length · **reversal-as-of
semantics** · **direction mapping (`D`/`C`)** · population spine · **FX join and cardinality** ·
publication rule.

**Author the expected rows** for one coherent parameterized exemplar, extended to: zero-eligible
spine account · unknown-transaction account · post-cutoff reversal · duplicate and missing FX rates ·
post-cutoff FX knowledge time — **with real availability timestamps**.

**Decide the FX branch and make it buildable:** prove and pin a same-catalog rate source, or write
the explicit tasks for a **reference-data join authorization** — not an entity bridge.

Also: the migration ledger.

> **Acceptance:** every contradiction has a decision record naming who decided; expected rows stored
> and hashed with availability timestamps; the FX branch chosen and its tasks written; the ledger's
> CI test fails on the seven collisions unless grandfathered.

### S0.5 — freeze the load-bearing contracts, in four blocks *(expanded [R13])*

**Do not begin S1 until these are frozen.** Seven revisions have each designed a stage against an
interface that did not fit — including two of this stage's own previous contents.

#### A · Authoring contracts

- **Schema-2 compatibility implementation** — its own parser, model and canonical projection emitting
  the **exact old bytes** (rule 7). `_plain_v2` serializes every dataclass field, so an optional
  field alone would move every schema-2 hash.
- **Schema-3 proposal and expectation types**, carrying `SemanticRowSelectionV1(kind, role,
  semantic_value)` on **`AggregateExpressionV2`** — the artifact the compiler reads — and on
  `ExpressionRoleExpectationV2` / `BoundExpressionExpectationV2` so expectation preservation can be
  checked. It is **not a filter**; `UNAUTHORED_FILTER` is unchanged.
- **One direction-policy owner** (rule 14): the selection has **no `policy_ref`**;
  `AuthorityRefsV2.direction_policy_ref` is sole owner; a **schema-3 coherence rule requires a
  direction policy whenever a direction selection exists**.
- **A deterministic-review replay event and status [R13]** — a **V2-only**
  `review_execution_status = not_run_reviewed_blueprint` and a `REVIEW_BYPASSED` trace transition.
  **Do not extend the shared V1 `CriticStatus`** (it is `clean | advisory | blocking` and raises on
  anything else) and **do not record a fake successful critic run**.
- **`SelectedFeatureRevision` [R13]** — created when the user selects a candidate, owning
  `executable_revision_id` and `output_column_name`. `GovernedFeatureContract` becomes an **optional
  later record pointing at it**. Revision 12 allocated identity "at contract creation", which is the
  governed confirmation path — and generation must not require confirmation. **Executable work is
  never identified by "latest feature with this name".**

#### B · Execution contracts

- **`AuthorizedCompilationV2` · `MaterializationContractV2` · `FeatureGroupPlanV2` ·
  `CompilationIdentityV2`** — an explicit V2 boundary. V1's `physical_type_policy_version` is an
  `int` and its Gate-2 token carries `FormulaExecutionIRV1`; a string policy id and a V2 IR cannot
  ride them without breaking rule 6.
- **A V2 renderer entry point** reusing language-neutral machinery — rendered nodes, wiring
  validation, sealing, staging, output gates, publication. **An adapter, not a platform.**
- **The complete Gate-2 read set** — formula **+ policy + spine**. Policy resolution adds reads the
  authored formula never named: reversal-link columns, status and direction columns, FX table, keys,
  rate and availability columns.
- **Per-read temporal semantics** (rule 15) — the formula window stays on the expression; every
  physical read/operator edge carries its own spec; input snapshots derive independently for
  transaction, reversal and FX reads; contract availability is their **union**.
- **The pilot physical-type policy, actually defined [R13]** — `formula-v2/physical-types@1` named a
  policy revision 12 never wrote. Freeze a small truth table that defines
  **`SUM(amount × booking_rate)` completely**: amount precision and scale · intermediate precision
  for the multiplication · where rounding occurs · SUM precision growth · overflow behaviour ·
  whether floating-point operands refuse · nullability after the empty-window policy. Do not design
  every future aggregate yet.
- **The exact refusal vocabulary** for every new gate.

#### C · Policy contracts

- **`derive_policy_occurrences(formula, bound_inputs)` [R13]**, replacing the wiring of
  `required_policy_kinds()` — which takes broad booleans, cannot name an expression path, physical
  source or semantic role, and asserts rules that are not generally true (*"every filtered formula
  needs status and reversal"*: a country filter does not; *"every monetary formula needs direction"*:
  an end-of-day balance does not). Occurrences derive from the authored semantic selection · the
  explicit authority reference · the bound physical source · the expression path · the grain
  transition · an actual need for currency conversion. Keep the old function only as a compatibility
  helper.
- **Realization revision selection** — which revision is current, and how a conflict refuses.
- **LLM proposal admissibility** (rule 16).
- **Typed operator-subgraph requirements per policy kind (rule 3):**
  ```
  FX:               as-of/temporal join · duplicate-rate gate · missing-rate gate
                    · optional inversion · decimal conversion · connected result path
  LINKED_REVERSAL:  as-of row population · original/reversal linkage · ambiguity gate
                    · survivor/neutralization operator · connected result path
  ```
- **Topology-derived coverage validation** over those requirements.

#### D · Verification and publication contracts

- **`VerificationExecutionIdentityV1`, defined [R13]:** generated project hash · compilation and
  group identity · environment and inventory · `business_dt` · exact run parameters · input
  observations · `verification_check_set_hash`. **It contains no publication attestation.** Decide
  whether staging carries a new `__verification_execution_hash` or an explicitly versioned
  compatibility alias — `prepare_run` requires `capability_attestation_id`, generated outputs carry
  `__sandbox_execution_hash`, and the 1034 terminal manifest requires publication mechanism and
  attestation, so **identity, generated system columns, run parameters and persistence change
  together or not at all**.
- **Staging system columns and manifests.**
- **The versioned check set [R13]** — "keys, grain, types, nulls, inflation" is not implementable.
  Freeze: the exact result schema per check · which columns must be non-null · feature-null rules
  derived from the formula's output policy · spine completeness and uniqueness · join
  orphan/amplification results · **which external requirement each check may satisfy** · the
  check-set hash.
- **`VerifiedOutputRevisionV1`**, including `verification_check_set_hash` and validator versions —
  otherwise an old pass stays current after verification rules change.
- **Environment scoping [R13]** — `materialization_group_binding` is `UNIQUE(logical_group_name)`
  with no environment. **Either scope this release explicitly to one sandbox environment, or key
  bindings and publications by `(environment_id, logical_group_name)`.**
- **The group-name allocator, completely** — `<requested-base>__<contract-class>__<short-hash>` is
  not automatically collision-free and Hive names are **≤ 128 chars**. Define truncation, reserved
  suffix length and collision extension.
- **Active-revision compare-and-set** (see S9).

> **Acceptance:** every contract has a frozen schema with a pinned hash and a test that fails if a
> field is added without updating it; **schema-2 fixtures serialize byte-for-byte identically**;
> `not_run_reviewed_blueprint` round-trips through the replay trace without a critic event; V1
> hashes, canonical bytes, signatures and contract types untouched; the `posted_debit_amount`
> blueprint derives a **direction-selecting** expectation and **blueprint, fixture and expected rows
> agree**; `EXPECTED_OUTCOMES` is re-measured and re-pinned with the delta explained.

### S1 — deterministic V2 authoring

Exact selected-feature identity, with its **mapping stated** to `suggestion_id_v3`,
`suggestion_revision_id`, `(considered_revision_id, option_id)`, the candidate key and the authoring
run — anchored on **`SelectedFeatureRevision`**, not contract creation. `govern.py:610`'s
latest-by-name query gets a **named remediation**.

**The output-naming algorithm**: parameter projection · maximum length · collision suffix ·
**behaviour when a display label changes** (the physical name must not move).

**`BuildDeclarationV1`, sealed** — population spine · cadence · availability promise · target
environment and inventory · requested build-set name · parameters and semantic parameter bindings.

**Generalize the work item — migration + backfill + compatibility reader** for
`formula_authoring_work_item`. Generation must not depend on shadow **ranking**, the **top-12 cap**,
or **opportunistic capture**.

**The deterministic authoring producer** binds the reviewed blueprint, constructs the proposal
deterministically, runs the **structural, expectation-preservation and output-authority** gates,
writes the standard V2 replay trace with **`REVIEW_BYPASSED` / `not_run_reviewed_blueprint`**, and
yields an ordinary `AuthoringResultV2`. **No provider call, and no fabricated critic event.**

**V2 resolution and admission:** version-triple-dispatched trace restoration · V2 terminal hash
verification · `ResolvedFeatureInputV2` · `AdmittedFeatureV2` · proposal/output-pair coherence ·
shared batch and name-collision handling.

**S1 ships deterministic formula authoring — not "feature generation".**

> **Acceptance:** four variants of one display name resolve to four features with four stable column
> names, and a label change moves none; a candidate outside the shadow top 12 authors and admits; a
> reviewed recipe produces a durable replayable `AuthoringResultV2` with **no provider call**; an
> executable revision exists **without a governed contract**; legacy work items read through the
> compatibility reader; V1 formula hashes pinned by frozen-bytes test.

### S2 — lifecycle split, on V1, with staging-only execution

**First: the existing-vs-new table map.** Then `generate_code()` / `verify_in_sandbox()` /
`publish_sandbox()`, with the neutral handoff:

```
generated project → StagedGroupOutputV1 → verification → VerifiedOutputRevisionV1 → CAS pointer switch
```

**The renderer's invariant is REPLACED, not deleted:** a project must write **either a published
target or a staging target**. `StagedGroupOutputV1` requires no publication capability and no
published table.

**The lifecycle is a hierarchy:**

```
SelectedBuildSet
  ├── DerivedGroup A → GeneratedArtifact A → verification → publication
  ├── DerivedGroup B → …
  └── DerivedGroup C → generation refusal
```

**Routes, permissions and `_contract_blockers`** per §0.3.

> **Acceptance (on V1, which runs today):** a project renders and seals writing a staging target and
> **no** published target, while a project writing **neither** still refuses; verification executes
> to staging with **no publication capability present**; a non-admin with `feature:generate`
> generates and cannot publish; every triple reaches a terminal state. **V2 boundaries assert at S6
> and S8.**

### S3 — occurrence-addressed policy realization, with a producer

**`PolicyRealizationRevisionV1`** — immutable, keyed by `abstract policy ref + physical dataset
binding + environment/source + effective semantics`, fields split by rule 5.

**Implement only the pilot's modes; define the rest and refuse them by name:** eligible statuses ·
indicator-based direction selection · the decided reversal mode · booking-date FX with the full typed
field set (rate table binding · currency keys · effective/booking key · **knowledge time** · quote
direction and inversion · rate column and decimal policy · missing-rate behaviour · duplicate-rate
refusal · **expected join cardinality**).

**The producer workflow** — LLM structured-output contract · evidence collection · deterministic
validation · conflict detection · current-revision selection · realization writes · UI disclosure,
under rule 16.

**Resolve the dual authority** with `eligibility_store.py`: migrate it or adapter it.

**Wire `derive_policy_occurrences()`** (S0.5-C).

**Policy inputs enter the materialization contract** — a restricted FX table must not ride into a
public group behind a public transaction input.

> **Acceptance:** an unresolvable reference refuses by name; a formula omitting a required occurrence
> refuses; an unsupported reversal mode refuses and names it; a realization whose as-of rule would
> read post-cutoff FX refuses; a country filter does **not** require a reversal policy; one status
> policy across two expressions yields two occurrence bindings that may pin one revision; two
> realizations differing only in provenance produce identical resolved executable fields;
> **conflicting LLM and source-declared evidence refuses rather than choosing**.

### S4 — the bound V2 formula

**`BoundFormulaRevisionV2`** — authored proposal (including the semantic selection), output policy,
physical bindings, realization pins. **No complete-read-set claim.**

> **Acceptance:** a compiler version bump leaves the bound-formula hash unchanged; the artifact
> cannot be constructed with an unpinned realization or an unresolved semantic selection.

### S5 — V2 IR, Gate 2, contracts and the V2 partitioner

**`FormulaExecutionIRV2`** with `realizes_occurrences` on operators and identity payloads holding
resolved executable semantics only.

**Gate 2 over the union [R13]:** finish V2 planning → compute the complete **formula + policy +
spine** read set → run Gate 2 over that union → mint **`AuthorizedCompilationV2`** → **require the
token in V2 contract derivation and rendering**.

**`MaterializationContractV2` / `FeatureGroupPlanV2`** carrying `formula-v2/physical-types@1`.

**`partition_contracts_v2()`** — derive one contract per feature, partition into one or more group
plans, each generating, verifying and publishing independently. **`group_by_contract()` keeps its
single-result refusal contract.**

**The operator order is derived from S0's reversal mode**, not frozen — a reversal row may carry a
reversal-specific status, the opposite direction, or fall outside the original's window while known
by the cutoff, so filtering first can delete the record needed to neutralize the original:

```
availability / PIT cutoff → construct reversal relationships from the required as-of row population
  → derive surviving economic events → apply eligible-status and direction semantics
  → event window → FX
```

> **Acceptance:** V1 IR canonical bytes and the single-contract path byte-identical; **removing the
> FX table from authorization while retaining it in the operator graph refuses compilation**; a set
> spanning two contracts yields two groups with deterministic collision-free names ≤ 128 chars; a
> re-approval that changes no executable field leaves `ir_hash` unchanged; a linked-reversal mode
> produces an order in which the reversal row survives to neutralize its original.

### S6 — generate, persist and serve readable code

Nodes derived from the S5 subgraph. **Execute nothing. Publish nothing.**

**A content-addressed file manifest and retrieval contract [R13]** — `materialization_compiled_artifact`
stores the plan and contract as jsonb and **no generated source files**.

**Rule 3's proof** is topology-derived coverage against S0.5-C's frozen operator-subgraph
requirements. `realizes_occurrences` is attribution.

> **Acceptance:** emitting a filter then aggregating the unfiltered input refuses; **an FX occurrence
> whose duplicate-rate gate is deleted refuses even with `realizes_occurrences` intact on the
> multiplication operator**; generated files are retrievable by content address; S2's boundaries
> re-asserted on V2.

### S7 — Spark gold proof, and proof-aware readiness

**Extend `spark_semantics_gate.py` and `l0_gate.py`** — do not build a harness — targeting
`deploy/kind/sandbox/Dockerfile.spark` via `LocalClusterSubmitter`.

The generated code reproduces the frozen exemplar including S0's added cases; **all six mutations
fail**.

**Readiness moves in-sequence [R13]** — revision 12 left it under "Still required" while
`_gold_evaluation_recorded()` returns `False` for every recipe and capability comes from renderer
dispatch. S7 delivers: the **qualified, current evaluation-artifact reader** · a **proof-aware
capability resolver** · the **action-specific blocker matrix** of §0.3.

**`OperatorExecutionProofV1` mints a capability signature** — input/output types · window form ·
policy operator families · null and empty behaviour · rounding.

**Close here:** the DATE-clock refusal outside UTC, and A.32 🔴 `requirements.lock`.

> **Acceptance:** every case and mutation behaves; each proof names S0's frozen hash; a signature
> mismatch does not satisfy a proof; **capability is computed from proofs, not dispatch**; a stale
> evaluation artifact does not read as a pass.

### S8 — execution infrastructure and on-demand verification

**Infrastructure:** `business_dt` on the API · server-side Hive schema read · captured inventory ·
the product's remote submission seam · a dedicated materialization worker.

**Checks run the S0.5-D versioned set as a DAG:** `build + static → execute → ⟨output_sanity⟩ →
fold`. Advisory EDA/profiling is a **separate non-blocking attempt**.

**Input observation, labelled honestly [R13]:** `runprep` states the resolved partition list is
*evidence, not an enforced read scope*, so a file-list hash does not prove Spark read it. **Either
the generated project reads the exact manifest — with path, size and version/checksum — or the
strength is `OBSERVED`, never `PINNED`.** `UNPINNED` remains valid and disclosed.

**Verification maps to requirements explicitly**, many-to-many over the registry's eight, emitting
`EXTERNAL_PASSED` only where the check's result schema matches. A group pass must not turn every
member `DATA-CHECKED`. Never write `USEFULNESS-CHECKED` here.

**Staleness is computed on read**; never re-run automatically.

> **Acceptance:** a verification survives restart with per-check results intact; changing any
> recorded input flips a pass to stale and names it; a keys/types check does not satisfy
> `JOIN_CONNECTIVITY`; a profiling failure leaves verification passed; observation strength is
> labelled `OBSERVED` or `UNPINNED` and never `PINNED` without enforced reads.

### S9 — atomic sandbox publication, under compare-and-set

Publish **only the exact verified output** — compare the staging manifest before promotion; **no
re-execution**. Recheck publication capability and policy dependencies.

**Compare-and-set, because sequence alone does not prevent staleness [R13].** Migration 1055's
trigger stops two publishers both winning; it does not stop an **old** verification published later
from taking a new `seq` and becoming current. `publish_sandbox()` carries
**`verified_output_revision_id`** and **`expected_active_revision_id`** (or `expected_active_seq`),
and the pointer switch is CAS. **An intentional rollback is a different action.**

**Decided:** generated code may remain unverified indefinitely; publication requires a current
passing verification. **`UNPINNED` inputs may publish, labelled, and never called reproducible or
source-current.**

> **Acceptance:** a changed or missing staging manifest blocks promotion; **publishing an older
> verified output over a newer active revision refuses under CAS**; a verification whose check-set
> hash predates the current validator reads as stale; capability revoked between verify and publish
> blocks; a partial group never becomes visible.

### S10 — complete API and UI workflow

**Backend endpoints, enumerated [R13]** — the frontend has only a materialization GET: build-set
creation and status · child-group status and refusals · **artifact file listing and file content** ·
verify request and results · publish request and results.

**Generate → Verify → Publish** over the child-group hierarchy, showing selection · **the derived
group split before verification** · generated code · **policy provenance** with `LLM_PROPOSED`
visible · verification state including observation strength · publish eligibility · that one failed
member blocks its group.

**The workspace ordering requirement, carried in at last [R13].** After hypothesis submission the
user's **goal, approved target label, current stage and workflow output belong at the top of the
workspace** — `WorkbenchScreen.tsx` still renders the result list far below repeated intake panels.
This was identified in the post-submit UX review and never assigned.

The UI for creating `BuildDeclarationV1` lands here; the contract shipped at S1.

> **Acceptance:** no path reaches execution without an explicit click; group scope visible before
> publish; a candidate outside the shadow top 12 is generable from the UI; results and stage sit
> above intake after submission; a stale result renders as stale, not failure.

### S11 — from one feature to the corpus, **generation only**

Batch **derive → compile → generate** across the derivable blueprints — **the count is S0.5's
re-measurement, not the pre-S0.5 "90"** — with a **declared default `BuildDeclarationV1` set**,
reporting blueprints that cannot be declared rather than inventing declarations. Land the as-of
snapshot window shape and measure its effect. Drive `WINDOW_NOT_EVENT_ANCHORED` down as registry
work.

**Verification and publication are NOT batched.** Their counts reflect prior explicit user actions.

> **Acceptance:** a coverage table (`derivable → generated`, then observed `verified → published`)
> with every refusal carrying a named code; **the batch triggers no execution**; undeclared
> blueprints reported, not defaulted; no blueprint made to pass by a special case.

### S12 — live-provider gate, then incremental expansion

**The gate is representative of what is advertised when it runs** — for the pilot, one family. It
tests parsing · expectation preservation · policy resolution · **refusal behaviour** · trace
persistence. **Operator dependency:** Anthropic billing must be topped up.

Then one family at a time, each needing renderer support **and** an execution proof: ratios →
offsets → composites → percentiles → slopes → allocation → future horizons. **Each family entering
the advertised set also enters the live gate.**

> **Acceptance:** the gate exercises every currently-advertised family and asserts refusals as well
> as successes; the advertised set is `renderer-supported ∩ execution-proved`.

## 2. Carried forward

**Parallel, gated at merge:** behavioural frontend↔backend contract tests · hide the retired "Write
definitions" control · recognition correctness in full · ruff ratchet (79 repo-wide, 35 in `src/`).

**Narrow the leakage claim** — carried since revision 2 without its context; recover the original
finding before acting.

**Still required:** canonical typed planning-request JSON persisted, since `repr()` cannot
reconstruct a request. *(The evaluation-artifact reader moved into S7 [R13].)*

**Deferred, explicitly:** cross-catalog V2 execution (`chain.py:906`) unless S0 decides otherwise ·
multi-environment support unless S0.5-D chooses it · Iceberg-grade content snapshots · operations
beyond the pilot family.

## 3. Sequencing

```
S0 business semantics ─► S0.5 frozen interfaces ⟨A authoring · B execution · C policy · D verify/publish⟩
  ─► S1 deterministic V2 authoring ─► S2 V1 staging / on-demand lifecycle split
  ─► S3 policy realization producer ─► S4 bound V2 ─► S5 V2 IR + Gate 2 + grouping
  ─► S6 generate/store/serve code ─► S7 Spark gold proof + proof-aware capability
  ─► S8 on-demand verification ─► S9 exact-output CAS publication ─► S10 API/UI workflow
  ─► S11 generation-only corpus ─► S12 live LLM gate + incremental operations
```

**What changed in revision 13.** Two of revision 12's own inventions were wrong: rule 7's byte-freeze
cannot be had by adding an optional field, because `_plain_v2` serializes every dataclass field — it
needs per-version parser, model and canonical projection dispatched together; and the deterministic
critic status **raises** on the existing `Literal`, so V2 gets its own review-execution status and a
`REVIEW_BYPASSED` transition instead. Rule 14 removes the second direction-policy owner revision 12
created. Rules 15 and 16 are new: per-read temporal semantics, and LLM realizations usable with
visible provenance. The execution boundary is now explicit — V1's contracts are typed V1 and cannot
carry a string policy id or a V2 IR — and Gate 2 runs over the **union** read set, since policy
resolution adds reads the authored formula never named. S0.5 is four blocks, and **S1 does not begin
until they are frozen**.

**No duration estimate.** Ten revisions have now carried one that a review invalidated.

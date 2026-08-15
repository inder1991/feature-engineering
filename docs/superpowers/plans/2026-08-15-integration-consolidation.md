# V2 as the feature language, on shared execution

**Date:** 2026-08-16 · **Revision 12** — a sixth review found **four blockers and four major gaps**,
all validated. The uncomfortable one: **S0.5, the stage added in revision 11 to stop interface
mismatches, contained one.** It froze the recipe-side expectation contract and not the artifact that
actually compiles, and never noticed that its own change moves V2's pinned bytes.

> **V1 remains the stable compatibility language. V2 becomes the intelligent, policy-aware language
> for all new features. LLMs create and enrich V2 formulas; a deterministic V2 compiler resolves
> their banking meaning and runs them through the existing Spark/Kedro materialization platform.**

**Do not reduce V2 to V1. Do not build a second V2 platform.** Revision 4's "lowering" stays
withdrawn. Reuse V1's **execution machinery**; never V1's **artifact**.

## 0. Position

| | V1 | V2 |
|---|---|---|
| Role | frozen compatibility language | the language for all new features |
| Gets | correctness fixes only | policy awareness, multi-expression, advanced aggregation, LLM authorship |
| Users choose? | no — the platform decides internally | no — the UI never asks for a version |

**"V2" is the language; `formula_schema_version` is a wire pin [R12].** They are different numbers and
this plan keeps them apart. The V2 language is served by wire schema **2 today and 3 after S0.5**;
bumping the pin does not create a "V3 language". Admission dispatches on the **version triple**
(`formula_schema_version`, `operation_grammar_version`, `canonicalization_version`), which
`_verify_schema_version` currently hardcodes to v1-only.

**Shared machinery, changed additively.** Revision 10's plan to change `group_by_contract` from
refuse to split stays **withdrawn** — it breaks its return contract and ripples through
`derive_group_contract` and `compile_feature_group`. V1 stays byte-identical; V2 gets
`partition_contracts_v2()`. The one genuinely shared change is the renderer's target contract (S2).

**Two-speed default.** *Deterministic recipe-driven V2 authoring* may ship at S1. **Free-form
LLM-authored V2 must not become the default until S12's live-provider gate passes.**

## 0.1 Verified before planning

Re-checked against this tree. **Baseline `29f3b8ac`; targeted suite 2,827 passed, 1 skipped.**

**Where "debit" must live — revision 11 got this wrong [R12]**

- The **proposal** is what compiles: `TypedFormulaProposalV2 → FormulaBodyV2 →
  AggregateExpressionV2`. Its only row-selection slot is **`filter: FilterNode | None`** — the exact
  field `recipe_authoring.py:542` rejects as `UNAUTHORED_FILTER`.
- `ExpressionRoleExpectationV2` / `BoundExpressionExpectationV2` are the **expectation** side. A
  field added only there is **never seen by the compiler**, so invariant 12 would be unimplemented.
- `AggregateExpressionV2` carries `aggregation · operand · source_relation · filter · window ·
  aggregation_argument · second_operand · authority_refs`. **No semantic selection.**

**And that change moves V2's pinned bytes, which no rule protected [R12]**

- `FORMULA_SCHEMA_VERSION_V2 = 2` is a **validated pin** — parse refuses a mismatch.
- The gold fixture pins `expected_proposal_hash = 093de7a0…`. Adding a field to
  `AggregateExpressionV2` changes `canonical_json` and moves it.
- **Rule 6 protected V1 only.** V2's frozen bytes had no invariant at all.

**Measurements that will move [R12]**

- `test_blueprint_derivation.py` pins `EXPECTED_OUTCOMES = {"DERIVED": 90,
  WINDOW_NOT_EVENT_ANCHORED: 102, …}`. **S0.5 changes the contract that produces them.**

**The pilot cannot be AUTHORED today**

- No semantic direction field anywhere in the expectation or proposal contracts; any proposal filter
  is `UNAUTHORED_FILTER`. A deterministic producer must either omit the filter (all posted amounts)
  or be rejected. The fixture's `direction == "debit"` is **structurally unauthorable**.

**Generate-without-publish cannot render today**

- `render/project.py` refuses when `datasets.published not in written`: *"no node writes the
  publication target … a project that computes a group and publishes nothing is one an operator would
  have to inspect a warehouse to discover."* The publication dataset is shaped from
  `PublisherSelection`; `chain.py` executes only under one.

**Activation is not the gate that blocks AI-proposed features**

- `@router.post("/materialization-runs", dependencies=[Depends(require_confirmer)])`;
  `require_confirmer` demands the raw **`platform-admin`** role claim. The fold also inherits
  **`_contract_blockers`**.

**Everything else confirmed**

- `PHYSICAL_TYPE_POLICY_VERSION = 4` hardcoded in **both** `contract.py:648` and `group_plan.py:346`.
- `replay_authoring_v2.py`: `if checkpoint.critic_result is None: review = critique(…)` — the critic
  **is** a provider call.
- `1023_recipe_formula_shadow.sql` gives the work item recipe-specific NOT NULL columns, a FK to
  `recipe_formula_shadow_expected_run`, and **four triggers**.
- `identity_payload()` holds `expr_path · physical_read_set · join_steps · pit · input_requirements ·
  aggregation · filter_tree` and **excludes** `non_physical_refs` / `operand_type`: identity covers
  *what it computes*, not *why it was allowed*. **`filter_tree` is an opaque `Mapping[str, Any]`.**
- `authority_refs` are documented as **"identity-bearing exactly as authored"** — *formula* identity,
  a different hash from `ir_hash`.
- `deploy/kind/sandbox/Dockerfile.spark` installs `openjdk-17-jre-headless`; `LocalClusterSubmitter`
  is a real local `kedro` run. `spark_semantics_gate.py` and `l0_gate.py` already run generated
  artifacts on a real JVM, outside the default suite.
- `AUTHORITY_MATRIX` feeds `authority_matrix_hash`; growing it makes frozen options surface
  `ACTIVATION_STATE_DRIFTED` and regenerate.
- `PitSpec` excludes `empty_window` / `null_input`; no offset or future-horizon representation.
- Input snapshot ids hash requirement, ordered partitions and business date — **not content**.
- **`GATES_PASSED` is not terminal**: `{GATES_FAILED, PUBLISHED, PUBLICATION_REFUSED, RUN_FAILED}`.
- **Eight external validation requirements** (`ADDITIVITY_SUPPORTS_OPERATION · CURRENCY_CONSISTENT ·
  GRAIN_IS_UNIQUE · JOIN_CONNECTIVITY · TEMPORAL_IS_POPULATED · TEMPORAL_LAG_BOUNDED ·
  TYPE_IS_NUMERIC · UNIT_CONSISTENT`).
- `sandbox_execution_hash` requires a non-defaulted `capability_attestation_id`.
- `resolve.py` imports only the V1 restorer; `admission.py` types `AuthoringResult` /
  `TypedFormulaV1`; `recipe_formula_shadow_work_item` is the only durable intent anchor; the V2
  worker always enters LLM replay.
- `govern.py:610` selects `WHERE feature_name = %s ORDER BY version DESC LIMIT 1`.
- `api.ts:4190` has only a GET; the POST takes shadow work-item ids capped at 12.
- `required_policy_kinds()` has zero production callers; `_manifest_lines` is "counts, hashes and
  locations only"; `resolve_physical_type` takes `TypedFormulaV1` only; `engine_capability` derives
  from renderer dispatch.
- `banking_policies.py` registers kinds; `eligibility_store.py` is keyed `(catalog_source, table)`,
  flag/code-only; **currency and allocation have no store**.
- `business_dt` appears nowhere in `api/routes/materialization_runs.py`.
- Cross-catalog execution refuses at `chain.py:906`; migration prefixes collide at **0973, 0974,
  1034, 1036, 1037, 1038, 1040**.
- The pilot's gold is still three disagreeing artifacts (blueprint UTC `(start,end]`; fixture
  Asia/Dubai `[start,end)` 90 d; execution gold `(start,end]` 30 d), the ledger's `direction` is
  `D`/`C`, and the fixture lacks availability timestamps.
- DEFERRED-WORK: A.32 🔴 `requirements.lock`; DATE clocks outside UTC refuse.

**Two standing instructions [R12]**

1. **Grep for the thing before designing it.** Six prior instances — `PitSpec`,
   `VERIFICATION_STAMPS`, the `engine_capability` precedent, the work item, the Spark gates, and
   revision 11's expectation-vs-proposal mistake.
2. **Every acceptance criterion must be satisfiable with only what exists at its own stage.** This
   has failed in four consecutive revisions: revision 8's artifact-before-resolvers, revision 10's
   S2-asserts-V2, and revision 11's S0-needs-S0.5 and S3-asserts-`ir_hash`. **Each acceptance block
   below has been re-read against this rule**, and the two that failed are moved.

## 0.2 Invariants

1. No V2 formula executes with an unresolved policy reference.
2. **A non-empty policy string is not proof that the policy exists.**
3. **Every required occurrence is covered by a complete, connected realization subgraph reaching the
   result**, proved at generation over the plan and the emitted code — never by a reported hash.
4. No policy may disappear when V2 reuses existing operators.
5. **Two identities, kept apart [R12].**
   - **Formula identity** (the proposal's canonical bytes) covers what was **authored**: authority
     refs and semantic selections, "identity-bearing exactly as authored" — the existing rule.
   - **`ir_hash`** covers **resolved executable semantics**, and excludes provenance and revision
     numbers.

   | Identity-bearing in `ir_hash` — **changes rows** | Recorded, **not hashed** — explains |
   |---|---|
   | status column, eligible values, null behaviour | producer, confidence, evidence, observed values |
   | direction column, debit values, normalization | model and prompt version |
   | reversal mode and its columns/linkage | approval state, who confirmed, when |
   | rate table, keys, effective timestamp, booking-vs-settlement, direction, rounding | **the realization revision id itself** |
   | allocation method and its columns | lineage narrative |

   Two realization revisions resolving to the same executable fields produce the same `ir_hash`;
   which was used is recorded on the compilation and execution records. Revision 11 stated this
   without saying which identity it governed, while `SemanticRowSelectionV1` lands in both.
6. **V1 formula hashes, V1 IR canonical bytes and V1 function signatures remain unchanged.**
7. **V2 wire schemas are frozen once shipped [R12].** Schema 2 stays registered and byte-frozen with
   its hash checked at import; S0.5's field lands in **schema 3**. Rule 6 protected V1 only, and the
   plan's own first contract change would have silently moved V2's pinned
   `expected_proposal_hash`. **A gold fixture is re-pinned deliberately, in a commit that says so —
   never repaired to make a suite green.**
8. V1 and V2 share publication and validation machinery.
9. The LLM proposes and explains; deterministic code executes.
10. The UI never asks a user to choose a formula version.
11. **"Supported" means generated code has been executed successfully**, recorded against a
    **capability signature** — not an operator name.
12. **Required = declared = resolved = covered, occurrence-addressed and MANY-TO-MANY:**

    ```
    PolicyRequirementOccurrence  ──covered by──►  PolicyRealizationSubgraph
                                                    ├── operator A
                                                    ├── operator B
                                                    └── output operator C
    ```

    `Scan`, `Aggregate` and `FinalCombination` may realize **no** policy; **one FX occurrence needs
    several operators** (temporal join, duplicate-rate gate, missing-rate gate, multiplication); one
    linked-reversal occurrence needs an as-of projection, self-join and anti-join; two compatible
    policies may legitimately **fuse**. Policy-generated operators carry
    **`realizes_occurrences: set[OccurrenceId]`**; generic operators carry the empty set. An
    occurrence is `expression_path + physical source binding + policy kind + semantic role`. **Two
    occurrences may pin the same immutable realization revision.**
13. **The formula says WHAT; the policy says HOW.** A formula declares the business direction it
    wants (`debit`); the realization says "debit means `D` in this source". **Neither may infer it
    from a recipe name or prose.**

**Rule 11's bootstrap:** an operator's first execution happens on **S7's development gold path**,
outside the product.

## 0.3 Division of labour

**LLM:** understand the hypothesis · select recipes · propose candidates · map roles to concepts ·
recommend physical columns · interpret source-specific status descriptions · propose debit/credit
conventions and reversal representations · identify currency and rate columns · produce structured V2
formulas · critique · explain refusals · summarise results.

**Deterministic code:** validate structure · bind columns · derive the required occurrence set ·
resolve realizations · check coverage · compile · generate Spark · execute · validate · publish.
**The LLM never injects free-form SQL.**

**Activation is three actions — but activation was never the blocker.** The route requires the raw
`platform-admin` claim and the fold inherits `_contract_blockers`. **S2 owns both explicitly
[R12]** — revision 11 named `_contract_blockers` twice and assigned it to nobody. S2 delivers
separate endpoints · **`feature:generate`** · a sandbox-verification permission · a distinct
sandbox-publication permission · action-specific activation that does not inherit human semantic
confirmation for generation or verification. Human approval stays **visible as trust provenance**
and stops being silently mandatory.

**Changing `AUTHORITY_MATRIX` is a migration**: it moves `authority_matrix_hash` and **every frozen
option pinned to the old hash regenerates**. Sequence it deliberately, with the regeneration expected
and counted.

**Hard contradictions, leakage, unsupported fan-out and missing semantics still refuse** at every
level.

## 0.4 Three words that must not blur

**Execution proof** (S7) — development-time, mutation-tested, on the existing Spark gates. **Sandbox
verification** (S8) — user-triggered, against the exact sealed artifact on real data.
**Publication** (S9) — atomic, group-wide, promoting *exactly the verified bytes*.

## 0.5 Migration ledger, and allocation

**S0 builds a ledger with a CI uniqueness test** (prefixes collide seven times). Each stage allocates
against it at the point it needs storage:

| Stage | Storage |
|---|---|
| S1 | generalized authoring work item *(migration + backfill + compatibility reader)* · exact selected-feature identity and build mapping · `BuildDeclarationV1` |
| S2 | build-set / child-group records · staged group output · the three request/attempt/result triples |
| S3 | policy realization revisions |
| S6 | the immutable artifact repository |
| S7 | operator execution proofs |
| S8 | verification requests/attempts/results · verified output revisions · profiles |
| S9 | publication requests/attempts |

**S2's first task is the explicit existing-vs-new map.** `materialization_request`,
`materialization_generation`, the compiled-artifact and run-event tables exist and carry meanings that
fit parts of the triples. **No parallel `FeatureBuild*` tables without it.**

## 1. The sequence

### S0 — decide the pilot's semantics *(hard STOP: humans decide)*

**Narrowed [R12]: S0 decides and records; it does not freeze artifact agreement.** Revision 11
required "all three artifacts agree" at S0, but the derived blueprint **cannot express a direction
until S0.5 adds the field**. S0 cannot freeze a truth one of its artifacts is structurally incapable
of stating.

Decide: policy-reference namespace · window timezone, boundary and length · **reversal-as-of
semantics** · **direction mapping (`D`/`C` ↔ debit/credit)** · population spine · **FX join and
cardinality** · publication rule.

**Author the expected rows** for one coherent parameterized exemplar, extended to the missing cases —
zero-eligible spine account · unknown-transaction account · post-cutoff reversal · duplicate and
missing FX rates · post-cutoff FX knowledge time — **and carrying real availability timestamps**,
without which an as-of rule cannot be tested.

**Decide the FX branch, and make the chosen branch buildable.** Either **prove and pin a same-catalog
rate source**, or add explicit conditional tasks for a **reference-data join authorization** — *not*
an entity bridge, which exists to establish customer identity and is the wrong instrument.

Also: the migration ledger.

> **Acceptance:** each contradiction has a decision record naming who decided; the expected rows are
> stored and hashed, with availability timestamps; the FX branch is chosen and its tasks written;
> the ledger's CI test fails on the seven existing collisions unless grandfathered. **Artifact
> agreement is S0.5's acceptance, not S0's.**

### S0.5 — freeze the load-bearing contracts

**Interfaces before implementation.** Six revisions have each found a stage designed against an
interface that did not fit — including revision 11's own version of this stage.

- **`SemanticRowSelectionV1`, on the artifact that COMPILES [R12]:**
  ```
  SemanticRowSelectionV1(kind="transaction_direction", role="direction",
                         semantic_value="debit",
                         policy_ref="direction_sign:foundation-dc-convention")
  ```
  It lands on **`AggregateExpressionV2`** — the proposal's expression, which is what the compiler
  reads — **and** on `ExpressionRoleExpectationV2` / `BoundExpressionExpectationV2` so the
  expectation can be checked against it. **Revision 11 named only the expectation types, which the
  compiler never sees.** It is **not a filter**: `UNAUTHORED_FILTER` keeps rejecting unauthored
  filters unchanged.
- **Wire schema 3 [R12].** The field changes `canonical_json`, and `FORMULA_SCHEMA_VERSION_V2 = 2`
  is a validated pin. **Schema 2 stays registered and byte-frozen, hash-checked at import; schema 3
  carries the new field** (rule 7). Admission dispatches on the version triple. The gold fixture's
  `expected_proposal_hash` is **re-pinned in a commit that says why**.
- **Occurrence-to-subgraph coverage** (rule 12), with `realizes_occurrences` on operators.
- **`StagedGroupOutputV1`** — the neutral handoff, requiring no publication capability and no
  published table.
- **The build-set / child-group lifecycle** (S2's hierarchy).
- **V2 physical-type identity**, namespaced — `formula-v2/physical-types@1`, never another
  unqualified integer beside V1's `PHYSICAL_TYPE_POLICY_VERSION = 4`.
- **Policy payload schemas** per kind, and the **automatic realization-selection rule**.

**Re-measure what this moves [R12].** `EXPECTED_OUTCOMES = {"DERIVED": 90,
WINDOW_NOT_EVENT_ANCHORED: 102, …}` is pinned against the contract this stage changes. **The counts
are re-taken and re-pinned deliberately** — S11's "90 derivable" is a pre-S0.5 number and is not a
constant of this program.

> **Acceptance:** each contract has a frozen schema with a pinned hash and a test that fails if a
> field is added without updating it; **schema 2 remains byte-frozen and parseable**; V1 signatures
> and canonical bytes untouched; the blueprint for `posted_debit_amount` derives a
> **direction-selecting** expectation, and **blueprint, fixture and expected rows now agree** (S0's
> deferred half); `EXPECTED_OUTCOMES` is re-pinned with the delta explained.

### S1 — selection and durable authoring

**Exact feature identity** (`feature_definition_key` / `revision` / `executable_revision_id` /
`output_column_name` / `display_label`), applied **at contract creation**, with its **mapping
stated** to `suggestion_id_v3`, `suggestion_revision_id`, `(considered_revision_id, option_id)`, the
candidate key and the authoring run. **`govern.py:610`'s latest-by-name query gets a named
remediation.**

**Stable output naming needs an algorithm:** parameter projection · maximum length · collision
suffix · **behaviour when a display label changes** (the physical name must not move).

**`BuildDeclarationV1`, sealed** — population spine · cadence · availability promise · target
environment and inventory · requested build-set name · parameters and semantic parameter bindings.
Compilation needs these at S5; the UI comes at S10.

**Generalize the work item — a migration design, not a rename.** `1023`'s table has recipe-specific
NOT NULL columns, a FK to `recipe_formula_shadow_expected_run`, and four triggers. S1 delivers
**migration + backfill + compatibility reader** for `formula_authoring_work_item`.

> Generation must not depend on shadow **ranking**, the **top-12 cap**, or **opportunistic capture**.

**A deterministic authoring producer, honestly described.** It binds the reviewed blueprint,
constructs the proposal deterministically, runs the **structural, expectation-preservation and
output-authority** gates, and writes the standard V2 replay trace. **The LLM critic records
`not_applicable_reviewed_blueprint`** and the trace records **`producer_kind=reviewed_blueprint`** —
`critique()` is a provider call, so "same critic gates" and "no provider call" could not both hold.

**S1 ships deterministic formula authoring — not "feature generation".** Code generation does not
exist until S6, execution proof until S7.

**V2 resolution and admission:** version-triple-dispatched trace restoration · V2 terminal hash
verification · `ResolvedFeatureInputV2` · `AdmittedFeatureV2` · proposal/output-pair coherence ·
shared batch and name-collision handling. V1 admission byte-untouched.

> **Acceptance:** four variants of one display name resolve to four features with four stable column
> names, and a label change moves none of them; a candidate outside the shadow top 12 authors and
> admits; a reviewed recipe produces a durable replayable `AuthoringResultV2` **with no provider call
> and no fabricated critic event**; legacy work items still read through the compatibility reader; a
> **frozen-bytes test pins existing V1 formula hashes**.

### S2 — lifecycle split, on V1, with staging-only execution

**First: the existing-vs-new table map.** Then three commands:

| Command | Does | Must not |
|---|---|---|
| `generate_code()` | render, seal, store | execute, publish |
| `verify_in_sandbox()` | execute the sealed artifact to **staging**, record results | publish |
| `publish_sandbox()` | promote **exactly the verified output** | execute |

**The neutral handoff:**

```
generated project → StagedGroupOutputV1 → verification → VerifiedOutputRevisionV1 → publication pointer switch
```

**The renderer's invariant is REPLACED, not deleted [R12].** Today it refuses when
`datasets.published not in written`, and that guard exists to catch a project that computes a group
and writes nothing discoverable. The replacement: **a project must write either a published target or
a staging target.** Revision 11 said "renders with no publication target", which a naive
implementation satisfies by deleting the check.

`StagedGroupOutputV1` requires **no publication capability and no published table**. This changes the
generated dataset contract, run preparation and execution identity — **it is not three wrappers.**

**The lifecycle is a hierarchy, because one selection can split into many groups:**

```
SelectedBuildSet
  ├── DerivedGroup A → GeneratedArtifact A → verification → publication
  ├── DerivedGroup B → GeneratedArtifact B → …
  └── DerivedGroup C → generation refusal
```

Each child carries independent generation, verification and publication state; the parent records
overall completion and partial results.

**Routes, permissions and `_contract_blockers` [R12]:** separate generate / verify / publish
endpoints · `feature:generate` · a sandbox-verification permission · a distinct sandbox-publication
permission · action-specific activation. **S2 owns removing generation and verification from
`_contract_blockers`' inherited human-confirmation requirements** — `require_confirmer` is why
AI-proposed features are blocked today, not the authority matrix.

**`VerificationExecutionIdentityV1`**, separate from `sandbox_execution_hash`.

> **Acceptance (on V1, which runs today):** a project renders and seals writing **a staging target
> and no published target**, and a project writing **neither** still refuses; verification executes
> to staging with **no publication capability present**; a non-admin with `feature:generate` can
> generate and cannot publish; every triple reaches a terminal state. **V2 boundaries are asserted
> at S6 and S8, not here.**

### S3 — occurrence-addressed policy realization, with a producer

**`PolicyRealizationRevisionV1`** — immutable, keyed by `abstract policy ref + physical dataset
binding + environment/source + effective semantics`, its fields split by rule 5's table.

**Implement only the pilot's modes; define the rest and refuse them by name:** eligible statuses ·
**indicator-based direction selection** (the `D`/`C` mapping behind `SemanticRowSelectionV1`) · the
decided reversal mode · booking-date FX.

**The booking-FX realization is typed, not a reference:** rate table binding · source and target
currency keys · effective/booking-time key · **knowledge/availability time** · quote direction and
whether inversion is permitted · rate column and decimal policy · missing-rate behaviour ·
duplicate-rate refusal · **expected join cardinality**.

**Add the producer workflow** — the LLM structured-output contract · source/profile evidence
collection · deterministic proposal validation · conflict detection · current-revision selection ·
realization writes · UI disclosure. Without it the store stays empty and every V2 formula refuses.

**Resolve the dual authority:** `eligibility_store.py` is mutable and already answers status and
reversal. Migrate it or put a single adapter in front of it.

**Wire `required_policy_kinds()`** into occurrence derivation (rule 12).

**Policy inputs get full PIT and read-set treatment**, and **enter the materialization contract** — a
restricted FX table must not ride into a public group behind a public transaction input.

> **Acceptance:** an unresolvable reference refuses by name; a formula omitting a required occurrence
> refuses; an unsupported reversal mode refuses and names it; a realization whose as-of rule would
> read post-cutoff FX refuses; **one status policy used by two expressions yields two occurrence
> bindings, which may pin the same realization revision**; **two realizations differing only in
> provenance produce identical resolved executable fields** *(the `ir_hash` half of rule 5 is
> asserted at S5, where the IR exists — revision 11 asserted it here [R12])*.

### S4 — the bound V2 formula

**`BoundFormulaRevisionV2`** — authored proposal *(including `SemanticRowSelectionV1`)*, output
policy, physical bindings, realization pins. **No complete-read-set claim** — only planning discovers
reads. **Compiler versions must not change its semantic identity.**

> **Acceptance:** a compiler version bump leaves the bound-formula hash unchanged; the artifact
> cannot be constructed with an unpinned realization or an unresolved semantic selection.

### S5 — V2 IR, complete reads, contracts and the V2 contract partitioner

**`FormulaExecutionIRV2`, a distinct type.** Operators carry `realizes_occurrences` (rule 12) and
identity payloads holding **resolved executable semantics only** (rule 5).

**All V2 expressions use `TemporalReadSpecV2`, including offset zero.** The renderer lowers the
simple case to the existing window helper, so nothing is duplicated in execution while V2 keeps one
temporal vocabulary.

**V2 physical types reach the contract honestly:** explicit V2 contract derivation and group-plan
construction carrying **`formula-v2/physical-types@1`**. Reusing the V1 builders would stamp
`PHYSICAL_TYPE_POLICY_VERSION = 4` on a V2 decision. **V1 canonical bytes unchanged.**

**`partition_contracts_v2()` — a V2 coordinator, leaving V1 alone:**

```
SelectedBuildSet → derive ONE contract per feature → partition into one or more FeatureGroupPlanV1
                 → each group generates, verifies and publishes independently
```

Plus a **deterministic group-name allocator**:
`<requested-base>__<contract-class>__<short-contract-hash>`. **`group_by_contract()` keeps its
single-result refusal contract.**

**The pilot's operator order is derived, not frozen.** A reversal row may carry a reversal-specific
status, the opposite direction, arrive after the original, or fall outside the original's event
window while still being known by the cutoff — so filtering status or direction first can delete the
record needed to neutralize the original. The general shape:

```
availability / PIT cutoff
  → construct reversal relationships from the required as-of row population
  → derive surviving economic events
  → apply eligible-status and direction semantics
  → event window
  → FX
```

**The subgraph is derived from the reversal mode S0 selects.**

> **Acceptance:** V1 IR canonical bytes and the single-contract path byte-identical; a set spanning
> two contracts yields two groups; group names are deterministic and collision-free; the direction
> mapping is a typed operator; **a re-approval that changes no executable field leaves `ir_hash`
> unchanged** *(moved here from S3)*; **a linked-reversal mode produces an order in which the
> reversal row survives long enough to neutralize its original**.

### S6 — generate, persist and serve readable code

Nodes derived from the S5 subgraph. **Execute nothing. Publish nothing.**

**The immutable artifact repository and retrieval API land HERE**, not S8 — S6 promises persistence
and a stable code-view API.

**Rule 3's proof, fourth attempt.** Copying plan hashes was forgeable; a renderer-reported hash from
the emitting branch was still forgeable. The proof is **subgraph coverage**: every required
occurrence must have a complete, connected realization subgraph reaching the result, resolved through
`realizes_occurrences`. Generic operators contribute no coverage and are not required to.

> **Acceptance:** emitting a filter and then aggregating the unfiltered input **refuses**; an FX
> occurrence whose duplicate-rate gate is dropped refuses even though its join remains; every
> occurrence has a connected subgraph; **S2's generate/verify/publish boundaries re-asserted on V2**.

### S7 — independent generated-code gold proof

**Extend `spark_semantics_gate.py` and `l0_gate.py`** — do not build a harness — and keep them
outside the default suite. **Target: `deploy/kind/sandbox/Dockerfile.spark` via
`LocalClusterSubmitter`.** S7 needs *a* Spark, not the product's remote seam.

The generated code reproduces the frozen exemplar including S0's added cases, and **all six mutations
fail**: drop the status filter · count reversal rows · one flat FX rate · settlement instead of
booking date · ignore direction · shift the window boundary.

**`OperatorExecutionProofV1` mints a capability signature** — input/output types · window form ·
policy operator families · null and empty behaviour · rounding.

**Close here:** the DATE-clock refusal outside UTC, and A.32 🔴 `requirements.lock`.

> **Acceptance:** every case and mutation behaves; each proof names S0's frozen hash; a signature
> mismatch does not satisfy a proof; the gates stay outside the default suite.

### S8 — execution infrastructure and on-demand verification

**Infrastructure:** `business_dt` on the API · server-side Hive schema read · captured inventory ·
**the product's remote submission seam** · **a dedicated materialization worker**.

**Checks are a DAG, and profiling is not in it:**
`build + static → execute → ⟨keys · grain · types · nulls · inflation⟩ → fold`. The blocking set is
**`output_sanity`**; advisory EDA/profile generation is a separate non-blocking attempt.

**Input snapshots become pinnable without Iceberg:** a Hive table snapshot/version where available;
otherwise a **partition file manifest hash**; compared at publication. Where neither exists, record
**`input_observation_strength = UNPINNED`**.

**`VerifiedOutputRevisionV1`** — generation and verification identities · immutable staging location ·
output data/file manifest hash · schema hash and row count · group-plan and IR hashes · result ·
expiry/retention · `input_observation_strength`.

**Verification maps to requirements explicitly**, many-to-many over the registry's eight, emitting
`EXTERNAL_PASSED` **only where the check's result schema genuinely matches**. A group pass must not
turn every member `DATA-CHECKED`. Never write `USEFULNESS-CHECKED` here.

**Staleness is computed on read**; detected automatically, never re-run automatically.

> **Acceptance:** a verification survives restart with per-check results intact; changing any
> recorded input flips a pass to stale and names it; a keys/types check does not satisfy
> `JOIN_CONNECTIVITY`; a profiling failure leaves verification passed; nothing writes a stamp
> directly.

### S9 — atomic sandbox publication

Publish **only the exact verified output** — compare the staging manifest before promotion; **no
re-execution**. Recheck publication capability and policy dependencies. Whole group, atomically.

**Decided:** *generated code may remain unverified indefinitely; publication requires a current
passing verification.*

**`UNPINNED` inputs may publish, labelled honestly.** An unpinned source cannot be proved unchanged,
so "current" cannot mean "source-current". Publish the exact verified staging output carrying
`input_observation_strength = UNPINNED`, and **never label it reproducible or source-current**.

> **Acceptance:** a changed or missing staging manifest blocks promotion; a stale verification
> blocks; capability revoked between verify and publish blocks; a partial group never becomes
> visible; an `UNPINNED` publication is labelled as such everywhere it is read.

### S10 — UI integration

**Generate → Verify → Publish**, three actions never collapsed, over the child-group hierarchy.

```
Posted debit amount · 90 days
  Formula        Ready
  Policies       Resolved
  Code           Generated
  Verification   Not run
  [View generated code]   [Verify in sandbox]
```

Show: selection · **the derived group split, before verification** · generated code · **policy
provenance** (rule 5's right-hand column) · verification state including `UNPINNED` · publish
eligibility. Disclose that **one failed member prevents publishing its group**.

The UI for creating `BuildDeclarationV1` lands here — the contract itself shipped at S1.

> **Acceptance (end-to-end):** no path reaches execution without an explicit click; group scope is
> visible before publish; a candidate outside the shadow top 12 is generable **from the UI**; a stale
> result renders as stale, not failure.

### S11 — from one feature to the corpus, **generation only**

- Batch the **derive → compile → generate** path across the derivable blueprints, recording refusals
  by named code. **The count is S0.5's re-measurement, not the pre-S0.5 "90" [R12].**
- **Each blueprint needs a `BuildDeclarationV1` [R12]** — spine, cadence, environment. The batch
  supplies a **declared default set** and records which blueprints could not be declared, rather than
  inventing one per blueprint.
- Land the **as-of snapshot window shape** and measure its effect on the count.
- Drive `WINDOW_NOT_EVENT_ANCHORED` down as registry work, not compiler special cases.

**Verification and publication are NOT batched.** Batching S0–S9 would execute and publish
automatically, contradicting the user-triggered rule S8 and S10 exist to enforce. **Verified and
published counts reflect prior explicit user actions only.**

> **Acceptance:** a batch run produces a coverage table (`derivable → generated`, then observed
> `verified → published`) with every refusal carrying a named code; **the batch triggers no
> execution**; blueprints lacking a declaration are reported, not defaulted silently; no blueprint is
> made to pass by a special case.

### S12 — live-provider gate, then incremental expansion

**The gate is representative of what is advertised when it runs [R12].** For the pilot that is one
family; the gate grows with the advertised set rather than claiming breadth it cannot have. It tests
parsing · expectation preservation · policy resolution · **refusal behaviour** · trace persistence.
One successful response proves a connection, not a language.

**Operator dependency:** Anthropic billing must be topped up — cluster LLM stages fail closed.

Then, one family at a time, each needing renderer support **and** an execution proof: ratios →
offsets → composites → percentiles → slopes → allocation → future horizons. **Each family entering
the advertised set also enters the live gate.**

> **Acceptance:** the gate exercises every currently-advertised family and asserts refusals as well
> as successes; the advertised set is computed as `renderer-supported ∩ execution-proved`.

## 2. Carried forward

**Parallel, gated at merge:** behavioural frontend↔backend contract tests, since `/features/recipe`
exists and refuses everything · hide the retired "Write definitions" control · recognition
correctness in full · ruff ratchet (79 repo-wide, 35 in `src/`).

**Narrow the leakage claim** — carried since revision 2 without its context; recover the original
finding before acting.

**Still required:** canonical typed planning-request JSON persisted, since `repr()` cannot
reconstruct a request · a **qualified evaluation-artifact reader** with a validity contract
(migration 1029's four tables exist; a stale pass is not a pass).

**Out of scope unless S0 decides otherwise:** cross-catalog execution refuses by construction
(`chain.py:906`); content-addressed input snapshots need the deferred Iceberg layer, which S8's
manifest hash works around rather than solves.

## 3. Sequencing

```
S0 decide semantics ─► S0.5 freeze contracts ⟨schema 3⟩ ─► S1 selection + authoring + BuildDeclaration
  ─► S2 lifecycle split ⟨on V1, staging-only⟩ ─► S3 policy realization ─► S4 bound formula
  ─► S5 IR + V2 contracts + partitioner ─► S6 generate + persist + serve
  ─► S7 gold proof ⟨local Spark gate⟩ ─► S8 infra + verify ─► S9 publish ─► S10 UI
  ─► S11 corpus ⟨generation only⟩ ─► S12 live gate + expansion
```

**What changed in revision 12.** `SemanticRowSelectionV1` moved onto `AggregateExpressionV2` — the
artifact that compiles — because revision 11 put it only on the expectation types the compiler never
reads. That change moves V2's pinned proposal hash, so **rule 7 is new**: V2 wire schemas freeze once
shipped, the field lands in schema 3, and schema 2 stays byte-frozen. Rule 5 now names **which**
identity it governs, since formula identity and `ir_hash` are different hashes and the semantic
selection lands in both. S0 no longer claims artifact agreement it cannot reach, and S3 no longer
asserts an `ir_hash` that does not exist until S5 — both instances of a rule now written down as
standing instruction 2.

**No duration estimate.** Nine revisions have now carried one that a review invalidated.

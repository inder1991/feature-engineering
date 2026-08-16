# V2 as the feature language, on shared execution

**Date:** 2026-08-16 · **Revision 18** — a ten-lens plan-vs-code audit (ordering cycles · acceptance
satisfiability · already-exists · three claim-verification passes · internal consistency · coverage
gaps · S0.5 premises · pilot executability), each finding adversarially refuted before acceptance.
**Nine blockers and nineteen majors survived.** Two are corrections to revision 17's *own* audit
section — including one where **a "correction" I published was itself wrong**.

> **S0.5 is task level; S1–S13 are programme level.** S1–S13 are **not executable as written**.

> **V1 remains the stable compatibility language. V2 becomes the intelligent, policy-aware language
> for all new features. LLMs create and enrich V2 formulas; a deterministic V2 compiler resolves
> their banking meaning and runs them through the existing Spark/Kedro materialization platform.**

**Do not reduce V2 to V1. Do not build a second V2 platform.**

## 0.0 Audit provenance, and a correction to a correction

Revision 15 checked 39 claims mechanically. Revision 17 verified 24 findings with a refutation pass.
**Revision 18 ran ten audit lenses, each finding refuted before acceptance.**

**Revision 17 published a "correction" that was false [R18].** It recorded that the planning request
stored as `repr()` is *"never read back"*. **It is read back and enforced**:
`load_option_decision_record` selects `planning_request_hash`, and `contract.py:373-379` raises a
**409 `DECISION_RECORD_TAMPERED`** when the manifest's hash disagrees with the record's — and the
record, including its `evidence`, is served as `detail["decision_record"]`. The earlier pass checked
`load_frozen_option_facts` (18 columns, neither field) and generalised from one reader to all
readers. **Only `parameter_values`' `repr` is genuinely dead.**

**The lesson is now a rule: a correction is a claim and gets the same verification as the claim it
corrects.** A single reader proving absence proves nothing about other readers.

**Standing corrections that still hold:** `run_authoring_v2` has zero production callers, so the
v1-tool trap is latent rather than live — **and there is no free-form V2 authoring path in
production at all**. The request-digest omission is deliberate and covered by
`test_a_key_reused_for_DIFFERENT_DECLARATIONS_is_refused`.

## 0. Position

| | V1 | V2 |
|---|---|---|
| Role | frozen compatibility language | the language for all new features |
| Gets | correctness fixes only | policy awareness, multi-expression, advanced aggregation, LLM authorship |
| Users choose? | no — the platform decides internally | no — the UI never asks for a version |

**"V2" is the language; `formula_schema_version` is a wire pin** — schema 2 today, 3 after S0.5.

**Deferred, and not silently:** cross-catalog V2 execution · multi-environment support ·
Iceberg-grade snapshots · operations beyond the pilot family · effective-dated policy history.

## 0.1 Verified before planning

Baseline `29f3b8ac`. Every claim below was mechanically re-checked for revision 18.

### The pilot cannot produce its own numbers — three structural blockers [R18]

- **No currency operand exists.** `posted_debit_amount` declares no `dim("currency", …)`; `_CCY` is
  the **policy ref** `currency_conversion:foundation-base-currency`, not an operand. Nothing can bind
  the currency column FX reads, so **`ACC2 = 93.50` is structurally unreachable** — this compounds
  the governed-facts precondition rather than duplicating it.
- **There is no physical pilot relation.** `public.txns` appears in `ir.py` docstrings **as an
  example ref format only**. The pilot "ledger" is `synthetic_ledger.json`, a document consumed by a
  test-side evaluator that imports no production code. **S8's generated Spark has nothing to read.**
- **The reversal link is optional.** `_CORRECTION_LINK = OperandSpecV2(…, required=False, …)`, so a
  binding may legally omit it and `LINKED_REVERSAL` has no linkage input — **`ACC1` would be
  380.00, not 300.00.**

### And three more pilot facts the plan must carry

- **Base-currency rows have no dated rate** — only a sentinel `"always": 1.0`. C-C10 makes a
  missing-rate gate mandatory, so **every AED row would refuse** without an explicit identity-rate
  operator.
- **The exemplar's policy refs and the recipe's are different strings**, so preservation fails by
  name (`AUTHORITY_REFS_NOT_PRESERVED`).
- **Timezone and boundary are shared derivation CONSTANTS, not pilot data** —
  `recipe_formula_blueprint_derivation.DERIVED_TIMEZONE = "UTC"` and a hardcoded
  `start_inclusive=Inclusivity.EXCLUSIVE`. **Changing them moves every derived blueprint**, which is
  what re-pins `EXPECTED_OUTCOMES`.

### Ordering and identity facts

- `chain.py` builds `irs: list[FormulaExecutionIRV1]` and authorizes **those exact IRs** afterwards;
  `authorize_compilation` has **one production call site**.
- `inventory: ClusterInventoryV1` is a **required keyword-only parameter** on `compile_feature_group`
  and `compile_ir`.
- `CompilationIdentity` is group-wide (plural, positionally-paired tuples; `group_plan_hash` covers
  every member), and **nothing durably maps a group to its members** — *"THE MEMBERS ARE SUPPLIED,
  BECAUSE NOTHING DURABLE MAPS THEM."*
- `environment_id` is **absent** from `CompilationIdentity` and **required** by
  `sandbox_execution_hash`.
- **Rolled-back runs already leave orphaned staged output** — `chain.py`: *"a rolled-back run leaves
  staged output that no plan"* references. **`runtime/blob_gc.py` already provides the discipline**
  (`marked_orphan → quarantined → swept`).
- **V1 and V2 share one flat `logical_group_name` namespace** with no language discriminator, and
  `group_binding` is `UNIQUE(logical_group_name)`.

### Authoring and activation facts

- `_plain_v2` walks **every** dataclass field; `parse_versioned` accepts **1 and 2**;
  `proposal_v{n}.schema.json` is picked up by the `*.schema.json` package-data glob (**no packaging
  change needed**).
- `gold_fixtures/*.json` are **schema 1**; `gold_v2/*.json` are **schema 2** — different
  canonicalizers.
- `CriticStatus` is `clean | advisory | blocking`; `OUTPUT_POLICY_RESOLVED` requires
  `CRITIC_COMPLETED`.
- **`_PROVENANCES = ("human_confirmed", "user_typed", "exploring")`** — an explicit no-target
  declaration **already ships** (migration 1059), with `target_ref` forced NULL by design.
- `record_target_reading` UPDATEs in place, **no provenance guard**, **no `catalog_source`**.
- Five activation actions, **sixteen** blocker codes, and `request_materialization` /
  `execute_materialization` are **byte-identical in outcome**.
- The conversion tooth fires only on `unit=monetary` / `currency=per_row` and refuses only on an
  **empty** ref.
- `run_authoring_v2` has **no `tool_runner` parameter** and zero production callers;
  `run_authoring_v2_replay` defaults `tool_runner=None`.

### Everything else re-confirmed

Leakage screens `target_ref in derives` only · Gate 2 checks existence and permission, not leakage ·
`SpineSpec.read_set` is already unioned · `PHYSICAL_TYPE_POLICY_VERSION = 4` hardcoded in two
builders · `FeatureGroupPlanV1.physical_type_policy_version: int` · `GENERATED.lock` carries
`generated_project_hash` · `empty_window` already decides V1 nullability ·
`materialization_compiled_artifact` stores plan and contract, **no source bytes** ·
`_gold_evaluation_recorded()` returns `False` · `engine_capability` derives from renderer dispatch ·
inventory records **eight** runtime versions (hive, spark, metastore, python, java, pyspark, kedro,
kedro_datasets) · `group_binding` (1034) and `feature_active_revision` (1055) lack an environment
key · `next_revision_seq` is read-then-write · eight external validation requirements ·
`GATES_PASSED` is not terminal · `eligibility_store` is a mutable upsert · highest migration
**1071**, prefixes collide seven times · gates are `make test`/`make lint`/`make typecheck` and,
from `frontend/`, `npm run typecheck` (`tsc -b`), `npm test`, `npx oxlint`.

**Standing instructions.** (1) **Grep before designing** — thirteen instances now, the newest being
`exploring` and `blob_gc`. (2) **Every acceptance must be satisfiable at its own stage.** (3) **A
correction is a claim** — verify it like one.

## 0.2 Invariants

1. No V2 formula executes with an unresolved policy reference.
2. **A non-empty policy string is not proof that the policy exists** — no gate may certify an output
   on the strength of one.
3. **Coverage is derived from typed operator kinds, parameters and graph topology.**
   `realizes_occurrences` is attribution, and **C-C8 creates it** (it exists nowhere today).
4. No policy may disappear when V2 reuses existing operators.
5. **Identity is layered:** `ExecutableFeatureRevisionV2` (per-feature IR hash) ·
   `DerivedGroupRevisionV2` (contract, plan, `CompilationIdentityV2`, **and the first durable
   group→member map**) · `GeneratedArtifact` (rendered identity, file manifest). *Formula identity*
   covers what was authored; **`ir_hash`** covers resolved executable semantics.
6. **V1 formula hashes, IR canonical bytes, function signatures and contract types remain
   unchanged.**
7. **V2 wire schemas freeze once shipped.** The freeze means **the v2 serializer keeps emitting the
   pre-change bytes for a v2 proposal** — it does **not** mean the fixture *files* never change
   (S0 re-authors them). Schema-2 round-trips are tested byte-for-byte.
8. V1 and V2 share **language-neutral** publication and validation machinery through a V2 adapter.
9. The LLM proposes and explains; deterministic code executes.
10. The UI never asks a user to choose a formula version.
11. **"Supported" = renderer-dispatchable ∧ execution-proved.** These are **two separately recorded
    facts** (§0.4), never one word.
12. **Required = declared = resolved = covered**, occurrence-addressed and many-to-many.
13. **The formula says WHAT; the policy says HOW.** `semantic_value` is a **closed enum per `kind`**
    (`direction ∈ {debit, credit}`) — which is what makes "a physical literal refuses" mechanically
    decidable.
14. **One policy reference has one owner:** `AuthorityRefsV2`.
15. **Every physical read carries its own temporal semantics**; the declared T+N availability promise
    is a **separate contract field**.
16. **LLM-proposed realizations are usable, visibly.** Source beats LLM with a retained visible
    conflict; two governed declarations refuse; two LLM-only with no deterministic winner refuse; one
    valid LLM-only is usable as `LLM_PROPOSED`.
17. **The approved target travels with the selection; a generation is authorized FOR a target.**
18. **Leakage's provable claim is narrow:** direct target exclusion via canonical target plus
    resolved aliases, and knowledge-time cutoffs. **It does not prove semantic proxies or target
    descendants harmless.**

## 0.3 Three evaluators, and every gate they carry

| Action | Requirements |
|---|---|
| **Generate** | selected revision · complete binding · valid formula · resolvable policies · **renderer-dispatchable** |
| **Verify** | the exact sealed artifact · execution permission · environment compatibility |
| **Publish sandbox** | a current passing verification · the exact staging output · publication permission and capability |

**C-D9 must enumerate all sixteen activation codes, code by code, as CARRIED or DROPPED with a
reason [R18].** Revision 17 listed requirements and silently lost **`PERSONAL_DATA_POLICY_REQUIRED`**
— a use-licence gate, not a review preference — so a V2 feature built from unlicensed personal data
would have generated. **Human *semantic confirmation* is what these evaluators drop; data-use
licence, read scope and binding completeness are carried.**

## 0.4 Words that must not blur

**Renderer-dispatchable** — the renderer has a branch for the operator (today's `engine_capability`
answer). **Execution proof** (S8) — generated code ran against reviewed gold, pinning toolchain,
runtime and the gold-project hash. **Sandbox verification** (S9) — user-triggered, tied to the exact
artifact, **never interchangeable** with a development proof. **Publication** (S10) — atomic, CAS.

## 0.6 Rollout and rollback *(new [R18])*

Thirteen stages ship forward-only, checksum-locked migrations. **Every stage declares:** the env flag
gating it (**default off**), backend-first migration order, and what a revert means for append-only
rows already written. **Migrations are files-only until an explicit operator go.** The platform is
pre-live, so a flag is a rollout lever rather than a permanent switch — **state that per stage rather
than assuming it**.

## 0.7 Migration ledger *(renumbered — no §0.5 section exists, because stage S0.5 does [R18])*

Highest today **1071**; prefixes collide at 0973, 0974, 1034, 1036, 1037, 1038, 1040. **S0 records
the ledger; the stage that needs a table writes it.**

| # | Table | Stage |
|---|---|---|
| 1072 | target-reading revision (append-only) · feature selection revision | S1 |
| 1073 | feature definition · authoring work item + backfill · typed planning request | S2 |
| 1074 | generation inventory observation · bound input set revision | S3 |
| 1075 | policy realization revision + current pointer + conflict findings | S4 |
| 1076 | bound formula revision · executable output policy | S5 |
| 1077 | executable feature revision · derived group revision (**incl. membership**) · generation authorization | S6 |
| 1078 | artifact file manifest **+ the content store it points at** | S7 |
| 1079 | operator execution proof | S8 |
| 1080 | verification request/attempt/result · verified output revision · verification inventory observation | S9 |
| 1081 | publication request/attempt | S10 |
| 1082 | build declaration · build set | S11 |
| **1083** | **`environment_id` on `group_binding` and `feature_active_revision`** — ADD COLUMN + backfill + constraint swap (C-D6) | S10 |

## 1. The sequence

### S0 — pilot semantics, numerics, **and pilot data reality** *(hard STOP: humans decide)*

**Make the pilot physically executable [R18].** Three tasks without which S8 proves nothing:

- **Declare a per-row currency operand** on `posted_debit_amount` (and the rate relation's binding).
  None exists; `_CCY` is a policy ref. Record that this moves the recipe's canonical hash.
- **Materialize the pilot ledger as a real relation** with the exact columns the formula binds
  (`acct_id`, `txn_amt`, `direction`, `status`, `currency`, `booking_ts`, the reversal link) plus the
  FX rate relation. Today it is a JSON document read by a test-side evaluator.
- **Decide the reversal link's home** — recipe operand (flip `_CORRECTION_LINK` to `required=True`
  for this recipe) or an input of the reversal realization. It is `required=False` today.

**Numeric decisions:** eligible status values and null handling · unknown-direction behaviour ·
target currency · **base-currency identity rate** (AED rows have only a sentinel `"always": 1.0`, and
a mandatory missing-rate gate would refuse them) · FX missing-rate behaviour · quote convention and
inversion · **conversion before or after aggregation** · rate rounding.

**Reconcile four axes** — timezone, boundary, length, **ref namespace**. Two of these land on
**shared derivation constants** (`DERIVED_TIMEZONE`, the hardcoded `start_inclusive`), so the change
moves **every** derived blueprint. **Apply the chosen policy-reference namespace to
`EligibilitySpecV2.policy_refs` in `transaction_foundation.py` AND to the exemplar in one change**,
or preservation fails by name.

**Author the expected rows** — zero-eligible spine account · unknown-transaction account ·
post-cutoff reversal · duplicate and missing FX rates · post-cutoff FX knowledge time — **with real
availability timestamps**.

**Decide the FX branch.** Also: the migration ledger.

> **Acceptance — branch A (FX in the pilot):** every decision recorded with its decider; the pilot
> relation exists and the formula binds it; a currency operand is declared and bound; **the pilot
> proposal WITHOUT a `currency_conversion_ref` refuses `CURRENCY_CONVERSION_UNDECLARED`**; base-
> currency rows resolve at rate 1 without tripping the missing-rate gate; refs are in the production
> namespace with no `authored::` surviving; expected rows stored and hashed with availability
> timestamps.
>
> **Acceptance — branch B (FX deferred to its own pilot):** as above **minus** every FX clause;
> **and the plan records that C-A7's target currency, C-C6's rate arithmetic, C-C10's FX subgraph and
> S8's FX mutations are deferred with it** [R18 — revision 17 offered this branch and then demanded
> FX artifacts unconditionally].

### S0.5 — freeze the contracts and the identity graph *(task level)*

**Every task has TWO gates [R18].** Revision 17 made S0.5 a hard gate before S1 while most of its
gates asserted durable behaviour over tables the ledger reserves for S1–S10 — **a deadlock, not a
stale reference**. So:

- **Gate (S0.5)** — contract-level: frozen schema, pinned hash, round-trip over an **in-memory or
  fixture store**. Satisfiable with **no migration**.
- **Gate (deferred)** — the behavioural assertion, restated verbatim as the acceptance of the stage
  that writes the table.

**S1 may begin when every S0.5 gate is green. The deferred gates travel with their stages.**

#### A · Authoring contracts

| Task | Deliverable | Gate (S0.5) | Gate (deferred) |
|---|---|---|---|
| **C-A1** | **The v2 SERIALIZER keeps emitting pre-change bytes for a v2 proposal** (invariant 7) — its own parser, model and canonical projection. **This is not "gold_v2 files never change"; S0 re-authors them.** `gold_fixtures/*.json` are **schema 1** and belong to rule 6's V1 test, not here | a v2 proposal round-trips **byte-identically** through the frozen projection, asserted on a **committed snapshot copy** taken before S0's re-authoring | — |
| **C-A2** | `proposal_v3.schema.json` + `parse_v3`; extend `parse_versioned` (accepts 1 and 2). Packaging needs no change (`*.schema.json` glob) | a v3 proposal parses; an unknown version refuses loudly | — |
| **C-A3** | `SemanticRowSelectionV1(kind, role, semantic_value)` on **`AggregateExpressionV2`** and the expectation types, **`semantic_value` a closed enum per `kind`** (invariant 13) — which is what makes the refusal decidable. **v3 gets its own dataclasses**; `_plain_v2` walks every field, so a shared dataclass would rehash every stored v2 artifact | `direction` accepts `debit`/`credit` and **refuses `D`**; a v2 proposal is unaffected by v3's field | — |
| **C-A4** | Schema-3 coherence: a direction selection **requires** `AuthorityRefsV2.direction_policy_ref` | a selection without the ref refuses by name | — |
| **C-A5** | `ReviewOutcomeV2 = CriticExecutedV2 \\| ReviewedBlueprintBypassV2` with V2-specific axes | the sum type round-trips; a bypass carries **no `critic_status`** | S2: a bypass replays **with no `CRITIC_COMPLETED` event** |
| **C-A6** | `REVIEW_BYPASSED` transition; **bump disposition, orchestrator and replay protocol versions**; old readers still read old traces | version constants moved and asserted | S2: an existing recorded trace replays unchanged |
| **C-A7** | **CREATE `AuthoredOutputIntentV2`** (derived from `FormulaOutputPolicyV2`) carrying unit, additivity, conversion-required, declared ref **and desired target currency and numeric shape** — the fields its own test compares. **The type exists nowhere today**, so revision 17's present-tense §0.1 claim about it is deleted | the type is constructible and its field set covers every field S5's refusal compares | **S5** (not S4 — the refusal moved): the refusal compares only fields the intent records |
| **C-A8** | **Tool-seam repair**: `run_authoring_v2` gains a `tool_runner` parameter; **`run_authoring_v2_replay`'s `tool_runner=None` default removed or fail-closed**; the schema-3 matrix covers **tool behaviour** with prompt, turn schema, frozen configuration, replay restoration, candidate union, recipe egress, expectation schema, WORM trace | omitting a runner **refuses** rather than falling back to v1 tools | S13: a v3 proposal through the shared tools is not reported invalid-because-v1 |
| **C-A9** | **[R18]** The **reviewed-expectation re-pin is a gated human act** — S0's re-authoring changes the exemplar's canonical hash and therefore the pinned registry entry. Name the owner, the required reviewer roles and the `recipe_review_event` at the current `canonical_recipe_v2_hash` | the re-pin task exists with an owner and reviewer roles named | S2: the registry entry matches the re-authored exemplar |

#### B · Identity graph

| Task | Deliverable | Gate (S0.5) | Gate (deferred) |
|---|---|---|---|
| **C-B1** | **`TargetReadingRevisionV1`, append-only** — catalog source · target ref · type · horizon · **the existing closed provenance vocabulary `human_confirmed \| user_typed \| exploring`** · canonical content hash. Today `record_target_reading` UPDATEs in place with no provenance guard and drops `catalog_source` | the type round-trips; two catalogs with the same `public.t.c` are distinguishable | S1 (1072): a second reading creates a **new revision**; a `human_confirmed` reading is never silently overwritten |
| **C-B2** | **`FeatureSelectionRevisionV1` is the ROOT record**, immutable, referencing an exact target reading. **`FeatureDefinitionV1` is created or resolved at authoring** with an append-only selection→definition link | a selection is constructible **with no definition** | S1/S2 (1072/1073): the link is append-only |
| **C-B3** | **`ExecutableFeatureRevisionV2`** — **defined here over opaque content hashes** for the bound formula and executable output policy, whose *types* are also frozen here (C-C6a) and whose *instances* S5 produces **[R18 — revision 17 froze it over types S5 delivered]** | constructible from hashes alone | S6 (1077) |
| **C-B4** | **`DerivedGroupRevisionV2`** — members · contract · plan · `CompilationIdentityV2`. **The first durable group→member map**: nothing maps a group to its members today | the type carries membership and a group-wide identity | S6 (1077): membership is queryable without replaying a request |
| **C-B5** | **`GenerationAuthorizationRevisionV2`** — selection · exact target reading · leakage-policy version · verdict · **the planned IR hashes that verdict screened** · Gate-2 authorization (invariant 17) | the envelope binds all five | S6 (1077): an artifact authorized for one target cannot be reused for another |
| **C-B6** | **`BoundInputSetRevisionV2`**, before occurrence derivation | constructible independently of any policy | S3 (1074) |
| **C-B7** | **`GenerationInventoryObservationV1`, captured BEFORE binding** — `inventory` is required by `compile_feature_group` and `compile_ir`. Pin **`environment_id` and the inventory content hash**; `captured_at` is provenance. **`environment_id` enters `sandbox_execution_hash`** | both are pinned and identity moves when either moves | S3 (1074): compilation is unreachable without an inventory |
| **C-B8** | **`VerificationInventoryObservationV1`** + an explicit compatibility algorithm over **all eight** runtime versions (hive, spark, metastore, python, java, pyspark, kedro, kedro_datasets) **and every physical input layout** | a comparison rule and a test **per dimension, eight of eight** | S9 (1080) |

#### C · Execution and policy contracts *(ordered so no task precedes its inputs [R18])*

| Task | Deliverable | Gate (S0.5) | Gate (deferred) |
|---|---|---|---|
| **C-C1** | `AuthorizedCompilationV2` · `MaterializationContractV2` · `FeatureGroupPlanV2` · `CompilationIdentityV2`; a V2 renderer entry point | V1 types, signatures and canonical bytes **unchanged** | S6 |
| **C-C2** | **`PlannedFormulaExecutionIRV2` FIRST**, then read set → leakage → Gate 2 → authorization **wrapping those exact planned IRs**; never rebuilt after | the ordering is expressed in types (authorization cannot be constructed without planned IRs) | S6: authorization names the IR hashes the renderer consumes |
| **C-C6a** | **`BoundFormulaRevisionV2` and `ExecutableOutputPolicyV2` TYPES** *(moved here so C-B3 has them)* | both types frozen | S5 (1076) |
| **C-C6** | `formula-v2/physical-types@1` defining **`SUM(amount × booking_rate)` completely** — precision, scale, intermediate precision, rounding site, SUM growth, overflow, float refusal, nullability. `empty_window` already decides V1 nullability — mirror it | every row of the truth table asserted; V1 and V2 agree where they overlap | S5 |
| **C-C7** | `derive_policy_occurrences(formula, bound_input_set)` replacing `required_policy_kinds()` wiring | a country filter needs no reversal policy; an operand whose C1 facts are not `monetary`/`per_row` needs no currency occurrence | S4 (1075) |
| **C-C8** | Realization identity — family key · **unique revision id PLUS a separate `executable_content_hash`** so a source and an LLM proposal with identical semantics stay distinct · CAS pointer · retained conflict findings · **and `realizes_occurrences`, which exists nowhere and is created here** [R18]. **Pilot realizations are TIMELESS; validity-interval detection is NOT BUILT** — `POLICY_INTERVAL_UNSUPPORTED` was never implemented, so there is nothing to remove | two proposals with identical semantics keep separate revisions; `realizes_occurrences` exists on the type | S4 (1075). **The withdrawal is scoped to POLICY VALIDITY INTERVALS — it does not touch the load-bearing mid-window FX RATE test (3.65→3.70 ⇒ 73.50)** [R18] |
| **C-C9** | LLM admissibility (invariant 16); policy literals evidence-linked where available, else `LLM_PROPOSED` and **not called evidence-validated** | the four-way table is a total function over its inputs | S4 |
| **C-C10** | Typed operator-subgraph requirements — **FX**: as-of join · duplicate-rate gate · missing-rate gate · **base-currency identity-rate bypass** · optional inversion · decimal conversion · connected path. **LINKED_REVERSAL**: as-of population · linkage · ambiguity gate · survivor operator · connected path. Plus topology-derived coverage | each requirement is a named constant with a test over a **stubbed** graph | S7: deleting the duplicate-rate gate refuses with `realizes_occurrences` intact |
| **C-C3** | `FullReadSetLeakageGateV2` over formula operands and filters · policy reads · join keys · reversal and FX inputs · temporal and availability columns · spine reads. **Claim stated narrowly** (invariant 18) | the gate runs over a **stubbed realization fixture** | S6: a mutation replacing a policy status/direction column with the target ref refuses |
| **C-C4** | Gate-2 union extended with **policy reads** — `SpineSpec.read_set` is already unioned. **The gate is restated [R18]: Gate 2's read set is DERIVED from the IRs and spine, so there is no separate list to "remove the FX table from"** | — | S6: **an FX rate column the supplied roles cannot read refuses `READ_SCOPE_INSUFFICIENT`; an ungoverned FX column refuses by its own code** |
| **C-C5** | Per-read temporal semantics; the declared T+N promise a **separate** field | the two are separate fields on the type | S6: post-cutoff FX and post-cutoff reversal refuse |
| **C-C11** | The S4 policy producer's LLM seam — registered output schema · bounded input contract · audited call · replay/idempotency · egress fields. **The existing seam fails closed on unknown fields** | the schema is registered and an unknown field fails closed | S4 |

#### D · Verification, publication and access

| Task | Deliverable | Gate (S0.5) | Gate (deferred) |
|---|---|---|---|
| **C-D1** | `VerificationExecutionIdentityV1` — **no publication attestation**; decide `__verification_execution_hash` vs a versioned alias, changing identity, system columns, run parameters and persistence **together** | constructible with **no attestation**; V1's hash still requires one | S9 (1080) |
| **C-D2** | The versioned check set — result schema per check · non-null columns · feature-null rules from the output policy · spine completeness and uniqueness · join orphan/amplification · **which of the eight external requirements each may satisfy** · the check-set hash | the mapping is explicit and total | S9: a keys/types check does not satisfy `JOIN_CONNECTIVITY` |
| **C-D3** | `VerifiedOutputRevisionV1` — check-set hash · validator versions · pinned executable policy hashes · **`input_observation_strength`** · **retention/expiry state [R18 — dropped in revision 17]**, reusing **`runtime/blob_gc`'s `marked_orphan → quarantined → swept`** discipline rather than inventing one | the type carries all five | S9: a policy changed after verification makes the pass stale; an expired staged output is swept |
| **C-D4** | Artifact file manifest **extending `GENERATED.lock`** — ordered path · SHA-256 · byte length · media type · **the content store the bytes actually live in [R18]** (DB blob, shared volume or object store — decided here, migration 1078) · byte verification before retrieval **and** before execution | the manifest type and the store choice are recorded | S7 (1078): a mismatched digest is neither served nor executed |
| **C-D5** | `OperatorExecutionProofV1` pinning signature and version · compiler and renderer versions · physical-type policy · topology version · gold corpus hash · **the exact generated gold-project hash** · check-set version · the eight runtime versions | the type refuses construction with any field absent | S8 (1079): changed bytes invalidate a proof even with no version bump |
| **C-D6** | Environment scoping on **`group_binding`** and **`feature_active_revision`** — **migration 1083 reserved [R18]**: ADD COLUMN + backfill + constraint swap. **Also reconcile the flat V1/V2 `logical_group_name` namespace**, which has no language discriminator | the target schema and backfill are written | S10 (1083) |
| **C-D7** | Group-name allocator. **[R18] Either it IS `hive_identifier` extended — one normalizer for feature columns and group names — or it is scoped to group names with a stated proof of non-collision with names already bound.** `hive_identifier` deliberately refuses to truncate | the choice is stated and the ≤128 bound proved | S6 |
| **C-D8** | Active-revision **CAS** — `publish_sandbox(verified_output_revision_id, expected_active_revision_id)`; 1055's trigger stops concurrent double-wins, not a stale publish | the signature carries both | S10 (1081): an older verified output over a newer active revision refuses |
| **C-D9** | **Three evaluator INTERFACES plus their refusal vocabulary**, and **a code-by-code table of all sixteen activation blockers: CARRIED or DROPPED, with a reason** — `PERSONAL_DATA_POLICY_REQUIRED` is **carried** [R18]. **The implementations ship at S8/S9/S10, not here** | a typed-contract test over **stub records**; the sixteen-row table is complete | S8/S9/S10: each evaluator's real decisions |
| **C-D10** | `BuildDeclarationV1` frozen — cadence · availability promise · spine · environment · parameter bindings · base name; **one declaration per derived group, or an explicit one-grain restriction**. Reconcile with the **deliberate** request-identity split and its named test | the type is frozen and the reconciliation stated | S11 (1082): the existing test still passes |
| **C-D11** | **Typed `FeaturePlanningRequestV1` persistence and reader. Premise corrected [R18]:** `planning_request_hash` is **load-bearing** (the 409 `DECISION_RECORD_TAMPERED` gate) and the `asdict()` copy **is served** in `evidence`; **only `parameter_values`' `repr` is dead** | the typed request round-trips | S2 (1073): legacy rows refuse by name rather than being reconstructed from `repr`; **the 409 tamper gate still fires** |
| **C-D12** | **[R18 — re-scoped, not new]** A target **MODE** axis independent of who declared it, **migrating the existing `exploring` provenance value onto it** — one field, one owner. An explicit no-target declaration already ships | the mode axis exists and `exploring` maps onto it | S1 (1072): legacy `exploring` rows map with no loss |
| **C-D13** | **[R18]** A **per-attempt staging location** — the existing root is generation-scoped, so repeated verifications would collide and S10's "exact staging output" would be ambiguous. Thread the attempt into `VerificationExecutionIdentityV1`, or make a second concurrent attempt refuse under a lease | the attempt component is in the identity | S9 (1080): two attempts do not share a path |

> **Acceptance (S0.5):** every contract has a frozen schema with a pinned hash and a test that fails
> if a field is added without updating it; **every S0.5 gate is satisfiable with no migration
> applied**; `make test`, `make lint`, `make typecheck` green and, from `frontend/`,
> `npm run typecheck`, `npm test`, `npx oxlint` green; V1 hashes, canonical bytes, signatures and
> contract types untouched.

### S1 — immutable selection + target-reading revision *(1072)*
> **Acceptance:** a selection exists with no definition and no executable revision; a re-read creates
> a new revision and the old stays readable; a `human_confirmed` reading is never silently
> overwritten; legacy `exploring` rows map onto the mode axis.

### S2 — deterministic V2 authoring + provisional output intent *(1073)*
The generalized work item · output naming · the deterministic producer emitting
`ReviewedBlueprintBypassV2` · V2 resolution and admission · typed planning-request persistence ·
`AuthoredOutputIntentV2` only. `FeatureDefinitionV1` created and linked.
> **Acceptance:** a candidate outside the shadow top 12 authors and admits; a reviewed recipe
> produces a durable replayable result **with no provider call and no `CRITIC_COMPLETED` event**; a
> non-empty `currency_conversion_ref` yields an **intent**; the 409 tamper gate still fires; V1
> formula hashes pinned.

### S3 — generation inventory + bound input set *(1074)*
> **Acceptance:** binding is unreachable without an inventory; the bound set is addressable
> independently of any policy.

### S4 — policy occurrences + realizations *(1075)*
> **Acceptance:** an unresolvable reference refuses by name; an operand whose C1 facts are not
> `monetary`/`per_row` needs no currency occurrence; conflicting source and LLM evidence resolves to
> source with the conflict retained; no V2 path writes through the mutable upsert.

### S5 — bound formula + executable output policy *(1076)*
> **Acceptance:** the refusal compares **only fields the intent records**; a compiler version bump
> leaves the bound-formula hash unchanged.

### S6 — planned IR → read set → leakage → Gate 2 → contracts → groups *(1077)*
> **Acceptance:** the IR is not rebuilt after authorization; a policy column swapped for the target
> ref refuses; an FX column the roles cannot read refuses `READ_SCOPE_INSUFFICIENT`; membership is
> queryable; V1 bytes and the single-contract path byte-identical.

### S7 — render, seal, persist and serve *(internal, 1078)*
> **Acceptance:** deleting the FX duplicate-rate gate refuses with `realizes_occurrences` intact; a
> mismatched digest is neither served nor executed; artifact bytes survive a worker restart.

### S8 — development gold proof, capability proofs, `evaluate_generate` *(1079)*
Mutations: wrong debit mapping · missing status filter · reversal neutralization removed ·
post-cutoff FX accepted · quote inversion reversed · conversion moved after aggregation ·
duplicate-rate-gate deletion. **Generation is authorized here; it has no user surface until S11.**
> **Acceptance:** every case and mutation behaves; changed bytes invalidate a proof without a version
> bump; capability is `renderer-dispatchable ∧ execution-proved`.

### S9 — on-demand sandbox verification *(1080)*
> **Acceptance:** verification executes with **no publication capability present**; two attempts do
> not share a staging path; a changed input flips a pass to stale and names it; observation strength
> is never `PINNED` without enforced reads.

### S10 — exact-output CAS publication *(1081, 1083)*
> **Acceptance:** an older verified output over a newer active revision refuses; a partial group never
> becomes visible; environment keying is in place.

### S11 — complete API and UI workflow *(1082)*
Endpoints incl. **an explicit generate endpoint** · the derived group split before verification ·
policy provenance with `LLM_PROPOSED` visible · **goal, target, stage and output at the top of the
workspace**.
> **Acceptance:** generation is reachable by a user for the first time; no path reaches execution
> without an explicit click; results sit above intake.

### S12 — corpus generation *(generation only)*
Under the target **mode** axis and a declared default `BuildDeclarationV1` set.
> **Acceptance:** a coverage table with every refusal named; **the batch triggers no execution**.

### S13 — build free-form V2 authoring, then expand
**There is no free-form V2 path in production — S13 BUILDS it**, then gates it live.
> **Acceptance:** a free-form V2 run reaches admission through the **v2** tool seam; the advertised
> set is `renderer-dispatchable ∩ execution-proved`.

## 2. Carried forward

**Parallel, gated at merge:** behavioural frontend↔backend contract tests · hide the retired "Write
definitions" control · recognition correctness in full · ruff ratchet (79 repo-wide, 35 in `src/`).

**Narrow the leakage claim** — carried since revision 2 without its context; invariant 18 now states
the narrow claim independently.

**Deferred:** cross-catalog V2 execution · multi-environment support · Iceberg-grade snapshots ·
effective-dated policy history · operations beyond the pilot family.

## 3. Sequencing

```
S0 semantics + numerics + PILOT DATA REALITY ─► S0.5 contracts ⟨A · B · C · D, two gates each⟩
  ─► S1 selection ─► S2 authoring ─► S3 inventory + bound inputs ─► S4 occurrences + realizations
  ─► S5 bound formula + executable output ─► S6 planned IR → leakage → Gate 2 → contracts → groups
  ─► S7 render/persist/serve ⟨internal⟩ ─► S8 gold proof + evaluate_generate
  ─► S9 verify ─► S10 CAS publish ─► S11 UI ⟨first user-reachable generation⟩
  ─► S12 corpus ─► S13 build free-form + expand
```

**What changed in revision 18.** The deadlock is gone: S0.5's gates were behavioural over tables its
own ledger reserved for later stages, so every task now has a contract gate satisfiable with **no
migration** and a deferred behavioural gate carried by the owning stage. S0 gains **pilot data
reality** — the pilot has no currency operand, no physical relation and an optional reversal link, so
it could not have produced its own numbers under any earlier revision. The evaluators freeze as
interfaces at S0.5 and ship at S8/S9/S10, and C-D9 must account for all sixteen activation codes
rather than three — revision 17 silently dropped a **data-use licence** gate. Artifact bytes get a
content store, staged outputs get retention through the **existing** `blob_gc` discipline, and
verification attempts get their own staging path. `exploring` and `blob_gc` are the twelfth and
thirteenth things this plan proposed to build that already exist. And **§0.5 was renamed §0.6**
because a section number collided with a stage name.

**No duration estimate.** Fourteen revisions have now carried one that a review invalidated.

# V2 as the feature language, on shared execution

**Date:** 2026-08-16 · **Revision 19** — an eleventh review, verified in parallel with an
adversarial refutation pass. **It corrected the plan on nine blockers and eight majors; the
verification corrected the REVIEW on three.** Revision 18's ten-lens audit (ordering cycles · acceptance
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
- **Logical refs MUST carry a `source::` prefix — revision 18 said the opposite [R19].**
  `schema.py`: *"logical_ref … must be 'source::schema.table[.column]'"*, enforced for **V1 and V2
  alike** through one `_split_logical_ref`. The rule is **"replace `authored::` with the selected
  real catalog source"**, never "strip the prefix". Revision 18 inferred "no prefix" from a grep of
  one module — the same over-generalisation that produced the `repr()` error.
- **The recipe does not structurally declare `debit` [R19].** It appears only in the recipe NAME and
  prose; blueprint derivation builds the expression with **no semantic selection**. The reviewed
  exemplar carries direction as a **physical `filter` predicate** — the thing the plan intends to
  replace — so a deterministic author could only infer `debit`.
- **Unit and currency are unreadable through the V2 authoring path [R19].** `authoring_v2` reads
  operand facts via `read_operational_value`, and `field_policies` classifies unit/currency so the
  reader returns **`not_operational` even for source-attested or human-confirmed decisions**;
  `_fact_text` then yields empty strings. **`CURRENCY_CONVERSION_UNDECLARED` can never fire through
  today's authoring path** — and **the failure is SILENT**: `not_operational` is not in
  `_C1_HARD_FAIL_STATUSES` (`fork · hash_mismatch · projection_unavailable`), so nothing is recorded
  and the empty facts reach `resolve_output_v2` **as if the operand were non-monetary**. **Both** V2
  fact paths fail identically — the frozen snapshot reader hardcodes
  `status = "not_operational"  # hint by policy — never governed` — and **no test drives the refusal
  through a C1 read**; the one test that reaches it injects facts by hand and says so. A usable seam
  exists: **`read_verified_decision_value`** re-resolves the value from selected evidence and
  verifies its hash, with **no governed-field restriction**.
- **No materialization module consumes any V2 formula type [R19]** — zero references to
  `TypedFormulaProposalV2` / `AuthorityRefsV2` / `FormulaBodyV2` under `materialize/`, and the V1 IR
  has **no closed operator vocabulary**, only `ExpressionExecutionIR` fields.
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
**1071**, prefixes collide seven times · the only repo-wide green gate is `make test`
(lint/typecheck/format are per-file ratchets — 79 / 469 / 1280 red respectively) and,
from `frontend/`, `npm run typecheck` (`tsc -b`), `npm test`, `npx oxlint`.

**Direction is still name-inferred in 17 recipes — measured, and inert today [R19.7].**
Across the full 317-recipe registry, 17 carry a `direction` operand and declare no
`row_selections`, including four debit/credit pairs distinguished only by their names
(`posted_debit_transaction_count` / `posted_credit_transaction_count`, `refund_*`,
`fan_in_*` / `fan_out_*`). **Every one is `FORMULA_BLOCKED`**, so none can reach authoring and
nothing can act on the ambiguity today — but each must be declared before it becomes authorable.
The validation added here is what surfaced them, and it also caught `posted_credit_amount`
selecting by direction while declaring **no direction policy at all**.

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
| 1073 | feature definition · authoring work item **+ compatibility reader, NOT an in-place backfill: the existing table is write-once by trigger with `UPDATE`/`DELETE` revoked from the app role [R19]** · typed planning request | S2 |
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

> ## S0 DECISIONS — TAKEN 2026-08-16 by the product owner. The decision half of S0 is CLOSED.
>
> **D1 · The first executable feature is `posted_debit_amount_30d`.** Window
> **`(2026-05-31, 2026-06-30]`**, expected **`ACC1 = 300.00 AED`**; the 2026-05-20 row is excluded,
> as are failed, credit and reversed rows. **The frozen V2 90-day exemplar is NOT modified** — it
> stays as historical V2. A **new V3 30-day exemplar** is created and approved. 90 days lands later
> as a second case with `ACC1 = 330.00`.
>
> **D2 · Kind-prefixed policy references** — `eligible_status:foundation-posted-events` ·
> `direction_sign:foundation-signed-by-indicator` · `reversal_correction:foundation-flag-or-code` ·
> later `currency_conversion:foundation-base-currency`. This is what lets the compiler catch a
> direction policy assigned to the status field. The `policy:*` V2 exemplar stays frozen as
> historical V2; the active reviewed **V3** exemplar carries the corrected namespace and a **new
> reviewed hash**.
>
> **D3 · FX execution is DEFERRED — sequencing, not scope reduction.** The first execution runs on an
> **explicitly fixed-AED input relation or source binding**. It must **never** take the existing
> mixed-currency population and quietly filter ACC2 out. Required behaviour:
>
> | Source | Outcome |
> |---|---|
> | fixed-AED | **executes** |
> | per-row or mixed currency, no executable FX | **refuses `CURRENCY_CONVERSION_UNDECLARED`** |
> | any | **never silently omits USD rows or accounts** |
>
> The `ACC2 = 93.50` mixed-currency gold case is **kept unchanged as the next FX pilot**. Deferred
> together: C-A7's target-currency work · C-C6's FX arithmetic · C-C10's FX graph · the related S8
> mutations.
>
> **This makes C-A3c load-bearing rather than optional.** The refusal D3 requires **cannot fire
> today**: unit and currency return `not_operational` and degrade silently to non-monetary, which is
> precisely the "silently omit" failure D3 forbids. **D3 is unimplementable until C-A3c lands.**
>
> **D4 · The spine is an explicit account-population snapshot** — accounts active as of
> **2026-06-30**, manually bound, containing at least **ACC1** (returns 300.00) and **ACC_ZERO**
> (returns 0.00). Execution **starts from the population**, left-joins transactions, and coalesces an
> empty window to **zero**. **A transaction belonging to an unknown account must not invent a
> population row.**
>
> **Settled by the fixture, confirmed:** amounts are positive magnitudes with a separate `D`/`C`
> indicator, and output debit magnitude is **positive**. The reversal mode is **linked**
> (`r06.reversal_of = r05`), so **`_CORRECTION_LINK` becomes `required=True`** for this recipe and
> `eligibility_store`'s flag/code shape cannot serve it.
>
> **Taken as the cheap option, no row being within a day of an edge:** timezone **UTC** and boundary
> **`(start, end]`** — the existing derivation constants, so no blueprint moves and
> `EXPECTED_OUTCOMES` is not re-pinned.
>
> **Still open (engineering half of S0):** the fixed-AED relation and the population snapshot as real
> bound relations; the currency operand *(deferred with FX, but the REFUSAL path is not)*; the
> migration ledger.


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
inversion · rate rounding — **and the amount-sign convention [R19]**: positive magnitude plus a
`D`/`C` indicator, or an already-signed amount; and whether the output debit magnitude is positive
or negative.

**Conversion happens BEFORE aggregation — settled, not open [R19].** C-C6 defines
`SUM(amount × booking_rate)` and S8 requires a mutation moving conversion after aggregation to
**fail**. With per-row currencies and per-booking rates, "after" is only meaningful if the
computation first groups by currency and separately defines which period-level rate converts each
subtotal — a different feature.

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
> C1 facts for `txn_amt` are read and recorded** — **the refusal test itself moves to C-A3c [R19]**, since it needs a reader that does not exist during a human-decision-only stage, and a green test asserting exactly this already exists over a hand-built literal (**instance 14**); base-
> currency rows resolve at rate 1 without tripping the missing-rate gate; refs carry the **selected real catalog source** in place of `authored::` — the `source::`
> prefix is mandatory and is never stripped **[R19]**; expected rows stored and hashed with availability
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
| **C-A1** | **ALREADY BUILT — verify, do not construct [R19.2, instance 15].** `tests/featuregen/formula/test_canonical_v2.py` already asserts byte-equality of `canonical_json_v2(proposal)` against the committed `canonical_json` for all 25 ok fixtures, **and** that the pinned hash is `sha256` of the pinned TEXT (so a quietly-refreshed hash over drifted bytes fails loudly). `test_the_projection_is_field_exhaustive` documents the exact hazard: *"a field added later is hash-bearing automatically"* — it is the alarm that would have fired on revision 18's plan to add a field to `AggregateExpressionV2`. C-A1 is therefore **"these stay green after V3 lands"**, not a new test | `uv run pytest tests/featuregen/formula/test_canonical_v2.py tests/featuregen/formula/test_canonical.py -q` → **24 passed today**, and still passes after C-A2/C-A3 | — |
| **C-A2** | **BUILT [R19.6]** — `schema_v3.py` · `canonical_v3.py` · `parse_v3.py` · `proposal_v3.schema.json` · dispatch extended in `parse_versioned`. **V3 types only where the shape differs**: `AggregateExpressionV3` + the four bodies + `TypedFormulaProposalV3`; `WindowPolicyV2`, `AuthorityRefsV2` and the v1 leaves are **imported unchanged**, because duplicating an identical shape creates two definitions of one concept. `_plain_v2` is reused as the canonical walker — it carries no v2 grammar, and a second copy would drift on exactly the leaves v3 shares. The JSON schema is **generated from v2's with a proved three-item delta** (the version pin, `row_selections`, `semanticRowSelection`) | v3 parses through dispatch; **25 v2 gold hashes recomputed and unchanged**; v3 projection field-exhaustive | **recipe expectations DONE** (C-A3b) and **egress DONE [R19.7]** — the egress boundary found itself: 38 tests failed `RecipeEgressViolation: expressions[0] keys…` the moment the expectation gained a field, which is the closed boundary working. `row_selections` now egresses with real validation (exact keys, bounded role, `kind`/`semantic_value` checked against **`schema_v3`'s vocabulary imported, not copied** — a second token set at the boundary is how a boundary widens while the schema stays narrow), and the pinned key count moved 12 → 13 deliberately. **critic DONE** (type gate widened to three PARSED types, still closed) · **result candidate union DONE** (`TypedFormulaProposalV2 | V3`, hash **dispatches on version** — hashing v3 under v2's projection would mint an identity from a walker that never saw `row_selections`) · **replay restoration ALREADY CORRECT [R19.8]** — it parses through `parse_versioned`, never a pinned `parse_proposal_v2`, so extending the dispatch covered it; a test now pins that. **NOT BUILT, deliberately: author turns + frozen configuration.** Both pin what an LLM authoring run was *conducted under*, and **nothing authors v3** — building them now is the *"pin with one producer and no consumer"* this codebase keeps rediscovering. They are driven by S2's deterministic producer and S13's free-form path, whichever lands first |
| **C-A3** | **BUILT [R19.6]** — `SemanticRowSelectionV1(kind, role, semantic_value)` on **`AggregateExpressionV3`**, a **tuple unique by `(kind, role)`** over a closed matrix (`transaction_direction → debit\|credit`, `eligibility → eligible`). **No `policy_ref`** (rule 14). A closed token set is what makes *"a physical literal refuses"* decidable at all — nothing at the schema layer can look at `D` and know it is physical, but it can know `D ∉ {debit, credit}`. A selection **requires** its matching `AuthorityRefsV2` field, and a selection beside a **filter** refuses `SELECTION_FILTER_CONFLICT` — the schema cannot prove which column a filter touches, so it refuses rather than risk applying direction twice | `test_schema_v3.py` **15 passed**; formula package **589 passed** | — |
| **C-A3b** | **BUILT [R19.7]** — `RecipeDefinitionV2.row_selections`, declared on `posted_debit_amount` (`debit`) and `posted_credit_amount` (`credit`), carried through `ExpressionRoleExpectationV2` / `BoundExpressionExpectationV2` and emitted by blueprint derivation. **Option (a) per the product owner**: canonicalization stays field-exhaustive, **all 317 recipe hashes move once**, the 996 prior review events stay as immutable history, and the sandbox is re-seeded **manually** with fresh `dev-fixture:<role>` approvals at the new hashes. Rationale recorded: preserving old hashes would imply reviewers had assessed row-selection meaning **when that concept did not exist**. `()` means *"this recipe declares no structural row selection"* — a positive statement, pinned by test, never "not migrated yet". **D2's kind-prefixed refs are what make the check possible**: policy refs are flat strings, and the prefix is the only thing that makes *"this ref governs direction"* machine-checkable | `test_recipe_row_selections_ca3b.py` **10 passed**; the seeder already satisfies checks 5 and 8 unchanged (plans only unsigned roles at the current revision → second run writes zero) | **operator: run the guarded re-seed** `--confirm-database <db> --apply`, development sandbox only |
| **C-A3c** | **COMPLETE — live AND frozen [R19.5]**. `formula/measure_facts.py` + `authoring_v2` (live) + **`feature_metadata_snapshot._build_item`** (sealed) + `recipe_authoring.formula_facts_v2` (frozen read), sharing one `project_measure_read` rule so the two readers cannot drift. **Option (b) taken by the product owner**: no new item kind, `FIELD_POLICY_VERSION` unchanged, `authority` stays `"hint"`, `status` becomes truthful — so a verified measure seals **`authority="hint"` + `status="resolved"`**, a combination the governed branch cannot produce. **One-time cost, accepted: every stored snapshot containing a measure item now reads `SNAPSHOT_ITEM_DRIFT` → `SNAPSHOT_STALE_REGENERATE`.** Rejected alternative: an additive item kind would have left old snapshots reading `current` while still asserting `not_operational`, and those could enter V3 authoring and bypass the protection being added. **The item table is WRITE-ONCE by trigger**, so regeneration can only mint a new revision — the "never rewrite old records" requirement is enforced by the database | `test_measure_snapshot_ca3c.py` **8 passed**, `test_measure_facts.py` **14 passed** | **MET in S4** — `policy_occurrence_measure_read` (1075) + `record_occurrence_set`'s REQUIRED `measure_reads`, refusing `OccurrenceProvenanceMissing` without them. `absent` is stored as a positive statement; reads are keyed per DERIVATION so a moved catalog does not restate an earlier one; provenance stays OUTSIDE the occurrence's identity hash (the house rule), so C-C7's committed identity is untouched |
| **C-A4** | Schema-3 coherence: a direction selection **requires** `AuthorityRefsV2.direction_policy_ref` | **MET [R19.10]** — the rule shipped with C-A3b (`_REQUIRED_POLICY_REF` + the check in `_check_selections`); what was MISSING was the coverage that makes it load-bearing. The one existing test passed `authority_refs=None`, which a check asking only *"are there any refs at all"* would also satisfy. Added: the **per-KIND** case (refs present, wrong one), the **ELIGIBILITY half** (never covered, so a wrong map entry for that kind was invisible), the reachable **empty-ref** shape, and that the map is **TOTAL** over `SelectionKind` — a kind with no entry would `KeyError` inside the validator instead of refusing by name | — |
| **C-A5** | **BUILT [R19.9]** — `CriticExecutedV2 \| ReviewedBlueprintBypassV2`, `AuthoringAxesV2` carrying `review`, `shared_axes()` projecting **for the fold only**. A bypass reports **`critic_status=None`**, never `"clean"`. **Adversarially reviewed before commit; five defects found and fixed** — (1) neutrality was *asserted*: one bypass value was measured folding two different proposals RESOLVED, so a bypass now requires and must match `reviewed_expectation_hash`; (2) a bypass could carry `critic_findings_hash` — findings from a critic that did not run; (3) `CriticExecutedV2.findings_hash` was never read, so the evidence existed twice and could disagree — now **derived**; (4) **a bypass-authored run was permanently unreplayable**, gutting the feature's own use case — replay now round-trips `review`; (5) the frozen `disposition_policy_hash` did not cover `shared_axes`, so a fail-open `else` would not have moved the seal. Plus one I found: replay's restore guard was V2-only and would reject a v3 trace | `test_review_outcome_ca5.py` **23 passed** |
| **C-A6** | `REVIEW_BYPASSED` transition; **bump disposition, orchestrator and replay protocol versions**; old readers still read old traces | **MET [R19.11]** — `formula/authoring_versions.py` + dispatcher in `replay_authoring_v2` (`91f87749`), `test_authoring_versions_ca6.py` **22 passed**, all EIGHT acceptance criteria. **Product-owner policy (2026-08-16): backward-READABLE, not cross-version RESUMABLE.** Three constants at 2; adapter chosen from the STORED manifest (current→resume · legacy+terminal→READ-ONLY replay · legacy+incomplete→`LEGACY_AUTHORING_RUN_RESTART_REQUIRED` · unknown/partial→reconciliation). `REVIEW_BYPASSED` added as the **alternative** to `CRITIC_COMPLETED`, never its successor; `formula_schema` now RECORDED so a v3 run records 3. **Two things the code forced**: `open_authoring_run` raises on any manifest difference, so the dispatcher had to move AHEAD of it and the legacy path does not re-open the run at all; and `formula_authoring_run` is **write-once by trigger**, so the legacy fixture patches the constants and drives a real run — a better fixture, and the write-once property now has its own test because the whole policy rests on it | S2: an existing recorded trace replays unchanged |
| **C-A7** | **CREATE `AuthoredOutputIntentV2`, DERIVED from the proposal/recipe output expectation** — never a second declaration that can disagree with `expected_output`. Carries unit, additivity, conversion-required, declared ref **and desired target currency and numeric shape**. **Plus a V3 authoring result** whose terminal artifact is *validated proposal + review outcome + provisional intent*, with an **`OUTPUT_INTENT_CAPTURED`** stage and **no `OUTPUT_POLICY_RESOLVED` until S5**. *(Verification note [R19]: `AuthoringResultV2` does have a legal no-policy shape — `NEEDS_REVIEW` with an unresolved `output_status` — so this is chosen for clarity, not forced.)* | **MET [R19.11]** — `formula/output_intent_v2.py` (`fc0d2db6`), `test_output_intent_v2.py` **20 passed**. The deriver takes the proposal and a hash **and nothing else** (asserted on its signature). **Advisory and structural kept apart**: `unit`/`target_currency` from `expected_output` and may be absent — `authored_expectation_present` records that rather than letting `""` collapse "expected nothing" into "expected an empty currency"; `conversion_required`/`declared_conversion_ref`/`numeric_shape` are structural. **No additivity is invented** (`ExpectedOutput` carries none). The two readings of the conversion fact **cannot disagree**, which is what stops the type becoming the second declaration it prevents. Two different conversions in one formula REFUSE. `OUTPUT_INTENT_CAPTURED` is legal after either review path and a prerequisite of nothing — a V3 run is terminal there | **S5**: the refusal compares only fields the intent records |
| **C-A8** | **Tool-seam repair**: `run_authoring_v2` gains a `tool_runner` parameter; **`run_authoring_v2_replay`'s `tool_runner=None` default removed or fail-closed**; the schema-3 matrix covers **tool behaviour** with prompt, turn schema, frozen configuration, replay restoration, candidate union, recipe egress, expectation schema, WORM trace | **MET [R19.11]** (`fc0d2db6`) — the defect was concrete: `author_formula`'s `tool_runner` defaults to **`run_tool`, the V1 set**, and `replay_authoring_v2` passed the kwarg conditionally (`**({} if tool_runner is None else …)`). Omitting it did not disable tools, it **quietly swapped them**. Now always passed; BOTH orchestrators refuse a missing runner (one failing closed and the other open would just move the defect to whichever entry point a caller used); `run_authoring_v2` gains the parameter it never had. The single production caller already supplied one, so `src/` behaviour is unchanged; **46 test call sites now STATE `run_tool`** instead of inheriting it | S13: a v3 proposal through the shared tools is not reported invalid-because-v1 |
| **C-A9** | **[R18]** The **reviewed-expectation re-pin is a gated human act** — S0's re-authoring changes the exemplar's canonical hash and therefore the pinned registry entry. Name the owner, the required reviewer roles and the `recipe_review_event` at the current `canonical_recipe_v2_hash` | **MET [R19.11]** — `overlay/upload/recipe_repin_policy.py` (`91f87749`), `test_recipe_repin_policy.py` **16 passed**. Accountable owner **`feature_generation_product_owner`**, execution owner **`formula_engineering`**, approving roles **`banking_sme` + `data_semantic_owner` + `formula_engineering`**, and **≥2 DISTINCT human identities** — roles are not people, and without that floor one individual holding two roles could approve their own re-pin. **`dev-fixture:*` never counts as human approval**: a fully-seeded sandbox covers all three roles and still reports UN-approved, and a test pins that this module's prefix matches `scripts/seed_dev_recipe_reviews`'s, since two spellings would mean a sandbox identity that looks human to the gate. Both the `canonical_recipe_v2_hash` and the re-authored expectation hash must be referenced | S2: the registry entry matches the re-authored exemplar |

#### B · Identity graph

| Task | Deliverable | Gate (S0.5) | Gate (deferred) |
|---|---|---|---|
| **C-B1** | **MET [R19.12] — `overlay/upload/selection_revisions.py` (`662f2f5f`), `test_selection_revisions.py` 30 passed.** Discriminated union: `ExplorationTargetV1` has **no ref/type/horizon fields to set**, stronger than nulling (a nulled column still admits a value). **Option taken: store ONLY `target_logical_ref`**, DERIVING `catalog_source` — one fact beats two, because a check can be skipped and a derivation cannot; two catalogs with the same `public.t.c` now hash differently. Superseding a human-origin reading with an exploration declaration **requires naming who acknowledged the loss**; changing WHICH target needs none. ORIGINAL: **`TargetReadingRevisionV1`, append-only and a DISCRIMINATED UNION [R19]** — `PREDICTION` (ref/type/horizon required) \| `EXPLORATION` (those fields forbidden; leakage result `NOT_APPLICABLE_EXPLORATION`). Since a canonical ref already contains its source, **either store only `target_logical_ref` or enforce that its parsed source equals `catalog_source`** — never two facts that can disagree. Carries the existing closed provenance vocabulary `human_confirmed \| user_typed \| exploring` and a canonical content hash. Today `record_target_reading` UPDATEs in place with no provenance guard and drops `catalog_source` | the type round-trips; two catalogs with the same `public.t.c` are distinguishable | S1 (1072): a second reading creates a **new revision**; a `human_confirmed` reading is never silently overwritten |
| **C-B2** | **MET [R19.12]** (`662f2f5f`) — all five pins required; constructible with **no definition** (asserted: no field name contains "definition"). ORIGINAL: **`FeatureSelectionRevisionV1`**, immutable, referencing an exact target reading and pinning **which served option was selected — `considered_revision_id` · `option_id` · `decision_id` · `planning_request_hash` · `binding_plan_hash` [R19]**. *(Verification note: migration 1063 records every option SERVED, not a selection, so the selection record is genuinely new — it pins 1063's identity rather than inventing one.)* Under `BuildSetRevisionV1` (C-B5b). **`FeatureDefinitionV1` is created or resolved at authoring** with an append-only selection→definition link | a selection is constructible **with no definition** | S1/S2 (1072/1073): the link is append-only |
| **C-B3** | **MET [R19.12]** (`662f2f5f`) — every field is `str`, constructible from hashes alone. ORIGINAL: **`ExecutableFeatureRevisionV2`** — **defined here over opaque content hashes** for the bound formula and executable output policy, whose *types* are also frozen here (C-C6a) and whose *instances* S5 produces **[R18 — revision 17 froze it over types S5 delivered]** | constructible from hashes alone | S6 (1077) |
| **C-B4** | **MET [R19.12]** (`662f2f5f`) — the first durable group→member map; **refuses when member count disagrees with the compilation identity's IR count**, because the smaller number is the one that gets published. ORIGINAL: **`DerivedGroupRevisionV2`** — members · contract · plan · `CompilationIdentityV2`. **The first durable group→member map**: nothing maps a group to its members today | the type carries membership and a group-wide identity | S6 (1077): membership is queryable without replaying a request |
| **C-B5** | **MET [R19.12]** (`662f2f5f`) — binds **every** member selection and carries the screened IR hashes; **screened and Gate-2-authorized must cover the same set**, or the group ships a feature nobody screened while the envelope still reads authorized. ORIGINAL: **`GenerationAuthorizationRevisionV2`** — derived group · **ALL member selection revisions (a group has many features; revision 18 named one) [R19]** · exact target binding/revision · leakage-policy version · verdict · **the planned IR hashes that verdict screened** · Gate-2 token | the envelope binds every member | S6 (1077): an artifact authorized for one target cannot be reused for another |
| **C-B5b** | **MET [R19.12]** (`662f2f5f`) — ORDERED selections (a set would discard the order a person chose in), one declaration on the BUILD SET, and `refuse_multi_grain` refuses two grains. ORIGINAL: **[R19] `BuildSetRevisionV1` — the missing root.** The UI requires a build-set/child-group hierarchy and none exists. It carries **ordered `FeatureSelectionRevision` ids · the exact `TargetReadingRevision` · one `BuildDeclarationV1`**. **One declaration per BUILD SET (not per derived group, which does not exist until S6), and multiple grains REFUSE**; derived access/sensitivity differences may still split it into several groups | the graph `BuildSet → selections → definitions`, and `DerivedGroup → BuildSet + ordered executable revisions`, is expressible with no forward reference | S11 (1082): a two-grain build set refuses |
| **C-B6** | **MET [R19.12] — `overlay/upload/inventory_revisions.py` (`662f2f5f`), `test_inventory_revisions.py` 49 passed.** Policy-free by construction (asserted: no field name contains "polic"). ORIGINAL: **`BoundInputSetRevisionV2`**, before occurrence derivation | constructible independently of any policy | S3 (1074) |
| **C-B7** | **MET [R19.12]** (`662f2f5f`) — the two gates pull opposite ways, which together say identity must be **narrower than the observation**. Stores the complete snapshot, hashes four things; reuses **`TableLayout.semantic_payload`**, which already excludes `location` for exactly this reason. Using an undeclared mapping REFUSES. ORIGINAL: **`GenerationInventoryObservationV1`, captured BEFORE binding** — `inventory` is required by `compile_feature_group` and `compile_ir`. **Identity covers `environment_id` · engine versions · the logical-schema mappings ACTUALLY USED · the physical layouts for the EXACT read set — never the whole observation [R19]**, which the inventory already separates from provenance; otherwise an unrelated table or a mere re-capture would invalidate a feature. Store the complete observation; hash only that subset. **`environment_id` enters `sandbox_execution_hash`** | an identical re-capture with a new observation id and capture time leaves identity unchanged; an unrelated table added to the inventory leaves it unchanged | S3 (1074): compilation is unreachable without an inventory |
| **C-B8** | **MET [R19.12]** (`662f2f5f`) — per-dimension over all **eight** runtimes, each with a rule AND a reason, driven by a parametrised test. spark/hive/metastore/pyspark **EXACT**; java/python/kedro/kedro_datasets tolerate a patch bump. `RUNTIME_DIMENSIONS` asserted **exhaustive over `EngineVersions`' fields**. ORIGINAL: **`VerificationInventoryObservationV1`** + an explicit compatibility algorithm over **all eight** runtime versions (hive, spark, metastore, python, java, pyspark, kedro, kedro_datasets) **and every physical input layout** | a comparison rule and a test **per dimension, eight of eight** | S9 (1080) |

#### C · Execution and policy contracts *(ordered so no task precedes its inputs [R18])*

| Task | Deliverable | Gate (S0.5) | Gate (deferred) |
|---|---|---|---|
| **C-C1** | **TYPES BUILT [R19.10] — `materialize/boundary_v2.py`** (`450ee7b5`): `FormulaExecutionIRV2` · `SelectedRowsV2` · `ir_hash_v2` · `PlannedFormulaExecutionIRV2` · `AuthorizedCompilationV2` · `MaterializationContractV2` · `FeatureGroupPlanV2` · `CompilationIdentityV2`. **`ExpressionExecutionIR`, `SpineSpec`, `PlannedFeature` and the contract's sub-declarations are REUSED, not re-declared** — they name no formula version, and a second copy would be a second answer to "what does this expression read". Exactly three parts are V2-shaped: the output policy (`currency` carries `"converted:<ref>"`, so a conversion is part of what a feature IS), `FinalOperationV2`, and C-A3b's semantic row selection. **Physical types became a POLICY ID** (`formula-v2/physical-types@1`), shape-checked so V1's ordinal cannot be smuggled in as `"1"`; C-C6 still owns what it MEANS. `realizes_occurrences` is deliberately **absent** — an empty field would claim a group realizes no policies when the truth is that realization does not exist (C-C8). **The V2 RENDERER ENTRY POINT is NOT built and is re-sequenced AFTER C-C10a**: `render_project` takes INJECTED nodes, and there is no V2 node producer, so the seam would be a door into a room with no floor | **MET** — V1 types, signatures and canonical bytes unchanged; V1 materialize suite 2135 passed. `_union_of`'s walk extracted to `_union_elements` + public `physical_read_set_of`; V1 DELEGATES (asserted structurally), so V1's coverage of join-endpoint-by-kind, join predicates and the empty-read-set refusal **is** the shared core's coverage | S6 |
| **C-C2** | **ORDERING BUILT [R19.10]** (`450ee7b5`). `PlannedFormulaExecutionIRV2.__post_init__` **re-derives** its read set through the shared walk and refuses a mismatch, so a plan claiming a narrower read set than its IR derives is unconstructible. `AuthorizedCompilationV2.planned` is typed on the planned IRs and its `__post_init__` refuses `authorized_refs` that fail to cover what the group reads. `build_compilation_identity_v2` takes **the TOKEN**, not a list of IRs, so both hash lists are paired one per feature by construction. **Leakage (C-C3) and the policy-read union (C-C4) are NOT yet in this chain** | **MET** — `test_boundary_v2.py` **31 passed**; the field annotation is asserted to name `PlannedFormulaExecutionIRV2`, and a forged narrow read set raises | S6: authorization names the IR hashes the renderer consumes |
| **C-C6a** | **`BoundFormulaRevisionV2` and `ExecutableOutputPolicyV2` TYPES** *(moved here so C-B3 has them)* | **MET [R19.10]** — `materialize/bound_formula_v2.py`, `test_bound_formula_v2.py` **26 passed**. `ExecutableOutputPolicyV2` separates DECLARED from EXECUTABLE: `FormulaOutputPolicyV2.currency` holds `"converted:<ref>"`, the executable type holds a **currency CODE** plus the converting policy as **two fields** — a declaration cannot be smuggled into the code field, and a monetary column with no currency refuses. `BoundFormulaRevisionV2` keeps **`compiler_version` outside identity** (S5's acceptance, asserted) and names its output policy **BY HASH**, so C-B3 is constructible from hashes alone — a test asserts no field holds the object | S5 (1076) |
| **C-C6** | `formula-v2/physical-types@1` defining **`SUM(amount × booking_rate)` completely** — precision, scale, intermediate precision, rounding site, SUM growth, overflow, float refusal, nullability. `empty_window` already decides V1 nullability — mirror it | **MET [R19.10]** — `materialize/physical_types_v2.py`, `test_physical_types_v2.py` **37 passed**. Grounded in **Spark's own arithmetic** (`p1+p2+1, s1+s2`; `SUM` → `p+10, s`), so the pilot types exactly: amount(18,2) × rate(9,6) = (28,8) → **(38,8)**, at the ceiling. **The ceiling REFUSES rather than caps** — Spark's default `allowPrecisionLoss=true` caps at 38 and reduces the SCALE, silently changing every value in its last places. Rounding site governed: per-row sums the DECLARED type, at-end sums the INTERMEDIATE one, and a test pins that the choice is sometimes the difference between a feature that types and one that refuses. Floats refused BY NAME; an unclassifiable type refused rather than assumed. **Overlap with V1 asserted against V1's own constant**, not a copied number; SATURATE refuses identically | S5 |
| **C-C7** | `derive_policy_occurrences(formula, bound_input_set)` replacing `required_policy_kinds()` wiring, emitting a durable **`PolicyOccurrenceSetV1`** — expression path · policy-ref field · kind/ref · semantic role · bound physical dataset/column · environment · occurrence hash **[R19]** | **MET [R19.10]** — `formula/policy_occurrences.py`, `test_policy_occurrences.py` **21 passed** (`e9dfc44a`). **Correction to the task text**: there is no `required_policy_kinds()` *wiring* to replace — nothing in `src/` calls it — so this is built fresh and the old function is left alone. Both gate clauses are asserted **directly against** the shape-based function, so the difference is a test rather than a docstring: a filter requires NOTHING (the schema cannot see which column a filter touches, so inferring from a filter's presence invents a requirement from something unreadable), and currency is required only by the OPERAND'S facts — tested both ways so it proves a discriminator, not a constant. The semantic **ROLE is a separate field from the wire field name**, because C-C8 keys families on the role; the set is ordered by occurrence hash, not walk order | **LANDED in S4** — persisted as `policy_occurrence_set` + `policy_occurrence` (1075), keyed to S3's bound input set by foreign key |
| **C-C8** | Realization identity — the **family key frozen explicitly as policy kind/ref + physical dataset binding + environment + semantic role [R19]**, or the "current" pointer would merge policies applying to different sources · **unique revision id PLUS a separate `executable_content_hash`** so a source and an LLM proposal with identical semantics stay distinct · CAS pointer · retained conflict findings · **and `realizes_occurrences`, which exists nowhere and is created here** [R18]. **Pilot realizations are TIMELESS; validity-interval detection is NOT BUILT** — `POLICY_INTERVAL_UNSUPPORTED` was never implemented, so there is nothing to remove | **MET [R19.10]** — `formula/policy_realization.py`, `test_policy_realization.py` **23 passed** (`1fc070e1`). All three family-key collapses tested (two ledgers, two environments, two roles). `revision_id == executable_content_hash` is **refused at construction**, so identical semantics cannot become one artifact. `realizes_occurrences` exists and powers `unrealized_occurrences()`, making an unanswered occurrence **detectable**. Conflicts retained after resolution. **Timelessness asserted as an ABSENCE** — a test enumerates temporal field names and requires none, with the reason recorded: a field meaning "no interval known yet" would read as "holds for all time" | **LANDED in S4** — `policy_realization_revision` + occurrence links + retained conflicts + a `pointer_version` CAS `policy_realization_current` (the ELEVEN-table house pattern, not 1055's newest-seq). **The withdrawal is scoped to POLICY VALIDITY INTERVALS — a test pins that it does not touch the as-of FX rate join** [R18] |
| **C-C9** | LLM admissibility (invariant 16); policy literals evidence-linked where available, else `LLM_PROPOSED` and **not called evidence-validated** | **MET [R19.10]** — `formula/policy_admissibility.py`, `test_policy_admissibility.py` **47 passed** (`1fc070e1`). Totality **enumerated** over 0..3 governed × 0..3 LLM × agree/disagree, not sampled. Two governed declarations refuse **even when they agree** (a governance defect, not a tie: the next edit to either diverges silently); two LLM proposals refuse unless their `executable_content_hash` matches. An LLM winner paired with an evidence-linked outcome is **unconstructible**, so the laundering is refused rather than discouraged | **LANDED in S4** — `publish_policy_realization` runs the table, persists the verdict's conflicts against the winner, and stores the LOSING candidates so the retained finding's reference resolves |
| **C-C10a** | **[R19] THE CLOSED PILOT OPERATOR GRAPH — without it none of the topology questions are decidable.** No materialization module consumes a V2 formula type and the V1 IR has no operator vocabulary at all. Freeze exactly: governed scan · PIT/availability filter · semantic selection · eligible-status filter · linked-reversal survivor · as-of FX join · duplicate-rate gate · missing-rate gate · quote inversion · decimal multiplication · aggregate · spine left join · group assembly. **Every node: typed payload · stable node id · ordered inputs · canonical identity** | **MET [R19.10] — `materialize/operator_graph_v2.py` (`b963211e`), `test_operator_graph_v2.py` 23 passed.** All thirteen frozen, one payload type per kind matched at construction (`_PAYLOAD_TYPE` asserted TOTAL over the enum), so a fourteenth operator is an amendment to the module rather than a caller's choice. **Payloads GROUNDED, never invented**: `PitSpec` verbatim, `AuthorityRefsV2`'s refs, `DecimalPolicy` verbatim, C-A3b's `SemanticRowSelectionV1`. Node ids **content-derived** (a counter would make graph identity depend on append order) and input order is identity-bearing. Now checkable: the fixed-AED pilot contains **no FX nodes**, so D3's base-currency bypass is their ABSENCE, not a fourteenth kind; the missing-rate gate **refuses** rather than left-joining to NULL (the silent omission D3 forbids); the rounding **site** is identity-bearing. `realizes_occurrences` deliberately ABSENT (C-C8), not empty. Honest note: the acyclicity guard has **no reachable refusal** while `node_id` is derived — a cycle would be a SHA-256 fixed point — so it is documented as defence in depth and a test pins the property it depends on | S6: the pilot compiles to exactly these node kinds |
| **C-C10** | Subgraph requirements over C-C10a — **FX**: as-of join · duplicate-rate gate · missing-rate gate · **base-currency identity-rate bypass** · optional inversion · decimal multiplication · connected path. **LINKED_REVERSAL**: as-of population · linkage · ambiguity gate · survivor · connected path. Plus topology-derived coverage | **MET [R19.10]** — `materialize/subgraph_requirements_v2.py` (`5577e9ad`), `test_subgraph_requirements_v2.py` **17 passed**. `FX_CONVERSION` and `LINKED_REVERSAL` are named constants, **triggered by topology** rather than asserted (a requirement a caller must remember to apply is one that gets forgotten). **Position is checked, not just presence**: a required operator must be downstream of the trigger and upstream of the terminal. Two findings: the graph type's single-terminal rule already catches the SIMPLEST disconnection, so the interesting case is a gate that IS consumed but sits on a branch the join never reaches (legal, single-terminal, protects nothing — pinned separately); and writing `LINKED_REVERSAL` found **`LinkedReversalSurvivorV2` could not express its own requirement** — two of the four facts were missing, so `as_of_population_ref` and `ambiguity_refusal_code` were added. The base-currency bypass stays an **absence**, reported as untriggered rather than as a pass | S7: deleting the duplicate-rate gate refuses (`realizes_occurrences` is C-C8 and still absent) |
| **C-C3** | `FullReadSetLeakageGateV2` over formula operands and filters · policy reads · join keys · reversal and FX inputs · temporal and availability columns · spine reads. **Claim stated narrowly** (invariant 18) | **MET [R19.10]** — `materialize/leakage_v2.py` (`0415d3cb`), `test_leakage_v2.py` **19 passed**. The S6 mutation lands: a status policy whose column IS the target refuses, attributed `policy_read:status` rather than to the formula. **Found while testing**: a ref can be read BOTH structurally and as a policy column, and reporting one path would send an author to close one door while the other still admits the target — the gate now emits **one finding per PATH**, and `structural_read_set` asks the shared walk the narrower question rather than adding a second walk. **Invariant 18's claim is carried ON the verdict** (`LEAKAGE_CLAIM_V2`), and a test asserts it never says "no leakage", "guarantees" or "is safe"; a verdict cannot be admitted while carrying findings | S6: a mutation replacing a policy status/direction column with the target ref refuses |
| **C-C4** | **DONE [R19.10]** (`0415d3cb`). **Prerequisite gap closed first**: `FormulaExecutionIRV2` had no record of which policies a formula DECLARED, so nothing could check coverage — a feature could declare an FX conversion, plan zero policy reads, and be authorized to read a rate table nobody authorized. `DeclaredPoliciesV2` carries `AuthorityRefsV2` verbatim per expression. Coverage is checked **both ways** (a declared policy with no read refuses; a read for an undeclared policy refuses), and `policy_reads` has **NO default** — `()` would be the claim "this feature reads no policy columns", wrong for every feature that declares one. Gate-2 union extended with **policy reads** — `SpineSpec.read_set` is already unioned. **The gate is restated [R18]: Gate 2's read set is DERIVED from the IRs and spine, so there is no separate list to "remove the FX table from"** | — | S6: **an FX rate column the supplied roles cannot read refuses `READ_SCOPE_INSUFFICIENT`; an ungoverned FX column refuses by its own code** |
| **C-C5** | Per-read temporal semantics; the declared T+N promise a **separate** field | **MET [R19.10]** — `TemporalReadV2(basis, declared_promise)`, asserted to be exactly those two fields. `KnowledgeTimeBasisV2.LATEST_AVAILABLE` refuses, and **a generous declared promise does not rescue it**: a promise says when data should ARRIVE, not which instant a read observes | S6: post-cutoff FX and post-cutoff reversal refuse |
| **C-C11** | The S4 policy producer's LLM seam — registered output schema · bounded input contract · audited call · replay/idempotency · egress fields. **The existing seam fails closed on unknown fields** | **MET [R19.10]** — `formula/policy_producer.py` + `("policy_realization", 1)` in `enrich_llm._SCHEMAS` (`b8f15c18`), `test_policy_producer.py` **21 passed**. `additionalProperties: false` at **every** level; the test resolves through `canonical_output_schema` (the dispatch path), not the dict, and asserts the schema survives `project_for_anthropic` — a projection-hostile node would fail EVERY live structured call closed, not just this one. Input is a **whitelist** (`PolicyProducerInputV1`), bounded at 20 distinct values with row counts — a vocabulary, not a data export. The **closed taxonomy rides INSIDE the repair loop** via `validate_semantics`, because JSON Schema cannot express "a token of the kind THIS call is about". Replay keys on the occurrence hash; provenance is hardcoded with **no `provenance` parameter to turn** | **LANDED in S4** — proved end to end without an LLM call: a payload that survives the closed-taxonomy validator becomes a revision, publishes, resolves as `LLM_PROPOSED`, and LOSES to a source with the disagreement retained. The store's entry point IS that validator, so nothing reaches persistence C-C11 would have refused |

#### D · Verification, publication and access

| Task | Deliverable | Gate (S0.5) | Gate (deferred) |
|---|---|---|---|
| **C-D0** | **MET [R19.14] — `materialize/compile/phases.py` (`273ba7fe`), `test_phases_cd0.py` 15 passed. PRODUCT-OWNER DECISION: four phases with SEPARATE durability boundaries, NOT one transaction** — one transaction cannot support artifacts sitting unverified indefinitely with on-demand verification, and its atomicity is not real anyway (a Hadoop submission is not rolled back by PostgreSQL, `os.replace` is not undone if the commit fails, and the code acknowledges orphaned staged output). The gate is a **PARTITION over a named side-effect vocabulary**, because a boolean would be satisfied by four empty Protocols; `SELECT_PUBLISHER` sits in phase 4 alone, making the publisher-refusal bug **unrepresentable**. `run_all_phases` takes no connection. **LIVE FIX shipped alongside**: `_RunAttempt.publication_refusal` preserves the verdict wherever the run stopped — `if not built:` is tested first, so a refused publisher plus a failed L0 previously reported nothing. ORIGINAL: **[R19] Four explicit extractions from the monolithic chain** — `generate_artifact()` · `request_verification()` · `execute_verification()` · `publish_verified_output()`. Today the chain selects the publisher **before** running and compiles, renders, validates, submits and publishes in one call; an identity type alone does not separate them. *(Verification note: a missing publisher does not short-circuit — it falls through to an unproven build rather than returning.)* | the four signatures exist and no one of them can reach another's side effects | S7/S9/S10 |
| **C-D1** | **MET [R19.13]** — `overlay/upload/verification_revisions.py` (`798924f5`), `test_verification_revisions.py` 23 passed. Carries **no** attestation field (asserted over four name patterns); V1's requiring one is what forces run-and-publish together, and separating them is what makes an on-demand sandbox verification possible. ORIGINAL: `VerificationExecutionIdentityV1` — **no publication attestation**; decide `__verification_execution_hash` vs a versioned alias, changing identity, system columns, run parameters and persistence **together** | constructible with **no attestation**; V1's hash still requires one | S9 (1080) |
| **C-D2** | **MET [R19.13]** (`798924f5`) — mapping total in BOTH directions. The load-bearing row: `RESULT_SCHEMA` covers keys/types and **NOT `JOIN_CONNECTIVITY`** — a schema check says nothing about whether the join found anything, and treating "the shape is right" as "the join worked" is how an all-null feature ships looking healthy. ORIGINAL: The versioned check set — result schema per check · non-null columns · feature-null rules from the output policy · spine completeness and uniqueness · join orphan/amplification · **which of the eight external requirements each may satisfy** · the check-set hash | the mapping is explicit and total | S9: a keys/types check does not satisfy `JOIN_CONNECTIVITY` |
| **C-D3** | **MET [R19.13]** (`798924f5`) — all five carried. An output pinning **no** policy hashes is REFUSED (it could never go stale, so nothing would notice); `stale_against` returns WHICH policy drifted, not a boolean. Retention reuses `blob_gc`'s `marked_orphan → quarantined → swept` and is **not** identity-bearing — record and blob have different lifetimes. ORIGINAL: `VerifiedOutputRevisionV1` — check-set hash · validator versions · pinned executable policy hashes · **`input_observation_strength`** · **retention/expiry state [R18 — dropped in revision 17]**, reusing **`runtime/blob_gc`'s `marked_orphan → quarantined → swept`** discipline rather than inventing one | the type carries all five | S9: a policy changed after verification makes the pass stale; an expired staged output is swept |
| **C-D4** | **MET [R19.14] — `materialize/artifact_manifest.py` + `artifact_store.py` + migration 1086 (FILE ONLY) (`239f9b54`), `test_artifact_manifest_cd4.py` 26 passed.** S0.5 half only; the `chain.py` call is S7. **`read_lock` is the real reason for an external manifest, not the cycle**: it enforces a two-key top level and `run_l0` calls `_lock_of` first inside the transaction without catching, so an extended lock ABORTS THE COMPILE — a test drives the real `read_lock`. **The plan's cycle argument is half wrong and is corrected in the file**: `generated_project_hash` skips the lock unconditionally, so no cycle exists for any non-lock file. **Manifest lives ONLY in Postgres** — the cluster gate hashes every tree file except the lock and `.pyc`, so a sidecar fails `PROJECT_INTEGRITY`; a test asserts neither module contains a filesystem writer. Verified at three NAMED points; both tables write-once by trigger; the DB also CHECKs no row names the lock. ORIGINAL: **An EXTERNAL control-plane manifest — `GENERATED.lock` left unchanged [R19].** The lock is excluded from `generated_project_hash`, `read_lock` enforces a strict **two-key** top level, and a blob pointer known only *after* storage would create a second cycle. The manifest carries artifact id · path · SHA-256 · byte length · media type · immutable content reference, and **for the first slice the generated text files live in PostgreSQL** rather than a new object-store subsystem. Bytes verified on **write, retrieval and execution**. *(Verification note: extending the lock would break `read_lock`, not V1 generated bytes — and `read_lock` already has an optional-key pattern one level down. An external manifest is still the cleaner cut.)* | the manifest type and the Postgres store are frozen; `GENERATED.lock` and `read_lock` untouched | S7 (1078): a mismatched digest is neither served nor executed |
| **C-D5** | **MET [R19.13]** — `overlay/upload/publication_revisions.py` (`798924f5`), `test_publication_revisions.py` 21 passed. Pins the exact generated project hash, so **changed bytes invalidate a proof with no version bump** — a version bump is a claim someone remembered to make. Carries **no** S9 check-set field at all rather than a nullable one, because a nullable one gets filled in eventually. All eight runtimes required. ORIGINAL: `OperatorExecutionProofV1` pinning signature and version · compiler and renderer versions · physical-type policy · topology version · gold corpus hash · **the exact generated gold-project hash** · **the MUTATION-set version — NOT S9's verification check-set, which does not exist at S8 and is a deliberately separate concept [R19]** · the eight runtime versions | the type refuses construction with any field absent and **carries no S9 check-set version** | S8 (1079): changed bytes invalidate a proof even with no version bump |
| **C-D6** | **MET [R19.14] — migration 1085 (FILE ONLY) + `publish.py` + the route, ONE coordinated change (`404d714b`), `test_environment_scoping_cd6.py` 16 passed.** The prevented failure is SILENT: with an environment-aware trigger and a blind `next_revision_seq`, environment B's first publication computes `max(seq)+1` across BOTH environments — which DOES extend B's empty sequence, so the trigger passes and B reads A's row. **Beyond two columns**: the WRITE side records it too (`publish_generation` passes `selection.environment_id`), `ActiveRevision` carries it, and `environment_id` is REQUIRED with no default on both readers. `IS NOT DISTINCT FROM` throughout, because `NULL = NULL` is NULL. **NULLABLE with partial indexes** per 1069/1070 precedent — a `NOT NULL DEFAULT` would assert every existing publication happened in an unrecorded environment, and both tables carry append-only triggers. **V1/V2 namespace reconciled**: ONE flat namespace per environment, `formula_language` recorded for audit and in no key — two same-named groups in one environment publish to ONE table whatever authored them. **Known gap, marked in code**: the runs route passes `environment_id=None` because `MaterializationRequestV1` carries no environment. ORIGINAL: Environment scoping on **`group_binding`** and **`feature_active_revision`** — **migration 1083 reserved [R18]**: ADD COLUMN + backfill + constraint swap. **Also reconcile the flat V1/V2 `logical_group_name` namespace**, which has no language discriminator | the target schema and backfill are written | S10 (1083) |
| **C-D7** | **MET [R19.13]** — `overlay/upload/group_name_allocator.py` (`621843b4`), `test_group_name_allocator.py` 19 passed. **CHOICE STATED: it IS `hive_identifier`** — that function's own docstring already argues for it (public so the group plan reaches the SAME answer; "a second normalizer would be a second chance to disagree"). A test asserts structurally that no NFKC/regex/translate reimplementation exists; the ≤128 bound is proved THROUGH the allocator. Collision **refuses, never suffixes**. ORIGINAL: Group-name allocator. **[R18] Either it IS `hive_identifier` extended — one normalizer for feature columns and group names — or it is scoped to group names with a stated proof of non-collision with names already bound.** `hive_identifier` deliberately refuses to truncate | the choice is stated and the ≤128 bound proved | S6 |
| **C-D8** | **MET [R19.13]** (`798924f5`) — both fields on the signature. 1055's trigger stops two concurrent writers both winning; it does **not** stop a slow writer arriving late holding an old answer, because that writer conflicts with nobody. The conflict RAISES rather than retrying — whether the newer revision supersedes theirs is a question about the outputs, not about locking. ORIGINAL: Active-revision **CAS** — `publish_sandbox(verified_output_revision_id, expected_active_revision_id)`; 1055's trigger stops concurrent double-wins, not a stale publish | the signature carries both | S10 (1081): an older verified output over a newer active revision refuses |
| **C-D9** | **MET [R19.13]** — `overlay/upload/evaluator_contracts.py` (`621843b4`), `test_evaluator_contracts.py` 17 passed. The sixteen were **found by reading what `activation_policy.py` emits**, not by trusting the count, and the completeness test **re-derives the set from that module's source** — a list copied into a test is exactly how revision 17 lost one. `PERSONAL_DATA_POLICY_REQUIRED` CARRIED. Four DROPPED, every one a semantic-confirmation gate or another action's. **Judgment call flagged**: `RECIPE_REVIEW_NOT_CURRENT` CARRIED, because the reviewed blueprint is the ground that justifies dropping the semantic gates. A verdict carrying an undisposed code is refused at construction. ORIGINAL: **Three evaluator INTERFACES plus their refusal vocabulary**, and **a code-by-code table of all sixteen activation blockers: CARRIED or DROPPED, with a reason** — `PERSONAL_DATA_POLICY_REQUIRED` is **carried** [R18]. **The implementations ship at S8/S9/S10, not here** | a typed-contract test over **stub records**; the sixteen-row table is complete | S8/S9/S10: each evaluator's real decisions |
| **C-D10** | **MET [R19.13]** (`798924f5`) — six operational terms frozen. Of the two options, takes the **explicit one-grain restriction** (`refuse_multi_grain`), because a derived group does not exist until S6. `planning_request_hash` deliberately ABSENT — folding it in would make every re-declaration a new request. ORIGINAL: `BuildDeclarationV1` frozen — cadence · availability promise · spine · environment · parameter bindings · base name; **one declaration per derived group, or an explicit one-grain restriction**. Reconcile with the **deliberate** request-identity split and its named test | the type is frozen and the reconciliation stated | S11 (1082): the existing test still passes |
| **C-D11** | **MET [R19.14] — `overlay/upload/planning_request_store.py` + migration 1084 (FILE ONLY) + route wiring (`e6362c77`), 14 + 1 API test passed.** **PRODUCT-OWNER DECISION: build the real second source rather than test an unreachable branch.** The gate had ONE occurrence in the whole repo, no test, no handler, and could not fire — both compared values are written from one in-memory object in one statement. Now: canonical payload + independently stored hash + decision reference, and the reader RECOMPUTES the hash from the payload's own bytes, so corrupting any of the three refuses. Parser is **field-exhaustive** (a hand-written one would drop a new field and the hash would never match); round-trip proved across **all 317 shipped recipes**. Legacy rows return `LEGACY_PLANNING_REQUEST_UNAVAILABLE` and serve 200 — not tampering. The **API test drives the REAL route** and gets its 409, corrupting a field that survives `__post_init__` so it proves the hash check rather than the validator. ORIGINAL: **Typed `FeaturePlanningRequestV1` persistence and reader. Premise corrected [R18]:** `planning_request_hash` is **load-bearing** (the 409 `DECISION_RECORD_TAMPERED` gate) and the `asdict()` copy **is served** in `evidence`; **only `parameter_values`' `repr` is dead** | the typed request round-trips | S2 (1073): legacy rows refuse by name rather than being reconstructed from `repr`; **the 409 tamper gate still fires** |
| **C-D12** | **MET [R19.13]** (`798924f5`) — **this CORRECTED my own C-B1 work.** Writing `exploring` into the provenance column does not merely conflate two questions, it ERASES one: `contract.py` does `provenance = "exploring" if … else "human_confirmed"`, so a person who explicitly declared no target had their identity overwritten by the declaration. Mode is now DERIVED from the discriminated union (the union already was the mode axis); provenance narrows to two human values. `map_legacy_provenance("exploring") → (EXPLORATION, None)`, and the None is truthful rather than lossy. ORIGINAL: **[R18 — re-scoped, not new]** A target **MODE** axis independent of who declared it, **migrating the existing `exploring` provenance value onto it** — one field, one owner. An explicit no-target declaration already ships | the mode axis exists and `exploring` maps onto it | S1 (1072): legacy `exploring` rows map with no loss |
| **C-D13** | **MET [R19.13]** (`798924f5`) — attempt is IN the identity, not only the path, so two attempts cannot share one by construction. Attempt 0 refuses: it would collide with the generation-scoped root this field replaces. ORIGINAL: **[R18]** A **per-attempt staging location** — the existing root is generation-scoped, so repeated verifications would collide and S10's "exact staging output" would be ambiguous. Thread the attempt into `VerificationExecutionIdentityV1`, or make a second concurrent attempt refuse under a lease | the attempt component is in the identity | S9 (1080): two attempts do not share a path |

> **Acceptance (S0.5):** every contract has a frozen schema with a pinned hash and a test that fails
> if a field is added without updating it; **every S0.5 gate is satisfiable with no migration
> applied**; **`make test` green — the ONLY repo-wide gate [R19.5, measured]: `ruff format --check
> .` reports 1280/1523 files would reformat, `uv run mypy` 469 errors in 124 files, and
> `uv run ruff check .` 79 errors, so lint/typecheck/format are RATCHETS ("the files you touch are
> clean"), never green gates** — and, from `frontend/`,
> `npm run typecheck`, `npm test`, `npx oxlint` green; V1 hashes, canonical bytes, signatures and
> contract types untouched.

### S1 — immutable selection + target-reading revision *(1072)*

> **DETERMINISTIC PRODUCER: BUILT [R19.9]** — `formula/deterministic_producer.py`.
> `proposal_from_bound_expectation()` turns a reviewed, BOUND blueprint into a
> `TypedFormulaProposalV3` **through `parse_proposal_v3`** — the same shape-then-semantics gate an
> authored proposal passes, so it is not a second admission path. `bypass_for()` builds the review
> outcome from the bound expectation itself, so the bypass names the artifact it stood on rather
> than a value a caller chose. **No provider seam is importable from the module, asserted by test.**
> Measured end to end on the shipped `posted_debit_amount`: derive → bind → produce yields a
> proposal carrying `direction/debit`, and `posted_credit_amount` yields `credit` — with nothing
> reading a recipe name.
>
> **It found a gap in C-A3b:** `bind_formula_expectation_v2` did not copy `row_selections`, so the
> binder silently dropped the declaration and returned the pilot to name-inference. Only an
> end-to-end test could catch that — the unit tests on each side both passed.
>
> **Deliberately still owed:** the replay-trace half (writing `REVIEW_BYPASSED` through
> `replay_authoring_v2` rather than folding a result directly), and wiring the worker to choose this
> path for a reviewed recipe.

> **Acceptance:** **a selection is CONSTRUCTIBLE before any definition exists** (the vacuous "no executable revision" half moves to S6, where that record first exists) **[R19]**; a re-read creates
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

**DONE** — `formula/policy_store.py` + migration `1075` (file only), `test_policy_store_s4.py`
**47 passed**. **This is the first thing in the platform that RESOLVES a governed policy reference**,
and that was verified rather than assumed: `parse_policy_ref` is shape-and-kind only and never
touches a connection, and `semantic_eligibility.py:326-327` emits `STATUS_POLICY_UNRESOLVED` on the
mere PRESENCE of a ref with no lookup between the lines — so until now every governed reference was
unresolved *by definition* and "unresolvable" was not a distinguishable state.

* **Clause 1** — `POLICY_REFERENCE_UNRESOLVABLE` joins `REASON_FAMILIES` as `needs_setup` and
  **precedes** `STATUS_POLICY_UNRESOLVED` in `REASON_PRECEDENCE`: the blanket code means *no resolver
  serves this kind*, the new one means *the resolver ran and found nothing*, and the two have
  different remedies. Publication keeps C-C9's own names (`TWO_GOVERNED_DECLARATIONS`,
  `NO_DETERMINISTIC_WINNER`, `NO_CANDIDATE` — including for an empty candidate list, rather than a
  second vocabulary for the same fact) so "unresolvable" is never reported when the truth is "two
  teams both declared it".
* **Clause 2** — asserted BEHAVIOURALLY through the store via `unmet_policy_kinds`, which keeps
  C-C7's two questions apart (need from bound facts, supply from occurrences that actually resolve)
  and returns only the difference. Both directions tested, so it proves a discriminator: the same
  expression and the same published direction policy leave `currency_conversion` unmet for a
  `monetary`/`per_row` operand and nothing unmet otherwise. **Declaring a ref is not supplying it** —
  a declared FX ref stays unmet until a realization is current.
* **Clause 3** — the clause is about PERSISTENCE, and the wiring is the point: C-C9 computes
  `SOURCE_OVERRODE_LLM` on the **verdict**, not on the winner (the winner is a candidate that knows
  nothing about the decision it went on to win), so a publication writing only the winner's own
  findings drops it. **Losing candidates are stored too** — a retained finding naming a revision
  nobody can look up records a disagreement nobody can inspect. Stored, and NOT current.
* **Clause 4** — the mutable upsert is pinned by name first (`eligibility_store.py:58-64`,
  `ON CONFLICT (catalog_source, table_name) DO UPDATE SET`, no version guard, no history) so the
  clause keeps meaning something; then the V2 path is shown unable to REACH it (no import, checked
  against comment-stripped source), to contain no `DO UPDATE` at all, and to refuse a stale writer
  by CAS while the superseded revision stays readable — the history the mutable path has none of.
* **The pointer follows the ELEVEN, not the one.** Checked before choosing: eleven `*_current`
  tables use an immutable revision table + a separate `pointer_version` CAS row;
  `feature_active_revision` (1055) resolves current by newest-seq and is the only one, its own header
  scoping that to publication. Resolution keys on the **family** (what C-C8 defines "current" to be
  current FOR), and `ResolvedPolicyV1.claims_occurrence` reports whether the current revision was
  built for this exact occurrence rather than hiding the difference.
* **C-A3c's deferred gate lands here — "provenance pinned on the occurrence" — and it is a REQUIRED
  argument, not an optional one.** `MeasureFact` already names itself *"the provenance an occurrence
  must pin"*, so `record_occurrence_set` refuses by name (`OccurrenceProvenanceMissing`) without the
  reads; a default would make the unpinned call the easy one to write. The defect it closes is
  specific: a per-row-currency monetary operand used to arrive looking NON-MONETARY **with nothing
  recorded**, so the FX requirement could not fire and a mixed-currency population was summed in
  silence. `operand_facts_from_measure` now feeds the need calculation from the VERIFIED read, and
  an `absent` disposition is stored as a **positive statement** — a missing row and a recorded
  absence are the two things `policy_occurrence_measure_read` exists to keep apart. Reads are keyed
  **per derivation** (`PRIMARY KEY (set_id, occurrence_hash, field)`) so two derivations over a
  catalog that moved each pin what they saw; keying on the occurrence alone would be the same
  in-place restatement clause 4 refuses everywhere else. The provenance is deliberately **outside**
  the occurrence's identity hash, the house rule everywhere here (`observation_id`/`captured_at`,
  `compiler_version`), so C-C7's committed identity is untouched.
* Mutation-checked: dropping the verdict conflicts kills 3 tests, recording only the winner kills the
  loser-inspectability test, and renaming the refusal string kills the by-name test.
* Suite **12380 passed, 20 skipped** (12333 → +47, exactly S4's).

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
> not share a staging path; **staleness is three-way [R19]** — a comparable `OBSERVED` input that
> changed ⇒ **stale**, an identical observation ⇒ **current**, `UNPINNED` ⇒ **neither**, remaining
> labelled unverifiable and never claimed current or stale on content; observation strength is never
> `PINNED` without enforced reads.

### S10 — exact-output CAS publication *(1081, 1083)*
**Publication reselects the mechanism against the CURRENT environment and records the exact
capability attestation — verification must not require one, publication must [R19].** The
implementation documents a cross-plane window where the Hive swap succeeds and the database
transaction later rolls back; rather than a distributed transaction, an attempt ends in
**`STARTED` · `SUCCEEDED` · `FAILED` · `UNKNOWN_RECONCILIATION_REQUIRED`**, and an uncertain attempt
is reconciled against the published generation marker **before** any retry.

> **Acceptance:** an older verified output over a newer active revision refuses; a partial group never
> becomes visible; environment keying is in place; **an interrupted swap lands
> `UNKNOWN_RECONCILIATION_REQUIRED` and blocks retry until reconciled**.

### S11 — consolidation and polish *(1082)*

**[R19] The surfaces ship with their capabilities, not all at the end** — the Generate endpoint,
code-view API and top-of-workspace stage UI land at **S8**; Verify request/results UI at **S9**;
Publish UI at **S10**. S11 consolidates the build-set/child-group hierarchy and the full UX.
Endpoints incl. **an explicit generate endpoint** · the derived group split before verification ·
policy provenance with `LLM_PROPOSED` visible · **goal, target, stage and output at the top of the
workspace**.
> **Acceptance [R19 — bounded, since "no path" is not observable from a UI test]:** the verification
> and publication handlers appear in **no relay route map and no timer**, and the only callers of
> `evaluate_verify` / `evaluate_publish_sandbox` are their two request endpoints — asserted by an
> enumeration test over the route table — plus a UI test that the buttons are the only client-side
> callers; results sit above intake.

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

**What changed in revision 19.** V3 becomes a **complete type family** — `AggregateExpressionV3`,
body types, proposal, `schema_v3`/`canonical_v3`/`parse_v3` and every consumer — because revision 18
said "v3 gets its own dataclasses" and then named `AggregateExpressionV2` as the target, which would
have rehashed every stored V2 artifact. The recipe gains **structured semantic selections**, since
`posted_debit_amount` declares `debit` only in its name and prose today. A **`MeasureFactsReaderV1`**
is added because unit and currency come back `not_operational` through the current reader, so the
currency requirement could never fire at all. The **closed pilot operator graph** is frozen, without
which no topology question is decidable — no materialization module consumes a V2 formula type and
the V1 IR has no operator vocabulary. `BuildSetRevisionV1` becomes the missing root, generation
authorization covers **all** member selections, the artifact manifest moves **out** of
`GENERATED.lock`, and the chain gets four named extractions. Conversion-before-aggregation is
recorded as **settled**, staleness becomes three-way so `UNPINNED` is neither current nor stale, and
publication gains `UNKNOWN_RECONCILIATION_REQUIRED`. **Logical refs must carry a `source::` prefix** —
revision 18 claimed the opposite from a single-module grep, the same over-generalisation that
produced the `repr()` error two revisions earlier.

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

**Three review claims were corrected by verification [R19]:** `AuthoringResultV2` *does* have a
legal no-policy shape (`NEEDS_REVIEW` with an unresolved output status), so the V3 result is a
clarity choice rather than a forced one; extending `GENERATED.lock` would break `read_lock`, **not**
V1 generated bytes, and an optional-key precedent already exists one level down; and migration 1063
records every option **served**, not a selection, so the selection record is genuinely new.

**No duration estimate.** Fifteen revisions have now carried one that a review invalidated.

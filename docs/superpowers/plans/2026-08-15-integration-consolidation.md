# V2 as the feature language, on shared execution

**Date:** 2026-08-16 · **Revision 16** — S0.5 folded in at task level. Revision 15's **plan-vs-code audit**: every factual claim in §0.1 was
checked mechanically against the tree rather than re-read. **39 claims tested; 33 held as written;
2 blockers and 4 accuracy errors were found and are fixed below.** The audit method and its results
are recorded in §0.0 so the next review can re-run it rather than re-derive it.

Revision 14's own framing stands and is repeated, because it is the most important sentence here:

> **S0.5 is now task-level; S1–S12 remain programme-level [R16].** The eighth review asked for a
> separate contract-freeze plan. It is folded in here instead, as **§S0.5's fourteen tasks with file
> paths, reserved migrations and the repo's own gate commands** — one document, so a contract and
> the stage that consumes it cannot drift apart. **S1–S12 are still programme-level and are not
> executable as written**; each gets a task-level pass when S0.5 lands.

> **V1 remains the stable compatibility language. V2 becomes the intelligent, policy-aware language
> for all new features. LLMs create and enrich V2 formulas; a deterministic V2 compiler resolves
> their banking meaning and runs them through the existing Spark/Kedro materialization platform.**

**Do not reduce V2 to V1. Do not build a second V2 platform.** The V2 execution boundary is an
**adapter** onto language-neutral machinery — rendered nodes, wiring validation, sealing, staging,
output gates, publication.

## 0.0 Audit provenance *(new [R15])*

Every claim in §0.1 was turned into a mechanical check against the working tree — a grep, a parse or
a count — and run. **33 of 39 held exactly as written.** What the audit found:

| # | Finding | Class |
|---|---|---|
| A1 | **Operand facts gate the entire currency thread, and no stage named them.** | **blocker** |
| A2 | **The ref namespace is a FOURTH disagreeing axis; `authored::` is test-only.** | **blocker** |
| A3 | `materialization_group_binding` **does not exist** — the table is `group_binding`. | accuracy |
| A4 | **Gate 2 already unions the spine read set**; only policy reads are missing. | accuracy |
| A5 | **`GENERATED.lock` already exists** and carries `generated_project_hash`. | accuracy |
| A6 | **`empty_window` already decides nullability** in V1 physical typing. | accuracy |

Two checks initially reported failure and were **wrong about the plan, not the code** — a malformed
`sed` range over `TERMINAL_RUN_EVENT_KINDS`, and a regex matching the word *filename* in a comment.
Both claims re-verified by direct parse. **A failing check is investigated, never accepted.**

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

Re-checked against this tree. **Baseline `29f3b8ac`; targeted suite 2,827 passed, 1 skipped.**

**The entire currency thread is gated by facts no stage named [R15 — blocker A1]**

- `output_authority_v2`'s conversion tooth fires **only** under
  `if facts.unit == "monetary" and facts.currency == "per_row"`. Those come from
  `OperandFactsV2`, built by `_read_c1_facts_v2(conn, proposal)` out of **governed C1 metadata**.
- **If the pilot's `txn_amt` does not carry governed `unit=monetary, currency=per_row`, the tooth
  never fires**: `CURRENCY_CONVERSION_UNDECLARED` never raises, no FX policy is ever required,
  resolved or consumed — and S0's FX branch, S3's typed booking-FX realization, S5's FX operators,
  S6's duplicate-rate gate and S7's FX mutations all test **nothing**.
- Fourteen revisions built an FX thread on an unstated precondition.

**The ref namespace is a FOURTH disagreeing axis [R15 — blocker A2]**

- The gold exemplar's refs are `authored::public.txns.txn_amt`. **`authored::` appears in `tests/`
  only — production emits no `xxx::` prefix at all.**
- So the frozen exemplar uses a ref format production does not produce, on top of the timezone,
  boundary and length disagreements. S0's reconciliation had three axes and needs four.

**Output authority certifies a currency it never resolved [R14]**

- `output_authority_v2.py`'s conversion tooth refuses only when `currency_conversion_ref` is
  **empty**. A non-empty ref passes and the output's currency becomes
  `converted:policy:governed-rate-at-booking` — establishing nothing about the policy's existence,
  target currency, rate precision, quote direction, temporal rule or rounding. **Rule 2, violated by
  the gate revision 13 scheduled at S1, before S3 resolves anything.**

**The review bypass still cannot be represented [R14]**

- `AuthoringResultV2` is built **only** by `derive_disposition_v2` and carries
  `critic_status: CriticStatus` (`clean | advisory | blocking`). It **cannot express "the critic did
  not run"**, and `critic_status="clean"` would falsely say it ran and found nothing. Revision 13
  added a status and a transition but still called the output "an ordinary `AuthoringResultV2`".
- `replay_trace.py` permits `OUTPUT_POLICY_RESOLVED` only after `CRITIC_COMPLETED`.

**The approved target is lost before execution [R14]**

- `suggestion_id` is *"deliberately independent of the SCREEN it was opened from, of the validation
  OUTCOME and of every build observation"* — it identifies the **logical candidate**, not what the
  user is predicting.
- The governed-contract path already re-reads the approved target, horizon and provenance from
  `contract_intent` **server-side, so the client cannot disable leakage checking**. Nothing carries
  that into the execution path.

**Policy-added reads bypass leakage screening [R14]**

- `feature_assist.py`: `if target_ref and target_ref in derives: return _reject(RejectCode.LEAKAGE,
  "leaks target")` — **the derives list only**.
- Gate 2 (`authorize_compilation`) validates existence and read permission, **not leakage**.
- S3/S5 then add status, direction, reversal, FX, temporal and spine reads. **A policy can introduce
  the target column after the candidate passed screening.**

**Removing the route guard does not remove the human gates [R14]**

- `_contract_blockers` independently appends `PROPOSED_METADATA_ONLY` for
  `confirmation_required_roles`, plus `BINDING_NOT_BOUND`; the fold also blocks on
  `RECIPE_REVIEW_NOT_CURRENT` and `EXECUTION_AUTHORITY_UNMET`. **A non-admin reaches the endpoint and
  is still refused.** Revision 13 put the route change at S2 and the replacement matrix at S7.

**Execution proofs would survive toolchain changes [R14]**

- `sandbox_execution_hash` reads `render.RENDERER_VERSION` and `compile.COMPILER_VERSION` *through
  their modules* precisely so a changed toolchain moves the hash.
- `inventory.py` records **eight runtime versions** — hive, spark, metastore, python, java, pyspark,
  kedro, kedro_datasets.
- Revision 13's capability signature omitted all of them.

**Schema-3 dispatch is narrower than the plan assumed [R14]**

- `parse_versioned` accepts **1 and 2 only**, refusing unknown versions loudly.
- `canonical_v2._plain_v2` serializes every dataclass field automatically.

**Everything else confirmed**

- `eligibility_store.record_eligibility` is a **mutable upsert** — *"re-proposing CLEARS a previous
  confirmation"* — so it overwrites history and cannot sit on a V2 execution path.
- `FeatureGroupPlanV1.physical_type_policy_version: int`; `authorize_compilation` takes
  `Sequence[FormulaExecutionIRV1]` and returns *"the only way into §2's downstream chain"*.
- `runprep` states input snapshots are *"prepared EVIDENCE, not an enforced read scope"*.
- `next_revision_seq` is read-then-write; 1055's trigger stops concurrent double-wins, **not** a
  stale verification published later.
- **`group_binding`** *(not `materialization_group_binding`, which does not exist [R15 — A3])* is
  `UNIQUE(logical_group_name)`, and `feature_active_revision` (migration 1055) is keyed
  `(logical_group_name, seq)`. **Environment is already first-class in the schema** —
  `publication_capability_attestation` and `pipeline_validation_report` both carry `environment_id`
  — **it is absent precisely from the two tables that decide what is current.** Hive identifiers
  ≤ **128 chars**.
- `materialization_compiled_artifact` stores `group_plan` and `materialization_contract` jsonb —
  **no generated source files**.
- `_gold_evaluation_recorded()` returns `False` for every recipe; `engine_capability` derives from
  renderer dispatch.
- `render/project.py` refuses when `datasets.published not in written`;
  `@router.post("/materialization-runs", dependencies=[Depends(require_confirmer)])`;
  `govern.py:610` selects latest-by-`feature_name`.
- `identity_payload()` excludes `non_physical_refs` / `operand_type`; `filter_tree` is opaque;
  `authority_refs` are *"identity-bearing exactly as authored"*.
- `PHYSICAL_TYPE_POLICY_VERSION = 4` hardcoded in `contract.py` and `group_plan.py`.
- `1023`'s work item has recipe-specific NOT NULL columns, a FK and four triggers; it is the only
  durable intent anchor. `required_policy_kinds()` has zero callers.
- **`GATES_PASSED` is not terminal**; eight external validation requirements; `api.ts:4190` has only
  a GET; the POST takes shadow ids capped at 12; `business_dt` absent from the route; cross-catalog
  refuses at `chain.py:906`; migration prefixes collide seven times.
- The pilot's gold is three disagreeing artifacts; the ledger's `direction` is `D`/`C`; no
  availability timestamps. `WorkbenchScreen.tsx` renders results far below repeated intake panels.

**Two standing instructions**

1. **Grep for the thing before designing it.** **Ten** prior instances — the newest two are
   A4 (Gate 2 already unions the spine) and A5 (`GENERATED.lock` already exists). **§0.0's audit is
   the mechanical form of this instruction; re-run it before the next revision.**
2. **Every acceptance criterion must be satisfiable with only what exists at its own stage.**

## 0.2 Invariants

1. No V2 formula executes with an unresolved policy reference.
2. **A non-empty policy string is not proof that the policy exists — and no gate may certify an
   output on the strength of one [R14].**
3. **Coverage is derived from typed operator kinds, parameters and graph topology — never from
   self-attestation.** `realizes_occurrences` is attribution.
4. No policy may disappear when V2 reuses existing operators.
5. **Two identities, kept apart.** *Formula identity* covers what was **authored**; **`ir_hash`**
   covers **resolved executable semantics** and excludes provenance and revision numbers.

   | Identity-bearing in `ir_hash` — **changes rows** | Recorded, **not hashed** — explains |
   |---|---|
   | status column, eligible values, null behaviour | producer, confidence, evidence, observed values |
   | direction column, debit values, normalization | model and prompt version |
   | reversal mode and its columns/linkage | approval state, who confirmed, when |
   | rate table, keys, effective timestamp, booking-vs-settlement, direction, rounding | **the realization revision id itself** |
   | allocation method and its columns | lineage narrative |
6. **V1 formula hashes, IR canonical bytes, function signatures and contract types remain
   unchanged.**
7. **V2 wire schemas freeze once shipped**, which requires per-version parser, model and canonical
   projection **dispatched together**, not an optional field. Schema-2 fixtures are tested
   **byte-for-byte**.
8. V1 and V2 share **language-neutral** publication and validation machinery through a V2 adapter.
9. The LLM proposes and explains; deterministic code executes.
10. The UI never asks a user to choose a formula version.
11. **"Supported" means generated code has been executed successfully**, recorded against a
    capability signature **that pins the toolchain and runtime that produced it [R14]**.
12. **Required = declared = resolved = covered, occurrence-addressed and many-to-many.**
13. **The formula says WHAT; the policy says HOW.**
14. **One policy reference has one owner.** `AuthorityRefsV2` owns refs; `SemanticRowSelectionV1`
    carries `kind · role · semantic_value` and no `policy_ref`.
15. **Every physical read carries its own temporal semantics.**
16. **LLM-proposed realizations are usable, visibly — with conflict rules that do not contradict
    themselves [R14].** Revision 13 said source outranks LLM **and** that conflicts refuse. Those are
    different behaviours. The rule:

    | Situation | Outcome |
    |---|---|
    | governed/source-declared **vs** LLM | **source wins**; a visible conflict finding is retained and the LLM proposal demoted |
    | two conflicting governed/source declarations | **refuse** |
    | multiple conflicting LLM-only realizations, no deterministic winner | **refuse** |
    | one structurally valid LLM-only realization | **usable**, marked `LLM_PROPOSED` |

    Human review changes trust provenance and ranking, **not executability**. A successful Spark run
    proves computational execution, **not semantic correctness**.
17. **The approved target travels with the selection, and every read is screened against it
    [R14].** Leakage screening today covers the authored derives list only; policy resolution adds
    reads afterwards.

**Rule 11's bootstrap:** an operator's first execution happens on **S7's development gold path**.

## 0.3 Three actions, three requirements

| Action | Functional requirements |
|---|---|
| **Generate** | selected revision · complete binding · valid formula · **resolvable** policies · renderer support |
| **Verify** | the exact sealed artifact · execution permission · environment compatibility |
| **Publish sandbox** | a current passing verification · the exact staging output · publication permission and capability |

**None of these is human semantic confirmation.** AI-proposed metadata, policy proposals and bridges
remain usable and visibly labelled. **Human review must not re-enter through
`RECIPE_REVIEW_NOT_CURRENT`, `PROPOSED_METADATA_ONLY` or `EXECUTION_AUTHORITY_UNMET` [R14]** —
removing `require_confirmer` from the route changes none of them, so **the matrix is frozen in S0.5
and implemented in S2, together**.

## 0.4 Three words that must not blur

**Execution proof** (S7) — development-time, mutation-tested, reusable, pinning its toolchain.
**Sandbox verification** (S8) — user-triggered, tied to the exact generated artifact, **never
interchangeable with the development proof**. **Publication** (S9) — atomic, CAS, promoting exactly
the verified bytes.

## 0.5 Migration ledger

**S0 builds a ledger with a CI uniqueness test.** Allocation happens per stage. **S2's first task is
the explicit existing-vs-new map** against `materialization_request`, `materialization_generation`,
the compiled-artifact and run-event tables.

## 1. The sequence

### S0 — decide the pilot's semantics *(hard STOP: humans decide)*

Decide: policy-reference namespace · window timezone, boundary and length · reversal-as-of
semantics · direction mapping (`D`/`C`) · population spine · FX join and cardinality · publication
rule.

**The numeric decisions revision 13 omitted [R14]** — without them the gold rows cannot be
authoritative: **eligible status values and null handling** · **unknown-direction behaviour** ·
**target currency** · **FX missing-rate behaviour** · **quote convention and whether inversion is
permitted** · **conversion before or after aggregation** · **rate rounding**.

**Confirm the pilot's governed operand facts FIRST [R15 — blocker A1].** Before any FX work is
scheduled, establish that `txn_amt` carries governed C1 facts of `unit=monetary` **and**
`currency=per_row`. **If it does not, the currency thread is inert and the FX branch decision is
premature** — the conversion tooth never fires, so nothing downstream is exercised. Either govern
the facts, or record that the pilot proves status/direction/reversal only and move FX to its own
pilot.

**Reconcile FOUR axes, not three [R15 — blocker A2]** — timezone, boundary, length **and the ref
namespace**. The exemplar's `authored::public.txns.txn_amt` uses a prefix that exists only in
`tests/`; production emits none. Decide the canonical production ref format and restate the
exemplar in it.

**Author the expected rows** for one coherent parameterized exemplar covering: zero-eligible spine
account · unknown-transaction account · post-cutoff reversal · duplicate and missing FX rates ·
post-cutoff FX knowledge time — **with real availability timestamps**.

**Decide the FX branch and make it buildable.** Same-catalog pinned source, or explicit tasks for a
**reference-data join authorization** — not an entity bridge.

Also: the migration ledger.

> **Acceptance:** every decision has a record naming who decided; **`txn_amt`'s governed C1 facts
> are read and recorded, and a test asserts that the pilot proposal WITHOUT a
> `currency_conversion_ref` refuses `CURRENCY_CONVERSION_UNDECLARED`** — proof the tooth fires at all
> **[A1]**; **the exemplar's refs are in the production format and no `authored::` prefix survives**
> **[A2]**; expected rows stored and hashed with availability timestamps; each numeric decision
> appears in the frozen exemplar; the FX branch chosen and its tasks written; the ledger's CI test
> fails on the seven collisions unless grandfathered.

### S0.5 — freeze the load-bearing contracts *(task level [R16])*

**Do not begin S1 until every task here is green.** Ten revisions each designed a stage against an
interface that did not fit.

**Conventions, from this repo:** Python gate `make test` (`uv run pytest -q`), plus `make lint`,
`make format-check`, `make typecheck`. Frontend gate — from `frontend/` — `npm run typecheck`
(`tsc -b`, **never** `tsc --noEmit`), `npm test` (`vitest run`), `npx oxlint`. JSON Schemas follow
`src/featuregen/formula/proposal_v{n}.schema.json`.

**Migration reservations.** Highest today is **1071**; prefixes already collide at 0973, 0974, 1034,
1036, 1037, 1038, 1040. S0 records these in the ledger; **the stage that needs the table writes it**:

| # | Table | Written at |
|---|---|---|
| 1072 | feature definition · selection revision · build declaration | S1 |
| 1073 | authoring work item (generalized) + backfill | S1 |
| 1074 | build set · derived group · staged group output | S2 |
| 1075 | policy realization revision + current pointer | S3 |
| 1076 | artifact file manifest | S6 |
| 1077 | operator execution proof | S7 |
| 1078 | verification request/attempt/result · verified output revision | S8 |
| 1079 | publication request/attempt | S9 |

#### A · Authoring contracts

| Task | Deliverable | Gate |
|---|---|---|
| **C-A1** | `proposal_v2.schema.json` **frozen**: its own parser, model and canonical projection emitting the exact pre-change bytes. `_plain_v2` walks every dataclass field, so v2 gets a projection that ignores v3-only fields. | a test serializes every `tests/featuregen/formula/gold_fixtures/*.json` and `gold_v2/*.json` and asserts **byte equality** with the committed file |
| **C-A2** | `proposal_v3.schema.json` + `parse_v3`, and `parse_versioned` extended — it accepts **1 and 2 only** today | `parse_versioned({"formula_schema_version": 3, …})` returns a v3 proposal; an unknown version still refuses loudly |
| **C-A3** | `SemanticRowSelectionV1(kind, role, semantic_value)` on **`AggregateExpressionV2`** *(schema_v2.py — the artifact the compiler reads)* and on `ExpressionRoleExpectationV2` / `BoundExpressionExpectationV2` *(recipe_formula_contracts_v2.py)*. **No `policy_ref`** | a v3 proposal with a selection parses; the same selection on a v2 proposal refuses; `UNAUTHORED_FILTER` behaviour is unchanged for `filter` |
| **C-A4** | Schema-3 coherence rule: **a direction selection requires `AuthorityRefsV2.direction_policy_ref`** (rule 14 — one owner) | a selection without the ref refuses by name; a selection whose ref is present passes |
| **C-A5** | `ReviewOutcomeV2 = CriticExecutedV2(status, findings_hash) \| ReviewedBlueprintBypassV2(blueprint_revision, expectation_hash)`, with **V2-specific axes** — `AuthoringResultV2` carries a mandatory three-member `critic_status` and cannot say the critic did not run | a bypass round-trips the replay trace **with no `CRITIC_COMPLETED` event and no `critic_status`**; a critic-executed run folds exactly as today |
| **C-A6** | `REVIEW_BYPASSED` replay transition + checkpoint reconstruction; **bump disposition, orchestrator and replay protocol versions**; old trace readers still read old traces | an existing recorded trace replays unchanged; a bypass trace replays; version constants moved and asserted |
| **C-A7** | Three identity records — `FeatureDefinitionV1` (stable key + stable output column, **target-neutral**), `FeatureSelectionRevisionV1` (**immutable**; `intent_id` · target logical ref · target window days and type · target-reading provenance · target-reading content hash), `ExecutableFeatureRevisionV2` (created after S4/S5) | a selection is constructible **without** an executable revision; the definition carries no target; a governed contract is optional |
| **C-A8** | Output authority splits: `AuthoredOutputIntentV2` (S1) vs `ExecutableOutputPolicyV2` (S4). **Only the executable policy may enter the contract** | a non-empty `currency_conversion_ref` yields an **intent**, never a certified `converted:<ref>` currency |

#### B · Execution contracts

| Task | Deliverable | Gate |
|---|---|---|
| **C-B1** | `AuthorizedCompilationV2` · `MaterializationContractV2` · `FeatureGroupPlanV2` · `CompilationIdentityV2` *(new modules beside `materialize/ir.py`, `contract.py`, `group_plan.py`)*. V1's `physical_type_policy_version` is an `int` and its Gate-2 token carries `FormulaExecutionIRV1` | V1 types, signatures and canonical bytes **unchanged**; a V2 IR cannot construct a V1 token |
| **C-B2** | V2 renderer entry point reusing language-neutral machinery — rendered nodes, wiring validation, sealing, staging, output gates | the V2 path reaches sealed bytes; **no V1 renderer signature changes** |
| **C-B3** | `FullReadSetLeakageGateV2` — deterministic, **after policy planning, before Gate 2** — over formula operands and filters · policy reads · join keys · reversal and FX inputs · temporal and availability columns · spine reads. Today `feature_assist` screens `target_ref in derives` **only** | a mutation replacing a policy status or direction column with the target ref **refuses compilation** |
| **C-B4** | Gate-2 union extended with **policy reads**. `SpineSpec.read_set` is **already** unioned *(A4)* — this adds the missing half | removing the FX table from authorization while retaining it in the operator graph refuses |
| **C-B5** | Per-read temporal semantics: the formula window stays on the expression; **every physical read/operator edge carries its own spec**; snapshots derive independently for transaction, reversal and FX; contract availability is their **union** | a post-cutoff FX knowledge time refuses; a post-cutoff reversal refuses; the three snapshot sets are separately addressable |
| **C-B6** | `formula-v2/physical-types@1` — a truth table defining **`SUM(amount × booking_rate)` completely**: amount precision and scale · intermediate precision · where rounding occurs · SUM precision growth · overflow · whether float operands refuse · nullability. **`empty_window` already decides nullability in V1** *(A6)* — mirror it | every row of the table is asserted; a float operand refuses; the V1 rule and the V2 rule agree where they overlap |
| **C-B7** | `BuildDeclarationV1` (declaration) split from `EnvironmentInventoryObservationV1` (**captured observation**). Only the observation enters artifact and execution identity | a declaration is constructible without an inventory; identity moves when the observation moves, not when the declaration does |
| **C-B8** | The refusal vocabulary, including `POLICY_INTERVAL_UNSUPPORTED` | every new refusal has a name, a test and a message naming the remedy |

#### C · Policy contracts

| Task | Deliverable | Gate |
|---|---|---|
| **C-C1** | `derive_policy_occurrences(formula, bound_inputs)` from the authored selection · explicit authority ref · bound physical source · expression path · grain transition · **actual** need for conversion. `required_policy_kinds()` stays a compatibility helper | a country filter requires **no** reversal policy; an end-of-day balance requires **no** direction policy; **an operand whose C1 facts are not `monetary`/`per_row` requires no currency occurrence** *(A1)* |
| **C-C2** | Realization identity split — **family key** (`policy ref + dataset binding + environment/source`) · **revision** (immutable content hash) · **current pointer** (CAS, separate). Pilot revisions are **timeless**; a window intersecting a known change refuses `POLICY_INTERVAL_UNSUPPORTED`. **The mutable `eligibility_store` upsert is off the V2 path** — it clears prior confirmations | two revisions with identical executable fields produce one `ir_hash`; a pointer move does not; no V2 path writes through the upsert |
| **C-C3** | LLM admissibility (rule 16's four-way table) | source beats LLM with a **retained visible conflict**; two governed declarations refuse; two LLM-only refuse; one valid LLM-only is usable and marked `LLM_PROPOSED` |
| **C-C4** | Policy-literal evidence — `D`, `POSTED`, reversal flags, currency codes linked to bounded profile/source evidence where available, else `LLM_PROPOSED` and **not called evidence-validated** | a literal with no supporting evidence is labelled, not silently accepted |
| **C-C5** | Typed operator-subgraph requirements — **FX**: as-of join · duplicate-rate gate · missing-rate gate · optional inversion · decimal conversion · connected path. **LINKED_REVERSAL**: as-of row population · linkage · ambiguity gate · survivor operator · connected path | each requirement is a named constant with a test |
| **C-C6** | Topology-derived coverage validation over C-C5 (rule 3) | **deleting the duplicate-rate gate refuses even with `realizes_occurrences` intact on the multiplication operator** |

#### D · Verification and publication contracts

| Task | Deliverable | Gate |
|---|---|---|
| **C-D1** | `VerificationExecutionIdentityV1` — generated project hash · compilation and group identity · environment and inventory · `business_dt` · run parameters · input observations · `verification_check_set_hash`. **No publication attestation.** Decide `__verification_execution_hash` vs a versioned alias — `prepare_run` requires `capability_attestation_id` and generated outputs carry `__sandbox_execution_hash`, so identity, system columns, run parameters and persistence change **together** | the identity is constructible with **no attestation**; a V1 `sandbox_execution_hash` still requires one |
| **C-D2** | The versioned check set — result schema per check · non-null columns · feature-null rules from the output policy · spine completeness and uniqueness · join orphan/amplification · **which of the eight external requirements each check may satisfy** · the check-set hash | a keys/types check does **not** satisfy `JOIN_CONNECTIVITY`; the hash moves when any check changes |
| **C-D3** | `VerifiedOutputRevisionV1` incl. `verification_check_set_hash` and validator versions | a pass whose check-set hash predates the current validator reads **stale** |
| **C-D4** | Artifact file manifest **extending `GENERATED.lock`** *(A5)* — ordered path · SHA-256 · byte length · media type · immutable blob pointer · **byte verification before retrieval and before execution** | a file whose digest does not match is neither served nor executed; `generated_project_hash` is unchanged |
| **C-D5** | `OperatorExecutionProofV1` pins capability/subgraph signature and version · **compiler and renderer versions** · physical-type policy · topology requirement version · **gold corpus hash** · check-set version · **development runtime family and versions** (inventory records **eight**: hive, spark, metastore, python, java, pyspark, kedro, kedro_datasets). Environment compatibility is a **separate use-time decision** | a renderer version bump invalidates the proof; a development proof never satisfies a user verification |
| **C-D6** | Environment scoping on the two tables that decide what is current — **`group_binding`** (1034) and **`feature_active_revision`** (1055). `environment_id` is already first-class elsewhere in 1034 | either one declared sandbox environment, or both keyed `(environment_id, logical_group_name)`, asserted |
| **C-D7** | Group-name allocator — truncation · reserved suffix length · collision extension · **≤ 128 chars** (Hive) | a pathological base name still yields a legal, collision-free, deterministic name |
| **C-D8** | Active-revision **compare-and-set** — `publish_sandbox(verified_output_revision_id, expected_active_revision_id)`. 1055's trigger stops concurrent double-wins, **not** a stale verification published later | publishing an **older** verified output over a newer active revision refuses |
| **C-D9** | The §0.3 action matrix, frozen — and **`RECIPE_REVIEW_NOT_CURRENT` / `PROPOSED_METADATA_ONLY` / `EXECUTION_AUTHORITY_UNMET` removed from generate and verify**. Removing `require_confirmer` alone leaves a non-admin refused | a non-admin with `feature:generate` and AI-proposed metadata generates; the same identity cannot publish |

> **Acceptance (S0.5 as a whole):** every contract has a frozen schema with a pinned hash and a test
> that fails if a field is added without updating it; **`make test`, `make lint`, `make typecheck`
> green, and from `frontend/` `npm run typecheck`, `npm test`, `npx oxlint` green**; schema-2
> fixtures serialize **byte-for-byte** identically; V1 hashes, canonical bytes, signatures and
> contract types untouched; the `posted_debit_amount` blueprint derives a **direction-selecting**
> expectation and blueprint, fixture and expected rows agree in the **production ref format**;
> `EXPECTED_OUTCOMES` re-measured and re-pinned with the delta explained.

### S1 — deterministic V2 authoring

Anchored on `FeatureDefinitionV1` + `FeatureSelectionRevisionV1`, **which pins the approved target**
(rule 17). `govern.py:610`'s latest-by-name query gets a named remediation.

The output-naming algorithm · `BuildDeclarationV1` · the generalized work item (**migration +
backfill + compatibility reader**) · the deterministic producer emitting
**`ReviewedBlueprintBypassV2`** with no provider call and no fabricated critic event · V2 resolution
and admission (version-triple dispatch, terminal hash verification, `ResolvedFeatureInputV2`,
`AdmittedFeatureV2`, pair coherence, batch and name-collision handling).

**S1 emits `AuthoredOutputIntentV2` only — it does not certify an executable output policy.**

> **Acceptance:** four variants of one display name resolve to four features with four stable column
> names, and a label change moves none; a candidate outside the shadow top 12 authors and admits; a
> reviewed recipe produces a durable replayable result **with no provider call**; **a non-empty
> `currency_conversion_ref` yields an output INTENT, never a certified converted currency**; the
> selection pins `intent_id` and the target-reading hash; legacy work items read through the
> compatibility reader; V1 formula hashes pinned by frozen-bytes test.

### S2 — generate only, and the action matrix

**Narrowed [R14].** Revision 13 gave S2 verification and terminal lifecycle states while S8 owned the
worker, submission, business date, inventory and check DAG, and S9 owned publication — unsatisfiable
at S2. **S2 owns generation: render, seal, persist and serve `GeneratedArtifact`.**

The existing-vs-new table map · the three-command split · the neutral handoff shape · the
`SelectedBuildSet → DerivedGroup → GeneratedArtifact` hierarchy.

**The renderer's invariant is REPLACED, not deleted:** a project must write **either a published
target or a staging target**.

**The action matrix is implemented here, whole [R14]** — routes, `feature:generate`,
sandbox-verify and sandbox-publish permissions, **and** clearing `RECIPE_REVIEW_NOT_CURRENT`,
`PROPOSED_METADATA_ONLY` and `EXECUTION_AUTHORITY_UNMET` from the generate and verify paths.
Removing `require_confirmer` alone leaves a non-admin refused.

> **Acceptance:** a project renders and seals writing a staging target and **no** published target,
> while a project writing **neither** still refuses; a **non-admin with `feature:generate` and
> AI-proposed metadata generates successfully**; generation leaves no run event on the plane; the
> existing-vs-new map is committed. **Verification and publication assert at S8 and S9.**

### S3 — occurrence-addressed policy realization, with a producer

`PolicyRealizationRevisionV1` under S0.5-C's family/revision/pointer split. Pilot modes only;
others defined and refused by name. The typed booking-FX field set. The producer workflow under
rule 16.

**The producer needs a provider path nine stages before S12 [R14].** Either S3 carries **its own
provider smoke gate**, or S0's decisions include a **deterministic policy-realization seed or import
path**. S12 remains the *free-form formula-authoring* gate, not the first live test of policy
production.

`derive_policy_occurrences()` wired. Policy inputs enter the materialization contract.

> **Acceptance:** an unresolvable reference refuses by name; a formula omitting a required occurrence
> refuses; an unsupported reversal mode refuses and names it; a realization whose as-of rule would
> read post-cutoff FX refuses; **an operand whose C1 facts are not `monetary`/`per_row` requires no
> currency occurrence, and the plan says so rather than silently skipping it [A1]**; a country
> filter does **not** require a reversal policy; **a known
> effective policy change intersecting the window refuses `POLICY_INTERVAL_UNSUPPORTED`**; **source
> evidence beats a conflicting LLM proposal with the conflict retained and visible, while two
> conflicting governed declarations refuse** (rule 16); no V2 path writes through the mutable
> eligibility upsert.

### S4 — the bound V2 formula and its executable output policy

`BoundFormulaRevisionV2` — authored proposal, physical bindings, realization pins — **plus
`ExecutableOutputPolicyV2`**, resolved from actual realizations. **Expectation preservation is
re-checked here against the executable output.**

> **Acceptance:** a compiler version bump leaves the bound-formula hash unchanged; the artifact
> cannot be constructed with an unpinned realization or an unresolved semantic selection; **an
> authored intent whose executable policy resolves to a different currency or scale refuses rather
> than silently adopting it**.

### S5 — leakage gate, Gate 2, V2 IR, contracts and the partitioner

**Order matters [R14]:** V2 planning → complete read set → **`FullReadSetLeakageGateV2`** → **Gate 2
over the union** → `AuthorizedCompilationV2` → contract derivation and rendering, **both requiring
the token**.

`FormulaExecutionIRV2` with `realizes_occurrences`; `MaterializationContractV2` /
`FeatureGroupPlanV2` carrying `formula-v2/physical-types@1` and **only S4's executable output
policy**; `partition_contracts_v2()`; `group_by_contract()` keeps its single-result contract.

**The operator order is derived from S0's reversal mode**, since filtering status or direction first
can delete the record needed to neutralize the original.

> **Acceptance:** V1 IR canonical bytes and the single-contract path byte-identical; **a mutation
> replacing a policy status or direction column with the target ref refuses compilation**; removing
> the FX table from authorization while retaining it in the operator graph refuses; a set spanning
> two contracts yields two groups with deterministic collision-free names ≤ 128 chars; a re-approval
> that changes no executable field leaves `ir_hash` unchanged; a linked-reversal mode produces an
> order in which the reversal row survives to neutralize its original.

### S6 — persist and serve readable code

Nodes derived from the S5 subgraph. **Execute nothing. Publish nothing.** The **content-addressed
file manifest** under S0.5-D's byte contract, with **byte verification before retrieval and before
execution**.

Rule 3's proof is topology-derived coverage against the frozen operator-subgraph requirements.

> **Acceptance:** emitting a filter then aggregating the unfiltered input refuses; **an FX occurrence
> whose duplicate-rate gate is deleted refuses even with `realizes_occurrences` intact**; a file
> whose SHA-256 does not match its manifest entry is not served and not executed.

### S7 — Spark gold proof, and proof-aware readiness

**Extend `spark_semantics_gate.py` and `l0_gate.py`**, targeting
`deploy/kind/sandbox/Dockerfile.spark` via `LocalClusterSubmitter`.

**The mutations, enumerated [R14]:** wrong debit mapping · missing status filter · reversal
neutralization removed · post-cutoff FX accepted · quote inversion reversed · conversion moved after
aggregation · **duplicate-rate-gate deletion** · **a mid-window policy change that must refuse**.

**Readiness in-sequence:** the qualified, current evaluation-artifact reader · a proof-aware
capability resolver · the §0.3 action matrix's `renderer support` input.

**`OperatorExecutionProofV1` pins its world** (S0.5-D), and **environment compatibility is decided
separately at use time**. A development proof is never interchangeable with a user's sandbox
verification.

**Close here:** the DATE-clock refusal outside UTC, and A.32 🔴 `requirements.lock`.

> **Acceptance:** every case and mutation behaves; each proof names S0's frozen hash and its
> toolchain and runtime versions; **a renderer version bump invalidates the proof**; capability is
> computed from proofs, not dispatch; a stale evaluation artifact does not read as a pass.

### S8 — execution infrastructure and on-demand verification

**Infrastructure:** `business_dt` on the API · server-side Hive schema read · **captured
`EnvironmentInventoryObservationV1`** · the product's remote submission seam · a dedicated
materialization worker.

**S8 owns staging and verification end-to-end [R14]:** it creates `StagedGroupOutput`, runs the
S0.5-D check set as a DAG (`build + static → execute → ⟨output_sanity⟩ → fold`), and creates
`VerifiedOutputRevisionV1`. Advisory profiling is a separate non-blocking attempt.

**Input observation, labelled honestly:** either the generated project reads the exact manifest —
path, size, version/checksum — or the strength is `OBSERVED`, never `PINNED`. `UNPINNED` remains
valid and disclosed.

**Verification maps to requirements explicitly**, emitting `EXTERNAL_PASSED` only where the check's
result schema matches. A group pass must not turn every member `DATA-CHECKED`.

**Staleness is computed on read**; never re-run automatically.

> **Acceptance:** verification executes with **no publication capability present**; a verification
> survives restart with per-check results intact; every verification triple reaches a terminal state;
> changing any recorded input flips a pass to stale and names it; a keys/types check does not satisfy
> `JOIN_CONNECTIVITY`; observation strength is never labelled `PINNED` without enforced reads.

### S9 — atomic sandbox publication, under compare-and-set

Publish **only the exact verified output**; compare the staging manifest before promotion; **no
re-execution**. `publish_sandbox()` carries `verified_output_revision_id` **and**
`expected_active_revision_id`, and the pointer switch is CAS — 1055's trigger stops concurrent
double-wins, not a stale verification published later. **An intentional rollback is a different
action.**

**Decided:** generated code may remain unverified indefinitely; publication requires a current
passing verification. `UNPINNED` inputs may publish, labelled, never called reproducible or
source-current.

> **Acceptance:** **publishing an older verified output over a newer active revision refuses under
> CAS**; a verification whose check-set hash predates the current validator reads as stale; a changed
> or missing staging manifest blocks promotion; capability revoked between verify and publish blocks;
> a partial group never becomes visible.

### S10 — complete API and UI workflow

**Backend endpoints:** build-set creation and status · child-group status and refusals · **artifact
file listing and file content** · verify request and results · publish request and results.

**Generate → Verify → Publish** over the child-group hierarchy, showing selection · the derived group
split before verification · generated code · policy provenance with `LLM_PROPOSED` visible ·
verification state including observation strength · publish eligibility · that one failed member
blocks its group.

**The workspace ordering requirement:** after hypothesis submission the user's **goal, approved
target label, current stage and workflow output belong at the top of the workspace** — results still
render far below repeated intake panels.

> **Acceptance:** no path reaches execution without an explicit click; group scope visible before
> publish; a candidate outside the shadow top 12 is generable from the UI; results and stage sit
> above intake after submission; a stale result renders as stale, not failure.

### S11 — from one feature to the corpus, **generation only**

Batch **derive → compile → generate** across the derivable blueprints — **the count is S0.5's
re-measurement** — with a declared default `BuildDeclarationV1` set, reporting blueprints that cannot
be declared. Land the as-of snapshot window shape and measure its effect. Drive
`WINDOW_NOT_EVENT_ANCHORED` down as registry work.

**Verification and publication are NOT batched.**

> **Acceptance:** a coverage table with every refusal carrying a named code; **the batch triggers no
> execution**; undeclared blueprints reported, not defaulted.

### S12 — live free-form authoring gate, then incremental expansion

**Scoped [R14]:** S12 gates **free-form formula authoring**. Policy production is tested at S3.

The gate is representative of what is advertised when it runs, testing parsing · expectation
preservation · policy resolution · **refusal behaviour** · trace persistence. **Operator
dependency:** Anthropic billing must be topped up.

Then one family at a time, each needing renderer support **and** an execution proof: ratios →
offsets → composites → percentiles → slopes → allocation → future horizons.

> **Acceptance:** the gate exercises every currently-advertised family and asserts refusals as well
> as successes; the advertised set is `renderer-supported ∩ execution-proved`.

## 2. Carried forward

**Parallel, gated at merge:** behavioural frontend↔backend contract tests · hide the retired "Write
definitions" control · recognition correctness in full · ruff ratchet (79 repo-wide, 35 in `src/`).

**Narrow the leakage claim** — carried since revision 2 without its context; recover the original
finding before acting.

**Still required:** canonical typed planning-request JSON persisted, since `repr()` cannot
reconstruct a request.

**Deferred, explicitly:** cross-catalog V2 execution unless S0 decides otherwise · multi-environment
support unless S0.5-D chooses it · Iceberg-grade content snapshots · effective-dated policy history ·
operations beyond the pilot family.

## 3. Sequencing

```
selection + approved-target pin ─► deterministic V2 formula + provisional output intent
  ─► policy occurrence derivation and realization ─► bound formula + FINAL executable output policy
  ─► complete-read-set target-leakage gate ─► Gate 2 read authorization
  ─► V2 IR, contract and grouping ─► sealed, content-addressed generated code
  ─► user-triggered sandbox verification ─► exact-output CAS publication

S0 semantics ─► S0.5 frozen interfaces ⟨A · B · C · D⟩ ─► S1 authoring ─► S2 generate only
  ─► S3 policy ─► S4 bound + executable output ─► S5 leakage + Gate 2 + IR + grouping
  ─► S6 persist/serve ─► S7 gold proof ─► S8 verify ─► S9 CAS publish ─► S10 UI
  ─► S11 corpus ─► S12 live authoring gate
```

**What changed in revision 14.** Output authority splits: S1 emits an *intent*, S4 the *executable
policy*, because a non-empty ref currently yields `converted:<ref>` and certifies nothing. The
review bypass gets a **sum type** — `AuthoringResultV2` requires `critic_status` and cannot say the
critic did not run. Identity becomes three records, since revision 13's selection owned an
executable revision that did not yet exist. The approved target is pinned on the selection and a
**full-read-set leakage gate** runs before Gate 2, because policy resolution adds reads after the
only screening that exists. The action matrix moves into S2 whole, since removing the route guard
leaves `_contract_blockers` refusing anyway. S2 narrows to generation; S8 owns staging and
verification. Execution proofs pin their toolchain and runtime. And rule 16's conflict semantics are
no longer self-contradictory.

**What the audit changed.** Two blockers reached the stages, not just the findings list: S0 now
confirms the pilot's governed operand facts **before** any FX work is scheduled, because the
conversion tooth is silent without `unit=monetary` / `currency=per_row` and every FX task downstream
would test nothing; and S0's reconciliation gains a fourth axis, the ref namespace, because the
exemplar is written in a prefix that exists only in tests. Four accuracy errors were corrected
against the tree — a table name that does not exist, a Gate-2 union that is already half-built, a
file-hash discipline that already exists, and a nullability rule V2 should mirror rather than invent.

**S0.5 is folded in rather than spun out**, so a contract and the stage consuming it live in one
file and cannot drift. Its 31 tasks carry file paths, reserved migrations 1072–1079 and this repo's
own gate commands. **S1–S12 stay programme-level** and each needs its own task-level pass.

**No duration estimate.** Twelve revisions have now carried one that a review invalidated.

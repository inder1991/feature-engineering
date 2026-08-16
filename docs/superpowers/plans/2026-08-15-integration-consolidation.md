# V2 as the feature language, on shared execution

**Date:** 2026-08-16 · **Revision 17** — a ninth review found **13 blockers and 11 major gaps**. All
were verified against the tree, and an adversarial pass **corrected the review itself on three
points**. The stage graph is rebuilt to the reviewer's corrected order (**S0–S13**), because six
findings were ordering cycles: stages consuming records that later stages create.

> **S0.5 is task level; S1–S13 are programme level.** S0.5 carries file paths, reserved migrations
> and this repo's own gate commands. **S1–S13 are not executable as written**; each needs a
> task-level pass once S0.5 lands.

> **V1 remains the stable compatibility language. V2 becomes the intelligent, policy-aware language
> for all new features. LLMs create and enrich V2 formulas; a deterministic V2 compiler resolves
> their banking meaning and runs them through the existing Spark/Kedro materialization platform.**

**Do not reduce V2 to V1. Do not build a second V2 platform.** The V2 execution boundary is an
**adapter** onto language-neutral machinery.

## 0.0 Audit provenance

Revision 15 mechanically checked 39 §0.1 claims (33 held; 2 blockers, 4 accuracy errors). **Revision
17 verified 24 further findings**, each read against the code, then put every CONFIRMED verdict
through an adversarial refutation pass. **Three review claims were corrected by that pass:**

| Review claimed | Actually |
|---|---|
| Live free-form V2 authoring gets false tool results | **Both mechanical halves true, impact wrong.** `run_authoring_v2` is the non-production sibling with **zero callers outside tests**; production V2 goes through `run_authoring_v2_replay`, whose caller injects `recipe_tool_runner_v2`. **A latent trap in dead code — and, more importantly, there is NO free-form V2 authoring path in production at all.** |
| Request identity is incomplete | **PARTIAL.** The digest omission is real and **deliberate**, covered by a named test — `test_a_key_reused_for_DIFFERENT_DECLARATIONS_is_refused`, whose docstring states the declarations are not part of the request row's identity. A reused key with different declarations already **refuses**. |
| Planning request stored as `repr()` | **PARTIAL.** All three storage forms confirmed — but **neither is ever read back**; `load_frozen_option_facts` selects 18 columns and includes neither. It is dead weight, not a live corruption path. |

**Findings the verification made *stronger* than claimed:** `request_materialization` and
`execute_materialization` are **byte-identical in outcome** — no branch distinguishes them, so you
cannot *request* materialization unless the full execution ladder clears. `record_target_reading`
has **no guard on prior provenance**: it overwrites a `human_confirmed` reading and resets
`target_confirmed_at`, losing the previous signature. `inventory` is a **required keyword-only
parameter** on both `compile_feature_group` and `compile_ir` — compilation cannot be invoked without
it. And `authorize_compilation` has **exactly one production call site**, after the IRs are built.

## 0. Position

| | V1 | V2 |
|---|---|---|
| Role | frozen compatibility language | the language for all new features |
| Gets | correctness fixes only | policy awareness, multi-expression, advanced aggregation, LLM authorship |
| Users choose? | no — the platform decides internally | no — the UI never asks for a version |

**"V2" is the language; `formula_schema_version` is a wire pin** — schema 2 today, 3 after S0.5.

**There is no free-form V2 authoring path in production [R17].** The only v2 entry is the
recipe/expectation-bound worker. **S13 must BUILD the free-form path, not merely gate it** — every
prior revision assumed it existed and only needed a live test.

**Deferred, and not silently:** cross-catalog V2 execution · multi-environment support ·
Iceberg-grade snapshots · operations beyond the pilot family · **effective-dated policy history**.

## 0.1 Verified before planning

Baseline `29f3b8ac`; targeted suite 2,827 passed, 1 skipped. Focused re-run over formula-V2,
recipe-authoring, target-intake, activation, materialization-route and identity: **224 passed**.

**Six ordering cycles — stages consuming what later stages create [R17]**

- **Generation cannot precede its inputs.** Revision 16's S2 claimed to *render, seal, persist and
  serve* at position 2, while S3 had not resolved policies, S4 had no bound formula or executable
  output policy, S5 had no IR/contract/group, S6 *separately* claimed persistence of the same code,
  and S7 had not proved the renderer support Generate requires.
- **`derive_policy_occurrences(formula, bound_inputs)`** ran at S3 while bindings first appeared at
  S4.
- **A complete read set requires a planned operator graph.** Revision 16 ordered *read set → leakage
  → Gate 2 → build IR*. The compiler does the opposite and is right: `chain.py:576-587` builds
  `irs: list[FormulaExecutionIRV1]` and authorizes **those exact IRs** afterwards.
- **Selection preceded a definition it was said to require** — selection happens before authoring.
- **Inventory is captured too late.** `inventory: ClusterInventoryV1` is a **required keyword-only
  parameter** on `compile_feature_group` (`chain.py:472`) and `compile_ir` (`ir.py:295`).
- **`AuthoredOutputIntentV2` could not support its own test** — it carries unit, additivity,
  conversion-required and the ref, while S4 demanded refusal when *currency or scale* differs.

**A per-feature record cannot own a group identity [R17]**

- `CompilationIdentity` (`identity.py:136-162`) is `formula_content_hashes: tuple[str, ...]` ·
  `ir_hashes: tuple[str, ...]` · `materialization_contract_hash` · `group_plan_hash` — **plural,
  positionally paired across the whole group**. One feature can join different groups.
- Finer than that: `group_plan_hash` hashes `FeatureGroupPlanV1.identity_payload()`, which includes
  every member's identity — so the identity moves when *any* sibling changes.
- **And nothing durably maps a group to its members [R17].** `materialization_runs.py` says so
  outright: *"THE MEMBERS ARE SUPPLIED, BECAUSE NOTHING DURABLE MAPS THEM."* Membership is frozen
  only inside `resolved_input_digest` and the queue payload — readable by replaying a request, not
  by querying.

**Environment identity was described backwards [R17]**

- Artifact identity is **two phases**, and `environment_id` enters only the second:
  `CompilationIdentity` excludes it; **`sandbox_execution_hash` requires it** (`identity.py:552`,
  `"environment_id": _required(environment_id, "environment_id")`).
- So "only the inventory observation enters artifact and execution identity" is **false** — the
  declared `environment_id` enters the execution hash directly.

**The activation ladder is five actions and sixteen codes [R17]**

- `ACTIVATION_ACTIONS = ("save_idea", "create_contract", "author_formula",
  "request_materialization", "execute_materialization")`.
- **Sixteen** distinct blocker codes, ten of which apply at create_contract/author_formula:
  `BINDING_NOT_BOUND · PROPOSED_METADATA_ONLY · RECIPE_REVIEW_NOT_CURRENT ·
  CONCEPTUAL_PATTERN_NOT_AUTHORABLE · UOA_MISMATCH · PHYSICAL_PLAN_MISSING ·
  SNAPSHOT_STALE_REGENERATE · ACTIVATION_STATE_DRIFTED · FORMULA_NOT_REVIEWED ·
  FORMULA_SCHEMA_UNSUPPORTED · READINESS_NOT_MATERIALIZATION_READY ·
  EXTERNAL_VALIDATION_OUTSTANDING · SEMANTIC_AUTHORITY_INSUFFICIENT · EXECUTION_AUTHORITY_UNMET ·
  EXECUTION_AUTHORITY_UNEVALUATED · PERSONAL_DATA_POLICY_REQUIRED`.
- **`request_materialization` and `execute_materialization` are byte-identical in outcome** — both
  fall to the bare `else`. **Clearing three of sixteen was never going to work.**

**The approved target is mutable and catalog-blind [R17]**

- `record_target_reading` issues `UPDATE contract_intent SET target_...` — **in place, with no guard
  on prior provenance**: it overwrites a `human_confirmed` reading, resets `target_confirmed_at`, and
  loses the previous signature.
- It stores `target_ref` and **no `catalog_source`**, though the catalog **is known at both write
  sites** (`IntakeIn`/`IntakeTargetIn` carry it and use it for read-permission validation) and is
  deliberately dropped. Two catalogs holding the same `public.table.column` are indistinguishable.

**The V2 tool seam is a latent trap, not a live bug [R17]**

- `tools.py` is hard-wired to v1: `parse_proposal_v1` in `validate_draft_formula`, and
  `list_supported_operations` enumerates the v1 four.
- `run_authoring_v2` has **no `tool_runner` parameter at all** — fixing it needs a signature change —
  but it has **zero production callers**. Production goes through `run_authoring_v2_replay`, whose
  caller injects `recipe_tool_runner_v2`.
- **The live-facing risk is `run_authoring_v2_replay`'s `tool_runner: … | None = None` default** — a
  future caller that forgets to inject silently falls back to v1 tools.

**Everything else, re-confirmed:** the conversion tooth fires only on `unit=monetary` /
`currency=per_row` **and** refuses only on an empty ref, so a non-empty ref yields `converted:<ref>`
certifying nothing · `AuthoringResultV2` carries a mandatory three-member `critic_status` ·
`replay_trace` permits `OUTPUT_POLICY_RESOLVED` only after `CRITIC_COMPLETED` · leakage screens
`target_ref in derives` only · Gate 2 validates existence and permission, not leakage ·
`SpineSpec.read_set` is already unioned into Gate 2 · `parse_versioned` accepts 1 and 2 ·
`_plain_v2` walks every dataclass field · `PHYSICAL_TYPE_POLICY_VERSION = 4` hardcoded in two
builders · `FeatureGroupPlanV1.physical_type_policy_version: int` · `GENERATED.lock` carries
`generated_project_hash` · `empty_window` already decides V1 nullability · `group_binding` (1034) and
`feature_active_revision` (1055) lack an environment key while `environment_id` is first-class
elsewhere in 1034 · `next_revision_seq` is read-then-write · eight external validation requirements ·
`GATES_PASSED` is not terminal · `eligibility_store` is a mutable upsert that clears confirmations ·
`materialization_compiled_artifact` stores plan and contract, no source files ·
`_gold_evaluation_recorded()` returns `False` · `engine_capability` derives from renderer dispatch ·
inventory records eight runtime versions · `authored::` is test-only · the pilot's gold is three
disagreeing artifacts with `D`/`C` and no availability timestamps.

**Two standing instructions**

1. **Grep for the thing before designing it.** Eleven instances — the newest is G1, where the
   "missing" declaration fields turned out to be a deliberate design with a named test.
2. **Every acceptance criterion must be satisfiable with only what exists at its own stage.** Six
   violations this round; the stage graph below is rebuilt around it.

## 0.2 Invariants

1. No V2 formula executes with an unresolved policy reference.
2. **A non-empty policy string is not proof that the policy exists** — and no gate may certify an
   output on the strength of one.
3. **Coverage is derived from typed operator kinds, parameters and graph topology**, never
   self-attestation. `realizes_occurrences` is attribution.
4. No policy may disappear when V2 reuses existing operators.
5. **Identity is layered, and each layer owns only what it can [R17]:**

   | Layer | Owns |
   |---|---|
   | `ExecutableFeatureRevisionV2` | bound formula · executable output policy · binding set · realization-set hash · **per-feature IR hash** |
   | `DerivedGroupRevisionV2` | member executable revisions · materialization contract · group plan · **`CompilationIdentityV2`** |
   | `GeneratedArtifact` | rendered artifact identity · file manifest |

   *Formula identity* covers what was **authored**; **`ir_hash`** covers **resolved executable
   semantics** and excludes provenance and revision numbers.
6. **V1 formula hashes, IR canonical bytes, function signatures and contract types remain
   unchanged.**
7. **V2 wire schemas freeze once shipped** — per-version parser, model and canonical projection,
   dispatched together. Schema-2 fixtures tested **byte-for-byte**.
8. V1 and V2 share **language-neutral** publication and validation machinery through a V2 adapter.
9. The LLM proposes and explains; deterministic code executes.
10. The UI never asks a user to choose a formula version.
11. **"Supported" means generated code has been executed successfully**, against a capability
    signature pinning **the toolchain, the runtime and the exact generated gold-project hash** — a
    forgotten version bump must not preserve a proof across changed bytes.
12. **Required = declared = resolved = covered, occurrence-addressed and many-to-many.**
13. **The formula says WHAT; the policy says HOW.** `SemanticRowSelectionV1.semantic_value` is a
    **semantic token** (`debit`) — **never the physical literal `D`**, which belongs only in the
    realization and the resulting IR.
14. **One policy reference has one owner:** `AuthorityRefsV2`.
15. **Every physical read carries its own temporal semantics** — and **the declared T+N delivery
    promise is a SEPARATE contract field [R17]**, not derivable from a union of per-read
    knowledge-times.
16. **LLM-proposed realizations are usable, visibly.** Source beats LLM with a retained visible
    conflict; two governed declarations refuse; two LLM-only with no deterministic winner refuse; one
    valid LLM-only is usable as `LLM_PROPOSED`. Human review changes provenance and ranking, **not
    executability**.
17. **The approved target travels with the selection; a generation is authorized FOR a target
    [R17].** Code may be target-neutral; **the Generate *result* may not be.**
18. **Leakage's provable claim is narrow, and stated narrowly [R17].** The deterministic gate proves
    **direct target exclusion** using the canonical target plus resolved aliases, and enforces
    knowledge-time cutoffs. **It does not prove semantic proxies or target descendants are
    harmless.** Do not let it imply otherwise.

## 0.3 Three actions, three evaluators

| Action | Requirements |
|---|---|
| **Generate** | selected revision · complete binding · valid formula · resolvable policies · **renderer support proved (S8)** |
| **Verify** | the exact sealed artifact · execution permission · environment compatibility |
| **Publish sandbox** | a current passing verification · the exact staging output · publication permission and capability |

**Three NEW evaluators — `evaluate_generate`, `evaluate_verify`, `evaluate_publish_sandbox` — not a
mutation of the existing ladder [R17].** That ladder has five actions and sixteen codes, and its
last two rungs are byte-identical; clearing three codes was never sufficient. Each new evaluator
consumes only the records available at its stage. **None reads human semantic confirmation.**

## 0.4 Words that must not blur

**Execution proof** (S8) — development-time, mutation-tested, reusable, pinning toolchain, runtime
and gold-project hash. **Sandbox verification** (S9) — user-triggered, tied to the exact generated
artifact, **never interchangeable** with the development proof. **Publication** (S10) — atomic, CAS.

## 0.5 Migration ledger

Highest today is **1071**; prefixes collide at 0973, 0974, 1034, 1036, 1037, 1038, 1040. S0 records
the ledger; the stage that needs a table writes it.

| # | Table | Stage |
|---|---|---|
| 1072 | target-reading revision (append-only) · feature selection revision | S1 |
| 1073 | feature definition · authoring work item (generalized) + backfill · planning request | S2 |
| 1074 | generation inventory observation · bound input set revision | S3 |
| 1075 | policy realization revision + current pointer + conflict findings | S4 |
| 1076 | executable feature revision · derived group revision · generation authorization | S6 |
| 1077 | artifact file manifest | S7 |
| 1078 | operator execution proof | S8 |
| 1079 | verification request/attempt/result · verified output revision | S9 |
| 1080 | publication request/attempt | S10 |

## 1. The sequence

### S0 — pilot semantic and numeric decisions *(hard STOP: humans decide)*

**Confirm the pilot's governed operand facts FIRST.** Establish that `txn_amt` carries governed C1
facts of `unit=monetary` **and** `currency=per_row`. **Without them the conversion tooth never
fires**, no FX policy is required, resolved or consumed, and every downstream FX task tests nothing.
Either govern the facts, or record that the pilot proves status/direction/reversal only and move FX
to its own pilot.

**Reconcile FOUR axes** — timezone, boundary, length **and the ref namespace** (`authored::` is
test-only; production emits no prefix).

**Decide:** policy-reference namespace · window timezone, boundary and length · reversal-as-of
semantics · direction mapping (`D`/`C`) · population spine · FX join and cardinality · publication
rule — **plus the numeric decisions**: eligible status values and null handling · unknown-direction
behaviour · target currency · FX missing-rate behaviour · quote convention and inversion ·
**conversion before or after aggregation** · rate rounding.

**Author the expected rows** for one parameterized exemplar covering zero-eligible spine account ·
unknown-transaction account · post-cutoff reversal · duplicate and missing FX rates · post-cutoff FX
knowledge time — **with real availability timestamps**.

**Decide the FX branch and make it buildable:** a pinned same-catalog rate source, or explicit tasks
for a **reference-data join authorization** — not an entity bridge.

Also: the migration ledger.

> **Acceptance:** every decision has a record naming who decided; **`txn_amt`'s C1 facts are read and
> recorded, and a test asserts the pilot proposal WITHOUT a `currency_conversion_ref` refuses
> `CURRENCY_CONVERSION_UNDECLARED`**; the exemplar's refs are in the production format with no
> `authored::` surviving; expected rows stored and hashed with availability timestamps; each numeric
> decision appears in the frozen exemplar; the FX branch chosen and its tasks written.

### S0.5 — freeze the contracts and the identity graph *(task level)*

**Do not begin S1 until every task is green.** Eleven revisions each designed a stage against an
interface that did not fit.

**Gates, from this repo:** `make test` (`uv run pytest -q`), `make lint`, `make format-check`,
`make typecheck`. From `frontend/`: `npm run typecheck` (`tsc -b`, **never** `tsc --noEmit`),
`npm test` (`vitest run`), `npx oxlint`. Schemas follow
`src/featuregen/formula/proposal_v{n}.schema.json`.

#### A · Authoring contracts

| Task | Deliverable | Gate |
|---|---|---|
| **C-A1** | `proposal_v2.schema.json` **frozen**: own parser, model and canonical projection emitting the exact pre-change bytes. `_plain_v2` walks every dataclass field, so v2's projection ignores v3-only fields | every `gold_fixtures/*.json` and `gold_v2/*.json` serializes **byte-identically** to the committed file |
| **C-A2** | `proposal_v3.schema.json` + `parse_v3`; extend `parse_versioned` (accepts **1 and 2** today) | a v3 proposal parses; an unknown version still refuses loudly |
| **C-A3** | `SemanticRowSelectionV1(kind, role, semantic_value)` on **`AggregateExpressionV2`** and the expectation types. **`semantic_value` is a semantic token, never `D`** (rule 13); no `policy_ref` | a selection carrying a physical literal **refuses**; `UNAUTHORED_FILTER` behaviour unchanged |
| **C-A4** | Schema-3 coherence: a direction selection **requires** `AuthorityRefsV2.direction_policy_ref` | a selection without the ref refuses by name |
| **C-A5** | `ReviewOutcomeV2 = CriticExecutedV2(status, findings_hash) \\| ReviewedBlueprintBypassV2(blueprint_revision, expectation_hash)` with **V2-specific axes** — `AuthoringResultV2`'s `critic_status` cannot say "did not run" | a bypass round-trips **with no `CRITIC_COMPLETED` event and no `critic_status`** |
| **C-A6** | `REVIEW_BYPASSED` transition + checkpoint reconstruction; **bump disposition, orchestrator and replay protocol versions**; old readers still read old traces | an existing recorded trace replays unchanged |
| **C-A7** | **`AuthoredOutputIntentV2` gains what its test needs [R17]** — expected unit, additivity, conversion-required, declared ref **plus desired target currency and numeric shape/tolerance**; or the comparison targets a separately pinned S0 output requirement | S4's refusal compares only fields the intent **records** |
| **C-A8** | **Tool-seam repair [R17]**: `run_authoring_v2` gains a `tool_runner` parameter (it has none); **`run_authoring_v2_replay`'s `tool_runner=None` default is removed or made fail-closed**; the schema-3 matrix covers **tool behaviour** alongside prompt, turn schema, frozen configuration, replay restoration, candidate union, recipe egress, expectation schema and WORM trace | a v2 proposal through the shared tools **cannot** be reported invalid-because-v1; omitting a runner **refuses** rather than falling back |

#### B · Identity graph *(rebuilt [R17])*

| Task | Deliverable | Gate |
|---|---|---|
| **C-B1** | **`TargetReadingRevisionV1`, append-only** — catalog source · logical target ref · target type · horizon · provenance · **explicit target mode** · canonical content hash. Today `record_target_reading` UPDATEs in place with no provenance guard and drops `catalog_source` | a second reading creates a **new revision**; a `human_confirmed` reading is never silently overwritten; two catalogs with the same `public.t.c` are distinguishable |
| **C-B2** | **`FeatureSelectionRevisionV1` is the ROOT record** — immutable, created at selection, referencing an exact `TargetReadingRevisionV1`. **`FeatureDefinitionV1` is created or resolved later, at authoring**, with an immutable selection→definition link | a selection is constructible **before any definition exists**; the link is append-only |
| **C-B3** | **`ExecutableFeatureRevisionV2`** — bound formula · executable output policy · binding set · realization-set hash · **per-feature IR hash**. **It does NOT own `CompilationIdentityV2`** | a feature revision is reusable across two groups without changing |
| **C-B4** | **`DerivedGroupRevisionV2`** — member executable revisions · materialization contract · group plan · **`CompilationIdentityV2`**. **This is also the first durable group→member map [R17]:** there is no member table anywhere today — `materialization_request` carries the group NAME, `group_binding` maps name→contract hash, `group_plan_revision` records a plan HASH, and members are **supplied by the caller per trigger** (frozen only inside `resolved_input_digest` and the queue payload) | moving a sibling changes the **group** revision, not the member's; membership is readable without replaying a request |
| **C-B5** | **`GenerationAuthorizationRevisionV2`** — selection revision · **exact target-reading revision** · leakage-policy version · leakage verdict · **the planned IR hashes that verdict screened** · Gate-2 authorization. Code may be target-neutral; **the Generate result is not** (rule 17) | an artifact authorized for one target **cannot** be reused for another without re-screening |
| **C-B6** | **`BoundInputSetRevisionV2`**, produced **before** occurrence derivation — resolving the S3/S4 cycle | `derive_policy_occurrences` takes a bound input set that already exists |
| **C-B7** | **`GenerationInventoryObservationV1`, captured BEFORE binding** — `inventory` is a required keyword-only parameter on `compile_feature_group` and `compile_ir`. Pin **both `environment_id` and the inventory semantic-content hash**; `captured_at` is provenance only. **`environment_id` enters `sandbox_execution_hash`** — the earlier "only the observation enters identity" was wrong | compilation is unreachable without an inventory; identity moves when the observation or the environment moves |
| **C-B8** | **`VerificationInventoryObservationV1`** captured at verification, compared under an **explicit compatibility algorithm** — exact comparisons for Spark, Hive, PySpark, Kedro, kedro-datasets, Python, Java **and every physical input layout** | "environment compatible" has a deterministic answer with a test per dimension |

#### C · Execution and policy contracts

| Task | Deliverable | Gate |
|---|---|---|
| **C-C1** | `AuthorizedCompilationV2` · `MaterializationContractV2` · `FeatureGroupPlanV2` · `CompilationIdentityV2`; a V2 renderer entry point onto language-neutral machinery | V1 types, signatures and canonical bytes **unchanged** |
| **C-C2** | **`PlannedFormulaExecutionIRV2` FIRST**, then complete read set → leakage → Gate 2 → `AuthorizedCompilationV2` **wrapping those exact planned IRs**. **The IR is never rebuilt after authorization** — `authorize_compilation` has one production call site, after IRs are built | authorization names the same IR hashes the renderer consumes |
| **C-C3** | `FullReadSetLeakageGateV2` over formula operands and filters · policy reads · join keys · reversal and FX inputs · temporal and availability columns · spine reads. **Claim stated narrowly** (rule 18) | a mutation replacing a policy status/direction column with the target ref **refuses**; the docstring claims direct exclusion only |
| **C-C4** | Gate-2 union extended with **policy reads** — `SpineSpec.read_set` is already unioned | removing the FX table from authorization while keeping it in the graph refuses |
| **C-C5** | Per-read temporal semantics; **declared T+N availability promise kept as a SEPARATE contract field** (rule 15) | post-cutoff FX and post-cutoff reversal refuse; the promise is not derived from the union |
| **C-C6** | `formula-v2/physical-types@1` defining **`SUM(amount × booking_rate)` completely** — precision, scale, intermediate precision, rounding site, SUM growth, overflow, float refusal, nullability. **`empty_window` already decides V1 nullability** — mirror it | every row asserted; V1 and V2 agree where they overlap |
| **C-C7** | `derive_policy_occurrences(formula, bound_input_set)` replacing `required_policy_kinds()` wiring | a country filter needs no reversal policy; **an operand whose C1 facts are not `monetary`/`per_row` needs no currency occurrence** |
| **C-C8** | Realization identity — family key · **unique revision id PLUS a separate `executable_content_hash`** so a source proposal and an LLM proposal with identical semantics stay distinct · CAS current pointer · retained conflict findings. **Pilot realizations are explicitly TIMELESS; `POLICY_INTERVAL_UNSUPPORTED` and the mid-window refusal are REMOVED [R17]** — without effective-dating the platform cannot know an interval intersects | two proposals with identical semantics keep separate revisions and provenance; no test asserts mid-window detection |
| **C-C9** | LLM admissibility (rule 16); policy literals evidence-linked where available, else `LLM_PROPOSED` and **not called evidence-validated** | source beats LLM with a retained visible conflict; two governed refuse |
| **C-C10** | Typed operator-subgraph requirements — **FX**: as-of join · duplicate-rate gate · missing-rate gate · optional inversion · decimal conversion · connected path. **LINKED_REVERSAL**: as-of population · linkage · ambiguity gate · survivor operator · connected path. Plus topology-derived coverage | **deleting the duplicate-rate gate refuses even with `realizes_occurrences` intact** |
| **C-C11** | The **S4 LLM policy producer's seam** — registered output schema · bounded input contract · audited call · replay/idempotency record · egress fields. The existing LLM seam **fails closed on unknown fields** | a producer call replays; an unknown field fails closed, not silently |

#### D · Verification, publication and access

| Task | Deliverable | Gate |
|---|---|---|
| **C-D1** | `VerificationExecutionIdentityV1` — generated project hash · compilation and group identity · environment and inventory · `business_dt` · run parameters · input observations · `verification_check_set_hash`. **No publication attestation** | constructible with **no attestation**; V1's `sandbox_execution_hash` still requires one |
| **C-D2** | The versioned check set — result schema per check · non-null columns · feature-null rules from the output policy · spine completeness and uniqueness · join orphan/amplification · **which of the eight external requirements each may satisfy** · the check-set hash | a keys/types check does **not** satisfy `JOIN_CONNECTIVITY` |
| **C-D3** | `VerifiedOutputRevisionV1` incl. check-set hash, validator versions **and the pinned executable policy hashes**, re-compared against the family's current chosen semantics at verify **and** publish | **a policy that changed after verification makes the pass stale** |
| **C-D4** | Artifact file manifest **extending `GENERATED.lock`** — ordered path · SHA-256 · byte length · media type · immutable blob pointer · byte verification before retrieval **and** before execution | a mismatched digest is neither served nor executed |
| **C-D5** | `OperatorExecutionProofV1` pinning signature and version · compiler and renderer versions · physical-type policy · topology requirement version · gold corpus hash · **the exact generated gold-project hash** · check-set version · the eight runtime versions | **changed bytes invalidate the proof even if no version constant moved** |
| **C-D6** | Environment scoping on **`group_binding`** (1034) and **`feature_active_revision`** (1055) — `environment_id` is already first-class elsewhere in 1034 | one declared sandbox environment, or both keyed `(environment_id, logical_group_name)` |
| **C-D7** | Group-name allocator — truncation · reserved suffix · collision extension · **≤ 128 chars** | a pathological base still yields a legal, deterministic, collision-free name |
| **C-D8** | Active-revision **CAS** — `publish_sandbox(verified_output_revision_id, expected_active_revision_id)`; 1055's trigger stops concurrent double-wins, **not** a stale publish | publishing an **older** verified output over a newer active revision refuses |
| **C-D9** | **Three new evaluators** — `evaluate_generate` / `evaluate_verify` / `evaluate_publish_sandbox`, each consuming only its stage's records, **none reading human semantic confirmation**. **Do not mutate the five-action / sixteen-code ladder globally** | a non-admin with `feature:generate` and AI-proposed metadata generates; the old ladder's behaviour for its own five actions is **unchanged** |
| **C-D10** | **`BuildDeclarationV1` frozen** — cadence · availability promise · population spine · environment · parameter bindings · requested base name; **one declaration per derived group, or an explicit one-grain restriction**. Reconcile with the **deliberate** request-identity split, whose named test `test_a_key_reused_for_DIFFERENT_DECLARATIONS_is_refused` already refuses a reused key with different declarations | a build set spanning two grains is refused or split, never silently merged; the existing test still passes |
| **C-D11** | **Canonical typed `FeaturePlanningRequestV1` persistence and a validated reader.** Today the option decision stores a planning hash, `repr()` parameter values and an unregistered `asdict()` copy — and **nothing reads any of them** | a request round-trips typed; **legacy rows refuse by name rather than being reconstructed from `repr`** |
| **C-D12** | An explicit **`EXPLORATION` target mode** (rule 17) so corpus generation has a target model — *"no prediction target"* is a **variant**, never `NULL` interpreted per reader | a corpus selection carries `EXPLORATION` and is distinguishable from a missing target |

> **Acceptance (S0.5):** every contract has a frozen schema with a pinned hash and a test that fails
> if a field is added without updating it; **`make test`, `make lint`, `make typecheck` green, and
> from `frontend/` `npm run typecheck`, `npm test`, `npx oxlint` green**; schema-2 fixtures serialize
> **byte-for-byte** identically; V1 hashes, canonical bytes, signatures and contract types untouched;
> the `posted_debit_amount` blueprint derives a direction-selecting expectation and blueprint,
> fixture and expected rows agree in the production ref format; `EXPECTED_OUTCOMES` re-measured and
> re-pinned with the delta explained.

### S1 — immutable selection + target-reading revision
The root records. A selection pins an exact `TargetReadingRevisionV1`; no definition is required yet.
> **Acceptance:** a selection exists with no definition and no executable revision; a re-read of the
> target creates a new revision and the old one stays readable.

### S2 — deterministic V2 authoring + provisional output intent
The generalized work item (migration + backfill + compatibility reader) · the output-naming algorithm
· the deterministic producer emitting `ReviewedBlueprintBypassV2` with **no provider call and no
fabricated critic event** · V2 resolution and admission · **`AuthoredOutputIntentV2` only** ·
canonical planning-request persistence. `FeatureDefinitionV1` is created or resolved here and linked.
> **Acceptance:** a candidate outside the shadow top 12 authors and admits; a non-empty
> `currency_conversion_ref` yields an **intent**, never a certified converted currency; V1 formula
> hashes pinned by frozen-bytes test.

### S3 — capture generation inventory + bound input set
`GenerationInventoryObservationV1` **before** binding; `BoundInputSetRevisionV2`.
> **Acceptance:** binding is unreachable without an inventory; the bound set is addressable
> independently of any policy.

### S4 — derive policy occurrences + resolve realizations
`derive_policy_occurrences(formula, bound_input_set)` · `PolicyRealizationRevisionV1` under C-C8 ·
pilot modes only, others refused by name · the typed booking-FX field set · the producer seam
(C-C11) · dual authority with `eligibility_store` resolved.
> **Acceptance:** an unresolvable reference refuses by name; an operand whose C1 facts are not
> `monetary`/`per_row` needs no currency occurrence; **no test asserts mid-window policy detection**;
> no V2 path writes through the mutable upsert.

### S5 — bound formula + final executable output policy
`BoundFormulaRevisionV2` + `ExecutableOutputPolicyV2`; expectation preservation re-checked.
> **Acceptance:** an intent whose executable policy resolves to a different **recorded** field
> refuses; a compiler version bump leaves the bound-formula hash unchanged.

### S6 — planned IR → read set → leakage → Gate 2 → contracts → derived groups
`PlannedFormulaExecutionIRV2` · complete read set · `FullReadSetLeakageGateV2` ·
`AuthorizedCompilationV2` wrapping those exact IRs · `MaterializationContractV2` /
`FeatureGroupPlanV2` · `partition_contracts_v2()` · `GenerationAuthorizationRevisionV2`. Operator
order derived from S0's reversal mode.
> **Acceptance:** the IR is not rebuilt after authorization; a policy column swapped for the target
> ref refuses; a set spanning two contracts yields two groups; V1 bytes and the single-contract path
> byte-identical.

### S7 — render, seal, persist and serve generated code *(internally)*
Rendering and artifact persistence happen **once, here**. The content-addressed manifest (C-D4).
Topology-derived coverage proof. **Not yet user-facing.**
> **Acceptance:** deleting the FX duplicate-rate gate refuses with `realizes_occurrences` intact; a
> mismatched digest is neither served nor executed.

### S8 — development gold proof, capability proofs, then enable Generate
Extend `spark_semantics_gate.py` and `l0_gate.py` against
`deploy/kind/sandbox/Dockerfile.spark`. Mutations: **wrong debit mapping · missing status filter ·
reversal neutralization removed · post-cutoff FX accepted · quote inversion reversed · conversion
moved after aggregation · duplicate-rate-gate deletion**. Mint `OperatorExecutionProofV1` (C-D5).
The qualified evaluation-artifact reader and proof-aware capability resolver. **`evaluate_generate`
ships here — the user-facing Generate action is enabled only now.**
> **Acceptance:** every case and mutation behaves; changed bytes invalidate a proof without a version
> bump; capability is computed from proofs, not dispatch.

### S9 — on-demand sandbox verification
Infrastructure (`business_dt`, server-side Hive read, remote submission seam, dedicated worker) ·
`StagedGroupOutput` · the check DAG (`build + static → execute → ⟨output_sanity⟩ → fold`; profiling
separate and non-blocking) · `VerifiedOutputRevisionV1` · `evaluate_verify` · observation strength
`OBSERVED`/`UNPINNED`, **never `PINNED` without enforced reads**.
> **Acceptance:** verification executes with **no publication capability present**; a changed input
> flips a pass to stale and names it; a policy changed after verification makes the pass stale.

### S10 — exact-output CAS publication
Publish only the exact verified output; compare the staging manifest; **no re-execution**; CAS on
`expected_active_revision_id`; `evaluate_publish_sandbox`. `UNPINNED` may publish, labelled, never
called reproducible or source-current.
> **Acceptance:** an older verified output over a newer active revision refuses; a partial group
> never becomes visible.

### S11 — complete API and UI workflow
Endpoints: build-set creation and status · child-group status and refusals · artifact file listing
and content · verify request/results · publish request/results. Generate → Verify → Publish over the
child-group hierarchy, showing the derived group split before verification, policy provenance with
`LLM_PROPOSED` visible, observation strength, and that one failed member blocks its group. **After
submission the user's goal, approved target label, current stage and workflow output belong at the
top of the workspace.**
> **Acceptance:** no path reaches execution without an explicit click; results and stage sit above
> intake; a candidate outside the shadow top 12 is generable from the UI.

### S12 — corpus generation *(generation only)*
Batch **derive → compile → generate** across the derivable blueprints — **the count is S0.5's
re-measurement** — under an explicit **`EXPLORATION` target mode** (C-D12) and a declared default
`BuildDeclarationV1` set. Land the as-of window shape and measure its effect. **Verification and
publication are NOT batched.**
> **Acceptance:** a coverage table with every refusal named; **the batch triggers no execution**;
> undeclared blueprints reported, not defaulted.

### S13 — build free-form V2 authoring, then expand
**There is no free-form V2 path in production — S13 BUILDS it**, then gates it live. The gate tests
parsing · expectation preservation · policy resolution · **refusal behaviour** · trace persistence,
covering every family advertised when it runs. **Operator dependency:** Anthropic billing.
Then one family at a time, each needing renderer support **and** an execution proof.
> **Acceptance:** a free-form V2 run reaches admission through the **v2** tool seam; the advertised
> set is `renderer-supported ∩ execution-proved`.

## 2. Carried forward

**Parallel, gated at merge:** behavioural frontend↔backend contract tests · hide the retired "Write
definitions" control · recognition correctness in full · ruff ratchet (79 repo-wide, 35 in `src/`).

**Narrow the leakage claim** — carried since revision 2 without its context; recover the original
finding before acting. *(Rule 18 now states the narrow claim independently.)*

**Deferred, explicitly:** cross-catalog V2 execution · multi-environment support · Iceberg-grade
content snapshots · **effective-dated policy history** · operations beyond the pilot family.

## 3. Sequencing

```
S0 semantics ─► S0.5 contracts + identity graph ⟨A · B · C · D⟩
  ─► S1 selection + target reading ─► S2 authoring + output intent
  ─► S3 inventory + bound input set ─► S4 occurrences + realizations
  ─► S5 bound formula + executable output policy
  ─► S6 planned IR → read set → leakage → Gate 2 → contracts → groups
  ─► S7 render/seal/persist/serve ⟨internal⟩ ─► S8 gold proof + proofs ⟨Generate enabled here⟩
  ─► S9 verify ─► S10 CAS publish ─► S11 UI ─► S12 corpus ─► S13 build free-form + expand
```

**What changed in revision 17.** Six ordering cycles are gone: Generate no longer precedes the IR,
contract and group that make rendering possible, and is enabled only at S8 once renderer support is
**proved**; the bound input set precedes occurrence derivation; the planned IR precedes the read set
and Gate 2, matching the compiler's one production call site; selection is the root record and the
definition is created at authoring; inventory is captured before binding, because it is a required
parameter of compilation; and the authored output intent now records the fields its own test
compares. Identity is layered — a per-feature revision cannot own a group-wide
`CompilationIdentity` — and a new generation-authorization envelope binds a target-neutral artifact
to the exact target it was screened for. The activation work becomes three new evaluators rather
than clearing three of sixteen codes on a ladder whose last two rungs are byte-identical. The
mid-window policy refusal is **withdrawn** as unimplementable without effective-dating. And S13
**builds** free-form V2 authoring, which does not exist in production — every earlier revision
assumed it did.

**No duration estimate.** Thirteen revisions have now carried one that a review invalidated.

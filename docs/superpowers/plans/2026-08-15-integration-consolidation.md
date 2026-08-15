# V2 as the feature language, on shared execution

**Date:** 2026-08-16 · **Revision 10** — a fourth review found **four blockers and five major gaps**,
all validated. Revision 9's *sequence* survives; its **invariants did not**. Rules 3, 5 and 11 were
each written to close a real hole, and two of them are **unimplementable as stated against the IR
that exists**. This revision fixes the invariants and names the execution target the plan had been
describing incorrectly since revision 6.

> **V1 remains the stable compatibility language. V2 becomes the intelligent, policy-aware language
> for all new features. LLMs create and enrich V2 formulas; a deterministic V2 compiler resolves
> their banking meaning and runs them through the existing Spark/Kedro materialization platform.**

**Do not reduce V2 to V1. Do not build a second V2 platform.** Revision 4's "lowering" stays
withdrawn — it destroys policy identity. Reuse V1's **execution machinery**; never V1's **artifact**.

**Scope discipline:** narrow the first executing vertical slice to semantics provable end-to-end,
while preserving the whole V2 language and its expansion roadmap.

## 0. Position

| | V1 | V2 |
|---|---|---|
| Role | frozen compatibility language | the language for all new features |
| Gets | correctness fixes only | policy awareness, multi-expression, advanced aggregation, LLM authorship |
| Users choose? | no — the platform decides internally | no — the UI never asks for a version |

**Shared machinery changes, which are neither [R10].** Two changes touch code V1 runs through, and
"correctness fixes only" was false without this row: S2 splits `compile_feature_group` into three
commands, and S5 changes `group_by_contract` from **refuse** to **split**. Both must preserve V1
semantics exactly and are covered by rule 6. Blast radius is small — `group_by_contract` has exactly
one caller (`contract.py:779`) — but it is not zero, and pre-live is the reason there is no rollback
flag, not an argument that none is needed.

**Two-speed default.** *Deterministic recipe-driven V2 generation* may ship at S1. **Free-form
LLM-authored V2 must not become the default until S12's live-provider acceptance passes** —
`replay_authoring_v2` records that every run driving it in the suite is a recorded fixture.

## 0.1 Verified before planning

Re-checked against this tree. **Baseline `29f3b8ac`; targeted suite 2,827 passed, 1 skipped.**

**The IR already has an identity principle, and revision 9's rule 5 contradicted it [R10]**

- `ExpressionExecutionIR.identity_payload()` contains `expr_path · physical_read_set · join_steps ·
  pit · input_requirements · aggregation · filter_tree`.
- It **deliberately excludes** `non_physical_refs` and `operand_type`, and states why: identity
  covers *what it computes*, not *why it was allowed*; including an explanatory field "would make a
  projection blip change `ir_hash` and fire §9's `IR_HASH_MISMATCH` gate against a computation nobody
  touched." The join plan likewise contributes its **steps** and not its authority columns or
  `roles_used`.
- **`filter_tree` is `Mapping[str, Any] | None`** — an opaque dict. Nothing in the IR records *why*
  a filter exists.

**A Spark execution target exists — the "no Java" claim carried since revision 6 was wrong [R10]**

- `deploy/kind/sandbox/Dockerfile.spark` installs `openjdk-17-jre-headless`, beside `spark.yaml`,
  `spark-thrift.yaml` and `up.sh`.
- `LocalClusterSubmitter` is "a local `kedro` run in the environment that has the engines", refusing
  to start without `PYSPARK_PYTHON` and `PYSPARK_DRIVER_PYTHON`. It is a legitimate mechanism, not a
  stub. What the *backend* image deliberately lacks is pyspark in the app's site-packages — a
  different fact that revision 6 turned into "no Java" and four revisions repeated.
- **The execution-proof harness already exists**: `tests/featuregen/materialize/spark_semantics_gate.py`
  and `l0_gate.py`, deliberately not named `test_*` so the main suite skips them, already run
  generated artifacts on a real JVM with a documented invocation.

**Activation changes have a blast radius [R10]**

- `AUTHORITY_MATRIX` content is part of `authority_matrix_hash`, and growing it "MOVES the policy
  hash by design (frozen options pinned to the old hash surface `ACTIVATION_STATE_DRIFTED` and
  regenerate)."

**The authoring and admission path is structurally V1-only**

- `AuthoringResultV2` (`result_v2.py:76`) folds `TypedFormulaProposalV2` + `FormulaOutputPolicyV2`;
  `replay_authoring_v2.py:209` has its own terminal restorer.
- `resolve.py:48` imports only the V1 restorer and `AuthoringResult`; `admission.py:156` types
  `result: AuthoringResult`, `:175` types `formula: TypedFormulaV1`, `:228` refuses v2 by name.
- **`recipe_formula_shadow_work_item` is the ONLY durable intent anchor** — `resolve.py` reads it
  directly and documents it as the columns "that carry an authoring intent".
- **The V2 worker always enters LLM replay** (`if authors_v2: run_authoring_v2_replay(...)`). There
  is **no deterministic authoring producer.**

**Grouping cannot be known early**

- `derive_contract(conn, ir: FormulaExecutionIRV1, ...)` takes **the IR**; classification consumes
  the complete physical read set. `group_by_contract` **refuses** more than one contract.

**The pilot's "gold" is three disagreeing artifacts, and the exemplar cannot reproduce its own answer**

| | timezone | boundary | length |
|---|---|---|---|
| derived blueprint (`recipe_formula_blueprint_derivation.py`) | **UTC** | `(start, end]` | recipe |
| V2 proposal fixture (`gold_v2/30_posted_debit_amount_exemplar.json`) | **Asia/Dubai** | `[start, end)` | **90 d** |
| execution gold (`recipes/gold/posted_debit_amount_expected.json`) | — | `(start, end]` | **30 d** |

**The decisive one:** the ledger stores `direction` as **`D`** (11 rows) / `C` (1 row) while the V2
fixture's predicate is `direction == "debit"`. **Rendering it literally returns zero rows — not
`ACC1=300.00` / `ACC2=93.50`.** The exemplar cannot reproduce its own hand-computed result. A
direction policy must *map* the semantic notion to the source's encoding; rendering the raw authored
predicate is precisely the bug.

Missing gold cases: a spine account with **zero** eligible rows · an unknown-transaction account · a
reversal arriving **after** the cutoff · duplicate and missing FX rates · an FX record whose
**knowledge time** is after the cutoff.

**Everything else confirmed**

- `PitSpec` (`expression_ir.py:284`) governs the expression window, **excludes** `empty_window` and
  `null_input`, and has no offset or future-horizon representation.
- Input snapshot ids hash requirement, ordered partitions and business date — **not content**.
- **`GATES_PASSED` is not terminal**: `{GATES_FAILED, PUBLISHED, PUBLICATION_REFUSED, RUN_FAILED}`.
- **Eight external validation requirements**: `ADDITIVITY_SUPPORTS_OPERATION · CURRENCY_CONSISTENT ·
  GRAIN_IS_UNIQUE · JOIN_CONNECTIVITY · TEMPORAL_IS_POPULATED · TEMPORAL_LAG_BOUNDED ·
  TYPE_IS_NUMERIC · UNIT_CONSISTENT`.
- `sandbox_execution_hash` requires a non-defaulted `capability_attestation_id`.
- `compile_feature_group` renders → validates → executes → publishes continuously, keyed by
  `logical_group_name`.
- `govern.py:610` selects `WHERE feature_name = %s ORDER BY version DESC LIMIT 1`.
- `api.ts:4190` has only a GET; the POST takes shadow work-item ids capped at 12.
- `required_policy_kinds()` has **zero production callers**.
- `_manifest_lines` is *"counts, hashes and locations only"*.
- `resolve_physical_type` accepts `TypedFormulaV1` only.
- `engine_capability` derives from renderer dispatch, not execution evidence.
- `banking_policies.py` registers kinds; `eligibility_store.py` is keyed `(catalog_source, table)`,
  flag/code-only reversal; **currency and allocation have no store**.
- `business_dt` appears nowhere in `api/routes/materialization_runs.py`.
- Cross-catalog execution refuses at `chain.py:906`; migration prefixes collide at **0973, 0974,
  1034, 1036, 1037, 1038, 1040**.
- DEFERRED-WORK: A.32 🔴 `requirements.lock`; DATE clocks outside UTC refuse (PIT has no clock type).

**Standing instruction: check what exists before designing what to add.** This has now happened
**five times** — `PitSpec`, `VERIFICATION_STAMPS`, the `engine_capability` precedent, the work item,
and now the Spark gates. Revision 9 added the instruction and violated it in the same document, and
its own count of "four" was itself wrong. **Before writing any stage, grep for the thing first.**

## 0.2 Invariants

1. No V2 formula executes with an unresolved policy reference.
2. **A non-empty policy string is not proof that the policy exists.**
3. **Every resolved policy occurrence is consumed, proved at generation time by typed DAG
   connectivity plus total dispatch** — never by a reported hash. The consumed operator must lie on
   the path from scan to the emitted aggregate, and every occurrence must be dispatched. **The proof
   runs over the plan and the emitted code; it is not a hash comparison. [R10]**
4. No policy may disappear when V2 reuses existing operators.
5. **Resolved executable semantics enter identity; provenance and revision numbers do not. [R10]**
   Revision 9 said "policy revisions and physical bindings enter executable identity", which
   contradicts the IR's own principle and would fire `IR_HASH_MISMATCH` on a re-approval that changes
   no rows. The split:

   | Identity-bearing — **changes rows** | Recorded, **not hashed** — explains |
   |---|---|
   | status column, eligible values, null behaviour | producer, confidence, evidence, observed values |
   | direction column, debit values, normalization | model and prompt version |
   | reversal mode and its columns/linkage | approval state, who confirmed, when |
   | rate table, keys, effective timestamp, booking-vs-settlement, rate direction, rounding | **the realization revision id itself** |
   | allocation method and its columns | lineage narrative |

   **Two realization revisions that resolve to the same executable fields produce the same
   `ir_hash`.** Which revision was used is recorded on the compilation and execution records — the
   existing pattern, where the join plan contributes its *steps* to identity while authority columns
   and `roles_used` are recorded elsewhere.
6. V1 formula hashes and V1 IR canonical bytes remain unchanged.
7. V1 and V2 share publication and validation machinery.
8. The LLM proposes and explains; deterministic code executes.
9. The UI never asks a user to choose a formula version.
10. **"Supported" means generated code has been executed successfully**, recorded against a
    **capability signature** — not an operator name.
11. **Required = declared = resolved = consumed, occurrence-addressed**, where an occurrence is
    `expression_path + physical source binding + policy kind + semantic role`. Kind-level sets fail
    for ratio, difference and composite formulas: two expressions may each need their own status or
    reversal realization, and one must not satisfy both by sharing a kind.

    **Every V2 IR operator carries the `policy_occurrence_id` it realizes [R10].** Without this rule
    11 is unprovable: `filter_tree` is an opaque `Mapping[str, Any]`, so a generic filter cannot be
    told apart from any other filter by connectivity alone. The occurrence id is **explanatory**
    (rule 5's right column) — it makes the proof possible without making a re-approval change the
    hash.

**Rule 10's bootstrap:** an operator's first execution happens on **S7's development gold path,
outside the product**. Nothing is advertised to a user in order to become executable.

## 0.3 Division of labour

**LLM:** understand the hypothesis · select recipes · propose candidates · map roles to concepts ·
recommend physical columns · interpret source-specific status descriptions · propose debit/credit
conventions and reversal representations · identify currency and rate columns · produce structured V2
formulas · critique · explain refusals · summarise results.

**Deterministic code:** validate structure · bind columns · **derive the required policy set** ·
resolve realizations · check executability · compile the plan · generate Spark · execute · validate ·
publish. **The LLM never injects free-form SQL.**

**Reviewed recipes instantiate deterministic V2 blueprints directly** — never ask a model to restate
arithmetic the registry already holds.

**Activation becomes consequence-specific**, replacing the single `execute_materialization` gate:

| Action | `llm/proposed`, deterministically validated |
|---|---|
| `generate_code` | **may clear** |
| `verify_in_sandbox` | **may clear** |
| `publish_sandbox` | **own policy — does not inherit the above** |

**This change is not free, and revision 9 did not say so [R10].** `AUTHORITY_MATRIX` feeds
`authority_matrix_hash`; growing it moves the hash **by design**, and **every frozen option pinned to
the old hash surfaces `ACTIVATION_STATE_DRIFTED` and regenerates.** Sequence it as a deliberate
migration with the regeneration expected and counted — not as a table edit.

**Hard contradictions, leakage, unsupported fan-out and missing semantics still refuse** at every
level: those are calculation problems, not governance preferences.

## 0.4 Three words that must not blur

**Execution proof** (S7) — development-time, mutation-tested, against reviewed gold. **Sandbox
verification** (S8) — user-triggered, against the exact sealed artifact on real data. **Publication**
(S9) — atomic, group-wide, promoting *exactly the verified bytes*.

**Never re-run generic gold fixtures for a user verification** without a tested input-remapping
mechanism.

## 0.5 Migration ledger, and allocation [R10]

Prefixes collide seven times, so **S0 builds a ledger with a CI uniqueness test**. Revision 9 then
listed eight storage needs and allocated nothing; **each stage allocates against the ledger at the
point it needs storage**, and records the number in this file when it does:

| Stage | Storage |
|---|---|
| S1 | generalized authoring work item · exact selected-feature identity and build mapping |
| S2 | the three request/attempt/result triples *(see the existing-vs-new map below)* |
| S3 | policy realization revisions |
| S7 | operator execution proofs |
| S8 | verification requests/attempts/results · verified output revisions · profiles |
| S9 | publication requests/attempts |

**Existing vs new must be stated, not implied.** `materialization_request`,
`materialization_generation`, the compiled-artifact and run-event tables already exist and carry
meanings that fit parts of S2's triples. **S2's first task is the explicit map** — which of the nine
names are renames of existing tables, which are new. **No parallel `FeatureBuild*` tables without
it.**

## 1. The sequence

### S0 — freeze the complete pilot truth *(hard STOP: humans decide)*

```
enumerate contradictions → governance/product decision → decision record
   → reconcile blueprint, fixture, execution gold and expected rows → freeze hash
```

**Reconcile all three artifacts.** Decide: policy-reference namespace · window timezone, boundary and
length · **reversal-as-of semantics** · **direction mapping (`D`/`C` ↔ debit/credit)** · population
spine · **FX join and cardinality** · publication rule.

**Replace the gold corpus with one coherent parameterized exemplar**, extended to the missing cases
in §0.1.

**Decide the FX catalog question.** The pilot joins booking-date FX while cross-catalog execution
refuses by construction. Either **prove and pin a same-catalog rate source**, or bring a bounded
reference-data join authorization into the pilot.

Also: the migration ledger.

> **Acceptance:** each contradiction has a decision record naming who decided; all three artifacts
> agree; the frozen exemplar is stored and hashed; **the frozen predicate returns the hand-computed
> rows against the ledger's actual `D`/`C` encoding**; the ledger's CI test fails on the seven
> existing collisions unless explicitly grandfathered.

### S1 — exact selection and durable authoring

**Exact feature identity** (`feature_definition_key` / `revision` / `executable_revision_id` /
`output_column_name` / `display_label`), applied **at contract creation**, reusing existing
suggestion and option identities.

**Generalize the work item — do not delete it.** It is the only durable bridge into materialization.
Generalize to **`formula_authoring_work_item`**; the requirement is:

> Generation must not depend on shadow **ranking**, the **top-12 cap**, or **opportunistic capture**.
> A selected option creates the same kind of sealed work item directly.

**Stable output-column naming** — four exact identities can still collide on one
`posted_debit_amount` Hive column.

**A deterministic authoring producer** — binds the reviewed blueprint → constructs the proposal
deterministically → runs the **same** structural, output-authority and critic gates → writes the
**standard V2 replay trace** → yields an ordinary `AuthoringResultV2`. **No second admission
shortcut.**

**V2 resolution and admission, as explicit tasks [R10 — new in r9, unmarked then]:**
version-dispatched trace restoration · V2 terminal hash verification · `ResolvedFeatureInputV2` ·
`AdmittedFeatureV2` · proposal/output-pair coherence · shared batch and name-collision handling. V1
admission byte-untouched.

> **Acceptance:** four variants of one display name resolve to four features with four stable column
> names; a candidate outside the shadow top 12 authors and admits; a reviewed recipe produces a
> durable replayable `AuthoringResultV2` with **no provider call**; a **frozen-bytes test pins
> existing V1 formula hashes** (rule 6).

### S2 — split actions and lifecycle contracts

**First task: the existing-vs-new table map** (§0.5). Then `compile_feature_group` becomes three
commands:

| Command | Does | Must not |
|---|---|---|
| `generate_code()` | render, seal, store | execute, publish |
| `verify_in_sandbox()` | execute the sealed artifact, record results | publish |
| `publish_sandbox()` | promote **exactly the verified output** | execute |

**Three request/attempt/result triples**, because `GATES_PASSED` is not terminal and a
verified-but-unpublished run would never close:

```
BuildRequest        → BuildAttempt        → GeneratedArtifact
VerificationRequest → VerificationAttempt → VerifiedOutputRevision
PublicationRequest  → PublicationAttempt  → ActiveRevision
```

**`VerificationExecutionIdentityV1`**, separate from `sandbox_execution_hash`, which requires a
`capability_attestation_id` and cannot honestly identify an execution with no publication intention.

**Validated against the V1 path, then re-validated for V2 [R10].** Revision 9's acceptance asserted
V2 generation and verification behaviour at a stage where neither exists — the same defect as
revision 8's artifact-before-resolvers. **S2 proves the split on V1, which runs today; the V2
assertions belong to S6 and S8.**

> **Acceptance (at S2, on V1):** generation leaves no run event on the plane; verification executes
> with no publication capability present; every triple reaches a terminal state; the existing-vs-new
> map is committed. **Re-asserted for V2 at S6 and S8.**

### S3 — minimal executable policy realization, **with a producer**

**`PolicyRealizationRevisionV1`** — immutable, keyed by `abstract policy ref + physical dataset
binding + environment/source + effective semantics`. **Its fields are split by rule 5's table**:
executable semantics are identity-bearing; provenance, approval and the revision id are recorded and
not hashed.

**Implement only the pilot's modes; define the rest and refuse them by name:** eligible statuses ·
**indicator-based debit selection** (the `D`/`C` mapping) · the decided reversal mode ·
booking-date FX.

**Add the producer workflow** — without it the store stays empty and every V2 formula refuses: the
LLM structured-output contract · source/profile evidence collection · deterministic proposal
validation · conflict detection · current-revision selection · realization writes · UI disclosure.

**Resolve the dual authority:** status and reversal already have a **mutable** store at
`eligibility_store.py`. Migrate it or put a single adapter in front of it. **Two current answers is
not an option.**

**Wire `required_policy_kinds()`** — it has no caller — into occurrence-addressed derivation
(rule 11) from aggregate and operation · monetary/unit facts · filter semantics · per-row currency ·
source lifecycle representation · cross-grain rollup · physical source characteristics.

**Policy inputs get full PIT and read-set treatment.** `PitSpec` covers only the expression window.
Each reading realization contributes event and knowledge time · as-of join rule · physical input
requirements · sensitivity/access/retention class · run input snapshots · lineage · authorization.
**Policy inputs also enter the materialization contract**: a restricted FX table must not ride into a
public group behind a public transaction input.

> **Acceptance:** an unresolvable reference refuses by name; a formula omitting a required occurrence
> refuses under rule 11; an unsupported reversal mode refuses and names it; a realization whose
> as-of rule would read post-cutoff FX refuses; one status kind used by two expressions requires two
> realizations; a restricted policy input changes the group's classification; **a re-approval that
> changes no executable field leaves `ir_hash` unchanged (rule 5)**.

### S4 — the bound V2 formula

**`BoundFormulaRevisionV2`** — authored proposal, output policy, physical formula bindings, policy
realization pins. **It makes no complete-read-set claim**; only planning discovers reads.

**Compiler versions must not change the bound formula's semantic identity.**

> **Acceptance:** a compiler version bump leaves the bound-formula hash unchanged; the artifact
> cannot be constructed with an unpinned realization.

### S5 — V2 IR, complete reads, contracts and the authoritative group split

**`FormulaExecutionIRV2`, a distinct type** — never a mutation of V1's IR. Bounded and typed:
`Scan · Filter · DirectionSelection · ReversalNeutralization · Join · CalculatedColumn · Aggregate ·
FinalCombination`. **No `TimeWindow`** — `PitSpec` models the window and the renderer renders it.

**Every operator carries its `policy_occurrence_id` (rule 11) [R10]**, and its identity payload
carries the **resolved executable semantics only** (rule 5). This is what makes S6's proof possible:
V1's `filter_tree` is an opaque dict, so without the tag a status filter is indistinguishable from
any other filter.

**`TemporalReadSpecV2` beside `PitSpec`** — V1's cannot express V2 offsets or future horizons, and
extending it would move V1 identity (rule 6).

**`PhysicalTypePolicyV2`, versioned** — `resolve_physical_type` takes `TypedFormulaV1` only. Settles
AVG scale/precision · ratio and percentage scale · FX multiplication precision · percentile · slope ·
nullability · empty-window behaviour · rounding and overflow.

**`CompilationIdentityV2`** = bound-formula hash + IR hash + compiler/IR versions.

**The authoritative group split, a numbered step:**

```
S1 records a user-selected build SET
  → after IR creation, derive ONE contract per feature
  → split the set into one or more FeatureGroupPlanV1
  → generate, verify and publish each group INDEPENDENTLY
```

`group_by_contract` today **refuses** more than one contract; it must split, preserving V1 semantics
for the single-contract case (§0's shared-machinery row). **The UI shows the split before
verification.**

Pilot plan: scan → filter eligible statuses → **select debit by indicator** → neutralize reversals →
window *(from `PitSpec`)* → join booking-date FX → `normalized_amount = amount × rate` → group by
account → `SUM`.

> **Acceptance:** V1 IR canonical bytes unchanged and the single-contract path byte-identical; a set
> spanning two contracts yields two groups rather than a refusal; the pilot compiles with no window
> node; the `D`/`C` mapping is a typed operator, not a literal predicate; every operator resolves to
> exactly one occurrence.

### S6 — generate and persist readable code

Nodes: `filter_eligible_transactions` · `select_debit_direction` · `neutralize_reversals` ·
`join_booking_fx_rates` · `calculate_base_currency_amount` · `aggregate_posted_debit_amount` ·
`validate_posted_debit_amount`. **No window node. Execute nothing. Publish nothing.**

**Rule 3's proof, third attempt.** Revision 7 copied plan hashes into the manifest — forgeable.
Revision 8 had the renderer report a hash from the emitting branch — still forgeable, since code
could emit a filter, report its hash, then aggregate the *raw* input. The proof is **typed DAG
connectivity plus total dispatch**, run at generation over the plan and the emitted code, resolving
each operator by its `policy_occurrence_id`. The runtime manifest may *retain* the proof; it must
never *manufacture* it.

Plus a **stable code-view API** for the UI.

> **Acceptance:** emitting a filter and then aggregating the unfiltered input **refuses**; deleting
> reversal filtering refuses with the hash still present in the plan; every occurrence maps to
> exactly one dispatched operator on the connected path; **S2's generate/verify/publish boundaries
> re-asserted on V2**.

### S7 — independent generated-code gold proof

**Extend the gates that already exist [R10].** `tests/featuregen/materialize/spark_semantics_gate.py`
and `l0_gate.py` already run generated artifacts on a real JVM, deliberately outside the main suite.
**Do not build a harness — extend these**, and keep the split (pyspark is not a platform dependency).

**The execution target is `deploy/kind/sandbox/Dockerfile.spark`** (`openjdk-17-jre-headless`), via
`LocalClusterSubmitter` with `PYSPARK_PYTHON` / `PYSPARK_DRIVER_PYTHON` set. **This is why S7 can
precede S8**: S7 needs *a* Spark, not the product's remote submission seam.

The **generated Spark code** reproduces the frozen exemplar: posted debit AED included · credit
excluded · pending/failed excluded · reversed original excluded · reversal row excluded · USD
converted at the booking-date rate · out-of-window excluded — **plus** zero-eligible spine account ·
unknown-transaction account · post-cutoff reversal · duplicate and missing FX rate · post-cutoff FX
knowledge time · PIT boundaries.

**Required mutation failures:** drop the status filter · count reversal rows · one flat FX rate ·
settlement instead of booking date · ignore direction · shift the window boundary.

**`OperatorExecutionProofV1` mints a capability signature** — not "SUM", but input/output types ·
window form · policy operator families · null and empty behaviour · rounding. Advertised operations
become `renderer-supported ∩ current execution proof`. (`engine_capability.py` is the precedent for
*deriving rather than hand-maintaining* — **not** for execution-derived advertising.)

**Close here:** the DATE-clock refusal outside UTC, and A.32 🔴 `requirements.lock`.

> **Acceptance:** every case and mutation behaves; each proof names S0's frozen hash; a signature
> mismatch does not satisfy a proof; the gates stay outside the default suite.

### S8 — execution infrastructure and on-demand verification

**Infrastructure:** `business_dt` on the API (absent entirely today) · server-side Hive schema read ·
captured inventory · persistent artifacts · **the product's remote submission seam** (distinct from
S7's local gate) · **a dedicated materialization worker**, since a long compile blocks relays,
timers, projections and ingestion.

> Code generation is immediate. **Verification is user-triggered.**

**Checks are a DAG:** `build + static → execute → ⟨keys · grain · types · nulls · inflation ·
profile⟩ → fold`.

**Input snapshots become pinnable without Iceberg:** record a Hive table snapshot/version where
available; otherwise a **partition file manifest hash**; compare at publication; where neither is
available, **mark the verification `input unpinned`** rather than calling it exact.

**`VerifiedOutputRevisionV1`** — the handoff publication needs, since publication must not execute:
generation and verification identities · immutable staging location · output data/file manifest hash ·
schema hash and row count · group-plan and IR hashes · verification result · expiry/retention status.

**Verification maps to requirements explicitly, never blanket-promotes.** Build a many-to-many
`verification_check_result → contract_id → requirement_id` map over the registry's eight
requirements, and **emit `EXTERNAL_PASSED` only where the check's result schema genuinely matches**,
letting `feature_validation_projection` derive `DATA-CHECKED`. **A group pass must not turn every
member `DATA-CHECKED`.** Never write `USEFULNESS-CHECKED` from materialization verification.

**Staleness is computed on read**; detected automatically, **never re-run automatically**.

**Profiling splits:** blocking output-sanity checks belong to verification; advisory EDA/profile
generation is a separate attempt that cannot fail a validated publication.

> **Acceptance:** a verification survives restart with per-check results intact; changing any
> recorded input flips a pass to stale and names it; an unpinnable environment marks `input
> unpinned`; a keys/types check does not satisfy `JOIN_CONNECTIVITY`; nothing writes a stamp
> directly; **S2's boundaries re-asserted on V2 execution**.

### S9 — atomic sandbox publication

Publish **only the exact verified output** — compare the staging manifest before promotion; **no
re-execution**. Recheck publication capability and policy dependencies at publish time. Whole group,
atomically, with reconciliation.

**Decided, previously open:** *generated code may remain unverified indefinitely; **feature-table
publication requires a current passing verification***. Revisitable by the operator.

> **Acceptance:** a changed or missing staging manifest blocks promotion; a stale verification
> blocks; capability revoked between verify and publish blocks; a partial group never becomes
> visible.

### S10 — UI integration

**Generate → Verify → Publish**, three actions never collapsed. States: `Generated — not verified` ·
`Verification queued` · `Verification running` · `Verification passed` · `Verification failed` ·
`Verification stale`.

```
Posted debit amount · 90 days
  Formula        Ready
  Policies       Resolved
  Code           Generated
  Verification   Not run
  [View generated code]   [Verify in sandbox]
```

Show: selection · **the derived group split, before verification** · generated code · **policy
provenance** (producer, confidence, evidence, observed values, model/prompt version — rule 5's
right-hand column, which exists to be shown) · verification state · publish eligibility. Disclose
that **one failed member prevents publishing its group**.

Unresolved policies name the blocker: *"Currency conversion unresolved: no governed booking-date rate
source is bound."* Needs the missing materialization POST client.

**Population spine, cadence, availability promise and logical group naming need a product workflow**
— the current API makes the caller invent all four.

> **Acceptance (end-to-end, re-asserting S1 through the UI):** no path reaches execution without an
> explicit click; group scope is visible before a publish action; a candidate outside the shadow top
> 12 is generable **from the UI**; a stale result renders as stale, not failure.

### S11 — from one feature to the corpus [R10 — new stage]

**Revision 9 proved one pilot and then expanded the language, never the catalog.** Nothing owned the
second feature, or the ninetieth. This stage does.

- Run the S0–S9 path across the **90 currently-derivable blueprints**, in batches, recording refusals
  by named code rather than fixing them ad hoc.
- Land the **as-of snapshot window shape**, which alone takes derivable blueprints from **90 to
  ~192**.
- Drive `WINDOW_NOT_EVENT_ANCHORED` (×102, the top blocker) down as registry work, not as
  compiler special cases.
- Report coverage as `derivable → generated → verified → published`, so the gap between "the language
  supports it" and "a feature exists" stays visible.

> **Acceptance:** a batch run produces a coverage table with every refusal carrying a named code; no
> blueprint is made to pass by a special case; the as-of shape's effect on the count is measured, not
> estimated.

### S12 — live-provider acceptance, then incremental expansion

**The gate:** at least one real-provider structured-output acceptance test before **free-form** V2
becomes the default. Deterministic recipe-driven generation shipped at S1.

**Operator dependency, flagged [R10]:** this needs Anthropic billing topped up — cluster LLM stages
currently fail closed, and no amount of engineering clears it.

Then, one family at a time, each needing renderer support **and** an execution proof: ratios →
offsets → composites → percentiles → slopes → allocation → future horizons.

> **Acceptance:** the live test asserts a real model's structured output parses, validates and
> admits — not merely that a call succeeded; the advertised set is computed as
> `renderer-supported ∩ execution-proved`; adding a renderer branch without a proof advertises
> nothing.

## 2. Carried forward

**Parallel, gated at merge:** behavioural (not route-existence) frontend↔backend contract tests,
since `/features/recipe` exists and refuses everything · hide the retired "Write definitions"
control · recognition correctness in full · ruff ratchet (79 repo-wide, 35 in `src/`).

**Narrow the leakage claim** — carried since revision 2 **without its context, which no revision has
restated**; recover the original finding before acting.

**Still required:** canonical typed planning-request JSON persisted, since `repr()` cannot
reconstruct a request · `MaterializationContractV1` / `FeatureGroupPlanV1` mapping with a cheap
preflight and a post-compile authoritative split *(now S5)* · a **qualified evaluation-artifact
reader** with a validity contract (migration 1029's four tables exist; a stale pass is not a pass).

**Out of scope, recorded so it is not assumed:** cross-catalog execution refuses by construction
(`chain.py:906`) — **unless S0 decides the pilot needs it**; content-addressed input snapshots need
the deferred Iceberg layer, which S8's manifest hash works around rather than solves.

## 3. Sequencing

```
S0 freeze truth ─► S1 selection + durable authoring ─► S2 split actions/lifecycle ⟨on V1⟩
  ─► S3 policy realization + producer ─► S4 bound formula ─► S5 IR + contracts + GROUP SPLIT
  ─► S6 generate-only ─► S7 gold proof ⟨local Spark gate⟩ ─► S8 infra + verify ⟨product path⟩
  ─► S9 publish ─► S10 UI ─► S11 the corpus ─► S12 live gate + language expansion
```

**What moved in revision 10.** S2's acceptance splits into a V1 proof now and V2 re-assertions at S6
and S8. S7 names its execution target and extends the existing gates rather than building a harness.
S11 is new — the corpus had no owner. And the invariants changed more than the stages did: rule 5
now distinguishes what changes rows from what explains them, and rule 11 requires every operator to
carry its occurrence id, without which rule 3's proof has nothing to resolve against.

**No duration estimate.** Seven revisions have now carried one that a review invalidated.

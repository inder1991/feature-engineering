# V2 as the feature language, on shared execution

**Date:** 2026-08-16 · **Revision 9** — restructured onto the reviewed **S0–S11** sequence. A third
review raised **14 functional blockers plus eight design gaps; all validated against this tree.** The
dominant correction is again sequencing: revision 8 decided group membership and minted an
"executable formula" **before the compiler knows the complete physical reads and the materialization
contract**, which is not derivable that early.

> **V1 remains the stable compatibility language. V2 becomes the intelligent, policy-aware language
> for all new features. LLMs create and enrich V2 formulas; a deterministic V2 compiler resolves
> their banking meaning and runs them through the existing Spark/Kedro materialization platform.**

**Do not reduce V2 to V1. Do not build a second V2 platform.** Revision 4's "lowering" stays
withdrawn — it destroys policy identity. Reuse V1's **execution machinery**; never V1's **artifact**.

**Scope discipline this revision adopts:** narrow the first executing vertical slice to semantics
provable end-to-end, while preserving the whole V2 language and its expansion roadmap. Nothing below
removes eventual functionality.

## 0. Position

| | V1 | V2 |
|---|---|---|
| Role | frozen compatibility language | the language for all new features |
| Gets | correctness fixes only | policy awareness, multi-expression, advanced aggregation, LLM authorship |
| Users choose? | no — the platform decides internally | no — the UI never asks for a version |

**Shared execution** means one Spark packaging, source binding, Hive access, window implementation,
aggregation, staging, validation, assembly, publication and run tracking. **V2 adds preparation and
calculation operators — not a second materialization product.**

**Two-speed default [R9].** *Deterministic recipe-driven V2 generation* may ship at S1. **Free-form
LLM-authored V2 must not become the default until S11's live-provider acceptance passes** —
`replay_authoring_v2` records that every run driving it in the suite is a recorded fixture, so "a
real model held to `AUTHOR_INSTRUCTION_V2` emits a usable v2 proposal" is unproven. Revision 8 put
the UI stage ahead of that gate without noticing the dependency.

## 0.1 Verified before planning

Re-checked against this tree for revision 9. **Reviewed baseline: `29f3b8ac`; targeted suite 2,827
passed, 1 skipped.**

**The authoring and admission path is structurally V1-only**

- `AuthoringResultV2` (`result_v2.py:76`) folds `TypedFormulaProposalV2` + `FormulaOutputPolicyV2`,
  and `replay_authoring_v2.py:209` has its own terminal restorer.
- **But `resolve.py:48` imports only the V1 restorer and `AuthoringResult`**;
  `admission.py:156` types `result: AuthoringResult`, `:175` types `formula: TypedFormulaV1`, and
  `:228` refuses v2 by name. "Add V2 admission" is six tasks, not a line.
- **`recipe_formula_shadow_work_item` is the ONLY durable intent anchor.** `resolve.py` reads it
  directly (`_SELECT_WORK_ITEM`) and documents it as the columns "that carry an authoring intent".
- **The V2 worker always enters the LLM replay orchestrator** (`recipe_formula_worker.py`,
  `if authors_v2: run_authoring_v2_replay(...)`). There is **no deterministic authoring producer.**

**Grouping cannot be known early**

- `derive_contract(conn, ir: FormulaExecutionIRV1, ...)` takes **the IR**; classification consumes
  the complete physical read set.
- **`group_by_contract` refuses a compilation with more than one contract** — it does not split.

**Activation contradicts the plan's own §0.3**

- `semantic_eligibility.py` `AUTHORITY_MATRIX`: `llm/proposed` → `authoring: False`,
  `execution_at_governed: False`, with the comment *"`llm/proposed` NEVER clears execution."*
- The materialization route gates on `_require_the_option_allows_materialization` under action
  `execute_materialization`.

**The pilot's "gold" is three disagreeing artifacts, and the exemplar cannot reproduce its own answer**

| | timezone | boundary | length |
|---|---|---|---|
| derived blueprint (`recipe_formula_blueprint_derivation.py`) | **UTC** | `(start, end]` | recipe |
| V2 proposal fixture (`gold_v2/30_posted_debit_amount_exemplar.json`) | **Asia/Dubai** | `[start, end)` | **90 d** |
| execution gold (`recipes/gold/posted_debit_amount_expected.json`) | — | `(start, end]` | **30 d** |

**And the decisive one [R9]:** the synthetic ledger stores `direction` as **`D`** (11 rows) / `C`
(1 row), while the V2 fixture's predicate is `direction == "debit"`. **Rendering that predicate
literally returns zero rows — not `ACC1=300.00` / `ACC2=93.50`.** The pilot exemplar, as frozen today,
cannot reproduce its own hand-computed result. This is the whole policy-realization thesis in one
fixture: a direction policy must *map* the semantic notion to the source's encoding, and rendering
the raw authored predicate is precisely the bug.

The execution gold is also missing cases it needs: a spine account with **zero** eligible
transactions · an unknown-transaction account · a reversal arriving **after** the cutoff · a
duplicate and a missing FX rate · an FX record whose **knowledge time** is after the cutoff.

**Everything else confirmed**

- `PitSpec` (`expression_ir.py:284`) governs the expression window and **excludes** `empty_window`
  and `null_input` (`:287`). It has no offset or future-horizon representation.
- **Input snapshot ids are not content snapshots** — `runprep.py` hashes the requirement id, ordered
  partitions and business date. An in-place rewrite yields the same id.
- **`GATES_PASSED` is not terminal.** `TERMINAL_RUN_EVENT_KINDS` = `{GATES_FAILED, PUBLISHED,
  PUBLICATION_REFUSED, RUN_FAILED}`. A verification the user never publishes stays permanently
  non-terminal.
- **The validation registry has exactly eight external requirements**: `ADDITIVITY_SUPPORTS_OPERATION
  · CURRENCY_CONSISTENT · GRAIN_IS_UNIQUE · JOIN_CONNECTIVITY · TEMPORAL_IS_POPULATED ·
  TEMPORAL_LAG_BOUNDED · TYPE_IS_NUMERIC · UNIT_CONSISTENT`.
- `sandbox_execution_hash` (`identity.py:479`) requires a non-defaulted `capability_attestation_id`.
- `compile_feature_group` (`chain.py:467`) renders → validates → executes → publishes continuously,
  keyed by `logical_group_name`.
- `govern.py:610` selects `WHERE feature_name = %s ORDER BY version DESC LIMIT 1`.
- `api.ts:4190` has only a materialization-run GET; the backend POST takes shadow work-item ids
  capped at `MAX_RECIPE_FORMULA_CAPTURES_PER_RUN = 12`.
- `required_policy_kinds()` has **zero production callers**.
- `_manifest_lines` is *"counts, hashes and locations only"*.
- `physical_types.resolve_physical_type` accepts `TypedFormulaV1` only.
- `engine_capability` derives from **renderer dispatch**, not execution evidence.
- `banking_policies.py` registers kinds; `eligibility_store.py` is keyed `(catalog_source, table)`
  with a **flag/code-only** reversal shape; currency and allocation have **no store at all**.
- `business_dt` appears **nowhere** in `api/routes/materialization_runs.py`.
- Cross-catalog execution refuses at `chain.py:906`; migration prefixes collide at **0973, 0974,
  1034, 1036, 1037, 1038, 1040**.
- DEFERRED-WORK: A.32 🔴 `requirements.lock` cannot construct the rendered catalog; DATE clocks
  outside UTC refuse because PIT carries no clock type.

**Standing instruction: check what exists before designing what to add.** Four revisions running
proposed building something the codebase already had.

## 0.2 Invariants

1. No V2 formula executes with an unresolved policy reference.
2. **A non-empty policy string is not proof that the policy exists.**
3. Every resolved policy is **consumed**, proved by **typed DAG connectivity plus total renderer
   dispatch** — never by a reported hash alone **[R9]**.
4. No policy may disappear when V2 reuses existing operators.
5. Policy revisions and physical bindings enter executable identity.
6. V1 formula hashes and V1 IR canonical bytes remain unchanged.
7. V1 and V2 share publication and validation machinery.
8. The LLM proposes and explains; deterministic code executes.
9. The UI never asks a user to choose a formula version.
10. **"Supported" means generated code has been executed successfully**, recorded against a
    **capability signature** — not an operator name **[R9]**.
11. **Required = declared = resolved = consumed, occurrence-addressed [R9]:**
    ```
    executable_revision + expression_path + physical source binding revision
      + policy kind + semantic role
    ```
    Kind-level sets fail for ratio, difference and composite formulas: two expressions may each need
    their own status or reversal realization, and one must not satisfy both merely by sharing a kind.
    The invariant is a **one-to-one** mapping between requirement occurrences, resolved realizations,
    typed IR operators and renderer consumption.

**Rule 10's bootstrap:** an operator's first execution happens on **S7's development gold path,
outside the product**. Nothing is advertised to a user in order to become executable.

## 0.3 Division of labour, and what each action costs

**LLM:** understand the hypothesis · select recipes · propose candidates · map roles to concepts ·
recommend physical columns · interpret source-specific status descriptions · propose debit/credit
conventions and reversal representations · identify currency and rate columns · produce structured V2
formulas · critique · explain refusals · summarise results.

**Deterministic code:** validate structure · bind columns · **derive the required policy set** ·
resolve realizations · check executability · compile the plan · generate Spark · execute · validate ·
publish. **The LLM never injects free-form SQL.**

**Reviewed recipes instantiate deterministic V2 blueprints directly** — never ask a model to restate
arithmetic the registry already holds.

**Activation becomes consequence-specific [R9].** Revision 8 asserted that human confirmation does
not gate sandbox experimentation; the `AUTHORITY_MATRIX` says `llm/proposed` clears nothing, and the
only positive route test monkeypatches state to pass. Replace the single `execute_materialization`
gate with three actions:

| Action | `llm/proposed`, deterministically validated |
|---|---|
| `generate_code` | **may clear** |
| `verify_in_sandbox` | **may clear** |
| `publish_sandbox` | **own policy — does not inherit the above** |

**Hard contradictions, leakage, unsupported fan-out and missing semantics still refuse** at every
level: those are calculation problems, not governance preferences.

## 0.4 Three words that must not blur

**Execution proof** (S7) — development-time, mutation-tested, against reviewed gold. **Sandbox
verification** (S8) — user-triggered, against the exact sealed artifact on real data. **Publication**
(S9) — atomic, group-wide, promoting *exactly the verified bytes*.

**Never re-run generic gold fixtures for a user verification** without a tested input-remapping
mechanism. They are different data with different meaning.

## 0.5 Migration ledger

Prefixes already collide seven times, so **S0 builds a ledger with a CI uniqueness test** before any
allocation. Storage needed: generalized authoring work item · exact selected-feature identity and
build mapping · policy realization revisions · verification requests/attempts/results · verified
output revisions · publication requests/attempts · operator execution proofs · profiles.

**Reuse existing tables where their meaning fits** — `materialization_request`,
`materialization_generation`, the compiled-artifact and run-event tables. **Do not add parallel
`FeatureBuild*` tables without an explicit migration map [R9].**

## 1. The sequence

### S0 — freeze the complete pilot truth *(hard STOP: humans decide)*

```
enumerate contradictions → governance/product decision → decision record
   → reconcile blueprint, fixture, execution gold and expected rows → freeze hash
```

**Reconcile all three artifacts, not two** — the derived blueprint (UTC, `(start,end]`), the V2
fixture (Asia/Dubai, `[start,end)`, 90 d) and the execution gold (`(start,end]`, 30 d). Decide:
policy-reference namespace · window timezone, boundary and length · **reversal-as-of semantics** ·
**direction mapping (`D`/`C` ↔ debit/credit)** · population spine · **FX join and cardinality** ·
publication rule.

**Replace the gold corpus with one coherent parameterized exemplar**, extended to cover: a spine
account with zero eligible rows · an unknown-transaction account · a reversal arriving after cutoff ·
duplicate and missing FX rates · an FX record whose knowledge time is after cutoff.

**Decide the FX catalog question [R9].** The pilot joins booking-date FX while cross-catalog
execution refuses by construction. Either **prove and pin a same-catalog rate source**, or bring a
bounded reference-data join authorization into the pilot. Do not assume it away.

Also: the migration ledger.

> **Acceptance:** each contradiction has a decision record naming who decided; all three artifacts
> agree afterwards; the frozen exemplar is stored and hashed; **the frozen predicate returns the
> hand-computed rows against the ledger's actual `D`/`C` encoding**; the ledger's CI uniqueness test
> fails on the seven existing collisions unless explicitly grandfathered.

### S1 — exact selection and durable authoring

**Exact feature identity** (`feature_definition_key` / `revision` / `executable_revision_id` /
`output_column_name` / `display_label`), applied **at contract creation**, reusing the existing
suggestion and option identities.

**Generalize the work item — do not delete it [R9].** Revision 8 required that no generation path
read `recipe_formula_shadow_work_item`; that row is the only durable bridge into materialization.
Generalize it into **`formula_authoring_work_item`**, and restate the requirement as:

> Generation must not depend on shadow **ranking**, the **top-12 cap**, or **opportunistic capture**.
> A selected option creates the same kind of sealed work item directly.

**Stable output-column naming [R9]** — four exact identities can still collide on one
`posted_debit_amount` Hive column. Variants need deterministic, stable physical names.

**A deterministic authoring producer [R9]** — today the V2 worker always enters LLM replay. The
producer binds the reviewed blueprint → constructs the proposal deterministically → runs the **same**
structural, output-authority and critic gates → writes the **standard V2 replay trace** → yields an
ordinary `AuthoringResultV2`. **No second admission shortcut.**

**V2 resolution and admission, as explicit tasks:** version-dispatched trace restoration · V2
terminal hash verification · `ResolvedFeatureInputV2` · `AdmittedFeatureV2` · proposal/output-pair
coherence · shared batch and name-collision handling. V1 admission byte-untouched.

> **Acceptance:** four variants of one display name resolve to four features with four stable column
> names; a candidate outside the shadow top 12 authors and admits; a reviewed recipe produces a
> durable replayable `AuthoringResultV2` with **no provider call**; a **frozen-bytes test pins
> existing V1 formula hashes** (rule 6, untested through revision 8).

### S2 — split actions and lifecycle contracts

`compile_feature_group` becomes three commands — it cannot be retrofitted with a request kind:

| Command | Does | Must not |
|---|---|---|
| `generate_code()` | render, seal, store | execute, publish |
| `verify_in_sandbox()` | execute the sealed artifact, record results | publish |
| `publish_sandbox()` | promote **exactly the verified output** | execute |

**Three request/attempt/result triples [R9]**, because `GATES_PASSED` is not terminal and a verified
-but-unpublished run would otherwise never close:

```
BuildRequest        → BuildAttempt        → GeneratedArtifact
VerificationRequest → VerificationAttempt → VerifiedOutputRevision
PublicationRequest  → PublicationAttempt  → ActiveRevision
```

**`VerificationExecutionIdentityV1`**, separate from `sandbox_execution_hash`, which requires a
`capability_attestation_id` and so cannot honestly identify an execution with no publication
intention. Covers rendered artifact identity · environment · business date · resolved parameters ·
input snapshots · check-suite version · compiler and renderer versions.

> **Acceptance:** generation leaves no run event on the plane; verification executes with no
> publication capability present; every triple reaches a terminal state; the migration map from
> existing tables is explicit.

### S3 — minimal executable policy realization, **with a producer**

**`PolicyRealizationRevisionV1`** — immutable, keyed by `abstract policy ref + physical dataset
binding + environment/source + effective semantics`, carrying executable fields, provenance,
revision, physical dependencies and temporal rules.

**Implement only the pilot's modes; define the rest and refuse them by name:** eligible statuses ·
**indicator-based debit selection** (the `D`/`C` mapping) · the decided reversal mode ·
booking-date FX.

**Add the producer workflow [R9]** — without it the store stays empty and every V2 formula refuses.
It needs: the LLM structured-output contract · source/profile evidence collection · deterministic
proposal validation · conflict detection · current-revision selection · realization writes · UI
disclosure.

**Resolve the dual authority [R9]:** status and reversal already have a **mutable** store at
`eligibility_store.py`. Either migrate it or put a single adapter in front of it. **Two current
answers is not an option.**

**Wire `required_policy_kinds()`** — it has no caller — into occurrence-addressed derivation
(rule 11) from aggregate and operation · monetary/unit facts · filter semantics · per-row currency ·
source lifecycle representation · cross-grain rollup · physical source characteristics.

**Policy inputs get full PIT and read-set treatment.** `PitSpec` covers only the expression window —
not FX tables, reversal history, linked reversals, allocation tables or effective-dated mappings.
Each reading realization contributes event and knowledge time · as-of join rule · physical input
requirements · sensitivity/access/retention class · run input snapshots · lineage · authorization ·
executable identity. **Policy inputs also enter the materialization contract**: a restricted FX table
must not ride into a public group behind a public transaction input.

> **Acceptance:** an unresolvable reference refuses by name; a formula omitting a required occurrence
> refuses under rule 11; an unsupported reversal mode refuses and names it; a realization whose
> as-of rule would read post-cutoff FX refuses; one status kind used by two expressions requires two
> realizations; a restricted policy input changes the group's classification.

### S4 — the bound V2 formula

**`BoundFormulaRevisionV2`** — authored proposal, output policy, physical formula bindings, policy
realization pins. **It makes no complete-read-set claim [R9]**; revision 8's artifact asserted reads
that only planning discovers.

**Compiler versions must not change the bound formula's semantic identity.**

> **Acceptance:** a compiler version bump leaves the bound-formula hash unchanged; the artifact
> cannot be constructed with an unpinned realization.

### S5 — V2 IR, complete reads, contracts and the authoritative group split

**`FormulaExecutionIRV2`, a distinct type** — never a mutation of V1's IR. Bounded and typed:
`Scan · Filter · DirectionSelection · ReversalNeutralization · Join · CalculatedColumn · Aggregate ·
FinalCombination`. **No `TimeWindow`** — `PitSpec` models the window and the renderer already renders
it.

**`TemporalReadSpecV2` beside `PitSpec` [R9]** — V1's `PitSpec` cannot express V2 offsets or future
horizons, and extending it would move V1 identity (rule 6).

**`PhysicalTypePolicyV2`, versioned** — `resolve_physical_type` takes `TypedFormulaV1` only. Settles
AVG scale/precision · ratio and percentage scale · FX multiplication precision · percentile · slope ·
nullability · empty-window behaviour · rounding and overflow.

**`CompilationIdentityV2`** = bound-formula hash + IR hash + compiler/IR versions.

**The authoritative group split becomes a numbered step [R9]**, out of "carried forward":

```
S1 records a user-selected build SET
  → after IR creation, derive ONE contract per feature
  → split the set into one or more FeatureGroupPlanV1
  → generate, verify and publish each group INDEPENDENTLY
```

`group_by_contract` today **refuses** more than one contract; it must split. **The UI shows the split
before verification.**

Pilot plan: scan → filter eligible statuses → **select debit by indicator** → neutralize reversals →
window *(from `PitSpec`)* → join booking-date FX → `normalized_amount = amount × rate` → group by
account → `SUM`.

> **Acceptance:** V1 IR canonical bytes unchanged; a selected set spanning two contracts yields two
> groups rather than a refusal; the pilot compiles with no window node; the `D`/`C` mapping appears
> as a typed operator, not a literal predicate.

### S6 — generate and persist readable code

Nodes: `filter_eligible_transactions` · `select_debit_direction` · `neutralize_reversals` ·
`join_booking_fx_rates` · `calculate_base_currency_amount` · `aggregate_posted_debit_amount` ·
`validate_posted_debit_amount`. **No window node.** **Execute nothing. Publish nothing.**

**Rule 3's proof, corrected twice over [R9].** Revision 7 copied plan hashes into the manifest —
forgeable. Revision 8 had the renderer report a hash from the emitting branch — **still forgeable**:
code could emit a filter, report its hash, and then aggregate the *raw* input. The proof must be
**typed DAG connectivity plus total renderer dispatch**: the consumed operator must lie on the path
from scan to the emitted aggregate, and every resolved occurrence must be dispatched. The runtime
manifest may *retain* the proof; it must never *manufacture* it.

Plus a **stable code-view API** for the UI.

> **Acceptance:** emitting a filter and then aggregating the unfiltered input **refuses**; deleting
> reversal filtering refuses with the hash still present in the plan; every occurrence from rule 11
> maps to exactly one dispatched operator on the connected path.

### S7 — independent generated-code gold proof

The **generated Spark code** reproduces the frozen exemplar: posted debit AED included · credit
excluded · pending/failed excluded · reversed original excluded · reversal row excluded · USD
converted at the booking-date rate · out-of-window excluded — **plus** zero-eligible spine account ·
unknown-transaction account · post-cutoff reversal · duplicate and missing FX rate · post-cutoff FX
knowledge time · PIT boundaries.

**Required mutation failures:** drop the status filter · count reversal rows · one flat FX rate ·
settlement instead of booking date · ignore direction · shift the window boundary.

**`OperatorExecutionProofV1` mints a capability signature [R9]** — not "SUM", but input/output types ·
window form · policy operator families · null and empty behaviour · rounding. Advertised operations
become `renderer-supported ∩ current execution proof`. (`engine_capability.py` is the precedent for
*deriving rather than hand-maintaining* — **not** for execution-derived advertising, which it does not
do.)

**Close here:** the DATE-clock refusal outside UTC, and A.32 🔴 `requirements.lock`.

> **Acceptance:** every case and mutation behaves; each proof names S0's frozen hash; a signature
> mismatch does not satisfy a proof.

### S8 — execution infrastructure and on-demand verification

**Infrastructure, assigned to this stage rather than listed loose [R9]:** `business_dt` on the API
(absent entirely today) · server-side Hive schema read · captured inventory · persistent artifacts ·
a real remote submission seam · **a dedicated materialization worker**, since a long compile blocks
relays, timers, projections and ingestion.

> Code generation is immediate. **Verification is user-triggered.**

**Checks are a DAG, not a flat parallel set:**
`build + static → execute → ⟨keys · grain · types · nulls · inflation · profile⟩ → fold`.

**Input snapshots become pinnable without Iceberg [R9]:** record a Hive table snapshot/version where
available; otherwise a **partition file manifest hash**; compare at publication; and where the
environment can supply neither, **mark the verification `input unpinned`** rather than calling it
exact. Revision 8 called these "exact snapshots" while `runprep.py` hashes only requirement,
partitions and business date.

**`VerifiedOutputRevisionV1` [R9]** — the handoff publication needs, since publication must not
execute: generation and verification identities · immutable staging location · output data/file
manifest hash · schema hash and row count · group-plan and IR hashes · verification result ·
expiry/retention status.

**Verification maps to requirements explicitly [R9], never blanket-promotes.** The registry holds
eight specific external requirements. Build a many-to-many
`verification_check_result → contract_id → requirement_id` map, and **emit `EXTERNAL_PASSED` only
where the check's result schema genuinely matches that requirement**, letting
`feature_validation_projection` derive `DATA-CHECKED`. **A group pass must not turn every member
`DATA-CHECKED`.** Never write `USEFULNESS-CHECKED` from materialization verification.

**Staleness is computed on read**, comparing the recorded hash set against current; detected
automatically, **never re-run automatically**.

**Profiling splits:** blocking output-sanity checks belong to verification; advisory EDA/profile
generation is a separate attempt that cannot fail a validated publication.

> **Acceptance:** a verification survives restart with per-check results intact; changing any
> recorded input flips a pass to stale and names it; an unpinnable environment marks `input
> unpinned`; a keys/types check does not satisfy `JOIN_CONNECTIVITY`; nothing writes a stamp
> directly.

### S9 — atomic sandbox publication

Publish **only the exact verified output** — compare the staging manifest before promotion; **no
re-execution**. Recheck publication capability and policy dependencies at publish time. Whole group,
atomically, with reconciliation.

**Decided by this review [R9], previously left open:** *generated code may remain unverified
indefinitely; **feature-table publication requires a current passing verification***. Recorded as a
product rule, revisitable by the operator — revision 8 deferred it to S10, which was too late to
shape the lifecycle.

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
provenance** (producer, confidence, evidence, observed values, model/prompt version) · verification
state · publish eligibility. Disclose that **one failed member prevents publishing its group**.

Unresolved policies name the blocker: *"Currency conversion unresolved: no governed booking-date rate
source is bound."* Needs the missing materialization POST client — `api.ts` has only the GET.

**Population spine, cadence, availability promise and logical group naming need a product workflow
[R9]** — the current API makes the caller invent all four.

> **Acceptance:** no path reaches execution without an explicit click; group scope is visible before
> a publish action; a candidate outside the shadow top 12 is generable; a stale result renders as
> stale, not failure.

### S11 — live-provider acceptance, then incremental expansion

**The gate:** at least one real-provider structured-output acceptance test before **free-form** V2
becomes the default. Deterministic recipe-driven generation already shipped at S1.

Then, one family at a time, each needing renderer support **and** an execution proof: ratios →
offsets → composites → percentiles → slopes → allocation → future horizons.

> **Acceptance:** the advertised set is computed as `renderer-supported ∩ execution-proved`; adding a
> renderer branch without a proof advertises nothing.

## 2. Carried forward

**Parallel, gated at merge:** behavioural (not route-existence) frontend↔backend contract tests,
since `/features/recipe` exists and refuses everything · hide the retired "Write definitions"
control · recognition correctness in full · ruff ratchet (79 repo-wide, 35 in `src/`).

**Narrow the leakage claim** — carried since revision 2 **without its context, which no revision has
restated**; recover the original finding before acting.

**Still required:** canonical typed planning-request JSON persisted, since `repr()` cannot
reconstruct a request · `MaterializationContractV1` / `FeatureGroupPlanV1` mapping with a cheap
preflight and a post-compile authoritative split on the full contract hash *(now S5)* · a
**qualified evaluation-artifact reader** with a validity contract (migration 1029's four tables
exist; a stale pass is not a pass) · the **as-of snapshot window shape**, which alone takes derivable
blueprints from 90 to ~192.

**Out of scope, recorded so it is not assumed:** cross-catalog execution refuses by construction
(`chain.py:906`) — **unless S0 decides the pilot needs it**; content-addressed input snapshots need
the deferred Iceberg layer, which S8's manifest hash works around rather than solves.

## 3. Sequencing

```
S0 freeze truth ─► S1 selection + durable authoring ─► S2 split actions/lifecycle
  ─► S3 policy realization + producer ─► S4 bound formula ─► S5 IR + contracts + GROUP SPLIT
  ─► S6 generate-only ─► S7 gold proof ⟨first real execution⟩ ─► S8 infra + verify
  ─► S9 publish ─► S10 UI ─► S11 live gate + expand
```

**What moved, and why.** The pilot truth is frozen first because the exemplar currently cannot
reproduce its own answer. Actions and lifecycles split at S2, before anything is built across the
boundary they create. The group split moved into S5, where contracts first exist — revision 8 fixed
membership at S1, four stages before the compiler could know it. The bound formula (S4) makes no
read-set claim; only the IR (S5) can.

**No duration estimate.** Six revisions have now carried one that a review invalidated.

# V2 as the feature language, on shared execution

**Date:** 2026-08-16 · **Revision 8** — restructured onto the reviewed twelve-stage sequence. A
second detailed review raised **26 findings; all 26 validated against this tree**, and eight of them
are errors revision 7 introduced. The phase numbering changes from `P1–P8` to `S1–S12` because the
*order* changed, not just the content — renumbering the old phases would have hidden that.

> **V1 remains the stable compatibility language. V2 becomes the intelligent, policy-aware language
> for all new features. LLMs create and enrich V2 formulas; a deterministic V2 compiler resolves
> their banking meaning and runs them through the existing Spark/Kedro materialization platform.**

**Do not reduce V2 to V1. Do not build a second V2 platform.**

Revision 4's "lowering" stays **withdrawn** — it destroys policy identity. Reuse V1's **execution
machinery**; never V1's **artifact**.

## 0. Position

| | V1 | V2 |
|---|---|---|
| Role | frozen compatibility language | the language for all new features |
| Gets | correctness fixes only | policy awareness, multi-expression, advanced aggregation, LLM authorship |
| Users choose? | no — the platform decides internally | no — the UI never asks for a version |

**Shared execution** means V1 and V2 use the same Spark packaging, source binding, Hive access,
window implementation, aggregation functions, staging, validation, feature-table assembly,
publication and run tracking. **V2 adds preparation and calculation operators — not a second
materialization product.**

**V2-as-default is not flag-guarded, deliberately** (pre-live; the standing steer is to avoid flags
that only defer a decision). But it **is** gated on S12's live-provider acceptance: `replay_v2`'s own
docstring records that every run driving it in the suite is a recorded fixture, so *"a real model
held to `AUTHOR_INSTRUCTION_V2` emits a usable v2 proposal"* is currently unproven.

## 0.1 Verified before planning

Everything here was re-checked against this working tree for revision 8.

**The shape of what exists**

- **The authored V2 artifact is already a durable pair** — `AuthoringResultV2` (`result_v2.py:76`)
  folds `TypedFormulaProposalV2` + `FormulaOutputPolicyV2`. **There is no executable V2 artifact, and
  no `TypedFormulaV2` was ever minted.**
- **The window is already a governed type.** `expression_ir.py:284` `PitSpec` carries
  `window_basis · window_length · window_unit · window_start_inclusive · window_end_inclusive ·
  window_timezone` plus the clock, and `nodes_compute.py:956` already renders the window from it. It
  **deliberately excludes** `empty_window` and `null_input` (`:287`).
- **`banking_policies.py` registers policy KINDS and their resolution homes — it is not a
  realization store.** Some homes point back at the formula schema or output authority itself.
- **`data_agent/eligibility_store.py` is keyed by `(catalog_source, table)`** and carries
  `reversal_mode · reversal_column · non_reversed_values` — a flag/code shape only.
- **The engine advertises four aggregates** (`sum`, `count_rows`, `count_non_null`, `count_distinct`)
  derived from **renderer dispatch** (`renderable_aggregations()`), **not from execution evidence**.
- **Physical typing accepts `TypedFormulaV1` only** (`physical_types.py:375`).
- **Two governance mechanisms exist, not one.** `governance/attributes.py:14` `VERIFICATION_STAMPS`
  is the `feature_versions` vocabulary (migration 0060 CHECK); `feature_validation_projection.py`
  folds `ASSESSED / EXTERNAL_PASSED / EXTERNAL_FAILED / INVALIDATED / SUPERSEDED` into `DATA-CHECKED`
  only when every blocking requirement holds a current pass.

**The shape of what is broken**

- **Generation, execution and publication are ONE operation.** `compile_feature_group`
  (`chain.py:467`) records → renders → validates → `_execute_the_run` (whenever the selection is a
  `PublisherSelection`) → `publish_generation`. Continuous.
- **It is group-wide**, keyed by `logical_group_name` — not per-feature.
- **Verification currently requires publication capability.** `sandbox_execution_hash`
  (`identity.py:479`) requires a non-defaulted `capability_attestation_id`; the docstring makes the
  non-default deliberate.
- **The UI cannot reach code generation.** `api.ts:4190` has **only** a materialization-run GET —
  no POST client. The backend POST takes raw `recipe_formula_shadow_work_item` ids, and those are
  opportunistic, flag-dependent, created on the *generation response* path, and capped at
  `MAX_RECIPE_FORMULA_CAPTURES_PER_RUN = 12`. **A user-selected candidate outside that top 12 has no
  executable work item.**
- **Feature identity still collapses variants.** `govern.py:610` selects
  `WHERE feature_name = %s ORDER BY version DESC LIMIT 1` — "count 30d", "count 90d", "count
  excluding reversals" and "count in base currency" all resolve to one feature.
- **`required_policy_kinds()` has ZERO production callers** — only its own `__all__`, its docstring,
  and tests. Nothing derives the *required* policy set anywhere in the pipeline.
- **The staging manifest carries no policy evidence** — `_manifest_lines` is *"counts, hashes and
  locations only"*.
- **`business_dt` never reaches the queue** — the string does not appear in
  `api/routes/materialization_runs.py` at all, so every public request is compile-only.
- **Cross-catalog execution refuses by construction** — `chain.py:906`: the chain has no parameter
  carrying a `BridgeExecutionAuthorization`, so a bridged group stops at `PREPARE_RUN` with
  `JOIN_CARDINALITY_UNKNOWN`.
- **Migration prefixes already collide** at **0973, 0974, 1034, 1036, 1037, 1038, 1040**. Prose
  reservations are demonstrably not enough.
- **Two DEFERRED-WORK blockers sit on the pilot path**: A.32 🔴 `requirements.lock` installs an
  environment that cannot construct the rendered catalog; and `input_snapshot_ids` are hashed but are
  not content snapshots, so two runs over the same partitions after an in-place rewrite share an
  execution identity.

**The exemplar's contradictions, enumerated rather than assumed**

| | Recipe (`transaction_foundation.py:46-49`) | Gold fixture (`gold_v2/30_posted_debit_amount_exemplar.json`) |
|---|---|---|
| status | `eligible_status:foundation-posted-events` | `policy:eligible-posted-status` |
| reversal | `reversal_correction:foundation-flag-or-code` | `policy:reversal-neutralizes-original` |
| direction | `direction_sign:foundation-signed-by-indicator` | `policy:dc-sign-convention` |
| currency | `currency_conversion:foundation-base-currency` | `policy:governed-rate-at-booking` |
| window | — | trailing 90 day, **`Asia/Dubai`**, **`[inclusive, exclusive)`**, `empty_window: zero`, `null_input: ignore` |

**Revision 7 over-narrowed this and is corrected here.** It claimed timezone and boundaries were
"not conventions to choose" because `PitSpec` governs them. `PitSpec` supplies the **fields**; it
does not supply the **values**, and the two artifacts disagree. No new window type is needed — **and
a decision is still required.**

**Standing instruction for every stage: check what exists before designing what to add.** Three
revisions running proposed building something the codebase already had.

## 0.2 Non-negotiable rules — this plan's invariants

1. No V2 formula executes with an unresolved policy reference.
2. **A non-empty policy string is not proof that the policy exists.**
3. Every resolved policy must be **consumed** by the execution plan, **proved by the renderer, not
   asserted by the plan**.
4. No policy may disappear when V2 reuses existing operators.
5. Policy revisions and physical bindings enter executable identity.
6. V1 formula hashes remain unchanged.
7. V1 and V2 share publication and validation machinery.
8. The LLM proposes and explains; deterministic code executes.
9. The UI never asks a user to choose a formula version.
10. **"Supported" means generated code has been executed successfully** — not that the schema can
    describe the operation.
11. **[R8] Required = declared = resolved = consumed.** Checking only that *declared* policies
    resolve is necessary and insufficient: `AuthorityRefsV2` permits a partial block, so a formula can
    omit a required policy entirely and still pass. A missing status, reversal or sign policy produces
    confidently wrong numbers.

**Rule 10's bootstrap.** Advertise-after-execution plus a UI offering only advertised operations is
circular. The escape: an operator's first execution happens on **S8's development gold path, outside
the product**. Nothing is advertised to a user in order to become executable.

## 0.3 Division of labour

**LLM:** understand the hypothesis · select recipes · propose candidates · map roles to concepts ·
recommend physical columns · interpret source-specific status descriptions · propose debit/credit
conventions and reversal representations · identify currency and rate columns · produce structured V2
formulas · critique · explain refusals · summarise results.

**Deterministic code:** validate V2 structure · bind columns · **derive the required policy set** ·
resolve policy records · check every policy is executable · compile the execution plan · generate
Spark · execute arithmetic · validate output · publish. **The LLM never injects free-form SQL.**

**Reviewed recipes instantiate deterministic V2 blueprints directly [R8].** Do not ask a model to
restate arithmetic the registry already knows.

**Human confirmation does not gate sandbox experimentation.** An LLM-proposed policy is usable when it
carries producer, confidence, evidence, source columns, observed values, and model/prompt version —
disclosed in the UI. **Missing or contradictory semantics still refuse**, because that is a
calculation problem, not a governance preference.

## 0.4 Three words that must not blur

- **Execution proof** (S8) — development-time, mutation-tested, against reviewed gold fixtures. How
  an operator earns advertisability under rule 10.
- **Sandbox verification** (S9) — the user-triggered product capability, against the exact sealed
  artifact on real data.
- **Publication** (S10) — atomic, group-wide, capability-checked.

**Do not re-run generic gold fixtures for a user verification** unless a tested input-remapping
mechanism exists. They are different data with different meaning.

## 0.5 Migration reservations — a checked ledger, not prose [R8]

Prefixes already collide at 0973, 0974, 1034, 1036, 1037, 1038, 1040, so **S1 builds a ledger with a
uniqueness test in CI** before any number below is used. Storage this plan needs: exact
selected-feature identity and build mapping · build requests and attempts · group membership · policy
realization revisions · verification requests and results · operator execution proofs · profiles.
**Numbers are allocated in S1 against the ledger, and deliberately not guessed here** — revision 7
reserved three for a plan that needs at least seven.

## 1. The sequence

### S1 — foundation decisions and ownership

**A hard STOP gate, because S1 is where humans decide [R8].** Revision 7 asked the executor to
"record contradictions but not decide them" while its acceptance demanded a frozen artifact. Those
cannot both happen. The gate is:

```
enumerate contradictions → governance/product decision → decision record
   → reconcile recipe, blueprint, fixture and expected rows → freeze hash
```

The contradictions are enumerated in §0.1 — **policy-reference namespace, timezone, window
inclusivity**. Whether the canonical namespace is `eligible_status:*` or `policy:*`, and which
window convention wins, are **the user's or operator's calls and are not resolved here**. Enumerate
further contradictions rather than assuming that table is complete.

Also in S1: the migration ledger · **exact selected-feature identity** (`feature_definition_key` /
`revision` / `executable_revision_id` / `output_column_name` / `display_label`) · build-request
identity · group membership · and a named owner for the V2 IR.

> **Acceptance:** a decision record exists for each contradiction, naming who decided; recipe,
> blueprint, fixture and expected rows agree afterwards; the frozen expectation is a stored hashed
> artifact; the migration ledger has a CI uniqueness test that fails on the seven existing
> collisions unless they are explicitly grandfathered.

### S2 — exact feature identity *(prerequisite, not parallel)*

**Promoted out of "carried forward, blocking nothing" [R8].** `govern.py:610`'s lookup by
`feature_name` collapses distinct variants, so generation and contract creation cannot address the
feature a user actually chose. Everything downstream inherits the ambiguity.

Applied **at contract creation**. Plus the **selected-feature build mapping**:

```
considered_revision_id + option_id + feature_definition_key
  + executable_revision_id + output_column_name  →  the exact build work item
```

**Generate must create or find that work item. It must never depend on shadow capture** — those are
opportunistic, flag-dependent and capped at 12.

> **Acceptance:** four variants of one display name resolve to four features; a candidate ranked
> outside the shadow top 12 generates successfully; no generation path reads
> `recipe_formula_shadow_work_item`.

### S3 — the policy-realization layer

**`banking_policies.py` is a registry of kinds, not a store [R8].** Build
**`PolicyRealizationRevisionV1`** — immutable, keyed by:

```
abstract policy ref + physical dataset binding + environment/source + effective semantics
```

carrying executable fields, provenance, revision, physical dependencies and temporal rules.

- **Status** → status column, eligible values, null behaviour.
- **Direction** → representation (positive magnitude + indicator, or signed amount), direction
  column, debit values, amount normalization.
- **Reversal** → flag/code · linked reversal transaction · compensating transaction · status history ·
  negative entry. Today's eligibility store implements **flag/code only**. Each mode gets its own
  deterministic implementation; **unsupported modes refuse by name, never approximated as a flag**.
- **Currency** → currency column, base currency, rate table, source/target keys, effective timestamp,
  booking-vs-settlement rule, rate direction, rounding. **No source-specific rate-policy store exists
  today**, and `output_authority_v2.py` currently reflects an arbitrary non-empty ref as
  `converted:<ref>` without proving the policy exists — rule 2, in code.
- **Allocation** → full · equal split · ownership percentage · primary owner · other governed method.
  **No executable realization store exists today.**

**Required-policy derivation, wired [R8].** `required_policy_kinds()` exists and has no caller. Give
it one, post-binding, deriving from aggregate and operation · monetary/unit facts · filter semantics ·
per-row currency · source lifecycle representation · cross-grain rollup · physical source
characteristics. Then enforce rule 11's equality.

**Policy inputs get full PIT and read-set treatment [R8].** `PitSpec` covers the expression's source
window and nothing else — not the FX rate table, reversal history, linked reversals, allocation
tables or effective-dated mappings. Every realization that reads data contributes its own event and
knowledge time · as-of join rule · physical input requirements · sensitivity/access/retention class ·
run input snapshots · lineage · authorization · executable identity. **Without this an FX or reversal
join can read the future, or read an undeclared restricted table.**

**Policy inputs also enter the materialization contract.** A feature reading a restricted FX table
must not land in a public group because its primary transaction input is public.

> **Acceptance:** an unresolvable reference refuses by name rather than defaulting; a formula that
> omits a required kind refuses under rule 11; an unsupported reversal mode refuses and names the
> mode; a realization whose as-of rule would read post-cutoff FX refuses; a restricted policy input
> changes the group's classification.

### S4 — the executable V2 revision

**`ExecutableFormulaRevisionV2`, and only now [R8].** Revision 7 placed this before policy
resolution existed, so its own acceptance was unsatisfiable. It combines: authored `AuthoringResultV2`
(unchanged) · physical bindings · resolved policy realizations · complete read set including policy
inputs · policy dependencies · **IR schema version, compiler version and policy-realization
versions**.

**The `compiler-lowering version` field is deleted — there is no lowering [R8].**

V2 admission added; V1 admission byte-untouched (rule 6).

> **Acceptance:** a revision round-trips through durable state with every hash re-verified; **a
> frozen-bytes test pins a corpus of existing V1 formula hashes and fails if any moves** — rule 6 is
> this program's compatibility guarantee and was untested through revision 7; a v3-or-later formula
> refuses by name at V2 admission, as `admission.py:228` already does for v2 at v1.

### S5 — V2 physical typing and IR

**`FormulaExecutionIRV2`, a distinct type [R8]** — never a mutation of V1's IR, so V1 canonical bytes
stay frozen. Bounded and typed: `Scan · Filter · ReversalNeutralization · Join · CalculatedColumn ·
Aggregate · FinalCombination`.

**No `TimeWindow` operator.** `PitSpec` models the window and the renderer already renders it; a
second representation is the duplication rule 4 exists to prevent.

**`PhysicalTypePolicyV2`, versioned [R8]** — `resolve_physical_type` takes `TypedFormulaV1` only, so
V2 has no Hive/Spark type adapter at all. It must settle AVG scale and precision · ratio and
percentage scale · FX multiplication precision · percentile output · slope output · nullability ·
empty-window behaviour · rounding and overflow. **The group plan and rendered schema cannot be
correct before this exists.**

Pilot plan: scan → filter eligible statuses → apply debit direction → neutralize reversals → window
*(from `PitSpec`)* → join booking-date FX → `normalized_amount = amount × rate` → group by account →
`SUM`.

**Safe reuse requires BOTH checks (rule 4):** does the structure fit existing operators, **and** can
every resolved policy be implemented by them? Only then is V1's computation path reused — and the
formula **remains V2** for identity, audit and provenance.

> **Acceptance:** V1 IR canonical bytes are unchanged by S5; the pilot compiles with no window node;
> a policy no existing operator can implement refuses rather than compiling to a plan that drops it;
> FX multiplication precision is asserted, not inherited by accident.

### S6 — code generation only

Render and seal readable Kedro nodes: `filter_eligible_transactions` ·
`normalize_transaction_direction` · `neutralize_reversals` · `join_booking_fx_rates` ·
`calculate_base_currency_amount` · `aggregate_posted_debit_amount` ·
`validate_posted_debit_amount`. **No window node** — the window renders inside the calculation node
from `PitSpec`, as it does for V1.

**Execute nothing. Publish nothing.**

**Rule 3's proof, corrected — the revision-7 mechanism was forgeable [R8].** Copying policy hashes
from the plan into the staging manifest proves only that *the plan mentioned them*. A mutation could
delete reversal filtering, leave the hash in place, and the gate would pass. Instead:

```
render_policy_operator(...) → rendered code fragment + consumed_policy_hashes
```

**The renderer may return a hash only from the code branch that actually emitted the operation.**
Before sealing, generation compares `union(renderer-reported consumed)` against the resolved
required set. The runtime manifest may *retain* that proof; it must never *manufacture* it.

Generation's honesty preconditions: structurally valid formula · every required column bound · **every
required policy resolved (rule 11)** · no placeholders · valid hashes · renderer-proved consumption.
Passing these means the code was generated honestly. **It claims nothing about execution.**

> **Acceptance:** deleting the reversal filter from a rendered node makes generation refuse and name
> the policy, **with the hash still present in the plan**; the consumed set is a function of emitted
> code, or the check proves nothing.

### S7 — split the chain into three commands

**The central prerequisite for on-demand verification [R8].** `compile_feature_group` cannot be
retrofitted with a request kind; it must become three real commands:

| Command | Does | Must not |
|---|---|---|
| `generate_artifact()` | render, seal, store code | execute, publish |
| `verify_artifact()` | execute the exact sealed artifact in sandbox, record results | publish |
| `publish_verified_artifact()` | revalidate current verification, recheck capability, publish atomically | execute |

**`VerificationExecutionIdentityV1`, separate from `sandbox_execution_hash` [R8].** The existing hash
requires a `capability_attestation_id`, so it cannot honestly identify an execution with no
publication intention. The new identity covers rendered artifact identity · environment · business
date · resolved parameters · exact input snapshots · verification check-suite version · compiler and
renderer versions. **Publication capability is checked only by `publish_verified_artifact()`.**

Three lifecycles kept apart: `FeatureBuildRequest` (intent) · `FeatureBuildAttempt` (retryable) ·
`MaterializationRun` (outcome).

> **Acceptance:** generation reaches sealed code with no run event on the plane; verification
> executes with no publication capability present; publication refuses on a stale or absent
> verification; no path from generate to publish exists without two further explicit commands.

### S8 — development execution proof

Against the synthetic ledger, the **generated Spark code** — not a test-side Python function —
reproduces the hand-calculated result: posted debit AED **included** · credit **excluded** ·
pending/failed **excluded** · reversed original **excluded** · reversal row **excluded** · USD
**converted at the booking-date rate** · out-of-window **excluded**.

**Required mutation failures:** drop the status filter · count reversal rows · use one flat FX rate ·
use settlement instead of booking date · ignore direction · shift the window boundary.

**`OperatorExecutionProofV1` [R8]** — a real store. Advertised operations become
`renderer-supported ∩ current successful execution proof`. **Revision 7 cited
`engine_capability.py` as the precedent for execution-derived advertising; it is not** — it derives
from `renderable_aggregations()`, i.e. renderer dispatch, and the tests pin that. It is the
precedent for *deriving rather than hand-maintaining*, and nothing more.

**Close the pilot's blockers here:** the DATE-clock refusal outside UTC (PIT carries no clock type)
and A.32 🔴 (`requirements.lock` cannot construct the rendered catalog).

> **Acceptance:** all seven expectations hold against generated code; all six mutations fail; each
> proof names S1's frozen gold hash, so a changed expectation invalidates it.

### S9 — on-demand sandbox verification

**Governing rule:**

> Code generation is immediate. **Verification is user-triggered.** Its checks run in dependency
> waves once triggered, and every result is attached to the exact generated artifact.

**Checks are a DAG, not a flat parallel set [R8]:**

```
build + static checks → execute → ⟨keys · grain · types · nulls · inflation · profile⟩ → fold
```

Only the last wave is parallel. The request is one durable object with per-check results, so a
failure names **which** check failed.

**Verification does not write `DATA-CHECKED` [R8].** Revision 7 had the wrong mechanism. Store the
verification as **its own immutable evidence**; if it satisfies a contract requirement, link it to
the exact `contract_id` and `requirement_id` and **emit `EXTERNAL_PASSED`**, letting
`feature_validation_projection` derive `DATA-CHECKED` when every blocking requirement holds a current
pass. A verification may occur before a feature version exists at all. **Never write
`USEFULNESS-CHECKED` from materialization verification.**

**Staleness is computed on read, never pushed on write.** The reader recompares the recorded hash set
— generated project · V2 formula · resolved policies · binding revisions · compiler and renderer
versions · input snapshot — against current. Disagreement *is* staleness. Detected automatically,
**never re-run automatically**: the state names what changed and waits.

**Profiling splits in two [R8]:** blocking output-sanity checks belong to verification; advisory
EDA/profile generation is a separate attempt whose failure cannot fail a validated publication.

> **Acceptance:** a verification survives restart with per-check results intact; changing any one
> recorded input flips a pass to stale and names it; a pass emits `EXTERNAL_PASSED` and writes no
> stamp directly; nothing writes `USEFULNESS-CHECKED`; `Generated — not verified` is never rendered
> by a failure path.

### S10 — atomic sandbox publication

Require a current passing verification *(provisional — see the open decision)*, recheck publication
capability and policy dependencies at publish time, then publish the **whole group atomically** with
table-level visibility and reconciliation.

**OPEN — the operator's decision, not this plan's:** may an unverified generation be published if
explicitly marked as such?

> **Acceptance:** a stale verification blocks publication; capability revoked between verify and
> publish blocks publication; a partial group never becomes visible.

### S11 — UI integration

Wire the exact selected option to **Generate → Verify → Publish**, three actions never collapsed.

**States:** `Generated — not verified` · `Verification queued` · `Verification running` ·
`Verification passed` · `Verification failed` · `Verification stale`.

```
Posted debit amount · 90 days
  Formula        Ready
  Policies       Resolved
  Code           Generated
  Verification   Not run
  [View generated code]   [Verify in sandbox]
```

**The UI must disclose group scope [R8].** Verification and publication are group-wide
(`logical_group_name`), while these read as per-feature actions. Show when several selected features
share one atomic group, and that **one failed member prevents publishing the group**.

Unresolved policies show the exact blocker: *"Currency conversion unresolved: no governed
booking-date rate source is bound."* Selection stays local and reversible; **Generate code** creates
the durable request. Needs the missing materialization POST client — `api.ts` has only the GET.

> **Acceptance:** no path reaches execution without an explicit click; a stale result renders as
> stale, not as a failure; group scope is visible before a publish action; a candidate outside the
> shadow top 12 is generable from the UI.

### S12 — incremental V2 expansion

Current-vs-previous differences → ratios and percentage changes → signed sums → avg/min/max → stddev
and percentiles → recency and streaks → slope and trend → concentration (HHI, top share) →
future-horizon and allocation.

**One family at a time, only when renderer support and gold execution proof both exist (rule 10).**

**Also here: V2's live-provider acceptance.** At least one real-provider structured-output test
before V2-as-default is honest — today only recorded fixtures exercise the path.

> **Acceptance:** the advertised set is computed as `renderer-supported ∩ execution-proved`; adding
> a renderer branch without a proof advertises nothing.

## 2. Carried forward

**Runs in parallel, gated at merge:** behavioural (not route-existence) frontend↔backend contract
tests, since `/features/recipe` exists and refuses everything · hide the retired "Write definitions"
control · recognition correctness in full · ruff ratchet (79 repo-wide, 35 in `src/`).

**Narrow the leakage claim.** Carried since revision 2 **without its context, which no revision has
restated** — recover the original finding from its source review before acting.

**Still required:** canonical typed planning-request JSON persisted, since `repr()` cannot
reconstruct a request · the group contract mapped onto `MaterializationContractV1` and
`FeatureGroupPlanV1` with a cheap preflight and a post-compile authoritative split on the full
contract hash · a **qualified evaluation-artifact reader** with a validity contract (migration 1029's
four tables exist; a stale pass is not a pass) · the **as-of snapshot window shape**, which alone
takes derivable blueprints from 90 to ~192.

**Execution infrastructure, feeding S8 and S9:** `business_dt` on the API — absent entirely today ·
live server-side Hive schema read · captured inventory · a real remote execution seam
(`LocalClusterSubmitter` is a local subprocess on an image with no Java) · a dedicated materialization
worker, since a long compile currently blocks relays, timers, projections and ingestion · persistent
artifacts, since the generated project sits on ephemeral worker disk.

**Out of scope, recorded so it is not assumed:** cross-catalog execution refuses by construction
(`chain.py:906`); content-addressed input snapshots need the deferred Iceberg layer.

## 3. Sequencing

```
S1 decisions ─► S2 identity ─► S3 policy realizations ─► S4 executable revision
   ─► S5 typing + IR ─► S6 generate-only ─► S7 split the chain
   ─► S8 execution proof ⟨first real run⟩ ─► S9 verify ─► S10 publish ─► S11 UI ─► S12 expand
                                    ▲
      execution infrastructure ─────┘   (parallel: integrity gate · recognition · leakage · ruff)
```

**What moved, and why.** Decisions and identity lead because everything downstream addresses a
feature (S2) under semantics someone chose (S1). Policy realizations precede the executable artifact,
because revision 7's artifact could not be assembled before its contents existed. Generation is
separated from the chain split so the renderer's consumption proof lands before there is a command
boundary to test it across.

**No duration estimate.** Five revisions have now carried one that a review invalidated.

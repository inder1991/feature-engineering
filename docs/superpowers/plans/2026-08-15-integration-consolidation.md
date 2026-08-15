# V2 as the feature language, on shared execution

**Date:** 2026-08-16 · **Revision 6** — revision 5 plus the generate → verify → publish workflow (§1.5), which
separates what code generation may claim from what only execution can prove.

> **V1 remains the stable compatibility language. V2 becomes the intelligent, policy-aware language
> for all new features. LLMs create and enrich V2 formulas; a deterministic V2 compiler resolves
> their banking meaning and runs them through the existing Spark/Kedro materialization platform.**

**Do not reduce V2 to V1. Do not build a second V2 platform.**

Revision 4 proposed "lowering" — translating a v2 formula into a stored `TypedFormulaV1`. **That is
withdrawn.** It destroys policy identity: the stored artifact would be a v1 formula with filters
baked in, and nothing would record that it ran under `eligible_status:foundation-posted-events`. When
that policy later changed, no executed feature would link back to it. Reuse V1's **execution
machinery**; never V1's **artifact**.

## 0. Position

| | V1 | V2 |
|---|---|---|
| Role | frozen compatibility language | the language for all new features |
| Gets | correctness fixes only | policy awareness, multi-expression, advanced aggregation, LLM authorship |
| Users choose? | **No.** The platform decides internally; the UI never asks for a version | |

**Shared execution** means V1 and V2 use the same Spark packaging, source binding, Hive access,
time-window implementation where semantics match, aggregation functions, staging, validation,
feature-table assembly, publication and run tracking. **V2 adds preparation and calculation
operators — not a second materialization product.**

## 0.1 Verified before planning

- **The pilot genuinely exercises four policies.** `posted_debit_amount` carries
  `eligible_status:foundation-posted-events`, `direction_sign:foundation-signed-by-indicator`,
  `reversal_correction:foundation-flag-or-code`, `currency_conversion:foundation-base-currency`.
  `allocation_policy_ref` is **empty** — allocation is genuinely the fifth resolver, not exercised here.
- **Rule 2 is the current state, not a hypothetical.** Three of those four identifiers appear in
  **zero** non-recipe source files. They are strings pointing at nothing.
- **The foundation is better than that sounds.** `banking_policies.py` (BR-10) already registers the
  policy KINDS, what a record of each must declare, and each kind's **resolution home** —
  eligible-status and reversal in `data_agent.eligibility`, direction/sign in the BR-5 authority
  envelope. Its own docstring states the target: *"a source must declare or resolve its
  lifecycle/status policy before filtered formulas are materialization-ready."* The kinds exist; the
  **realizations** do not.
- **`materialize/` never sees `authority_refs`.** A v2 formula's policies are declarations no
  execution honours today.
- **Admission consumes `TypedFormulaV1` only**; BR-6 never minted a `TypedFormulaV2`.
- **The engine advertises four aggregates** — `sum`, `count_rows`, `count_non_null`, `count_distinct`.
- **`merchant_mcc_diversity` is ineligible** on three contradictions (customer vs merchant grain;
  additive vs non-additive; eligibility rules the v1 expression contract cannot express at all).

## 0.2 Non-negotiable rules — this plan's invariants

1. No V2 formula executes with an unresolved policy reference.
2. **A non-empty policy string is not proof that the policy exists.**
3. Every resolved policy must be **consumed** by the execution plan.
4. No policy may disappear when V2 reuses existing operators.
5. Policy revisions and physical bindings enter executable identity.
6. V1 formula hashes remain unchanged.
7. V1 and V2 share publication and validation machinery.
8. The LLM proposes and explains; deterministic code executes.
9. The UI never asks a user to choose a formula version.
10. **"Supported" means generated code has been executed successfully** — not that the schema can
    describe the operation.

## 0.3 Division of labour

**LLM:** understand the hypothesis · select recipes · propose candidates · map roles to concepts ·
recommend physical columns · interpret source-specific status descriptions · propose debit/credit
conventions and reversal representations · identify currency and rate columns · produce structured V2
formulas · critique · explain refusals · summarise results.

**Deterministic code:** validate V2 structure · bind columns · resolve policy records · check every
policy is executable · compile the execution plan · generate Spark · execute arithmetic · validate
output · publish. **The LLM never injects free-form SQL into the pipeline.**

**Human confirmation does not gate sandbox experimentation.** An LLM-proposed policy is usable when it
carries producer, confidence, evidence, source columns, observed values, and model/prompt version —
disclosed in the UI. **Missing or contradictory semantics still refuse**, because that is a
calculation problem, not a governance preference.

## 1. Phases

### P1 — reconcile and freeze the exemplar
Reconcile `posted_debit_amount` across recipe, formula and gold data: one canonical policy-reference
namespace, one timezone, one window-boundary convention, one reversal representation for the gold
case, one currency-rate timing rule, one window length. Freeze as the canonical reviewed expectation
with one canonical expected result.

**Enumerate and record what is found rather than assuming the list above is complete** —
`merchant_mcc_diversity` was carried as one contradiction and proved to have three.

### P2 — the executable V2 artifact and admission
`ExecutableFormulaV2`: source proposal + proposal hash · resolved output policy + policy hash ·
physical column bindings · resolved status / direction / reversal / currency / allocation policies ·
policy realization hashes · compiler-lowering version. Restored from durable authoring state, both
hashes verified. **V2 admission added; V1 admission untouched (rule 6).**

### P3 — policy realizations *(after physical binding — a reference means different SQL per source)*
Typed resolvers for **eligible status · direction/sign · reversal · currency conversion ·
allocation**, each pinning realization revision and physical dependencies into executable identity
(rule 5). Resolution homes are BR-10's, not a new store.

- **Status** → status column, eligible values, null behaviour.
- **Direction** → representation (positive magnitude + indicator, or signed amount), direction
  column, debit values, amount normalization (raw, or `ABS`).
- **Reversal** → flag/code · linked reversal transaction · compensating transaction · status history ·
  negative entry. **Each mode gets its own deterministic implementation; unsupported modes refuse by
  name and are never approximated as a flag.**
- **Currency** → currency column, base currency, rate table, source/target keys, effective timestamp,
  booking-vs-settlement rule, rate direction, rounding.
- **Allocation** → full · equal split · ownership percentage · primary owner · other governed method.

### P4 — the V2 execution IR
A bounded, typed plan — **not an unlimited SQL language**: `Scan · Filter · TimeWindow ·
ReversalNeutralization · Join · CalculatedColumn · Aggregate · FinalCombination`. Implement only what
the pilot needs. **Reuse existing V1 window and SUM implementations where semantics match exactly.**

For the pilot: scan → filter eligible statuses → apply debit direction → neutralize reversals →
trailing window → join booking-date FX → `normalized_amount = amount × rate` → group by account →
`SUM`.

**Safe reuse requires BOTH checks (rule 4):** does the structure fit existing operators, **and** can
every resolved policy be implemented by them? Only then is the V1 computation path reused — and the
formula **remains V2** for identity, audit and provenance.

### P5 — Spark/Kedro rendering
Readable nodes inside the existing generated project: `filter_eligible_transactions` ·
`normalize_transaction_direction` · `neutralize_reversals` · `join_booking_fx_rates` ·
`calculate_base_currency_amount` · `aggregate_posted_debit_amount` · `validate_posted_debit_amount`.

### P6 — execution verification
Against the synthetic ledger, the **generated Spark code** — not a test-side Python function —
reproduces the hand-calculated result: posted debit AED **included** · credit **excluded** ·
pending/failed **excluded** · reversed original **excluded** · reversal row **excluded** · USD
**converted at the booking-date rate** · out-of-window **excluded**.

**Required mutation failures:** drop the status filter · count reversal rows · use one flat FX rate ·
use settlement instead of booking date · ignore direction · shift the window boundary. Each must
change or fail the expected result.

**Infrastructure this phase requires** (assumed by the direction, enumerated here): `business_dt` on
the API — absent today, so every public request is compile-only · **live Hive schema read
server-side** (`published_schema` stops being a caller assertion) · captured inventory · a **real
remote execution seam** (`LocalClusterSubmitter` is a local subprocess on an image with no Java) ·
**a dedicated materialization worker** before any cluster run — a long compile currently blocks
relays, timers, projections and ingestion · **persistent artifacts**, since the generated project sits
on ephemeral worker disk and a restart loses the sealed bytes. Then the approved Hive pilot data.

### P7 — the feature-generation UI
Stages: formula authored · columns bound · **policies resolved** · execution compiled · Kedro project
generated · cluster run submitted · output validated · feature published. Unresolved policies show the
exact blocker: *"Currency conversion unresolved: no governed booking-date rate source is bound."*

Requires the build-request seam: selection stays **local and reversible**; the explicit **Build in
sandbox** click creates the durable request. Three lifecycles kept apart — `FeatureBuildRequest`
(intent) · `FeatureBuildAttempt` (retryable) · `MaterializationRun` (outcome). Publication narrowed to
**table-level atomic visibility** with reconciliation. Profiling is a **separate attempt** whose
failure cannot fail a validated publication.

### P7b — the generate → verify → publish workflow **[R6]**

**Governing rule:**

> Code generation is immediate. **Verification is user-triggered.** Its checks run in parallel once
> triggered, and every result is attached to the exact generated artifact.

Nothing executes because code appeared. Generation answers *"can this be generated honestly?"* —
never *"does this work?"*

#### Three distinct actions, never collapsed

| Action | Does | Available |
|---|---|---|
| **Generate code** | produces visible, sealed code; executes nothing | when policies resolve |
| **Verify in sandbox** | on-demand execution and correctness validation | after generation |
| **Publish to sandbox** | promotes the verified generation | after a passing verification |

#### What generation checks, automatically

These are the honesty preconditions, and they run as part of generation:

- the formula is structurally valid;
- every required column is bound;
- **every policy reference is resolved** (invariant 1);
- the generated code contains **no placeholders**;
- formula and artifact hashes are valid;
- **the generated code consumes every declared policy** (invariant 3 — checked here, at the one point
  where the plan and the emitted code are both in hand).

Passing these means the code was generated honestly. **It claims nothing about execution.**

#### What verification runs, on demand and in parallel

Build the generated project · run gold-data semantic tests · Spark compilation checks · execute on
sandbox/Hadoop · validate keys and grain · validate types and null policies · check row-count
inflation · profile the output · compare expected against actual semantics.

Independent checks run concurrently; the request is one durable object with per-check results, so a
failure names **which** check failed rather than failing the set.

#### States

`Generated — not verified` · `Verification queued` · `Verification running` · `Verification passed` ·
`Verification failed` · **`Verification stale`**

```
Posted debit amount · 90 days
  Formula        Ready
  Policies       Resolved
  Code           Generated
  Verification   Not run
  [View generated code]   [Verify in sandbox]
```

After a pass, the result is itemised — project builds · gold-data result matches · Spark execution
completed · output grain is account · no duplicate account/date keys · output types match · profile
completed — with `[View results] [Run again] [Publish to sandbox]`.

#### Verification is bound to the exact artifact

A result records: generated project hash · V2 formula hash · resolved policy hashes · physical binding
revisions · compiler and renderer versions · verification dataset or input snapshot · the result.

**Staleness is detected automatically; re-verification is not.** If the formula, code, binding or
policy moves, the state flips to *"Verification stale — formula or dependency changed"* and **names
what changed**. The platform does **not** silently re-run: spending compute is the user's decision,
and a quietly-refreshed pass would hide the fact that the thing verified is no longer the thing built.

#### Two facts checked before writing this, both of which constrain the build

**There is no state today between "code generated" and "run submitted."** The run lifecycle
(`control_plane.py:142-149`) is one continuous sequence — `PREPARED → SUBMITTED → COMPUTED →
VALIDATED → PUBLISHED | REFUSED`. A run that is prepared proceeds. So this is **not a UI relabel**:
the seam has to exist in the control plane. Verification is therefore modelled as **its own request
kind against a sealed generation**, not as a materialization run that stops early — a run that
halts before submission is indistinguishable from one that failed, and `Generated — not verified`
must never read as a failure.

**A verification vocabulary already exists, and must not be duplicated.**
`governance/attributes.py:14` defines `VERIFICATION_STAMPS = ("DESIGN-CHECKED", "DATA-CHECKED",
"USEFULNESS-CHECKED")`, ordered, with `predicates.py:15` gating on rank. The new result **feeds that
ladder** — a passing sandbox verification is what earns `DATA-CHECKED` — rather than introducing a
parallel notion. Two disagreeing definitions of "verified" in one platform is exactly the class of
defect this plan exists to remove. Note what the ladder already knows and the six UI states do not:
`USEFULNESS-CHECKED` is a further rung, so **"Verification passed" means the code is correct, never
that the feature is useful.** The UI must not let the tick imply the second.

#### Two notes

- **Verification is execution**, so it needs everything P6 enumerates — `business_dt`, live Hive
  reads, a real remote submitter, the dedicated worker, persistent artifacts. Making the trigger
  explicit changes *when* that infrastructure is exercised, not whether it is needed.
- **Profiling appears twice, deliberately.** In verification it is a *check* (did the output look
  sane?). At publication it is the durable `feature_group_profile` / `feature_column_profile` record,
  a separate attempt whose failure must not fail a validated publication.

**OPEN — the operator's decision, not this plan's:** may an *unverified* generation be published if
explicitly marked as such? The direction leaves it open. Until it is answered, publication requires a
passing verification.

### P8 — expand V2 incrementally
Current-vs-previous differences → ratios and percentage changes → signed sums (credits − debits −
fees) → avg/min/max → stddev and percentiles → recency and streaks → slope and trend → concentration
(HHI, top share) → future-horizon and allocation.

**An operation is advertised only after generated Spark code has executed against a reviewed gold
dataset (rule 10).**

## 2. Carried forward from earlier revisions

**Runs in parallel, gated at merge, blocking nothing:** the integrity gate — behavioural (not
route-existence) frontend↔backend contract tests, since `/features/recipe` exists and refuses
everything · hide the retired "Write definitions" control · narrow the leakage claim now · recognition
correctness in full · ruff ratchet.

**Still required, unchanged:** exact selected-feature identity (`feature_definition_key` /
`revision` / `executable_revision_id` / `output_column_name` / `display_label`, applied **at contract
creation** or `govern.py`'s one-feature-per-name lookup collapses variants) · canonical typed
planning-request JSON persisted, since `repr()` cannot reconstruct a request · the group contract
mapped onto `MaterializationContractV1` and `FeatureGroupPlanV1` with a cheap preflight and a
post-compile authoritative split on the full contract hash · a **qualified evaluation-artifact
reader** with a validity contract (migration 1029's four tables exist; a stale pass is not a pass) ·
the **as-of snapshot window shape**, which alone takes derivable blueprints from 90 to ~192.

## 3. Sequencing

```
P1 ─► P2 ─► P3 ─► P4 ─► P5 ─► P6 ⟨first real run⟩ ─► P7 ─► P7b ⟨generate/verify/publish⟩ ─► P8
                                    ▲
        identity · group contract · execution infrastructure ─┘
        (parallel: integrity gate · recognition · leakage wording · ruff)
```

**No duration estimate.** The last three revisions each carried one that a review invalidated. P1's
reconciliation and P3's realization surface are what make estimation possible; until then a number
would be a guess with a decimal point.

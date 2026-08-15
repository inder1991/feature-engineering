# V2 as the feature language, on shared execution

**Date:** 2026-08-16 · **Revision 7** — revision 6 corrected by a detailed self-review. Three
blockers and four major findings folded in; the largest is that **revision 6 proposed building a type
the codebase already has**, one section after warning against exactly that.

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
| Users choose? | no — the platform decides internally | no — the UI never asks for a version |

**Shared execution** means V1 and V2 use the same Spark packaging, source binding, Hive access,
time-window implementation, aggregation functions, staging, validation, feature-table assembly,
publication and run tracking. **V2 adds preparation and calculation operators — not a second
materialization product.**

**V2-as-default is not flag-guarded, deliberately.** The platform is pre-live and the standing
steer is to avoid flags that exist only to defer a decision. Stated here so its absence reads as a
choice rather than an oversight.

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
  envelope. The kinds exist; the **realizations** do not.
- **The window is already a governed type.** `expression_ir.py:284` `PitSpec` carries
  `window_basis · window_length · window_unit · window_start_inclusive · window_end_inclusive ·
  window_timezone` plus the clock (`event_time_ref`, `availability_ref`, `availability_basis`,
  `availability_lag_hours`), and `nodes_compute.py:956` already renders the window filter from it.
  **[R7 — this invalidated revision 6's P1 and P4.]**
- **The renderer already writes a per-node staging manifest** bound to the computation by `ir_hash`
  (`nodes_compute.py:2170,2197`). That is the hook rule 3 needs. **[R7]**
- **A verification vocabulary already exists.** `governance/attributes.py:14` defines the ordered
  `VERIFICATION_STAMPS = ("DESIGN-CHECKED", "DATA-CHECKED", "USEFULNESS-CHECKED")`, gated by rank at
  `predicates.py:15`.
- **`materialize/` never sees `authority_refs`.** A v2 formula's policies are declarations no
  execution honours today.
- **Admission consumes `TypedFormulaV1` only**; BR-6 never minted a `TypedFormulaV2`.
- **The engine advertises four aggregates** — `sum`, `count_rows`, `count_non_null`, `count_distinct`.
- **`merchant_mcc_diversity` is ineligible** on three contradictions (customer vs merchant grain;
  additive vs non-additive; eligibility rules the v1 expression contract cannot express at all).
- **The run lifecycle has no state between "generated" and "submitted."** `control_plane.py:142-149`
  runs `PREPARED → SUBMITTED → COMPUTED → VALIDATED → PUBLISHED | REFUSED` continuously.

**Standing instruction for every phase below: check what exists before designing what to add.** Two
revisions in a row proposed building something the codebase already had. That is this plan's
characteristic failure mode, and it is cheaper to prevent than to review out.

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

**Rule 10's bootstrap, stated because it is circular without it [R7].** Advertise-after-execution
plus a UI that offers only advertised operations means nothing could ever reach a first execution.
The escape is that **an operator's first execution happens on the development gold-dataset path
(P6), outside the product**. P8's ladder advances there and the product only ever offers what that
path has already proven. No operation is advertised to a user in order to become executable.

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

## 0.4 Two words that must not blur [R7]

- **Execution proof** (P6) — development-time, mutation-tested, against a reviewed gold dataset. It
  is how an operator earns the right to be advertised under rule 10.
- **Sandbox verification** (P2b, P7) — the user-triggered product capability, run against a real
  generation on demand.

Revision 6 called both "verification". They have different triggers, audiences and durability, and a
shared name would have made rule 10 unauditable.

## 0.5 Migration reservations [R7]

**1071 is the highest today. Verify at execution before use.** Reserved: **1072** executable V2
formula · **1073** policy realizations · **1074** verification request and result. This repo has a
recorded history of double-allocation; nothing below may take a number ad hoc.

## 1. Phases

### P1 — reconcile and freeze the exemplar

**Narrowed by `PitSpec` [R7].** Timezone, window boundaries and window length are **not conventions
to choose** — they are governed fields that already exist and already render. P1 checks the recipe
and gold data *against* them and records disagreement; it does not invent a convention.

What remains genuinely open for `posted_debit_amount`: one canonical policy-reference namespace · one
reversal representation for the gold case · one currency-rate timing rule · one canonical expected
result — the same result set P6 asserts against.

**Separate finding from deciding [R7].** The executor **enumerates contradictions and proposes**;
the reversal representation and the rate-timing rule are governance decisions and are **the user's or
operator's to settle**. Do not resolve them inside the task.

**Enumerate rather than assume this list is complete** — `merchant_mcc_diversity` was carried as one
contradiction and proved to have three.

> **Acceptance:** every contradiction found is recorded with its two sides and its source; each is
> marked *mechanical* or *needs-a-decision*; the frozen expectation is a stored artifact with a hash,
> not a prose paragraph; a test asserts the recipe's declared window equals the `PitSpec` the
> compiler produces for it.

### P2 — the executable V2 artifact and admission

`ExecutableFormulaV2`: source proposal + proposal hash · resolved output policy + policy hash ·
physical column bindings · resolved status / direction / reversal / currency / allocation policies ·
policy realization hashes · compiler-lowering version. Restored from durable authoring state, both
hashes verified. **V2 admission added; V1 admission untouched (rule 6).** Migration **1072**.

> **Acceptance:** a V2 artifact round-trips through durable state with both hashes re-verified on
> restore; V1 admission is byte-unchanged; **a frozen-bytes test pins a corpus of existing V1 formula
> hashes and fails if any moves** — rule 6 is the compatibility guarantee of this whole program and
> was untested through revision 6 **[R7]**; a v3-or-later formula reaching V2 admission refuses by
> name, as `admission.py:228` already does for v2 reaching v1.

### P2b — the sandbox verification request kind [R7 — moved from P7b]

**Moved here from the end of the plan, because it is an artifact-and-lifecycle change, not a UI
addition.** Revision 6 sequenced it after P6 and P7, where it would have re-modelled a verification
path P6 had already built.

**Governing rule:**

> Code generation is immediate. **Verification is user-triggered.** Its checks run in parallel once
> triggered, and every result is attached to the exact generated artifact.

Nothing executes because code appeared. Generation answers *"can this be generated honestly?"* —
never *"does this work?"*

**Its own request kind, not a run that stops early.** The lifecycle in `control_plane.py:142-149`
has no state between generated and submitted, so the seam must exist in the control plane. A run
halted before submission is indistinguishable from one that failed, and `Generated — not verified`
must never read as a failure. Migration **1074**.

**It feeds the existing ladder.** A passing sandbox verification is what earns **`DATA-CHECKED`** in
`VERIFICATION_STAMPS`; it does not introduce a parallel notion. The ladder also knows something the
UI states do not — `USEFULNESS-CHECKED` is a further rung — so **a pass means the code is correct,
never that the feature is useful**, and the UI must not let the tick imply the second.

#### What generation checks, automatically

The honesty preconditions, run as part of generation: the formula is structurally valid · every
required column is bound · **every policy reference is resolved** (rule 1) · the generated code
contains **no placeholders** · formula and artifact hashes are valid · **the generated code consumes
every declared policy** (rule 3 — mechanism in P5).

Passing these means the code was generated honestly. **It claims nothing about execution.**

#### What verification runs, on demand and in parallel

Build the generated project · run gold-data semantic tests · Spark compilation checks · execute on
sandbox/Hadoop · validate keys and grain · validate types and null policies · check row-count
inflation · profile the output · compare expected against actual semantics.

Independent checks run concurrently; the request is one durable object with per-check results, so a
failure names **which** check failed rather than failing the set.

#### Binding, and staleness

A result records: generated project hash · V2 formula hash · resolved policy hashes · physical
binding revisions · compiler and renderer versions · verification dataset or input snapshot · the
result.

**Staleness is computed on read, never pushed on write [R7].** Revision 6 said the state "flips",
which implies a background watcher nobody owns. The reader recomputes the current hash set and
compares it with the recorded one; disagreement *is* staleness. This is cheap, has no invalidation
fan-out, and cannot go stale itself.

**Detected automatically; never re-run automatically.** The state shows *"Verification stale — formula
or dependency changed"* and **names what changed**. Spending compute is the user's decision, and a
quietly-refreshed pass would hide that the thing verified is no longer the thing built.

> **Acceptance:** a verification request survives a restart with per-check results intact; changing
> any one recorded input flips a passing result to stale and names that input; a stale result is
> never silently re-run; a passing result writes `DATA-CHECKED` and nothing writes
> `USEFULNESS-CHECKED`; `Generated — not verified` is never rendered by a failure path.

**OPEN — the operator's decision, not this plan's:** may an *unverified* generation be published if
explicitly marked as such? The direction leaves it open. Until it is answered, publication requires a
passing verification.

### P3 — policy realizations *(after physical binding — a reference means different SQL per source)*

Typed resolvers for **eligible status · direction/sign · reversal · currency conversion ·
allocation**, each pinning realization revision and physical dependencies into executable identity
(rule 5). Resolution homes are BR-10's, not a new store. Migration **1073**.

- **Status** → status column, eligible values, null behaviour.
- **Direction** → representation (positive magnitude + indicator, or signed amount), direction
  column, debit values, amount normalization (raw, or `ABS`).
- **Reversal** → flag/code · linked reversal transaction · compensating transaction · status history ·
  negative entry. **Each mode gets its own deterministic implementation; unsupported modes refuse by
  name and are never approximated as a flag.**
- **Currency** → currency column, base currency, rate table, source/target keys, effective timestamp,
  booking-vs-settlement rule, rate direction, rounding.
- **Allocation** → full · equal split · ownership percentage · primary owner · other governed method.

> **Acceptance:** each resolver refuses by name on an unresolvable reference rather than defaulting;
> a realization's revision and physical dependencies appear in the executable identity hash, and
> changing either changes the hash; an unsupported reversal mode refuses and names the mode.

### P4 — the V2 execution IR

A bounded, typed plan — **not an unlimited SQL language**: `Scan · Filter · ReversalNeutralization ·
Join · CalculatedColumn · Aggregate · FinalCombination`. Implement only what the pilot needs.

**`TimeWindow` is deliberately absent [R7].** Revision 6 listed it as a new operator. `PitSpec`
already models the window and the renderer already renders it, so a second representation would be
the exact duplication rule 4 and §0.1 exist to prevent. **The window arrives via `PitSpec`; P4 adds
no window type.**

For the pilot: scan → filter eligible statuses → apply debit direction → neutralize reversals →
window *(from `PitSpec`)* → join booking-date FX → `normalized_amount = amount × rate` → group by
account → `SUM`.

**Safe reuse requires BOTH checks (rule 4):** does the structure fit existing operators, **and** can
every resolved policy be implemented by them? Only then is the V1 computation path reused — and the
formula **remains V2** for identity, audit and provenance.

> **Acceptance:** the pilot compiles to a plan containing no window node, with its window sourced
> from `PitSpec`; a policy that no existing operator can implement refuses rather than compiling to a
> plan that silently drops it; each IR op has one implementation.

### P5 — Spark/Kedro rendering, and the rule-3 mechanism

Readable nodes inside the existing generated project: `filter_eligible_transactions` ·
`normalize_transaction_direction` · `neutralize_reversals` · `join_booking_fx_rates` ·
`calculate_base_currency_amount` · `aggregate_posted_debit_amount` · `validate_posted_debit_amount`.
**No window node — the window renders inside the calculation node from `PitSpec`, as it does for V1
today [R7].**

**Rule 3 gets a named mechanism, on machinery that already exists [R7].** Revision 6 asserted that
consumption is "checked where the plan and the code are both in hand" without saying how. The
renderer already writes a per-node staging manifest bound by `ir_hash` (`nodes_compute.py:2170`).
**Each node records the policy realization hashes it consumed into that manifest; the gate compares
the union against the resolved set and refuses on any difference.** Unconsumed policy and
unresolved policy are distinct refusals with distinct names.

> **Acceptance:** removing a policy's use from a rendered node makes the gate refuse and name the
> policy; the manifest's consumed set is a function of the emitted code, not of the plan that
> requested it, or the check proves nothing.

### P6 — execution proof *(development-time)*

Against the synthetic ledger, the **generated Spark code** — not a test-side Python function —
reproduces the hand-calculated result: posted debit AED **included** · credit **excluded** ·
pending/failed **excluded** · reversed original **excluded** · reversal row **excluded** · USD
**converted at the booking-date rate** · out-of-window **excluded**.

**Required mutation failures:** drop the status filter · count reversal rows · use one flat FX rate ·
use settlement instead of booking date · ignore direction · shift the window boundary. Each must
change or fail the expected result.

This is the path rule 10's bootstrap names: **an operator becomes advertisable here, outside the
product.**

**Infrastructure this phase requires** (assumed by the direction, enumerated here): `business_dt` on
the API — absent today, so every public request is compile-only · **live Hive schema read
server-side** (`published_schema` stops being a caller assertion) · captured inventory · a **real
remote execution seam** (`LocalClusterSubmitter` is a local subprocess on an image with no Java) ·
**a dedicated materialization worker** before any cluster run — a long compile currently blocks
relays, timers, projections and ingestion · **persistent artifacts**, since the generated project sits
on ephemeral worker disk and a restart loses the sealed bytes. Then the approved Hive pilot data.

**P2b's sandbox verification needs this same infrastructure.** Making the trigger explicit changes
*when* it is exercised, not whether it is needed.

> **Acceptance:** all seven expectations hold against generated code; all six mutations fail; the
> proof names the gold artifact hash frozen in P1, so a changed expectation invalidates the proof.

### P7 — the feature-generation UI

**Rewritten to P2b's action model [R7].** Revision 6 left this section's single **"Build in sandbox"**
click in place beside P2b's three actions; an implementer reading top-to-bottom would have built the
one-click version.

**Three actions, never collapsed:**

| Action | Does | Available |
|---|---|---|
| **Generate code** | produces visible, sealed code; executes nothing | when policies resolve |
| **Verify in sandbox** | on-demand execution and correctness validation | after generation |
| **Publish to sandbox** | promotes the verified generation | after a passing verification |

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

After a pass, the result is itemised — project builds · gold-data result matches · Spark execution
completed · output grain is account · no duplicate account/date keys · output types match · profile
completed — with `[View results] [Run again] [Publish to sandbox]`.

Stages behind the surface: formula authored · columns bound · **policies resolved** · execution
compiled · Kedro project generated · verification run · output validated · feature published.
Unresolved policies show the exact blocker: *"Currency conversion unresolved: no governed
booking-date rate source is bound."*

Selection stays **local and reversible**; the explicit **Generate code** click is what creates the
durable request. Three lifecycles kept apart — `FeatureBuildRequest` (intent) · `FeatureBuildAttempt`
(retryable) · `MaterializationRun` (outcome). Publication narrowed to **table-level atomic
visibility** with reconciliation.

**Profiling appears twice, deliberately.** In verification it is a *check* (did the output look
sane?). At publication it is the durable `feature_group_profile` / `feature_column_profile` record, a
**separate attempt whose failure cannot fail a validated publication**.

> **Acceptance:** no path reaches execution without an explicit user click; a stale result renders as
> stale and not as a failure; the three lifecycles are separately queryable; a profiling failure
> leaves a validated publication published.

### P8 — expand V2 incrementally

Current-vs-previous differences → ratios and percentage changes → signed sums (credits − debits −
fees) → avg/min/max → stddev and percentiles → recency and streaks → slope and trend → concentration
(HHI, top share) → future-horizon and allocation.

**An operation is advertised only after generated Spark code has executed against a reviewed gold
dataset (rule 10), on P6's development path.**

> **Acceptance:** the advertised set is derived from recorded execution proofs, never from a
> hand-maintained list — `engine_capability.py` already derives from the renderer's dispatch and is
> the precedent.

## 2. Carried forward from earlier revisions

**Runs in parallel, gated at merge, blocking nothing:** the integrity gate — behavioural (not
route-existence) frontend↔backend contract tests, since `/features/recipe` exists and refuses
everything · hide the retired "Write definitions" control · recognition correctness in full · ruff
ratchet (79 repo-wide, 35 in `src/`).

**Narrow the leakage claim.** Carried since revision 2 **without its context, which this plan has
never restated [R7]** — recover the original finding from its source review before acting; do not
reconstruct it from this line.

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
P1 ─► P2 ─► P2b ─► P3 ─► P4 ─► P5 ─► P6 ⟨first real run⟩ ─► P7 ─► P8
                                        ▲
        identity · group contract · execution infrastructure ─┘
        (parallel: integrity gate · recognition · leakage wording · ruff)
```

**P2b moved forward of P3–P6 [R7].** It is the artifact and lifecycle for verification; building it
after P6 would re-model a path P6 had already implemented. P7 is then purely the surface over P2b.

**No duration estimate.** The last four revisions each carried one that a review invalidated. P1's
reconciliation and P3's realization surface are what make estimation possible; until then a number
would be a guess with a decimal point.

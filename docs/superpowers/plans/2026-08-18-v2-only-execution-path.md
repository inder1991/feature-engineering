# V2-only: from a READY formula to a published feature

**Decision (given):** one formula language. V2 semantics, wire format `formula_schema_version = 3`.
No V1/V2 router, no compatibility mode, no V1 migration, no byte-equivalence guarantee, no
user-visible version choice. The product is not live. V1's *execution* machinery is reused; V1's
*language* is not.

**Revision 3**, after two review rounds. Three rulings adopted (compile target, capability model, FX
ownership), two blocking contradictions fixed, three missing contracts added, and the deletion
inventory folded back into the earlier steps it changes. Changes are marked ▲ and the reasons kept,
because the corrections are the useful part.

**No open questions remain.** The plan is ready for task-level execution.

**Verification standard:** every number here was produced by running the shipped code, not counted
by eye. Revision 1 broke that standard once and the record is kept in §2.

---

## 0. Execution status — updated 2026-08-19

| Steps | State | Notes |
|---|---|---|
| **0–8** | **done** | migrations 1091, 1092, 1093 APPLIED to live (189 total) |
| 9–15 | not started | 9 is the narrow pilot — the first end-to-end proof |

**Found during execution, and not in the plan when it was written:**

* **Step 4** — the six typed computation fields were dropped by the candidate serializer, so
  identity described how a candidate *read* rather than what it *computes*. Fixed forward; the
  frozen v2 candidates refuse with `CANDIDATE_REGENERATION_REQUIRED`, and regeneration is an
  operator runbook (`docs/architecture/candidate-regeneration-runbook.md`), never an automatic
  retry.
* **Step 5** — three defects its own tests could not reach, all found by step 6 consuming its
  output: the IR carried resolved payloads where `DeclaredPoliciesV2` belongs, `row_selections` were
  read off the proposal (a field that does not exist there) and silently dropped, and compiling
  returned a bare IR where the plan says planned.
* **Step 5** — policy payloads did not record **when** their columns are read, which is the one
  fact that decides whether a policy leaks. Added as a required field with no default: a default of
  `event_time` would have made every policy pass the leakage gate by construction.
* **Step 8** — the ten sites were confirmed exactly ten, and the mechanism was worse than recorded.
  The two enums do not merely fail `is`: they compare EQUAL and **hash equal**, so a V2 member finds
  the right entry in a V1-keyed dispatch table. Dispatch working while identity fails is what let a
  V2 feature render down the wrong arm. Both halves are now one vocabulary, crossed once, in
  `compile_expression`, and enforced by `ExpressionExecutionIR.__post_init__`.

---

## 1. Rulings adopted

### Decision 1 — `FormulaExecutionIRV2` is the compile target

```
Typed Formula V3
  → PlannedFormulaExecutionIRV2
  → (leakage + authorization)
  → AuthorizedFormulaExecutionIRV2
  → OperatorGraphV2                  ← DERIVED VIEW, deterministic from the IR
  → Kedro/Spark renderer
```

The graph is derived for capability checking, dependency analysis, audit display, proof coverage and
explaining execution to a user. It is **not** a second independently authored executable form.

**▲ Verified, and it settles the matter:** `OperatorKindV2` has 13 members and **none of them is a
final-combination node**:

```
governed_scan, pit_availability_filter, semantic_selection, eligible_status_filter,
linked_reversal_survivor, as_of_fx_join, duplicate_rate_gate, missing_rate_gate,
quote_inversion, decimal_multiplication, aggregate, spine_left_join, group_assembly
```

There is no node for identity, ratio, difference or signed sum. The graph therefore *cannot* express
the final combination of any feature, and today it cannot truthfully claim to be the complete
executable form. Two honest options, and the plan takes the first:

1. **Add `FINAL_COMBINE`** with a variant per final operation. Keeps the graph able to describe a
   whole feature, which the capability model in Decision 2 needs.
2. Rename it an execution-topology/safety view and stop implying completeness.

### Decision 2 — capability by typed signature, bound to the build

Kind-level capability cannot express the true state, which is `sum` and `count_rows` supported while
`avg`, `median` and `percentile` are not — all four are `OperatorKindV2.AGGREGATE`.

Not 21 new top-level kinds. Keep the small topology vocabulary and qualify it:

```
engine_id
operator_kind          "aggregate"       "final_combine"   "semantic_selection"
operator_variant       "sum" | "avg"     "ratio"           "eligible_status"
renderer_build_hash    ← binds the proof to the code that produced it
renderer_dispatchable
execution_proof_hash
```

`renderer_build_hash` is the part that stops a stale proof outliving the code it was about. Without
it the renderer can change while an old proof stays nominally valid — a proof about a build that no
longer exists.

**This supersedes migration 1079's shape** and needs a new migration (1091), not an edit: 1079 is
applied on the live cluster.

---

## 2. The renderer gates — a corrected count

Revision 1 said five. The review said six. **Both were wrong; it is ten.**

```
 1   isinstance(ir, FormulaExecutionIRV1)        nodes_compute.py:2223   refuses LOUDLY — safe
 5   ` is FinalOperation.X `                     2447, 2586, 3081, 3086, 3144
 4   ` is AggregateFunction.X `                  2526, 2824, 2909, 2945
---
10   sites that must move atomically
```

**Why the nine are dangerous:** `FinalOperationV2.RATIO is FinalOperation.RATIO` is `False` —
different enum object, same name, same value. A V2 ratio does not fail; it takes the else-branch and
renders **something else**. Same for `AggregateFunctionV2.SUM`.

**▲ How revision 1 got it wrong, since the method matters more than the number.** It grepped four
enum names at once and reported the total. But `NullInput`, `EmptyWindowResult` and `FilterNode` are
*the same objects* in `schema` and `schema_v2` — re-exported, genuinely version-neutral. Eight of the
seventeen were comparisons that are perfectly fine. A plan that inflates a hazard is not safer than
one that understates it; it just moves the error.

**Rule: all ten move in one commit, or none.** Removing 2223 alone converts a loud refusal into a
wrong number, which here means a wrong feature in a credit model.

**▲ Beyond the sweep** — normalize V2 enum values **once at the renderer boundary**, validate against
a **closed dispatch table**, and refuse anything unknown by name. And migrate
`ExpressionExecutionIR.aggregation` off the V1 enum type: relying on string-enum equality is
accidental compatibility, not an execution contract.

**Structural guard worth its keep:** no ` is ` comparison against a V1 enum member anywhere under
`render/`. The defect class, stated once, in a form that cannot drift.

---

## 3. The two blocking contradictions in revision 1

### ▲ Contradiction 1 — the pilot could not seal

`seal_v2(conn, graph: OperatorGraphV2, ...)` — the graph is the **second positional argument**
(`seal_v2.py:106-108`). Revision 1 put the graph builder in Phase 5 and asked Phase 3's pilot to
reach sealed code. That is impossible without hand-constructing a graph, which would bypass the
architecture the pilot exists to prove.

**Fix:** the minimum deterministic graph builder moves **before** the pilot, covering exactly:

```
governed_scan → pit_availability_filter → aggregate{sum,count} → spine_left_join
              → group_assembly → final_combine{identity}
```

Later phases *expand* graph coverage rather than introducing the graph for the first time.

### ▲ Contradiction 2 — Phase 0 could not produce READY

`advertised_operators()` selects `WHERE renderer_dispatchable AND execution_proof_hash IS NOT NULL`.
Revision 1 proposed writing only dispatchability in Phase 0, deferring proofs to Phase 6, and then
claimed a formula would reach READY. Both cannot hold.

This was self-contradictory on its face — the intersection was quoted earlier in the same session
and then ignored. The temptation it creates is the dangerous part: the shortest path to a green
pilot is to write a proof record for a proof nobody ran, which is precisely the lie the advertised
set exists to prevent.

**Fix — three separate states, because they are three separate claims:**

| State | Means | Gates |
|---|---|---|
| **Formula admitted** | structurally valid, and the renderer supports every operation it needs | **code generation** |
| **Execution qualified** | this implementation passed its developer gold-data proof | visible; enforced per publication policy |
| **Artifact verified** | *this* generated artifact passed on-demand verification | **publication** |

Code generation requires only the first. Publication requires a current artifact verification.
Execution qualification stays visible and enforceable but does not block a user from *seeing* code —
which is the product decision already made.

**No manufactured proof records, ever.**

---

## 4. Missing contracts revision 1 did not define

### ▲ 4.1 The generation orchestrator — "Prepare selected features"

Revision 1 described components and no production operation joining them, which would have made the
pilot a hand-assembled demonstration rather than a feature.

```
select candidates → immutable BuildSet → queued generation request
  → load admitted formulas → bind physical inputs → resolve policies → compile IR
  → leakage + authorization → derive group + operator graph → render → seal
  → code and blockers in the UI
```

Must define, before implementation:

- a durable generation request with status
- an idempotency key (the draft lane's formula-identity pattern is the precedent — a double-click
  must not buy a second run)
- the binding from selected candidate to **formula revision**
- retry behaviour
- **partial group failure** behaviour — one refused feature in a group of five
- where every refusal is stored
- which artifact the UI shows

Note `BuildSetRevisionV1` is a dataclass with no store, and no build-set migration exists. That is
part of this work, not a prerequisite someone else did.

### ▲ 4.2 Executable policy payloads

A realization currently gives a content hash and provenance. `eligible_status_policy_hash = abc123`
does not let a renderer emit `WHERE transaction_status IN ('POSTED','SETTLED')`. A hash names a
decision; it is not the decision's content.

A versioned executable-policy payload store is needed, covering at least: eligible status values;
debit/credit direction mapping; reversal linkage and survivor rule; currency fields and rate
relation; FX quote convention; missing-rate behaviour.

**Every declared policy resolves to executable content or causes a named refusal. No silent
defaults** — a defaulted policy is a wrong number wearing a governed costume.

### 4.3 What is built and merely unwired

Needs calling, not building — each has zero production callers: `resolve_output_v2`,
`authorize_compilation_v2` (Gate 2 for V2), `leakage_v2` (V1 has *no* compile-time leakage gate at
all), `seal_v2`, `record_group_plan`, `record_bound_formula`, `record_inventory_observation`.

Does not exist: `compile_ir_v2` (nothing in production constructs a `FormulaExecutionIRV2` — every
V2 test wraps an expression compiled by the **V1** compiler), the graph builder, any writer of
`engine_operator_capability`.

---

## 5. Scope, stated honestly

**▲ Revision 1's headline was too broad.** "Not a rewrite, mostly unblocking" is true of **the narrow
pilot only**. Full V2 execution needs genuinely new calculation semantics: policies, row selections,
window offsets, second operands, signed expressions, FX, and seventeen unsupported aggregates.

What remains true: three of the five node renderers never touch the formula IR; `FormulaExecutionIRV2`
is V1's ten fields under the same names plus two, with zero renames. The *project and wiring* layers
are reusable as they stand. The *calculation* layer is where the new work is.

```
V2 aggregate functions:  21
renderer can emit:        4    sum, count_rows, count_non_null, count_distinct
```

`avg` is not renderable. "Average balance over 90 days" cannot render today — which is why the pilot
is sum/count only.

---

## 6. Sequence

Adopted from the review, with the verified detail attached.

| # | Step | Notes |
|---|---|---|
| **0** | **Extract the shared schema leaves** — split `formula/schema.py` into shared leaves + `schema_v1` | ▲ **new, and first.** 24 names V2/V3 import verbatim currently live in a module named for V1. Mechanical, low-risk, independent — and until it lands, nothing in step 15 can be deleted safely. Doing it first turns the last step from "work out what breaks" into "delete `schema_v1`". |
| 1 | Typed capability signatures + build fingerprints | migration 1091; supersedes 1079's shape. Changes visible readiness for all 263 recipes — deliberate, not incidental. |
| 2 | Separate admitted / execution-qualified / artifact-verified | resolves §3's contradiction 2 |
| 3 | BuildSet + generation request + worker | §4.1; the product operation |
| 4 | V2 physical binding + policy resolution | §4.2 payload store; **plus the two missing resolvers** (§8.3) |
| 5 | Compile → `PlannedFormulaExecutionIRV2` | the missing `compile_ir_v2` |
| 6 | Leakage + authorization → authorized IR | calls the built-but-unwired `leakage_v2`, `authorize_compilation_v2` |
| 7 | Minimum deterministic operator graph | **before** the pilot; adds `FINAL_COMBINE` |
| 8 | Remove all ten renderer gates atomically; normalize V2 dispatch | §2 — plus the four join-step `isinstance` sites (§8.8) |
| 9 | **Narrow pilot** — no policy, identity + sum/count, to sealed code | the first end-to-end proof |
| 10 | Semantic pilot — status, direction, reversal policies | first real policy payloads |
| 11 | Common aggregates — avg, min, max | unblocks ordinary features |
| 12 | Complex V2 ops, FX, remaining aggregates | FX ownership settled in §9 |
| 13 | On-demand verification worker | lifecycle REQUESTED→CLAIMED→RUNNING→PASSED/FAILED/REFUSED; execution identity must include the sealed artifact id |
| 14 | Publication requires a current passing verification | plus the reconciler; then the endpoint can expose the active revision, which lets the UI say "published" again |
| 15 | Delete `schema_v1` and the V1 product language; regenerate goldens | one deliberate commit. Cheap **because** step 0 happened. Verify/drain shadow work items first (§8.6). |

**Why 9 is still where it is:** every step before it is a contract or a gate, and the pilot is the
first thing that can be *wrong in an interesting way*. If the seam analysis is mistaken, it surfaces
there — on a slice — rather than after the seventeen-aggregate payload.

---

## 7. FX ownership — ruled

**The policy realization owns the rate relation. The graph carries only its resolved execution
binding.** The last open question is closed, and closed without creating a second source of truth.

```
Currency-conversion policy
  ↓ identifies the required conversion semantics
Policy realization        ← AUTHORITATIVE: rate relation, keys, time column, rate column,
  ↓                          quote convention, missing-rate behaviour
Physical binding          ← resolves the governed relation to THIS environment's dataset
  ↓
AsOfFxJoinV2              ← records exactly what THIS compilation will execute
  ↓
Gate 2                    ← authorizes that exact resolved read set
```

`AsOfFxJoinV2.rate_table_ref` must **never** be independently chosen by the graph builder or accepted
from an external caller.

### What changes

The payload today is four bare refs with no link to any realization — the two-sources-of-truth shape
this ruling removes:

```python
# now (operator_graph_v2.py:210-221)          # ruled
currency_conversion_ref: str                  currency_conversion_ref: str
rate_table_ref: str          ← chosen freely  policy_realization_revision_id: str
as_of_ref: str                                executable_content_hash: str
rate_column_ref: str                          bound_rate_dataset_ref: str
                                              binding_snapshot_id: str
                                              as_of_column_ref: str
                                              rate_column_ref: str
                                              rate_key_refs: tuple[str, ...]
```

The apparent duplication is acceptable **only** as a derived snapshot: the realization is the
decision, the binding is the environment's answer, and the graph is the frozen record of what this
compilation will run.

### The builder refuses if

Any of these means the snapshot has stopped agreeing with its source, and a snapshot that disagrees
with its source is worse than no snapshot:

1. the policy payload's rate relation cannot be bound;
2. the bound dataset differs from the graph value;
3. the rate columns lie outside that dataset;
4. the rate dataset is missing from the authorized read set;
5. the realization or binding changed after compilation.

Each refuses **by name** — never a silent default, and never a re-derivation that quietly picks a
different table.

### Two notes for whoever implements it

* **Graph identity moves.** `AsOfFxJoinV2.identity_payload()` (`:229-232`) feeds the content-addressed
  graph hash, so this changes it. That is safe here and only here: the graph is deliberately **not
  persisted** — `seal_v2` stores the verdict, not the graph — so no stored hash is invalidated. Under
  Decision 1 the graph is a derived view, and a derived view's identity is allowed to move with its
  derivation.
* **`rate_key_refs` is new** and `as_of_ref` is renamed `as_of_column_ref`. Neither exists today, so
  the producer must supply them from the realization rather than infer them — which is the entire
  point of the ruling.

---

## 8. Deletion inventory, and the corrections it forces upstream

The V1-removal analysis finished after revision 2 was written. Most of it is step 15 detail, but
four findings change **earlier** steps and one contradicts the original brief.

### 8.1 ▲ `formula/schema.py` is not a V1 module — and this reorders step 15

It is the **shared structural-leaf library** that V2 and V3 import verbatim: `FilterNode`,
`NullInput`, `EmptyWindowResult`, windows, grains, parameters, decimal policy — 24 names. Deleting
it breaks V2, not V1.

**Consequence:** nothing can be safely deleted until those 24 names have a home that is not a V1
module. So step 15 gains a prerequisite that is worth doing early and independently:

> **Split `formula/schema.py` into shared leaves + `schema_v1`.**

This is low-risk, mechanical, and it converts step 15 from "work out what breaks" into "delete
`schema_v1`". Same reason the file already says the leaves "carry no versioned vocabulary" — the
module name simply lies about its contents.

### 8.2 ▲ The renderer goldens are the execution proof — regenerate, never delete

The original brief lists "testing that V1 output remains unchanged" as removable work. Mostly true,
with one exception that would be expensive to get wrong: the **renderer goldens** look like V1
output-stability tests but they are the only thing pinning emitted Spark against reviewed expected
output. They must be **regenerated against V2 output**, not deleted.

Genuinely deletable: four explicit V1 byte/source-freeze assertions — two of which `sha256` the
**source text** of V1 functions — plus roughly **187 test functions across ten files and an
11-fixture gold corpus, about 3,435 lines.**

### 8.3 ▲ The V2 restorer does not exist — a missing link steps 4–5 assume

`materialize/resolve.py` is privately bound to the V1 restorer, and there is no V2 equivalent. This
is the concrete gap between an admitted V2 formula and a compilable one. Estimated ~200 lines and
described as the smallest high-leverage item in the whole map — it belongs in step 4, named, rather
than being discovered inside step 5.

**▲ Name it `restore_formula_v3.py`, not `resolve_v2.py`.** `resolve_output_v2` already exists
(`formula/output_authority_v2.py:77`), and a `resolve_v2` beside it invites the reading that one is
the general case of the other. They are different verbs:

* **resolve_\*** — *decide* a value that was undetermined (an output policy, a physical type).
* **restore_\*** — *rehydrate* a stored artifact into the object a compiler can use.

Keeping the two verbs distinct is worth more than matching the existing suffix.

Related and equally unnamed: `physical_types_v2.py` is **not** the V2 replacement for
`physical_types.py`; the V2 feature→type resolver does not exist. `PlannedFeature` hard-requires a
resolved physical type, so step 4 cannot complete without it.

### 8.4 ▲ `compile/wiring.py` reads `empty_window` / `null_input` off the admitted **V1** formula

Because `PitSpec` deliberately excludes them. Easy to miss, and it means the V2 compile path needs
its own carrier for those two values before step 8's renderer work can pass them. Neither IR carries
them today.

### 8.5 Cross-effect: step 1 touches user-visible recipe readiness

The engine's advertised capability — derived from the V1 renderer's four aggregates — drives the
readiness answer shown for **all 263 recipes**. Changing the capability model in step 1 changes that
display. Not a blocker, but it must be deliberate: a capability refactor that silently re-labels 263
recipes is a product change wearing an infrastructure commit message.

### 8.6 Two things that look deletable and are not

- **The recipe/shadow lane is DUAL**, chosen per work item from a declaration whose *absence* means
  v1. The v1 arm cannot be retired by flipping a default — existing rows select it by saying nothing.
- **The v1 egress byte-freeze exists because durable work-item rows were sealed against those exact
  bytes**, and every dispatch re-validates against them. "No V1 data to preserve" is true of
  *product* data; it is not true of the shadow lane's sealed work items. **Verify before deleting.**

### 8.7 What is already V2 and already dead

The S11 generate/code/verify/publish surface is V2 throughout — and unreachable, because `seal_v2`
has no production caller. A whole V2 execution sub-chain is built, unit-tested, and has zero
production callers. That is not V1 debt to remove; it is finished work waiting to be connected,
which is the entire point of steps 3–9.

### 8.8 One additional gate class, beyond the ten

The join machinery dispatches by `isinstance` on the **V1 step classes**
(`CrossCatalogJoinStepV1`, `CrosswalkJoinStepV1`) at four sites in `nodes_compute.py`. A V2 graph
either reuses those step classes or needs an adapter producing them. This is separate from §2's ten
enum/type gates and was not counted there.

Corroborating the other direction: `OperatorGraphV2`'s `PitAvailabilityFilterV2` carries `PitSpec`
**verbatim** — the V2 vocabulary already reuses the exact V1 execution type. The PIT renderer needs
no change for the two supported window bases.

---

## 9. Found during step 4: the declared grain never reaches the model

**`FeatureIdea.grain_ref` is computed and then discarded**, and nothing downstream can tell.

The generator sets it (`feature_assist.py:2614`, from the resolved grain operand). The considered
revision does not serialise it — `_idea_json` emits fifteen keys and `grain_ref` is not among them —
so `_chosen_option_from_revision` always returns an idea whose `grain_ref` is `None`. Verified on the
live cluster: **zero** stored options carry it.

**What that costs.** The draft worker builds its authoring intent as:

```python
target_grain_keys = (tuple(sorted(column_refs)) if idea.grain_ref is None
                     else (logical_ref_of(...grain_ref...),))
```

The `else` branch is dead in production. Every formula is therefore authored with grain keys listing
**every column the feature derives from**, rather than the one column it is computed per. A feature
meant to be "per customer" is described to the model as grained on its amount column, its date
column and its customer column together.

The intent hash covers `target_grain_keys`, so this is consistent — the restorer re-derives the same
wrong value and the checkpoint agrees. Consistency is why nothing has noticed.

**Why it is not fixed here.** `_idea_json` feeds `_candidate_identity`, which is the canonical
candidate identity hash. Adding a field changes that hash, which changes every stored option
identity and every draft's `planning_request_hash`. That is a migration-shaped change with identity
consequences, and doing it inside a step about restoring formulas would bury it.

**Where it belongs:** step 4's physical-binding work, as an explicit item — the grain is exactly what
binding needs to be correct about. Until then, formulas are authored against a grain nobody
declared, which is a correctness question rather than a cosmetic one.

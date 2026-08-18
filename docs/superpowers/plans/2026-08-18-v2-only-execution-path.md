# V2-only: from a READY formula to a published feature

**Decision (given):** one formula language. V2 semantics, wire format `formula_schema_version = 3`.
No V1/V2 router, no compatibility mode, no V1 migration, no byte-equivalence guarantee, no
user-visible version choice. The product is not live. V1's *execution* machinery is reused; V1's
*language* is not.

**Revision 2**, after review. Two rulings adopted, two blocking contradictions fixed, three missing
contracts added. Changes from revision 1 are marked ▲ and the reasons kept, because the corrections
are the useful part.

**Verification standard:** every number here was produced by running the shipped code, not counted
by eye. Revision 1 broke that standard once and the record is kept in §2.

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
| 1 | Typed capability signatures + build fingerprints | migration 1091; supersedes 1079's shape |
| 2 | Separate admitted / execution-qualified / artifact-verified | unblocks §3's contradiction 2 |
| 3 | BuildSet + generation request + worker | §4.1; the product operation |
| 4 | V2 physical binding + policy resolution | §4.2 payload store lands here |
| 5 | Compile → `PlannedFormulaExecutionIRV2` | the missing `compile_ir_v2` |
| 6 | Leakage + authorization → authorized IR | calls the built-but-unwired `leakage_v2`, `authorize_compilation_v2` |
| 7 | Minimum deterministic operator graph | **before** the pilot; adds `FINAL_COMBINE` |
| 8 | Remove all ten renderer gates atomically; normalize V2 dispatch | §2 |
| 9 | **Narrow pilot** — no policy, identity + sum/count, to sealed code | the first end-to-end proof |
| 10 | Semantic pilot — status, direction, reversal policies | first real policy payloads |
| 11 | Common aggregates — avg, min, max | unblocks ordinary features |
| 12 | Complex V2 ops, FX, remaining aggregates | driven by product demand, not by completing an enum |
| 13 | On-demand verification worker | lifecycle REQUESTED→CLAIMED→RUNNING→PASSED/FAILED/REFUSED; execution identity must include the sealed artifact id |
| 14 | Publication requires a current passing verification | plus the reconciler; then the endpoint can expose the active revision, which is what lets the UI say "published" again |
| 15 | Remove V1 product-language code; rename reusable execution machinery | one deliberate commit |

**Why 9 is still where it is:** every step before it is a contract or a gate, and the pilot is the
first thing that can be *wrong in an interesting way*. If the seam analysis is mistaken, it surfaces
there — on a slice — rather than after the seventeen-aggregate payload.

---

## 7. Open — and now genuinely open

Only one remains, and it is a spec question rather than a decision:

**The as-of FX join carries `rate_table_ref` in its own payload, and the policy realization also
names a rate relation.** Which owns it? Two places naming the same table is how they come to
disagree. This blocks step 12, not before.

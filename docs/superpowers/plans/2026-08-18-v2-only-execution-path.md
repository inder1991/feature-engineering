# V2-only: from a READY formula to a published feature

**Decision (given, not re-litigated):** one formula language. V2 semantics, wire format
`formula_schema_version = 3`. No V1/V2 router, no compatibility mode, no V1 migration, no
byte-equivalence guarantee, no user-visible version choice. The product is not live, so there is no
V1 data to preserve. V1's *execution* machinery is reused; V1's *language* is not.

**Status of this document:** every claim below was verified against the tree at `bed4bed0` by
reading the code, not from memory. Where a number appears (4 of 21, 5 of 13) it was computed by
running the shipped function, not counted by eye. Two questions are left open deliberately, at the
bottom — they are decisions, not unknowns.

---

## 0. The finding that shapes everything

**This is not a rewrite. The renderer is already language-neutral almost everywhere.**

Counted by field, not by type name:

| Renderer | Touches the formula IR? |
|---|---|
| `render_spine_node` | **never** |
| `render_assembly_node` | **never** |
| `render_gate_node` | **never** |
| `render_projection_node` | 4 of `ExpressionExecutionIR`'s 9 fields — read set, join topology, join authority, `PitSpec`. All execution- or governance-shaped. |
| `render_calculation_node` | 5 of `FormulaExecutionIRV1`'s 10 fields. |

And the two IRs are nearly the same object: `FormulaExecutionIRV2` is V1's ten fields **under the
same names in the same order**, plus two (`row_selections`, `policies`), with two widened enums and
**zero renames or deletions** (`ir.py:166-175` vs `boundary_v2.py:256-267`).

So a native V2 renderer is not needed. What blocks V2 today is not incompatibility — it is a
handful of **type-name gates** and a genuinely narrow **compute vocabulary**.

### The gates (verified by hand, and corrected once)

```
nodes_compute.py:2223   if not isinstance(ir, FormulaExecutionIRV1):     # rejects V2 on type NAME
```

That one refuses **loudly** and is therefore safe. The dangerous ones are the enum identity
comparisons, and there are more of them than a first pass suggested:

```
17   ` is FinalOperation.X ` / ` is AggregateFunction.X `   across render/
 2   dict lookups keyed on the enum (_BODY_SLOTS, _AGGREGATE_CALLS) — LOUD KeyError, safe
```

**Why the seventeen are dangerous, precisely.** `FinalOperationV2.RATIO is FinalOperation.RATIO` is
`False` — different enum object, same name, same value. So a V2 ratio does not *fail*; it takes the
else-branch and renders **something else**. The same holds for `AggregateFunctionV2.SUM` against
`AggregateFunction.SUM`. Some of those fall through into a dict lookup and die loudly a few lines
later; others sit inside boolean expressions and simply change the emitted code.

*(An earlier draft of this plan said five, counting only `FinalOperation` in one file. It is
seventeen, across two enums. The recount is recorded rather than quietly fixed because the number is
the argument: five is a careful edit, seventeen is a sweep with a test behind it.)*

**Rule for the whole plan: the type-name gate at 2223 and all seventeen identity comparisons move in
the same commit, or none of them move.** Removing 2223 alone converts a loud refusal into a wrong
number, which on this platform means a wrong feature in a credit model.

### `ExpressionExecutionIR` is not language-neutral either

Both IRs share this type, which invites the assumption that it crosses the boundary intact. It does
not — three of its nine fields are V1-language-bound:

| Field | How it is bound | Fails how |
|---|---|---|
| `expr_path` | closed to V1's five body paths, `raise` at `expression_ir.py:875` | loudly |
| `aggregation` | typed `AggregateFunction` — 4 members against V2's 21 | see above |
| `pit: PitSpec` | no `offset_periods`; `window_basis` a bare `str` the renderer hard-refuses outside `{trailing, calendar_period}` | loudly |

`BODY_PATHS` is exactly `body.expr`, `body.numerator`, `body.denominator`, `body.minuend`,
`body.subtrahend` — the identity, ratio and difference slots. **There is no path for `SIGNED_SUM`'s
N terms**, which is why it appears in Phase 4 rather than Phase 2: it needs a vocabulary extension,
not a gate removal.

### The real gap: compute vocabulary, one level down

The boundary is not between the IRs. It is inside `ExpressionExecutionIR` — which both IRs share —
and inside the renderer's node templates.

```
V2 aggregate functions:        21
renderer can actually emit:     4    sum, count_rows, count_non_null, count_distinct
NOT renderable:                17    avg, min, max, median, percentile, stddev, zscore, slope,
                                     recency, hhi, top_share, streak_periods, any_match,
                                     first_known, last_known, effective_at_cutoff, date_diff_avg
```

**`avg` is not renderable.** "Average balance over 90 days" — the most ordinary feature this product
exists to build — cannot render today. That single fact decides the pilot's shape (§3).

Also absent from the renderer: `SIGNED_SUM` (V2's fourth final operation, no `_BODY_SLOTS` entry and
no composite body-path spelling anywhere), row-level arithmetic (`_operand_column` requires exactly
one column ref), `offset_periods` (never reaches `materialize/` at all), `row_selections` and
`policies` (on the V2 IR, no renderer code path), and V2's as-of FX join (no representation in the
V1 join machinery).

### What is built and merely unwired

These need **calling**, not building — each has zero production callers:

- `resolve_output_v2` (S5) — pure, V3-accepting, directly reusable at compile time
- `authorize_compilation_v2` (Gate 2 for V2) — complete, delegates to the shipped V1 verdict
- `leakage_v2` — and note V1 has **no** compile-time leakage gate at all, so this is the only one
- `admit_artifacts_v2` — wired this week, but see §1
- `seal_v2`, `record_group_plan`, `record_bound_formula`, `record_inventory_observation`

### What does not exist at all

- **`compile_ir_v2`.** Nothing in production constructs a `FormulaExecutionIRV2`. Every V2 test
  builds one by wrapping an expression compiled by the **V1** compiler. This is the missing link.
- **An operator-graph builder.** Three production functions *consume* `OperatorGraphV2`; none
  produces one.
- **Any writer of `engine_operator_capability`.** So `advertised_operators()` returns `()` and V2
  admission refuses every formula (§1).

---

## 1. Correction to the stated starting point

The brief says the formula-draft branch already reaches `Candidate → V2/V3 formula → admission →
READY`.

It reaches admission. **READY is unreachable in production.** Nothing writes
`engine_operator_capability`, `advertised_operators()` returns empty, and admission refuses
everything. Verified live on the cluster today: a real draft's honest ceiling is
BLOCKED-on-capability.

This does not change the architecture. It adds one cheap item to the front of the critical path.

---

## Phase 0 — Open the door

**Why first:** independent of everything else, small, and until it is done every later phase is
tested against a system that refuses all input.

The renderer already describes its own abilities — `renderable_aggregations()` at
`nodes_compute.py:293` is the renderer telling you what it can emit, and `engine_capability.py:25-29`
already derives an advertisement from it. Extend that same derivation to per-operator dispatch and
call `record_renderer_dispatch` at startup.

**Do not touch the proof half.** Renderer-dispatch is a fact about this build and is derivable.
Execution-proof is an assertion that something ran against reviewed gold and was correct. Writing
the second without a harness is the one lie the advertised set exists to prevent. Until Phase 6,
admission is honestly capability-limited and drafts say so by name.

**Done when:** a drafted formula using only renderable aggregates reaches READY on the cluster.

---

## Phase 1 — `compile_ir_v2`

The missing link: a V3 proposal → `FormulaExecutionIRV2`.

V1's `compile_expression` is reusable except one language-bound field (identified in the map; it
would silently produce a wrong value under V2 rather than refuse — treat it with the same rule as
§0's identity comparisons).

**Order inside the phase:** call the already-built, unwired pieces rather than writing new ones —
`resolve_output_v2` for the output policy, `authorize_compilation_v2` for Gate 2, `leakage_v2` for
the leakage check.

**Done when:** a READY draft becomes a `FormulaExecutionIRV2` in production code, with the output
policy resolved and Gate 2 passed.

---

## Phase 2 — Unblock the renderer

Replace the type-name gate at 2223 with a structural check, and **in the same commit** fix all
seventeen identity comparisons across `render/` — both `FinalOperation` and `AggregateFunction` — so
a V2 enum takes the right branch. Then the three token gates and eleven plan/contract gates.

Widen `ExpressionExecutionIR.aggregation` to the V2 vocabulary at the same time, or accept that
every V2 aggregate dies at `_AGGREGATE_CALLS[...]` with a `KeyError` rather than a named refusal.
The second is survivable for the pilot (§3 uses only renderable aggregates) but must not survive
Phase 4.

**Test that earns its place:** a V2 ratio must render *as a ratio*, and a V2 `sum` as a sum. Assert
on the **emitted body**, not on the absence of an exception — the failure mode here is a wrong
answer, not a crash, and a test that only checks "it did not raise" passes against the bug.

A cheap structural guard is worth adding beside it: no ` is ` comparison against a V1 enum member
anywhere under `render/`. That is the defect class, stated once, in a form that cannot drift.

**Done when:** an IDENTITY-body V2 IR over `sum` renders byte-plausible Kedro/Spark through the
existing renderer.

---

## Phase 3 — The narrow pilot

**The sequencing decision that de-risks the rest.**

Pick one candidate whose formula uses **only** `sum` / `count_*` and an **IDENTITY** body, and drive
it the whole way:

```
candidate → V3 formula → admission → compile_ir_v2 → render → seal → code visible in the UI
```

This proves the chain end-to-end **before** any of the large compute work in Phase 4. If the seam
analysis is wrong somewhere, it surfaces here, on a narrow slice, rather than after seventeen
aggregate implementations.

**Explicitly excluded from the pilot:** `avg` (not renderable), ratios, composite bodies, FX, row
selections. Not because they do not matter — because they are Phase 4, and the pilot's job is to
prove the pipe, not the payload.

**Done when:** generated code for a real candidate is on screen, sealed, from a V2 formula.

---

## Phase 4 — Widen the compute vocabulary

Only now, and **driven by real candidates rather than by completing an enum**. Each item ships
independently and each should be justified by a candidate that needs it:

1. `avg`, `min`, `max` — trivial, unblock the most ordinary features
2. `median`, `percentile`, `stddev` — need `aggregation_argument`, which `ExpressionExecutionIR` has
   no slot for
3. `SIGNED_SUM` composite bodies — needs a body-path spelling that does not exist yet
4. row-level arithmetic — `_operand_column` currently requires exactly one column ref
5. `offset_periods` (lag/delta) — never reaches `materialize/` at all
6. `row_selections` / `policies` — no renderer path; needs the semantic→physical resolution that is
   deliberately absent by design today
7. as-of FX join — no representation in the V1 join machinery, and an open spec question (§Open)

**Refuse by name.** Anything not yet renderable must produce a named blocker, never a wrong number.
That is already the pattern the draft lane uses.

---

## Phase 5 — Operator graph builder

**Deliberately not first.** It is the third link in a chain whose first two are missing, and part of
it cannot be written yet at all.

Before writing it, settle the open question in §Open about the two disjoint V2 shapes.

Note: the renderer can honestly emit **5 of the 13** operator kinds. The builder must refuse the
other eight by name until Phase 4 catches up. `derive_policy_occurrences` is the closest structural
template; `implied_operator_kinds` is the only production function deriving operator vocabulary.

---

## Phase 6 — Verification, and the proof harness

Currently no worker consumes `verification_attempt`; there is no runner and no settlement. Build:

- a dedicated worker consuming **only explicitly requested** attempts (never a timer — the S11 test
  asserts this and it is correct)
- a lifecycle: REQUESTED → CLAIMED → RUNNING → PASSED / FAILED / REFUSED, so "pending" is
  distinguishable from "the worker crashed" and from "nothing consumes this table"
- the execution identity must include the sealed artifact id (it does not today)

This phase also produces the **execution proofs** Phase 0 deliberately left alone: generate a gold
project, execute it, compare against reviewed expected output, apply each mutation, confirm each is
detected. Only then does `record_execution_proof` have an honest caller.

---

## Phase 7 — Publication

A worker that performs the swap, reads the observed active revision **inside** the publication
transaction, and settles or reconciles the attempt. Plus a reconciler — today an unresolved STARTED
attempt blocks retries permanently.

Then the endpoint can expose the **active revision**, which is what lets the UI honestly say
"published" again. It cannot today, which is why that stage was removed from the screen.

---

## Phase 8 — Delete V1

One deliberate commit, after the pilot passes and the vocabulary is wide enough for real use — not
incremental erosion. Scaffolding is sticky; a V1 path that still runs is a path someone keeps using.
The moment V1 stops being reachable should be visible in history and testable.

*(The precise deletion inventory is still being verified separately and will be appended. It matters
less than the others because nothing depends on it — it is cleanup, and doing it early is the only
way to get it wrong.)*

---

## Open questions — decisions, not unknowns

**1. Two disjoint V2 shapes, and nothing reconciles them.**
`FormulaExecutionIRV2` and `OperatorGraphV2` both describe "the executable form of a V2 formula",
and no code converts between them. Before Phase 5, decide which is the compile target:

- *IR as the target, graph as a derived view* — smallest change, reuses the renderer directly, and
  the graph becomes an analysis/capability artifact.
- *Graph as the target, IR retired* — cleaner conceptually, but the renderer consumes IR, so this
  means a rendering layer over the graph and pushes Phase 3 out considerably.

The evidence favours the first: the renderer already reads five IR attributes that both IRs carry.
But this is an architecture decision and should be made explicitly rather than by whichever phase
gets written first.

**2. Capability granularity disagreement.**
Two capability systems exist at different grains: per-*aggregate* (`engine_capability.py`) and
per-*operator-kind* (`engine_operator_capability`). `OperatorKindV2.AGGREGATE` is a single kind, so
a kind-level check cannot express "this engine can sum but cannot compute a median" — which is
exactly the true state today. Decide whether the capability row carries the aggregate function, or
whether `AGGREGATE` splits into finer kinds.

This is on the critical path for Phase 0: what gets written depends on the answer.

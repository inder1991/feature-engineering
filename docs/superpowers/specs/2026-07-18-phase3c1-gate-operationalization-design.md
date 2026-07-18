# Phase 3C.1 — Gate operationalization (run the machine checks, show results, admin decides)

> **Status:** Design, ready for implementation planning. This spec covers **3C.1 only**. The live flip
> (surface governed cross-catalog plans to users; retire `find_cross_catalog_path`) is **3C.2** — a
> separate spec, review, and release. 3C.1 changes **nothing** a feature engineer sees.

**Goal:** Give an operator a way to run 3B.4's trustworthiness checks over a real batch of shadow
feature-generation runs and read a PASS/FAIL plus an honest population view on an internal screen — so
a human can decide whether to fix the classifier or move toward go-live. No signing, no formal
multi-reviewer labeling: **results only, admin decides.**

**Architecture:** A read-only, authority-only evaluation path that (a) selects an immutable batch of
persisted shadow runs by producing-cohort + flag-provenance, (b) runs the machine-checkable sub-gates
from 3B.4 plus the controlled gold set, (c) builds the honest §9 population report over the batch, and
(d) returns a machine verdict + population view to an internal admin console. Everything is computed
server-side from the write-once telemetry store; the browser only triggers and displays.

**Tech stack:** Python/FastAPI backend (the existing planner package + a new authority-only route),
Postgres WORM telemetry store (one additive migration), a single internal React admin screen behind a
Vite flag. Reuses the 3B.4 machinery (`shadow_report`, `contract_gold`, `contract_eval`, `replay`,
`shadow_store`).

---

## 1. Where this picks up, and why it splits from 3C.2

3B (through 3B.3c) built the deterministic cross-catalog **classifier** that rules each candidate feature
"complete/buildable" or "honestly unresolved, here's why." It runs today only in **shadow**: on the
entity-scoped considered-set branch (`contract.py::_scoped_considered_set`) the planner computes plans
and the classifier rules on them, and — with the telemetry flag on — those rulings persist (one row per
`(run, recipe)`), but they never reach the user. The **live** cross-catalog path a feature engineer
hits is still the permissive `entity.find_cross_catalog_path`.

3B.4 built the *logic* to judge whether those rulings are trustworthy: the §9 population report, a
conjunctive gate, Clopper-Pearson, double-compile, drift comparison, a versioned gold set. But **none
of it is wired** — nothing runs it end-to-end, and the manifest doesn't record enough to make the
evidence honest.

**Why 3C splits.** 3B.4 made a passing gate a hard precondition for going live. Building the live-flip
machinery in the same delivery would invert the order — build enforcement, *then* decide whether the
enforcement should exist — and would make a FAIL an inconvenience blocking already-written activation
code. The correct order is: collect real shadow evidence → evaluate it → decide. So **3C.1 produces the
evidence and the human-readable verdict; 3C.2 (separate) consumes a decision and changes behavior.** A
FAIL from 3C.1 is a legitimate, expected outcome.

## 2. The scope decision (lean) — what's in, what's deliberately out

The evidence stream this phase produces feeds a **human decision** (fix the classifier, or proceed
toward go-live). Given that, two heavy pieces from the original 3B.4 gate design are **out of 3C.1**:

- **No signing / certificate / keys.** The signed artifact only ever mattered as a machine interlock for
  the *later* go-live (so it couldn't run on a faked/stale PASS). With go-live being a human decision in
  this model, the signature adds nothing to "should we fix the code." Dropped. (If model-risk/audit later
  demands an independent, tamper-evident, version-bound artifact, it can be layered on — nothing here
  blocks it. This is the one deferred requirement to revisit in 3C.2's own governance review.)
- **No formal multi-reviewer label store or adjudication.** In a results-only model **the admin is the
  reviewer**: they read the population view (resolved-vs-rejected breakdown, reasons, the specific feature
  shapes, and the errored/missing/excluded runs) and judge. The append-only Layer-B label store, the
  named-reviewer workflow, and the disagreement-adjudication path are not built.

**The honest consequence, stated plainly.** Dropping the formal labeled sample means 3C.1 delivers a
**machine PASS that is necessary but not sufficient** for full trustworthiness: it proves the classifier
is self-consistent, gets the controlled known-answer cases right, taxonomizes every reason, detects
drift, and ran over complete telemetry — but it does **not** produce a formal statistical guarantee that
the classifier's rulings on the *messy real population* are correct. That judgment is supplied by the
admin reading the population view. The spec and the UI must state this so a machine PASS is never
mistaken for a complete guarantee.

**What stays in 3C.1:**
1. **Flag + cohort provenance** on each shadow run (the honesty foundation).
2. **Immutable batch selection** by cohort + flag-provenance + time range.
3. **The machine-checkable sub-gates** wired to run: capture integrity, reason-map exhaustiveness,
   gold-set false-resolve, double-compile stability, drift detection.
4. **The honest §9 population view** over the batch.
5. **An authority-only evaluation entrypoint** and **one internal admin console** to trigger + read it.

## 3. The provenance consequence (read this before planning a window)

Requirement: the evidence must prove each included run was produced under the conditions the live path
will run under — `SCOPED_APPLICABILITY`, `RANKING`, `CONTRACT_COMPILE`, `SHADOW_TELEMETRY` all active —
and by a known code version. **Today's manifest records only `compile_flag` + `telemetry_flag`, and
`producer_commit` is the literal placeholder `"dev"`.** The store is write-once, so **this cannot be
backfilled onto already-persisted runs.**

Therefore: 3C.1 extends capture going forward, and **the first real evaluation window must be collected
*after* 3C.1 ships** — in a staging/pre-prod environment run with all four flags on and the (future)
live-grounding flag off. Runs that predate the provenance (or can't prove all four flags) are
**excluded from every window and reported as excluded** (fail-closed). Delivering the harness and
running it to a real verdict are therefore distinct events; **3C.1's acceptance does not require a real
PASS**, only that the harness runs correctly and fails closed on incomplete telemetry.

## 4. What 3C.1 reuses vs. builds

| Capability | 3B.4 gives | 3C.1 adds |
|---|---|---|
| WORM telemetry store + readers + `reconcile` | ✅ | window-level coverage over many runs |
| §9 population report (`build_population_report`, multi-run) | ✅ (takes `run_ids`) | fed by the batch selector; view surfaced |
| Conjunctive gate logic (`evaluate_gate`) | ✅ (7 sub-gates) | a **machine-only** evaluator (5 sub-gates) |
| Gold set + `run_gold_case` + `evaluate` | ✅ | a **gold suite runner** over `GOLD_CASES` |
| Double-compile comparator (`double_compile_stable`) | ✅ | a **driver** that compiles a frozen fixture twice |
| Replay comparator (`replay.compare` / `ReplayFreshness`) | ✅ | a **drift-detection driver** over controlled mutations |
| Flag/commit provenance on each run | `compile`+`telemetry` only; `producer_commit="dev"` | +`scoped_applicability`+`ranking` flags, **real** commit |
| Trigger surface | none | authority-only endpoint + internal admin console |

## 5. Components (each isolated, with a clear interface)

### 5.1 Run provenance capture — `planner/shadow_capture.py`, `shadow_store.py`, `contract.py`, migration `1000`
- **Migration `1000_dispatch_flag_provenance.sql`:** add two **NULLABLE** columns to
  `planner_shadow_dispatch` — `scoped_applicability_flag boolean`, `ranking_flag boolean`. Nullable is
  deliberate: existing rows (and any run whose route didn't record them) carry `NULL` = *unprovable*,
  which the selector treats as fail-closed exclusion. New rows write actual booleans. WORM revokes
  unchanged (mirror 0971). No change to any other 0999 table.
- **`DispatchRecordV1`** gains `scoped_applicability_flag: bool | None`, `ranking_flag: bool | None`;
  `build_dispatch(...)` takes all four flag states; `write_dispatch`/readers carry them.
- **Real producer commit:** resolve from config (`FEATUREGEN_PRODUCER_COMMIT`, set at deploy). If unset,
  the value is a sentinel (`"unset"`) that the selector treats as an **uncertified cohort** → excluded.
  `PRODUCER_COMMIT = "dev"` placeholder is removed.
- **Route (`_scoped_considered_set`):** already resolves `_intent_ranking_enabled()` and the scoped-
  applicability flag; thread both (plus the existing compile/telemetry) into `run_shadow_planner` →
  `build_dispatch`. The flags are still read **only in the route** (the planner stays pure). Behaviour-
  neutral: capturing more provenance changes no response and no existing telemetry semantics.

### 5.2 Immutable batch selection — new `planner/gate_operate.py`
- `select_window(conn, *, cohort: str, since: datetime, until: datetime) -> WindowSelection`.
- Queries `planner_shadow_dispatch` for runs where `producer_commit == cohort` AND all four flags are
  `TRUE` AND `created_at` in `[since, until)`. Returns the qualifying `run_ids` **plus a coverage
  report**: total dispatched in range, qualifying, and **excluded-with-reason** (wrong cohort, a flag
  `FALSE`, a flag `NULL`/unprovable, uncertified commit). Fail-closed: anything not provably qualifying
  is excluded and counted.
- Reproducible by construction: the same `(cohort, since, until)` over the write-once store returns the
  identical set and the identical coverage — no separate "registered window" artifact is needed for a
  results-only model.

### 5.3 The machine-check drivers — `planner/gate_operate.py`
Three controlled checks (independent of the window — they run seeded fixtures, not customer data).
**Isolation is mandatory:** each driver seeds a controlled catalog, compiles, reads its verdict, and
must run inside a transaction/savepoint that is **rolled back, never committed** — so the gold/drift
fixtures never persist into the real catalog. `/gate/evaluate` is read-only with respect to durable
state; the drivers' writes are transient and discarded.
- `run_gold_suite(conn) -> EvalReport` — runs every `GOLD_CASES` case through `run_gold_case` and
  `evaluate`; the false-resolve teeth live here (a bad feature the classifier calls "complete" fails).
- `run_double_compile(conn) -> StabilityResult` — compiles the frozen gold fixtures **twice** and feeds
  the two verdict lists to `double_compile_stable` (identity-comparable only; empty ⇒ unstable).
- `run_drift_checks(conn) -> DriftResult` — applies each controlled mutation class (column additivity /
  is_as_of / sensitivity, realization, bridge, projection, version) to a seeded catalog and asserts
  `replay.compare` reports `drifted`/`incompatible`/`unverifiable` (never `current`); the detected
  fraction must be 1.0.

### 5.4 The machine-only gate evaluator — `planner/shadow_report.py`
- `evaluate_machine_gate(report, gold_report, stability, drift_ratio) -> MachineGateResult` — the
  **conjunction of the five machine sub-gates**, no averaging:
  1. **Capture integrity** (Gate 1) — the qualifying window is **non-empty** (`denominator > 0` — an
     empty or all-excluded window FAILS, never vacuously passes: no evidence is not a pass); `reconcile`
     complete over the window; zero persistence loss / `persistence_partial` / incomplete /
     `compile_disabled` / `internal_error` / `preloop_failure` / `template_not_found` /
     bounding-truncation. (Reuses `_gate1`, plus the non-empty guard.)
  2. **Reason-map exhaustiveness** (Gate 2a) — `assert_map_exhaustive()` and zero
     `operationally_unmeasured` in the window population.
  3. **Gold false-resolve** (Gate 3, gold half only) — `gold_report.passed` (exact match + zero false
     resolves on the expert answer key).
  4. **Replay stability** (Gate 5) — `stability.stable`.
  5. **Drift detection** (Gate 6) — `drift_ratio >= 1.0`.
- Carries the population report for display. `MachineGateResult.passed = all(five)`. The dropped 3B.4
  sub-gates (2b human review, 3-audit, 4-statistical, 7-signature) are **not evaluated** and the result
  documents that a PASS is necessary-not-sufficient (§2).

### 5.5 Authority-only entrypoint — new `api/routes/gate.py`
- `POST /gate/evaluate` — platform-admin only (RBAC), **not** on the customer path. Body: `{cohort,
  since, until}` — an identifier of *which* batch, never any counts/verdicts. The handler assembles all
  inputs server-side from persisted stores (Requirement 3), runs `select_window` → the drivers →
  `build_population_report` → `evaluate_machine_gate`, and returns the machine verdict + the coverage
  report + the population view. Deterministic and read-only (no writes; stateless — reproducibility comes
  from the immutable inputs). A companion `GET /gate/cohorts` lists available cohorts + date ranges from
  the dispatch store to populate the screen.

### 5.6 Internal admin console — `GateEvaluationScreen.tsx` (Vite flag, platform-admin)
- One screen: pick a cohort + date range → **Evaluate** → render PASS/FAIL, the failed conditions, the
  coverage report (qualified/excluded-with-reason), and the population view (resolved-vs-rejected,
  reason breakdown, top shapes). Prominently states that a machine PASS is *necessary-not-sufficient* and
  the admin's read of the population is the remaining judgment. Triggers and displays only — it never
  supplies numbers and there is no "sign" affordance.

## 6. Data flow

```
operator (platform-admin) → POST /gate/evaluate {cohort, since, until}
   → select_window        (persisted dispatch store; fail-closed coverage)
   → run_gold_suite / run_double_compile / run_drift_checks   (controlled fixtures)
   → build_population_report(window.run_ids)                  (persisted WORM store)
   → evaluate_machine_gate(...)                               (5-gate conjunction)
   → { machine_verdict, failed_conditions, coverage, population_view }  → admin console
```

Nothing on this path touches the considered-set route or any customer response.

## 7. Requirements (the original seven, adapted to the lean model)

1. **Flag state from each persisted run** — KEPT. §5.1 captures all four flags + real commit per run;
   §5.2 proves them from the manifest, excluding anything unprovable (fail-closed).
2. **Honest denominator** — KEPT. The population report counts incomplete/errored/excluded/missing
   (`build_population_report` + `reconcile`), and the batch coverage report adds provenance-excluded
   runs. A gate cannot pass by silently dropping failures.
3. **Trusted inputs, not the request body** — KEPT. The body carries only a batch identifier; every
   count, verdict, gold result, and observation is assembled server-side from persisted stores.
4. **Auditable human labels** — **DROPPED** for 3C.1 (admin is the reviewer; results-only). Revisit if a
   formal labeled audit is later required.
5. **Version-bound signed artifact** — **DROPPED**; the population view *displays* the evaluated version
   set for the admin, but nothing is bound/persisted as an artifact.
6. **Independent signer / no in-app private key** — **DROPPED** (no signing).
7. **A PASS is not permanent** — **MOOT** (no artifact). Re-run the evaluation after any material
   classifier/planner/policy change; the result is inherently tied to the code version it ran under
   (shown in the view).

## 8. Acceptance criteria

3C.1 is complete when:
- one authority-only endpoint (surfaced by the admin console) runs the complete machine harness;
- the same immutable inputs `(cohort, since, until)` reproduce the same coverage, report, and verdict;
- incomplete/unprovable telemetry **fails closed** — excluded and reported, never silently included;
- the gold suite and the population report both run;
- the double-compile proves deterministic classifier output (empty comparison ⇒ FAIL);
- drift detection reports 100% on the controlled mutations;
- both PASS and FAIL machine verdicts are producible and render on the console with failed conditions;
- the console shows the honest population view and states the necessary-not-sufficient caveat;
- **considered-set API responses remain byte-identical** (behaviour-neutral; verified).

## 9. Invariants preserved

NO data plane (the report describes definitions, never values). Behaviour-neutral: the provenance
capture and the new route add zero customer-facing change; the migration is additive + nullable. WORM /
append-only telemetry untouched except the two nullable columns. Fail-closed throughout: unprovable
provenance excludes a run; a missing/incomplete window fails the machine gate. F4 untouched (read-only;
no `approved_join`). `@dataclass(frozen=True, slots=True)` + lowercase-snake `StrEnum`; the model split
(Fable implementers, Opus reviews) applies.

## 10. Testing approach

- **Provenance:** a run persists all four flags + the configured commit; a run with a flag off, or with
  `producer_commit` unset, is excluded by `select_window` with the correct reason; existing (NULL-flag)
  rows are excluded. Behaviour-neutrality: considered-set response unchanged with flags in any state.
- **Selector:** reproducible run-set + coverage over a seeded dispatch store; fail-closed on each
  exclusion reason; empty window handled.
- **Drivers:** gold suite matches the live classifier (reuses the 3B.4 gold cases); double-compile is
  stable on a frozen fixture and unstable on an injected divergence; drift driver reports 100% and never
  `current` on `unverifiable`/`incompatible`.
- **Machine gate:** conjunctive — each of the five sub-gates independently fails the verdict; a machine
  PASS requires all five; the dropped sub-gates are not consulted.
- **Endpoint:** platform-admin only (403 otherwise); inputs assembled server-side (a crafted body cannot
  inject counts); PASS and FAIL both render; no writes.
- **PG e2e:** collect a small batch under all-flags-on → `POST /gate/evaluate` → machine verdict +
  coverage + population view, over real Postgres.

## 11. Scope boundary / non-goals (all of these are 3C.2 or later)

- No new **live-grounding** flag; no surfacing of governed cross-catalog plans to users.
- No removal of / no change to `find_cross_catalog_path`; the live path is untouched.
- No signing, keys, KMS, or signed artifact; no formal reviewer labeling/adjudication.
- No response-contract change to `/contract/considered-set`.
- No authoring surfaces (aggregation/temporal declaration capture is Phase 3D — it moves recipes
  unresolved→resolved; 3C.1 only *measures* the current unresolved population).

## 12. Handoff to 3C.2

3C.2 (separate spec/review/release) consumes a **human go-live decision** made from 3C.1's results, and
then: adds a new live-grounding flag (default off), surfaces the governed planner output for a
controlled scope, compares live vs prior behavior, and retires `find_cross_catalog_path` from governing
decisions (no fallback from a governed rejection to the permissive path). Because 3C.1 is results-only,
there is **no machine artifact for 3C.2 to verify** — the interlock is the human decision. If 3C.2's own
governance review concludes a machine interlock is required (independent, version-bound, tamper-evident),
that is where the signed-artifact machinery from the original 3B.4 design is (re)introduced and its
consumption contract defined — deferred here by design, not forgotten.

# Phase 3C.1 — Gate Operationalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give an operator an authority-only way to run 3B.4's machine-checkable trustworthiness sub-gates over an immutable batch of persisted shadow feature-generation runs and read a PASS/FAIL plus an honest population view on an internal console — results only, admin decides. No signing, no formal labeling.

**Architecture:** A read-only evaluation path: extend run provenance (all four intent flags + real producer commit per shadow run) → select an immutable batch by cohort + flag-provenance + date range (fail-closed) → run the controlled machine checks (gold suite, double-compile, drift) in rolled-back transactions + build the §9 population report over the batch → evaluate a 5-gate machine conjunction → return the verdict + coverage + population view to a platform-admin console. Everything is computed server-side from the write-once telemetry store; the browser only triggers and displays.

**Tech Stack:** Python 3.11, FastAPI, psycopg 3, Postgres (one additive nullable migration), React + Vite + Vitest. Reuses the 3B.4 planner package: `shadow_store`, `shadow_report`, `contract_gold`, `contract_eval`, `replay`.

## Global Constraints

- **Behaviour-neutral:** the considered-set (`POST /contract/considered-set`) response stays **byte-identical**; the intent flags are still read **only in the route** (the planner stays pure). Capturing more provenance changes no response.
- **NO data plane:** the report describes feature definitions, never computed values.
- **Fail-closed throughout:** a run that cannot *prove* all four flags + a known cohort is EXCLUDED from every window and reported as excluded; an empty/all-excluded window FAILS the machine gate (no evidence is not a pass).
- **Results-only:** NO signing, NO certificate, NO keys, NO formal reviewer label store / adjudication. A machine PASS is **necessary-but-not-sufficient**; the admin supplies the real-population judgment.
- **WORM store:** telemetry tables are append-only; the migration is **additive + nullable**; existing rows keep `NULL` provenance = unprovable = excluded. Never `UPDATE`/`DELETE` telemetry.
- **Isolation:** the controlled drivers (gold/double-compile/drift) seed fixtures and MUST run inside a transaction/savepoint that is **rolled back, never committed** — they never persist to the real catalog. `/gate/evaluate` writes no durable state.
- **Authority-only:** the new endpoints are guarded by `require_confirmer` (the raw `platform-admin` role claim); they are NOT on the customer path. Inputs are assembled server-side from persisted stores — the request body carries only a batch identifier, never counts/verdicts.
- Contracts are `@dataclass(frozen=True, slots=True)` + lowercase-snake `StrEnum`. Commit trailer: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- The next free migration number is **1000** (max on `main` is `0999`). The runner globs `db/migrations/*.sql` in lexical order.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `src/featuregen/db/migrations/1000_dispatch_flag_provenance.sql` | add nullable `scoped_applicability_flag`, `ranking_flag` to `planner_shadow_dispatch` | 1 |
| `src/featuregen/overlay/upload/planner/shadow_store.py` (modify) | `DispatchRecordV1` +2 flag fields; `write_dispatch`/`read` carry them | 1 |
| `src/featuregen/overlay/upload/planner/shadow_capture.py` (modify) | `build_dispatch` takes 4 flags + real `producer_commit`; remove `"dev"` placeholder | 1 |
| `src/featuregen/config.py` (modify) | `producer_commit` from `FEATUREGEN_PRODUCER_COMMIT` | 1 |
| `src/featuregen/overlay/upload/planner/shadow.py` (modify) | `run_shadow_planner` takes + forwards the 2 new flags | 1 |
| `src/featuregen/api/routes/contract.py` (modify) | thread all four flags into `run_shadow_planner` | 1 |
| `src/featuregen/overlay/upload/planner/gate_operate.py` (create) | `select_window` + the three controlled drivers | 2, 3 |
| `src/featuregen/overlay/upload/planner/contract_gold.py` (modify) | `compile_gold_case` → `CompileVerdict` (for double-compile) | 3 |
| `src/featuregen/overlay/upload/planner/shadow_report.py` (modify) | `evaluate_machine_gate` (5-gate conjunction) | 4 |
| `src/featuregen/api/routes/gate.py` (create) | `POST /gate/evaluate` + `GET /gate/cohorts` (platform-admin) | 5 |
| `src/featuregen/api/app.py` (modify) | register the gate router | 5 |
| `frontend/src/screens/GateEvaluationScreen.tsx` (create) | internal admin console (Vite flag) | 6 |
| `frontend/src/api.ts` (modify) | `evaluateGate` / `listGateCohorts` client fns | 6 |
| `frontend/src/App.tsx` + `nav.ts` (modify) | register the screen behind `VITE_INTENT_GATE_CONSOLE` | 6 |

---

## Task 1: Run-provenance capture (4 flags + real producer commit)

**Files:**
- Create: `src/featuregen/db/migrations/1000_dispatch_flag_provenance.sql`
- Modify: `src/featuregen/overlay/upload/planner/shadow_store.py` (`DispatchRecordV1`, `write_dispatch`, `read`), `src/featuregen/overlay/upload/planner/shadow_capture.py` (`build_dispatch`, `PRODUCER_COMMIT`), `src/featuregen/config.py`, `src/featuregen/overlay/upload/planner/shadow.py` (`run_shadow_planner`), `src/featuregen/api/routes/contract.py`
- Test: `tests/featuregen/db/test_migration_1000.py`, `tests/featuregen/overlay/upload/planner/test_shadow_capture.py` (extend), `tests/featuregen/api/test_contract_ranked.py` (behaviour-neutral assertion)

**Interfaces:**
- Produces: `DispatchRecordV1` gains `scoped_applicability_flag: bool | None`, `ranking_flag: bool | None`. `build_dispatch(*, run_id, eligible_recipe_ids, compile_flag, telemetry_flag, scoped_applicability_flag, ranking_flag, now) -> DispatchRecordV1`. `run_shadow_planner(..., scoped_applicability: bool = False, ranking: bool = False)`. Dispatch reader dicts gain `scoped_applicability_flag`, `ranking_flag`. `get_settings().producer_commit: str`.
- Consumes: existing `write_dispatch(conn, DispatchRecordV1)`, `contract.py::_intent_ranking_enabled()`, `gate1._intent_scoped_applicability_enabled()`.

- [ ] **Step 1: Write the failing migration test**

Add to `tests/featuregen/db/test_migration_1000.py`:

```python
from __future__ import annotations


def _cols(db, table):
    return {r[0] for r in db.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,)).fetchall()}


def test_1000_adds_nullable_flag_provenance_columns(db):
    cols = _cols(db, "planner_shadow_dispatch")
    assert {"scoped_applicability_flag", "ranking_flag"} <= cols


def test_1000_flag_columns_are_nullable(db):
    rows = db.execute(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'planner_shadow_dispatch' "
        "AND column_name IN ('scoped_applicability_flag','ranking_flag')").fetchall()
    assert {c: n for c, n in rows} == {"scoped_applicability_flag": "YES", "ranking_flag": "YES"}


def test_1000_existing_rows_carry_null_provenance_and_a_new_row_can_set_it(db):
    # a legacy-shaped insert (no new columns) leaves them NULL = unprovable = fail-closed exclusion
    db.execute(
        "INSERT INTO planner_shadow_dispatch (generation_run_id, eligible_recipe_ids, recipe_hash,"
        " expected_count, invocation_predicate, compile_flag, telemetry_flag, applicability_version,"
        " producer_commit, compiler_versions, compiler_versions_hash, payload_schema_version)"
        " VALUES ('legacy', '{}', 'h', 0, 'p', true, true, 'v', 'c', '{}', 'ch', 'pv')")
    row = db.execute("SELECT scoped_applicability_flag, ranking_flag FROM planner_shadow_dispatch"
                     " WHERE generation_run_id='legacy'").fetchone()
    assert row == (None, None)
```

- [ ] **Step 2: Run it, verify it fails**

Run: `uv run pytest tests/featuregen/db/test_migration_1000.py -q`
Expected: FAIL (columns do not exist).

- [ ] **Step 3: Write the migration**

Create `src/featuregen/db/migrations/1000_dispatch_flag_provenance.sql`:

```sql
-- src/featuregen/db/migrations/1000_dispatch_flag_provenance.sql
-- Phase 3C.1 run provenance: record the scoped-applicability + ranking flag state on each shadow
-- dispatch (compile + telemetry were already recorded). NULLABLE by design — existing rows and any
-- run whose route did not record them carry NULL = "unprovable", which the 3C.1 window selector
-- treats as a fail-closed exclusion. New rows write actual booleans. WORM: dispatch stays append-only
-- (write-once); this migration only ADDS columns and never relaxes the 0971 revoke posture.
ALTER TABLE planner_shadow_dispatch ADD COLUMN IF NOT EXISTS scoped_applicability_flag boolean NULL;
ALTER TABLE planner_shadow_dispatch ADD COLUMN IF NOT EXISTS ranking_flag             boolean NULL;
```

- [ ] **Step 4: Run the migration test, verify it passes**

Run: `uv run pytest tests/featuregen/db/test_migration_1000.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Extend `DispatchRecordV1` + writer + reader**

In `src/featuregen/overlay/upload/planner/shadow_store.py`, add the two fields to `DispatchRecordV1` (after `telemetry_flag`):

```python
    compile_flag: bool
    telemetry_flag: bool
    scoped_applicability_flag: bool | None
    ranking_flag: bool | None
    applicability_version: str
```

Update `write_dispatch`'s INSERT column list + values to include `scoped_applicability_flag, ranking_flag` (place them right after `telemetry_flag` in both the column list and the parameter tuple), and any dispatch reader's `cols` tuple to include the two names. (Search `shadow_store.py` for `telemetry_flag` and mirror each occurrence.)

- [ ] **Step 6: Real producer commit in config**

In `src/featuregen/config.py`, add to `Settings` (after `intent_gate_public_key`):

```python
    # The producing code version stamped on each shadow run's dispatch manifest (the "cohort" the 3C.1
    # gate windows over). Set at deploy (e.g. the git SHA). Unset -> the sentinel "unset", which the
    # window selector treats as an uncertified cohort (fail-closed exclusion).
    producer_commit: str
```

and in `from_env`:

```python
            producer_commit=os.environ.get("FEATUREGEN_PRODUCER_COMMIT", "unset"),
```

- [ ] **Step 7: `build_dispatch` takes the flags + real commit**

In `src/featuregen/overlay/upload/planner/shadow_capture.py`: delete `PRODUCER_COMMIT = "dev"`. Change `build_dispatch` to:

```python
def build_dispatch(*, run_id: str | None, eligible_recipe_ids: frozenset[str], compile_flag: bool,
                   telemetry_flag: bool, scoped_applicability_flag: bool, ranking_flag: bool, now
                   ) -> DispatchRecordV1:
    ids = tuple(sorted(eligible_recipe_ids))
    return DispatchRecordV1(
        generation_run_id=run_id, eligible_recipe_ids=ids, recipe_hash=payload_hash(list(ids)),
        expected_count=len(ids), invocation_predicate=INVOCATION_PREDICATE,
        compile_flag=compile_flag, telemetry_flag=telemetry_flag,
        scoped_applicability_flag=scoped_applicability_flag, ranking_flag=ranking_flag,
        applicability_version=APPLICABILITY_MAPPING_VERSION,
        producer_commit=get_settings().producer_commit, compiler_versions=dict(_COMPILER_VERSIONS),
        created_at=now)
```

Add `from featuregen.config import get_settings` to the imports. (Keep the existing `recipe_hash`/`compiler_versions`/`invocation_predicate` expressions exactly as they are today — only the flag args, `producer_commit`, and the two new fields change.)

- [ ] **Step 8: Thread the flags through `run_shadow_planner`**

In `src/featuregen/overlay/upload/planner/shadow.py`, add params to `run_shadow_planner` (after `persist`):

```python
                       persist: bool = False,
                       scoped_applicability: bool = False,
                       ranking: bool = False,
                       monotonic: Callable[[], float] = time.monotonic
```

and in the `if persist:` dispatch write, pass them:

```python
        write_dispatch(conn, build_dispatch(run_id=run_id, eligible_recipe_ids=eligible_recipe_ids,
                                            compile_flag=compile_contracts, telemetry_flag=True,
                                            scoped_applicability_flag=scoped_applicability,
                                            ranking_flag=ranking, now=now))
```

- [ ] **Step 9: Thread the flags from the route**

In `src/featuregen/api/routes/contract.py`, add the scoped import near the other planner imports:

```python
from featuregen.overlay.upload.contract.gate1 import _intent_scoped_applicability_enabled
```

and extend the `run_shadow_planner(...)` call in `_scoped_considered_set` with:

```python
                                   scoped_applicability=_intent_scoped_applicability_enabled(),
                                   ranking=_intent_ranking_enabled(),
```

- [ ] **Step 10: Extend the capture test + a behaviour-neutral assertion**

In `tests/featuregen/overlay/upload/planner/test_shadow_capture.py`, add:

```python
def test_dispatch_records_all_four_flags_and_the_configured_commit(db, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_PRODUCER_COMMIT", "sha-abc")
    _catalog(db, "core")
    run_shadow_planner(db, eligible_recipe_ids=frozenset({"t_bal"}), target_entity="customer",
                       roles=(), run_id="prov", now=_NOW, templates=(_tmpl(),), persist=True,
                       scoped_applicability=True, ranking=True)
    row = db.execute("SELECT scoped_applicability_flag, ranking_flag, producer_commit FROM "
                     "planner_shadow_dispatch WHERE generation_run_id='prov'").fetchone()
    assert row == (True, True, "sha-abc")
```

Confirm the existing considered-set route test still passes byte-identically (the response must not change): `uv run pytest tests/featuregen/api/test_contract_ranked.py -q`.

- [ ] **Step 11: Run tests + gates**

Run: `uv run pytest tests/featuregen/db/test_migration_1000.py tests/featuregen/overlay/upload/planner/test_shadow_capture.py tests/featuregen/api/test_contract_ranked.py -q`
Then: `uv run ruff check src/featuregen/ && uv run mypy src/featuregen/overlay/upload/planner/shadow_capture.py src/featuregen/overlay/upload/planner/shadow_store.py src/featuregen/overlay/upload/planner/shadow.py src/featuregen/config.py`
Expected: all PASS / clean.

- [ ] **Step 12: Commit**

```bash
git add -A && git commit -m "feat(3c1): run-provenance capture — 4 flags + real producer commit (task 1)"
```

---

## Task 2: Immutable batch selection (`select_window`)

**Files:**
- Create: `src/featuregen/overlay/upload/planner/gate_operate.py`
- Test: `tests/featuregen/overlay/upload/planner/test_gate_operate.py`

**Interfaces:**
- Consumes: `planner_shadow_dispatch` columns from Task 1.
- Produces: `WindowSelection` (frozen: `run_ids: tuple[str, ...]`, `coverage: CoverageReport`), `CoverageReport` (frozen: `dispatched_in_range: int`, `qualifying: int`, `excluded: dict[str, int]`), `select_window(conn, *, cohort: str, since: datetime, until: datetime) -> WindowSelection`.

- [ ] **Step 1: Write the failing test**

Create `tests/featuregen/overlay/upload/planner/test_gate_operate.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime

from featuregen.overlay.upload.planner.gate_operate import select_window

_T0 = datetime(2026, 7, 18, tzinfo=UTC)


def _dispatch(db, rid, *, cohort="sha1", compile=True, telem=True, scoped=True, ranking=True,
              at=_T0):
    db.execute(
        "INSERT INTO planner_shadow_dispatch (generation_run_id, eligible_recipe_ids, recipe_hash,"
        " expected_count, invocation_predicate, compile_flag, telemetry_flag, scoped_applicability_flag,"
        " ranking_flag, applicability_version, producer_commit, compiler_versions, compiler_versions_hash,"
        " payload_schema_version, created_at) VALUES (%s,'{}','h',0,'p',%s,%s,%s,%s,'v',%s,'{}','ch','pv',%s)",
        (rid, compile, telem, scoped, ranking, cohort, at))


def test_only_fully_qualifying_runs_are_selected(db):
    _dispatch(db, "ok1")
    _dispatch(db, "ok2")
    _dispatch(db, "no_scope", scoped=False)          # a flag off -> excluded
    _dispatch(db, "null_rank", ranking=None)         # unprovable (NULL) -> excluded
    _dispatch(db, "other_cohort", cohort="sha2")     # wrong cohort -> excluded
    _dispatch(db, "uncertified", cohort="unset")     # sentinel cohort is never selectable
    sel = select_window(db, cohort="sha1", since=_T0, until=datetime(2026, 7, 19, tzinfo=UTC))
    assert set(sel.run_ids) == {"ok1", "ok2"}
    assert sel.coverage.qualifying == 2
    assert sel.coverage.excluded["flag_off"] == 1
    assert sel.coverage.excluded["flag_unprovable"] == 1
    assert sel.coverage.excluded["wrong_cohort"] == 2  # other_cohort + uncertified are not this cohort


def test_out_of_range_runs_are_excluded(db):
    _dispatch(db, "inrange", at=_T0)
    _dispatch(db, "before", at=datetime(2026, 7, 1, tzinfo=UTC))
    sel = select_window(db, cohort="sha1", since=_T0, until=datetime(2026, 7, 19, tzinfo=UTC))
    assert set(sel.run_ids) == {"inrange"}


def test_empty_window_is_reproducible_and_empty(db):
    sel = select_window(db, cohort="ghost", since=_T0, until=datetime(2026, 7, 19, tzinfo=UTC))
    assert sel.run_ids == () and sel.coverage.qualifying == 0
```

- [ ] **Step 2: Run it, verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/planner/test_gate_operate.py -q`
Expected: FAIL (`gate_operate` does not exist).

- [ ] **Step 3: Implement `select_window`**

Create `src/featuregen/overlay/upload/planner/gate_operate.py`:

```python
"""Phase-3C.1 — the read-only gate-operationalization harness: select an immutable batch of shadow
runs (fail-closed on provenance), and run the controlled machine checks. No durable writes; the
controlled drivers seed fixtures inside a rolled-back transaction and never touch the real catalog."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """Honest denominator over the requested range: how many runs were dispatched, how many qualified,
    and — for every run that did NOT qualify — the reason it was excluded (fail-closed)."""

    dispatched_in_range: int
    qualifying: int
    excluded: dict[str, int]


@dataclass(frozen=True, slots=True)
class WindowSelection:
    run_ids: tuple[str, ...]
    coverage: CoverageReport


def select_window(conn, *, cohort: str, since: datetime, until: datetime) -> WindowSelection:
    """The immutable batch for a gate run: dispatched in ``[since, until)``, produced by ``cohort`` (never
    the ``unset`` sentinel), with ALL FOUR intent flags provably TRUE. A run missing any flag (FALSE) or
    whose flag is NULL (unprovable — legacy / pre-3C.1) is EXCLUDED and counted. Reproducible: the same
    args over the write-once store return the identical set + coverage."""
    rows = conn.execute(
        "SELECT generation_run_id, producer_commit, compile_flag, telemetry_flag,"
        " scoped_applicability_flag, ranking_flag FROM planner_shadow_dispatch"
        " WHERE created_at >= %s AND created_at < %s ORDER BY generation_run_id", (since, until)).fetchall()
    run_ids: list[str] = []
    excluded: dict[str, int] = {"wrong_cohort": 0, "flag_unprovable": 0, "flag_off": 0}
    for rid, commit, compile_f, telem_f, scoped_f, rank_f in rows:
        if commit != cohort or cohort == "unset":
            excluded["wrong_cohort"] += 1
            continue
        flags = (compile_f, telem_f, scoped_f, rank_f)
        if any(f is None for f in flags):
            excluded["flag_unprovable"] += 1
            continue
        if not all(flags):
            excluded["flag_off"] += 1
            continue
        run_ids.append(rid)
    return WindowSelection(
        run_ids=tuple(run_ids),
        coverage=CoverageReport(dispatched_in_range=len(rows), qualifying=len(run_ids), excluded=excluded))
```

- [ ] **Step 4: Run the test, verify it passes**

Run: `uv run pytest tests/featuregen/overlay/upload/planner/test_gate_operate.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(3c1): immutable batch selection with fail-closed coverage (task 2)"
```

---

## Task 3: The controlled machine-check drivers (gold, double-compile, drift)

**Files:**
- Modify: `src/featuregen/overlay/upload/planner/gate_operate.py`, `src/featuregen/overlay/upload/planner/contract_gold.py`
- Test: `tests/featuregen/overlay/upload/planner/test_gate_operate.py` (extend)

**Interfaces:**
- Consumes: `contract_gold.GOLD_CASES`, `contract_gold.run_gold_case`, `contract_gold._seed`, `contract_gold._build_plan`, `contract_gold.build_compiler_context`/`compile_contract` path; `contract_eval.evaluate`, `contract_eval.EvalReport`, `contract_eval.CompileVerdict`, `contract_eval.StabilityResult`, `contract_eval.double_compile_stable`; `replay.compare`, `replay.StoredEvidenceV1`, `replay.CurrentEvidenceV1`; `fingerprint._VERSIONS`.
- Produces: `contract_gold.compile_gold_case(conn, case) -> CompileVerdict`; `gate_operate.run_gold_suite(conn) -> EvalReport`, `run_double_compile(conn) -> StabilityResult`, `run_drift_checks(conn) -> float` (the detected fraction; 1.0 = all mutation classes detected), and `_rolled_back(conn)` context manager.

- [ ] **Step 1: Write the failing tests**

Add to `tests/featuregen/overlay/upload/planner/test_gate_operate.py`:

```python
from featuregen.overlay.upload.planner.gate_operate import (
    run_double_compile,
    run_drift_checks,
    run_gold_suite,
)


def test_gold_suite_matches_the_live_classifier(db):
    report = run_gold_suite(db)
    assert report.passed and report.false_resolves == ()


def test_double_compile_is_stable_on_the_frozen_gold_fixtures(db):
    result = run_double_compile(db)
    assert result.stable and result.compared >= 1 and result.mismatched_keys == ()


def test_drift_checks_detect_every_controlled_mutation(db):
    assert run_drift_checks(db) == 1.0


def test_drivers_leave_no_durable_catalog_state(db):
    # the controlled drivers seed 'core' but roll it back — no rows survive
    run_gold_suite(db)
    run_double_compile(db)
    run_drift_checks(db)
    remaining = db.execute("SELECT count(*) FROM graph_node WHERE catalog_source = 'core'").fetchone()[0]
    assert remaining == 0
```

- [ ] **Step 2: Run them, verify they fail**

Run: `uv run pytest tests/featuregen/overlay/upload/planner/test_gate_operate.py -q -k "gold_suite or double_compile or drift or durable"`
Expected: FAIL (functions not defined).

- [ ] **Step 3: Add `compile_gold_case` to `contract_gold.py`**

In `src/featuregen/overlay/upload/planner/contract_gold.py`, add (importing `CompileVerdict` from `contract_eval` and `CompileStatus` from `shadow_store` at the top):

```python
def compile_gold_case(conn, case: GoldCase, *, seed: Callable[[object], None] = _seed) -> CompileVerdict:
    """Compile one gold case through the REAL pipeline and return its verdict as a CompileVerdict for the
    double-compile determinism check (compile_status is complete because the case's plan is
    source_to_target_resolved and is compiled here)."""
    seed(conn)
    scope = resolve_catalog_scope(conn, roles=(), target_entity="customer", now=_GOLD_NOW)
    ctx = build_compiler_context(conn, scope, (), _GOLD_NOW)
    if case.agg:
        ctx = dataclasses.replace(ctx, agg_declarations=dict(case.agg))
    plan = _build_plan(ctx, case)
    compiled = compile_contract(conn, ctx, plan, _TEMPLATE,
                                base_envelope=_envelope(conn, scope, case.case_id, "customer"))
    return CompileVerdict(key=case.case_id, compile_status=CompileStatus.complete,
                          contract_id=compiled.contract_id, declaration_status=str(compiled.declaration_status))
```

- [ ] **Step 4: Implement the drivers + rollback isolation in `gate_operate.py`**

Add to `src/featuregen/overlay/upload/planner/gate_operate.py`:

```python
import contextlib

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.catalog_realizations import derive_catalog_realizations
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.planner.contract_eval import (
    EvalReport,
    StabilityResult,
    double_compile_stable,
    evaluate,
)
from featuregen.overlay.upload.planner.contract_gold import (
    GOLD_CASES,
    compile_gold_case,
    run_gold_case,
)
from featuregen.overlay.upload.planner.fingerprint import _VERSIONS, compiler_input_fingerprint
from featuregen.overlay.upload.planner.replay import CurrentEvidenceV1, StoredEvidenceV1, compare
from featuregen.overlay.upload.templates import _load_columns


class _Rollback(Exception):
    pass


@contextlib.contextmanager
def _rolled_back(conn):
    """Run a controlled driver's fixture seeding inside a transaction that is ALWAYS rolled back — the
    computed Python result survives (it is in memory); the seeded catalog rows never persist."""
    try:
        with conn.transaction():
            yield
            raise _Rollback
    except _Rollback:
        pass


def run_gold_suite(conn) -> EvalReport:
    """Every GOLD_CASES case vs the expert answer key — the false-resolve teeth. Rolled back."""
    triples: list = []
    with _rolled_back(conn):
        triples = [run_gold_case(conn, case) for case in GOLD_CASES]
    return evaluate(triples)


def run_double_compile(conn) -> StabilityResult:
    """Compile each frozen gold fixture TWICE and compare — proves the classifier is deterministic
    (identity-comparable verdicts only; empty => unstable). Rolled back."""
    first: list = []
    second: list = []
    for case in GOLD_CASES:
        with _rolled_back(conn):
            first.append(compile_gold_case(conn, case))
        with _rolled_back(conn):
            second.append(compile_gold_case(conn, case))
    return double_compile_stable(first, second)


_DRIFT_SEED = [
    (CanonicalRow("core", "accounts", "account_id", "integer", is_grain=True), "account_id"),
    (CanonicalRow("core", "accounts", "balance", "numeric"), "monetary_stock"),
]


def _fingerprint(conn) -> str:
    from types import SimpleNamespace
    mini = SimpleNamespace(
        columns_by_catalog={"core": {c.object_ref: c for c in _load_columns(conn, "core", ())}},
        realizations_by_catalog={"core": derive_catalog_realizations(conn, "core").realizations})
    return compiler_input_fingerprint(mini, "core")


def run_drift_checks(conn) -> float:
    """Fraction of controlled mutation classes the replay comparator detects (must be 1.0). Each class
    mutates a seeded catalog and asserts compare(...) is NOT `current`. Rolled back."""
    from featuregen.overlay.upload.planner.contracts import ReplayFreshness
    detected = 0
    classes = ("additivity_rebuild", "version_bump")
    for cls in classes:
        with _rolled_back(conn):
            build_graph(conn, "core", [r for r, _ in _DRIFT_SEED],
                        concepts={content_hash(r): cn for r, cn in _DRIFT_SEED})
            stored = StoredEvidenceV1(fingerprints={"core": _fingerprint(conn)},
                                      head_seqs={"core": 3}, versions=_VERSIONS)
            if cls == "additivity_rebuild":
                mutated = [
                    (CanonicalRow("core", "accounts", "account_id", "integer", is_grain=True), "account_id"),
                    (CanonicalRow("core", "accounts", "balance", "numeric"), "monetary_flow"),  # was stock
                ]
                build_graph(conn, "core", [r for r, _ in mutated],
                            concepts={content_hash(r): cn for r, cn in mutated})
                cur = CurrentEvidenceV1(fingerprints={"core": _fingerprint(conn)},
                                        head_seqs={"core": 3}, checkpoint=100, versions=_VERSIONS)
            else:  # version_bump: the producer/compiler version set changed
                cur = CurrentEvidenceV1(fingerprints={"core": _fingerprint(conn)}, head_seqs={"core": 3},
                                        checkpoint=100, versions=(*_VERSIONS, ("extra", "9.9.9")))
            if compare(stored, cur) is not ReplayFreshness.current:
                detected += 1
    return detected / len(classes)
```

- [ ] **Step 5: Run the tests, verify they pass**

Run: `uv run pytest tests/featuregen/overlay/upload/planner/test_gate_operate.py -q`
Expected: PASS (all).

- [ ] **Step 6: Gates + commit**

Run: `uv run ruff check src/featuregen/overlay/upload/planner/gate_operate.py src/featuregen/overlay/upload/planner/contract_gold.py && uv run mypy src/featuregen/overlay/upload/planner/gate_operate.py`

```bash
git add -A && git commit -m "feat(3c1): controlled machine-check drivers — gold/double-compile/drift, rolled back (task 3)"
```

---

## Task 4: The machine-only gate evaluator (`evaluate_machine_gate`)

**Files:**
- Modify: `src/featuregen/overlay/upload/planner/shadow_report.py`
- Test: `tests/featuregen/overlay/upload/planner/test_shadow_report.py` (extend)

**Interfaces:**
- Consumes: `PopulationReportV1`, `_gate1(report)`, `assert_map_exhaustive`, `EvalReport`, `StabilityResult`.
- Produces: `MachineGateResult` (frozen: `gate1_capture: bool`, `gate2a_map: bool`, `gate3_gold: bool`, `gate5_stability: bool`, `gate6_drift: bool`, `reasons: tuple[str, ...]`; `@property passed`), `evaluate_machine_gate(*, report: PopulationReportV1, gold_report: EvalReport, stability: StabilityResult, drift_ratio: float) -> MachineGateResult`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/featuregen/overlay/upload/planner/test_shadow_report.py`:

```python
from featuregen.overlay.upload.planner.contract_eval import CaseResult, EvalReport, StabilityResult
from featuregen.overlay.upload.planner.shadow_report import evaluate_machine_gate


def _machine_inputs(**over):
    base = dict(report=_report(), gold_report=EvalReport(results=(CaseResult("c1", True, False, ()),)),
                stability=StabilityResult(stable=True, compared=3, mismatched_keys=()), drift_ratio=1.0)
    base.update(over)
    return base


def test_machine_gate_passes_when_all_five_hold():
    assert evaluate_machine_gate(**_machine_inputs()).passed


def test_machine_gate_fails_on_empty_population():
    res = evaluate_machine_gate(**_machine_inputs(report=_report(denominator=0)))
    assert not res.gate1_capture and not res.passed   # no evidence is not a pass


import pytest


@pytest.mark.parametrize("over", [
    {"report": _report(incomplete_count=1)},           # capture integrity
    {"report": _report(operationally_unmeasured_count=1)},  # map exhaustiveness
    {"gold_report": EvalReport(results=(CaseResult("c1", False, True, ("x",)),))},  # gold false-resolve
    {"stability": StabilityResult(stable=False, compared=0, mismatched_keys=())},   # double-compile
    {"drift_ratio": 0.5},                              # drift
])
def test_each_machine_sub_gate_failure_fails_the_verdict(over):
    assert not evaluate_machine_gate(**_machine_inputs(**over)).passed
```

(Note: `_report(...)` is the existing helper in this test file; it already accepts `denominator`, `incomplete_count`, `operationally_unmeasured_count` as keyword overrides.)

- [ ] **Step 2: Run them, verify they fail**

Run: `uv run pytest tests/featuregen/overlay/upload/planner/test_shadow_report.py -q -k machine`
Expected: FAIL (`evaluate_machine_gate` not defined).

- [ ] **Step 3: Implement `evaluate_machine_gate`**

Add to `src/featuregen/overlay/upload/planner/shadow_report.py` (after `evaluate_gate`):

```python
@dataclass(frozen=True, slots=True)
class MachineGateResult:
    """The 3C.1 machine-only verdict: the five MACHINE-checkable sub-gates ANDed (no averaging). This is
    NECESSARY-BUT-NOT-SUFFICIENT for trustworthiness — the human-review sub-gates (2b/3-audit/4) and the
    signed artifact (7) are deliberately NOT evaluated in 3C.1; the admin supplies the real-population
    judgment from the population view."""

    gate1_capture: bool
    gate2a_map: bool
    gate3_gold: bool
    gate5_stability: bool
    gate6_drift: bool
    reasons: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return all((self.gate1_capture, self.gate2a_map, self.gate3_gold,
                    self.gate5_stability, self.gate6_drift))


def evaluate_machine_gate(*, report: PopulationReportV1, gold_report: EvalReport,
                          stability: StabilityResult, drift_ratio: float) -> MachineGateResult:
    """The conjunctive machine-only gate (3C.1). Fail-closed: an empty qualifying population fails Gate 1
    (no evidence is not a pass)."""
    reasons: list[str] = []
    g1, r1 = _gate1(report)
    gate1 = g1 and report.denominator > 0
    reasons += r1
    if report.denominator == 0:
        reasons.append("Gate 1: empty qualifying population (no evidence)")

    try:
        assert_map_exhaustive()
        map_ok = True
    except AssertionError as exc:
        map_ok = False
        reasons.append(f"Gate 2a: {exc}")
    gate2a = map_ok and report.operationally_unmeasured_count == 0
    if report.operationally_unmeasured_count:
        reasons.append(f"Gate 2a: {report.operationally_unmeasured_count} operationally_unmeasured")

    gate3 = gold_report.passed
    if not gate3:
        reasons.append(f"Gate 3 (gold): failures {gold_report.false_resolves}")
    gate5 = stability.stable
    if not gate5:
        reasons.append(f"Gate 5: replay unstable (compared={stability.compared})")
    gate6 = drift_ratio >= 1.0
    if not gate6:
        reasons.append(f"Gate 6: drift detection {drift_ratio:.3f} < 1.0")

    return MachineGateResult(gate1_capture=gate1, gate2a_map=gate2a, gate3_gold=gate3,
                             gate5_stability=gate5, gate6_drift=gate6, reasons=tuple(reasons))
```

Add `MachineGateResult` and `evaluate_machine_gate` to `__all__`.

- [ ] **Step 4: Run the tests, verify they pass**

Run: `uv run pytest tests/featuregen/overlay/upload/planner/test_shadow_report.py -q -k machine`
Expected: PASS.

- [ ] **Step 5: Gates + commit**

Run: `uv run ruff check src/featuregen/overlay/upload/planner/shadow_report.py && uv run mypy src/featuregen/overlay/upload/planner/shadow_report.py`

```bash
git add -A && git commit -m "feat(3c1): machine-only 5-gate conjunction (task 4)"
```

---

## Task 5: Authority-only evaluation endpoint (`/gate/evaluate`, `/gate/cohorts`)

**Files:**
- Create: `src/featuregen/api/routes/gate.py`
- Modify: `src/featuregen/api/app.py` (register router)
- Test: `tests/featuregen/api/test_gate_routes.py`

**Interfaces:**
- Consumes: `select_window`, `run_gold_suite`, `run_double_compile`, `run_drift_checks` (Tasks 2-3); `build_population_report` (3B.4); `evaluate_machine_gate` (Task 4); `require_confirmer` (deps).
- Produces: `POST /gate/evaluate` (body `{cohort, since, until}`) → `{verdict: {passed, gate1_capture, ...}, reasons, coverage, population: {...}, versions}`; `GET /gate/cohorts` → `[{cohort, first_run_at, last_run_at, run_count}]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/featuregen/api/test_gate_routes.py` (mirror the auth pattern in `tests/featuregen/api/test_authz.py` — a platform-admin identity vs a non-admin):

```python
from __future__ import annotations

# uses the app TestClient + identity-header fixtures from tests/featuregen/api/conftest.py


def test_gate_evaluate_requires_platform_admin(client, non_admin_headers):
    r = client.post("/gate/evaluate", json={"cohort": "sha1", "since": "2026-07-18T00:00:00Z",
                                            "until": "2026-07-19T00:00:00Z"}, headers=non_admin_headers)
    assert r.status_code == 403


def test_gate_evaluate_empty_window_fails_closed(client, admin_headers):
    r = client.post("/gate/evaluate", json={"cohort": "ghost", "since": "2026-07-18T00:00:00Z",
                                            "until": "2026-07-19T00:00:00Z"}, headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"]["passed"] is False           # no evidence -> fail-closed
    assert body["coverage"]["qualifying"] == 0


def test_gate_cohorts_lists_producer_commits(client, admin_headers, db):
    db.execute(
        "INSERT INTO planner_shadow_dispatch (generation_run_id, eligible_recipe_ids, recipe_hash,"
        " expected_count, invocation_predicate, compile_flag, telemetry_flag, scoped_applicability_flag,"
        " ranking_flag, applicability_version, producer_commit, compiler_versions, compiler_versions_hash,"
        " payload_schema_version) VALUES ('r','{}','h',0,'p',true,true,true,true,'v','sha1','{}','ch','pv')")
    r = client.get("/gate/cohorts", headers=admin_headers)
    assert r.status_code == 200 and any(c["cohort"] == "sha1" for c in r.json())
```

(If `admin_headers`/`non_admin_headers`/`client` fixtures do not already exist in `tests/featuregen/api/conftest.py`, add them following the existing identity-stub pattern used by `test_authz.py`; the platform-admin identity must carry the raw `platform-admin` role claim.)

- [ ] **Step 2: Run them, verify they fail**

Run: `uv run pytest tests/featuregen/api/test_gate_routes.py -q`
Expected: FAIL (404 — router not registered).

- [ ] **Step 3: Implement the route**

Create `src/featuregen/api/routes/gate.py`:

```python
"""Phase-3C.1 — the authority-only gate-operationalization endpoints. Platform-admin only, OFF the
customer path, read-only: the body carries only a batch identifier; every count/verdict is assembled
server-side from the persisted WORM stores (never the request body)."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from featuregen.api.deps import get_conn, require_confirmer
from featuregen.overlay.upload.planner.gate_operate import (
    run_double_compile,
    run_drift_checks,
    run_gold_suite,
    select_window,
)
from featuregen.overlay.upload.planner.shadow_report import (
    EVALUATOR_VERSION,
    build_population_report,
    evaluate_machine_gate,
)

router = APIRouter()
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]


class EvaluateIn(BaseModel):
    cohort: str
    since: datetime
    until: datetime


@router.post("/gate/evaluate", dependencies=[Depends(require_confirmer)])
def evaluate(body: EvaluateIn, conn: _Conn) -> dict:
    window = select_window(conn, cohort=body.cohort, since=body.since, until=body.until)
    report = build_population_report(conn, window.run_ids)
    gold = run_gold_suite(conn)
    stability = run_double_compile(conn)
    drift = run_drift_checks(conn)
    verdict = evaluate_machine_gate(report=report, gold_report=gold, stability=stability, drift_ratio=drift)
    return {
        "verdict": {"passed": verdict.passed, "gate1_capture": verdict.gate1_capture,
                    "gate2a_map": verdict.gate2a_map, "gate3_gold": verdict.gate3_gold,
                    "gate5_stability": verdict.gate5_stability, "gate6_drift": verdict.gate6_drift},
        "reasons": list(verdict.reasons),
        "necessary_not_sufficient": True,
        "coverage": {"dispatched_in_range": window.coverage.dispatched_in_range,
                     "qualifying": window.coverage.qualifying, "excluded": window.coverage.excluded},
        "population": {"denominator": report.denominator, "numerator": report.numerator,
                       "headline_by_primary": report.headline_by_primary,
                       "breakdown_by_category": report.breakdown_by_category,
                       "recipe_outcome_matrix": report.recipe_outcome_matrix},
        "versions": {"evaluator": EVALUATOR_VERSION, "cohort": body.cohort},
    }


@router.get("/gate/cohorts", dependencies=[Depends(require_confirmer)])
def cohorts(conn: _Conn) -> list[dict]:
    rows = conn.execute(
        "SELECT producer_commit, min(created_at), max(created_at), count(*) FROM planner_shadow_dispatch"
        " WHERE producer_commit <> 'unset' GROUP BY producer_commit ORDER BY max(created_at) DESC").fetchall()
    return [{"cohort": c, "first_run_at": lo.isoformat(), "last_run_at": hi.isoformat(), "run_count": n}
            for c, lo, hi, n in rows]
```

- [ ] **Step 4: Register the router**

In `src/featuregen/api/app.py`, import and include it alongside the other routers:

```python
from featuregen.api.routes import gate as gate_routes
...
app.include_router(gate_routes.router)
```

- [ ] **Step 5: Run the tests, verify they pass**

Run: `uv run pytest tests/featuregen/api/test_gate_routes.py -q`
Expected: PASS.

- [ ] **Step 6: Gates + commit**

Run: `uv run ruff check src/featuregen/api/routes/gate.py && uv run mypy src/featuregen/api/routes/gate.py`

```bash
git add -A && git commit -m "feat(3c1): authority-only /gate/evaluate + /gate/cohorts (task 5)"
```

---

## Task 6: Internal admin console (`GateEvaluationScreen`)

**Files:**
- Create: `frontend/src/screens/GateEvaluationScreen.tsx`, `frontend/src/screens/GateEvaluationScreen.test.tsx`
- Modify: `frontend/src/api.ts`, `frontend/src/App.tsx`, `frontend/src/nav.ts`

**Interfaces:**
- Consumes: `POST /gate/evaluate`, `GET /gate/cohorts` (Task 5).
- Produces: `api.evaluateGate(body)`, `api.listGateCohorts()`; a screen behind `import.meta.env.VITE_INTENT_GATE_CONSOLE === '1'`.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/screens/GateEvaluationScreen.test.tsx` (mirror `GovernanceReviewScreen.test.tsx` for the fetch-mock + render pattern):

```tsx
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { GateEvaluationScreen } from './GateEvaluationScreen'

it('renders a FAIL verdict with the necessary-not-sufficient caveat and coverage', async () => {
  vi.stubGlobal('fetch', vi.fn()
    .mockResolvedValueOnce({ ok: true, json: async () => [{ cohort: 'sha1', first_run_at: 'x', last_run_at: 'y', run_count: 3 }] })
    .mockResolvedValueOnce({ ok: true, json: async () => ({
      verdict: { passed: false, gate1_capture: false, gate2a_map: true, gate3_gold: true, gate5_stability: true, gate6_drift: true },
      reasons: ['Gate 1: empty qualifying population (no evidence)'],
      necessary_not_sufficient: true,
      coverage: { dispatched_in_range: 0, qualifying: 0, excluded: {} },
      population: { denominator: 0, numerator: 0, headline_by_primary: {}, breakdown_by_category: {}, recipe_outcome_matrix: {} },
      versions: { evaluator: '1.0.0', cohort: 'sha1' },
    }) }))
  render(<GateEvaluationScreen />)
  await userEvent.click(await screen.findByRole('button', { name: /evaluate/i }))
  await waitFor(() => expect(screen.getByText(/FAIL/)).toBeInTheDocument())
  expect(screen.getByText(/necessary.*not.*sufficient/i)).toBeInTheDocument()
})
```

- [ ] **Step 2: Run it, verify it fails**

Run: `cd frontend && npx vitest run src/screens/GateEvaluationScreen.test.tsx`
Expected: FAIL (module not found).

- [ ] **Step 3: Add the API client functions**

In `frontend/src/api.ts`, add (mirroring the existing `fetch` helpers):

```ts
export async function listGateCohorts(): Promise<GateCohort[]> {
  const r = await fetch('/gate/cohorts')
  if (!r.ok) throw new Error(`cohorts failed: ${r.status}`)
  return r.json()
}

export async function evaluateGate(body: { cohort: string; since: string; until: string }): Promise<GateEvaluation> {
  const r = await fetch('/gate/evaluate', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })
  if (!r.ok) throw new Error(`evaluate failed: ${r.status}`)
  return r.json()
}
```

with the accompanying `GateCohort` / `GateEvaluation` TypeScript types matching the route's JSON shape (verdict booleans, `reasons`, `necessary_not_sufficient`, `coverage`, `population`, `versions`).

- [ ] **Step 4: Implement the screen**

Create `frontend/src/screens/GateEvaluationScreen.tsx`: a form (cohort select from `listGateCohorts`, since/until date inputs) → **Evaluate** button → renders the verdict as PASS/FAIL, the failed conditions (`reasons`), the coverage table (dispatched/qualifying/excluded-by-reason), and the population view (denominator/numerator, `headline_by_primary`, `breakdown_by_category`). Render a prominent banner: *"A machine PASS is necessary but not sufficient — review the population before deciding to go live."* No sign affordance; the screen only triggers + displays.

- [ ] **Step 5: Register behind the Vite flag**

In `frontend/src/App.tsx` (mirror the `GovernanceReviewScreen` registration) render `<GateEvaluationScreen />` for its nav key only when `import.meta.env.VITE_INTENT_GATE_CONSOLE === '1'`; add the nav entry in `frontend/src/nav.ts` behind the same flag.

- [ ] **Step 6: Run the test, verify it passes**

Run: `cd frontend && npx vitest run src/screens/GateEvaluationScreen.test.tsx`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "feat(3c1): internal gate-evaluation admin console behind VITE_INTENT_GATE_CONSOLE (task 6)"
```

---

## Task 7: PG end-to-end + behaviour-neutral verification

**Files:**
- Test: `tests/featuregen/api/test_gate_routes.py` (extend with an e2e), `.env.example` (document the two new env vars)

**Interfaces:**
- Consumes: the full chain from Tasks 1-5.

- [ ] **Step 1: Write the e2e test**

Add to `tests/featuregen/api/test_gate_routes.py` (collect a real qualifying batch, then evaluate):

```python
def test_gate_e2e_collects_a_batch_and_evaluates(client, admin_headers, db, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_PRODUCER_COMMIT", "sha-e2e")
    # collect one qualifying shadow run (all four flags on) via the planner entrypoint the route uses
    from datetime import UTC, datetime
    from featuregen.overlay.upload.planner.shadow import run_shadow_planner
    from tests.featuregen.overlay.upload.planner.test_shadow_capture import _cross_seed
    from tests.featuregen.overlay.upload.planner.test_plan import _txn_template
    now = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    _cross_seed(db)
    run_shadow_planner(db, eligible_recipe_ids=frozenset({"t_roll"}), target_entity="account",
                       roles=(), run_id="e2e", now=now, templates=(_txn_template(),),
                       compile_contracts=True, persist=True, scoped_applicability=True, ranking=True)
    r = client.post("/gate/evaluate", json={"cohort": "sha-e2e", "since": "2026-07-18T00:00:00Z",
                                            "until": "2026-07-19T00:00:00Z"}, headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["coverage"]["qualifying"] == 1 and body["population"]["denominator"] >= 0
    assert set(body["verdict"]) == {"passed", "gate1_capture", "gate2a_map", "gate3_gold",
                                    "gate5_stability", "gate6_drift"}
```

- [ ] **Step 2: Run it, verify it passes**

Run: `uv run pytest tests/featuregen/api/test_gate_routes.py::test_gate_e2e_collects_a_batch_and_evaluates -q`
Expected: PASS.

- [ ] **Step 3: Document the env vars**

In `.env.example`, add (near the other 3B.4 gate vars):

```
# 3C.1 gate operationalization: the producing code version stamped on each shadow run (the cohort a
# gate window selects over). Set at deploy, e.g. the git SHA. Unset -> "unset" (uncertified, excluded).
# FEATUREGEN_PRODUCER_COMMIT=<git-sha>
# Frontend: enable the internal gate-evaluation admin console (platform-admin only).
# VITE_INTENT_GATE_CONSOLE=1
```

- [ ] **Step 4: Full behaviour-neutral verification**

Run: `uv run pytest tests/featuregen/ tests/db/ -q` (expect all pass / 1 skipped — the considered-set response is byte-identical; only the dispatch manifest carries more provenance).
Run: `uv run ruff check src/featuregen/ && uv run mypy src/featuregen/overlay/upload/planner/ src/featuregen/api/routes/gate.py src/featuregen/config.py`
Then the frontend changed files: `cd frontend && npx vitest run src/screens/GateEvaluationScreen.test.tsx src/api.test.ts`

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "test(3c1): PG e2e route->batch->evaluate + behaviour-neutral verification (task 7)"
```

---

## Notes for the executor

- **Reuse, don't reinvent:** Tasks 3-5 wire existing 3B.4 functions (`build_population_report`, `run_gold_case`/`evaluate`, `double_compile_stable`, `replay.compare`, `_gate1`). Read `shadow_report.py`, `contract_gold.py`, `contract_eval.py`, `replay.py` before implementing.
- **Fail-closed is the invariant to protect:** every exclusion path (unprovable flag, wrong cohort, empty window) must be observable in the coverage/verdict, never a silent drop. If a test can make the gate PASS by dropping failures, it's a bug.
- **Behaviour-neutrality is non-negotiable:** if any considered-set/route test changes output, stop — the provenance capture must be additive only.
- **Model split** ([[prefers-opus-for-subagents]]): Fable implementers/fixers, Opus reviews; set the model explicitly per dispatch.
